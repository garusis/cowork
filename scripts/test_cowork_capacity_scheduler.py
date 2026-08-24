#!/usr/bin/env python3
"""Focused tests for M3 Package D: scheduler lease decisions
(`cowork_capacity_scheduler.py`), built over Package B's lock-protected
PauseLease accessors (`cowork_state.py`) and Package A's pure contracts
(`cowork_capacity.py`).

Run standalone:

    python3 -m unittest scripts/test_cowork_capacity_scheduler.py -v
"""

import ast
import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_capacity as capacity  # noqa: E402
import cowork_capacity_scheduler as scheduler  # noqa: E402
import cowork_state as state_store  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class _SchedEnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test (mirrors test_cowork_state_m3.py's
    _M3EnvMixin), so nothing ever touches the real home dir."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root


# --------------------------------------------------------------------------- #
# Fixture builders (mirror test_cowork_state_m3.py's conventions).           #
# --------------------------------------------------------------------------- #


def _binding(**overrides):
    b = dict(role="builder", provider_session_id="sess-1",
            controller_policy_digest="a" * 64, candidate_digest="b" * 64,
            artifact_hashes={"artifact.txt": "c" * 64})
    b.update(overrides)
    return b


def _make_pause_lease(lease_id=None, failed_wake_attempts=0,
                      consumption_state="unclaimed", **overrides):
    lease = dict(schema_version=1, package_id="pkg-1",
                lease_id=lease_id or _uuid(),
                resume_mode="scheduled", not_before="2024-01-01T00:10:00Z",
                automation_ref="auto-1",
                consumption_state=consumption_state,
                failed_wake_attempts=failed_wake_attempts,
                issued_at="2024-01-01T00:00:00Z")
    lease.update(_binding())
    lease.update(overrides)
    return lease


def _create(session_id, **overrides):
    lease = _make_pause_lease(**overrides)
    return state_store.create_pause_lease(session_id, lease)


def _signed_manual_signal(secret_key=None, key_id="key-1", **overrides):
    """Build a manual-capacity-signal record with a GENUINE Ed25519
    signature, reaching into `cowork_state`'s own self-contained self-test
    signer -- mirrors test_cowork_state_m3.py's `_signed_manual_signal`
    convention exactly. Returns (record, pinned_public_keys)."""
    secret_key = secret_key or hashlib.sha256(os.urandom(32)).digest()
    public_key = state_store._ed25519_selftest_publickey(secret_key)
    record = dict(schema_version=1, package_id="pkg-1",
                 candidate_digest="b" * 64, role="builder",
                 provider_session_id="sess-1",
                 controller_policy_digest="a" * 64,
                 signal_journal_ref="journal-" + _uuid(),
                 signer_public_key_id=key_id,
                 detached_signature="00" * 64,
                 issued_at="2024-01-01T00:00:00Z")
    record.update(overrides)
    message = state_store.canonical_manual_capacity_signal_message(record)
    signature = state_store._ed25519_selftest_sign(message, secret_key, public_key)
    record["detached_signature"] = signature.hex()
    pinned = {key_id: public_key.hex()}
    return record, pinned


# --------------------------------------------------------------------------- #
# Ordinary claim: strict early refusal.                                      #
# --------------------------------------------------------------------------- #


class OrdinaryClaimEarlyRefusalTest(_SchedEnvMixin, unittest.TestCase):
    def test_claim_before_not_before_refused(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(session_id, "lease-1", "worker-a",
                            now="2024-01-01T00:09:59Z", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "early_refusal")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_claim_at_exact_not_before_succeeds(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        result = scheduler.claim(session_id, "lease-1", "worker-a",
                                 now="2024-01-01T00:10:00Z", automation_ref="auto-1")
        self.assertEqual(result["outcome"], "claimed")
        self.assertEqual(result["lease"]["consumption_state"], "claimed")

    def test_claim_after_not_before_succeeds(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        result = scheduler.claim(session_id, "lease-1", "worker-a",
                                 now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        self.assertEqual(result["outcome"], "claimed")

    def test_ordinary_claim_has_no_override_parameter(self):
        """Structural proof, not merely a docstring claim: `claim`'s
        signature carries no parameter that could smuggle in override
        evidence."""
        import inspect
        params = set(inspect.signature(scheduler.claim).parameters)
        self.assertNotIn("evidence", params)
        self.assertNotIn("override", params)
        self.assertNotIn("manual_signal_record", params)

    def test_claim_manual_signal_mode_lease_always_refused(self):
        """D-N03: ordinary claim() has no honest way to verify a manual
        capacity signal ever occurred, so it must never silently grant a
        manual_signal-mode lease -- only
        claim_with_authorized_early_override may."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1", resume_mode="manual_signal",
               not_before=None)
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(session_id, "lease-1", "worker-a",
                            now="2024-01-01T00:00:01Z", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason,
                         "manual_signal_requires_verified_evidence")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_claim_requires_well_formed_now(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with self.assertRaises(ValueError):
            scheduler.claim(session_id, "lease-1", "worker-a",
                            now="not-a-timestamp", automation_ref="auto-1")

    def test_claim_missing_lease_not_found(self):
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(_uuid(), "nope", "worker-a",
                            now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_found")

    def test_claim_success_surfaces_clock_skew_detected_key(self):
        """D-N05: skew is surfaced on EVERY returned dict, not merely on
        refusal."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        result = scheduler.claim(session_id, "lease-1", "worker-a",
                                 now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        self.assertIn("clock_skew_detected", result)
        self.assertFalse(result["clock_skew_detected"])


# --------------------------------------------------------------------------- #
# Mismatched automation_ref (D-N04: automation_ref is required everywhere).  #
# --------------------------------------------------------------------------- #


class AutomationRefMismatchTest(_SchedEnvMixin, unittest.TestCase):
    def test_claim_rejects_mismatched_automation_ref(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(session_id, "lease-1", "worker-a",
                            now="2024-06-01T00:00:00Z", automation_ref="wrong-ref")
        self.assertEqual(ctx.exception.reason, "automation_ref_mismatch")
        self.assertEqual(ctx.exception.details["expected"], "auto-1")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_cancel_rejects_mismatched_automation_ref(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.cancel(session_id, "lease-1", automation_ref="wrong-ref")
        self.assertEqual(ctx.exception.reason, "automation_ref_mismatch")

    def test_consume_rejects_mismatched_automation_ref(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.mark_consumed(session_id, "lease-1", automation_ref="wrong-ref")
        self.assertEqual(ctx.exception.reason, "automation_ref_mismatch")

    def test_replace_rejects_mismatched_automation_ref(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-old")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.replace(session_id, "lease-old",
                              _make_pause_lease(lease_id="lease-new"),
                              automation_ref="wrong-ref")
        self.assertEqual(ctx.exception.reason, "automation_ref_mismatch")

    def test_reclaim_rejects_mismatched_automation_ref(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.reclaim_if_expired(
                session_id, "lease-1", now="2024-06-01T00:00:00Z",
                expiry_after_seconds=60, automation_ref="wrong-ref")
        self.assertEqual(ctx.exception.reason, "automation_ref_mismatch")

    def test_record_failed_wake_attempt_rejects_mismatched_automation_ref(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.record_failed_wake_attempt(session_id, "lease-1", "wrong-ref")
        self.assertEqual(ctx.exception.reason, "automation_ref_mismatch")

    def test_override_rejects_automation_ref_mismatch(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
                manual_signal_record=record, pinned_public_keys=pinned,
                automation_ref="wrong-ref")
        self.assertEqual(ctx.exception.reason, "automation_ref_mismatch")

    def test_automation_ref_is_a_required_positional_everywhere(self):
        """Structural proof (D-N04): none of these mutating decisions
        default automation_ref to None/omittable."""
        import inspect
        for fn in (scheduler.cancel, scheduler.mark_consumed, scheduler.replace,
                  scheduler.reclaim_if_expired, scheduler.record_failed_wake_attempt):
            sig = inspect.signature(fn)
            param = sig.parameters["automation_ref"]
            self.assertIs(param.default, inspect.Parameter.empty,
                          "%s.automation_ref must have no default" % fn.__name__)


# --------------------------------------------------------------------------- #
# Same/different owner duplicate claims.                                     #
# --------------------------------------------------------------------------- #


class DuplicateClaimOwnershipTest(_SchedEnvMixin, unittest.TestCase):
    def test_same_owner_duplicate_claim_is_idempotent(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        first = scheduler.claim(session_id, "lease-1", "worker-a",
                                now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        second = scheduler.claim(session_id, "lease-1", "worker-a",
                                 now="2024-06-01T00:00:05Z", automation_ref="auto-1")
        self.assertEqual(first["outcome"], "claimed")
        self.assertEqual(second["outcome"], "already_claimed")
        self.assertEqual(second["lease"]["claimant_ref"], "worker-a")

    def test_different_owner_duplicate_claim_is_explicit_conflict(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(session_id, "lease-1", "worker-b",
                            now="2024-06-01T00:00:05Z", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "different_owner_conflict")
        self.assertEqual(ctx.exception.details["claimant_ref"], "worker-a")

    def test_claim_of_terminal_lease_is_not_claimable(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(session_id, "lease-1", "worker-b",
                            now="2024-06-01T00:00:05Z", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_claimable")
        self.assertEqual(ctx.exception.details["state"], "consumed")


# --------------------------------------------------------------------------- #
# Cancel / consume / replace / start_new_episode.                             #
# --------------------------------------------------------------------------- #


class CancelTest(_SchedEnvMixin, unittest.TestCase):
    def test_cancel_from_unclaimed(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        result = scheduler.cancel(session_id, "lease-1", automation_ref="auto-1")
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(result["lease"]["consumption_state"], "cancelled")

    def test_cancel_from_claimed(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        result = scheduler.cancel(session_id, "lease-1", automation_ref="auto-1")
        self.assertEqual(result["outcome"], "cancelled")

    def test_double_cancel_is_explicit_conflict_not_idempotent(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.cancel(session_id, "lease-1", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.cancel(session_id, "lease-1", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_cancellable")

    def test_cancel_missing_lease_not_found(self):
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.cancel(_uuid(), "nope", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_found")


class ConsumeTest(_SchedEnvMixin, unittest.TestCase):
    def test_consume_after_claim(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        result = scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        self.assertEqual(result["outcome"], "consumed")
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["lease"]["consumption_state"], "consumed")

    def test_consume_is_idempotent(self):
        """D-N07: idempotence is computed from the locked result (the
        returned record's own `consumed_at`), not merely trusted from a
        pre-lock read."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        first = scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        second = scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["lease"]["consumed_at"], second["lease"]["consumed_at"])

    def test_consume_never_claimed_conflicts(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "never_claimed")


class ReplaceTest(_SchedEnvMixin, unittest.TestCase):
    def test_replace_carries_forward_counter_monotonically(self):
        session_id = _uuid()
        stored = _create(session_id, lease_id="lease-old")
        lease = state_store.pause_lease_from_stored_record(stored)
        for _ in range(2):
            lease = capacity.record_failed_wake_attempt(lease)
        enriched = dict(lease)
        enriched.update(state_store._PAUSE_LEASE_BOOKKEEPING_DEFAULTS)
        path = state_store.pause_lease_path_for(session_id, "lease-old")
        self.assertTrue(state_store.write_json_atomic_durable(path, enriched))

        result = scheduler.replace(session_id, "lease-old",
                                   _make_pause_lease(lease_id="lease-new"),
                                   automation_ref="auto-1")
        self.assertEqual(result["outcome"], "replaced")
        self.assertEqual(result["lease"]["failed_wake_attempts"], 2)

    def test_replace_rejects_cross_binding(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-old")
        cross = _make_pause_lease(lease_id="lease-new", role="different-role")
        with self.assertRaises(state_store.CrossBindingReplacementError):
            scheduler.replace(session_id, "lease-old", cross, automation_ref="auto-1")

    def test_replace_of_terminal_lease_is_not_replaceable_never_masquerades(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-old")
        scheduler.claim(session_id, "lease-old", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        scheduler.mark_consumed(session_id, "lease-old", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.replace(session_id, "lease-old",
                              _make_pause_lease(lease_id="lease-new"),
                              automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_replaceable")
        # The distinct, explicit path forward for a terminal binding is
        # start_new_episode -- never a silent fallback inside replace().
        started = scheduler.start_new_episode(
            session_id, _make_pause_lease(lease_id="lease-fresh-episode"))
        self.assertEqual(started["outcome"], "episode_started")
        self.assertEqual(started["lease"]["failed_wake_attempts"], 0)

    def test_replace_missing_old_lease_not_found(self):
        session_id = _uuid()
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.replace(session_id, "nope",
                              _make_pause_lease(lease_id="lease-new"),
                              automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_found")


# --------------------------------------------------------------------------- #
# D-MJ-01 / D-N12: ceiling enforced per binding across reclaim + new episode,#
# and the read-only binding-current resolution seam that makes it possible.  #
# --------------------------------------------------------------------------- #


class BindingResolutionSeamTest(_SchedEnvMixin, unittest.TestCase):
    def test_returns_none_for_binding_that_never_had_a_lease(self):
        session_id = _uuid()
        self.assertIsNone(
            scheduler.resolve_current_lease_for_binding(session_id, _binding()))

    def test_returns_current_lease_after_replacement_chain(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-old")
        scheduler.replace(session_id, "lease-old",
                          _make_pause_lease(lease_id="lease-new"),
                          automation_ref="auto-1")
        current = scheduler.resolve_current_lease_for_binding(session_id, _binding())
        self.assertEqual(current["lease_id"], "lease-new")

    def test_returns_terminal_lease_as_is(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.cancel(session_id, "lease-1", automation_ref="auto-1")
        current = scheduler.resolve_current_lease_for_binding(session_id, _binding())
        self.assertEqual(current["lease_id"], "lease-1")
        self.assertEqual(current["consumption_state"], "cancelled")

    def test_never_reimplements_a_lock_no_lock_file_opened(self):
        """Purely observational: this seam performs reads only -- calling
        it never creates a NEW lock file (Package B's own accessors do,
        for their own binding index; this function must not add another)."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        before = set(os.listdir(state_store.capacity_dir_for(session_id)))
        scheduler.resolve_current_lease_for_binding(session_id, _binding())
        after = set(os.listdir(state_store.capacity_dir_for(session_id)))
        self.assertEqual(before, after)


class CeilingPerBindingTest(_SchedEnvMixin, unittest.TestCase):
    def _exhaust_ceiling(self, session_id, lease_id):
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            state_store.record_pause_lease_failed_wake_attempt(session_id, lease_id)

    def test_start_new_episode_refused_after_ceiling_exhausted_then_reclaimed(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        self._exhaust_ceiling(session_id, "lease-1")
        scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-06-01T00:00:00Z",
            expiry_after_seconds=0, automation_ref="auto-1")
        expired = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(expired["consumption_state"], "expired")
        self.assertEqual(expired["failed_wake_attempts"],
                         capacity.FAILED_WAKE_ATTEMPT_CEILING)

        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.start_new_episode(
                session_id, _make_pause_lease(lease_id="lease-2"))
        self.assertEqual(ctx.exception.reason, "binding_ceiling_exhausted")
        self.assertEqual(ctx.exception.details["blocking_lease_id"], "lease-1")
        self.assertIsNone(state_store.read_pause_lease(session_id, "lease-2"))

    def test_start_new_episode_allowed_when_ceiling_not_reached(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-06-01T00:00:00Z",
            expiry_after_seconds=0, automation_ref="auto-1")
        result = scheduler.start_new_episode(
            session_id, _make_pause_lease(lease_id="lease-2"))
        self.assertEqual(result["outcome"], "episode_started")

    def test_start_new_episode_via_consumed_prior_at_ceiling_also_refused(self):
        """The enforcement is not expiry-specific: ANY terminal prior
        (consumed here, not expired) at the ceiling still blocks a fresh
        episode for the same binding."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        self._exhaust_ceiling(session_id, "lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.start_new_episode(
                session_id, _make_pause_lease(lease_id="lease-2"))
        self.assertEqual(ctx.exception.reason, "binding_ceiling_exhausted")

    def test_start_new_episode_for_a_different_binding_is_unaffected(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1", provider_session_id="sess-x")
        self._exhaust_ceiling(session_id, "lease-1")
        scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-06-01T00:00:00Z",
            expiry_after_seconds=0, automation_ref="auto-1")
        result = scheduler.start_new_episode(
            session_id, _make_pause_lease(lease_id="lease-2", provider_session_id="sess-y"))
        self.assertEqual(result["outcome"], "episode_started")


class RecordFailedWakeAttemptTest(_SchedEnvMixin, unittest.TestCase):
    """D-N11: direct coverage for record_failed_wake_attempt."""

    def test_increments_by_one(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        result = scheduler.record_failed_wake_attempt(session_id, "lease-1", "auto-1")
        self.assertEqual(result["outcome"], "failed_wake_attempt_recorded")
        self.assertEqual(result["lease"]["failed_wake_attempts"], 1)

    def test_ceiling_exhausted_conflict(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            scheduler.record_failed_wake_attempt(session_id, "lease-1", "auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.record_failed_wake_attempt(session_id, "lease-1", "auto-1")
        self.assertEqual(ctx.exception.reason, "ceiling_exhausted")

    def test_not_unclaimed_conflict(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.record_failed_wake_attempt(session_id, "lease-1", "auto-1")
        self.assertEqual(ctx.exception.reason, "not_unclaimed")

    def test_not_found_conflict(self):
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.record_failed_wake_attempt(_uuid(), "nope", "auto-1")
        self.assertEqual(ctx.exception.reason, "not_found")


# --------------------------------------------------------------------------- #
# Verified-evidence early override.                                          #
# --------------------------------------------------------------------------- #


class VerifiedOverrideTest(_SchedEnvMixin, unittest.TestCase):
    def test_override_bypasses_early_refusal_with_verified_evidence(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        result = scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        self.assertEqual(result["outcome"], "claimed_via_override")
        self.assertEqual(result["lease"]["consumption_state"], "claimed")
        # The override is durably recorded DISTINCTLY from the ordinary
        # claim bookkeeping.
        journal = scheduler.read_manual_signal_journal(session_id)
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["lease_id"], "lease-1")
        self.assertEqual(journal[0]["signal_journal_ref"], record["signal_journal_ref"])
        self.assertEqual(journal[0]["schema_version"],
                         scheduler.MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION)
        # Genuinely, durably persisted via Package B's own accessor too.
        durable = state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"])
        self.assertIsNotNone(durable)

    def test_claim_manual_signal_mode_lease_succeeds_via_verified_override(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1", resume_mode="manual_signal",
               not_before=None)
        record, pinned = _signed_manual_signal()
        result = scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        self.assertEqual(result["outcome"], "claimed_via_override")

    def test_ordinary_claim_on_a_sibling_lease_still_refuses_early(self):
        """Proves the override is a genuinely SEPARATE path: an identical
        binding's second, independent lease claimed the ORDINARY way is
        still strictly refused early."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1", provider_session_id="sess-a")
        record, pinned = _signed_manual_signal(
            role="builder", provider_session_id="sess-a")
        scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        _create(session_id, lease_id="lease-2", provider_session_id="sess-b")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(session_id, "lease-2", "worker-a",
                            now="2024-01-01T00:00:01Z", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "early_refusal")

    def test_override_rejects_tampered_signature(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        record["detached_signature"] = "ff" * 64  # tampered
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
                manual_signal_record=record, pinned_public_keys=pinned,
                automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "override_evidence_invalid")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_override_rejects_unpinned_signer(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, _pinned = _signed_manual_signal()
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
                manual_signal_record=record, pinned_public_keys={},
                automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "override_evidence_invalid")

    def test_override_rejects_binding_mismatch_never_persists_evidence(self):
        """D-N02: the binding check runs BEFORE the evidence is ever
        durably persisted -- a mismatched-binding record leaves nothing
        behind in Package B's manual_signals/ store."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1", candidate_digest="b" * 64)
        record, pinned = _signed_manual_signal(candidate_digest="d" * 64)
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
                manual_signal_record=record, pinned_public_keys=pinned,
                automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "override_binding_mismatch")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")
        self.assertIsNone(state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"]))

    def test_override_same_owner_idempotent(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        first = scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        second = scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:02Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        self.assertEqual(first["outcome"], "claimed_via_override")
        self.assertEqual(second["outcome"], "already_claimed")
        # Idempotent retry never appends a second journal entry.
        self.assertEqual(len(scheduler.read_manual_signal_journal(session_id)), 1)

    def test_override_evidence_cannot_be_reused_for_a_different_lease(self):
        """D-N01: single-use/fresh evidence -- the SAME signal_journal_ref
        can never authorize a DIFFERENT lease_id."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        scheduler.cancel(session_id, "lease-1", automation_ref="auto-1")
        _create(session_id, lease_id="lease-2")  # same binding, now terminal-freed
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                session_id, "lease-2", "worker-a", now="2024-01-01T00:00:02Z",
                manual_signal_record=record, pinned_public_keys=pinned,
                automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "override_evidence_already_used")
        self.assertEqual(ctx.exception.details["other_lease_id"], "lease-1")
        stored = state_store.read_pause_lease(session_id, "lease-2")
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_override_crash_between_claim_and_journal_append_is_repaired(self):
        """D-MJ-03: a crash strictly between the durable claim succeeding
        and the durable journal-append completing must be repairable on
        retry, not leave the attribution permanently missing."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        with mock.patch.object(state_store, "append_jsonl_atomic", return_value=False):
            with self.assertRaises(scheduler.SchedulerOverrideRecordingFailed):
                scheduler.claim_with_authorized_early_override(
                    session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
                    manual_signal_record=record, pinned_public_keys=pinned,
                    automation_ref="auto-1")
        # The underlying claim already durably succeeded despite the crash.
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "claimed")
        self.assertEqual(scheduler.read_manual_signal_journal(session_id), [])

        # Retry (no longer patched) REPAIRS the missing journal entry via
        # the idempotent already_claimed path -- never re-attempts the
        # (already-durable) claim itself.
        result = scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:02Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        self.assertEqual(result["outcome"], "already_claimed")
        journal = scheduler.read_manual_signal_journal(session_id)
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["lease_id"], "lease-1")


# --------------------------------------------------------------------------- #
# Deterministic bounded jitter.                                              #
# --------------------------------------------------------------------------- #


class DeterministicJitterTest(unittest.TestCase):
    def test_jitter_is_deterministic_for_same_seed(self):
        a = scheduler.deterministic_jitter_seconds("lease-1:1", 60.0)
        b = scheduler.deterministic_jitter_seconds("lease-1:1", 60.0)
        self.assertEqual(a, b)

    def test_jitter_bounded(self):
        for i in range(50):
            value = scheduler.deterministic_jitter_seconds("lease-%d" % i, 30.0)
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 30.0)

    def test_jitter_varies_by_seed(self):
        values = {scheduler.deterministic_jitter_seconds("seed-%d" % i, 100.0)
                 for i in range(10)}
        self.assertGreater(len(values), 1)

    def test_zero_max_jitter_is_always_zero(self):
        self.assertEqual(scheduler.deterministic_jitter_seconds("lease-1", 0), 0.0)

    def test_negative_max_jitter_rejected(self):
        with self.assertRaises(ValueError):
            scheduler.deterministic_jitter_seconds("lease-1", -1)

    def test_next_scheduled_wake_epoch_never_before_not_before(self):
        epoch = scheduler.next_scheduled_wake_epoch(1000.0, "lease-1:1", 60.0)
        self.assertGreaterEqual(epoch, 1000.0)
        self.assertLess(epoch, 1060.0)

    def test_jitter_uses_no_global_random_state(self):
        """Calling deterministic_jitter_seconds must never perturb the
        global `random` module's state -- proof this module never reaches
        into it (it does not even import `random`, checked structurally
        below, but this is the behavioral twin of that check)."""
        import random
        random.seed(12345)
        before = random.random()
        random.seed(12345)
        scheduler.deterministic_jitter_seconds("lease-1", 10.0)
        after = random.random()
        self.assertEqual(before, after)


# --------------------------------------------------------------------------- #
# Clock-skew handling.                                                       #
# --------------------------------------------------------------------------- #


class ClockSkewTest(unittest.TestCase):
    def test_no_reference_trusts_now_verbatim(self):
        epoch, skewed = scheduler.resolve_effective_now_epoch("2024-01-01T00:00:00Z")
        self.assertFalse(skewed)
        self.assertEqual(epoch, capacity.rfc3339_to_epoch_seconds("2024-01-01T00:00:00Z"))

    def test_small_skew_within_tolerance_not_flagged(self):
        epoch, skewed = scheduler.resolve_effective_now_epoch(
            "2024-01-01T00:00:10Z", reference_now="2024-01-01T00:00:00Z",
            max_clock_skew_seconds=30)
        self.assertFalse(skewed)
        self.assertEqual(epoch, capacity.rfc3339_to_epoch_seconds("2024-01-01T00:00:10Z"))

    def test_large_forward_skew_clamped_to_reference_plus_tolerance(self):
        epoch, skewed = scheduler.resolve_effective_now_epoch(
            "2024-01-01T01:00:00Z", reference_now="2024-01-01T00:00:00Z",
            max_clock_skew_seconds=30)
        self.assertTrue(skewed)
        self.assertEqual(epoch, capacity.rfc3339_to_epoch_seconds("2024-01-01T00:00:00Z") + 30)

    def test_skew_prevents_fast_clock_from_forcing_early_claim(self):
        """Without a trusted reference clock, a fast/lying local clock
        could claim a lease before its genuine not_before. WITH a
        reference clock and skew bound, the claim is correctly refused."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        fast_local_now = "2024-01-01T00:20:00Z"   # claims to be well past
        genuine_reference_now = "2024-01-01T00:01:00Z"  # actually still early

        # Without a reference clock, the (lying) local clock alone would
        # wrongly permit the claim.
        unguarded = scheduler.claim(
            session_id, "lease-1", "worker-a", now=fast_local_now,
            automation_ref="auto-1")
        self.assertEqual(unguarded["outcome"], "claimed")

        session_id_2 = _uuid()
        _create(session_id_2, lease_id="lease-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(
                session_id_2, "lease-1", "worker-a", now=fast_local_now,
                automation_ref="auto-1", reference_now=genuine_reference_now,
                max_clock_skew_seconds=30)
        self.assertEqual(ctx.exception.reason, "early_refusal")
        self.assertTrue(ctx.exception.details["clock_skew_detected"])

    def test_malformed_reference_now_rejected(self):
        with self.assertRaises(ValueError):
            scheduler.resolve_effective_now_epoch(
                "2024-01-01T00:00:00Z", reference_now="garbage")


# --------------------------------------------------------------------------- #
# Truthful expiry / reclaim.                                                  #
# --------------------------------------------------------------------------- #


class ExpiryReclaimTest(_SchedEnvMixin, unittest.TestCase):
    def test_not_yet_expired_before_grace_window_no_mutation(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        result = scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-01-01T00:20:00Z",
            expiry_after_seconds=3600, automation_ref="auto-1")
        self.assertEqual(result["outcome"], "not_yet_expired")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_expired_after_grace_window(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        result = scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-01-01T02:00:00Z",
            expiry_after_seconds=3600, automation_ref="auto-1")
        self.assertEqual(result["outcome"], "expired")
        self.assertEqual(result["lease"]["consumption_state"], "expired")

    def test_expired_lease_is_not_claimable(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-01-01T02:00:00Z",
            expiry_after_seconds=3600, automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(session_id, "lease-1", "worker-a",
                            now="2024-01-01T02:00:01Z", automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_claimable")
        self.assertEqual(ctx.exception.details["state"], "expired")

    def test_reclaim_of_terminal_lease_not_expirable(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.reclaim_if_expired(
                session_id, "lease-1", now="2024-06-02T00:00:00Z",
                expiry_after_seconds=3600, automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_expirable")

    def test_reclaim_refuses_claimed_lease_never_anchors_on_claimed_at(self):
        """D-MJ-04: reclaim applies ONLY to a still-unclaimed lease. A
        claimant claiming long after not_before must never be reported as
        expired the instant they claim it."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-01-01T05:00:00Z", automation_ref="auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.reclaim_if_expired(
                session_id, "lease-1", now="2024-01-01T05:00:01Z",
                expiry_after_seconds=60, automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_expirable")
        self.assertEqual(ctx.exception.details["state"], "claimed")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "claimed")

    def test_reclaim_manual_signal_mode_anchors_on_issued_at(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1", resume_mode="manual_signal",
               not_before=None)
        not_yet = scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-01-01T00:30:00Z",
            expiry_after_seconds=3600, automation_ref="auto-1")
        self.assertEqual(not_yet["outcome"], "not_yet_expired")
        expired = scheduler.reclaim_if_expired(
            session_id, "lease-1", now="2024-01-01T02:00:00Z",
            expiry_after_seconds=3600, automation_ref="auto-1")
        self.assertEqual(expired["outcome"], "expired")

    def test_reclaim_missing_lease_not_found(self):
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.reclaim_if_expired(_uuid(), "nope", now="2024-01-01T00:00:00Z",
                                         expiry_after_seconds=60, automation_ref="auto-1")
        self.assertEqual(ctx.exception.reason, "not_found")


# --------------------------------------------------------------------------- #
# Crash between claim and consume: durable, resumable, never fabricated.     #
# --------------------------------------------------------------------------- #


class CrashBetweenClaimAndConsumeTest(_SchedEnvMixin, unittest.TestCase):
    def test_state_after_claim_is_durable_and_resumable(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")

        # "Crash": nothing further happens in this process. A completely
        # FRESH read (no cached in-memory scheduler state exists to lose --
        # this module carries none across calls) must show 'claimed', never
        # a fabricated 'consumed'.
        resumed_stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(resumed_stored["consumption_state"], "claimed")
        self.assertEqual(scheduler.wake_decision(session_id, "lease-1"),
                         "wake_retry_eligible")

        # A fresh scheduler call can now resume and legitimately complete
        # the consume step.
        result = scheduler.mark_consumed(session_id, "lease-1", automation_ref="auto-1")
        self.assertEqual(result["outcome"], "consumed")
        self.assertFalse(result["idempotent"])
        final = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(final["consumption_state"], "consumed")

    def test_never_fabricates_a_completed_wake_before_consume(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-a",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        # Before any consume call, mark_consumed on a DIFFERENT process's
        # view of the same lease_id must still require an explicit call --
        # reading state alone never silently reports 'consumed'.
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertNotEqual(stored["consumption_state"], "consumed")


# --------------------------------------------------------------------------- #
# Real, separate-process duplicate claim through Package B's own locked      #
# accessor: exactly one success.                                             #
# --------------------------------------------------------------------------- #


def _mp_scheduler_claim(root, session_id, lease_id, claimant, now, automation_ref,
                        barrier, result_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_capacity_scheduler as sched
    barrier.wait()
    try:
        result = sched.claim(session_id, lease_id, claimant, now, automation_ref)
        out = {"outcome": result["outcome"], "claimant_ref": result["lease"]["claimant_ref"]}
    except sched.SchedulerLeaseConflict as exc:
        out = {"outcome": "conflict", "reason": exc.reason}
    with open(result_path, "w") as fh:
        json.dump(out, fh)


class RealProcessClaimRaceTest(_SchedEnvMixin, unittest.TestCase):
    def test_different_claimants_exactly_one_success(self):
        session_id = _uuid()
        _create(session_id, lease_id="race-1")
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        now = "2024-06-01T00:00:00Z"
        p1 = ctx.Process(target=_mp_scheduler_claim,
                         args=(self._root, session_id, "race-1", "worker-a", now,
                              "auto-1", barrier, r1))
        p2 = ctx.Process(target=_mp_scheduler_claim,
                         args=(self._root, session_id, "race-1", "worker-b", now,
                              "auto-1", barrier, r2))
        p1.start(); p2.start()
        p1.join(timeout=30); p2.join(timeout=30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        outcomes = sorted([o1["outcome"], o2["outcome"]])
        self.assertEqual(outcomes, ["claimed", "conflict"])
        conflict = o1 if o1["outcome"] == "conflict" else o2
        self.assertEqual(conflict["reason"], "different_owner_conflict")
        final = state_store.read_pause_lease(session_id, "race-1")
        self.assertEqual(final["consumption_state"], "claimed")
        self.assertIn(final["claimant_ref"], ("worker-a", "worker-b"))

    def test_same_claimant_races_itself_both_report_success_idempotent(self):
        session_id = _uuid()
        _create(session_id, lease_id="race-2")
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        now = "2024-06-01T00:00:00Z"
        p1 = ctx.Process(target=_mp_scheduler_claim,
                         args=(self._root, session_id, "race-2", "worker-a", now,
                              "auto-1", barrier, r1))
        p2 = ctx.Process(target=_mp_scheduler_claim,
                         args=(self._root, session_id, "race-2", "worker-a", now,
                              "auto-1", barrier, r2))
        p1.start(); p2.start()
        p1.join(timeout=30); p2.join(timeout=30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        outcomes = sorted([o1["outcome"], o2["outcome"]])
        self.assertEqual(outcomes, ["already_claimed", "claimed"])
        final = state_store.read_pause_lease(session_id, "race-2")
        self.assertEqual(final["consumption_state"], "claimed")
        self.assertEqual(final["claimant_ref"], "worker-a")


# --------------------------------------------------------------------------- #
# Wake-trigger contract: versioned arguments/exit codes.                      #
# --------------------------------------------------------------------------- #


class WakeTriggerContractTest(_SchedEnvMixin, unittest.TestCase):
    def _argv(self, session_id, lease_id="lease-1", claimant_ref="worker-a",
             automation_ref="auto-1", now="2024-06-01T00:00:00Z", extra=()):
        return [
            "--session-uuid", session_id, "--lease-id", lease_id,
            "--claimant-ref", claimant_ref, "--automation-ref", automation_ref,
            "--now", now,
        ] + list(extra)

    def test_success_exit_code(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        lines = []
        code = scheduler.run_wake_trigger(self._argv(session_id), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["success"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "claimed")
        self.assertIn("clock_skew_detected", payload)

    def test_not_due_exit_code_exposes_jittered_next_wake(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        lines = []
        code = scheduler.run_wake_trigger(
            self._argv(session_id, now="2024-01-01T00:00:00Z"), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["not_due"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["reason"], "early_refusal")
        not_before_epoch = capacity.rfc3339_to_epoch_seconds("2024-01-01T00:10:00Z")
        self.assertGreaterEqual(payload["next_wake_epoch"], not_before_epoch)
        self.assertLess(payload["next_wake_epoch"],
                        not_before_epoch + scheduler.DEFAULT_MAX_JITTER_SECONDS)

    def test_next_wake_epoch_zero_jitter_is_exact(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        lines = []
        scheduler.run_wake_trigger(
            self._argv(session_id, now="2024-01-01T00:00:00Z",
                      extra=["--max-jitter-seconds", "0"]),
            output=lines.append)
        payload = json.loads(lines[0])
        self.assertEqual(payload["next_wake_epoch"],
                         capacity.rfc3339_to_epoch_seconds("2024-01-01T00:10:00Z"))

    def test_conflict_exit_code(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "worker-b",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        lines = []
        code = scheduler.run_wake_trigger(self._argv(session_id), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["conflict"])

    def test_not_found_lease_is_a_conflict(self):
        lines = []
        code = scheduler.run_wake_trigger(
            self._argv(_uuid(), lease_id="nope"), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["conflict"])

    def test_attempts_exhausted_exit_code_never_attempts_claim(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        lines = []
        code = scheduler.run_wake_trigger(self._argv(session_id), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["attempts_exhausted"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "attempts_exhausted")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")  # never attempted

    def test_internal_error_exit_code_maps_lock_io_failure_and_counts_it(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        lines = []
        with mock.patch.object(state_store, "claim_pause_lease",
                               side_effect=OSError("simulated lock timeout")):
            code = scheduler.run_wake_trigger(self._argv(session_id), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["internal_error"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "internal_error")
        # Durably counted as exactly one failed wake attempt (D-MJ-02).
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["failed_wake_attempts"], 1)
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_internal_error_never_double_counts_within_one_invocation(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        with mock.patch.object(state_store, "claim_pause_lease",
                               side_effect=OSError("simulated")):
            scheduler.run_wake_trigger(self._argv(session_id), output=lambda s: None)
            scheduler.run_wake_trigger(self._argv(session_id), output=lambda s: None)
        stored = state_store.read_pause_lease(session_id, "lease-1")
        # Exactly one increment PER invocation -- two invocations, two
        # increments, never more than one per call.
        self.assertEqual(stored["failed_wake_attempts"], 2)

    def test_wake_decision_io_failure_maps_to_internal_error(self):
        session_id = _uuid()
        lines = []
        with mock.patch.object(state_store, "pause_lease_wake_decision",
                               side_effect=OSError("simulated")):
            code = scheduler.run_wake_trigger(
                self._argv(session_id, lease_id="nope"), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["internal_error"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "internal_error")

    def test_invalid_arguments_exit_code_missing_required_arg(self):
        code = scheduler.run_wake_trigger(
            ["--session-uuid", "x"], output=lambda s: None)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["invalid_arguments"])

    def test_invalid_arguments_exit_code_malformed_now(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        lines = []
        code = scheduler.run_wake_trigger(
            self._argv(session_id, now="not-a-timestamp"), output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["invalid_arguments"])

    def test_exit_codes_are_a_versioned_exported_contract(self):
        self.assertEqual(scheduler.WAKE_TRIGGER_CONTRACT_VERSION, 2)
        self.assertEqual(scheduler.WAKE_TRIGGER_EXIT_CODES, {
            "success": 0, "internal_error": 1, "invalid_arguments": 2,
            "not_due": 3, "conflict": 4, "attempts_exhausted": 5,
        })

    def test_max_jitter_seconds_argument_exists(self):
        parser = scheduler.build_wake_trigger_arg_parser()
        args = parser.parse_args(self._argv(_uuid(), extra=["--max-jitter-seconds", "5"]))
        self.assertEqual(args.max_jitter_seconds, 5.0)


class ManualSignalJournalContractTest(_SchedEnvMixin, unittest.TestCase):
    def test_journal_entry_shape_is_exact(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        entry = scheduler.read_manual_signal_journal(session_id)[0]
        validated = scheduler.validate_manual_signal_journal_entry(entry)
        self.assertEqual(validated, entry)

    def test_journal_entry_rejects_unknown_key(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        entry = dict(scheduler.read_manual_signal_journal(session_id)[0])
        entry["extra_field"] = "nope"
        with self.assertRaises(ValueError):
            scheduler.validate_manual_signal_journal_entry(entry)

    def test_journal_entry_rejects_malformed_requested_at(self):
        """D-N09: requested_at must be a genuine RFC3339 timestamp, not
        merely any nonempty string."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        record, pinned = _signed_manual_signal()
        scheduler.claim_with_authorized_early_override(
            session_id, "lease-1", "worker-a", now="2024-01-01T00:00:01Z",
            manual_signal_record=record, pinned_public_keys=pinned,
            automation_ref="auto-1")
        entry = dict(scheduler.read_manual_signal_journal(session_id)[0])
        entry["requested_at"] = "not-a-timestamp"
        with self.assertRaises(ValueError):
            scheduler.validate_manual_signal_journal_entry(entry)

    def test_journal_schema_version_is_versioned_contract(self):
        self.assertEqual(scheduler.MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION, 1)


# --------------------------------------------------------------------------- #
# Structural gates: no wall-clock/sleep/global randomness, no storage/lock   #
# reimplementation, no control-plane event emission (M3A-REV-014).           #
# --------------------------------------------------------------------------- #


class StructuralGatesTest(unittest.TestCase):
    def _module_path(self):
        return os.path.join(_HERE, "cowork_capacity_scheduler.py")

    def _tree(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=self._module_path())

    def _top_level_imports(self):
        names = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_imports_no_wall_clock_or_random_or_lock_modules(self):
        imported = self._top_level_imports()
        self.assertNotIn("time", imported)
        self.assertNotIn("datetime", imported)
        self.assertNotIn("random", imported)
        self.assertNotIn("fcntl", imported)

    def test_imports_no_control_plane_module(self):
        """M3A-REV-014: this module must remain a pure decision layer and
        emit no control-plane event -- it must not even IMPORT
        cowork_control_plane."""
        imported = self._top_level_imports()
        self.assertNotIn("cowork_control_plane", imported)

    def test_imports_only_expected_modules(self):
        imported = self._top_level_imports()
        self.assertEqual(
            imported,
            {"argparse", "hashlib", "json", "os", "re", "sys",
            "cowork_capacity", "cowork_state"})

    def test_source_contains_no_sleep_or_wall_clock_calls(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("time.sleep(", "time.time(", "datetime.now(",
                          "datetime.utcnow(", "random.random(", "random.seed(",
                          ".flock(", "open(", "os.open("):
            self.assertNotIn(forbidden, source,
                             "found forbidden call/token: %s" % forbidden)

    def test_source_never_calls_advance_or_references_control_plane_in_code(self):
        """No `advance(...)` call anywhere (AST-level, so this cannot be
        fooled by whitespace/formatting), and no CODE token (as opposed to
        prose in the module docstring, which legitimately names
        `cowork_control_plane` when explaining M3A-REV-014) references the
        control-plane module."""
        tree = self._tree()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else None)
                self.assertNotEqual(name, "advance",
                                    "found a call to something named advance(...)")
            if isinstance(node, ast.Name):
                self.assertNotEqual(node.id, "cowork_control_plane")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                self.assertNotEqual(node.value.id, "cowork_control_plane")

    def test_module_docstring_names_m3a_rev_014(self):
        tree = self._tree()
        doc = ast.get_docstring(tree)
        self.assertIn("M3A-REV-014", doc)

    def test_git_status_shows_exactly_the_two_allowed_paths(self):
        """D-N10: a NON-VACUOUS allowlist gate -- actually inspects `git
        status` against the repo root, rather than merely asserting the
        two files exist (which would trivially pass even if unrelated
        files were also modified, or if it were run somewhere with no git
        repo at all)."""
        repo_root = os.path.dirname(_HERE)
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=repo_root, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git unavailable in this environment")
        if result.returncode != 0:
            self.skipTest("git status failed: %s" % result.stderr)
        changed = set()
        for line in result.stdout.splitlines():
            path = line[3:].strip()
            if path:
                changed.add(path)
        allowed = {"scripts/cowork_capacity_scheduler.py",
                  "scripts/test_cowork_capacity_scheduler.py"}
        self.assertEqual(changed, allowed)


class ABCIntegrityTest(unittest.TestCase):
    """Package D imports Packages A/B without needing to modify them, and
    their own test suites are unaffected by this addition."""

    def test_package_a_still_importable_and_functional(self):
        lease = dict(schema_version=1, package_id="pkg-1", lease_id="x",
                    resume_mode="scheduled", not_before="2024-01-01T00:10:00Z",
                    automation_ref="auto-1", consumption_state="unclaimed",
                    failed_wake_attempts=0, issued_at="2024-01-01T00:00:00Z")
        lease.update(_binding())
        capacity.validate_pause_lease(lease)  # does not raise

    def test_package_b_pause_lease_accessors_present(self):
        for name in ("create_pause_lease", "claim_pause_lease",
                    "cancel_pause_lease", "mark_pause_lease_consumed",
                    "replace_pause_lease", "mark_pause_lease_expired",
                    "record_pause_lease_failed_wake_attempt",
                    "verify_manual_capacity_signal",
                    "write_manual_capacity_signal", "append_jsonl_atomic"):
            self.assertTrue(hasattr(state_store, name))

    def test_package_c_control_plane_still_importable(self):
        import cowork_control_plane  # noqa: F401


if __name__ == "__main__":
    unittest.main()
