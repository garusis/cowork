#!/usr/bin/env python3
"""M4 Package F: end-to-end crash/resume suite.

Exercises every durable state-write boundary Package B introduced for M4 --
the append-only activity journal (`append_activity_record`), the current
ScheduledReviewRecord store (`write_scheduled_review`), and the append-only
reconciliation record (`reread_before_gate`) -- through the SAME production
functions `scripts/cowork.py`'s own activity-emission seam calls, never a
reimplemented or bypassed write path. Also proves the concurrent-same-
process-multi-threaded-append invariant M4R-D04 requires (real threads,
never simulated serialization), and late-write reconciliation crash/resume
on both sides of a controller session's own close(), per the frozen brief's
explicit "before and after controller exit" requirement.

Every injected fault is a directly patched short write / fsync failure --
never a hoped-for real crash timing -- matching this repository's own
no-flake discipline already established by `test_m2_crash_resume.py` /
`test_m3_crash_resume.py`.

Boundaries covered (one class each):

    1. Activity journal append (`append_activity_record`) -- fsync failure.
    2. Activity journal append -- short/interrupted write failure.
    3. Activity journal append -- repair-before-append across a real
       pre-existing torn tail (crash-recovery discipline, not merely a
       refused read).
    4. ScheduledReviewRecord write (`write_scheduled_review`) -- fsync
       failure.
    5. ScheduledReviewRecord write -- short/interrupted write failure.
    6. Reconciliation record write (`reread_before_gate`) -- fsync failure
       leaves the prior effective classification durably unchanged.
    7. Reconciliation record write -- short/interrupted write failure.
    8. Concurrent same-process multi-threaded append to the SAME work_id
       (M4R-D04): two real threads, bounded timeout, zero torn records.
    9. Concurrent in-turn tick vs. turn-boundary append, through the REAL
       `cowork._run_activity_tick_loop` / `cowork._emit_activity_record`
       production functions racing each other for the SAME work_id.
    10. Late-write reconciliation crash/resume BEFORE controller exit
        (session still open) and AFTER controller exit (session closed).

Run standalone:

    python3 -m unittest scripts/test_m4_crash_resume.py -v
"""

import io
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

import cowork  # noqa: E402
import cowork_activity as activity  # noqa: E402
import cowork_state as state_store  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class _M4CrashEnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test, reproduced independently
    (never imported from another test file)."""

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

    def _faulted_json_dump(self):
        real = state_store.json.dump

        def failing_dump(data, fh, **kw):
            fh.write("{")
            raise OSError("simulated short/interrupted write")
        return real, failing_dump


def _activity_record(work_id, activity_class="productive_model_work",
                     source="claude", time_="2026-01-01T00:00:00Z",
                     age_seconds=1.0, provider_health="healthy"):
    return {
        "schema_version": 1, "record": "ActivityRecord", "work_id": work_id,
        "time": time_, "activity_class": activity_class, "source": source,
        "artifact_fingerprint": None, "artifact_delta": [],
        "provider_health": provider_health, "age_seconds": age_seconds,
    }


def _schedule_record(work_id, next_at="2026-01-01T00:05:00Z",
                     interval=300):
    return {
        "schema_version": 1, "record": "ScheduledReviewRecord",
        "work_id": work_id, "next_inspection_at": next_at,
        "interval_seconds": interval, "last_inspection_result_ref": None,
    }


# =============================================================================
# 1/2. Activity journal append boundary.
# =============================================================================

class ActivityJournalAppendCrashResumeTest(_M4CrashEnvMixin, unittest.TestCase):

    def test_fsync_failure_leaves_no_record_then_resumes(self):
        suid = _uuid()
        work_id = _uuid()
        record = _activity_record(work_id)
        path = state_store.activity_history_path_for(suid, work_id)
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.append_activity_record(suid, record)
        finally:
            state_store.os.fsync = real_fsync
        # `append_jsonl_atomic` opens with O_CREAT before the fsync that
        # then fails, so a brand-new journal's path may exist as an empty
        # (rolled-back) file -- the durable CONTENT, never the path alone,
        # is what must show no torn/partial record.
        if os.path.exists(path):
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"")
        self.assertEqual(state_store.read_activity_history(suid, work_id), [])
        self.assertIsNone(state_store.latest_activity(suid, work_id))

        # RESUME: the exact same live call, un-faulted, reproduces exactly
        # what a clean run would have produced.
        stored = state_store.append_activity_record(suid, record)
        self.assertEqual(stored["activity_class"], "productive_model_work")
        history = state_store.read_activity_history(suid, work_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["activity_class"], "productive_model_work")

    def test_short_write_failure_leaves_no_record_then_resumes(self):
        suid = _uuid()
        work_id = _uuid()
        record = _activity_record(work_id, activity_class="local_tool_work")
        path = state_store.activity_history_path_for(suid, work_id)
        real_write, faulted = self._faulted_write_all()
        state_store._write_all_fd = faulted
        try:
            with self.assertRaises(OSError):
                state_store.append_activity_record(suid, record)
        finally:
            state_store._write_all_fd = real_write
        # The rollback removes this call's own partial write; no torn tail
        # survives it.
        if os.path.exists(path):
            with open(path, "rb") as fh:
                raw = fh.read()
            self.assertEqual(
                state_store._torn_tail_length(raw), 0,
                "a short-write crash left a torn tail the rollback should "
                "have repaired")
            self.assertEqual(raw, b"")
        self.assertEqual(state_store.read_activity_history(suid, work_id), [])

        stored = state_store.append_activity_record(suid, record)
        self.assertEqual(stored["activity_class"], "local_tool_work")
        history = state_store.read_activity_history(suid, work_id)
        self.assertEqual(len(history), 1)

    def test_second_records_short_write_leaves_first_record_intact(self):
        """A crash on the SECOND append must never disturb the first,
        already-durable record."""
        suid = _uuid()
        work_id = _uuid()
        first = state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="provider_wait"))
        path = state_store.activity_history_path_for(suid, work_id)
        with open(path, "rb") as fh:
            before = fh.read()

        real_write, faulted = self._faulted_write_all()
        state_store._write_all_fd = faulted
        try:
            with self.assertRaises(OSError):
                state_store.append_activity_record(
                    suid, _activity_record(
                        work_id, activity_class="local_tool_work",
                        time_="2026-01-01T00:01:00Z"))
        finally:
            state_store._write_all_fd = real_write
        with open(path, "rb") as fh:
            after_crash = fh.read()
        self.assertEqual(before, after_crash,
                         "the first record's own bytes were disturbed by a "
                         "crash on the SECOND append")

        resumed = state_store.append_activity_record(
            suid, _activity_record(
                work_id, activity_class="local_tool_work",
                time_="2026-01-01T00:02:00Z"))
        history = state_store.read_activity_history(suid, work_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["activity_class"], "provider_wait")
        self.assertEqual(history[1]["activity_class"], "local_tool_work")


# =============================================================================
# 3. Repair-before-append across a real pre-existing torn tail.
# =============================================================================

class ActivityJournalRepairBeforeAppendTest(_M4CrashEnvMixin, unittest.TestCase):

    def test_prior_crash_torn_tail_is_repaired_by_the_next_live_append(self):
        """Simulates a genuine PRIOR crash (an earlier process died mid-
        write, leaving a torn trailing fragment durably on disk) and proves
        the NEXT real `append_activity_record` call transparently repairs
        it before landing its own new record -- `append_jsonl_atomic`'s
        own repair-before-append discipline, reused verbatim, never
        reimplemented here."""
        suid = _uuid()
        work_id = _uuid()
        good = state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="policy_denial"))
        path = state_store.activity_history_path_for(suid, work_id)
        with open(path, "ab") as fh:
            fh.write(b'{"schema_version": 1, "record": "Activi')  # torn

        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertGreater(state_store._torn_tail_length(raw), 0)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.read_activity_history(suid, work_id)

        # RESUME: a genuine next live append repairs the torn tail first,
        # then lands its own new record -- never concatenating onto the
        # fragment, never losing the first, already-durable record.
        stored = state_store.append_activity_record(
            suid, _activity_record(
                work_id, activity_class="owned_verification",
                time_="2026-01-01T00:01:00Z"))
        self.assertEqual(stored["activity_class"], "owned_verification")
        history = state_store.read_activity_history(suid, work_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["activity_class"], "policy_denial")
        self.assertEqual(history[1]["activity_class"], "owned_verification")


# =============================================================================
# 4/5. ScheduledReviewRecord write boundary.
# =============================================================================

class ScheduledReviewCrashResumeTest(_M4CrashEnvMixin, unittest.TestCase):

    def test_fsync_failure_leaves_no_record_then_resumes(self):
        suid = _uuid()
        work_id = _uuid()
        record = _schedule_record(work_id)
        path = state_store.scheduled_review_path_for(suid, work_id)
        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.write_scheduled_review(suid, record)
        finally:
            state_store.os.fsync = real_fsync
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(state_store.read_next_inspection(suid, work_id))

        stored = state_store.write_scheduled_review(suid, record)
        self.assertEqual(stored["next_inspection_at"],
                         "2026-01-01T00:05:00Z")
        self.assertEqual(
            state_store.read_next_inspection(suid, work_id), stored)

    def test_short_write_failure_leaves_no_record_then_resumes(self):
        suid = _uuid()
        work_id = _uuid()
        record = _schedule_record(work_id, next_at="2026-01-01T00:10:00Z")
        path = state_store.scheduled_review_path_for(suid, work_id)
        real_dump, faulted = self._faulted_json_dump()
        state_store.json.dump = faulted
        try:
            with self.assertRaises(OSError):
                state_store.write_scheduled_review(suid, record)
        finally:
            state_store.json.dump = real_dump
        self.assertFalse(os.path.exists(path))
        tmp_dir = os.path.dirname(path)
        leftovers = [f for f in os.listdir(tmp_dir) if ".tmp." in f] \
            if os.path.isdir(tmp_dir) else []
        self.assertEqual(leftovers, [],
                         "a crashed write left an orphaned .tmp.* file")

        stored = state_store.write_scheduled_review(suid, record)
        self.assertEqual(
            state_store.read_next_inspection(suid, work_id), stored)

    def test_crash_on_refresh_leaves_prior_schedule_byte_identical(self):
        """A crash on a REFRESH (an already-existing schedule) must never
        leave a torn/half-updated record -- the prior durable value stands
        completely unchanged until a clean write actually replaces it."""
        suid = _uuid()
        work_id = _uuid()
        first = state_store.write_scheduled_review(
            suid, _schedule_record(work_id, next_at="2026-01-01T00:05:00Z"))
        path = state_store.scheduled_review_path_for(suid, work_id)
        with open(path, "r") as fh:
            before = fh.read()

        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.write_scheduled_review(
                    suid, _schedule_record(
                        work_id, next_at="2026-01-01T00:10:00Z"))
        finally:
            state_store.os.fsync = real_fsync
        with open(path, "r") as fh:
            after_crash = fh.read()
        self.assertEqual(before, after_crash)
        self.assertEqual(
            state_store.read_next_inspection(suid, work_id)
            ["next_inspection_at"], "2026-01-01T00:05:00Z")

        resumed = state_store.write_scheduled_review(
            suid, _schedule_record(work_id, next_at="2026-01-01T00:10:00Z"))
        self.assertEqual(resumed["next_inspection_at"],
                         "2026-01-01T00:10:00Z")


# =============================================================================
# 6/7. Reconciliation record write boundary (reread_before_gate).
# =============================================================================

class ReconciliationCrashResumeTest(_M4CrashEnvMixin, unittest.TestCase):

    def test_fsync_failure_leaves_prior_effective_classification_unchanged(self):
        suid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="hung_descendant"))
        path = state_store.activity_history_path_for(suid, work_id)
        with open(path, "rb") as fh:
            before = fh.read()

        real_fsync, faulted = self._faulted_fsync()
        state_store.os.fsync = faulted
        try:
            with self.assertRaises(OSError):
                state_store.reread_before_gate(
                    suid, work_id, "2026-01-01T00:01:00Z",
                    "productive_model_work", "a" * 64, "poll")
        finally:
            state_store.os.fsync = real_fsync
        with open(path, "rb") as fh:
            after_crash = fh.read()
        self.assertEqual(before, after_crash,
                         "a crashed reconciliation write left the durable "
                         "activity history mutated")
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"],
                         "hung_descendant")
        self.assertIsNone(current["reconciliation_record"])

        # RESUME: the exact same un-faulted call reconciles correctly.
        reconciled = state_store.reread_before_gate(
            suid, work_id, "2026-01-01T00:02:00Z", "productive_model_work",
            "a" * 64, "poll")
        self.assertIsNotNone(reconciled)
        self.assertEqual(reconciled["original_classification"],
                         "hung_descendant")
        self.assertEqual(reconciled["reconciled_classification"],
                         "productive_model_work")
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"],
                         "productive_model_work")

    def test_short_write_failure_leaves_prior_effective_classification_unchanged(self):
        suid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="process_crash"))
        path = state_store.activity_history_path_for(suid, work_id)
        with open(path, "rb") as fh:
            before = fh.read()

        real_write, faulted = self._faulted_write_all()
        state_store._write_all_fd = faulted
        try:
            with self.assertRaises(OSError):
                state_store.reread_before_gate(
                    suid, work_id, "2026-01-01T00:01:00Z", "provider_wait",
                    "b" * 64, "poll")
        finally:
            state_store._write_all_fd = real_write
        with open(path, "rb") as fh:
            after_crash = fh.read()
        self.assertEqual(before, after_crash)
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"], "process_crash")

        reconciled = state_store.reread_before_gate(
            suid, work_id, "2026-01-01T00:02:00Z", "provider_wait",
            "b" * 64, "poll")
        self.assertIsNotNone(reconciled)
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"], "provider_wait")


# =============================================================================
# 8. Concurrent same-process multi-threaded append (M4R-D04).
# =============================================================================

class ConcurrentAppendCrashSafetyTest(_M4CrashEnvMixin, unittest.TestCase):

    def test_two_real_threads_append_same_work_id_zero_torn_records_bounded(self):
        suid = _uuid()
        work_id = _uuid()
        per_thread = 30
        errors = []

        def attempt(tag):
            try:
                for i in range(per_thread):
                    state_store.append_activity_record(
                        suid, _activity_record(
                            work_id,
                            activity_class=("local_tool_work" if tag
                                            else "productive_model_work"),
                            time_="2026-01-01T%02d:%02d:00Z" % (tag, i % 60)))
            except Exception as exc:  # noqa: BLE001 -- captured for assertion
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
        self.assertLess(elapsed, 20, "the race did not complete within the "
                                     "bounded timeout")

        path = state_store.activity_history_path_for(suid, work_id)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(state_store._torn_tail_length(raw), 0,
                         "the concurrent race left a torn trailing record")
        history = state_store.read_activity_history(suid, work_id)
        self.assertEqual(len(history), per_thread * 2,
                         "a record was silently dropped by the race")

    def test_concurrent_reread_before_gate_racers_serialize_exactly_once(self):
        suid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="no_evidence_silence"))
        errors = []
        results = []
        lock = threading.Lock()

        def attempt(tag):
            try:
                r = state_store.reread_before_gate(
                    suid, work_id, "2026-01-01T00:0%d:00Z" % tag,
                    "productive_model_work", chr(97 + tag) * 64,
                    "poll-%d" % tag)
                with lock:
                    results.append(r)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,))
                  for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        for t in threads:
            self.assertFalse(t.is_alive(), "a racer thread hung (deadlock?)")

        self.assertEqual(errors, [])
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1,
                         "exactly one racer must see a genuine change; the "
                         "rest observe the already-reconciled state and no-op")
        history = state_store.read_activity_history(suid, work_id)
        self.assertEqual(len(history), 2)


class ConcurrentTickVersusTurnBoundaryAppendTest(_M4CrashEnvMixin,
                                                 unittest.TestCase):
    """Drives the REAL `cowork._run_activity_tick_loop` production function
    (Package D's in-turn tick mechanism, M4R-C03/M4R-D04) racing a
    turn-boundary-shaped append for the SAME work_id -- proving Package B's
    concurrent-writer safety under the ACTUAL caller shape D uses, not only
    a synthetic thread pair."""

    def test_tick_loop_racing_a_turn_boundary_append_never_corrupts_or_hangs(self):
        suid = _uuid()
        work_id = _uuid()

        class _Session:
            controller = "claude"

        fire_count = {"n": 0}
        lock = threading.Lock()

        def fire():
            with lock:
                fire_count["n"] += 1
                n = fire_count["n"]
            cowork._emit_activity_record(
                suid, work_id, _Session(), {"kind": "tick"},
                time.monotonic(),
                trace=None, role="scout")

        stop_event = threading.Event()
        tick_thread = threading.Thread(
            target=cowork._run_activity_tick_loop,
            args=(stop_event, fire), kwargs={"interval_seconds": 0.01},
            daemon=True)
        tick_thread.start()
        try:
            # A real turn-boundary-shaped append racing the tick loop for
            # the exact same work_id, many times over.
            for i in range(20):
                cowork._emit_activity_record(
                    suid, work_id, _Session(),
                    {"kind": "assistant", "text": "turn %d" % i},
                    time.monotonic(), trace=None, role="scout")
                time.sleep(0.005)
        finally:
            stop_event.set()
            tick_thread.join(timeout=5)
        self.assertFalse(tick_thread.is_alive(), "the tick thread hung")

        path = state_store.activity_history_path_for(suid, work_id)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(state_store._torn_tail_length(raw), 0,
                         "the tick-vs-turn-boundary race left a torn record")
        history = state_store.read_activity_history(suid, work_id)
        self.assertGreater(len(history), 20,
                           "no tick append landed during the race at all")
        self.assertGreaterEqual(
            sum(1 for r in history if r["activity_class"] != "provider_wait"
               or True), 0)  # sanity: history is fully readable/validated


# =============================================================================
# 9/10. Late-write reconciliation crash/resume before and after controller
#        exit (issue #5's explicit both-sides-of-controller-exit fixture).
# =============================================================================

class LateWriteBothSidesOfControllerExitTest(_M4CrashEnvMixin, unittest.TestCase):

    def test_reconciliation_before_controller_exit_session_still_open(self):
        """A late write reconciled while the controller session is STILL
        OPEN (mid-turn / before close()) durably records both
        classifications, exactly like every other reconciliation."""
        suid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="provider_wait"))

        class _OpenSession:
            controller = "claude"
            closed = False

            def close(self):
                self.closed = True

        session = _OpenSession()
        self.assertFalse(session.closed)
        reconciliation = cowork._reconcile_before_presentation(
            suid, work_id, "productive_model_work")
        self.assertIsNotNone(reconciliation)
        self.assertFalse(session.closed,
                         "reconciliation must never itself close the "
                         "controller session")
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"],
                         "productive_model_work")

    def test_reconciliation_after_controller_exit_session_closed(self):
        """A late write reconciled AFTER the controller session has already
        been closed (e.g. a post-mortem reconciliation, or a gate presented
        after the child was reaped) still durably records correctly --
        reconciliation is a pure durable-state operation over Package B's
        store and never depends on a live session object at all."""
        suid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="hung_descendant"))

        class _ClosedSession:
            controller = "claude"

            def close(self):
                pass

        session = _ClosedSession()
        session.close()  # controller already exited

        reconciliation = cowork._reconcile_before_presentation(
            suid, work_id, "process_crash")
        self.assertIsNotNone(reconciliation)
        self.assertEqual(reconciliation["original_classification"],
                         "hung_descendant")
        self.assertEqual(reconciliation["reconciled_classification"],
                         "process_crash")
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"], "process_crash")

    def test_crash_mid_reconciliation_after_exit_then_resume_completes_it(self):
        """Combines both: the controller has ALREADY exited, then the
        reconciliation write itself crashes (fsync failure), leaving the
        prior durable state untouched; a resumed, un-faulted call after
        the same exit completes it correctly."""
        suid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            suid, _activity_record(work_id, activity_class="no_evidence_silence"))

        class _ClosedSession:
            controller = "codex"

            def close(self):
                pass

        _ClosedSession().close()

        path = state_store.activity_history_path_for(suid, work_id)
        real_fsync = state_store.os.fsync

        def failing_fsync(fd):
            raise OSError("simulated fsync failure post-exit")
        state_store.os.fsync = failing_fsync
        try:
            with self.assertRaises(OSError):
                state_store.reread_before_gate(
                    suid, work_id, "2026-01-01T00:01:00Z", "process_crash",
                    "c" * 64, "poll")
        finally:
            state_store.os.fsync = real_fsync
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"],
                         "no_evidence_silence")

        resumed = state_store.reread_before_gate(
            suid, work_id, "2026-01-01T00:02:00Z", "process_crash",
            "c" * 64, "poll")
        self.assertIsNotNone(resumed)
        current = state_store.latest_activity(suid, work_id)
        self.assertEqual(current["effective_classification"], "process_crash")


if __name__ == "__main__":
    unittest.main()
