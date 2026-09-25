#!/usr/bin/env python3
"""Focused tests for durable role-status milestones (garusis/cowork-internal
#27): the RoleStatusMilestone contract and liveness classifier
(`cowork_activity`), the append-only per-role store (`cowork_state`), the
ToolActivityTrace tool-end hook (`cowork_bridge`), and the `_role_loop`
tracker + recovery diagnostics (`cowork`).

Run standalone:

    python3 -m unittest scripts.test_cowork_status_milestones -v
"""

import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_activity as activity  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_state as state_store  # noqa: E402


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _now_dt():
    return datetime.datetime.now(datetime.timezone.utc)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh)


def _write_status(path, status, **extra):
    result = {}
    if status == "needs_input":
        result["pending_question"] = "Which option?"
    _write_json(path, dict({"session": "S", "role": "builder",
                            "status": status, "result": result}, **extra))


def _milestone_record(role="builder", round_=1, milestone="started",
                      boundary="turn_start", **overrides):
    rec = {
        "schema_version": 1, "record": "RoleStatusMilestone", "role": role,
        "round": round_, "milestone": milestone, "boundary": boundary,
        "status_sha256": None, "recorded_at": _iso(_now_dt()),
        "work_id": None,
    }
    rec.update(overrides)
    return rec


def _activity_record(work_id, time_iso, activity_class="productive_model_work"):
    return {
        "schema_version": 1, "record": "ActivityRecord", "work_id": work_id,
        "time": time_iso, "activity_class": activity_class,
        "source": "claude", "artifact_fingerprint": None,
        "artifact_delta": [], "provider_health": None, "age_seconds": 0.0,
    }


class FakeTrace:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))
        return str(uuid.uuid4())

    def named(self, name):
        return [fields for event, fields in self.events if event == name]


class FakeSession:
    """Scripted controller session. Each turn is a dict: `steps` (callables,
    each followed by a tool-end boundary through the hook `_role_loop`
    installs), `final` (a callable run with no boundary after it), `result`
    (the send result), `sleep` (seconds)."""

    controller = "claude"

    def __init__(self, turns, uuid_, role):
        self.turns = list(turns)
        self.uuid = uuid_
        self.role = role
        self.sends = 0
        self.tool_boundary_hook = None
        self.snapshots = []
        self.closed = False

    def _current_round(self):
        records = state_store.read_status_milestones(self.uuid, self.role)
        if not records:
            return []
        current = max(r["round"] for r in records)
        return [r["milestone"] for r in records if r["round"] == current]

    def send(self, text):
        self.sends += 1
        turn = self.turns.pop(0) if self.turns else {}
        for step in turn.get("steps", []):
            step()
            if self.tool_boundary_hook:
                self.tool_boundary_hook()
            self.snapshots.append(self._current_round())
        if turn.get("sleep"):
            time.sleep(turn["sleep"])
        if turn.get("final"):
            turn["final"]()
        return turn.get("result")

    def close(self):
        self.closed = True


class _EnvMixin:
    """Isolated COWORK_SESSIONS_ROOT and a temp git repo as the cwd."""

    def setUp(self):
        super().setUp()
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._old_cwd = os.getcwd()
        self.addCleanup(self._restore)
        self._fresh_env()
        for name, value in (
                ("_run_owned_verification_transaction", (None, None)),
                ("_record_readiness_from_transaction",
                 {"state": "verified", "reason": None, "event_id": None}),
                ("_update_receipt_pointer_for_readiness", None)):
            patcher = mock.patch.object(cowork, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fresh_env(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda root=self.root: shutil.rmtree(
            root, ignore_errors=True))
        os.environ["COWORK_SESSIONS_ROOT"] = os.path.join(self.root, "sessions")
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        for argv in (["git", "init", "-q"],
                     ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "t"]):
            subprocess.run(argv, cwd=self.repo, check=True)
        with open(os.path.join(self.repo, "base.txt"), "w") as fh:
            fh.write("base\n")
        subprocess.run(["git", "add", "base.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q",
                        "-m", "base"], cwd=self.repo, check=True)
        os.chdir(self.repo)
        self.uuid = "11111111-2222-3333-4444-555555555555"
        self.work_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.art = os.path.join(self.root, "artifacts")
        os.makedirs(self.art)
        self.status_path = os.path.join(self.art, "builder.status.json")
        self.summary_path = os.path.join(self.art, "builder.summary.md")

    def _restore(self):
        os.chdir(self._old_cwd)
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root

    # step builders ------------------------------------------------------
    def edit(self, name="a.txt", content="edit\n"):
        def step():
            with open(os.path.join(self.repo, name), "w") as fh:
                fh.write(content)
        return step

    def summary(self, text="# Build summary\n"):
        def step():
            with open(self.summary_path, "w") as fh:
                fh.write(text)
        return step

    def status(self, value, path=None):
        def step():
            _write_status(path or self.status_path, value)
        return step

    def run_loop(self, turns, role="builder", review_fn=None, trace=None,
                 summary=True, is_resume=False, status_path=None,
                 controller="claude"):
        sess = FakeSession(turns, self.uuid, role)
        sess.controller = controller
        rc, outcome, payload = cowork._role_loop(
            sess, "seed", status_path or self.status_path, context="",
            io_out=io.StringIO(), role=role, review_fn=review_fn,
            trace=trace, session_uuid=self.uuid,
            build_summary_path=self.summary_path if summary else None,
            role_work_id=self.work_id, is_resume=is_resume)
        return sess, outcome, payload

    def records(self, role="builder"):
        return state_store.read_status_milestones(self.uuid, role)

    def pairs(self, round_=None, role="builder"):
        return [(r["milestone"], r["boundary"]) for r in self.records(role)
                if round_ is None or r["round"] == round_]


def _approve(_path, _round):
    return {"verdict": "approve"}


# --------------------------------------------------------------------------- #
# Criterion 1: a long builder turn advances at real boundaries.              #
# --------------------------------------------------------------------------- #


class MilestoneAdvanceTests(_EnvMixin, unittest.TestCase):

    def test_scenario_a_tool_boundaries(self):
        sess, outcome, _payload = self.run_loop(
            [{"steps": [self.edit(), self.summary(),
                        self.status("ready_for_review")]}],
            review_fn=_approve)
        self.assertEqual(outcome, "approved")
        self.assertEqual(sess.snapshots[0], ["started", "implementation_started"])
        self.assertEqual(sess.snapshots[1], ["started", "implementation_started",
                                             "self_audit_started"])
        self.assertEqual(sess.snapshots[2], sess.snapshots[1])
        self.assertEqual(self.pairs(), [
            ("started", "turn_start"),
            ("implementation_started", "tool_end"),
            ("self_audit_started", "tool_end"),
            ("waiting_on_orchestration", "turn_end"),
        ])

    def test_scenario_b_same_final_boundary(self):
        def everything():
            self.edit()()
            self.summary()()
            self.status("ready_for_review")()
        self.run_loop([{"steps": [everything]}], review_fn=_approve)
        self.assertEqual(self.pairs(), [
            ("started", "turn_start"),
            ("implementation_started", "tool_end"),
            ("self_audit_started", "tool_end"),
            ("waiting_on_orchestration", "turn_end"),
        ])

    def test_last_tool_never_closes(self):
        # Everything lands after the last observed tool end: the turn-end pass
        # records the same ordered sequence.
        def everything():
            self.edit()()
            self.summary()()
            self.status("ready_for_review")()
        self.run_loop([{"final": everything}], review_fn=_approve)
        self.assertEqual(self.pairs(), [
            ("started", "turn_start"),
            ("implementation_started", "turn_end"),
            ("self_audit_started", "turn_end"),
            ("waiting_on_orchestration", "turn_end"),
        ])

    def test_scenario_c_no_summary_path(self):
        self.run_loop([{"steps": [self.edit(), self.summary(),
                                  self.status("ready_for_review")]}],
                      review_fn=_approve, summary=False)
        self.assertEqual([m for m, _ in self.pairs()],
                         ["started", "implementation_started",
                          "waiting_on_orchestration"])

    def test_hook_without_trace_handle(self):
        calls = []
        tat = bridge.ToolActivityTrace(None, "claude", "builder",
                                       on_tool_end=lambda: calls.append(1))
        tat.end()
        self.assertEqual(calls, [])
        tat.start({"name": "Edit"})
        tat.end()
        self.assertEqual(calls, [1])

        def boom():
            raise RuntimeError("hook failure")
        raising = bridge.ToolActivityTrace(None, "claude", "builder",
                                           on_tool_end=boom)
        raising.observe({"kind": "tool", "name": "Edit"})
        raising.observe({"kind": "tool_done"})
        self.assertIsNone(raising.open_tool)

    def test_reopened_round_dirty_tree(self):
        verdicts = [{"verdict": "revise", "findings": ["fix it"]},
                    {"verdict": "approve"}]
        self.run_loop(
            [{"steps": [self.edit("a.txt", "one\n"),
                        self.status("ready_for_review")]},
             {"steps": [self.edit("a.txt", "second edit\n"),
                        self.status("ready_for_review")]}],
            review_fn=lambda _p, _r: verdicts.pop(0), summary=False)
        self.assertEqual([m for m, _ in self.pairs(round_=2)],
                         ["started", "implementation_started",
                          "waiting_on_orchestration"])

    def test_scout_discovery_complete(self):
        scout_status = os.path.join(self.art, "scout.status.json")
        self.run_loop([{"steps": [self.status("ready_for_review",
                                              path=scout_status)]}],
                      role="scout", review_fn=_approve,
                      status_path=scout_status)
        self.assertEqual(self.pairs(role="scout"), [
            ("started", "turn_start"),
            ("discovery_complete", "tool_end"),
            ("waiting_on_orchestration", "turn_end"),
        ])

    def test_failed_send_in_reopened_round_records_no_waiting(self):
        verdicts = [{"verdict": "revise", "findings": ["fix it"]}]
        _sess, outcome, payload = self.run_loop(
            [{"steps": [self.edit(), self.status("ready_for_review")]},
             {"result": {"ok": False, "result": "error"}}],
            review_fn=lambda _p, _r: verdicts.pop(0), summary=False,
            controller="fake")
        self.assertEqual(state_store.read_status(self.status_path),
                         "needs_input")
        self.assertEqual(outcome, "ended")
        self.assertEqual(self.pairs(round_=2), [("started", "turn_start")])
        self.assertEqual(payload["status_diagnostics"]["milestone"], "started")


# --------------------------------------------------------------------------- #
# Criterion 2: fingerprint + age are durable and visible in diagnostics.     #
# --------------------------------------------------------------------------- #


_DIAG_KEYS = ("milestone", "status_sha256", "status_age_s",
              "controller_output_age_s", "status_liveness")


class MilestoneDiagnosticsTests(_EnvMixin, unittest.TestCase):

    def test_record_carries_status_sha_and_recorded_at(self):
        self.run_loop([{"steps": [self.edit(),
                                  self.status("ready_for_review")]}],
                      review_fn=_approve, summary=False)
        with open(self.status_path, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        waiting = self.records()[-1]
        self.assertEqual(waiting["milestone"], "waiting_on_orchestration")
        self.assertEqual(waiting["status_sha256"], sha)
        self.assertEqual(waiting["work_id"], self.work_id)
        activity._check_rfc3339(waiting["recorded_at"], "recorded_at")
        self.assertIsNone(self.records()[0]["status_sha256"])

    def test_trace_event_matches_record(self):
        trace = FakeTrace()
        self.run_loop([{"steps": [self.edit(), self.summary(),
                                  self.status("ready_for_review")]}],
                      review_fn=_approve, trace=trace)
        events = [(e["round"], e["milestone"], e["boundary"], e["status_sha256"])
                  for e in trace.named("role.status_milestone")]
        stored = [(r["round"], r["milestone"], r["boundary"], r["status_sha256"])
                  for r in self.records()]
        self.assertEqual(events, stored)
        self.assertEqual(len(events), 4)

    def test_stop_payload_carries_diagnostics(self):
        trace = FakeTrace()
        _sess, outcome, payload = self.run_loop(
            [{"steps": [self.status("needs_input")]}], trace=trace)
        self.assertEqual(outcome, "stopped")
        diag = payload["status_diagnostics"]
        for key in _DIAG_KEYS:
            self.assertIn(key, diag)
        self.assertEqual(diag["milestone"], "waiting_on_orchestration")
        self.assertEqual(diag["status_liveness"], "fresh")
        self.assertEqual(trace.named("role.status_diagnostics")[0],
                         dict(diag, role="builder"))

    def test_failed_role_payload_carries_diagnostics(self):
        _sess, outcome, payload = self.run_loop(
            [{"result": {"ok": False, "result": "error"}}], controller="fake")
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "controller_failure")
        diag = payload["status_diagnostics"]
        for key in _DIAG_KEYS:
            self.assertIn(key, diag)
        self.assertEqual(diag["milestone"], "started")

    def test_resume_event_carries_diagnostics(self):
        state_store.append_status_milestone(
            self.uuid, _milestone_record(milestone="implementation_started",
                                         boundary="tool_end"))
        trace = FakeTrace()
        self.run_loop([{"steps": [self.status("needs_input")]}],
                      trace=trace, is_resume=True)
        resume, = trace.named("role.status_milestone.resume")
        self.assertEqual(resume["milestone"], "implementation_started")
        self.assertEqual(resume["round"], 1)
        # the resumed send opened a new durable round
        self.assertEqual(self.records()[1]["round"], 2)

    def test_resume_diagnostic_fault_never_blocks_the_turn(self):
        trace = FakeTrace()
        with mock.patch.object(
                state_store, "latest_status_milestone",
                side_effect=RuntimeError("diagnostic fault")):
            sess, outcome, payload = self.run_loop(
                [{"steps": [self.status("needs_input")]}],
                trace=trace, is_resume=True)
        self.assertEqual(sess.sends, 1)
        self.assertTrue(sess.closed)
        self.assertEqual(outcome, "stopped")
        self.assertEqual(payload["status_diagnostics"]["status_liveness"],
                         "unknown")
        resume, = trace.named("role.status_milestone.resume")
        self.assertEqual(resume["status_liveness"], "unknown")

    def test_terminal_payload_diagnostic_fault_never_replaces_failure(self):
        with mock.patch.object(
                activity, "classify_status_liveness",
                side_effect=RuntimeError("diagnostic fault")):
            sess, outcome, payload = self.run_loop(
                [{"result": {"ok": False, "result": "controller error"}}],
                controller="fake")
        self.assertTrue(sess.closed)
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "controller_failure")
        self.assertEqual(payload["status_diagnostics"]["status_liveness"],
                         "unknown")

    def _stale_setup(self, output_age_s):
        now = _now_dt()
        _write_status(self.status_path, "ready_for_review")
        old = (now - datetime.timedelta(seconds=2000)).timestamp()
        os.utime(self.status_path, (old, old))
        state_store.append_status_milestone(self.uuid, _milestone_record(
            recorded_at=_iso(now - datetime.timedelta(seconds=2000))))
        state_store.append_activity_record(self.uuid, _activity_record(
            self.work_id, _iso(now - datetime.timedelta(seconds=output_age_s))))
        # A later liveness tick (provider_wait) must not mask silence.
        state_store.append_activity_record(self.uuid, _activity_record(
            self.work_id, _iso(now), activity_class="provider_wait"))
        return cowork._status_milestone_diagnostics(
            self.uuid, "builder", self.status_path, self.work_id,
            now=_iso(now))

    def test_liveness_active_controller_stale_status(self):
        diag = self._stale_setup(output_age_s=10)
        self.assertGreater(diag["status_age_s"], 900)
        self.assertLess(diag["controller_output_age_s"], 300)
        self.assertEqual(diag["status_liveness"],
                         "stale_status_active_controller")

    def test_liveness_silent_controller(self):
        diag = self._stale_setup(output_age_s=1000)
        self.assertEqual(diag["status_liveness"], "inactive_controller")
        self.assertNotEqual(diag["status_liveness"],
                            "stale_status_active_controller")

    def test_classify_status_liveness_boundaries(self):
        classify = activity.classify_status_liveness
        self.assertEqual(classify(0, None), "unknown")
        self.assertEqual(classify(0, "x"), "unknown")
        self.assertEqual(classify(0, -1), "unknown")
        self.assertEqual(classify(0, 300.0), "fresh")
        self.assertEqual(classify(0, 300.1), "inactive_controller")
        self.assertEqual(classify(None, 0), "stale_status_active_controller")
        self.assertEqual(classify(900.0, 0), "fresh")
        self.assertEqual(classify(900.1, 0), "stale_status_active_controller")
        self.assertEqual(classify(5000, 5000), "inactive_controller")


# --------------------------------------------------------------------------- #
# Criterion 3: bounded, deterministic, no extra turns, no timer writes.      #
# --------------------------------------------------------------------------- #


class MilestoneBoundednessTests(_EnvMixin, unittest.TestCase):

    def _scenario_a(self):
        self.run_loop([{"steps": [self.edit(), self.summary(),
                                  self.status("ready_for_review")]}],
                      review_fn=_approve)
        return [{k: v for k, v in r.items() if k != "recorded_at"}
                for r in self.records()]

    def test_replay_is_deterministic(self):
        first = self._scenario_a()
        os.chdir(self._old_cwd)
        self._fresh_env()
        second = self._scenario_a()
        self.assertEqual(len(first), 4)
        self.assertEqual(first, second)

    def test_no_new_evidence_no_new_records(self):
        tracker = cowork._RoleStatusMilestones(
            self.uuid, "builder", self.status_path, self.summary_path,
            work_id=self.work_id)
        tracker.begin_round()
        self.edit()()
        tracker.on_tool_end()
        path = state_store.status_milestones_path_for(self.uuid, "builder")
        with open(path, "rb") as fh:
            before = fh.read()
        for _ in range(5):
            tracker.on_tool_end()
            tracker.on_turn_end(send_ok=True)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        for milestone in ("implementation_started", "started"):
            with self.assertRaises(ValueError):
                state_store.append_status_milestone(
                    self.uuid, _milestone_record(milestone=milestone))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual([r["milestone"] for r in self.records()],
                         ["started", "implementation_started"])

    def test_send_count_is_one_per_turn(self):
        sess, _outcome, _payload = self.run_loop(
            [{"steps": [self.edit(), self.summary(),
                        self.status("ready_for_review")]}],
            review_fn=_approve)
        self.assertEqual(sess.sends, 1)

    def test_tick_never_writes_milestones(self):
        with mock.patch.object(cowork, "_ACTIVITY_TICK_INTERVAL_SECONDS", 0.01):
            self.run_loop([{"sleep": 0.2,
                            "final": self.status("ready_for_review")}],
                          review_fn=_approve)
        history = state_store.read_activity_history(self.uuid, self.work_id)
        ticks = [r for r in history if r["activity_class"] == "provider_wait"]
        self.assertGreater(len(ticks), 0)
        self.assertEqual(self.pairs(), [
            ("started", "turn_start"),
            ("waiting_on_orchestration", "turn_end"),
        ])


# --------------------------------------------------------------------------- #
# Criterion 4: legacy sessions + record validation.                          #
# --------------------------------------------------------------------------- #


class MilestoneCompatibilityTests(_EnvMixin, unittest.TestCase):

    def test_legacy_session_without_store(self):
        self.assertEqual(state_store.read_status_milestones(self.uuid, "builder"), [])
        self.assertIsNone(state_store.latest_status_milestone(self.uuid, "builder"))
        self.assertEqual(state_store.next_status_milestone_round(
            self.uuid, "builder"), 1)
        diag = cowork._status_milestone_diagnostics(
            self.uuid, "builder", self.status_path, self.work_id)
        self.assertIsNone(diag["milestone"])
        self.assertIsNone(diag["round"])
        self.assertEqual(diag["status_liveness"], "unknown")
        trace = FakeTrace()
        self.run_loop([{"steps": [self.status("needs_input")]}],
                      trace=trace, is_resume=True)
        resume, = trace.named("role.status_milestone.resume")
        self.assertIsNone(resume["milestone"])

    def test_torn_store_reads_tolerantly(self):
        state_store.append_status_milestone(self.uuid, _milestone_record())
        path = state_store.status_milestones_path_for(self.uuid, "builder")
        with open(path, "a") as fh:
            fh.write('{"record": "RoleStatusMilestone", "round": 1\n{"torn')
        self.assertEqual([r["milestone"] for r in self.records()], ["started"])

    def test_validator_rejects_bad_records(self):
        validate = activity.validate_role_status_milestone_record
        self.assertEqual(validate(_milestone_record())["milestone"], "started")
        for bad in (_milestone_record(milestone="coding"),
                    dict(_milestone_record(), extra=1),
                    _milestone_record(round_=0),
                    _milestone_record(boundary="timer"),
                    _milestone_record(status_sha256="abc")):
            with self.assertRaises(ValueError):
                validate(bad)


if __name__ == "__main__":
    unittest.main()
