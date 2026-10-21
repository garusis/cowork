"""Neutral negative controls for immutable dispatch-allocation bindings."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import cowork_allocation as allocation
import cowork_state as state
import cowork
import cowork_handoff as handoff
import test_dispatch_allocation as allocation_tests
import test_exact_role_recovery as exact_recovery


class AllocationFailClosedTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.environment = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.scratch.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.session = "synthetic-session"
        self.package = "synthetic-work"
        self.limits = allocation.parse_spec("build=2,correction=1")
        state.admit_dispatch_allocation(self.session, self.package, self.limits)

    def replace_json(self, path, value):
        Path(path).write_text(json.dumps(value), encoding="utf-8")

    def test_corrupt_existing_binding_never_becomes_unallocated(self):
        path = state.dispatch_allocation_binding_path_for(self.session)
        Path(path).write_text("{truncated", encoding="utf-8")
        with self.assertRaises(state.AllocationConflict):
            state.consume_dispatch_allocation(self.session, "building")

    def test_binding_without_admitted_digest_is_not_trusted(self):
        path = state.dispatch_allocation_binding_path_for(self.session)
        binding = json.loads(Path(path).read_text())
        binding.pop("limits_digest")
        self.replace_json(path, binding)
        with self.assertRaises(state.AllocationConflict):
            state.consume_dispatch_allocation(self.session, "building")

    def test_missing_consumed_counter_never_refunds_spent_turns(self):
        state.consume_dispatch_allocation(self.session, "building")
        state.consume_dispatch_allocation(self.session, "building")
        path = state.allocation_ledger_path_for(self.package)
        ledger = json.loads(Path(path).read_text())
        ledger["consumed"].pop("build")
        self.replace_json(path, ledger)
        with self.assertRaises(state.AllocationConflict):
            state.consume_dispatch_allocation(self.session, "building")

    def test_null_consumed_counter_never_refunds_spent_turns(self):
        path = state.allocation_ledger_path_for(self.package)
        ledger = json.loads(Path(path).read_text())
        ledger["consumed"]["build"] = None
        self.replace_json(path, ledger)
        with self.assertRaises(state.AllocationConflict):
            state.consume_dispatch_allocation(self.session, "building")

    def test_existing_session_cannot_rebind_to_fresh_allocation(self):
        state.consume_dispatch_allocation(self.session, "building")
        state.consume_dispatch_allocation(self.session, "building")
        with self.assertRaises(state.AllocationConflict):
            state.admit_dispatch_allocation(
                self.session, "different-synthetic-work", self.limits)
        with self.assertRaises(state.AllocationExhausted):
            state.consume_dispatch_allocation(self.session, "building")

    def test_existing_incomplete_ledger_is_not_fresh_admission(self):
        path = state.allocation_ledger_path_for(self.package)
        self.replace_json(path, {})
        with self.assertRaises(state.AllocationConflict):
            state.admit_dispatch_allocation(
                "replacement-synthetic-session", self.package, self.limits)

    def test_deleting_an_admitted_binding_never_restores_legacy_mode(self):
        Path(state.dispatch_allocation_binding_path_for(self.session)).unlink()
        with self.assertRaises(state.AllocationConflict):
            state.consume_dispatch_allocation(self.session, "building")

    def test_readmission_cannot_recreate_a_deleted_spent_ledger(self):
        state.consume_dispatch_allocation(self.session, "building")
        state.consume_dispatch_allocation(self.session, "building")
        path = Path(state.allocation_ledger_path_for(self.package))
        path.unlink()
        with self.assertRaises(state.AllocationConflict):
            state.admit_dispatch_allocation(
                self.session, self.package, self.limits)
        self.assertFalse(path.exists())

    def test_replacement_cannot_recreate_a_deleted_spent_package(self):
        state.consume_dispatch_allocation(self.session, "building")
        state.consume_dispatch_allocation(self.session, "building")
        path = Path(state.allocation_ledger_path_for(self.package))
        path.unlink()
        with self.assertRaises(state.AllocationConflict):
            state.admit_dispatch_allocation(
                "replacement-synthetic-session", self.package, self.limits)
        self.assertFalse(path.exists())

    def test_concurrent_admission_cannot_leave_refused_limits_in_marker(self):
        self._assert_concurrent_admission_marker()

    def test_crashed_contender_cannot_poison_successful_package(self):
        self._assert_concurrent_admission_marker(crash_contender=True)

    def _assert_concurrent_admission_marker(self, crash_contender=False):
        package = "synthetic-concurrent-admission"
        marker_path = state.package_admission_marker_path_for(package)
        limits = {
            "allocation-left": allocation.parse_spec("build=1"),
            "allocation-right": allocation.parse_spec("build=2")}
        read_done = {name: threading.Event() for name in limits}
        left_marker = threading.Event()
        right_marker = threading.Event()
        left_finished = threading.Event()
        real_read = state.read_package_admission_marker
        real_write = state.write_json_atomic_durable
        successes, errors = [], []

        class SimulatedCrash(BaseException):
            pass

        def synchronized_read(ref):
            value = real_read(ref)
            name = threading.current_thread().name
            if ref == package and name in limits and value is None:
                read_done[name].set()
                other = ("allocation-right" if name == "allocation-left"
                         else "allocation-left")
                read_done[other].wait(2)
            return value

        def ordered_write(path, record):
            name = threading.current_thread().name
            if str(path) != str(marker_path) or name not in limits:
                return real_write(path, record)
            if name == "allocation-left":
                result = real_write(path, record)
                left_marker.set()
                if not crash_contender:
                    right_marker.wait(2)
                return result
            left_marker.wait(2)
            winner_already_succeeded = False
            if crash_contender:
                winner_already_succeeded = left_finished.wait(2)
            result = real_write(path, record)
            right_marker.set()
            if crash_contender and winner_already_succeeded:
                raise SimulatedCrash("process died after durable marker write")
            if not crash_contender:
                left_finished.wait(2)
            return result

        def admit(name):
            try:
                state.admit_dispatch_allocation(name, package, limits[name])
                if crash_contender:
                    state.consume_dispatch_allocation(name, "building")
                successes.append(name)
            except SimulatedCrash:
                errors.append("simulated_crash")
            except state.AllocationConflict as exc:
                errors.append(exc.reason)
            finally:
                if name == "allocation-left":
                    left_finished.set()

        with mock.patch.object(state, "read_package_admission_marker",
                               side_effect=synchronized_read), \
             mock.patch.object(state, "write_json_atomic_durable",
                               side_effect=ordered_write):
            workers = [threading.Thread(target=admit, args=(name,), name=name,
                                        daemon=True) for name in limits]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(12)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(len(successes), 1)
        self.assertIn(errors, (["limits_immutable"], ["simulated_crash"])
                      if crash_contender else (["limits_immutable"],))
        ledger = state.read_dispatch_allocation_ledger(package)
        marker = state.read_package_admission_marker(package)
        self.assertEqual(marker["limits_digest"], ledger["limits_digest"])
        self.assertEqual(ledger["limits"], limits[successes[0]])

    def test_counter_values_are_validated_without_numeric_coercion(self):
        path = state.allocation_ledger_path_for(self.package)
        original = json.loads(Path(path).read_text())
        for invalid in ("0", False, 1.5, -1):
            with self.subTest(invalid=invalid):
                ledger = dict(original, consumed=dict(original["consumed"]))
                ledger["consumed"]["build"] = invalid
                self.replace_json(path, ledger)
                with self.assertRaises(state.AllocationConflict):
                    state.consume_dispatch_allocation(self.session, "building")

    def test_ledger_package_identity_must_match_its_binding(self):
        path = state.allocation_ledger_path_for(self.package)
        ledger = json.loads(Path(path).read_text())
        ledger["package_ref"] = "unrelated-synthetic-work"
        self.replace_json(path, ledger)
        with self.assertRaises(state.AllocationConflict):
            state.consume_dispatch_allocation(self.session, "building")


class RecoveryProbeFailClosedTest(allocation_tests.NoProviderAfterRefusalTests):
    def test_malformed_reviewer_recovery_cannot_probe_a_spent_lead(self):
        session_path = self._saved_planning_session()
        saved = state.load(session_path)
        saved["config"]["planner"]["controller"] = "codex"
        saved["sessions"]["planner"] = {
            "controller": "codex", "id": "synthetic-provider"}
        saved["pending_switches"] = {
            cowork.PLANNING_ADVISOR: {
                "failed_turn": {"schema": 1},
                "pending_turn": "synthetic reviewer retry"}}
        state.save(session_path, saved)
        self._exhaust()
        with mock.patch.object(cowork, "_claude_probe",
                               return_value=(True, None, None)) as probe:
            self._run(["--session-file", session_path,
                       "--switch-controller", "planner=claude"])
        self.assertEqual(probe.call_count, 0)


class SpentReviewerRepairTest(exact_recovery._FlowCase):
    def test_spent_lead_still_allows_its_failed_reviewer_to_switch(self):
        case = exact_recovery.CASES[0]
        self.make_session(case)
        state.admit_dispatch_allocation(
            self.UUID, "synthetic-review-repair",
            allocation.parse_spec("planning=1"))
        self.fail_once(case)
        self.assertIsNotNone(self.record_of(case))
        self.assertEqual(state.dispatch_allocation_state(
            self.UUID)["consumed"]["planning"], 1)
        before = len(self.sessions[case["lead"]])
        self.log.clear()
        self.verdicts[case["reviewer"]] = [
            {"verdict": "approve", "findings": []}]
        rc, output, _box = self.run_flow([
            "--switch-controller", case["reviewer"] + "=codex"])
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.log, [(case["reviewer"], "review")])
        self.assertEqual(self.lead_sends(case["lead"], before), 0)
        self.assertEqual(state.dispatch_allocation_state(
            self.UUID)["consumed"]["planning"], 1)


class GatewaySingleAttemptTest(unittest.TestCase):
    def test_transport_body_errors_cannot_trigger_an_implicit_second_send(self):
        for error in (TypeError, ValueError):
            with self.subTest(error=error.__name__):
                class SendRaisesAfterAttempt:
                    def __init__(self):
                        self.calls = 0

                    def send(self, text, meta=None):
                        self.calls += 1
                        raise error("failure after transport entry")

                session = SendRaisesAfterAttempt()
                delivery = handoff.direct_delivery(
                    handoff._static_role_text("neutral bounded work"))
                outcome = cowork._send(session, delivery, meta={"fresh": True})
                self.assertFalse(outcome["ok"])
                self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
