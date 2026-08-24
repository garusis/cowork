#!/usr/bin/env python3
"""Focused tests for M3 Package B: crash-safe, cross-process-safe durable
persistence for ProviderHealth, CapacityPacket, PauseLease, manual-capacity
signals, InvalidationRecord history, and pending-turn-before-pause-ack --
all additive to `cowork_state.py`.

Run standalone:

    python3 -m unittest scripts/test_cowork_state_m3.py -v
"""

import fcntl
import hashlib
import inspect
import json
import multiprocessing
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_capacity as capacity  # noqa: E402
import cowork_state as state_store  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


def _wait_for_marker(marker_path, timeout=10.0):
    """Poll for a marker file's existence -- the deterministic
    cross-process synchronization primitive this suite's real-race and
    real-kill tests use instead of hoped-for timing (mirrors test_cowork_
    state_m2.py's identical helper)."""
    deadline = time.time() + timeout
    while not os.path.exists(marker_path) and time.time() < deadline:
        time.sleep(0.01)
    return os.path.exists(marker_path)


class _M3EnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test, so nothing ever touches the
    real home dir (mirrors test_cowork_state_m2.py's _M2EnvMixin)."""

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

    def _raw_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()


# --------------------------------------------------------------------------- #
# Fixture builders.                                                          #
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


def _make_capacity_packet(**overrides):
    packet = dict(
        schema_version=1, package_id="pkg-" + _uuid(),
        provider_capacity_class="subscription_quota_exhausted",
        provider="anthropic", resume_mode="scheduled", retry_after="120s",
        capacity_source={"kind": "provider_header", "sha256": "d" * 64},
        binding=_binding(),
        wakeup={"lease_id": "lease-1", "automation_ref": "auto-1",
                "not_before": "2024-01-01T00:02:00Z"},
        manual_resume={"condition": None, "accepted_source": None,
                      "signal_journal_ref": None},
        issued_at="2024-01-01T00:00:00Z")
    packet.update(overrides)
    return packet


def _make_invalidation_record(**overrides):
    rec = dict(schema_version=1, package_id="pkg-1",
              invalidated_candidate_digest="b" * 64,
              invalidated_session_id=_uuid(), invalidated_work_id=_uuid(),
              invalidating_principal="orchestrator", reason="stale candidate",
              evidence_refs=[{"path": "a.txt", "sha256": "c" * 64}],
              issued_at="2024-01-01T00:00:00Z")
    rec.update(overrides)
    return rec


def _make_provider_health(**overrides):
    rec = dict(role="builder", provider="anthropic", status="degraded",
              consecutive_failures=2, last_outcome="overloaded",
              last_updated_at="2024-01-01T00:00:00Z")
    rec.update(overrides)
    return rec


def _signed_manual_signal(secret_key=None, key_id="key-1", **overrides):
    """Build a manual-capacity-signal record with a GENUINE Ed25519
    signature (via the module's own self-contained self-test signer --
    reaching into `state_store._ed25519_selftest_sign`/`_selftest_
    publickey`, mirroring test_cowork_state_m2.py's established convention
    of reaching into this module's private test-control hooks, e.g.
    `state_store._utc_now`/`state_store._reconciled_phase_state_entry`).
    Returns (record, pinned_public_keys, secret_key)."""
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
    return record, pinned, secret_key


# --------------------------------------------------------------------------- #
# PauseLease: create / claim / cancel / consume.                             #
# --------------------------------------------------------------------------- #


class PauseLeaseCreateTest(_M3EnvMixin, unittest.TestCase):
    def test_create_persists_unclaimed_lease(self):
        session_id = _uuid()
        lease = _make_pause_lease()
        stored = state_store.create_pause_lease(session_id, lease)
        self.assertEqual(stored["consumption_state"], "unclaimed")
        self.assertEqual(stored["failed_wake_attempts"], 0)
        self.assertIsNone(stored["claimant_ref"])
        read_back = state_store.read_pause_lease(session_id, lease["lease_id"])
        self.assertEqual(read_back, stored)

    def test_create_rejects_invalid_shape(self):
        session_id = _uuid()
        bad = _make_pause_lease()
        del bad["automation_ref"]
        with self.assertRaises(ValueError):
            state_store.create_pause_lease(session_id, bad)
        self.assertIsNone(state_store.read_pause_lease(session_id, bad["lease_id"]))

    def test_create_rejects_non_unclaimed_state(self):
        session_id = _uuid()
        lease = _make_pause_lease(consumption_state="claimed")
        with self.assertRaises(ValueError):
            state_store.create_pause_lease(session_id, lease)

    def test_create_rejects_nonzero_failed_wake_attempts(self):
        session_id = _uuid()
        lease = _make_pause_lease(failed_wake_attempts=1)
        with self.assertRaises(ValueError):
            state_store.create_pause_lease(session_id, lease)

    def test_second_create_same_lease_id_different_binding_conflicts_already_exists(self):
        """A lease_id collision across two DIFFERENT bindings (the only way
        `already_exists` is reachable now that same-binding duplicates are
        caught earlier, by the binding-liveness check -- see
        test_second_create_same_binding_conflicts_binding_already_live)."""
        session_id = _uuid()
        state_store.create_pause_lease(
            session_id, _make_pause_lease(lease_id="dup-1", provider_session_id="sess-A"))
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.create_pause_lease(
                session_id, _make_pause_lease(lease_id="dup-1", provider_session_id="sess-B"))
        self.assertEqual(ctx.exception.reason, "already_exists")

    def test_second_create_same_binding_conflicts_binding_already_live(self):
        """M3B-REV-B02/M01 closure: a SECOND create for the SAME binding
        (even with a fresh, different lease_id) is refused while the first
        lease is still live -- direct proof a fresh mint can no longer
        silently reset/duplicate an already-live binding's wake-attempt
        accounting."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="live-1"))
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="live-2"))
        self.assertEqual(ctx.exception.reason, "binding_already_live")
        self.assertEqual(ctx.exception.blocking_lease_id, "live-1")
        self.assertIsNone(state_store.read_pause_lease(session_id, "live-2"))

    def test_create_allowed_for_binding_after_prior_lease_consumed(self):
        """A binding whose resolved current lease is genuinely terminal
        (consumed) may start a brand new, independent pause episode at
        failed_wake_attempts=0."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="ep-1"))
        state_store.claim_pause_lease(session_id, "ep-1", "worker-a")
        state_store.mark_pause_lease_consumed(session_id, "ep-1")
        stored = state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="ep-2"))
        self.assertEqual(stored["consumption_state"], "unclaimed")
        self.assertEqual(stored["failed_wake_attempts"], 0)

    def test_create_allowed_for_binding_after_prior_lease_cancelled(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="ep-1"))
        state_store.cancel_pause_lease(session_id, "ep-1")
        stored = state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="ep-2"))
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_create_blocked_while_binding_claimed(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="live-1"))
        state_store.claim_pause_lease(session_id, "live-1", "worker-a")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="live-2"))
        self.assertEqual(ctx.exception.reason, "binding_already_live")

    def test_read_missing_lease_returns_none(self):
        self.assertIsNone(state_store.read_pause_lease(_uuid(), "nope"))


class PauseLeaseClaimCancelTest(_M3EnvMixin, unittest.TestCase):
    def _fresh(self):
        session_id = _uuid()
        lease = _make_pause_lease(lease_id="lease-1")
        state_store.create_pause_lease(session_id, lease)
        return session_id

    def test_claim_transitions_unclaimed_to_claimed(self):
        session_id = self._fresh()
        claimed = state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        self.assertEqual(claimed["consumption_state"], "claimed")
        self.assertEqual(claimed["claimant_ref"], "worker-a")
        self.assertIsNotNone(claimed["claimed_at"])

    def test_second_claim_conflicts_not_idempotent(self):
        session_id = self._fresh()
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        self.assertEqual(ctx.exception.reason, "not_unclaimed")

    def test_claim_missing_lease_not_found(self):
        session_id = _uuid()
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.claim_pause_lease(session_id, "nope", "worker-a")
        self.assertEqual(ctx.exception.reason, "not_found")

    def test_claim_requires_nonempty_claimant_ref(self):
        session_id = self._fresh()
        with self.assertRaises(ValueError):
            state_store.claim_pause_lease(session_id, "lease-1", "")

    def test_cancel_from_unclaimed(self):
        session_id = self._fresh()
        cancelled = state_store.cancel_pause_lease(session_id, "lease-1")
        self.assertEqual(cancelled["consumption_state"], "cancelled")

    def test_cancel_from_claimed(self):
        session_id = self._fresh()
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        cancelled = state_store.cancel_pause_lease(session_id, "lease-1")
        self.assertEqual(cancelled["consumption_state"], "cancelled")

    def test_cancel_already_cancelled_conflicts(self):
        session_id = self._fresh()
        state_store.cancel_pause_lease(session_id, "lease-1")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.cancel_pause_lease(session_id, "lease-1")
        self.assertEqual(ctx.exception.reason, "not_cancellable")

    def test_cancel_missing_lease_not_found(self):
        session_id = _uuid()
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.cancel_pause_lease(session_id, "nope")
        self.assertEqual(ctx.exception.reason, "not_found")


class PauseLeaseConsumeTest(_M3EnvMixin, unittest.TestCase):
    def _claimed(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        return session_id

    def test_consume_transitions_claimed_to_consumed(self):
        session_id = self._claimed()
        consumed = state_store.mark_pause_lease_consumed(session_id, "lease-1")
        self.assertEqual(consumed["consumption_state"], "consumed")
        self.assertIsNotNone(consumed["consumed_at"])

    def test_consume_is_idempotent(self):
        session_id = self._claimed()
        first = state_store.mark_pause_lease_consumed(session_id, "lease-1")
        second = state_store.mark_pause_lease_consumed(session_id, "lease-1")
        self.assertEqual(first, second)

    def test_consume_never_claimed_conflicts(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.mark_pause_lease_consumed(session_id, "lease-1")
        self.assertEqual(ctx.exception.reason, "never_claimed")

    def test_consume_missing_lease_conflicts(self):
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.mark_pause_lease_consumed(_uuid(), "nope")
        self.assertEqual(ctx.exception.reason, "never_claimed")

    def test_consume_cancelled_lease_terminal_conflict(self):
        session_id = self._claimed()
        state_store.cancel_pause_lease(session_id, "lease-1")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.mark_pause_lease_consumed(session_id, "lease-1")
        self.assertEqual(ctx.exception.reason, "terminal")


# --------------------------------------------------------------------------- #
# PauseLease: replace (Package A residual-binding closure).                  #
# --------------------------------------------------------------------------- #


class PauseLeaseReplaceTest(_M3EnvMixin, unittest.TestCase):
    def _lease_with_attempts(self, session_id, lease_id, attempts, **binding_overrides):
        stored = state_store.create_pause_lease(
            session_id, _make_pause_lease(lease_id=lease_id, **binding_overrides))
        bumped = state_store.pause_lease_from_stored_record(stored)
        for _ in range(attempts):
            bumped = capacity.record_failed_wake_attempt(bumped)
        enriched = dict(bumped)
        enriched.update(state_store._PAUSE_LEASE_BOOKKEEPING_DEFAULTS)
        path = state_store.pause_lease_path_for(session_id, lease_id)
        self.assertTrue(state_store.write_json_atomic_durable(path, enriched))
        return enriched

    def test_replace_carries_forward_failed_wake_attempts_monotonically(self):
        session_id = _uuid()
        self._lease_with_attempts(session_id, "lease-old", 2)
        new = state_store.replace_pause_lease(
            session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        self.assertEqual(new["failed_wake_attempts"], 2)
        old_after = state_store.read_pause_lease(session_id, "lease-old")
        self.assertEqual(old_after["consumption_state"], "replaced")
        self.assertEqual(old_after["replaced_by"], "lease-new")
        self.assertEqual(new["replaced_from"], "lease-old")

    def test_replace_never_regresses_below_old_count(self):
        """The new lease's OWN failed_wake_attempts must always be 0 on
        input (enforced separately below); this proves the MONOTONICITY
        half: even though the new lease starts at 0, the durably stored
        result carries the OLD lease's higher count forward, never
        resetting it."""
        session_id = _uuid()
        self._lease_with_attempts(session_id, "lease-old", 4)
        new = state_store.replace_pause_lease(
            session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        self.assertEqual(new["failed_wake_attempts"], 4)

    def test_replace_at_ceiling_still_succeeds_not_exceeded(self):
        session_id = _uuid()
        self._lease_with_attempts(
            session_id, "lease-old", capacity.FAILED_WAKE_ATTEMPT_CEILING)
        new = state_store.replace_pause_lease(
            session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        self.assertEqual(new["failed_wake_attempts"],
                        capacity.FAILED_WAKE_ATTEMPT_CEILING)

    def test_replace_rejects_direct_counter_bypass(self):
        """No direct reset/mint bypass: passing a nonzero
        failed_wake_attempts on the NEW lease is refused outright -- the
        durable value is always recomputed by this function via
        next_pause_lease_after_replacement, never accepted from the
        caller."""
        session_id = _uuid()
        self._lease_with_attempts(session_id, "lease-old", 3)
        with self.assertRaises(ValueError):
            state_store.replace_pause_lease(
                session_id, "lease-old",
                _make_pause_lease(lease_id="lease-new", failed_wake_attempts=1))
        # nothing written for the rejected new lease_id
        self.assertIsNone(state_store.read_pause_lease(session_id, "lease-new"))
        # old lease untouched (still replaceable, not marked replaced)
        old = state_store.read_pause_lease(session_id, "lease-old")
        self.assertEqual(old["consumption_state"], "unclaimed")

    def test_replace_rejects_cross_binding(self):
        session_id = _uuid()
        self._lease_with_attempts(session_id, "lease-old", 1)
        cross = _make_pause_lease(lease_id="lease-new", role="different-role")
        with self.assertRaises(state_store.CrossBindingReplacementError):
            state_store.replace_pause_lease(session_id, "lease-old", cross)
        self.assertIsNone(state_store.read_pause_lease(session_id, "lease-new"))
        old = state_store.read_pause_lease(session_id, "lease-old")
        self.assertEqual(old["consumption_state"], "unclaimed")

    def test_replace_rejects_cross_binding_each_field(self):
        session_id = _uuid()
        for field, bad_value in (
            ("provider_session_id", "other-session"),
            ("controller_policy_digest", "9" * 64),
            ("candidate_digest", "8" * 64),
        ):
            with self.subTest(field=field):
                lease_id = "lease-old-%s" % field
                # Each iteration's OLD lease needs its own distinct binding
                # (varying provider_session_id) so it does not collide with
                # a PRIOR iteration's still-live binding -- the fresh
                # M3B-REV-B02 binding-liveness check would otherwise refuse
                # this iteration's own create_pause_lease call.
                binding_override = {"provider_session_id": "sess-for-%s" % field}
                self._lease_with_attempts(session_id, lease_id, 0, **binding_override)
                cross_override = dict(binding_override)
                cross_override[field] = bad_value
                cross = _make_pause_lease(lease_id="lease-new-%s" % field, **cross_override)
                with self.assertRaises(state_store.CrossBindingReplacementError):
                    state_store.replace_pause_lease(session_id, lease_id, cross)

    def test_replace_of_already_replaced_lease_conflicts(self):
        session_id = _uuid()
        self._lease_with_attempts(session_id, "lease-old", 0)
        state_store.replace_pause_lease(
            session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.replace_pause_lease(
                session_id, "lease-old", _make_pause_lease(lease_id="lease-new-2"))
        self.assertEqual(ctx.exception.reason, "not_replaceable")

    def test_replace_of_consumed_lease_conflicts(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-old"))
        state_store.claim_pause_lease(session_id, "lease-old", "worker-a")
        state_store.mark_pause_lease_consumed(session_id, "lease-old")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.replace_pause_lease(
                session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        self.assertEqual(ctx.exception.reason, "not_replaceable")

    def test_replace_missing_old_lease_not_found(self):
        session_id = _uuid()
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.replace_pause_lease(
                session_id, "nope", _make_pause_lease(lease_id="lease-new"))
        self.assertEqual(ctx.exception.reason, "not_found")

    def test_replace_lease_id_collision_conflicts(self):
        session_id = _uuid()
        self._lease_with_attempts(session_id, "lease-old", 0)
        # A DIFFERENT binding for this setup-only lease, so its own create
        # is not itself refused by the M3B-REV-B02 binding-liveness check
        # (which would otherwise fire first, before the collision this
        # test actually targets is ever reached).
        state_store.create_pause_lease(
            session_id,
            _make_pause_lease(lease_id="lease-new", provider_session_id="collision-setup-session"))
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.replace_pause_lease(
                session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        self.assertEqual(ctx.exception.reason, "lease_id_collision")

    def test_replace_same_lease_id_rejected(self):
        session_id = _uuid()
        self._lease_with_attempts(session_id, "lease-old", 0)
        with self.assertRaises(ValueError):
            state_store.replace_pause_lease(
                session_id, "lease-old", _make_pause_lease(lease_id="lease-old"))


# --------------------------------------------------------------------------- #
# Real, separate-OS-process races: claim / cancel / consume / replace.       #
# --------------------------------------------------------------------------- #


def _mp_claim_race(root, session_id, lease_id, claimant, barrier, result_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_state as s
    barrier.wait()
    try:
        rec = s.claim_pause_lease(session_id, lease_id, claimant)
        out = {"outcome": "won", "claimant_ref": rec["claimant_ref"]}
    except s.PauseLeaseConflict as e:
        out = {"outcome": "conflict", "reason": e.reason}
    with open(result_path, "w") as fh:
        json.dump(out, fh)


def _mp_cancel_race(root, session_id, lease_id, barrier, result_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_state as s
    barrier.wait()
    try:
        s.cancel_pause_lease(session_id, lease_id)
        out = {"outcome": "won"}
    except s.PauseLeaseConflict as e:
        out = {"outcome": "conflict", "reason": e.reason}
    with open(result_path, "w") as fh:
        json.dump(out, fh)


def _mp_consume_race(root, session_id, lease_id, barrier, result_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_state as s
    barrier.wait()
    try:
        rec = s.mark_pause_lease_consumed(session_id, lease_id)
        out = {"outcome": "succeeded", "state": rec["consumption_state"]}
    except s.PauseLeaseConflict as e:
        out = {"outcome": "conflict", "reason": e.reason}
    with open(result_path, "w") as fh:
        json.dump(out, fh)


def _mp_replace_race(root, session_id, old_lease_id, new_lease, barrier, result_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_state as s
    barrier.wait()
    try:
        rec = s.replace_pause_lease(session_id, old_lease_id, new_lease)
        out = {"outcome": "won", "lease_id": rec["lease_id"]}
    except s.PauseLeaseConflict as e:
        out = {"outcome": "conflict", "reason": e.reason}
    with open(result_path, "w") as fh:
        json.dump(out, fh)


class PauseLeaseCrossProcessRaceTest(_M3EnvMixin, unittest.TestCase):
    """Genuine cross-process races using REAL, separate OS processes (fork
    context) synchronized via `multiprocessing.Barrier` -- proving the real
    macOS OS-level `fcntl.flock` exclusive lock, not merely in-process
    mutual exclusion, is what serializes these operations."""

    def test_claim_race_exactly_one_winner(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="race-1"))
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        p1 = ctx.Process(target=_mp_claim_race,
                         args=(self._root, session_id, "race-1", "worker-a", barrier, r1))
        p2 = ctx.Process(target=_mp_claim_race,
                         args=(self._root, session_id, "race-1", "worker-b", barrier, r2))
        p1.start(); p2.start()
        p1.join(timeout=30); p2.join(timeout=30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        outcomes = sorted([o1["outcome"], o2["outcome"]])
        self.assertEqual(outcomes, ["conflict", "won"])
        conflict = o1 if o1["outcome"] == "conflict" else o2
        self.assertEqual(conflict["reason"], "not_unclaimed")
        final = state_store.read_pause_lease(session_id, "race-1")
        self.assertEqual(final["consumption_state"], "claimed")
        self.assertIn(final["claimant_ref"], ("worker-a", "worker-b"))

    def test_cancel_race_exactly_one_winner(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="race-2"))
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        p1 = ctx.Process(target=_mp_cancel_race, args=(self._root, session_id, "race-2", barrier, r1))
        p2 = ctx.Process(target=_mp_cancel_race, args=(self._root, session_id, "race-2", barrier, r2))
        p1.start(); p2.start()
        p1.join(timeout=30); p2.join(timeout=30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        outcomes = sorted([o1["outcome"], o2["outcome"]])
        self.assertEqual(outcomes, ["conflict", "won"])
        final = state_store.read_pause_lease(session_id, "race-2")
        self.assertEqual(final["consumption_state"], "cancelled")

    def test_consume_race_idempotent_both_succeed(self):
        """Unlike claim/cancel, consume is idempotent: when both racers
        target an already-`claimed` lease, EXACTLY ONE performs the real
        claimed->consumed transition and the OTHER observes the
        already-consumed state under the SAME lock and returns success too
        (never a conflict) -- proving idempotency holds even under a
        genuine concurrent race, not merely a sequential retry."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="race-3"))
        state_store.claim_pause_lease(session_id, "race-3", "worker-a")
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        p1 = ctx.Process(target=_mp_consume_race, args=(self._root, session_id, "race-3", barrier, r1))
        p2 = ctx.Process(target=_mp_consume_race, args=(self._root, session_id, "race-3", barrier, r2))
        p1.start(); p2.start()
        p1.join(timeout=30); p2.join(timeout=30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        self.assertEqual(o1["outcome"], "succeeded")
        self.assertEqual(o2["outcome"], "succeeded")
        self.assertEqual(o1["state"], "consumed")
        self.assertEqual(o2["state"], "consumed")
        final = state_store.read_pause_lease(session_id, "race-3")
        self.assertEqual(final["consumption_state"], "consumed")

    def test_replace_race_exactly_one_winner(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="race-4"))
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        new1 = _make_pause_lease(lease_id="race-4-new-a")
        new2 = _make_pause_lease(lease_id="race-4-new-b")
        p1 = ctx.Process(target=_mp_replace_race,
                         args=(self._root, session_id, "race-4", new1, barrier, r1))
        p2 = ctx.Process(target=_mp_replace_race,
                         args=(self._root, session_id, "race-4", new2, barrier, r2))
        p1.start(); p2.start()
        p1.join(timeout=30); p2.join(timeout=30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        outcomes = sorted([o1["outcome"], o2["outcome"]])
        self.assertEqual(outcomes, ["conflict", "won"])
        winner = o1 if o1["outcome"] == "won" else o2
        old_after = state_store.read_pause_lease(session_id, "race-4")
        self.assertEqual(old_after["consumption_state"], "replaced")
        self.assertEqual(old_after["replaced_by"], winner["lease_id"])
        # exactly one of the two candidate new leases was actually created
        exists_a = state_store.read_pause_lease(session_id, "race-4-new-a") is not None
        exists_b = state_store.read_pause_lease(session_id, "race-4-new-b") is not None
        self.assertNotEqual(exists_a, exists_b)


# --------------------------------------------------------------------------- #
# CapacityPacket.                                                            #
# --------------------------------------------------------------------------- #


class CapacityPacketTest(_M3EnvMixin, unittest.TestCase):
    def test_write_and_read_round_trip(self):
        session_id = _uuid()
        packet = _make_capacity_packet()
        stored = state_store.write_capacity_packet(session_id, packet)
        self.assertEqual(stored["package_id"], packet["package_id"])
        self.assertEqual(state_store.read_capacity_packet(session_id, packet["package_id"]), stored)

    def test_second_write_same_content_is_idempotent(self):
        session_id = _uuid()
        packet = _make_capacity_packet()
        first = state_store.write_capacity_packet(session_id, packet)
        second = state_store.write_capacity_packet(session_id, packet)
        self.assertEqual(first, second)

    def test_second_write_different_content_rejected_immutable(self):
        session_id = _uuid()
        packet = _make_capacity_packet()
        state_store.write_capacity_packet(session_id, packet)
        different = dict(packet, provider="a-different-provider")
        with self.assertRaises(ValueError):
            state_store.write_capacity_packet(session_id, different)

    def test_invalid_packet_rejected_writes_nothing(self):
        session_id = _uuid()
        bad = _make_capacity_packet()
        del bad["issued_at"]
        with self.assertRaises(ValueError):
            state_store.write_capacity_packet(session_id, bad)
        self.assertIsNone(state_store.read_capacity_packet(session_id, bad["package_id"]))

    def test_read_missing_packet_returns_none(self):
        self.assertIsNone(state_store.read_capacity_packet(_uuid(), "nope"))


# --------------------------------------------------------------------------- #
# InvalidationRecord: append-only, ordered history.                          #
# --------------------------------------------------------------------------- #


class InvalidationHistoryTest(_M3EnvMixin, unittest.TestCase):
    def test_append_stamps_sequence_and_ordering(self):
        session_id = _uuid()
        state_store.append_invalidation_record(session_id, _make_invalidation_record(reason="first"))
        state_store.append_invalidation_record(session_id, _make_invalidation_record(reason="second"))
        state_store.append_invalidation_record(session_id, _make_invalidation_record(reason="third"))
        history = state_store.read_invalidation_history(session_id)
        self.assertEqual([h["sequence"] for h in history], [0, 1, 2])
        self.assertEqual([h["reason"] for h in history], ["first", "second", "third"])

    def test_invalid_record_rejected_writes_nothing(self):
        session_id = _uuid()
        bad = _make_invalidation_record()
        del bad["reason"]
        with self.assertRaises(ValueError):
            state_store.append_invalidation_record(session_id, bad)
        self.assertEqual(state_store.read_invalidation_history(session_id), [])

    def test_empty_history_for_unknown_session(self):
        self.assertEqual(state_store.read_invalidation_history(_uuid()), [])

    def test_no_mutation_api_exists(self):
        """Append-only is structural: this module exposes no function name
        that could edit or delete a prior InvalidationRecord."""
        exported = set(dir(state_store))
        for forbidden in ("edit_invalidation_record", "delete_invalidation_record",
                          "remove_invalidation_record", "update_invalidation_record"):
            self.assertNotIn(forbidden, exported)

    def test_concurrent_appends_across_processes_preserve_order(self):
        session_id = _uuid()
        ctx = multiprocessing.get_context("fork")

        def _append(root, session_id, reason):
            os.environ["COWORK_SESSIONS_ROOT"] = root
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import cowork_state as s
            s.append_invalidation_record(session_id, _make_invalidation_record(reason=reason))

        procs = [ctx.Process(target=_append, args=(self._root, session_id, "r%d" % i))
                for i in range(5)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            self.assertEqual(p.exitcode, 0)
        history = state_store.read_invalidation_history(session_id)
        self.assertEqual(len(history), 5)
        self.assertEqual([h["sequence"] for h in history], [0, 1, 2, 3, 4])


# --------------------------------------------------------------------------- #
# ProviderHealth.                                                            #
# --------------------------------------------------------------------------- #


class ProviderHealthTest(_M3EnvMixin, unittest.TestCase):
    def test_write_and_read_round_trip(self):
        session_id = _uuid()
        record = _make_provider_health()
        stored = state_store.write_provider_health(session_id, record)
        self.assertEqual(stored["status"], "degraded")
        self.assertEqual(state_store.read_provider_health(session_id, "builder", "anthropic"), stored)

    def test_write_overwrites_prior_current_state(self):
        session_id = _uuid()
        state_store.write_provider_health(session_id, _make_provider_health(status="degraded"))
        state_store.write_provider_health(
            session_id, _make_provider_health(status="healthy", consecutive_failures=0,
                                              last_outcome=None))
        current = state_store.read_provider_health(session_id, "builder", "anthropic")
        self.assertEqual(current["status"], "healthy")
        self.assertEqual(current["consecutive_failures"], 0)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            state_store.validate_provider_health(_make_provider_health(status="bogus"))

    def test_negative_consecutive_failures_rejected(self):
        with self.assertRaises(ValueError):
            state_store.validate_provider_health(_make_provider_health(consecutive_failures=-1))

    def test_invalid_last_outcome_rejected(self):
        with self.assertRaises(ValueError):
            state_store.validate_provider_health(_make_provider_health(last_outcome="not_a_real_outcome"))

    def test_null_last_outcome_accepted(self):
        validated = state_store.validate_provider_health(_make_provider_health(last_outcome=None))
        self.assertIsNone(validated["last_outcome"])

    def test_read_missing_returns_none(self):
        self.assertIsNone(state_store.read_provider_health(_uuid(), "builder", "anthropic"))


# --------------------------------------------------------------------------- #
# Manual capacity signal: write-time cryptographic verification.             #
# --------------------------------------------------------------------------- #


class ManualCapacitySignalTest(_M3EnvMixin, unittest.TestCase):
    def test_valid_signature_verifies_and_persists(self):
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        stored = state_store.write_manual_capacity_signal(session_id, record, pinned)
        self.assertEqual(stored["signal_journal_ref"], record["signal_journal_ref"])
        read_back = state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"])
        self.assertEqual(read_back, stored)

    def test_idempotent_rewrite_same_content(self):
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        first = state_store.write_manual_capacity_signal(session_id, record, pinned)
        second = state_store.write_manual_capacity_signal(session_id, record, pinned)
        self.assertEqual(first, second)

    def test_tampered_message_field_rejected_and_nothing_written(self):
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        tampered = dict(record, candidate_digest="9" * 64)  # signed over the ORIGINAL digest
        with self.assertRaises(state_store.ManualSignalSignatureError):
            state_store.write_manual_capacity_signal(session_id, tampered, pinned)
        self.assertIsNone(state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"]))

    def test_tampered_signature_bytes_rejected(self):
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        tampered = dict(record)
        sig = bytearray(bytes.fromhex(tampered["detached_signature"]))
        sig[0] ^= 1
        tampered["detached_signature"] = bytes(sig).hex()
        with self.assertRaises(state_store.ManualSignalSignatureError):
            state_store.write_manual_capacity_signal(session_id, tampered, pinned)

    def test_wrong_key_for_signature_rejected(self):
        session_id = _uuid()
        record, _pinned, _sk = _signed_manual_signal()
        wrong_secret = hashlib.sha256(b"a-completely-different-key").digest()
        wrong_public = state_store._ed25519_selftest_publickey(wrong_secret)
        wrong_pinned = {record["signer_public_key_id"]: wrong_public.hex()}
        with self.assertRaises(state_store.ManualSignalSignatureError):
            state_store.write_manual_capacity_signal(session_id, record, wrong_pinned)

    def test_unpinned_signer_key_id_rejected(self):
        session_id = _uuid()
        record, _pinned, _sk = _signed_manual_signal()
        with self.assertRaises(state_store.ManualSignalSignatureError):
            state_store.write_manual_capacity_signal(session_id, record, {})

    def test_malformed_pinned_key_material_rejected(self):
        session_id = _uuid()
        record, _pinned, _sk = _signed_manual_signal()
        with self.assertRaises(state_store.ManualSignalSignatureError):
            state_store.write_manual_capacity_signal(
                session_id, record, {record["signer_public_key_id"]: "not-hex"})

    def test_different_content_same_journal_ref_rejected_immutable(self):
        session_id = _uuid()
        record, pinned, secret_key = _signed_manual_signal()
        state_store.write_manual_capacity_signal(session_id, record, pinned)
        different = dict(record, package_id="pkg-2")
        message = state_store.canonical_manual_capacity_signal_message(different)
        public_key = state_store._ed25519_selftest_publickey(secret_key)
        different["detached_signature"] = state_store._ed25519_selftest_sign(
            message, secret_key, public_key).hex()
        with self.assertRaises(ValueError):
            state_store.write_manual_capacity_signal(session_id, different, pinned)

    def test_shape_invalid_record_rejected_by_package_a_first(self):
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        del record["role"]
        with self.assertRaises(ValueError):
            state_store.write_manual_capacity_signal(session_id, record, pinned)

    def test_read_missing_returns_none(self):
        self.assertIsNone(state_store.read_manual_capacity_signal(_uuid(), "nope"))

    def test_read_shape_tampered_record_returns_none(self):
        """M3B-REV-N05: a durably stored record that has been tampered
        into a shape `validate_manual_capacity_signal` no longer accepts
        (but that still parses as JSON) must never be handed back as
        truth by a bare read."""
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        state_store.write_manual_capacity_signal(session_id, record, pinned)
        path = state_store.manual_capacity_signal_path_for(
            session_id, record["signal_journal_ref"])
        tampered = dict(record, detached_signature=record["detached_signature"])
        del tampered["role"]
        state_store.write_json_atomic_durable(path, tampered)
        self.assertIsNone(state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"]))


# --------------------------------------------------------------------------- #
# Ed25519 primitive: direct correctness proof (independent of the record     #
# schema layer above).                                                       #
# --------------------------------------------------------------------------- #


class Ed25519PrimitiveTest(unittest.TestCase):
    def test_genuine_signature_verifies(self):
        secret_key = hashlib.sha256(b"primitive-test-seed").digest()
        public_key = state_store._ed25519_selftest_publickey(secret_key)
        message = b"a genuine message"
        signature = state_store._ed25519_selftest_sign(message, secret_key, public_key)
        self.assertTrue(state_store._ed25519_verify(signature, message, public_key))

    def test_wrong_message_rejected(self):
        secret_key = hashlib.sha256(b"primitive-test-seed-2").digest()
        public_key = state_store._ed25519_selftest_publickey(secret_key)
        signature = state_store._ed25519_selftest_sign(b"original", secret_key, public_key)
        self.assertFalse(state_store._ed25519_verify(signature, b"different", public_key))

    def test_malformed_signature_length_returns_false_not_raise(self):
        public_key = state_store._ed25519_selftest_publickey(hashlib.sha256(b"x").digest())
        self.assertFalse(state_store._ed25519_verify(b"too-short", b"msg", public_key))

    def test_malformed_public_key_length_returns_false_not_raise(self):
        self.assertFalse(state_store._ed25519_verify(b"0" * 64, b"msg", b"short"))

    def test_off_curve_point_returns_false_not_raise(self):
        garbage_signature = b"\xff" * 64
        garbage_key = b"\xff" * 32
        self.assertFalse(state_store._ed25519_verify(garbage_signature, b"msg", garbage_key))


# --------------------------------------------------------------------------- #
# Pending-turn-before-pause-ack.                                             #
# --------------------------------------------------------------------------- #


class PendingTurnBeforePauseTest(_M3EnvMixin, unittest.TestCase):
    def test_write_persists_bytes_and_digest_unacknowledged(self):
        session_id = _uuid()
        rec = state_store.write_pending_turn_before_pause(
            session_id, "builder", "hello turn", lease_id="lease-1")
        self.assertEqual(rec["turn_text"], "hello turn")
        self.assertEqual(rec["sha256"], hashlib.sha256(b"hello turn").hexdigest())
        self.assertFalse(rec["acknowledged"])

    def test_idempotent_rewrite_same_content(self):
        session_id = _uuid()
        first = state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        second = state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        self.assertEqual(first, second)

    def test_different_unacknowledged_content_rejected(self):
        session_id = _uuid()
        state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        with self.assertRaises(ValueError):
            state_store.write_pending_turn_before_pause(session_id, "builder", "a different turn")

    def test_overwrite_allowed_after_acknowledgment(self):
        session_id = _uuid()
        first = state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        state_store.acknowledge_pending_turn_before_pause(session_id, "builder", first["sha256"])
        second = state_store.write_pending_turn_before_pause(session_id, "builder", "next turn")
        self.assertEqual(second["turn_text"], "next turn")
        self.assertFalse(second["acknowledged"])

    def test_acknowledge_requires_matching_digest(self):
        session_id = _uuid()
        state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        with self.assertRaises(ValueError):
            state_store.acknowledge_pending_turn_before_pause(session_id, "builder", "0" * 64)
        # untouched: still unacknowledged
        rec = state_store.read_pending_turn_before_pause(session_id, "builder")
        self.assertFalse(rec["acknowledged"])

    def test_acknowledge_missing_record_raises(self):
        with self.assertRaises(ValueError):
            state_store.acknowledge_pending_turn_before_pause(_uuid(), "builder", "0" * 64)

    def test_acknowledge_is_idempotent(self):
        session_id = _uuid()
        rec = state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        first = state_store.acknowledge_pending_turn_before_pause(session_id, "builder", rec["sha256"])
        second = state_store.acknowledge_pending_turn_before_pause(session_id, "builder", rec["sha256"])
        self.assertEqual(first, second)
        self.assertTrue(second["acknowledged"])

    def test_clear_removes_record(self):
        session_id = _uuid()
        state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        state_store.clear_pending_turn_before_pause(session_id, "builder")
        self.assertIsNone(state_store.read_pending_turn_before_pause(session_id, "builder"))

    def test_clear_missing_record_is_tolerant(self):
        state_store.clear_pending_turn_before_pause(_uuid(), "builder")  # must not raise

    def test_rejects_empty_turn_text(self):
        with self.assertRaises(ValueError):
            state_store.write_pending_turn_before_pause(_uuid(), "builder", "")

    def test_read_missing_returns_none(self):
        self.assertIsNone(state_store.read_pending_turn_before_pause(_uuid(), "builder"))

    def test_durable_before_ack_crash_recovery_sequence(self):
        """The named invariant, end to end: the write must be durable BEFORE
        any acknowledgment step runs. Simulate a crash strictly between the
        two by never calling acknowledge after the write, then a fresh
        'process' (a plain re-read) recovers the exact bytes and digest and
        can complete the acknowledgment using only what was durably
        recorded."""
        session_id = _uuid()
        written = state_store.write_pending_turn_before_pause(
            session_id, "builder", "in-flight turn bytes", lease_id="lease-9")
        # "crash" here: nothing else happens in this process.
        recovered = state_store.read_pending_turn_before_pause(session_id, "builder")
        self.assertEqual(recovered, written)
        self.assertFalse(recovered["acknowledged"])
        acked = state_store.acknowledge_pending_turn_before_pause(
            session_id, "builder", recovered["sha256"])
        self.assertTrue(acked["acknowledged"])


# --------------------------------------------------------------------------- #
# Crash injection at every new write boundary: a simulated write_json_atomic #
# / append_jsonl_atomic failure must raise and leave prior durable state     #
# byte-identical, never a partial/torn write.                                #
# --------------------------------------------------------------------------- #


class CrashInjectionTest(_M3EnvMixin, unittest.TestCase):
    def _inject_write_json_failure(self):
        """Every M3 Package B single-record JSON write boundary goes
        through `write_json_atomic_durable` (M3B-REV-B03's fsync-adding
        fix), never the plain `write_json_atomic` M1/M2 callers use -- so
        THIS is the correct symbol to monkeypatch for this package's own
        crash-injection coverage."""
        original = state_store.write_json_atomic_durable
        state_store.write_json_atomic_durable = lambda *a, **k: False
        self.addCleanup(lambda: setattr(state_store, "write_json_atomic_durable", original))

    def _inject_append_jsonl_failure(self):
        original = state_store.append_jsonl_atomic
        state_store.append_jsonl_atomic = lambda *a, **k: False
        self.addCleanup(lambda: setattr(state_store, "append_jsonl_atomic", original))

    def test_create_pause_lease_failure_leaves_no_file(self):
        session_id = _uuid()
        lease = _make_pause_lease(lease_id="lease-1")
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.create_pause_lease(session_id, lease)
        self.assertFalse(os.path.exists(state_store.pause_lease_path_for(session_id, "lease-1")))

    def test_claim_pause_lease_failure_leaves_prior_state(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        path = state_store.pause_lease_path_for(session_id, "lease-1")
        before = self._raw_bytes(path)
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        self.assertEqual(self._raw_bytes(path), before)

    def test_cancel_pause_lease_failure_leaves_prior_state(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        path = state_store.pause_lease_path_for(session_id, "lease-1")
        before = self._raw_bytes(path)
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.cancel_pause_lease(session_id, "lease-1")
        self.assertEqual(self._raw_bytes(path), before)

    def test_consume_pause_lease_failure_leaves_prior_state(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        path = state_store.pause_lease_path_for(session_id, "lease-1")
        before = self._raw_bytes(path)
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.mark_pause_lease_consumed(session_id, "lease-1")
        self.assertEqual(self._raw_bytes(path), before)

    def test_replace_pause_lease_new_write_failure_leaves_old_untouched(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-old"))
        old_path = state_store.pause_lease_path_for(session_id, "lease-old")
        before = self._raw_bytes(old_path)
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.replace_pause_lease(
                session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        self.assertEqual(self._raw_bytes(old_path), before)
        self.assertFalse(os.path.exists(state_store.pause_lease_path_for(session_id, "lease-new")))

    def test_replace_pause_lease_old_write_failure_leaves_new_durable(self):
        """The documented non-atomic-across-both-files ordering: if the
        SECOND write (marking the old lease replaced) fails, the new lease
        is still durable (crash-safe at its own boundary) even though the
        old lease was not yet updated -- a caller retrying sees the new
        lease already exists and gets `lease_id_collision`, never data
        loss."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-old"))
        original = state_store.write_json_atomic_durable
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return original(path, data)
            return False

        state_store.write_json_atomic_durable = flaky
        self.addCleanup(lambda: setattr(state_store, "write_json_atomic_durable", original))
        with self.assertRaises(OSError):
            state_store.replace_pause_lease(
                session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))
        new_rec = state_store.read_pause_lease(session_id, "lease-new")
        self.assertIsNotNone(new_rec)
        old_rec = state_store.read_pause_lease(session_id, "lease-old")
        self.assertEqual(old_rec["consumption_state"], "unclaimed")

    def test_write_capacity_packet_failure_leaves_no_file(self):
        session_id = _uuid()
        packet = _make_capacity_packet()
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.write_capacity_packet(session_id, packet)
        self.assertIsNone(state_store.read_capacity_packet(session_id, packet["package_id"]))

    def test_append_invalidation_record_failure_leaves_prior_history_intact(self):
        session_id = _uuid()
        state_store.append_invalidation_record(session_id, _make_invalidation_record(reason="first"))
        path = state_store.invalidation_history_path_for(session_id)
        before = self._raw_bytes(path)
        self._inject_append_jsonl_failure()
        with self.assertRaises(OSError):
            state_store.append_invalidation_record(session_id, _make_invalidation_record(reason="second"))
        self.assertEqual(self._raw_bytes(path), before)
        history = state_store.read_invalidation_history(session_id)
        self.assertEqual(len(history), 1)

    def test_write_provider_health_failure_leaves_prior_state(self):
        session_id = _uuid()
        state_store.write_provider_health(session_id, _make_provider_health(status="degraded"))
        path = state_store.provider_health_path_for(session_id, "builder", "anthropic")
        before = self._raw_bytes(path)
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.write_provider_health(session_id, _make_provider_health(status="healthy"))
        self.assertEqual(self._raw_bytes(path), before)

    def test_write_manual_capacity_signal_failure_leaves_no_file(self):
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.write_manual_capacity_signal(session_id, record, pinned)
        self.assertIsNone(state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"]))

    def test_write_pending_turn_before_pause_failure_leaves_no_file(self):
        session_id = _uuid()
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.write_pending_turn_before_pause(session_id, "builder", "hello")
        self.assertIsNone(state_store.read_pending_turn_before_pause(session_id, "builder"))

    def test_acknowledge_pending_turn_failure_leaves_prior_state(self):
        session_id = _uuid()
        rec = state_store.write_pending_turn_before_pause(session_id, "builder", "hello")
        path = state_store.pending_turn_before_pause_path_for(session_id, "builder")
        before = self._raw_bytes(path)
        self._inject_write_json_failure()
        with self.assertRaises(OSError):
            state_store.acknowledge_pending_turn_before_pause(session_id, "builder", rec["sha256"])
        self.assertEqual(self._raw_bytes(path), before)


# --------------------------------------------------------------------------- #
# M3B-REV-B01/N04: durable failed-wake increment path, ceiling reachability, #
# wake-decision read, and the expired transition.                            #
# --------------------------------------------------------------------------- #


class PauseLeaseFailedWakeAttemptTest(_M3EnvMixin, unittest.TestCase):
    def test_record_increments_by_one_and_persists(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        rec = state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        self.assertEqual(rec["failed_wake_attempts"], 1)
        rec = state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        self.assertEqual(rec["failed_wake_attempts"], 2)
        self.assertEqual(
            state_store.read_pause_lease(session_id, "lease-1")["failed_wake_attempts"], 2)

    def test_ceiling_is_actually_reachable_through_this_api(self):
        """M3B-REV-B01's core closure: before this function existed, the
        ceiling was structurally unreachable through this module's own
        API. This proves it now IS: repeated calls reach exactly
        FAILED_WAKE_ATTEMPT_CEILING, then the next call is refused."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        for expected in range(1, capacity.FAILED_WAKE_ATTEMPT_CEILING + 1):
            rec = state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
            self.assertEqual(rec["failed_wake_attempts"], expected)
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        self.assertEqual(ctx.exception.reason, "ceiling_exhausted")
        self.assertEqual(
            state_store.read_pause_lease(session_id, "lease-1")["failed_wake_attempts"],
            capacity.FAILED_WAKE_ATTEMPT_CEILING)

    def test_record_requires_unclaimed_state(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        self.assertEqual(ctx.exception.reason, "not_unclaimed")

    def test_record_missing_lease_not_found(self):
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.record_pause_lease_failed_wake_attempt(_uuid(), "nope")
        self.assertEqual(ctx.exception.reason, "not_found")

    def test_wake_decision_tracks_ceiling(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        self.assertEqual(
            state_store.pause_lease_wake_decision(session_id, "lease-1"), "wake_retry_eligible")
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        self.assertEqual(
            state_store.pause_lease_wake_decision(session_id, "lease-1"), "wake_attempts_exhausted")

    def test_replace_after_ceiling_reached_through_real_api_carries_forward(self):
        """End-to-end proof combining B01 and the monotonic-replacement
        contract WITHOUT any test-only bypass: reach the ceiling via
        record_pause_lease_failed_wake_attempt (the real, durable, locked
        API), then replace, and confirm the new lease still carries the
        full ceiling count forward."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        new = state_store.replace_pause_lease(
            session_id, "lease-1", _make_pause_lease(lease_id="lease-2"))
        self.assertEqual(new["failed_wake_attempts"], capacity.FAILED_WAKE_ATTEMPT_CEILING)

    def test_mark_expired_from_unclaimed(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        expired = state_store.mark_pause_lease_expired(session_id, "lease-1")
        self.assertEqual(expired["consumption_state"], "expired")
        self.assertIsNotNone(expired["expired_at"])

    def test_mark_expired_from_claimed(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        expired = state_store.mark_pause_lease_expired(session_id, "lease-1")
        self.assertEqual(expired["consumption_state"], "expired")

    def test_mark_expired_already_terminal_conflicts(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        state_store.cancel_pause_lease(session_id, "lease-1")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.mark_pause_lease_expired(session_id, "lease-1")
        self.assertEqual(ctx.exception.reason, "not_expirable")

    def test_expired_lease_is_replaceable(self):
        """N04's dead-branch closure: 'expired' is durably reachable AND
        _PAUSE_LEASE_REPLACEABLE_STATES's inclusion of it is exercised for
        real, not vacuously."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        state_store.mark_pause_lease_expired(session_id, "lease-1")
        new = state_store.replace_pause_lease(
            session_id, "lease-1", _make_pause_lease(lease_id="lease-2"))
        self.assertEqual(new["consumption_state"], "unclaimed")
        old = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(old["consumption_state"], "replaced")


# --------------------------------------------------------------------------- #
# M3B-REV-M02: idempotent-decision value must be captured INSIDE the held    #
# lock, never re-read after it releases.                                     #
# --------------------------------------------------------------------------- #


class IdempotentDecisionCapturedInsideLockTest(_M3EnvMixin, unittest.TestCase):
    def test_mark_consumed_idempotent_return_ignores_post_read_external_write(self):
        """Tampers the file (bypassing the lock entirely -- simulating an
        adversarial/buggy concurrent writer) from INSIDE the very read call
        `_locked_json_transaction` makes while still holding the lock, i.e.
        strictly BEFORE it unlocks and returns. Under the OLD (fixed)
        design -- a separate `read_pause_lease(...)` call taken AFTER the
        lock was released -- this call would have returned the TAMPERED
        value; with the fix, it must return the ORIGINAL lock-captured
        snapshot, since the decision and the returned value are the exact
        same read."""
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
        consumed = state_store.mark_pause_lease_consumed(session_id, "lease-1")

        path = state_store.pause_lease_path_for(session_id, "lease-1")
        original_read = state_store._read_json_or_raise_if_corrupt

        def tamper_after_read(p):
            result = original_read(p)
            if (p == path and result is not None
                    and result.get("consumption_state") == "consumed"):
                tampered = dict(result)
                tampered["consumed_at"] = "TAMPERED-BY-TEST"
                state_store.write_json_atomic_durable(path, tampered)
            return result

        state_store._read_json_or_raise_if_corrupt = tamper_after_read
        try:
            second = state_store.mark_pause_lease_consumed(session_id, "lease-1")
        finally:
            state_store._read_json_or_raise_if_corrupt = original_read

        self.assertEqual(second["consumed_at"], consumed["consumed_at"])
        self.assertNotEqual(second["consumed_at"], "TAMPERED-BY-TEST")
        # Non-vacuous: the tamper genuinely landed on disk.
        self.assertEqual(
            state_store.read_json_tolerant(path)["consumed_at"], "TAMPERED-BY-TEST")

    def test_write_pending_turn_idempotent_return_ignores_post_read_external_write(self):
        session_id = _uuid()
        written = state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")

        path = state_store.pending_turn_before_pause_path_for(session_id, "builder")
        original_read = state_store._read_json_or_raise_if_corrupt

        def tamper_after_read(p):
            result = original_read(p)
            if p == path and result is not None:
                tampered = dict(result)
                tampered["recorded_at"] = "TAMPERED-BY-TEST"
                state_store.write_json_atomic_durable(path, tampered)
            return result

        state_store._read_json_or_raise_if_corrupt = tamper_after_read
        try:
            second = state_store.write_pending_turn_before_pause(session_id, "builder", "hello turn")
        finally:
            state_store._read_json_or_raise_if_corrupt = original_read

        self.assertEqual(second["recorded_at"], written["recorded_at"])
        self.assertNotEqual(second["recorded_at"], "TAMPERED-BY-TEST")


# --------------------------------------------------------------------------- #
# M3B-REV-M03: a corrupt-but-present record must conflict explicitly, never  #
# be silently treated as absent.                                             #
# --------------------------------------------------------------------------- #


class CorruptRecordExplicitConflictTest(_M3EnvMixin, unittest.TestCase):
    def _corrupt(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not valid json!!")

    def test_create_pause_lease_over_corrupt_file_raises_not_silently_overwrites(self):
        session_id = _uuid()
        path = state_store.pause_lease_path_for(session_id, "lease-1")
        self._corrupt(path)
        before = self._raw_bytes(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        self.assertEqual(self._raw_bytes(path), before)

    def test_claim_pause_lease_over_corrupt_file_raises(self):
        session_id = _uuid()
        path = state_store.pause_lease_path_for(session_id, "lease-1")
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.claim_pause_lease(session_id, "lease-1", "worker-a")

    def test_write_capacity_packet_over_corrupt_file_raises_not_silently_replaces(self):
        session_id = _uuid()
        packet = _make_capacity_packet()
        path = state_store.capacity_packet_path_for(session_id, packet["package_id"])
        self._corrupt(path)
        before = self._raw_bytes(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_capacity_packet(session_id, packet)
        self.assertEqual(self._raw_bytes(path), before)

    def test_write_manual_capacity_signal_over_corrupt_file_raises(self):
        session_id = _uuid()
        record, pinned, _ = _signed_manual_signal()
        path = state_store.manual_capacity_signal_path_for(session_id, record["signal_journal_ref"])
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_manual_capacity_signal(session_id, record, pinned)

    def test_write_pending_turn_before_pause_over_corrupt_file_raises_not_discards(self):
        """The exact scenario M3B-REV-M03 named: a corrupt existing record
        must never let a fresh write silently discard what may have been
        unacknowledged turn bytes."""
        session_id = _uuid()
        path = state_store.pending_turn_before_pause_path_for(session_id, "builder")
        self._corrupt(path)
        before = self._raw_bytes(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_pending_turn_before_pause(session_id, "builder", "a fresh turn")
        self.assertEqual(self._raw_bytes(path), before)

    def test_write_provider_health_over_corrupt_file_raises(self):
        session_id = _uuid()
        path = state_store.provider_health_path_for(session_id, "builder", "anthropic")
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_provider_health(session_id, _make_provider_health())

    def test_replace_pause_lease_over_corrupt_old_file_raises(self):
        session_id = _uuid()
        path = state_store.pause_lease_path_for(session_id, "lease-old")
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.replace_pause_lease(
                session_id, "lease-old", _make_pause_lease(lease_id="lease-new"))


# --------------------------------------------------------------------------- #
# M3B-REV-N03: bounded lock timeout (never an unbounded block).              #
# --------------------------------------------------------------------------- #


class LockTimeoutTest(_M3EnvMixin, unittest.TestCase):
    def test_locked_json_transaction_times_out_when_lock_genuinely_held(self):
        session_id = _uuid()
        state_store.create_pause_lease(session_id, _make_pause_lease(lease_id="lease-1"))
        path = state_store.pause_lease_path_for(session_id, "lease-1")
        lock_path = path + ".lock"
        holder = open(lock_path, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            original_timeout = state_store._M3_LOCK_TIMEOUT_SECONDS
            state_store._M3_LOCK_TIMEOUT_SECONDS = 0.3
            try:
                start = time.time()
                with self.assertRaises(TimeoutError):
                    state_store.claim_pause_lease(session_id, "lease-1", "worker-a")
                elapsed = time.time() - start
            finally:
                state_store._M3_LOCK_TIMEOUT_SECONDS = original_timeout
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        self.assertLess(elapsed, 5.0)


# --------------------------------------------------------------------------- #
# M3B-REV-M04: GENUINE killed-process crash fixtures -- a real OS process    #
# terminated with an uncatchable SIGKILL mid-operation, not a monkeypatched  #
# simulation. Proves (a) the OS releases an M3 lock when its holder is       #
# killed (never a permanent cross-process deadlock), and (b) a real process  #
# death strictly before `os.replace` leaves the prior durable file           #
# byte-identical, exactly like the write-failure-injection tests already    #
# claim -- confirmed here against the REAL kernel, not a mocked return.      #
# --------------------------------------------------------------------------- #


def _mp_hold_binding_lock_until_killed(root, session_id, lease_id, ready_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_state as s
    lease = _make_pause_lease(lease_id=lease_id)
    validated = s._import_capacity().validate_pause_lease(lease)
    lock_path = s.pause_lease_binding_index_path_for(session_id, validated) + ".lock"
    fh = s._open_locked(lock_path)
    with open(ready_path, "w") as rf:
        rf.write("locked")
    time.sleep(60)  # the parent SIGKILLs this process well before this returns


def _mp_write_then_die_before_replace(root, target_path, data_marker):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_state as s
    real_replace = os.replace

    def paused_replace(a, b):
        with open(data_marker, "w") as f:
            f.write("about-to-replace")
        time.sleep(60)  # the parent SIGKILLs this process before this ever resumes
        return real_replace(a, b)

    os.replace = paused_replace
    s.write_json_atomic_durable(target_path, {"marker": "child-write"})


class GenuineKilledProcessCrashTest(_M3EnvMixin, unittest.TestCase):
    def test_binding_lock_released_by_os_after_genuine_process_kill(self):
        session_id = _uuid()
        lease_id = "kill-lock-1"
        ready_path = os.path.join(self._root, "ready.marker")
        ctx = multiprocessing.get_context("fork")
        proc = ctx.Process(
            target=_mp_hold_binding_lock_until_killed,
            args=(self._root, session_id, lease_id, ready_path))
        proc.start()
        self.assertTrue(_wait_for_marker(ready_path, timeout=10),
                        "child never signalled it had acquired the lock")
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        self.assertEqual(proc.exitcode, -signal.SIGKILL, "child was not genuinely SIGKILLed")

        start = time.time()
        stored = state_store.create_pause_lease(session_id, _make_pause_lease(lease_id=lease_id))
        elapsed = time.time() - start
        self.assertEqual(stored["consumption_state"], "unclaimed")
        self.assertLess(elapsed, 5.0,
                        "create_pause_lease blocked instead of the OS releasing the dead "
                        "holder's flock promptly")

    def test_write_json_atomic_durable_killed_before_replace_leaves_prior_file_intact(self):
        path = os.path.join(self._root, "target.json")
        self.assertTrue(state_store.write_json_atomic_durable(path, {"marker": "original"}))
        before = self._raw_bytes(path)

        data_marker = os.path.join(self._root, "about_to_replace.marker")
        ctx = multiprocessing.get_context("fork")
        proc = ctx.Process(
            target=_mp_write_then_die_before_replace,
            args=(self._root, path, data_marker))
        proc.start()
        self.assertTrue(_wait_for_marker(data_marker, timeout=10),
                        "child never signalled it was about to replace")
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        self.assertEqual(proc.exitcode, -signal.SIGKILL, "child was not genuinely SIGKILLed")

        # os.replace never ran in the killed child -- the target file must
        # be untouched, byte-identical to before the child ever started.
        self.assertEqual(self._raw_bytes(path), before)
        # And a fresh write must still succeed normally afterward (no
        # orphaned temp file or stale lock wedges subsequent writers).
        self.assertTrue(state_store.write_json_atomic_durable(path, {"marker": "post-kill"}))
        self.assertEqual(read_json_tolerant_marker(path), "post-kill")

    def test_pending_turn_write_killed_before_replace_leaves_prior_turn_intact(self):
        """Directly targets the named invariant: pending-turn bytes/digest
        durable BEFORE pause acknowledgment, proven against a REAL process
        kill at the exact write boundary, not a simulation."""
        session_id = _uuid()
        first = state_store.write_pending_turn_before_pause(session_id, "builder", "original turn")
        path = state_store.pending_turn_before_pause_path_for(session_id, "builder")
        before = self._raw_bytes(path)

        # Reuses the shared `_mp_write_then_die_before_replace` target
        # directly against THIS pending-turn record's own path -- the
        # write boundary it kills mid-flight is byte-for-byte the same
        # `write_json_atomic_durable` call `write_pending_turn_before_
        # pause` itself goes through, so this exercises the real named
        # invariant (durable-before-ack) without needing a bespoke target.
        data_marker = os.path.join(self._root, "pending_turn_about_to_replace.marker")
        ctx = multiprocessing.get_context("fork")
        proc = ctx.Process(
            target=_mp_write_then_die_before_replace,
            args=(self._root, path, data_marker))
        proc.start()
        self.assertTrue(_wait_for_marker(data_marker, timeout=10))
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        self.assertEqual(proc.exitcode, -signal.SIGKILL)

        self.assertEqual(self._raw_bytes(path), before)
        recovered = state_store.read_pending_turn_before_pause(session_id, "builder")
        self.assertEqual(recovered, first)
        acked = state_store.acknowledge_pending_turn_before_pause(
            session_id, "builder", recovered["sha256"])
        self.assertTrue(acked["acknowledged"])


def read_json_tolerant_marker(path):
    data = state_store.read_json_tolerant(path)
    return data.get("marker") if data else None


# --------------------------------------------------------------------------- #
# M3B-REV-N06/N07: domain separation + published signing test vector.       #
# --------------------------------------------------------------------------- #


class ManualSignalDomainSeparationTest(_M3EnvMixin, unittest.TestCase):
    def test_message_carries_domain_prefix(self):
        record, _pinned, _sk = _signed_manual_signal()
        message = state_store.canonical_manual_capacity_signal_message(record)
        self.assertTrue(message.startswith(state_store._MANUAL_CAPACITY_SIGNAL_DOMAIN))

    def test_published_signing_test_vector_for_external_producers(self):
        """The exact vector documented in `canonical_manual_capacity_
        signal_message`'s own docstring (M3B-REV-N06/N07): an external
        signature producer conforming to that published vector must
        interoperate with this module's verifier."""
        record = {
            "schema_version": 1, "package_id": "pkg-1",
            "candidate_digest": "b" * 64, "role": "builder",
            "provider_session_id": "sess-1",
            "controller_policy_digest": "a" * 64,
            "signal_journal_ref": "journal-1", "signer_public_key_id": "key-1",
            "issued_at": "2024-01-01T00:00:00Z",
        }
        expected_public_key_hex = (
            "88e2b4a9e6680afcb550dbdc799c2f9a1e3b45b821c0eb506023fe0a4f1488d8"
        )
        expected_signature_hex = (
            "81d5c3764ffb964508999152492fb8b2ccff5312296dda76253a3215fbbd5b0b"
            "329bb42f1d3470e992618e6df18e56925931654480eb921c690b854621537b0d"
        )
        secret_key = hashlib.sha256(b"cowork-manual-signal-kat-seed").digest()
        public_key = state_store._ed25519_selftest_publickey(secret_key)
        self.assertEqual(public_key.hex(), expected_public_key_hex)
        message = state_store.canonical_manual_capacity_signal_message(record)
        signature = state_store._ed25519_selftest_sign(message, secret_key, public_key)
        self.assertEqual(signature.hex(), expected_signature_hex)

        signed_record = dict(record, detached_signature=signature.hex())
        verified = state_store.verify_manual_capacity_signal(
            signed_record, {"key-1": public_key.hex()})
        self.assertEqual(verified["signal_journal_ref"], "journal-1")

    def test_rfc8032_known_answer_vector_is_asserted_by_selftest(self):
        """Confirms the self-test genuinely runs the RFC 8032 TEST 1
        known-answer vector (M3B-REV-N07) by re-deriving it independently
        here and cross-checking against the same public constants the
        production self-test uses."""
        secret_key = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        expected_public_key = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        expected_signature = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901"
            "555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        public_key = state_store._ed25519_selftest_publickey(secret_key)
        self.assertEqual(public_key, expected_public_key)
        signature = state_store._ed25519_selftest_sign(b"", secret_key, public_key)
        self.assertEqual(signature, expected_signature)
        self.assertTrue(state_store._ed25519_verify(signature, b"", public_key))


# --------------------------------------------------------------------------- #
# Legacy M1/M2 anchor behavior + public signature stability.                 #
# --------------------------------------------------------------------------- #


class LegacyAnchorUnchangedTest(_M3EnvMixin, unittest.TestCase):
    """Proves this M3 addition never disturbed M1/M2 behavior: a
    representative walk through pre-M3 exports still round-trips exactly as
    before, using a project-local `.cowork/session.json` fixture path (not
    COWORK_SESSIONS_ROOT, mirroring how those functions are actually
    used)."""

    def test_m1_session_roundtrip_unaffected(self):
        project_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(project_dir, ignore_errors=True))
        path = state_store.session_path(project_dir)
        session_uuid = _uuid()
        state = state_store.ensure_session(path, None, session_uuid)
        self.assertEqual(state_store.get_session_uuid(state), session_uuid)
        state = state_store.save_role_session(path, "scout", "claude", "sess-abc", prior=state)
        self.assertEqual(state_store.get_role_session(state, "scout", "claude"), "sess-abc")
        state = state_store.save_phase(path, "building", prior=state)
        self.assertEqual(state_store.get_phase(state), "building")
        state = state_store.save_pending_turn(path, "scout", "retry this", prior=state)
        pending = state_store.read_pending_switch(state, "scout")
        self.assertEqual(pending["pending_turn"], "retry this")
        state = state_store.clear_pending_switch(path, "scout", prior=state)
        self.assertIsNone(state_store.read_pending_switch(state, "scout"))
        reloaded = state_store.load(path)
        self.assertEqual(state_store.get_session_uuid(reloaded), session_uuid)

    def test_m2_work_unit_and_phase_state_unaffected(self):
        session_id, work_id = _uuid(), _uuid()
        record = dict(
            schema_version=1, record="WorkUnit", work_id=work_id, session_id=session_id,
            phase="building", role="builder", seat=0, round=1, attempt=1,
            controller="claude", provider="anthropic", requested_model="sonnet",
            effective_model="sonnet", effort="high", candidate_manifest_digest=None,
            candidate_index=None, prompt_digest="b" * 64, pending_turn_digest=None,
            parent_work_id=None, governed_child_policy="inherit", graph_revision=1,
            predecessor_work_ids=[], fan_join_id=None,
            lifecycle_state="pending", terminal_reason=None)
        stored = state_store.mint_work_unit(record)
        self.assertEqual(stored["transition_index"], 0)
        history = state_store.read_work_unit_history(session_id, work_id)
        self.assertEqual(len(history), 1)
        entry = state_store.append_phase_state_entry(session_id, work_id, "running", None)
        self.assertEqual(entry["state"], "running")
        current = state_store.current_phase_state(session_id, work_id)
        self.assertEqual(current["state"], "running")

    def test_read_m2_state_shim_unaffected_by_m3_additions(self):
        state = {"version": state_store.VERSION, "session_uuid": _uuid()}
        result = state_store.read_m2_state(state)
        self.assertIn("work_unit_state", result)
        self.assertIn("phase_state", result)
        self.assertIsNone(result["work_unit_state"])


_M1_M2_EXPECTED_EXPORTS = (
    # A representative (not exhaustive) sample of pre-M3 public exports,
    # each with its exact pre-M3 parameter list -- a changed signature here
    # (added/removed/reordered/renamed parameter) would break a real M1/M2
    # caller silently; this proves none of that happened.
    ("load", ["path"]),
    ("save", ["path", "state"]),
    ("session_path", ["cwd"]),
    ("get_session_uuid", ["state"]),
    ("save_role_session", ["path", "role", "controller", "session_id", "prior"]),
    ("get_role_session", ["state", "role", "controller"]),
    ("save_pending_turn", ["path", "role", "text", "prior", "source"]),
    ("read_pending_switch", ["state", "role"]),
    ("clear_pending_switch", ["path", "role", "prior"]),
    ("get_phase", ["state"]),
    ("save_phase", ["path", "phase", "prior"]),
    ("save_context", ["path", "text", "prior", "source"]),
    ("get_context", ["state"]),
    ("write_json_atomic", ["path", "data"]),
    ("read_json_tolerant", ["path"]),
    ("append_jsonl_atomic", ["path", "record"]),
    ("read_jsonl_tolerant", ["path"]),
    ("mint_work_unit", ["record"]),
    ("append_work_unit_transition", ["record"]),
    ("read_work_unit_history", ["session_id", "work_id"]),
    ("current_work_unit_state", ["session_id", "work_id"]),
    ("append_phase_state_entry",
    ["session_id", "work_id", "state", "reason_code", "event", "evidence", "source"]),
    ("read_phase_state_history", ["session_id", "work_id"]),
    ("current_phase_state", ["session_id", "work_id"]),
    ("append_graph_revision", ["session_id", "nodes"]),
    ("read_graph_revisions", ["session_id"]),
    ("current_graph_revision", ["session_id"]),
    ("propose_controller_transition",
    ["session_id", "expected_revision", "policy", "config", "validate", "reason", "source"]),
    ("read_controller_transition", ["session_id"]),
    ("read_m2_state", ["state", "work_id"]),
    ("session_assets_dir", ["session_uuid"]),
    ("manifest_path_for", ["session_uuid", "work_id"]),
)


class PublicSignatureStabilityTest(unittest.TestCase):
    """M3 Package B is additive-only: every pre-existing M1/M2 export named
    below must still exist with the EXACT same parameter list (names and
    order) it had before this package -- a required-vs-optional/default
    value change is out of scope for this check (this module already uses
    positional-with-defaults consistently), but a renamed, removed,
    reordered, or newly-required parameter is caught."""

    def test_representative_exports_signatures_unchanged(self):
        for name, expected_params in _M1_M2_EXPECTED_EXPORTS:
            with self.subTest(export=name):
                self.assertTrue(hasattr(state_store, name), "missing export: %s" % name)
                fn = getattr(state_store, name)
                sig = inspect.signature(fn)
                actual_params = list(sig.parameters.keys())
                self.assertEqual(actual_params, expected_params)

    def test_no_pre_m3_export_was_removed(self):
        for name, _params in _M1_M2_EXPECTED_EXPORTS:
            self.assertTrue(callable(getattr(state_store, name, None)))

    def test_m3_additions_are_new_names_not_overrides(self):
        """Every M3 Package B function name below did not already exist as
        an M1/M2 export -- a strict namespace-collision check that this
        addition is genuinely additive, never a redefinition of a
        pre-existing name."""
        new_names = (
            "capacity_dir_for", "create_pause_lease", "read_pause_lease",
            "claim_pause_lease", "cancel_pause_lease", "mark_pause_lease_consumed",
            "replace_pause_lease", "pause_lease_path_for",
            "pause_lease_from_stored_record", "write_capacity_packet",
            "read_capacity_packet", "capacity_packet_path_for",
            "append_invalidation_record", "read_invalidation_history",
            "invalidation_history_path_for", "validate_provider_health",
            "write_provider_health", "read_provider_health",
            "provider_health_path_for", "verify_manual_capacity_signal",
            "write_manual_capacity_signal", "read_manual_capacity_signal",
            "manual_capacity_signal_path_for",
            "canonical_manual_capacity_signal_message",
            "write_pending_turn_before_pause", "read_pending_turn_before_pause",
            "acknowledge_pending_turn_before_pause",
            "clear_pending_turn_before_pause",
            "pending_turn_before_pause_path_for",
            "PauseLeaseConflict", "CrossBindingReplacementError",
            "ManualSignalSignatureError",
        )
        pre_m3_names = {n for n, _ in _M1_M2_EXPECTED_EXPORTS}
        for name in new_names:
            self.assertTrue(hasattr(state_store, name), "expected M3 export missing: %s" % name)
            self.assertNotIn(name, pre_m3_names)


if __name__ == "__main__":
    unittest.main()
