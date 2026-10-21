#!/usr/bin/env python3
"""Tests for the package dispatch allocation (#116): a durable, immutable,
per-package ceiling on REAL PROVIDER LEAD DISPATCHES, checked and charged
before every lead turn.

Three layers:
  * the pure grammar and counter arithmetic (`cowork_allocation`);
  * the durable, package-scoped ledger and its atomic consumption
    (`cowork_state`);
  * the `_role_loop` gate itself, driven with fake lead sessions and fake
    reviewer runners over the real reopen paths (reviewer revise, owned
    verification red, a persisted resume), asserting on the exact number of
    sends that reached a session.

Every input is neutral and synthetic: fabricated package refs, fresh UUIDs,
scripted verdicts. COWORK_SESSIONS_ROOT is pinned to a temp directory. No live
provider is used and no provider binary is launched.

Run: python3 scripts/cowork_offline_tests.py test_dispatch_allocation
"""

import contextlib
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_allocation as allocation  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

SCOUTING, PLANNING, BUILDING = "scouting", "planning", "building"
PHASE_ROLES = {SCOUTING: ("scout", "scout-reviewer"),
               PLANNING: ("planner", "planning-advisor"),
               BUILDING: ("builder", "build-reviewer")}

APPROVE = {"verdict": "approve"}
REVISE = {"verdict": "revise", "findings": ["keep going"]}


# --------------------------------------------------------------------------- #
# Layer 1: the pure grammar and counter arithmetic.                           #
# --------------------------------------------------------------------------- #

class GrammarTests(unittest.TestCase):

    def test_build2_correction1_is_initial_plus_one_correction(self):
        # The exact semantics #116 turned on: "build2/correction1" is TWO
        # builder dispatches in total, not 2 extra and not 3.
        limits = allocation.parse_spec("build=2,correction=1")
        self.assertEqual(limits["build"],
                         {"dispatches": 2, "corrections": 1})
        first = allocation.evaluate(limits, "build", 0)
        self.assertTrue(first["allowed"])
        self.assertEqual((first["ordinal"], first["kind"]), (1, "initial"))
        second = allocation.evaluate(limits, "build", 1)
        self.assertTrue(second["allowed"])
        self.assertEqual((second["ordinal"], second["kind"]), (2, "correction"))
        third = allocation.evaluate(limits, "build", 2)
        self.assertFalse(third["allowed"])
        self.assertEqual(third["refusal_code"], "dispatch_limit_exhausted")
        self.assertEqual(third["ordinal"], 3)
        self.assertEqual(third["consumed_dispatches"], 2)
        self.assertEqual(third["consumed_corrections"], 1)

    def test_zero_forbids_the_phase_entirely(self):
        for phase, spec in (("build", "build=0"),
                            ("discovery", "discovery=0"),
                            ("planning", "planning=0")):
            with self.subTest(phase=phase):
                limits = allocation.parse_spec(spec)
                verdict = allocation.evaluate(limits, phase, 0)
                self.assertFalse(verdict["allowed"])
                self.assertEqual(verdict["refusal_code"],
                                 "dispatch_limit_exhausted")
                self.assertEqual(verdict["ordinal"], 1)

    def test_zero_corrections_admits_only_the_initial_dispatch(self):
        limits = allocation.parse_spec("build=5,correction=0")
        self.assertTrue(allocation.evaluate(limits, "build", 0)["allowed"])
        second = allocation.evaluate(limits, "build", 1)
        self.assertFalse(second["allowed"])
        self.assertEqual(second["refusal_code"], "correction_limit_exhausted")

    def test_a_phase_nobody_allocated_is_unbounded(self):
        limits = allocation.parse_spec("build=1")
        for consumed in (0, 1, 50):
            for phase in ("discovery", "planning"):
                self.assertTrue(
                    allocation.evaluate(limits, phase, consumed)["allowed"])

    def test_discovery_and_planning_carry_their_own_bounds(self):
        limits = allocation.parse_spec("discovery=2,planning=2")
        for phase in ("discovery", "planning"):
            with self.subTest(phase=phase):
                self.assertTrue(
                    allocation.evaluate(limits, phase, 1)["allowed"])
                self.assertFalse(
                    allocation.evaluate(limits, phase, 2)["allowed"])

    def test_loop_phase_mapping_covers_only_the_lead_phases(self):
        self.assertEqual(allocation.phase_for_loop_phase(SCOUTING), "discovery")
        self.assertEqual(allocation.phase_for_loop_phase(PLANNING), "planning")
        self.assertEqual(allocation.phase_for_loop_phase(BUILDING), "build")
        for other in ("worktree", "reviewing", "", None):
            self.assertIsNone(allocation.phase_for_loop_phase(other))

    def test_malformed_specs_are_refused_never_defaulted(self):
        for spec in ("", "   ", "build", "build=", "=2", "build=-1",
                     "build=two", "build=2,", "nonsense=1", "build=2=3",
                     "build=1,build=2", "build_corrections=1,correction=2",
                     "correction=1,corrections=2", "build=99999999"):
            with self.subTest(spec=spec):
                with self.assertRaises(allocation.AllocationError):
                    allocation.parse_spec(spec)

    def test_edited_limits_are_refused_on_readback(self):
        for limits in ({"build": {"dispatches": -1}},
                       {"build": {"dispatches": "2"}},
                       {"build": {"dispatches": True}},
                       {"build": {"unknown": 1}},
                       {"nowhere": {"dispatches": 1}},
                       {"build": {"dispatches": None}},
                       "build=2", None, []):
            with self.subTest(limits=limits):
                with self.assertRaises(allocation.AllocationError):
                    allocation.validate_limits(limits)

    def test_whitespace_and_case_are_tolerated_but_values_are_not_guessed(self):
        limits = allocation.parse_spec(" BUILD = 2 , Correction = 1 ")
        self.assertEqual(limits["build"],
                         {"dispatches": 2, "corrections": 1})

    def test_evaluate_refuses_a_negative_consumed_count(self):
        with self.assertRaises(allocation.AllocationError):
            allocation.evaluate(allocation.parse_spec("build=2"), "build", -1)


# --------------------------------------------------------------------------- #
# Layer 2: the durable, package-scoped ledger.                                #
# --------------------------------------------------------------------------- #

class LedgerEnv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sessions_root = os.path.join(self.tmp, "sessions")
        patcher = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.sessions_root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.package = "pkg-" + uuid.uuid4().hex[:8]

    def new_session(self):
        sid = str(uuid.uuid4())
        os.makedirs(state_store.session_assets_dir(sid), exist_ok=True)
        return sid

    def admit(self, sid, spec, package=None):
        return state_store.admit_dispatch_allocation(
            sid, package or self.package, allocation.parse_spec(spec))

    def ledger(self, package=None):
        return state_store.read_dispatch_allocation_ledger(
            package or self.package)


class LedgerTests(LedgerEnv):

    def test_consumption_walks_the_allocation_and_then_refuses(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        kinds = []
        for _ in range(2):
            kinds.append(
                state_store.consume_dispatch_allocation(sid, BUILDING)["kind"])
        self.assertEqual(kinds, ["initial", "correction"])
        with self.assertRaises(state_store.AllocationExhausted) as caught:
            state_store.consume_dispatch_allocation(sid, BUILDING)
        verdict = caught.exception.verdict
        self.assertEqual(verdict["refusal_code"], "dispatch_limit_exhausted")
        self.assertEqual(verdict["consumed_dispatches"], 2)
        self.assertEqual(verdict["consumed_corrections"], 1)
        self.assertEqual(verdict["limit_dispatches"], 2)
        self.assertEqual(verdict["limit_corrections"], 1)
        self.assertEqual(verdict["package_ref"], self.package)
        # The refusal changed nothing on disk.
        self.assertEqual(self.ledger()["consumed"]["build"], 2)

    def test_phases_are_counted_independently(self):
        sid = self.new_session()
        self.admit(sid, "discovery=1,planning=1,build=1")
        for loop_phase in (SCOUTING, PLANNING, BUILDING):
            self.assertIsNotNone(
                state_store.consume_dispatch_allocation(sid, loop_phase))
        for loop_phase in (SCOUTING, PLANNING, BUILDING):
            with self.assertRaises(state_store.AllocationExhausted):
                state_store.consume_dispatch_allocation(sid, loop_phase)
        self.assertEqual(self.ledger()["consumed"],
                         {"discovery": 1, "planning": 1, "build": 1})

    def test_an_unallocated_phase_is_never_charged(self):
        sid = self.new_session()
        self.admit(sid, "build=1")
        for _ in range(5):
            self.assertIsNone(
                state_store.consume_dispatch_allocation(sid, SCOUTING))
        self.assertEqual(self.ledger()["consumed"]["discovery"], 0)

    def test_a_session_with_no_binding_is_completely_unaffected(self):
        sid = self.new_session()
        self.assertIsNone(state_store.check_dispatch_allocation(sid, BUILDING))
        self.assertIsNone(
            state_store.consume_dispatch_allocation(sid, BUILDING))
        self.assertIsNone(state_store.read_dispatch_allocation_binding(sid))

    def test_admission_is_immutable_for_a_package_ref(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        state_store.consume_dispatch_allocation(sid, BUILDING)
        for spec in ("build=9", "build=2", "build=2,correction=2",
                     "build=0", "discovery=1"):
            with self.subTest(spec=spec):
                with self.assertRaises(state_store.AllocationConflict) as c:
                    self.admit(sid, spec)
                self.assertEqual(c.exception.reason, "limits_immutable")
        # Neither the limits nor the already-spent unit moved.
        self.assertEqual(self.ledger()["consumed"]["build"], 1)
        self.assertEqual(self.ledger()["limits"]["build"],
                         {"dispatches": 2, "corrections": 1})

    def test_readmitting_the_identical_allocation_never_resets_counters(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        state_store.consume_dispatch_allocation(sid, BUILDING)
        state_store.consume_dispatch_allocation(sid, BUILDING)
        self.admit(sid, "build=2,correction=1")
        self.assertEqual(self.ledger()["consumed"]["build"], 2)
        with self.assertRaises(state_store.AllocationExhausted):
            state_store.consume_dispatch_allocation(sid, BUILDING)

    def test_a_replacement_session_inherits_the_spent_budget(self):
        # The lineage property: a brand new session uuid -- a "restart", a
        # different --session-file, a replacement selector -- naming the SAME
        # package ref cannot buy back a single spent dispatch.
        first = self.new_session()
        self.admit(first, "build=2,correction=1")
        state_store.consume_dispatch_allocation(first, BUILDING)
        state_store.consume_dispatch_allocation(first, BUILDING)
        for _ in range(3):
            replacement = self.new_session()
            self.admit(replacement, "build=2,correction=1")
            with self.assertRaises(state_store.AllocationExhausted) as caught:
                state_store.consume_dispatch_allocation(replacement, BUILDING)
            self.assertEqual(caught.exception.verdict["consumed_dispatches"], 2)
        self.assertEqual(self.ledger()["consumed"]["build"], 2)

    def test_a_different_package_ref_is_a_different_budget(self):
        first = self.new_session()
        self.admit(first, "build=1")
        state_store.consume_dispatch_allocation(first, BUILDING)
        other = self.new_session()
        self.admit(other, "build=1", package=self.package + "-other")
        self.assertIsNotNone(
            state_store.consume_dispatch_allocation(other, BUILDING))

    def test_a_corrupt_ledger_fails_closed(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        with open(state_store.allocation_ledger_path_for(self.package),
                  "w") as fh:
            fh.write("{ this is not json")
        with self.assertRaises(state_store.AllocationConflict) as c:
            state_store.consume_dispatch_allocation(sid, BUILDING)
        self.assertEqual(c.exception.reason, "ledger_corrupt")
        with self.assertRaises(state_store.AllocationConflict):
            state_store.check_dispatch_allocation(sid, BUILDING)

    def test_limits_edited_under_the_binding_fail_closed(self):
        # Editing only `limits` breaks the ledger's own self-consistency
        # (its stored limits_digest no longer describes its limits), so it is
        # caught by the strict schema BEFORE the binding is even consulted.
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        path = state_store.allocation_ledger_path_for(self.package)
        with open(path) as fh:
            record = json.load(fh)
        record["limits"]["build"]["dispatches"] = 99
        with open(path, "w") as fh:
            json.dump(record, fh)
        for call in (state_store.check_dispatch_allocation,
                     state_store.consume_dispatch_allocation):
            with self.subTest(call=call.__name__):
                with self.assertRaises(state_store.AllocationConflict) as c:
                    call(sid, BUILDING)
                self.assertEqual(c.exception.reason, "ledger_invalid")

    def test_a_self_consistent_limits_swap_still_fails_the_binding(self):
        # The harder case: the ledger is rewritten with LARGER limits AND a
        # matching self-digest, so it is internally valid. Only the digest the
        # session was admitted under proves the swap, and it must.
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        path = state_store.allocation_ledger_path_for(self.package)
        with open(path) as fh:
            record = json.load(fh)
        bigger = allocation.parse_spec("build=99")
        record["limits"] = bigger
        record["limits_digest"] = state_store.limits_digest(bigger)
        with open(path, "w") as fh:
            json.dump(record, fh)
        for call in (state_store.check_dispatch_allocation,
                     state_store.consume_dispatch_allocation):
            with self.subTest(call=call.__name__):
                with self.assertRaises(state_store.AllocationConflict) as c:
                    call(sid, BUILDING)
                self.assertEqual(c.exception.reason, "binding_mismatch")
        self.assertEqual(self.ledger()["consumed"]["build"], 0)

    def test_unusable_limits_are_never_read_as_unlimited(self):
        sid = self.new_session()
        self.admit(sid, "build=1")
        path = state_store.allocation_ledger_path_for(self.package)
        with open(path) as fh:
            record = json.load(fh)
        record["limits"] = {"build": {"dispatches": -4}}
        with open(path, "w") as fh:
            json.dump(record, fh)
        with self.assertRaises(state_store.AllocationConflict) as c:
            state_store.consume_dispatch_allocation(sid, BUILDING)
        self.assertEqual(c.exception.reason, "ledger_invalid")

    def test_a_deleted_ledger_fails_closed_for_a_bound_session(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        os.unlink(state_store.allocation_ledger_path_for(self.package))
        with self.assertRaises(state_store.AllocationConflict) as c:
            state_store.consume_dispatch_allocation(sid, BUILDING)
        self.assertEqual(c.exception.reason, "ledger_missing")

    def test_check_is_read_only(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        before = self.ledger()["consumed"]["build"]
        for _ in range(4):
            verdict = state_store.check_dispatch_allocation(sid, BUILDING)
            self.assertTrue(verdict["allowed"])
            self.assertEqual(verdict["ordinal"], 1)
        self.assertEqual(self.ledger()["consumed"]["build"], before)

    def test_check_reports_the_refusal_an_exhausted_package_will_get(self):
        # This is exactly what the capacity-wake replay path screens on.
        sid = self.new_session()
        self.admit(sid, "build=1")
        state_store.consume_dispatch_allocation(sid, BUILDING)
        verdict = state_store.check_dispatch_allocation(sid, BUILDING)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["refusal_code"], "dispatch_limit_exhausted")

    def test_a_package_ref_can_never_escape_the_allocations_root(self):
        for ref in ("../../etc/passwd", "a/b", "", "   ", None, 7):
            with self.subTest(ref=ref):
                if isinstance(ref, str) and ref.strip():
                    path = state_store.allocation_ledger_path_for(ref)
                    self.assertEqual(os.path.dirname(path),
                                     state_store.allocations_root())
                else:
                    with self.assertRaises(ValueError):
                        state_store.package_ref_digest(ref)


class ConcurrencyTests(LedgerEnv):

    def _race(self, sid, workers, loop_phase=BUILDING):
        granted, refused, errors = [], [], []
        barrier = threading.Barrier(workers)

        def run():
            barrier.wait()
            try:
                granted.append(
                    state_store.consume_dispatch_allocation(sid, loop_phase))
            except state_store.AllocationExhausted:
                refused.append(1)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        return granted, refused

    def test_racing_threads_never_overspend_the_allocation(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        granted, refused = self._race(sid, 12)
        self.assertEqual(len(granted), 2)
        self.assertEqual(len(refused), 10)
        self.assertEqual(sorted(v["ordinal"] for v in granted), [1, 2])
        self.assertEqual(self.ledger()["consumed"]["build"], 2)

    def test_racing_threads_on_a_zero_allocation_grant_nothing(self):
        sid = self.new_session()
        self.admit(sid, "build=0")
        granted, refused = self._race(sid, 8)
        self.assertEqual(granted, [])
        self.assertEqual(len(refused), 8)
        self.assertEqual(self.ledger()["consumed"]["build"], 0)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork")
    def test_racing_processes_never_overspend_the_allocation(self):
        # The lock is a real `fcntl.flock`, so the guarantee holds across
        # genuinely separate OS processes, not only threads. Forked children
        # report their outcome through an exit code; nothing is exec'd.
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        pids = []
        for _ in range(10):
            pid = os.fork()
            if pid == 0:
                code = 1
                try:
                    if state_store.consume_dispatch_allocation(sid, BUILDING):
                        code = 0
                except state_store.AllocationExhausted:
                    code = 1
                except BaseException:  # noqa: BLE001
                    code = 2
                finally:
                    os._exit(code)
            pids.append(pid)
        outcomes = []
        for pid in pids:
            _pid, status = os.waitpid(pid, 0)
            outcomes.append(os.WEXITSTATUS(status))
        self.assertNotIn(2, outcomes)
        self.assertEqual(outcomes.count(0), 2)
        self.assertEqual(self.ledger()["consumed"]["build"], 2)


# --------------------------------------------------------------------------- #
# Layer 3: the `_role_loop` gate, over the real reopen paths.                 #
# --------------------------------------------------------------------------- #

class _LeadSession:
    """A lead whose every send leaves a fresh `ready_for_review` status. Its
    `sent` list is the proof of how many real dispatches a loop performed."""

    def __init__(self, status_path, turns):
        self.status_path = status_path
        self.turns = turns
        self.sent = []
        self.closed = False

    def send(self, text, meta=None):
        self.sent.append(str(text))
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        with open(self.status_path, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"turn": next(self.turns)}}, fh)
        return {"ok": True, "result": "ok"}

    def close(self):
        self.closed = True


class LoopEnv(unittest.TestCase):
    """A pinned sessions root, one session, and a `_role_loop` driver."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloc-loop-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sessions_root = os.path.join(self.tmp, "sessions")
        patcher = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.sessions_root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sid = str(uuid.uuid4())
        self.assets = state_store.session_assets_dir(self.sid)
        os.makedirs(self.assets)
        self.trace = trace_store.Trace(
            os.path.join(self.assets, "trace.jsonl"),
            session_uuid=self.sid, run_id="R")
        self.turns = itertools.count(1)
        self.package = "pkg-" + uuid.uuid4().hex[:8]

    # ---- fixture helpers
    def admit(self, spec, session_uuid=None):
        return state_store.admit_dispatch_allocation(
            session_uuid or self.sid, self.package,
            allocation.parse_spec(spec))

    def ledger(self):
        return state_store.read_dispatch_allocation_ledger(self.package)

    def status_path(self, phase):
        return os.path.join(self.assets, "%s.status.json" % PHASE_ROLES[phase][0])

    def review_path(self, phase):
        return os.path.join(self.assets, "%s.review.json" % PHASE_ROLES[phase][1])

    def events(self, name=None):
        path = os.path.join(self.assets, "trace.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if name is None or r["event"] == name]

    def drive(self, phase, script, session_uuid=..., unverified=False,
              session=None, loop_kwargs=None):
        """Run one `_role_loop` over scripted reviewer verdicts; a resume is a
        second call. `unverified` drives the OWNED-VERIFICATION-RED reopen
        instead of the reviewer, with a synthetic red readiness.

        Returns (outcome, payload, lead session)."""
        lead, reviewer = PHASE_ROLES[phase]
        status_path = self.status_path(phase)
        review_path = self.review_path(phase)
        sid = self.sid if session_uuid is ... else session_uuid
        script = list(script)

        def runner(config, context, selected, artifact_path, review_file,
                   resume_id=None, on_session=None, context_update=None,
                   eval_scratch_path=None, eval_specs=None, **kw):
            verdict = script.pop(0) if script else APPROVE
            with open(review_file, "w") as fh:
                json.dump(verdict, fh)
            return json.loads(json.dumps(verdict))

        review_fn = cowork.make_review_fn(
            cowork.default_config([lead, reviewer]), "ctx", [lead, reviewer],
            review_path, reviewer_runner=runner, reviewer_role=reviewer,
            phase=phase, trace=self.trace, session_uuid=sid)
        lead_session = session or _LeadSession(status_path, self.turns)
        kwargs = dict(role=lead, review_fn=review_fn, trace=self.trace,
                      reviewer_role=reviewer, phase=phase,
                      review_path=review_path, session_uuid=sid)
        kwargs.update(loop_kwargs or {})
        patches = [mock.patch.object(cowork,
                                     "_run_owned_verification_transaction",
                                     return_value=(None, None))]
        if unverified:
            patches.append(mock.patch.object(
                cowork, "_record_readiness_from_transaction",
                return_value={"state": "unverified",
                              "reason": "synthetic owned-verification red"}))
            patches.append(mock.patch.object(
                cowork, "_update_receipt_pointer_for_readiness",
                return_value=None))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            rc, outcome, payload = cowork._role_loop(
                lead_session, "seed", status_path, "", io.StringIO(), **kwargs)
        self.assertEqual(rc, 0)
        return outcome, payload, lead_session

    # ---- assertions
    def assert_allocation_refusal(self, payload, **facts):
        self.assertEqual(payload["kind"], cowork.ALLOCATION_STOP_KIND)
        self.assertEqual(payload["requires"], "operator")
        self.assertIs(payload["approved"], False)
        for key, value in facts.items():
            self.assertEqual(payload.get(key), value, key)


class LoopGateTests(LoopEnv):

    def test_reviewer_revise_cannot_reach_a_third_builder_turn(self):
        # The #116 scenario: the reviewer keeps asking for changes, nothing
        # human is attached, and the automatic hand-back path would run until
        # REVIEW_ROUND_CAP. build2/correction1 stops it at two dispatches.
        self.admit("build=2,correction=1")
        outcome, payload, sess = self.drive(BUILDING, [REVISE] * 6)
        self.assertEqual(len(sess.sent), 2)
        self.assertEqual(outcome, "ended")
        self.assertNotEqual(outcome, "approved")
        self.assert_allocation_refusal(
            payload, allocation_phase="build", attempted_ordinal=3,
            attempted_kind="correction", refusal_code="dispatch_limit_exhausted",
            limit_dispatches=2, limit_corrections=1,
            consumed_dispatches=2, consumed_corrections=1,
            package_ref=self.package)
        self.assertEqual(self.ledger()["consumed"]["build"], 2)

    def test_owned_verification_red_cannot_reach_a_third_builder_turn(self):
        # The other automatic reopen: Cowork's own owned verification comes
        # back red and hands the candidate straight back to the builder with
        # no reviewer and no human in the loop.
        self.admit("build=2,correction=1")
        outcome, payload, sess = self.drive(BUILDING, [], unverified=True)
        self.assertEqual(len(sess.sent), 2)
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(
            payload, attempted_ordinal=3, consumed_dispatches=2,
            refusal_code="dispatch_limit_exhausted")

    def test_a_correction_cap_of_zero_admits_only_the_initial_turn(self):
        self.admit("build=5,correction=0")
        outcome, payload, sess = self.drive(BUILDING, [REVISE] * 4)
        self.assertEqual(len(sess.sent), 1)
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(
            payload, refusal_code="correction_limit_exhausted",
            attempted_ordinal=2, attempted_kind="correction",
            consumed_dispatches=1)

    def test_a_zero_allocation_launches_no_provider_at_all(self):
        for phase, spec in ((BUILDING, "build=0"),
                            (SCOUTING, "discovery=0"),
                            (PLANNING, "planning=0")):
            with self.subTest(phase=phase):
                self.setUp()
                self.admit(spec)
                outcome, payload, sess = self.drive(phase, [APPROVE])
                self.assertEqual(sess.sent, [])
                self.assertEqual(outcome, "ended")
                self.assert_allocation_refusal(
                    payload, attempted_ordinal=1, attempted_kind="initial",
                    consumed_dispatches=0, consumed_corrections=0,
                    refusal_code="dispatch_limit_exhausted")

    def test_discovery_and_planning_bounds_hold_on_their_own_phases(self):
        for phase, spec, key in ((SCOUTING, "discovery=2", "discovery"),
                                 (PLANNING, "planning=2", "planning")):
            with self.subTest(phase=phase):
                self.setUp()
                self.admit(spec)
                outcome, payload, sess = self.drive(phase, [REVISE] * 6)
                self.assertEqual(len(sess.sent), 2)
                self.assert_allocation_refusal(
                    payload, allocation_phase=key, attempted_ordinal=3)
                self.assertEqual(self.ledger()["consumed"][key], 2)

    def test_an_unallocated_phase_keeps_its_existing_behavior(self):
        # build is bounded; scouting is not mentioned at all and must behave
        # exactly as it did before this mechanism existed.
        self.admit("build=1")
        outcome, _payload, sess = self.drive(
            SCOUTING, [REVISE, REVISE, APPROVE])
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(sess.sent), 3)
        self.assertEqual(self.ledger()["consumed"]["discovery"], 0)

    def test_a_session_with_no_allocation_is_unchanged(self):
        outcome, _payload, sess = self.drive(
            BUILDING, [REVISE, REVISE, REVISE, APPROVE])
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(sess.sent), 4)
        self.assertIsNone(state_store.read_dispatch_allocation_binding(self.sid))

    def test_an_unsessioned_loop_is_unchanged(self):
        self.admit("build=0")
        outcome, _payload, sess = self.drive(
            BUILDING, [REVISE, APPROVE], session_uuid=None)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(sess.sent), 2)

    def test_the_allocation_never_turns_a_refusal_into_an_approval(self):
        # A generous allocation does not approve anything by itself: the
        # paired reviewer still decides, and a reviewer that never approves
        # still ends the phase unapproved.
        self.admit("build=50")
        outcome, payload, _sess = self.drive(
            BUILDING, [REVISE] * cowork.REVIEW_ROUND_CAP)
        self.assertEqual(outcome, "stopped")
        self.assertEqual(payload["kind"], "review_round_cap")
        self.assertIs(payload["approved"], False)

    def test_an_approve_still_approves_under_an_allocation(self):
        self.admit("build=2,correction=1")
        outcome, _payload, sess = self.drive(BUILDING, [APPROVE])
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(sess.sent), 1)
        self.assertEqual(self.ledger()["consumed"]["build"], 1)

    def test_the_refusal_is_traced_as_a_runtime_gate_decision(self):
        self.admit("build=1")
        self.drive(BUILDING, [REVISE, REVISE])
        refusals = [e for e in self.events("gate.decision")
                    if e.get("gate") == "dispatch_allocation"
                    and e.get("action") == "refuse"]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["refusal_code"],
                         "dispatch_limit_exhausted")
        consumes = [e for e in self.events("gate.decision")
                    if e.get("gate") == "dispatch_allocation"
                    and e.get("action") == "consume"]
        self.assertEqual([e["ordinal"] for e in consumes], [1])
        self.assertEqual([e["dispatch_kind"] for e in consumes], ["initial"])


class ResumeAndRecoveryTests(LoopEnv):

    def test_a_persisted_resume_cannot_restart_the_budget(self):
        # Two separate `_role_loop` invocations against the same session --
        # what a resume is -- share one durable ledger.
        self.admit("build=2,correction=1")
        _outcome, _payload, first = self.drive(BUILDING, [REVISE, REVISE])
        self.assertEqual(len(first.sent), 2)
        outcome, payload, second = self.drive(BUILDING, [REVISE])
        self.assertEqual(second.sent, [])
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(payload, attempted_ordinal=3,
                                       consumed_dispatches=2)

    def test_a_replacement_session_cannot_restart_the_budget(self):
        # A fresh session uuid bound to the SAME package ref: the "replacement
        # selector" case. It inherits the spent counters.
        self.admit("build=1")
        _outcome, _payload, first = self.drive(BUILDING, [REVISE])
        self.assertEqual(len(first.sent), 1)
        replacement = str(uuid.uuid4())
        os.makedirs(state_store.session_assets_dir(replacement), exist_ok=True)
        self.admit("build=1", session_uuid=replacement)
        outcome, payload, sess = self.drive(
            BUILDING, [REVISE], session_uuid=replacement)
        self.assertEqual(sess.sent, [])
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(payload, consumed_dispatches=1)

    def test_a_ledger_that_goes_corrupt_mid_session_fails_closed(self):
        self.admit("build=5")
        _outcome, _payload, first = self.drive(BUILDING, [REVISE, APPROVE])
        self.assertGreaterEqual(len(first.sent), 1)
        with open(state_store.allocation_ledger_path_for(self.package),
                  "w") as fh:
            fh.write("not json at all")
        outcome, payload, sess = self.drive(BUILDING, [APPROVE])
        self.assertEqual(sess.sent, [])
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(
            payload, refusal_code="allocation_unreadable")

    def test_limits_edited_mid_session_fail_closed(self):
        # An internally inconsistent ledger is caught by the strict schema.
        self.admit("build=1")
        path = state_store.allocation_ledger_path_for(self.package)
        with open(path) as fh:
            record = json.load(fh)
        record["limits"]["build"]["dispatches"] = 99
        with open(path, "w") as fh:
            json.dump(record, fh)
        outcome, payload, sess = self.drive(BUILDING, [APPROVE])
        self.assertEqual(sess.sent, [])
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(
            payload, refusal_code="allocation_unreadable",
            conflict_reason="ledger_invalid")

    def test_a_self_consistent_limits_swap_mid_session_fails_closed(self):
        # A self-consistent swap to a larger allocation is caught by the
        # digest the session was admitted under.
        self.admit("build=1")
        path = state_store.allocation_ledger_path_for(self.package)
        with open(path) as fh:
            record = json.load(fh)
        bigger = allocation.parse_spec("build=99")
        record["limits"] = bigger
        record["limits_digest"] = state_store.limits_digest(bigger)
        with open(path, "w") as fh:
            json.dump(record, fh)
        outcome, payload, sess = self.drive(BUILDING, [APPROVE])
        self.assertEqual(sess.sent, [])
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(
            payload, refusal_code="allocation_binding_mismatch")

    def test_a_deleted_ledger_mid_session_fails_closed(self):
        self.admit("build=5")
        os.unlink(state_store.allocation_ledger_path_for(self.package))
        outcome, payload, sess = self.drive(BUILDING, [APPROVE])
        self.assertEqual(sess.sent, [])
        self.assert_allocation_refusal(
            payload, refusal_code="allocation_unreadable")


# --------------------------------------------------------------------------- #
# The orchestrator-facing contract: the refusal is typed, terminal, and       #
# cannot be answered into a larger budget.                                    #
# --------------------------------------------------------------------------- #

class RefusalContractTests(LoopEnv):

    def _refusal_payload(self):
        self.admit("build=1")
        _outcome, payload, _sess = self.drive(BUILDING, [REVISE, REVISE])
        return payload

    def test_the_stop_kind_is_not_an_answerable_decision(self):
        self.assertNotIn(cowork.ALLOCATION_STOP_KIND,
                         state_store.DECISION_RESPONSES)

    def test_the_run_result_reports_a_failure_with_the_exact_counts(self):
        payload = self._refusal_payload()
        result = cowork.build_run_result(1, {
            "last": {"outcome": "ended", "payload": payload,
                     "role": "builder", "phase": BUILDING},
            "session_file": os.path.join(self.tmp, "session.json"),
            "session_uuid": self.sid,
            "dispatch_allocation": {"package_ref": self.package,
                                    "limits": "build=1",
                                    "consumed": {}},
        })
        self.assertEqual(result["rc"], 1)
        self.assertEqual(result["outcome"], "failed")
        self.assertIs(result["approved"], False)
        stop = result["stop"]
        self.assertEqual(stop["kind"], cowork.ALLOCATION_STOP_KIND)
        self.assertEqual(stop["requires"], "operator")
        self.assertEqual(stop["consumed_dispatches"], 1)
        self.assertEqual(stop["limit_dispatches"], 1)
        self.assertEqual(stop["attempted_ordinal"], 2)
        # No reply of any kind is offered: the budget cannot be answered up.
        self.assertNotIn("decision_argv", result)
        self.assertEqual(result["resume_argv"][0], "--session-file")
        # The durable counts are re-read from the ledger, not from the caller.
        self.assertEqual(result["dispatch_allocation"]["consumed"]["build"], 1)

    def test_no_code_path_raises_an_admitted_limit(self):
        # Nothing anywhere increases a limit once admitted: every durable
        # mutation only ever increments a consumed counter.
        payload = self._refusal_payload()
        self.assertEqual(payload["limit_dispatches"], 1)
        for _ in range(3):
            outcome, again, sess = self.drive(BUILDING, [REVISE])
            self.assertEqual(sess.sent, [])
            self.assertEqual(outcome, "ended")
            self.assertEqual(again["limit_dispatches"], 1)
        self.assertEqual(self.ledger()["limits"]["build"]["dispatches"], 1)
        self.assertEqual(self.ledger()["consumed"]["build"], 1)


class PreLaunchScreenTests(LoopEnv):
    """`_role_loop` is where a unit is CHARGED, but a controller process is
    constructed before that loop is entered. The pre-launch screen refuses
    first, consuming nothing, so an exhausted budget never spawns one."""

    def test_the_screen_passes_a_phase_with_budget_left(self):
        self.admit("build=2,correction=1")
        self.assertIsNone(cowork._allocation_preflight_stop(
            self.sid, BUILDING, "builder", trace=self.trace))
        self.assertEqual(self.ledger()["consumed"]["build"], 0)

    def test_the_screen_refuses_a_zero_allocation_before_any_launch(self):
        self.admit("build=0")
        stop = cowork._allocation_preflight_stop(
            self.sid, BUILDING, "builder", trace=self.trace)
        self.assert_allocation_refusal(
            stop, allocation_phase="build", attempted_ordinal=1,
            attempted_kind="initial", consumed_dispatches=0,
            refusal_code="dispatch_limit_exhausted")
        self.assertEqual(self.ledger()["consumed"]["build"], 0)

    def test_the_screen_refuses_an_exhausted_allocation(self):
        self.admit("build=1")
        state_store.consume_dispatch_allocation(self.sid, BUILDING)
        stop = cowork._allocation_preflight_stop(
            self.sid, BUILDING, "builder", trace=self.trace)
        self.assert_allocation_refusal(stop, attempted_ordinal=2,
                                       consumed_dispatches=1)
        # Still exactly one spent: screening is read-only.
        self.assertEqual(self.ledger()["consumed"]["build"], 1)

    def test_the_screen_fails_closed_on_an_unreadable_ledger(self):
        self.admit("build=5")
        with open(state_store.allocation_ledger_path_for(self.package),
                  "w") as fh:
            fh.write("{ broken")
        stop = cowork._allocation_preflight_stop(
            self.sid, BUILDING, "builder", trace=self.trace)
        self.assert_allocation_refusal(
            stop, refusal_code="allocation_unreadable",
            conflict_reason="ledger_corrupt")

    def test_the_screen_is_silent_without_an_allocation(self):
        for sid in (self.sid, None):
            with self.subTest(sid=sid):
                self.assertIsNone(cowork._allocation_preflight_stop(
                    sid, BUILDING, "builder", trace=self.trace))

    def test_the_screen_ignores_phases_no_allocation_bounds(self):
        self.admit("build=0")
        for loop_phase in (SCOUTING, PLANNING):
            self.assertIsNone(cowork._allocation_preflight_stop(
                self.sid, loop_phase, "scout", trace=self.trace))

    def test_every_lead_launch_site_screens_before_it_dispatches(self):
        # Structural: each of the three lead launches in `run_flow` runs the
        # screen between its controller-availability check and its run_*_fn
        # call, so none of them can construct a controller first.
        source = open(cowork.__file__).read()
        for role, loop_phase, runner in (
                ("scout", SCOUTING, "run_scout_fn("),
                ("planner", PLANNING, "run_planner_fn("),
                ("builder", BUILDING, "run_builder_fn(")):
            with self.subTest(role=role):
                guard = ('ensure_controller_available("%s", '
                         'reason="lead_launch")' % role)
                screen = ('_allocation_preflight_stop(\n')
                self.assertIn(guard, source)
                guard_at = source.index(guard)
                runner_at = source.index(runner, guard_at)
                window = source[guard_at:runner_at]
                self.assertIn(screen, window)
                self.assertIn('"%s", "%s", trace=trace' % (loop_phase, role),
                              window)


class CapacityReplayChainTests(unittest.TestCase):
    """The capacity-wake replay on the REAL production path.

    These drive `cowork._role_loop` into a genuine durable `awaiting_capacity`
    pause and then `cowork.run_resume_trigger` in-process with real argv --
    never a read-only helper standing in for the dispatch that actually
    happens. A replay performs a real provider send, so it must charge a real
    unit before the controller is constructed."""

    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="alloc-wake-")
        self.addCleanup(shutil.rmtree, self._root, True)
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)
        workdir = tempfile.mkdtemp(prefix="alloc-wake-cwd-")
        self.addCleanup(shutil.rmtree, workdir, True)
        prior = os.getcwd()
        self.addCleanup(os.chdir, prior)
        os.chdir(workdir)
        self._dir = workdir
        self.package = "pkg-" + uuid.uuid4().hex[:8]

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root

    # ---- fixtures (self-contained; neutral synthetic inputs only)
    def _session(self):
        suid = str(uuid.uuid4())
        spath = os.path.join(self._dir, ".cowork", "session.json")
        state_store.ensure_session(spath, None, suid)
        return spath, suid

    def admit(self, session_uuid, spec):
        return state_store.admit_dispatch_allocation(
            session_uuid, self.package, allocation.parse_spec(spec))

    def ledger(self):
        return state_store.read_dispatch_allocation_ledger(self.package)

    def consumed(self, phase="build"):
        return self.ledger()["consumed"][phase]

    def _bind_candidate(self, session_uuid, role, controller="claude"):
        work_id = cowork._role_work_id(session_uuid, role, 0, 0)
        manifest, _ = cowork._compile_role_manifest(
            role=role, session_uuid=session_uuid, work_id=role,
            controller=controller, mode="implement", model=None, effort=None,
            sessions_dir=state_store.session_assets_dir(session_uuid))
        cowork._ensure_work_unit(session_uuid, work_id, role, controller)
        cowork._advance_phase(session_uuid, work_id, "preflight_started")
        cowork._advance_phase(session_uuid, work_id, "preflight_passed")
        cowork._bind_candidate(session_uuid, work_id, manifest["digest"])
        return work_id

    def _status_path(self, session_uuid, role):
        path = os.path.join(state_store.session_assets_dir(session_uuid),
                            "%s.status.json" % role)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"status": "needs_input"}, fh)
        return path

    class _FailingSession:
        """A send that fails with a genuine quota classification."""

        def __init__(self, controller="claude", session_id="prov-sess-1"):
            self.controller = controller
            self.session_id = session_id
            self.model = None
            self.effort = None
            self.sends = []

        def send(self, text, meta=None):
            self.sends.append(str(text))
            return {"ok": False, "result": "error",
                    "error_type": "rate_limit_error",
                    "retry_evidence": {"source": "provider_header",
                                       "value": "0s"}}

        def close(self):
            pass

    class _AcceptingSession:
        def __init__(self, controller="claude", session_id="prov-sess-1"):
            self.controller = controller
            self.session_id = session_id
            self.model = None
            self.effort = None
            self.sends = []

        def send(self, text, meta=None):
            self.sends.append(str(text))
            return {"ok": True, "result": "ok"}

        def close(self):
            pass

    def _enter_capacity(self, role="builder"):
        """Real `_role_loop` -> classified quota failure -> durable pause."""
        spath, suid = self._session()
        work_id = self._bind_candidate(suid, role)
        status_path = self._status_path(suid, role)
        sess = self._FailingSession()
        _rc, outcome, payload = cowork._role_loop(
            sess, "neutral bounded work", status_path, context="",
            io_out=io.StringIO(), role=role, phase=BUILDING,
            session_uuid=suid, role_work_id=work_id)
        self.assertEqual(outcome, "awaiting_capacity",
                         "fixture precondition: capacity entry must succeed")
        state = state_store.load(spath)
        state.setdefault("config", {})[role] = {
            "controller": "claude", "model": None, "effort": None,
            "mode": "implement", "yolo": True}
        state.setdefault("sessions", {})[role] = {
            "controller": "claude", "id": sess.session_id}
        state_store.save(spath, state)
        return suid, work_id, payload, sess

    def _run_resume_trigger(self, suid, payload, factory, claimant="claimant-1",
                            automation_ref=None):
        argv = ["--session-uuid", suid, "--lease-id", payload["lease_id"],
                "--claimant-ref", claimant,
                "--automation-ref", automation_ref or payload["automation_ref"],
                "--now", cowork._capacity_now()]
        lines = []
        rc = cowork.run_resume_trigger(
            argv, output=lines.append, session_factory=factory)
        return rc, (json.loads(lines[0]) if lines else None)

    # ---- the chain
    def test_the_capacity_replay_charges_its_own_real_dispatch(self):
        suid, _work_id, payload, failing = self._enter_capacity()
        self.admit(suid, "build=2,correction=1")
        # The pause fixture ran before admission, so the ledger starts clean
        # and this test isolates exactly what the REPLAY charges.
        self.assertEqual(self.consumed(), 0)
        self.assertEqual(len(failing.sends), 1)
        accepted = self._AcceptingSession()
        rc, result = self._run_resume_trigger(
            suid, payload, lambda *a, **k: accepted)
        self.assertEqual(result["outcome"], "success", result)
        self.assertEqual(rc, 0)
        # A real provider send happened, and it cost exactly one unit.
        self.assertEqual(len(accepted.sends), 1)
        self.assertEqual(self.consumed(), 1)

    def test_build2_cannot_buy_three_real_sends_through_a_capacity_pause(self):
        # The exact overspend the check-only replay allowed:
        #   initial (ledger 1) -> pause -> replay (ledger 2) -> correction
        # must be REFUSED, because the replay is itself a paid send.
        spath, suid = self._session()
        role = "builder"
        work_id = self._bind_candidate(suid, role)
        status_path = self._status_path(suid, role)
        self.admit(suid, "build=2,correction=1")

        failing = self._FailingSession()
        _rc, outcome, payload = cowork._role_loop(
            failing, "neutral bounded work", status_path, context="",
            io_out=io.StringIO(), role=role, phase=BUILDING,
            session_uuid=suid, role_work_id=work_id)
        self.assertEqual(outcome, "awaiting_capacity")
        self.assertEqual(len(failing.sends), 1)
        self.assertEqual(self.consumed(), 1)    # the initial dispatch
        state = state_store.load(spath)
        state.setdefault("config", {})[role] = {
            "controller": "claude", "model": None, "effort": None,
            "mode": "implement", "yolo": True}
        state.setdefault("sessions", {})[role] = {
            "controller": "claude", "id": failing.session_id}
        state_store.save(spath, state)

        accepted = self._AcceptingSession()
        rc, result = self._run_resume_trigger(
            suid, payload, lambda *a, **k: accepted)
        self.assertEqual(result["outcome"], "success", result)
        self.assertEqual(len(accepted.sends), 1)
        self.assertEqual(self.consumed(), 2)    # the replay is a paid send

        # Budget gone: the third real send can never happen on any path.
        third = self._AcceptingSession()
        _rc3, outcome3, payload3 = cowork._role_loop(
            third, "neutral bounded work", status_path, context="",
            io_out=io.StringIO(), role=role, phase=BUILDING,
            session_uuid=suid, role_work_id=work_id)
        self.assertEqual(third.sends, [])
        self.assertEqual(outcome3, "ended")
        self.assertEqual(payload3["kind"], cowork.ALLOCATION_STOP_KIND)
        self.assertEqual(payload3["consumed_dispatches"], 2)
        self.assertEqual(payload3["attempted_ordinal"], 3)
        self.assertEqual(self.consumed(), 2)

    def test_an_exhausted_package_refuses_the_replay_before_any_controller(self):
        suid, _work_id, payload, _failing = self._enter_capacity()
        self.admit(suid, "build=0")
        constructed = []

        def factory(*args, **kwargs):
            constructed.append(args)
            raise AssertionError("a controller was constructed after refusal")

        rc, result = self._run_resume_trigger(suid, payload, factory)
        self.assertNotEqual(rc, 0)
        self.assertEqual(result["reason"], "dispatch_allocation_exhausted")
        self.assertEqual(constructed, [])
        self.assertEqual(self.consumed(), 0)

    def test_an_unprovable_binding_refuses_the_replay(self):
        suid, _work_id, payload, _failing = self._enter_capacity()
        self.admit(suid, "build=5")
        with open(state_store.allocation_ledger_path_for(self.package),
                  "w") as fh:
            fh.write("{ not json")

        def factory(*args, **kwargs):
            raise AssertionError("a controller was constructed after refusal")

        rc, result = self._run_resume_trigger(suid, payload, factory)
        self.assertNotEqual(rc, 0)
        self.assertEqual(result["reason"], "dispatch_allocation_unreadable")

    def test_an_unbound_session_replays_exactly_as_before(self):
        suid, _work_id, payload, _failing = self._enter_capacity()
        accepted = self._AcceptingSession()
        rc, result = self._run_resume_trigger(
            suid, payload, lambda *a, **k: accepted)
        self.assertEqual(result["outcome"], "success", result)
        self.assertEqual(rc, 0)
        self.assertEqual(len(accepted.sends), 1)
        self.assertIsNone(
            state_store.read_dispatch_allocation_binding(suid))

    def test_a_failed_replay_send_does_not_refund_its_unit(self):
        suid, _work_id, payload, _failing = self._enter_capacity()
        self.admit(suid, "build=3")
        self.assertEqual(self.consumed(), 0)

        class _Rejecting:
            controller = "claude"
            session_id = "prov-sess-1"
            model = None
            effort = None

            def __init__(self):
                self.sends = []

            def send(self, text, meta=None):
                self.sends.append(str(text))
                return {"ok": False, "result": "error",
                        "error_type": "rate_limit_error"}

            def close(self):
                pass

        rejecting = _Rejecting()
        _rc, _result = self._run_resume_trigger(
            suid, payload, lambda *a, **k: rejecting)
        self.assertEqual(len(rejecting.sends), 1)
        # The send was attempted and paid for; a refund is exactly the error
        # that would let the package overspend.
        self.assertEqual(self.consumed(), 1)

    def test_the_role_to_phase_map_covers_exactly_the_lead_roles(self):
        self.assertEqual(cowork._ALLOCATION_PHASE_BY_ROLE,
                         {"scout": SCOUTING, "planner": PLANNING,
                          "builder": BUILDING})
        for reviewer in ("scout-reviewer", "planning-advisor",
                         "build-reviewer", "worktree"):
            self.assertNotIn(reviewer, cowork._ALLOCATION_PHASE_BY_ROLE)


# --------------------------------------------------------------------------- #
# The invocation contract: a malformed or unbindable allocation is refused    #
# with rc 2 before anything is created, and an exhausted one never reaches a  #
# lead runner at all.                                                         #
# --------------------------------------------------------------------------- #

class InvocationTests(unittest.TestCase):
    """`run_flow` with every lead runner replaced by a fake that fails the
    test if it is reached: nothing here may dispatch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloc-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(
            os.environ,
            {"COWORK_SESSIONS_ROOT": os.path.join(self.tmp, "sessions")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _unscripted(self, name):
        def fail(*_a, **_k):
            raise AssertionError("unscripted %s reached" % name)
        return fail

    def _run(self, argv, **kw):
        box = {}
        out = io.StringIO()
        for name in ("run_scout_fn", "run_planner_fn", "run_builder_fn",
                     "run_worktree_fn"):
            kw.setdefault(name, self._unscripted(name))
        rc = cowork.run_flow(
            cowork.build_parser().parse_args(argv), io_out=out,
            which=lambda c: "/bin/" + c, result_box=box, **kw)
        return rc, cowork.build_run_result(rc, box)

    def test_a_malformed_allocation_is_refused_before_anything_is_created(self):
        for spec in ("bogus", "build=-1", "build=two", "build=2,build=3",
                     "", "nonsense=1"):
            with self.subTest(spec=spec):
                rc, result = self._run(
                    ["--package-ref", "synthetic-package",
                     "--dispatch-allocation", spec, "--context", "x"])
                self.assertEqual(rc, 2)
                self.assertEqual(result["reason"], "allocation_malformed")
                self.assertEqual(result["outcome"], "invalid_invocation")
                self.assertIs(result["approved"], False)
                self.assertIsNone(result["session_uuid"])

    def test_an_allocation_without_a_package_ref_is_refused(self):
        rc, result = self._run(
            ["--dispatch-allocation", "build=2", "--context", "x"])
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason"], "allocation_requires_package_ref")

    def test_an_ephemeral_run_cannot_carry_an_allocation(self):
        rc, result = self._run(
            ["--package-ref", "synthetic-package",
             "--dispatch-allocation", "build=2", "--no-session",
             "--context", "x"])
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason"], "allocation_requires_session")

    def test_a_package_ref_naming_no_admitted_allocation_is_refused(self):
        rc, result = self._run(
            ["--package-ref", "never-admitted", "--context", "x"])
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason"], "allocation_not_admitted")

    def test_an_unsafe_package_ref_is_refused(self):
        rc, result = self._run(
            ["--package-ref", "   ", "--dispatch-allocation", "build=1",
             "--context", "x"])
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason"], "package_ref_malformed")

    def test_no_allocation_flags_change_nothing_about_the_invocation(self):
        # The same run without the flags gets past allocation screening
        # entirely and fails only because its lead runner is unscripted.
        with self.assertRaises(AssertionError) as caught:
            self._run(["--context", "x"])
        self.assertIn("unscripted", str(caught.exception))


# --------------------------------------------------------------------------- #
# Fail-closed bound state: losing, truncating or forging a binding record     #
# never restores unbounded dispatch.                                          #
# --------------------------------------------------------------------------- #

class BoundWitnessTests(LedgerEnv):
    """A session is bound when EITHER durable record exists. Only the complete
    absence of both can mean "this is an old, unallocated session"."""

    def _records(self, sid):
        return (state_store.dispatch_allocation_witness_path_for(sid),
                state_store.dispatch_allocation_binding_path_for(sid))

    def test_admission_writes_both_durable_records(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        for path in self._records(sid):
            self.assertTrue(os.path.exists(path), path)
        binding = state_store.read_dispatch_allocation_binding(sid)
        self.assertEqual(binding["package_ref"], self.package)
        self.assertEqual(binding["session_uuid"], sid)

    def test_losing_either_record_fails_closed(self):
        for index in (0, 1):
            with self.subTest(record=index):
                sid = self.new_session()
                self.admit(sid, "build=2")
                os.unlink(self._records(sid)[index])
                for call in (state_store.read_dispatch_allocation_binding,
                             state_store.check_dispatch_allocation,
                             state_store.consume_dispatch_allocation):
                    args = ((sid,) if call is
                            state_store.read_dispatch_allocation_binding
                            else (sid, BUILDING))
                    with self.assertRaises(state_store.AllocationConflict) as c:
                        call(*args)
                    self.assertEqual(c.exception.reason, "binding_incomplete")

    def test_truncating_either_record_fails_closed(self):
        for index in (0, 1):
            with self.subTest(record=index):
                sid = self.new_session()
                self.admit(sid, "build=2")
                with open(self._records(sid)[index], "w") as fh:
                    fh.write("{ truncated")
                with self.assertRaises(state_store.AllocationConflict) as c:
                    state_store.consume_dispatch_allocation(sid, BUILDING)
                self.assertEqual(c.exception.reason, "binding_corrupt")

    def test_disagreeing_records_fail_closed(self):
        for field, value in (("package_ref", "other-synthetic-package"),
                             ("limits_digest", "0" * 64)):
            with self.subTest(field=field):
                sid = self.new_session()
                self.admit(sid, "build=2")
                path = self._records(sid)[1]
                with open(path) as fh:
                    record = json.load(fh)
                record[field] = value
                with open(path, "w") as fh:
                    json.dump(record, fh)
                with self.assertRaises(state_store.AllocationConflict) as c:
                    state_store.consume_dispatch_allocation(sid, BUILDING)
                self.assertIn(c.exception.reason,
                              ("binding_mismatch", "binding_invalid"))

    def test_a_binding_copied_from_another_session_is_refused(self):
        donor = self.new_session()
        self.admit(donor, "build=2")
        thief = self.new_session()
        for src, dst in zip(self._records(donor), self._records(thief)):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src) as fh:
                raw = fh.read()
            with open(dst, "w") as fh:
                fh.write(raw)
        with self.assertRaises(state_store.AllocationConflict) as c:
            state_store.consume_dispatch_allocation(thief, BUILDING)
        self.assertEqual(c.exception.reason, "binding_invalid")

    def test_a_truly_unbound_session_is_still_legacy(self):
        sid = self.new_session()
        for path in self._records(sid):
            self.assertFalse(os.path.exists(path))
        self.assertIsNone(state_store.read_dispatch_allocation_binding(sid))
        self.assertIsNone(state_store.check_dispatch_allocation(sid, BUILDING))
        self.assertIsNone(
            state_store.consume_dispatch_allocation(sid, BUILDING))


class StrictSchemaTests(LedgerEnv):
    """Nothing on these paths defaults, coerces or repairs."""

    def _ledger_raw(self):
        with open(state_store.allocation_ledger_path_for(self.package)) as fh:
            return json.load(fh)

    def _write_ledger(self, record):
        with open(state_store.allocation_ledger_path_for(self.package),
                  "w") as fh:
            json.dump(record, fh)

    def test_every_counter_type_is_exact(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        original = self._ledger_raw()
        for invalid in ("0", "1", True, False, 1.0, 1.5, -1, None, [], {}):
            with self.subTest(invalid=repr(invalid)):
                record = dict(original, consumed=dict(original["consumed"]))
                record["consumed"]["build"] = invalid
                self._write_ledger(record)
                with self.assertRaises(state_store.AllocationConflict) as c:
                    state_store.consume_dispatch_allocation(sid, BUILDING)
                self.assertEqual(c.exception.reason, "ledger_invalid")

    def test_the_counter_set_must_be_exactly_the_phases(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        original = self._ledger_raw()
        for mutate in (lambda d: d.pop("build"),
                       lambda d: d.pop("discovery"),
                       lambda d: d.update(extra=0)):
            with self.subTest(mutate=mutate):
                record = dict(original, consumed=dict(original["consumed"]))
                mutate(record["consumed"])
                self._write_ledger(record)
                with self.assertRaises(state_store.AllocationConflict) as c:
                    state_store.consume_dispatch_allocation(sid, BUILDING)
                self.assertEqual(c.exception.reason, "ledger_invalid")

    def test_mandatory_ledger_fields_are_mandatory(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        original = self._ledger_raw()
        for field in ("schema", "package_ref", "limits", "limits_digest",
                      "consumed", "sessions"):
            with self.subTest(field=field):
                record = dict(original)
                record.pop(field)
                self._write_ledger(record)
                with self.assertRaises(state_store.AllocationConflict):
                    state_store.consume_dispatch_allocation(sid, BUILDING)

    def test_a_wrong_schema_version_is_refused(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        original = self._ledger_raw()
        for version in (0, 2, "1", True, None, 1.0):
            with self.subTest(version=repr(version)):
                self._write_ledger(dict(original, schema=version))
                with self.assertRaises(state_store.AllocationConflict) as c:
                    state_store.consume_dispatch_allocation(sid, BUILDING)
                self.assertEqual(c.exception.reason, "ledger_invalid")

    def test_a_ledger_must_identify_its_own_package(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        original = self._ledger_raw()
        self._write_ledger(dict(original, package_ref="unrelated-package"))
        with self.assertRaises(state_store.AllocationConflict) as c:
            state_store.consume_dispatch_allocation(sid, BUILDING)
        self.assertEqual(c.exception.reason, "ledger_invalid")

    def test_a_binding_digest_is_mandatory_and_well_formed(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        path = state_store.dispatch_allocation_binding_path_for(sid)
        with open(path) as fh:
            original = json.load(fh)
        for value in (None, "", "abc", "Z" * 64, 1, True, "a" * 63):
            with self.subTest(value=repr(value)):
                record = dict(original)
                record["limits_digest"] = value
                with open(path, "w") as fh:
                    json.dump(record, fh)
                with self.assertRaises(state_store.AllocationConflict) as c:
                    state_store.consume_dispatch_allocation(sid, BUILDING)
                self.assertEqual(c.exception.reason, "binding_invalid")

    def test_an_incomplete_existing_ledger_is_never_fresh_admission(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        original = self._ledger_raw()
        for broken in ({}, {"schema": 1}, {"limits": None},
                       dict(original, consumed={}),
                       dict(original, limits_digest="0" * 64)):
            with self.subTest(broken=broken):
                self._write_ledger(broken)
                replacement = self.new_session()
                with self.assertRaises(state_store.AllocationConflict):
                    self.admit(replacement, "build=2")
                # Refused without rewriting the existing durable state.
                self.assertEqual(self._ledger_raw(), broken)


class PackageAdmissionMarkerTests(LedgerEnv):
    """Deleting a spent ledger must not look like a package that was never
    admitted. The marker is the durable fact that admission happened."""

    def test_admission_writes_a_marker_beside_the_ledger(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        marker = state_store.read_package_admission_marker(self.package)
        self.assertEqual(marker["package_ref"], self.package)
        self.assertEqual(
            marker["limits_digest"],
            state_store.limits_digest(
                allocation.parse_spec("build=2,correction=1")))

    def test_a_deleted_ledger_is_never_re_admitted_as_a_fresh_budget(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        state_store.consume_dispatch_allocation(sid, BUILDING)
        state_store.consume_dispatch_allocation(sid, BUILDING)
        path = state_store.allocation_ledger_path_for(self.package)
        os.unlink(path)
        # The same session, and any replacement session, are both refused.
        for session_uuid in (sid, self.new_session()):
            with self.subTest(session=session_uuid):
                with self.assertRaises(state_store.AllocationConflict) as c:
                    self.admit(session_uuid, "build=2,correction=1")
                self.assertEqual(c.exception.reason, "ledger_missing")
                self.assertFalse(os.path.exists(path))

    def test_a_marker_with_other_limits_refuses_re_admission(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        os.unlink(state_store.allocation_ledger_path_for(self.package))
        with self.assertRaises(state_store.AllocationConflict) as c:
            self.admit(self.new_session(), "build=9")
        self.assertEqual(c.exception.reason, "limits_immutable")

    def test_a_corrupt_marker_fails_closed(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        with open(state_store.package_admission_marker_path_for(self.package),
                  "w") as fh:
            fh.write("{ truncated")
        with self.assertRaises(state_store.AllocationConflict) as c:
            self.admit(self.new_session(), "build=2")
        self.assertEqual(c.exception.reason, "marker_corrupt")

    def test_a_marker_must_identify_its_own_package(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        path = state_store.package_admission_marker_path_for(self.package)
        with open(path) as fh:
            record = json.load(fh)
        record["package_ref"] = "unrelated-synthetic-package"
        with open(path, "w") as fh:
            json.dump(record, fh)
        with self.assertRaises(state_store.AllocationConflict) as c:
            self.admit(self.new_session(), "build=2")
        self.assertEqual(c.exception.reason, "marker_invalid")

    def test_the_marker_is_created_once_and_never_overwritten(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        path = state_store.package_admission_marker_path_for(self.package)
        with open(path) as fh:
            before = fh.read()
        # A second create attempt reports that it lost and writes nothing.
        self.assertFalse(state_store.create_json_exclusive_durable(
            path, {"schema": 1, "package_ref": self.package,
                   "limits_digest": "f" * 64, "admitted": 1.0}))
        with open(path) as fh:
            self.assertEqual(fh.read(), before)

    def test_a_contender_that_loses_the_marker_race_is_refused(self):
        # The loser must never overwrite the winner's claim, so a contender
        # that then dies cannot leave the marker describing limits the ledger
        # never admitted.
        sid = self.new_session()
        self.admit(sid, "build=1")
        other = self.new_session()
        with mock.patch.object(state_store, "read_package_admission_marker",
                               return_value=None):
            with self.assertRaises(state_store.AllocationConflict) as c:
                self.admit(other, "build=2")
        self.assertEqual(c.exception.reason, "limits_immutable")
        marker = state_store.read_package_admission_marker(self.package)
        ledger = self.ledger()
        self.assertEqual(marker["limits_digest"], ledger["limits_digest"])
        self.assertEqual(ledger["limits"]["build"]["dispatches"], 1)

    def test_a_refused_admission_reconciles_the_marker_to_the_ledger(self):
        # If a marker is lost but its ledger survives, an admission under
        # different limits is refused AND leaves the marker agreeing with the
        # ledger rather than with the refused request.
        sid = self.new_session()
        self.admit(sid, "build=1")
        ledger_digest = self.ledger()["limits_digest"]
        os.unlink(state_store.package_admission_marker_path_for(self.package))
        with self.assertRaises(state_store.AllocationConflict) as c:
            self.admit(self.new_session(), "build=7")
        self.assertEqual(c.exception.reason, "limits_immutable")
        marker = state_store.read_package_admission_marker(self.package)
        self.assertEqual(marker["limits_digest"], ledger_digest)

    def test_a_genuinely_new_package_still_admits_normally(self):
        sid = self.new_session()
        self.assertIsNone(
            state_store.read_package_admission_marker(self.package))
        record = self.admit(sid, "build=2")
        self.assertEqual(record["consumed"]["build"], 0)
        self.assertIsNotNone(
            state_store.consume_dispatch_allocation(sid, BUILDING))


class SessionRebindTests(LedgerEnv):

    def test_a_bound_session_cannot_be_rebound_to_another_package(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        state_store.consume_dispatch_allocation(sid, BUILDING)
        with self.assertRaises(state_store.AllocationConflict) as c:
            self.admit(sid, "build=2,correction=1",
                       package=self.package + "-other")
        self.assertEqual(c.exception.reason, "session_rebind_refused")
        binding = state_store.read_dispatch_allocation_binding(sid)
        self.assertEqual(binding["package_ref"], self.package)
        self.assertEqual(self.ledger()["consumed"]["build"], 1)
        # And the spent slot is still spent.
        state_store.consume_dispatch_allocation(sid, BUILDING)
        with self.assertRaises(state_store.AllocationExhausted):
            state_store.consume_dispatch_allocation(sid, BUILDING)

    def test_a_bound_session_cannot_be_readmitted_with_other_limits(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        with self.assertRaises(state_store.AllocationConflict) as c:
            self.admit(sid, "build=9")
        self.assertEqual(c.exception.reason, "limits_immutable")

    def test_readmitting_the_identical_binding_is_idempotent(self):
        sid = self.new_session()
        first = self.admit(sid, "build=2,correction=1")
        state_store.consume_dispatch_allocation(sid, BUILDING)
        again = self.admit(sid, "build=2,correction=1")
        self.assertEqual(again["limits"], first["limits"])
        self.assertEqual(self.ledger()["consumed"]["build"], 1)


class AllocationStateTests(LedgerEnv):
    """`dispatch_allocation_state` is what a plain resume reads."""

    def test_it_reports_identity_and_current_counts(self):
        sid = self.new_session()
        self.admit(sid, "build=2,correction=1")
        state_store.consume_dispatch_allocation(sid, BUILDING)
        snapshot = state_store.dispatch_allocation_state(sid)
        self.assertEqual(snapshot["package_ref"], self.package)
        self.assertEqual(snapshot["consumed"]["build"], 1)
        self.assertEqual(snapshot["limits"]["build"],
                         {"dispatches": 2, "corrections": 1})

    def test_it_is_none_only_for_a_truly_unbound_session(self):
        self.assertIsNone(
            state_store.dispatch_allocation_state(self.new_session()))

    def test_it_raises_rather_than_reporting_an_unprovable_session(self):
        sid = self.new_session()
        self.admit(sid, "build=2")
        os.unlink(state_store.allocation_ledger_path_for(self.package))
        with self.assertRaises(state_store.AllocationConflict):
            state_store.dispatch_allocation_state(sid)


# --------------------------------------------------------------------------- #
# The transport gateway: one reserved unit can never pay for two real sends.  #
# --------------------------------------------------------------------------- #

class GatewayOneSendTests(unittest.TestCase):

    def _delivery(self):
        return handoff.direct_delivery(
            handoff._static_role_text("neutral bounded work"))

    def test_an_error_raised_inside_send_never_triggers_a_second_send(self):
        # A metadata-capable transport whose body raises TypeError/ValueError
        # AFTER the provider was handed the turn. The old shared `try` read
        # that as "this session does not accept meta" and re-sent.
        for error in (TypeError, ValueError, RuntimeError, KeyError):
            with self.subTest(error=error.__name__):
                class _RaisesAfterDispatch:
                    def __init__(self):
                        self.calls = 0

                    def send(self, text, meta=None):
                        self.calls += 1
                        raise error("failure after transport entry")

                session = _RaisesAfterDispatch()
                result = cowork._send(session, self._delivery(),
                                      meta={"fresh": True})
                self.assertIs(result["ok"], False)
                self.assertEqual(session.calls, 1)
                self.assertEqual(result["error_type"], error.__name__)

    def test_a_legacy_signature_session_still_receives_one_plain_send(self):
        class _Legacy:
            def __init__(self):
                self.calls = []

            def send(self, text):
                self.calls.append(str(text))
                return None

        session = _Legacy()
        result = cowork._send(session, self._delivery(), meta={"fresh": True})
        self.assertIs(result["ok"], True)
        self.assertEqual(len(session.calls), 1)

    def test_a_legacy_session_that_raises_is_not_retried(self):
        class _LegacyRaises:
            def __init__(self):
                self.calls = 0

            def send(self, text):
                self.calls += 1
                raise ValueError("legacy body failure")

        session = _LegacyRaises()
        result = cowork._send(session, self._delivery(), meta={"fresh": True})
        self.assertIs(result["ok"], False)
        self.assertEqual(session.calls, 1)

    def test_a_metadata_session_receives_the_meta_exactly_once(self):
        class _Meta:
            def __init__(self):
                self.metas = []

            def send(self, text, meta=None):
                self.metas.append(meta)
                return {"ok": True, "result": "ok"}

        session = _Meta()
        cowork._send(session, self._delivery(), meta={"fresh": True})
        self.assertEqual(len(session.metas), 1)
        self.assertEqual(session.metas[0]["fresh"], True)

    def test_an_unintrospectable_callable_falls_back_once(self):
        # `inspect.signature` raising is the ONLY thing allowed to choose the
        # legacy call shape, and it still produces exactly one send.
        class _Opaque:
            def __init__(self):
                self.calls = 0

            def send(self, *args, **kwargs):
                self.calls += 1
                return {"ok": True, "result": "ok"}

        session = _Opaque()
        with mock.patch.object(cowork.inspect, "signature",
                               side_effect=ValueError("no signature")):
            result = cowork._send(session, self._delivery(),
                                  meta={"fresh": True})
        self.assertIs(result["ok"], True)
        self.assertEqual(session.calls, 1)


# --------------------------------------------------------------------------- #
# Reviewer-only recovery: a resume that dispatches no lead is not refused by  #
# an exhausted LEAD budget, but the correction it may ask for still is.       #
# --------------------------------------------------------------------------- #

class ReviewerFirstRecoveryTests(LoopEnv):

    def _ready_candidate(self, phase=BUILDING):
        status_path = self.status_path(phase)
        os.makedirs(os.path.dirname(status_path), exist_ok=True)
        with open(status_path, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"turn": "neutral"}}, fh)
        with open(status_path, "rb") as fh:
            sha = __import__("hashlib").sha256(fh.read()).hexdigest()
        return status_path, sha

    def _binding(self, sha, phase=BUILDING):
        lead, reviewer = PHASE_ROLES[phase]
        return {"tier": "exact", "record": {"role": reviewer},
                "lead_role": lead, "reviewer_role": reviewer,
                "candidate_sha256": sha, "summary_sha256": None, "round": 1}

    def test_a_reviewer_only_resume_runs_with_a_fully_spent_budget(self):
        # The last permitted lead turn already produced this candidate; its
        # paired reviewer failed. Re-running only the reviewer costs no lead
        # dispatch, so a spent lead budget must not strand the work.
        self.admit("build=1")
        state_store.consume_dispatch_allocation(self.sid, BUILDING)
        self.assertEqual(self.ledger()["consumed"]["build"], 1)
        _status_path, sha = self._ready_candidate()
        outcome, _payload, sess = self.drive(
            BUILDING, [APPROVE],
            loop_kwargs={"reviewer_first": self._binding(sha)})
        self.assertEqual(sess.sent, [])          # zero lead dispatches
        self.assertEqual(outcome, "approved")    # the reviewer approved
        self.assertEqual(self.ledger()["consumed"]["build"], 1)

    def test_a_correction_that_reviewer_only_recovery_asks_for_is_refused(self):
        # If that reviewer then asks for a real lead correction, the gate
        # charges and refuses it at the one point a lead send is attempted.
        self.admit("build=1")
        state_store.consume_dispatch_allocation(self.sid, BUILDING)
        _status_path, sha = self._ready_candidate()
        outcome, payload, sess = self.drive(
            BUILDING, [REVISE, APPROVE],
            loop_kwargs={"reviewer_first": self._binding(sha)})
        self.assertEqual(sess.sent, [])
        self.assertEqual(outcome, "ended")
        self.assert_allocation_refusal(
            payload, attempted_ordinal=2, consumed_dispatches=1,
            refusal_code="dispatch_limit_exhausted")

    def test_the_lead_launch_screens_sit_after_the_recovery_route(self):
        # Structural: for planner and builder the screen must be inside an
        # `if recovery_route != "reviewer_first":` guard, and that guard must
        # come after the route is decided and before the run_*_fn call.
        source = open(cowork.__file__).read()
        for role, runner in (("planner", "run_planner_fn("),
                             ("builder", "run_builder_fn(")):
            with self.subTest(role=role):
                guard_at = source.index(
                    'ensure_controller_available("%s", reason="lead_launch")'
                    % role)
                route_at = source.index(
                    "recovery_route, recovery_detail = recovery_route_for(",
                    guard_at)
                runner_at = source.index(runner, route_at)
                window = source[route_at:runner_at]
                self.assertIn('if recovery_route != "reviewer_first":', window)
                self.assertIn("_allocation_preflight_stop(", window)
                # And no screen between the controller check and the route.
                self.assertNotIn("_allocation_preflight_stop(",
                                 source[guard_at:route_at])


# --------------------------------------------------------------------------- #
# No provider process, probe or spawn once the allocation is spent.           #
# --------------------------------------------------------------------------- #

class NoProviderAfterRefusalTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloc-noprov-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "sessions")
        patcher = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        prior = os.getcwd()
        self.addCleanup(os.chdir, prior)
        self.work = tempfile.mkdtemp(prefix="alloc-noprov-cwd-")
        self.addCleanup(shutil.rmtree, self.work, True)
        # `run_flow` refuses outside a git work tree before anything else, so
        # the fixture needs a real (throwaway, synthetic) repository.
        subprocess.run(["git", "init", "-q", self.work], check=True,
                       capture_output=True)
        os.chdir(self.work)
        self.package = "pkg-" + uuid.uuid4().hex[:8]
        self.sid = "ALLOC-NOPROV-S"

    def _saved_planning_session(self):
        spath = os.path.join(self.work, ".cowork", "session.json")
        state = state_store.ensure_session(spath, None, self.sid)
        team = ["scout", "scout-reviewer", "planner", cowork.PLANNING_ADVISOR]
        state = state_store.save_config(
            spath, team, cowork.default_config(team), prior=state)
        state = state_store.save_phase(spath, "planning", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "old-claude", prior=state)
        intel = os.path.join(state_store.session_assets_dir(self.sid),
                             "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "neutral"}}, fh)
        return spath

    def _exhaust(self, spec="planning=0"):
        os.makedirs(state_store.session_assets_dir(self.sid), exist_ok=True)
        state_store.admit_dispatch_allocation(
            self.sid, self.package, allocation.parse_spec(spec))

    def _run(self, argv, **kw):
        box = {}
        out = io.StringIO()
        for name in ("run_scout_fn", "run_planner_fn", "run_builder_fn",
                     "run_worktree_fn"):
            kw.setdefault(name, self._unscripted(name))
        rc = cowork.run_flow(
            cowork.build_parser().parse_args(argv), io_out=out,
            which=lambda c: "/bin/" + c, result_box=box, **kw)
        return rc, cowork.build_run_result(rc, box), out.getvalue()

    def _unscripted(self, name):
        def fail(*_a, **_k):
            raise AssertionError("unscripted %s reached" % name)
        return fail

    def test_a_controller_switch_probes_nothing_once_exhausted(self):
        # `apply_controller_update` runs a REAL `_claude_probe` spawn on a
        # cache miss. An exhausted package must refuse before that.
        spath = self._saved_planning_session()
        self._exhaust()
        probes = []
        spawns = []
        with mock.patch.object(cowork, "_claude_probe",
                               side_effect=lambda *a, **k: probes.append(k)), \
             mock.patch.object(bridge, "_real_claude_spawn",
                               side_effect=lambda *a, **k: spawns.append(k)):
            rc, result, _text = self._run(
                ["--session-file", spath,
                 "--switch-controller", "planner=codex"])
        self.assertEqual(rc, 1)
        self.assertEqual(probes, [])
        self.assertEqual(spawns, [])
        self.assertEqual(result["reason"], cowork.ALLOCATION_STOP_KIND)
        self.assertEqual(result["stop"]["kind"], cowork.ALLOCATION_STOP_KIND)
        self.assertEqual(result["stop"]["requires"], "operator")

    def test_a_plain_resume_refuses_before_reaching_any_lead_runner(self):
        spath = self._saved_planning_session()
        self._exhaust()
        rc, result, _text = self._run(["--session-file", spath])
        self.assertEqual(rc, 1)
        self.assertEqual(result["stop"]["kind"], cowork.ALLOCATION_STOP_KIND)
        self.assertEqual(result["stop"]["allocation_phase"], "planning")
        self.assertEqual(result["stop"]["attempted_ordinal"], 1)

    def test_the_screen_is_wired_before_the_controller_update(self):
        source = open(cowork.__file__).read()
        update_at = source.index("ok, message, rc_reject = apply_controller_update(")
        window = source[:update_at]
        screen_at = window.rindex("_allocation_preflight_stop(")
        self.assertIn('stage="pre_controller_update"',
                      source[screen_at:update_at])

    def test_the_screen_is_wired_before_the_worktree_role(self):
        source = open(cowork.__file__).read()
        wt_at = source.index("artifact = run_worktree_fn(")
        window = source[:wt_at]
        screen_at = window.rindex("_allocation_preflight_stop(")
        self.assertIn('stage="pre_worktree"', source[screen_at:wt_at])


# --------------------------------------------------------------------------- #
# A plain resume repeats no flags and must still report the binding.          #
# --------------------------------------------------------------------------- #

class TerminalRefusalTests(NoProviderAfterRefusalTests):
    """An exhausted allocation is terminal for that allocation: nothing in the
    orchestrator-facing surface can reopen or raise it."""

    def test_a_refusal_opens_no_decision_request(self):
        spath = self._saved_planning_session()
        self._exhaust()
        rc, result, _text = self._run(["--session-file", spath])
        self.assertEqual(rc, 1)
        self.assertNotIn("decision_argv", result)
        self.assertIsNone(
            state_store.read_decision_request(self.sid))

    def test_an_answer_cannot_reopen_a_refused_allocation(self):
        spath = self._saved_planning_session()
        self._exhaust()
        rc, _result, _text = self._run(["--session-file", spath])
        self.assertEqual(rc, 1)
        # There is nothing to answer, so an --answer naming anything at all
        # is refused as an invalid invocation rather than buying budget.
        rc2, result2, _text2 = self._run(
            ["--session-file", spath, "--answer", "0" * 64,
             "--context", "please continue"])
        self.assertEqual(rc2, 2)
        self.assertEqual(result2["reason"], "decision_no_open_request")
        self.assertEqual(
            state_store.read_dispatch_allocation_ledger(
                self.package)["consumed"]["planning"], 0)

    def test_repeating_the_refused_run_never_raises_the_limit(self):
        spath = self._saved_planning_session()
        self._exhaust("planning=1")
        state_store.consume_dispatch_allocation(self.sid, PLANNING)
        for _ in range(3):
            rc, result, _text = self._run(["--session-file", spath])
            self.assertEqual(rc, 1)
            self.assertEqual(result["stop"]["limit_dispatches"], 1)
            self.assertEqual(result["stop"]["consumed_dispatches"], 1)
        ledger = state_store.read_dispatch_allocation_ledger(self.package)
        self.assertEqual(ledger["limits"]["planning"]["dispatches"], 1)
        self.assertEqual(ledger["consumed"]["planning"], 1)

    def test_renaming_the_package_is_refused_rather_than_re_granted(self):
        spath = self._saved_planning_session()
        self._exhaust("planning=1")
        state_store.consume_dispatch_allocation(self.sid, PLANNING)
        rc, result, _text = self._run(
            ["--session-file", spath,
             "--package-ref", self.package + "-v2",
             "--dispatch-allocation", "planning=9"])
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason"], "allocation_session_rebind_refused")


class ControllerSwitchScreenTests(NoProviderAfterRefusalTests):
    """The pre-update screen has no reviewer-first exception, so a malformed
    or stale recovery record cannot buy a real provider probe."""

    def _record_failed_reviewer(self, spath, failed_turn):
        saved = state_store.load(spath)
        saved["pending_switches"] = {
            cowork.PLANNING_ADVISOR: {"failed_turn": failed_turn,
                                      "pending_turn": "synthetic retry"}}
        state_store.save(spath, saved)

    def test_a_malformed_recovery_record_cannot_probe_a_spent_lead(self):
        spath = self._saved_planning_session()
        self._record_failed_reviewer(spath, {"schema": 1})
        self._exhaust()
        with mock.patch.object(cowork, "_claude_probe",
                               return_value=(True, None, None)) as probe:
            rc, result, _text = self._run(
                ["--session-file", spath,
                 "--switch-controller", "planner=codex"])
        self.assertEqual(probe.call_count, 0)
        self.assertEqual(rc, 1)
        self.assertEqual(result["stop"]["kind"], cowork.ALLOCATION_STOP_KIND)

    def test_a_well_formed_recovery_record_also_does_not_probe(self):
        spath = self._saved_planning_session()
        self._record_failed_reviewer(
            spath, {"schema": 1, "role": cowork.PLANNING_ADVISOR,
                    "phase": "planning", "round": 1})
        self._exhaust()
        with mock.patch.object(cowork, "_claude_probe",
                               return_value=(True, None, None)) as probe:
            rc, _result, _text = self._run(
                ["--session-file", spath,
                 "--switch-controller", "planner=codex"])
        self.assertEqual(probe.call_count, 0)
        self.assertEqual(rc, 1)

    def test_the_screen_is_scoped_to_the_lead_roles_it_targets(self):
        # Structural: the screen iterates the proposal's own mappings and
        # skips any role that is not a phase lead, rather than guessing a
        # recovery route from the presence of a failed-turn record.
        source = open(cowork.__file__).read()
        update_at = source.index(
            "ok, message, rc_reject = apply_controller_update(")
        screen_at = source[:update_at].rindex("_allocation_preflight_stop(")
        guard_at = source[:screen_at].rindex("if proposal is not None")
        window = source[guard_at:update_at]
        self.assertIn("proposal.mappings", window)
        self.assertIn("_ALLOCATION_PHASE_BY_ROLE.get(_update_role)", window)
        self.assertNotIn("failed_turn_present", window)

    def test_switching_only_the_reviewer_is_not_screened(self):
        # A reviewer-only switch is legitimate on a spent lead budget: the
        # reviewer turn is never charged and no lead dispatches.
        spath = self._saved_planning_session()
        self._record_failed_reviewer(
            spath, {"schema": 1, "role": cowork.PLANNING_ADVISOR,
                    "phase": "planning", "round": 1})
        self._exhaust()
        stop = cowork._allocation_preflight_stop(
            self.sid, "planning", "planner", trace=None)
        self.assertIsNotNone(stop)   # the LEAD would be refused
        # ... but the reviewer is not a lead role at all, so the screen's own
        # scoping skips it.
        self.assertNotIn(cowork.PLANNING_ADVISOR,
                         cowork._ALLOCATION_PHASE_BY_ROLE)


class ResumeHydrationTests(NoProviderAfterRefusalTests):

    def test_a_plain_resume_reports_identity_and_current_counts(self):
        spath = self._saved_planning_session()
        self._exhaust("planning=2")
        state_store.consume_dispatch_allocation(self.sid, PLANNING)

        def fake_planner(config, context, selected, on_outcome=None, **kw):
            if on_outcome:
                on_outcome("approved", None)
            return 0

        # The official resume_argv -- no --package-ref, no
        # --dispatch-allocation.
        rc, result, _text = self._run(
            ["--session-file", spath], run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        record = result["dispatch_allocation"]
        self.assertEqual(record["package_ref"], self.package)
        self.assertEqual(record["limits"], "planning=2")
        self.assertEqual(record["consumed"]["planning"], 1)
        self.assertNotIn("consumed_read_failed", record)

    def test_the_result_reports_counts_this_run_actually_charged(self):
        spath = self._saved_planning_session()
        self._exhaust("planning=2")

        def fake_planner(config, context, selected, on_outcome=None, **kw):
            # Stand in for the lead loop's own charge.
            state_store.consume_dispatch_allocation(self.sid, PLANNING)
            if on_outcome:
                on_outcome("approved", None)
            return 0

        rc, result, _text = self._run(
            ["--session-file", spath], run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        # Hydration happened before the phase ran; the result line must show
        # the post-charge count, not the pre-charge snapshot.
        self.assertEqual(
            result["dispatch_allocation"]["consumed"]["planning"], 1)

    def test_an_unreadable_ledger_never_reports_stale_counts_as_current(self):
        record = cowork.build_run_result(1, {
            "last": {"outcome": "ended", "payload": None},
            "session_uuid": "ALLOC-ABSENT-S",
            "dispatch_allocation": {"package_ref": self.package,
                                    "limits": "planning=2",
                                    "consumed": {"planning": 1}},
        })["dispatch_allocation"]
        self.assertIsNone(record["consumed"])
        self.assertIs(record["consumed_read_failed"], True)

    def test_an_unbound_resume_reports_no_allocation_at_all(self):
        spath = self._saved_planning_session()

        def fake_planner(config, context, selected, on_outcome=None, **kw):
            if on_outcome:
                on_outcome("approved", None)
            return 0

        rc, result, _text = self._run(
            ["--session-file", spath], run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        self.assertNotIn("dispatch_allocation", result)

    def test_a_bound_session_whose_state_is_lost_refuses_the_whole_run(self):
        spath = self._saved_planning_session()
        self._exhaust("planning=2")
        os.unlink(state_store.allocation_ledger_path_for(self.package))
        rc, result, _text = self._run(["--session-file", spath])
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason"], "allocation_ledger_missing")


# --------------------------------------------------------------------------- #
# Admission happens under the acquired owner lease.                           #
# --------------------------------------------------------------------------- #

class AdmissionOwnershipTests(unittest.TestCase):

    def test_admission_is_wired_inside_the_owned_region(self):
        # Structural, because the hazard is an ORDERING one: a losing
        # concurrent invocation must not be able to rewrite the active
        # owner's binding on its way to an owner-conflict refusal.
        source = open(cowork.__file__).read()
        acquire_at = source.index(
            "owner_lease = cowork_owner.acquire_owner_lease(")
        # The run_flow call sites are the FIRST of each AFTER the lease is
        # acquired (`build_run_result`, defined earlier in the file, also
        # reads the state -- read-only, and not an admission).
        admit_at = source.index(
            "state_store.admit_dispatch_allocation(", acquire_at)
        hydrate_at = source.index(
            "state_store.dispatch_allocation_state(", acquire_at)
        self.assertLess(acquire_at, admit_at)
        self.assertLess(acquire_at, hydrate_at)
        # Nothing admits or re-binds BEFORE the lease is acquired.
        self.assertNotIn("state_store.admit_dispatch_allocation(",
                         source[source.index("def run_flow("):acquire_at])
        # And inside the try whose finally releases the lease.
        owned_at = source.index(
            "# Graph vertex bind/fence (#75): the FIRST statement")
        self.assertLess(owned_at, admit_at)

    def test_the_capacity_replay_consumes_before_constructing_a_controller(self):
        source = open(cowork.__file__).read()
        consume_at = source.index(
            "state_store.consume_dispatch_allocation(\n                    session_uuid, _wake_phase)")
        ctor_at = source.index("sessions_dir = state_store.session_assets_dir(session_uuid)",
                               consume_at)
        send_at = source.index('meta={"prompt_kind": "resume_wake"}', consume_at)
        self.assertLess(consume_at, ctor_at)
        self.assertLess(ctor_at, send_at)


class SeparationTests(unittest.TestCase):

    def test_allocation_policy_is_not_in_the_authentication_module(self):
        import cowork_profiles
        source = open(cowork_profiles.__file__).read()
        for token in ("dispatch_allocation", "package_ref",
                      "AllocationExhausted"):
            self.assertNotIn(token, source)

    def test_the_pure_module_imports_nothing_from_this_repo(self):
        source = open(allocation.__file__).read()
        self.assertNotIn("import cowork_", source)

    def test_the_verification_worker_closure_does_not_grow(self):
        # The owned-verification worker execs a captured `cowork_verification`
        # and imports `cowork_state` from alongside it, with only its
        # historical sibling modules present. The allocation store lives in
        # `cowork_state` but must not add itself to that closure, so its
        # `cowork_allocation` import is lazy. Proven by importing the worker
        # entry point from a throwaway directory holding ONLY those siblings.
        import shutil as _shutil
        import subprocess
        here = os.path.dirname(os.path.abspath(state_store.__file__))
        scratch = tempfile.mkdtemp(prefix="alloc-closure-")
        self.addCleanup(_shutil.rmtree, scratch, True)
        for name in ("cowork_verification.py", "cowork_state.py",
                     "cowork_policy.py", "cowork_ledger.py"):
            _shutil.copyfile(os.path.join(here, name),
                             os.path.join(scratch, name))
        self.assertFalse(
            os.path.exists(os.path.join(scratch, "cowork_allocation.py")))
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import cowork_verification"
             % scratch],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])

    def test_the_allocation_store_fails_closed_without_its_policy_module(self):
        # And when the lazy import genuinely cannot resolve, the caller sees
        # an error rather than an unbounded allocation.
        with mock.patch.dict(sys.modules, {"cowork_allocation": None}):
            with self.assertRaises(ImportError):
                state_store._allocation()

    def test_capacity_and_allocation_share_no_record(self):
        import cowork_capacity_scheduler
        source = open(cowork_capacity_scheduler.__file__).read()
        self.assertNotIn("dispatch_allocation", source)


if __name__ == "__main__":
    unittest.main()
