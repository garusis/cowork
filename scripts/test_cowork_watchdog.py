#!/usr/bin/env python3
"""Tests for M4 Package D's dual-evidence watchdog decisions
(`cowork_watchdog.py`)."""

import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_activity as activity  # noqa: E402
import cowork_watchdog as watchdog  # noqa: E402
import cowork  # noqa: E402
import cowork_state as state_store  # noqa: E402

WORK_ID = "11111111-1111-1111-1111-111111111111"
NOW = "2026-01-01T00:10:00Z"


def _activity_record(activity_class, time="2026-01-01T00:00:00Z",
                     age_seconds=5.0, source="claude"):
    return activity.validate_activity_record({
        "schema_version": 1, "record": "ActivityRecord", "work_id": WORK_ID,
        "time": time, "activity_class": activity_class, "source": source,
        "artifact_fingerprint": None, "artifact_delta": [],
        "provider_health": None, "age_seconds": age_seconds,
    })


def _reconciliation(original, reconciled, time="2026-01-01T00:05:00Z"):
    return activity.validate_activity_reconciliation_record({
        "schema_version": 1, "record": "ActivityReconciliationRecord",
        "work_id": WORK_ID, "time": time,
        "original_classification": original,
        "reconciled_classification": reconciled,
        "revision_digest": "a" * 64, "quiescence_marker": "poll",
    })


def _schedule(next_inspection_at="2026-01-01T00:05:00Z", interval=300,
             last_ref=None):
    return activity.validate_scheduled_review_record({
        "schema_version": 1, "record": "ScheduledReviewRecord",
        "work_id": WORK_ID, "next_inspection_at": next_inspection_at,
        "interval_seconds": interval, "last_inspection_result_ref": last_ref,
    })


class FakeSession(object):
    def __init__(self, controller="claude", proc=None, live_proc=None):
        self.controller = controller
        if controller == "claude":
            self.proc = proc
        else:
            self._live_proc = live_proc


class FakeProc(object):
    def __init__(self, pid=4242, alive=True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class ProcessProbeTest(unittest.TestCase):
    def test_alive_claude_child_is_truthful(self):
        session = FakeSession("claude", proc=FakeProc(pid=99, alive=True))
        alive, ref = watchdog.process_probe(session)
        self.assertTrue(alive)
        self.assertEqual(ref, "pid:99")

    def test_dead_claude_child_is_truthful(self):
        session = FakeSession("claude", proc=FakeProc(pid=99, alive=False))
        alive, ref = watchdog.process_probe(session)
        self.assertFalse(alive)
        self.assertEqual(ref, "dead:no_live_child")

    def test_no_session_object_at_all(self):
        session = FakeSession("claude", proc=None)
        alive, ref = watchdog.process_probe(session)
        self.assertFalse(alive)
        self.assertEqual(ref, "dead:no_live_child")

    def test_codex_child_probed_non_null_while_alive(self):
        session = FakeSession("codex", live_proc=FakeProc(pid=7, alive=True))
        alive, ref = watchdog.process_probe(session)
        self.assertTrue(alive)
        self.assertEqual(ref, "pid:7")

    def test_opencode_child_probed_non_null_while_alive(self):
        session = FakeSession("opencode", live_proc=FakeProc(pid=8, alive=True))
        alive, ref = watchdog.process_probe(session)
        self.assertTrue(alive)
        self.assertEqual(ref, "pid:8")

    def test_opencode_child_probed_dead_between_turns(self):
        session = FakeSession("opencode", live_proc=None)
        alive, ref = watchdog.process_probe(session)
        self.assertFalse(alive)
        self.assertEqual(ref, "dead:no_live_child")


class IndependentHungDescendantEvidenceTest(unittest.TestCase):
    def test_no_pid_is_no_evidence(self):
        self.assertIsNone(watchdog.independent_hung_descendant_evidence(None))

    def test_orphaned_reparented_process_is_evidence(self):
        def runner(argv):
            return "4242    1    S\n"
        evidence = watchdog.independent_hung_descendant_evidence(
            4242, ps_runner=runner)
        self.assertIsNotNone(evidence)
        self.assertIn("ppid=1", evidence)

    def test_zombie_process_is_evidence(self):
        def runner(argv):
            return "4242 55  Z\n"
        evidence = watchdog.independent_hung_descendant_evidence(
            4242, ps_runner=runner)
        self.assertIsNotNone(evidence)
        self.assertIn("stat=Z", evidence)

    def test_ordinary_alive_child_of_a_real_parent_is_no_evidence(self):
        def runner(argv):
            return "4242 100  S\n"
        self.assertIsNone(watchdog.independent_hung_descendant_evidence(
            4242, ps_runner=runner))

    def test_empty_ps_output_is_no_evidence(self):
        self.assertIsNone(watchdog.independent_hung_descendant_evidence(
            4242, ps_runner=lambda argv: ""))

    def test_ps_runner_raising_is_no_evidence_never_an_exception(self):
        def runner(argv):
            raise OSError("no such tool")
        self.assertIsNone(watchdog.independent_hung_descendant_evidence(
            4242, ps_runner=runner))

    def test_mismatched_pid_in_output_is_no_evidence(self):
        def runner(argv):
            return "9999 1 S\n"
        self.assertIsNone(watchdog.independent_hung_descendant_evidence(
            4242, ps_runner=runner))


class ReviewDueTest(unittest.TestCase):
    def test_none_schedule_is_unknown(self):
        self.assertIsNone(watchdog.review_due(None, NOW))

    def test_overdue_is_true(self):
        self.assertTrue(watchdog.review_due(_schedule("2026-01-01T00:00:00Z"), NOW))

    def test_not_yet_due_is_false(self):
        self.assertFalse(watchdog.review_due(_schedule("2026-01-01T23:00:00Z"), NOW))


class DecideDualEvidenceTest(unittest.TestCase):
    """The core dual-evidence law: BOTH legs are always consulted, and a
    live probe always wins over durable silence."""

    def test_alive_child_is_always_no_action_even_with_terminal_class(self):
        session = FakeSession("claude", proc=FakeProc(alive=True))
        record = _activity_record("process_crash")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule(), session=session)
        self.assertEqual(decision["verdict"], "no_action")
        self.assertIsNotNone(decision["durable_evidence_ref"])
        self.assertIsNotNone(decision["process_probe_ref"])

    def test_quiet_alive_long_turn_is_never_hung(self):
        # Large age_seconds + a genuinely alive child: never terminal.
        session = FakeSession("claude", proc=FakeProc(alive=True))
        record = _activity_record("provider_wait", age_seconds=99999.0)
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule("2020-01-01T00:00:00Z"),
            session=session)
        self.assertEqual(decision["verdict"], "no_action")

    def test_dead_child_non_terminal_class_is_no_action(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("productive_model_work")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule("2020-01-01T00:00:00Z"),
            session=session)
        self.assertEqual(decision["verdict"], "no_action")

    def test_dead_child_hung_descendant_without_ps_evidence_is_soft(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("hung_descendant")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule(), session=session,
            hung_ps_evidence=None)
        self.assertEqual(decision["verdict"], "soft_warning")

    def test_dead_child_hung_descendant_with_ps_evidence_is_hard_stall(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("hung_descendant")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule(), session=session,
            hung_ps_evidence="ps:pid=1,ppid=1,stat=Z")
        self.assertEqual(decision["verdict"], "hard_stall_eligible")
        self.assertIn("ps:pid=1", decision["process_probe_ref"])

    def test_dead_child_crash_overdue_review_is_hard_stall(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("process_crash")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule("2020-01-01T00:00:00Z"),
            session=session)
        self.assertEqual(decision["verdict"], "hard_stall_eligible")

    def test_dead_child_crash_not_yet_due_is_soft_warning(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("process_crash")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule("2099-01-01T00:00:00Z"),
            session=session)
        self.assertEqual(decision["verdict"], "soft_warning")

    def test_no_session_object_is_treated_as_dead_not_unknown(self):
        record = _activity_record("no_evidence_silence")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule("2020-01-01T00:00:00Z"),
            session=None)
        self.assertEqual(decision["verdict"], "hard_stall_eligible")
        self.assertEqual(decision["process_probe_ref"], "dead:no_live_child")

    def test_reconciliation_supersedes_raw_classification(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("hung_descendant")
        reconciliation = _reconciliation("hung_descendant", "productive_model_work")
        decision = watchdog.decide(
            WORK_ID, NOW, record, reconciliation, _schedule(), session=session)
        self.assertEqual(decision["verdict"], "no_action")

    def test_every_terminal_verdict_carries_both_evidence_refs(self):
        # Structural proof that `decide` never constructs a one-leg decision
        # -- `validate_watchdog_decision` itself would reject any attempt.
        session = FakeSession("claude", proc=None)
        for cls in sorted(watchdog.TERMINAL_ACTIVITY_CLASSES):
            record = _activity_record(cls)
            decision = watchdog.decide(
                WORK_ID, NOW, record, None, _schedule("2020-01-01T00:00:00Z"),
                session=session, hung_ps_evidence="ps:pid=1,ppid=1,stat=Z")
            if decision["verdict"] != "no_action":
                self.assertIsNotNone(decision["durable_evidence_ref"])
                self.assertIsNotNone(decision["process_probe_ref"])
            # Revalidate independently: a one-leg decision would raise here.
            activity.validate_watchdog_decision(decision)


class DecideNegativeControlsTest(unittest.TestCase):
    """Elapsed-only / either-single-leg inputs are structurally impossible
    to construct through this module's public API -- these tests prove
    that a hand-assembled one-leg WatchdogDecision (bypassing `decide`
    entirely) is rejected by the same validator `decide` itself always
    routes through, so nothing downstream could ever accept one."""

    def test_hand_built_decision_missing_process_probe_ref_is_rejected(self):
        with self.assertRaises(ValueError):
            activity.validate_watchdog_decision({
                "schema_version": 1, "record": "WatchdogDecision",
                "work_id": WORK_ID, "time": NOW,
                "verdict": "hard_stall_eligible",
                "durable_evidence_ref": "activity:%s@t" % WORK_ID,
                "process_probe_ref": None,
            })

    def test_hand_built_decision_missing_durable_evidence_ref_is_rejected(self):
        with self.assertRaises(ValueError):
            activity.validate_watchdog_decision({
                "schema_version": 1, "record": "WatchdogDecision",
                "work_id": WORK_ID, "time": NOW,
                "verdict": "soft_warning",
                "durable_evidence_ref": None,
                "process_probe_ref": "dead:no_live_child",
            })

    def test_decide_output_always_round_trips_through_validation(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("no_evidence_silence")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule("2020-01-01T00:00:00Z"),
            session=session)
        # decide() already returns a validated/normalized dict; asserting a
        # second, independent pass through the validator is idempotent
        # proves decide() never hands back an unvalidated shape.
        self.assertEqual(decision, activity.validate_watchdog_decision(decision))


class OverdueScheduledReviewExplicitTest(unittest.TestCase):
    """Gate 9's explicit overdue-scheduled-review requirement."""

    def test_overdue_schedule_alone_never_certifies_hard_stall_while_alive(self):
        session = FakeSession("claude", proc=FakeProc(alive=True))
        record = _activity_record("process_crash")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None,
            _schedule("2000-01-01T00:00:00Z"), session=session)
        self.assertEqual(decision["verdict"], "no_action")

    def test_overdue_schedule_with_dead_probe_and_crash_is_hard_stall(self):
        session = FakeSession("claude", proc=None)
        record = _activity_record("process_crash")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None,
            _schedule("2000-01-01T00:00:00Z"), session=session)
        self.assertEqual(decision["verdict"], "hard_stall_eligible")
        self.assertTrue(watchdog.review_due(_schedule("2000-01-01T00:00:00Z"), NOW))


# =========================================================================== #
# M4D-MAJ-01 correction: D-owned integration tests for cowork.py's own        #
# activity-emission seam wiring -- tick lifecycle (normal/exception/         #
# SIGTERM) and headless refusal/no-first-token termination. These exercise   #
# the REAL `cowork._role_loop` (never a reimplementation of its logic), the  #
# only writable-region home available for cowork.py's own new behavior.      #
# =========================================================================== #

class _SessionsRootMixin(object):
    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._tmp = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._tmp
        self._prior_interval = cowork._ACTIVITY_TICK_INTERVAL_SECONDS
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = 0.05
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()

    def tearDown(self):
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = self._prior_interval
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _status_path(self):
        return os.path.join(tempfile.mkdtemp(), "status.json")


class TickLifecycleTest(_SessionsRootMixin, unittest.TestCase):
    """Non-vacuous in-turn tick, bounded try/finally teardown, and the
    shared shutdown event's post-SIGTERM append inhibition -- all through
    the real `cowork._role_loop` wiring."""

    def _gated_session(self, controller="claude"):
        gate = threading.Event()
        released = threading.Event()

        class GatedSession(object):
            def __init__(self):
                self.controller = controller

            def send(self, text, meta=None):
                gate.set()
                released.wait(timeout=5)
                return {"ok": True, "result": "ok"}

            def close(self):
                pass

        return GatedSession(), gate, released

    def test_normal_lifecycle_ticks_before_completion_then_stops(self):
        session_uuid = "aaaa1111-0000-0000-0000-000000000001"
        work_id = "bbbb1111-0000-0000-0000-000000000001"
        session, gate, released = self._gated_session()
        out = io.StringIO()

        result = {}
        thread = threading.Thread(target=lambda: result.update(
            r=cowork._role_loop(
                session, "seed", self._status_path(), context="",
                io_in=io.StringIO(""), io_out=out, headless=True,
                session_uuid=session_uuid, role_work_id=work_id,
                role="scout")))
        thread.start()
        self.assertTrue(gate.wait(timeout=5), "send() never started")

        # A tick must append BEFORE the turn completes -- non-vacuous.
        deadline_count = 0
        for _ in range(60):
            history = state_store.read_activity_history(session_uuid, work_id)
            if history:
                deadline_count = len(history)
                break
            threading.Event().wait(0.02)
        self.assertGreater(deadline_count, 0,
                           "no tick appended before the blocked send returned")
        self.assertEqual(out.getvalue(), "",
                         "a tick must perform zero io_out writes")

        released.set()
        thread.join(timeout=5)
        final_count = len(
            state_store.read_activity_history(session_uuid, work_id))
        self.assertGreater(final_count, deadline_count,
                           "the real turn-boundary append never landed")

        # Bounded teardown: no further ticks land once the turn is over,
        # proving the daemon thread actually stopped rather than merely
        # being abandoned.
        threading.Event().wait(0.3)
        settled_count = len(
            state_store.read_activity_history(session_uuid, work_id))
        self.assertEqual(settled_count, final_count,
                         "a tick fired after the turn already completed -- "
                         "the tick thread was not bounded/stopped")

    def test_exception_lifecycle_still_tears_down_the_tick_thread(self):
        # A KeyboardInterrupt propagating out of session.send() (through
        # `_send`'s own explicit re-raise) must still hit the tick's
        # `finally` -- proving the try/finally teardown is unconditional,
        # not merely reached on the ordinary return path.
        session_uuid = "aaaa2222-0000-0000-0000-000000000002"
        work_id = "bbbb2222-0000-0000-0000-000000000002"
        gate = threading.Event()
        released = threading.Event()

        class RaisingSession(object):
            controller = "claude"

            def send(self, text, meta=None):
                gate.set()
                released.wait(timeout=5)
                raise KeyboardInterrupt()

            def close(self):
                pass

        out = io.StringIO()
        result = {}
        thread = threading.Thread(target=lambda: result.update(
            r=cowork._role_loop(
                RaisingSession(), "seed", self._status_path(), context="",
                io_in=io.StringIO(""), io_out=out, headless=True,
                session_uuid=session_uuid, role_work_id=work_id,
                role="scout")))
        thread.start()
        self.assertTrue(gate.wait(timeout=5))
        for _ in range(10):
            if state_store.read_activity_history(session_uuid, work_id):
                break
            threading.Event().wait(0.02)
        before = len(state_store.read_activity_history(session_uuid, work_id))
        self.assertGreater(before, 0, "no tick fired before the interrupt")

        released.set()
        thread.join(timeout=5)
        self.assertEqual(result["r"][1], "interrupted")

        # The tick thread must be torn down even though the turn raised --
        # no further appends land once the interrupted call has returned.
        threading.Event().wait(0.3)
        after = len(state_store.read_activity_history(session_uuid, work_id))
        self.assertEqual(after, before,
                         "the tick thread kept running past a raised "
                         "KeyboardInterrupt -- try/finally teardown failed")

    def test_sigterm_inhibits_further_ticks_and_the_turn_boundary_append(self):
        session_uuid = "aaaa3333-0000-0000-0000-000000000003"
        work_id = "bbbb3333-0000-0000-0000-000000000003"
        session, gate, released = self._gated_session()
        out = io.StringIO()

        result = {}
        thread = threading.Thread(target=lambda: result.update(
            r=cowork._role_loop(
                session, "seed", self._status_path(), context="",
                io_in=io.StringIO(""), io_out=out, headless=True,
                session_uuid=session_uuid, role_work_id=work_id,
                role="scout")))
        thread.start()
        self.assertTrue(gate.wait(timeout=5))
        for _ in range(10):
            if state_store.read_activity_history(session_uuid, work_id):
                break
            threading.Event().wait(0.02)
        before = len(state_store.read_activity_history(session_uuid, work_id))
        self.assertGreater(before, 0)

        # Simulate a real SIGTERM: `_handle_external_kill` sets this event
        # FIRST, before its own durable `aborted` write.
        cowork._ACTIVITY_SHUTDOWN_EVENT.set()
        threading.Event().wait(0.3)
        during_shutdown = len(
            state_store.read_activity_history(session_uuid, work_id))
        self.assertEqual(during_shutdown, before,
                         "a tick appended AFTER the shared shutdown event "
                         "was set -- post-SIGTERM append race not closed")

        released.set()
        thread.join(timeout=5)
        # The turn-boundary append itself is ALSO inhibited while the
        # shutdown event remains set -- checked immediately before the
        # durable append, not only inside the tick loop.
        after_join_still_set = len(
            state_store.read_activity_history(session_uuid, work_id))
        self.assertEqual(after_join_still_set, before,
                         "the turn-boundary append landed after the shared "
                         "shutdown event was set")

        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()


class HeadlessTerminationTest(_SessionsRootMixin, unittest.TestCase):
    """Headless refusal/no-first-token, no-fallback termination: rc 17,
    naming the provider reason; interactive mode never terminates the
    process on identical evidence."""

    def test_no_first_token_headless_returns_exit_code_and_reason(self):
        session_uuid = "cccc1111-0000-0000-0000-000000000001"
        work_id = "dddd1111-0000-0000-0000-000000000001"

        class NoFirstTokenSession(object):
            controller = "claude"

            def send(self, text, meta=None):
                return {"ok": False, "result": "no_first_token",
                        "error_type": "no_first_token"}

            def close(self):
                pass

        out = io.StringIO()
        rc, outcome, payload = cowork._role_loop(
            NoFirstTokenSession(), "seed", self._status_path(), context="",
            io_in=io.StringIO(""), io_out=out, headless=True,
            session_uuid=session_uuid, role_work_id=work_id, role="scout")
        self.assertEqual(outcome, cowork._OUTCOME_HEADLESS_TERMINATED)
        self.assertEqual(payload["exit_code"], cowork.HEADLESS_REFUSAL_EXIT_CODE)
        self.assertEqual(payload["exit_code"], 17)
        self.assertEqual(payload["controller"], "claude")
        self.assertIn("no_first_token", payload["reason"])
        self.assertIn("no_first_token", out.getvalue())
        self.assertIn("claude", out.getvalue())
        self.assertIn("no fallback available", out.getvalue())

    def test_refused_headless_names_the_provider_reason(self):
        session_uuid = "cccc2222-0000-0000-0000-000000000002"
        work_id = "dddd2222-0000-0000-0000-000000000002"

        class RefusedSession(object):
            controller = "opencode"

            def send(self, text, meta=None):
                return {
                    "ok": False, "result": "error", "error_type": "denied",
                    "controller_turn_outcome": {
                        "schema_version": 1, "record": "ControllerTurnOutcome",
                        "outcome": "refused", "failure_class": "auth",
                    },
                }

            def close(self):
                pass

        out = io.StringIO()
        rc, outcome, payload = cowork._role_loop(
            RefusedSession(), "seed", self._status_path(), context="",
            io_in=io.StringIO(""), io_out=out, headless=True,
            session_uuid=session_uuid, role_work_id=work_id, role="scout")
        self.assertEqual(outcome, cowork._OUTCOME_HEADLESS_TERMINATED)
        self.assertEqual(payload["exit_code"], 17)
        self.assertEqual(payload["reason"], "auth")
        self.assertIn("auth", out.getvalue())
        self.assertIn("opencode", out.getvalue())

    def test_interactive_mode_never_terminates_on_identical_evidence(self):
        session_uuid = "cccc3333-0000-0000-0000-000000000003"
        work_id = "dddd3333-0000-0000-0000-000000000003"

        class NoFirstTokenSession(object):
            controller = "claude"

            def send(self, text, meta=None):
                return {"ok": False, "result": "no_first_token",
                        "error_type": "no_first_token"}

            def close(self):
                pass

        out = io.StringIO()
        rc, outcome, payload = cowork._role_loop(
            NoFirstTokenSession(), "seed", self._status_path(), context="",
            io_in=io.StringIO("end\n"), io_out=out, headless=False,
            session_uuid=session_uuid, role_work_id=work_id, role="scout")
        self.assertNotEqual(outcome, cowork._OUTCOME_HEADLESS_TERMINATED)
        self.assertIsNone(payload)


class ScheduleRefreshPolicyTest(_SessionsRootMixin, unittest.TestCase):
    """M4D-MAJ-02: the prior schedule is evaluated BEFORE any refresh
    decision -- a terminal turn never pushes `next_inspection_at` further
    into the future, and the very first terminal turn for a work_id is due
    immediately rather than deferred a full interval."""

    def test_non_terminal_class_extends_the_schedule_into_the_future(self):
        session_uuid = "eeee1111-0000-0000-0000-000000000001"
        work_id = "ffff1111-0000-0000-0000-000000000001"
        record = cowork._ensure_scheduled_review(
            session_uuid, work_id, "2026-01-01T00:00:00Z",
            activity_class="productive_model_work")
        self.assertEqual(record["next_inspection_at"], "2026-01-01T00:05:00Z")

    def test_first_ever_terminal_turn_is_due_immediately(self):
        session_uuid = "eeee2222-0000-0000-0000-000000000002"
        work_id = "ffff2222-0000-0000-0000-000000000002"
        record = cowork._ensure_scheduled_review(
            session_uuid, work_id, "2026-01-01T00:00:00Z",
            activity_class="process_crash")
        self.assertEqual(record["next_inspection_at"], "2026-01-01T00:00:00Z")
        self.assertTrue(watchdog.review_due(record, "2026-01-01T00:00:00Z"))

    def test_terminal_class_never_extends_an_existing_schedule(self):
        session_uuid = "eeee3333-0000-0000-0000-000000000003"
        work_id = "ffff3333-0000-0000-0000-000000000003"
        # A prior, still-future schedule (minted by an earlier productive
        # turn) must NOT be pushed further out just because the CURRENT
        # turn happens to be terminal.
        prior = cowork._ensure_scheduled_review(
            session_uuid, work_id, "2026-01-01T00:00:00Z",
            activity_class="productive_model_work")
        self.assertEqual(prior["next_inspection_at"], "2026-01-01T00:05:00Z")
        after_failure = cowork._ensure_scheduled_review(
            session_uuid, work_id, "2026-01-01T00:01:00Z",
            activity_class="hung_descendant")
        self.assertEqual(after_failure["next_inspection_at"],
                         "2026-01-01T00:05:00Z")

    def test_terminal_class_never_extends_an_already_overdue_schedule(self):
        session_uuid = "eeee4444-0000-0000-0000-000000000004"
        work_id = "ffff4444-0000-0000-0000-000000000004"
        first = cowork._ensure_scheduled_review(
            session_uuid, work_id, "2026-01-01T00:00:00Z",
            activity_class="process_crash")
        self.assertEqual(first["next_inspection_at"], "2026-01-01T00:00:00Z")
        # A SECOND terminal turn, later, must still leave the (already
        # overdue) schedule exactly as it was -- never refreshed forward.
        second = cowork._ensure_scheduled_review(
            session_uuid, work_id, "2026-01-01T00:10:00Z",
            activity_class="no_evidence_silence")
        self.assertEqual(second["next_inspection_at"], "2026-01-01T00:00:00Z")


class HardStallReachableEndToEndTest(_SessionsRootMixin, unittest.TestCase):
    """M4D-MAJ-02: `hard_stall_eligible` is genuinely reachable through the
    real `cowork._role_loop` wiring -- not only through `cowork_watchdog.
    decide()` in isolation."""

    class _RecordingTrace(object):
        def __init__(self):
            self.events = []

        def event(self, name, **kw):
            self.events.append((name, kw))
            return None

    def test_first_ever_failure_reaches_hard_stall_eligible(self):
        session_uuid = "11119999-0000-0000-0000-000000000001"
        work_id = "22229999-0000-0000-0000-000000000001"

        class GenericFailSession(object):
            controller = "claude"

            def send(self, text, meta=None):
                return {"ok": False, "result": "error",
                        "error_type": "some_error"}

            def close(self):
                pass

        trace = self._RecordingTrace()
        out = io.StringIO()
        cowork._role_loop(
            GenericFailSession(), "seed", self._status_path(), context="",
            io_in=io.StringIO(""), io_out=out, headless=True,
            session_uuid=session_uuid, role_work_id=work_id, role="scout",
            trace=trace)
        decisions = [kw["verdict"] for name, kw in trace.events
                    if name == "watchdog.decision"]
        self.assertIn("hard_stall_eligible", decisions)


class HungDescendantPsWiringIntegrationTest(_SessionsRootMixin,
                                            unittest.TestCase):
    """M4D-MAJ-02: `cowork._watchdog_decision_for_presentation` genuinely
    attempts a real independent `ps` check whenever a live pid IS available
    for a `hung_descendant` classification -- never fabricated, and never
    attempted for any other classification."""

    def test_ps_check_is_attempted_when_a_live_pid_is_available(self):
        session_uuid = "33339999-0000-0000-0000-000000000001"
        work_id = "44449999-0000-0000-0000-000000000001"

        class FakeProc(object):
            pid = 987654

            def poll(self):
                return None  # reports alive per live_child_handle's own rule

        class HungButLiveSession(object):
            controller = "claude"

            def __init__(self):
                self.proc = FakeProc()

            def send(self, text, meta=None):
                return {"ok": False, "result": "no_first_token",
                        "error_type": "no_first_token"}

            def close(self):
                pass

        with mock.patch.object(
                watchdog, "independent_hung_descendant_evidence",
                return_value=None) as spy:
            cowork._role_loop(
                HungButLiveSession(), "seed", self._status_path(), context="",
                io_in=io.StringIO(""), io_out=io.StringIO(), headless=True,
                session_uuid=session_uuid, role_work_id=work_id, role="scout")
        spy.assert_called_with(987654)

    def test_ps_check_never_attempted_for_a_non_hung_classification(self):
        session_uuid = "33339999-0000-0000-0000-000000000002"
        work_id = "44449999-0000-0000-0000-000000000002"

        class FakeProc(object):
            pid = 111222

            def poll(self):
                return None

        class GenericFailButLiveSession(object):
            controller = "claude"

            def __init__(self):
                self.proc = FakeProc()

            def send(self, text, meta=None):
                return {"ok": False, "result": "error",
                        "error_type": "some_error"}

            def close(self):
                pass

        with mock.patch.object(
                watchdog, "independent_hung_descendant_evidence",
                return_value=None) as spy:
            cowork._role_loop(
                GenericFailButLiveSession(), "seed", self._status_path(),
                context="", io_in=io.StringIO(""), io_out=io.StringIO(),
                headless=True, session_uuid=session_uuid, role_work_id=work_id,
                role="scout")
        spy.assert_not_called()

    def test_ps_check_not_attempted_when_no_live_pid_exists(self):
        session_uuid = "33339999-0000-0000-0000-000000000003"
        work_id = "44449999-0000-0000-0000-000000000003"

        class NoFirstTokenSession(object):
            controller = "codex"

            def send(self, text, meta=None):
                return {"ok": False, "result": "no_first_token",
                        "error_type": "no_first_token"}

            def close(self):
                pass

        with mock.patch.object(
                watchdog, "independent_hung_descendant_evidence",
                return_value=None) as spy:
            cowork._role_loop(
                NoFirstTokenSession(), "seed", self._status_path(), context="",
                io_in=io.StringIO(""), io_out=io.StringIO(), headless=True,
                session_uuid=session_uuid, role_work_id=work_id, role="scout")
        spy.assert_not_called()

    def test_hard_stall_eligible_reachable_via_decide_given_dead_probe_and_ps(
            self):
        # The decision-logic proof that hung_descendant DOES reach
        # hard_stall_eligible once BOTH legs (a dead probe and independent
        # ps evidence) are genuinely present -- cowork.py's wiring above
        # supplies exactly this shape whenever both legs are truthfully
        # available (see the module docstring of `_hung_descendant_ps_
        # evidence` for the structural reason a single continuous process
        # can rarely observe a dead-but-orphaned same-session child).
        session = FakeSession("claude", proc=None)
        record = _activity_record("hung_descendant")
        decision = watchdog.decide(
            WORK_ID, NOW, record, None, _schedule(), session=session,
            hung_ps_evidence="ps:pid=1,ppid=1,stat=Z")
        self.assertEqual(decision["verdict"], "hard_stall_eligible")


if __name__ == "__main__":
    unittest.main()
