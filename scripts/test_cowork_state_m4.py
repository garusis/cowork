#!/usr/bin/env python3
"""Focused tests for M4 Package B: the durable, crash-safe, append-only
activity journal (`append_activity_record`/`read_activity_history`/
`latest_activity`/`reread_before_gate`) and the durable scheduled-review
store (`write_scheduled_review`/`read_next_inspection`), plus the pure
`activity_status_age_seconds` helper -- all additive to `cowork_state.py`.

Run standalone:

    python3 -m unittest scripts.test_cowork_state_m4 -v
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_state as state_store  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class _M4EnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test, so nothing ever touches the
    real home dir (mirrors test_cowork_state_m2.py/test_cowork_state_m3.py's
    identical `_M2EnvMixin`/`_M3EnvMixin`)."""

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
# Fixture builders.                                                          #
# --------------------------------------------------------------------------- #


def _activity_record(work_id, **overrides):
    rec = dict(
        schema_version=1, record="ActivityRecord", work_id=work_id,
        time="2026-01-01T00:00:00Z", activity_class="productive_model_work",
        source="claude", artifact_fingerprint=None, artifact_delta=[],
        provider_health=None, age_seconds=0,
    )
    rec.update(overrides)
    return rec


def _scheduled_review(work_id, **overrides):
    rec = dict(
        schema_version=1, record="ScheduledReviewRecord", work_id=work_id,
        next_inspection_at="2026-01-01T00:05:00Z", interval_seconds=300,
        last_inspection_result_ref=None,
    )
    rec.update(overrides)
    return rec


def _binding(**overrides):
    b = dict(role="builder", provider_session_id="sess-1",
            controller_policy_digest="a" * 64, candidate_digest="b" * 64,
            artifact_hashes={"artifact.txt": "c" * 64})
    b.update(overrides)
    return b


def _pause_lease(lease_id=None, **overrides):
    lease = dict(schema_version=1, package_id="pkg-1",
                lease_id=lease_id or _uuid(), resume_mode="scheduled",
                not_before="2026-01-01T00:10:00Z", automation_ref="auto-1",
                consumption_state="unclaimed", failed_wake_attempts=0,
                issued_at="2026-01-01T00:00:00Z")
    lease.update(_binding())
    lease.update(overrides)
    return lease


def _capacity_packet(**overrides):
    packet = dict(
        schema_version=1, package_id="pkg-" + _uuid(),
        provider_capacity_class="subscription_quota_exhausted",
        provider="anthropic", resume_mode="scheduled", retry_after="120s",
        capacity_source={"kind": "provider_header", "sha256": "d" * 64},
        binding=_binding(),
        wakeup={"lease_id": "lease-1", "automation_ref": "auto-1",
               "not_before": "2026-01-01T00:02:00Z"},
        manual_resume={"condition": None, "accepted_source": None,
                      "signal_journal_ref": None},
        issued_at="2026-01-01T00:00:00Z")
    packet.update(overrides)
    return packet


def _provider_health(**overrides):
    rec = dict(role="builder", provider="anthropic", status="healthy",
              consecutive_failures=0, last_outcome=None,
              last_updated_at="2026-01-01T00:00:00Z")
    rec.update(overrides)
    return rec


# --------------------------------------------------------------------------- #
# append_activity_record / read_activity_history / latest_activity.          #
# --------------------------------------------------------------------------- #


class AppendAndReadTest(_M4EnvMixin, unittest.TestCase):
    def test_append_then_read_round_trips(self):
        session_uuid = _uuid()
        work_id = _uuid()
        stored = state_store.append_activity_record(
            session_uuid, _activity_record(work_id))
        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(history, [stored])
        self.assertEqual(stored["work_id"], work_id.lower())

    def test_missing_history_reads_as_empty_list(self):
        session_uuid = _uuid()
        work_id = _uuid()
        self.assertEqual(
            state_store.read_activity_history(session_uuid, work_id), [])
        self.assertIsNone(state_store.latest_activity(session_uuid, work_id))

    def test_append_is_append_only_never_overwrites(self):
        session_uuid = _uuid()
        work_id = _uuid()
        first = state_store.append_activity_record(
            session_uuid, _activity_record(work_id, time="2026-01-01T00:00:00Z"))
        second = state_store.append_activity_record(
            session_uuid, _activity_record(
                work_id, time="2026-01-01T00:01:00Z",
                activity_class="local_tool_work"))
        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(history, [first, second])

    def test_latest_activity_with_no_reconciliation_reports_raw_class(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(work_id, activity_class="provider_wait"))
        latest = state_store.latest_activity(session_uuid, work_id)
        self.assertEqual(latest["effective_classification"], "provider_wait")
        self.assertIsNone(latest["reconciliation_record"])

    def test_case_insensitive_work_id_addresses_same_history(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(work_id.upper()))
        history_lower = state_store.read_activity_history(session_uuid, work_id.lower())
        history_upper = state_store.read_activity_history(session_uuid, work_id.upper())
        self.assertEqual(len(history_lower), 1)
        self.assertEqual(history_lower, history_upper)


# --------------------------------------------------------------------------- #
# Package A validation happens BEFORE any disk write.                        #
# --------------------------------------------------------------------------- #


class ValidationBeforeDiskTest(_M4EnvMixin, unittest.TestCase):
    def test_append_activity_record_rejects_invalid_before_any_write(self):
        session_uuid = _uuid()
        work_id = _uuid()
        bad = _activity_record(work_id, activity_class="not_a_real_class")
        with self.assertRaises(ValueError):
            state_store.append_activity_record(session_uuid, bad)
        path = state_store.activity_history_path_for(session_uuid, work_id)
        self.assertFalse(os.path.exists(path))

    def test_append_activity_record_rejects_extra_key_before_any_write(self):
        session_uuid = _uuid()
        work_id = _uuid()
        bad = _activity_record(work_id, bogus="x")
        with self.assertRaises(ValueError):
            state_store.append_activity_record(session_uuid, bad)
        self.assertEqual(
            state_store.read_activity_history(session_uuid, work_id), [])

    def test_reread_before_gate_rejects_invalid_reconciled_classification(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(work_id))
        with self.assertRaises(ValueError):
            state_store.reread_before_gate(
                session_uuid, work_id, "2026-01-01T00:01:00Z",
                "not_a_real_class", "a" * 64, "poll")
        self.assertEqual(
            len(state_store.read_activity_history(session_uuid, work_id)), 1)

    def test_reread_before_gate_rejects_invalid_revision_digest(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(work_id))
        with self.assertRaises(ValueError):
            state_store.reread_before_gate(
                session_uuid, work_id, "2026-01-01T00:01:00Z",
                "local_tool_work", "not-hex64", "poll")
        self.assertEqual(
            len(state_store.read_activity_history(session_uuid, work_id)), 1)

    def test_write_scheduled_review_rejects_invalid_before_any_write(self):
        session_uuid = _uuid()
        work_id = _uuid()
        bad = _scheduled_review(work_id, interval_seconds=0)
        with self.assertRaises(ValueError):
            state_store.write_scheduled_review(session_uuid, bad)
        path = state_store.scheduled_review_path_for(session_uuid, work_id)
        self.assertFalse(os.path.exists(path))


# --------------------------------------------------------------------------- #
# reread_before_gate: identical-re-read no-op, genuine reconciliation, and   #
# the late-write-never-rewrites-a-presented-gate invariant.                  #
# --------------------------------------------------------------------------- #


class ReconciliationTest(_M4EnvMixin, unittest.TestCase):
    def test_reread_before_gate_requires_existing_activity(self):
        session_uuid = _uuid()
        work_id = _uuid()
        with self.assertRaises(ValueError):
            state_store.reread_before_gate(
                session_uuid, work_id, "2026-01-01T00:00:00Z",
                "productive_model_work", "a" * 64, "poll")
        self.assertEqual(
            state_store.read_activity_history(session_uuid, work_id), [])

    def test_identical_rereads_write_no_phantom_reconciliation(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(
            work_id, activity_class="no_evidence_silence"))

        result1 = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:01:00Z",
            "no_evidence_silence", "a" * 64, "poll-1")
        self.assertIsNone(result1)

        result2 = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:02:00Z",
            "no_evidence_silence", "a" * 64, "poll-2")
        self.assertIsNone(result2)

        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(history), 1)

    def test_genuine_change_writes_one_reconciliation_with_explicit_classes(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(
            work_id, activity_class="no_evidence_silence"))

        rec = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:01:00Z",
            "productive_model_work", "b" * 64, "quiescence-wait-2s")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["record"], "ActivityReconciliationRecord")
        self.assertEqual(rec["original_classification"], "no_evidence_silence")
        self.assertEqual(rec["reconciled_classification"], "productive_model_work")
        self.assertEqual(rec["revision_digest"], "b" * 64)
        self.assertEqual(rec["quiescence_marker"], "quiescence-wait-2s")

        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(history), 2)
        latest = state_store.latest_activity(session_uuid, work_id)
        self.assertEqual(latest["effective_classification"], "productive_model_work")
        self.assertEqual(latest["reconciliation_record"], rec)

        # A second identical re-read now compares against the RECONCILED
        # baseline -- no phantom double-reconciliation.
        result_again = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:02:00Z",
            "productive_model_work", "b" * 64, "poll-3")
        self.assertIsNone(result_again)
        self.assertEqual(
            len(state_store.read_activity_history(session_uuid, work_id)), 2)

    def test_late_write_after_gate_does_not_rewrite_presented_gate(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(
            work_id, activity_class="no_evidence_silence"))

        gate1 = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:01:00Z",
            "productive_model_work", "b" * 64, "poll-1")
        self.assertIsNotNone(gate1)

        # A LATE raw ActivityRecord write lands after the gate was already
        # presented -- it must never mutate or remove that reconciliation.
        state_store.append_activity_record(session_uuid, _activity_record(
            work_id, time="2026-01-01T00:00:30Z",
            activity_class="no_evidence_silence"))

        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[1], gate1)  # untouched, byte-identical

        # The effective state now reverts to the fresh raw record's own
        # classification, until the NEXT gate reconciles it again.
        latest = state_store.latest_activity(session_uuid, work_id)
        self.assertEqual(latest["effective_classification"], "no_evidence_silence")
        self.assertIsNone(latest["reconciliation_record"])

        gate2 = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:03:00Z",
            "productive_model_work", "c" * 64, "poll-2")
        self.assertIsNotNone(gate2)
        self.assertNotEqual(gate2, gate1)
        final_history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(final_history), 4)
        self.assertEqual(final_history[1], gate1)  # still untouched


# --------------------------------------------------------------------------- #
# Corrupt/torn-tail refusal, and its deliberate contrast with append's own   #
# self-healing repair (which this section reuses, unmodified, from M1/M2).  #
# --------------------------------------------------------------------------- #


class CorruptTailTest(_M4EnvMixin, unittest.TestCase):
    def _append_torn_fragment(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(b'{"schema_version": 1, "record": "ActivityRecord"')

    def test_torn_tail_refused_by_read_history(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(work_id))
        path = state_store.activity_history_path_for(session_uuid, work_id)
        self._append_torn_fragment(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.read_activity_history(session_uuid, work_id)

    def test_torn_tail_refused_by_latest_activity(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(work_id))
        path = state_store.activity_history_path_for(session_uuid, work_id)
        self._append_torn_fragment(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.latest_activity(session_uuid, work_id)

    def test_torn_tail_refused_by_reread_before_gate(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(work_id))
        path = state_store.activity_history_path_for(session_uuid, work_id)
        self._append_torn_fragment(path)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.reread_before_gate(
                session_uuid, work_id, "2026-01-01T00:01:00Z",
                "local_tool_work", "a" * 64, "poll")

    def test_middle_of_file_corruption_also_refused(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(work_id))
        path = state_store.activity_history_path_for(session_uuid, work_id)
        with open(path, "w") as fh:
            fh.write("not even json\n")
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.read_activity_history(session_uuid, work_id)

    def test_append_self_heals_torn_tail_then_succeeds(self):
        """append_activity_record reuses append_jsonl_atomic's own,
        unmodified repair-before-append step -- unlike the read/decide
        surfaces above, appending fresh evidence over a torn tail left by
        an earlier, unrelated crash must still succeed."""
        session_uuid = _uuid()
        work_id = _uuid()
        first = state_store.append_activity_record(session_uuid, _activity_record(work_id))
        path = state_store.activity_history_path_for(session_uuid, work_id)
        with open(path, "ab") as fh:
            fh.write(b'{"broken')

        second = state_store.append_activity_record(
            session_uuid, _activity_record(work_id, activity_class="local_tool_work"))

        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(history, [first, second])


# --------------------------------------------------------------------------- #
# Crash injection at every new write boundary, including fsync faults --    #
# and M3 file-before-parent fsync ordering.                                  #
# --------------------------------------------------------------------------- #


class CrashInjectionTest(_M4EnvMixin, unittest.TestCase):
    def _faulted_fsync(self):
        real_fsync = state_store.os.fsync

        def failing_fsync(fd):
            raise OSError("simulated fsync failure")
        return real_fsync, failing_fsync

    def test_append_activity_record_fsync_fault_rolls_back_then_resumes(self):
        session_uuid = _uuid()
        work_id = _uuid()
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.append_activity_record(
                    session_uuid, _activity_record(work_id))
        finally:
            state_store.os.fsync = real_fsync

        self.assertEqual(state_store.read_activity_history(session_uuid, work_id), [])

        # RESUME: the exact same live call, un-faulted, succeeds cleanly.
        stored = state_store.append_activity_record(
            session_uuid, _activity_record(work_id))
        self.assertEqual(
            state_store.read_activity_history(session_uuid, work_id), [stored])

    def test_append_activity_record_parent_dir_fsync_fault_rolls_back_new_file(self):
        """M3 file-before-parent fsync ordering: a newly created history
        file's own content fsync succeeds, but a failed PARENT-directory
        fsync must still roll the whole append back -- never a false
        success for a record whose directory entry was never proven
        durable."""
        session_uuid = _uuid()
        work_id = _uuid()
        real_parent_fsync = state_store._fsync_parent_dir

        def failing_parent_fsync(path):
            raise OSError("simulated parent-dir fsync failure")

        state_store._fsync_parent_dir = failing_parent_fsync
        try:
            with self.assertRaises(OSError):
                state_store.append_activity_record(
                    session_uuid, _activity_record(work_id))
        finally:
            state_store._fsync_parent_dir = real_parent_fsync

        self.assertEqual(state_store.read_activity_history(session_uuid, work_id), [])

        stored = state_store.append_activity_record(
            session_uuid, _activity_record(work_id))
        self.assertEqual(
            state_store.read_activity_history(session_uuid, work_id), [stored])

    def test_reread_before_gate_fsync_fault_rolls_back_then_resumes(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(
            work_id, activity_class="no_evidence_silence"))

        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.reread_before_gate(
                    session_uuid, work_id, "2026-01-01T00:01:00Z",
                    "productive_model_work", "a" * 64, "poll")
        finally:
            state_store.os.fsync = real_fsync

        self.assertEqual(
            len(state_store.read_activity_history(session_uuid, work_id)), 1)

        result = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:02:00Z",
            "productive_model_work", "a" * 64, "poll-2")
        self.assertIsNotNone(result)
        self.assertEqual(
            len(state_store.read_activity_history(session_uuid, work_id)), 2)

    def test_write_scheduled_review_fsync_fault_leaves_no_file_then_resumes(self):
        session_uuid = _uuid()
        work_id = _uuid()
        path = state_store.scheduled_review_path_for(session_uuid, work_id)
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.write_scheduled_review(
                    session_uuid, _scheduled_review(work_id))
        finally:
            state_store.os.fsync = real_fsync

        self.assertFalse(os.path.exists(path))

        stored = state_store.write_scheduled_review(
            session_uuid, _scheduled_review(work_id))
        self.assertEqual(
            state_store.read_next_inspection(session_uuid, work_id), stored)

    def test_write_scheduled_review_parent_dir_fsync_fault_never_silently_succeeds(self):
        """Mirrors test_m3_crash_resume.py's own `test_parent_fsync_failure_
        after_replace_never_silently_reports_success`: `write_scheduled_
        review` reuses `write_json_atomic_durable` (M3, unmodified) verbatim,
        so a parent-directory fsync failing AFTER `os.replace` already
        landed is disclosed exactly like that existing precedent -- the
        rename itself is not undone (there is no way to un-rename), so the
        file on disk already holds the new, complete, untorn record, but
        this call still raises (never a silent True/success) because the
        directory-entry durability it promised was never actually proven."""
        session_uuid = _uuid()
        work_id = _uuid()
        path = state_store.scheduled_review_path_for(session_uuid, work_id)
        pending = _scheduled_review(work_id)
        real_parent_fsync = state_store._fsync_parent_dir

        def failing_parent_fsync(p):
            raise OSError("simulated parent-dir fsync failure")

        state_store._fsync_parent_dir = failing_parent_fsync
        try:
            with self.assertRaises(OSError):
                state_store.write_scheduled_review(session_uuid, pending)
        finally:
            state_store._fsync_parent_dir = real_parent_fsync

        with open(path) as fh:
            on_disk = state_store.json.load(fh)
        self.assertEqual(on_disk["next_inspection_at"], pending["next_inspection_at"])

        # RESUME: an un-faulted call still succeeds and remains the
        # durable, readable current record.
        stored = state_store.write_scheduled_review(
            session_uuid, _scheduled_review(
                work_id, next_inspection_at="2026-01-01T00:20:00Z"))
        self.assertEqual(
            state_store.read_next_inspection(session_uuid, work_id), stored)


# --------------------------------------------------------------------------- #
# write_scheduled_review / read_next_inspection.                            #
# --------------------------------------------------------------------------- #


class ScheduledReviewTest(_M4EnvMixin, unittest.TestCase):
    def test_round_trip(self):
        session_uuid = _uuid()
        work_id = _uuid()
        stored = state_store.write_scheduled_review(
            session_uuid, _scheduled_review(work_id))
        self.assertEqual(
            state_store.read_next_inspection(session_uuid, work_id), stored)

    def test_missing_reads_as_none(self):
        session_uuid = _uuid()
        work_id = _uuid()
        self.assertIsNone(state_store.read_next_inspection(session_uuid, work_id))

    def test_overwrite_replaces_wholesale_not_append_only(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.write_scheduled_review(session_uuid, _scheduled_review(
            work_id, next_inspection_at="2026-01-01T00:05:00Z"))
        second = state_store.write_scheduled_review(session_uuid, _scheduled_review(
            work_id, next_inspection_at="2026-01-01T00:10:00Z",
            last_inspection_result_ref="ref-1"))
        self.assertEqual(
            state_store.read_next_inspection(session_uuid, work_id), second)

    def test_corrupt_existing_raises_and_read_stays_tolerant(self):
        session_uuid = _uuid()
        work_id = _uuid()
        path = state_store.scheduled_review_path_for(session_uuid, work_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not valid json!!")
        with open(path, "rb") as fh:
            before = fh.read()

        with self.assertRaises(state_store.CorruptRecordError):
            state_store.write_scheduled_review(
                session_uuid, _scheduled_review(work_id))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

        # A bare public read_* accessor stays tolerant, mirroring
        # read_provider_health's own M3 precedent -- corrupt-but-present
        # reads as None, never raises.
        self.assertIsNone(state_store.read_next_inspection(session_uuid, work_id))


# --------------------------------------------------------------------------- #
# Real two-thread same-work-id concurrent append race, bounded timeout.     #
# --------------------------------------------------------------------------- #


class ConcurrencyTest(_M4EnvMixin, unittest.TestCase):
    def test_two_real_threads_append_same_work_id_serialize_under_timeout(self):
        session_uuid = _uuid()
        work_id = _uuid()
        n_per_thread = 25
        errors = []

        def attempt(tag):
            try:
                for i in range(n_per_thread):
                    state_store.append_activity_record(
                        session_uuid,
                        _activity_record(
                            work_id,
                            time="2026-01-01T00:%02d:%02dZ" % (tag, i % 60),
                            activity_class=(
                                "local_tool_work" if tag else "productive_model_work")))
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                errors.append(exc)

        t1 = threading.Thread(target=attempt, args=(0,))
        t2 = threading.Thread(target=attempt, args=(1,))
        start = time.time()
        t1.start()
        t2.start()
        t1.join(timeout=20)
        t2.join(timeout=20)
        elapsed = time.time() - start

        self.assertFalse(t1.is_alive(), "thread 1 never finished (deadlock?)")
        self.assertFalse(t2.is_alive(), "thread 2 never finished (deadlock?)")
        self.assertEqual(errors, [])
        self.assertLess(elapsed, 20)

        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(history), n_per_thread * 2)

    def test_two_real_threads_reread_before_gate_serialize_without_corruption(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(
            work_id, activity_class="no_evidence_silence"))
        errors = []
        results = []
        lock = threading.Lock()

        def attempt(tag):
            try:
                r = state_store.reread_before_gate(
                    session_uuid, work_id, "2026-01-01T00:0%d:00Z" % tag,
                    "productive_model_work", chr(97 + tag) * 64, "poll-%d" % tag)
                with lock:
                    results.append(r)
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        for t in threads:
            self.assertFalse(t.is_alive(), "a reread_before_gate thread hung")

        self.assertEqual(errors, [])
        # Exactly one racer sees a genuine change and writes; the others
        # observe the now-already-reconciled classification and no-op.
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(history), 2)


# --------------------------------------------------------------------------- #
# Pure status-age helper.                                                    #
# --------------------------------------------------------------------------- #


class StatusAgeHelperTest(unittest.TestCase):
    def test_basic_delta(self):
        self.assertEqual(
            state_store.activity_status_age_seconds(
                "2026-01-01T00:00:00Z", "2026-01-01T00:00:30Z"), 30.0)

    def test_accepts_explicit_offset_form(self):
        self.assertEqual(
            state_store.activity_status_age_seconds(
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00Z"), 60.0)

    def test_negative_delta_is_clamped_to_zero(self):
        self.assertEqual(
            state_store.activity_status_age_seconds(
                "2026-01-01T00:01:00Z", "2026-01-01T00:00:00Z"), 0.0)

    def test_invalid_since_raises_value_error(self):
        with self.assertRaises(ValueError):
            state_store.activity_status_age_seconds(
                "not-a-time", "2026-01-01T00:00:00Z")

    def test_invalid_now_raises_value_error(self):
        with self.assertRaises(ValueError):
            state_store.activity_status_age_seconds(
                "2026-01-01T00:00:00Z", "not-a-time")

    def test_is_pure_never_reads_a_wall_clock(self):
        a = state_store.activity_status_age_seconds(
            "2020-06-01T00:00:00Z", "2020-06-01T00:00:05Z")
        b = state_store.activity_status_age_seconds(
            "2020-06-01T00:00:00Z", "2020-06-01T00:00:05Z")
        self.assertEqual(a, b)
        self.assertEqual(a, 5.0)


# --------------------------------------------------------------------------- #
# Namespace hygiene + legacy/M2/M3 preservation.                             #
# --------------------------------------------------------------------------- #

_M4_NEW_NAMES = (
    "activity_dir_for", "activity_history_path_for", "scheduled_review_path_for",
    "append_activity_record", "read_activity_history", "latest_activity",
    "reread_before_gate", "write_scheduled_review", "read_next_inspection",
    "activity_status_age_seconds",
)


class NamespaceAndPreservationTest(_M4EnvMixin, unittest.TestCase):
    def test_m4_names_are_present_and_callable(self):
        for name in _M4_NEW_NAMES:
            self.assertTrue(
                callable(getattr(state_store, name, None)), "missing M4 export: %s" % name)

    def test_provider_health_still_works_after_m4(self):
        session_uuid = _uuid()
        stored = state_store.write_provider_health(session_uuid, _provider_health())
        self.assertEqual(
            state_store.read_provider_health(session_uuid, "builder", "anthropic"), stored)

    def test_pause_lease_still_works_after_m4(self):
        session_uuid = _uuid()
        lease = _pause_lease()
        stored = state_store.create_pause_lease(session_uuid, lease)
        self.assertEqual(stored["consumption_state"], "unclaimed")
        read_back = state_store.read_pause_lease(session_uuid, lease["lease_id"])
        self.assertEqual(read_back["lease_id"], lease["lease_id"])

    def test_capacity_packet_still_works_after_m4(self):
        session_uuid = _uuid()
        packet = _capacity_packet()
        stored = state_store.write_capacity_packet(session_uuid, packet)
        self.assertEqual(
            state_store.read_capacity_packet(session_uuid, packet["package_id"]), stored)

    def test_legacy_session_save_load_still_works(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = state_store.session_path(d)
        state_store.ensure_session(path, None, _uuid())
        loaded = state_store.load(path)
        self.assertEqual(loaded["version"], state_store.VERSION)

    def test_phase_state_history_still_works_after_m4(self):
        session_id = _uuid()
        work_id = _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
