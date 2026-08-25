#!/usr/bin/env python3
"""M3 Package G: end-to-end crash/resume suite.

Exercises every durable state-write boundary Package B introduced for M3 --
CapacityPacket, PauseLease (create/claim/cancel/consume/replace/expire),
failed-wake-attempt accounting, the manual-signal journal, InvalidationRecord,
and the pending-turn-before-pause record -- through the SAME production
functions (`cowork_state.*`, `cowork_capacity_scheduler.*`) `scripts/cowork.py`
itself calls, never a reimplemented or bypassed write path. Every injected
fault is a directly patched short write / fsync failure -- never a
hoped-for real crash timing -- matching this repository's own no-flake
discipline (see `test_m2_crash_resume.py`).

Boundaries covered (one class each):

    1. CapacityPacket write (`write_capacity_packet`).
    2. PauseLease create (`create_pause_lease`), including the binding-index
       write ordering.
    3. PauseLease transitions (claim / cancel / consume / replace / expire).
    4. Failed-wake-attempt accounting (`record_pause_lease_failed_wake_attempt`).
    5. Manual-signal journal (`write_manual_capacity_signal`).
    6. InvalidationRecord append (`append_invalidation_record`).
    7. Pending-turn-before-pause (`write_pending_turn_before_pause` /
       `acknowledge_pending_turn_before_pause`).
    8. File-then-parent-directory fsync ordering (`write_json_atomic_durable`).
    9. Corrupt-state refusal (`CorruptRecordError`) across every locked
       single-record M3 store.
    10. A real, separately-spawned cross-process duplicate-claim race, at
        Package B's own `create_pause_lease` boundary (distinct from Package
        D's/E's own cross-process race coverage).

Run standalone:

    python3 -m unittest scripts/test_m3_crash_resume.py -v
"""

import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_capacity as capacity_contracts  # noqa: E402
import cowork_capacity_scheduler as scheduler  # noqa: E402
import cowork_state as state_store  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class _M3CrashEnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test, matching every other M3/M2
    crash suite's own isolation discipline, reproduced independently here."""

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

    def _faulted_fsync(self):
        real_fsync = state_store.os.fsync

        def failing_fsync(fd):
            raise OSError("simulated fsync failure")
        return real_fsync, failing_fsync

    def _faulted_write_all(self):
        real = state_store._write_all_fd

        def failing_write_all(fd, data):
            os.write(fd, data[:4])  # a genuine partial write actually lands
            raise OSError("simulated short/interrupted write")
        return real, failing_write_all


# --------------------------------------------------------------------------- #
# Shared fixtures.                                                            #
# --------------------------------------------------------------------------- #

_ARTIFACT_HASHES = {"manifest": "c" * 64}


def _capacity_packet(session_uuid, package_id="pkg-1", role="builder",
                     candidate_digest=None):
    return capacity_contracts.validate_capacity_packet({
        "schema_version": 1, "package_id": package_id,
        "provider_capacity_class": "subscription_quota_exhausted",
        "provider": "claude", "resume_mode": "scheduled",
        "retry_after": "30s",
        "capacity_source": {"kind": "provider_header", "sha256": "d" * 64},
        "binding": {"role": role, "provider_session_id": "sess-1",
                   "controller_policy_digest": "a" * 64,
                   "candidate_digest": candidate_digest or ("b" * 64),
                   "artifact_hashes": _ARTIFACT_HASHES},
        "wakeup": {"lease_id": "lease-1", "automation_ref": "auto-1",
                  "not_before": "2026-01-01T00:10:00Z"},
        "manual_resume": {"condition": None, "accepted_source": None,
                          "signal_journal_ref": None},
        "issued_at": "2026-01-01T00:00:00Z",
    })


def _pause_lease(lease_id="lease-1", role="builder", candidate_digest=None,
                 resume_mode="scheduled", not_before="2026-01-01T00:10:00Z"):
    return {
        "schema_version": 1, "package_id": "pkg-1", "lease_id": lease_id,
        "role": role, "provider_session_id": "sess-1",
        "controller_policy_digest": "a" * 64,
        "candidate_digest": candidate_digest or ("b" * 64),
        "resume_mode": resume_mode,
        "not_before": not_before if resume_mode == "scheduled" else None,
        "automation_ref": "auto-1", "artifact_hashes": _ARTIFACT_HASHES,
        "consumption_state": "unclaimed", "failed_wake_attempts": 0,
        "issued_at": "2026-01-01T00:00:00Z",
    }


def _invalidation_record(session_uuid, candidate_digest=None):
    return capacity_contracts.validate_invalidation_record({
        "schema_version": 1, "package_id": "audit-g",
        "invalidated_candidate_digest": candidate_digest or ("b" * 64),
        "invalidated_session_id": session_uuid,
        "invalidated_work_id": "builder",
        "invalidating_principal": "audit-g-crash-resume",
        "reason": "crash/resume proof",
        "evidence_refs": [{"path": "audit.md", "sha256": "a" * 64}],
        "issued_at": "2026-01-01T00:00:00Z"})


# =============================================================================
# 1. CapacityPacket write boundary.
# =============================================================================

class CapacityPacketCrashResumeTest(_M3CrashEnvMixin, unittest.TestCase):

    def test_fsync_failure_leaves_no_packet_then_resumes(self):
        suid = _uuid()
        packet = _capacity_packet(suid)
        path = state_store.capacity_packet_path_for(suid, "pkg-1")
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.write_capacity_packet(suid, packet)
        finally:
            state_store.os.fsync = real_fsync
        self.assertFalse(os.path.exists(path),
                         "a fsync failure must never leave a torn packet file")
        self.assertIsNone(state_store.read_capacity_packet(suid, "pkg-1"))

        # RESUME: the exact same live call, un-faulted, reproduces exactly
        # what a clean run would have produced.
        stored = state_store.write_capacity_packet(suid, packet)
        self.assertEqual(stored["package_id"], "pkg-1")
        self.assertEqual(state_store.read_capacity_packet(suid, "pkg-1"),
                         stored)

    def test_short_write_leaves_no_packet_then_resumes(self):
        suid = _uuid()
        packet = _capacity_packet(suid, package_id="pkg-2")
        path = state_store.capacity_packet_path_for(suid, "pkg-2")
        real_write, faulted = self._faulted_write_all()

        real_dump = state_store.json.dump

        def failing_dump(data, fh, **kw):
            fh.write("{")
            raise OSError("simulated short/interrupted write")
        state_store.json.dump = failing_dump
        try:
            with self.assertRaises(OSError):
                state_store.write_capacity_packet(suid, packet)
        finally:
            state_store.json.dump = real_dump
        self.assertFalse(os.path.exists(path))
        # No orphaned .tmp.* file survives the crash either.
        tmp_dir = os.path.dirname(path)
        leftovers = [f for f in os.listdir(tmp_dir) if ".tmp." in f] \
            if os.path.isdir(tmp_dir) else []
        self.assertEqual(leftovers, [])

        stored = state_store.write_capacity_packet(suid, packet)
        self.assertEqual(state_store.read_capacity_packet(suid, "pkg-2"),
                         stored)


# =============================================================================
# 2. PauseLease create boundary, including binding-index write ordering.
# =============================================================================

class PauseLeaseCreateCrashResumeTest(_M3CrashEnvMixin, unittest.TestCase):

    def test_fsync_failure_on_lease_record_leaves_nothing_then_resumes(self):
        suid = _uuid()
        lease = _pause_lease()
        lease_path = state_store.pause_lease_path_for(suid, "lease-1")
        index_path = state_store.pause_lease_binding_index_path_for(suid, lease)
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.create_pause_lease(suid, lease)
        finally:
            state_store.os.fsync = real_fsync
        self.assertFalse(os.path.exists(lease_path),
                         "no torn lease record after a crash mid-create")
        self.assertFalse(os.path.exists(index_path),
                         "the binding index must never be written before "
                         "the lease record it points to is durable")

        resumed = state_store.create_pause_lease(suid, lease)
        self.assertEqual(resumed["consumption_state"], "unclaimed")
        self.assertEqual(state_store.read_pause_lease(suid, "lease-1"),
                         resumed)
        current = scheduler.resolve_current_lease_for_binding(suid, lease)
        self.assertIsNotNone(current)
        self.assertEqual(current["lease_id"], "lease-1")

    def test_index_write_failure_after_durable_lease_is_disclosed_not_silent(self):
        """A crash strictly BETWEEN the lease record's own durable write and
        the binding-index write (a narrower window than the fsync-failure
        test above, which faults the lease write itself): `create_pause_
        lease` still raises OSError to its caller -- the operation is never
        reported as having silently succeeded -- and the durable lease
        record itself is left exactly as `mutate` produced it (schema-valid,
        never torn). This is recorded as observed behavior for Package B's
        owning package, not patched here (G is read-only production)."""
        suid = _uuid()
        lease = _pause_lease(lease_id="lease-index-fault")
        lease_path = state_store.pause_lease_path_for(suid, "lease-index-fault")
        index_path = state_store.pause_lease_binding_index_path_for(suid, lease)
        real_write_json_atomic_durable = state_store.write_json_atomic_durable
        call_count = {"n": 0}

        def selective_fault(path, data):
            call_count["n"] += 1
            if path == index_path:
                return False
            return real_write_json_atomic_durable(path, data)
        state_store.write_json_atomic_durable = selective_fault
        try:
            with self.assertRaises(OSError):
                state_store.create_pause_lease(suid, lease)
        finally:
            state_store.write_json_atomic_durable = real_write_json_atomic_durable
        # The lease record itself IS durable (the failure was strictly in
        # the index write) -- never a torn/partial record.
        stored = state_store.read_pause_lease(suid, "lease-index-fault")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["consumption_state"], "unclaimed")
        # But the failure was genuinely reported (OSError raised), never
        # silently swallowed -- a caller never mistakes this for success.


# =============================================================================
# 3. PauseLease transitions: claim / cancel / consume / replace / expire.
# =============================================================================

class PauseLeaseTransitionCrashResumeTest(_M3CrashEnvMixin, unittest.TestCase):

    def _seeded(self, suid, **kw):
        lease = _pause_lease(**kw)
        return state_store.create_pause_lease(suid, lease)

    def test_claim_fsync_failure_leaves_unclaimed_then_resumes(self):
        suid = _uuid()
        self._seeded(suid)
        before = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.claim_pause_lease(suid, "lease-1", "worker-a")
        finally:
            state_store.os.fsync = real_fsync
        after = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        self.assertEqual(before, after)
        current = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(current["consumption_state"], "unclaimed")

        resumed = state_store.claim_pause_lease(suid, "lease-1", "worker-a")
        self.assertEqual(resumed["consumption_state"], "claimed")
        self.assertEqual(resumed["claimant_ref"], "worker-a")

    def test_cancel_fsync_failure_leaves_claimed_then_resumes(self):
        suid = _uuid()
        self._seeded(suid)
        state_store.claim_pause_lease(suid, "lease-1", "worker-a")
        before = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.cancel_pause_lease(suid, "lease-1")
        finally:
            state_store.os.fsync = real_fsync
        after = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        self.assertEqual(before, after)

        resumed = state_store.cancel_pause_lease(suid, "lease-1")
        self.assertEqual(resumed["consumption_state"], "cancelled")

    def test_consume_fsync_failure_leaves_claimed_then_resumes_idempotent(self):
        suid = _uuid()
        self._seeded(suid)
        state_store.claim_pause_lease(suid, "lease-1", "worker-a")
        before = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.mark_pause_lease_consumed(suid, "lease-1")
        finally:
            state_store.os.fsync = real_fsync
        after = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        self.assertEqual(before, after)

        resumed = state_store.mark_pause_lease_consumed(suid, "lease-1")
        self.assertEqual(resumed["consumption_state"], "consumed")
        # Idempotent resume: a second un-faulted call is a harmless no-op,
        # returning the SAME durable record.
        again = state_store.mark_pause_lease_consumed(suid, "lease-1")
        self.assertEqual(again["consumed_at"], resumed["consumed_at"])

    def test_replace_fsync_failure_on_new_lease_leaves_old_lease_live(self):
        suid = _uuid()
        self._seeded(suid)
        new_lease = _pause_lease(lease_id="lease-1-replacement")
        new_lease["failed_wake_attempts"] = 0
        new_path = state_store.pause_lease_path_for(suid, "lease-1-replacement")
        old_path = state_store.pause_lease_path_for(suid, "lease-1")
        before_old = self._raw_bytes(old_path)
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.replace_pause_lease(suid, "lease-1", new_lease)
        finally:
            state_store.os.fsync = real_fsync
        self.assertFalse(os.path.exists(new_path),
                         "the new lease must never appear half-written")
        self.assertEqual(self._raw_bytes(old_path), before_old,
                         "the old lease must be untouched until the new one "
                         "is durably written first (write-new-before-old "
                         "ordering)")
        old_current = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(old_current["consumption_state"], "unclaimed")

        resumed = state_store.replace_pause_lease(suid, "lease-1", new_lease)
        self.assertEqual(resumed["consumption_state"], "unclaimed")
        old_after = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(old_after["consumption_state"], "replaced")
        self.assertEqual(old_after["replaced_by"], "lease-1-replacement")
        current = scheduler.resolve_current_lease_for_binding(suid, new_lease)
        self.assertEqual(current["lease_id"], "lease-1-replacement")

    def test_expire_fsync_failure_leaves_unclaimed_then_resumes(self):
        suid = _uuid()
        self._seeded(suid)
        before = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.mark_pause_lease_expired(suid, "lease-1")
        finally:
            state_store.os.fsync = real_fsync
        self.assertEqual(
            self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1")),
            before)
        resumed = state_store.mark_pause_lease_expired(suid, "lease-1")
        self.assertEqual(resumed["consumption_state"], "expired")


# =============================================================================
# 4. Failed-wake-attempt accounting.
# =============================================================================

class FailedWakeAttemptCrashResumeTest(_M3CrashEnvMixin, unittest.TestCase):

    def test_fsync_failure_leaves_counter_unchanged_then_resumes(self):
        suid = _uuid()
        state_store.create_pause_lease(suid, _pause_lease())
        before = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.record_pause_lease_failed_wake_attempt(
                    suid, "lease-1")
        finally:
            state_store.os.fsync = real_fsync
        after = self._raw_bytes(state_store.pause_lease_path_for(suid, "lease-1"))
        self.assertEqual(before, after)
        current = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(current["failed_wake_attempts"], 0)

        resumed = state_store.record_pause_lease_failed_wake_attempt(
            suid, "lease-1")
        self.assertEqual(resumed["failed_wake_attempts"], 1)

    def test_ceiling_never_bypassed_by_repeated_crash_retries(self):
        suid = _uuid()
        state_store.create_pause_lease(suid, _pause_lease())
        for _ in range(capacity_contracts.FAILED_WAKE_ATTEMPT_CEILING):
            state_store.record_pause_lease_failed_wake_attempt(suid, "lease-1")
        with self.assertRaises(state_store.PauseLeaseConflict) as ctx:
            state_store.record_pause_lease_failed_wake_attempt(suid, "lease-1")
        self.assertEqual(ctx.exception.reason, "ceiling_exhausted")
        current = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(current["failed_wake_attempts"],
                         capacity_contracts.FAILED_WAKE_ATTEMPT_CEILING)


# =============================================================================
# 5. Manual-signal journal write boundary.
# =============================================================================

class ManualSignalJournalCrashResumeTest(_M3CrashEnvMixin, unittest.TestCase):

    def _signed_record(self):
        import hashlib
        secret_key = hashlib.sha256(b"crash-resume-kat-seed").digest()
        public_key = state_store._ed25519_selftest_publickey(secret_key)
        record = dict(
            schema_version=1, package_id="pkg-1", candidate_digest="b" * 64,
            role="builder", provider_session_id="sess-1",
            controller_policy_digest="a" * 64,
            signal_journal_ref="journal-" + _uuid(),
            signer_public_key_id="key-1", detached_signature="00" * 64,
            issued_at="2026-01-01T00:00:00Z")
        message = state_store.canonical_manual_capacity_signal_message(record)
        signature = state_store._ed25519_selftest_sign(
            message, secret_key, public_key)
        record["detached_signature"] = signature.hex()
        return record, {"key-1": public_key.hex()}

    def test_fsync_failure_leaves_no_recorded_signal_then_resumes(self):
        suid = _uuid()
        record, pinned = self._signed_record()
        path = state_store.manual_capacity_signal_path_for(
            suid, record["signal_journal_ref"])
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.write_manual_capacity_signal(suid, record, pinned)
        finally:
            state_store.os.fsync = real_fsync
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(state_store.read_manual_capacity_signal(
            suid, record["signal_journal_ref"]))

        stored = state_store.write_manual_capacity_signal(suid, record, pinned)
        self.assertEqual(stored["signal_journal_ref"],
                         record["signal_journal_ref"])
        # Idempotent resume: re-verifying the SAME byte-identical record is
        # a harmless no-op, never a conflict.
        again = state_store.write_manual_capacity_signal(suid, record, pinned)
        self.assertEqual(again, stored)


# =============================================================================
# 6. InvalidationRecord append boundary.
# =============================================================================

class InvalidationRecordCrashResumeTest(_M3CrashEnvMixin, unittest.TestCase):

    def test_short_write_crash_mid_append_persists_no_record_then_resumes(self):
        suid = _uuid()
        path = state_store.invalidation_history_path_for(suid)
        record = _invalidation_record(suid)
        real, faulted = self._faulted_write_all()
        state_store._write_all_fd = faulted
        try:
            with self.assertRaises(OSError):
                state_store.append_invalidation_record(suid, record)
        finally:
            state_store._write_all_fd = real
        self.assertEqual(state_store.read_invalidation_history(suid), [])

        stored = state_store.append_invalidation_record(suid, record)
        self.assertEqual(stored["sequence"], 0)
        history = state_store.read_invalidation_history(suid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["invalidated_candidate_digest"],
                         record["invalidated_candidate_digest"])

    def test_fsync_failure_then_resume_produces_correctly_numbered_next_record(self):
        suid = _uuid()
        first = _invalidation_record(suid, candidate_digest="c" * 64)
        state_store.append_invalidation_record(suid, first)
        second = _invalidation_record(suid, candidate_digest="d" * 64)
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.append_invalidation_record(suid, second)
        finally:
            state_store.os.fsync = real_fsync
        self.assertEqual(len(state_store.read_invalidation_history(suid)), 1)

        stored = state_store.append_invalidation_record(suid, second)
        self.assertEqual(stored["sequence"], 1)
        history = state_store.read_invalidation_history(suid)
        self.assertEqual([h["sequence"] for h in history], [0, 1])


# =============================================================================
# 7. Pending-turn-before-pause boundary.
# =============================================================================

class PendingTurnCrashResumeTest(_M3CrashEnvMixin, unittest.TestCase):

    def test_write_fsync_failure_persists_nothing_then_resumes(self):
        suid = _uuid()
        path = state_store.pending_turn_before_pause_path_for(suid, "builder")
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.write_pending_turn_before_pause(
                    suid, "builder", "do the thing", lease_id="lease-1")
        finally:
            state_store.os.fsync = real_fsync
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(state_store.read_pending_turn_before_pause(
            suid, "builder"))

        stored = state_store.write_pending_turn_before_pause(
            suid, "builder", "do the thing", lease_id="lease-1")
        self.assertFalse(stored["acknowledged"])
        self.assertEqual(stored["turn_text"], "do the thing")

    def test_acknowledge_fsync_failure_leaves_unacknowledged_then_resumes(self):
        suid = _uuid()
        pending = state_store.write_pending_turn_before_pause(
            suid, "builder", "do the thing", lease_id="lease-1")
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.acknowledge_pending_turn_before_pause(
                    suid, "builder", pending["sha256"])
        finally:
            state_store.os.fsync = real_fsync
        current = state_store.read_pending_turn_before_pause(suid, "builder")
        self.assertFalse(current["acknowledged"])

        resumed = state_store.acknowledge_pending_turn_before_pause(
            suid, "builder", pending["sha256"])
        self.assertTrue(resumed["acknowledged"])
        # Idempotent resume: acknowledging again is a harmless no-op.
        again = state_store.acknowledge_pending_turn_before_pause(
            suid, "builder", pending["sha256"])
        self.assertTrue(again["acknowledged"])


# =============================================================================
# 8. File-then-parent-directory fsync ordering.
# =============================================================================

class FileAndParentFsyncOrderingTest(_M3CrashEnvMixin, unittest.TestCase):

    def test_file_fsync_happens_before_parent_directory_fsync(self):
        suid = _uuid()
        path = state_store.capacity_packet_path_for(suid, "pkg-order")
        calls = []
        real_fsync = state_store.os.fsync
        real_fsync_parent = state_store._fsync_parent_dir

        def recording_fsync(fd):
            calls.append("file")
            return real_fsync(fd)

        def recording_fsync_parent(p):
            # Entered strictly AFTER the data file's own fsync -- recorded
            # BEFORE delegating to the real implementation (whose own
            # internal `os.fsync(dirfd)` call also passes through the
            # patched `recording_fsync` above, appending one further
            # "file"-labeled entry for the DIRECTORY fd -- irrelevant to
            # the ordering property under test, which is only "did the
            # DATA file's fsync happen before this function was even
            # entered").
            calls.append("parent")
            return real_fsync_parent(p)
        state_store.os.fsync = recording_fsync
        state_store._fsync_parent_dir = recording_fsync_parent
        try:
            ok = state_store.write_json_atomic_durable(path, {"a": 1})
        finally:
            state_store.os.fsync = real_fsync
            state_store._fsync_parent_dir = real_fsync_parent
        self.assertTrue(ok)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], "file",
                         "the data file's own bytes must be fsynced first")
        self.assertEqual(calls[1], "parent",
                         "the parent-directory fsync must be entered "
                         "strictly after the data file's own fsync, never "
                         "before")

    def test_parent_fsync_failure_after_replace_never_silently_reports_success(self):
        """A crash strictly between the durable `os.replace` and the
        parent-directory fsync: `write_json_atomic_durable` still reports
        False (never silently True) even though the rename itself already
        landed -- disclosed here as observed Package B behavior, not a
        defect this read-only audit package patches. The durable content on
        disk is never torn/corrupt either way -- it is always exactly the
        new, complete record (the rename already succeeded) or exactly the
        prior record (rename never reached), never a half-written mix."""
        suid = _uuid()
        path = state_store.capacity_packet_path_for(suid, "pkg-parent-fault")
        real_fsync_parent = state_store._fsync_parent_dir

        def failing_fsync_parent(p):
            raise OSError("simulated parent-directory fsync failure")
        state_store._fsync_parent_dir = failing_fsync_parent
        try:
            ok = state_store.write_json_atomic_durable(path, {"a": 1})
        finally:
            state_store._fsync_parent_dir = real_fsync_parent
        self.assertFalse(ok, "a parent-fsync failure must never report True")
        # The rename itself is not rolled back by this function (it has no
        # way to un-rename); the file on disk is exactly the new content,
        # never a torn mixture -- confirmed directly.
        with open(path) as fh:
            self.assertEqual(json.load(fh), {"a": 1})


# =============================================================================
# 9. Corrupt-state refusal.
# =============================================================================

class CorruptStateRefusalTest(_M3CrashEnvMixin, unittest.TestCase):

    def _corrupt(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not valid json")

    def test_corrupt_pause_lease_refused_never_silently_absent(self):
        suid = _uuid()
        path = state_store.pause_lease_path_for(suid, "lease-1")
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.claim_pause_lease(suid, "lease-1", "worker-a")
        # The tolerant bare reader stays tolerant (unaffected contract).
        self.assertIsNone(state_store.read_pause_lease(suid, "lease-1"))
        # The corrupt bytes are left exactly as they were -- never
        # overwritten or silently discarded.
        with open(path) as fh:
            self.assertEqual(fh.read(), "{not valid json")

    def test_corrupt_capacity_packet_refused_never_overwritten(self):
        suid = _uuid()
        path = state_store.capacity_packet_path_for(suid, "pkg-1")
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_capacity_packet(suid, _capacity_packet(suid))
        with open(path) as fh:
            self.assertEqual(fh.read(), "{not valid json")

    def test_corrupt_manual_signal_refused(self):
        suid = _uuid()
        import hashlib
        secret_key = hashlib.sha256(b"corrupt-test-seed").digest()
        public_key = state_store._ed25519_selftest_publickey(secret_key)
        record = dict(
            schema_version=1, package_id="pkg-1", candidate_digest="b" * 64,
            role="builder", provider_session_id="sess-1",
            controller_policy_digest="a" * 64,
            signal_journal_ref="journal-corrupt-1",
            signer_public_key_id="key-1", detached_signature="00" * 64,
            issued_at="2026-01-01T00:00:00Z")
        message = state_store.canonical_manual_capacity_signal_message(record)
        signature = state_store._ed25519_selftest_sign(
            message, secret_key, public_key)
        record["detached_signature"] = signature.hex()
        path = state_store.manual_capacity_signal_path_for(
            suid, "journal-corrupt-1")
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_manual_capacity_signal(
                suid, record, {"key-1": public_key.hex()})

    def test_corrupt_pending_turn_refused(self):
        suid = _uuid()
        path = state_store.pending_turn_before_pause_path_for(suid, "builder")
        self._corrupt(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_pending_turn_before_pause(
                suid, "builder", "do the thing")

    def test_corrupt_pause_lease_binding_index_refused(self):
        suid = _uuid()
        lease = _pause_lease()
        state_store.create_pause_lease(suid, lease)
        index_path = state_store.pause_lease_binding_index_path_for(suid, lease)
        self._corrupt(index_path)
        other_lease = _pause_lease(lease_id="lease-2")
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.create_pause_lease(suid, other_lease)
        # The genuinely new lease record must never have been minted either
        # -- the corrupt-index refusal happens before it is written.
        self.assertIsNone(state_store.read_pause_lease(suid, "lease-2"))


# =============================================================================
# 10. Real, separately-spawned cross-process duplicate-claim race at
#     Package B's own `create_pause_lease` boundary.
# =============================================================================

def _mp_create_pause_lease(root, session_id, lease_id, role, barrier, result_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork_state as ss
    lease = {
        "schema_version": 1, "package_id": "pkg-1", "lease_id": lease_id,
        "role": role, "provider_session_id": "sess-1",
        "controller_policy_digest": "a" * 64, "candidate_digest": "b" * 64,
        "resume_mode": "scheduled", "not_before": "2026-01-01T00:10:00Z",
        "automation_ref": "auto-1", "artifact_hashes": {"manifest": "c" * 64},
        "consumption_state": "unclaimed", "failed_wake_attempts": 0,
        "issued_at": "2026-01-01T00:00:00Z",
    }
    barrier.wait()
    try:
        rec = ss.create_pause_lease(session_id, lease)
        out = {"outcome": "created", "lease_id": rec["lease_id"]}
    except ss.PauseLeaseConflict as exc:
        out = {"outcome": "conflict", "reason": exc.reason,
              "blocking_lease_id": exc.blocking_lease_id}
    with open(result_path, "w") as fh:
        json.dump(out, fh)


class RealCrossProcessDuplicateClaimRaceAtBLevelTest(
        _M3CrashEnvMixin, unittest.TestCase):

    def test_two_real_separate_processes_race_same_binding_exactly_one_creates(self):
        suid = _uuid()
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        p1 = ctx.Process(target=_mp_create_pause_lease,
                         args=(self._root, suid, "race-lease-a", "builder",
                              barrier, r1))
        p2 = ctx.Process(target=_mp_create_pause_lease,
                         args=(self._root, suid, "race-lease-b", "builder",
                              barrier, r2))
        p1.start()
        p2.start()
        p1.join(timeout=60)
        p2.join(timeout=60)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        outcomes = sorted([o1["outcome"], o2["outcome"]])
        self.assertEqual(
            outcomes, ["conflict", "created"],
            "exactly one of two genuinely separate racing OS processes "
            "creating a lease for the SAME binding must win: %r / %r"
            % (o1, o2))
        conflict = o1 if o1["outcome"] == "conflict" else o2
        self.assertEqual(conflict["reason"], "binding_already_live")
        winner_lease_id = (o1 if o1["outcome"] == "created" else o2)["lease_id"]
        self.assertEqual(conflict["blocking_lease_id"], winner_lease_id)
        binding = _pause_lease()
        current = scheduler.resolve_current_lease_for_binding(suid, binding)
        self.assertEqual(current["lease_id"], winner_lease_id)


if __name__ == "__main__":
    unittest.main()
