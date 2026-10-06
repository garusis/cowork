#!/usr/bin/env python3
"""Exact-role recovery: after a paired reviewer (planning-advisor or
build-reviewer) fails, a plain resume or a `--switch-controller` of that
reviewer resumes the REVIEWER first, against the exact candidate it was
judging, and the completed lead receives no send.

Layers, all offline with fake sessions and synthetic candidates:

  - state: the additive `failed_turn` record (save / read / retire / carry
    across a controller switch) in `cowork_state`;
  - loop: `_role_loop` with a `reviewer_first` binding, the writer callback at
    the `reviewer_unavailable` stop, and the genuine-revise return to the lead;
  - flow: production `run_flow` with the real `run_planner` / `run_builder`
    and fake sessions, covering routing, holds, linked reopen reasons, the
    supervisor exit from a stop, and the unchanged fresh / lead-failure paths.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_exact_role_recovery
"""

import functools
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

PLANNER = "planner"
BUILDER = "builder"
ADVISOR = cowork.PLANNING_ADVISOR
BUILD_REVIEWER = cowork.BUILD_REVIEWER
RECOVERY_KIND = "recovery_binding_mismatch"

CASES = (
    {"lead": PLANNER, "reviewer": ADVISOR, "phase": "planning"},
    {"lead": BUILDER, "reviewer": BUILD_REVIEWER, "phase": "building"},
)


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(doc, fh)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def make_record(reviewer=ADVISOR, lead=PLANNER, phase="planning", **overrides):
    """A schema-1 failed-turn record with neutral synthetic identities."""
    text = ("Review the completed %s candidate at /x/status.json (sha256 %s), "
            "write the verdict to /x/review.json, review round 1."
            % (phase, "a" * 64))
    record = {
        "schema": state_store.FAILED_TURN_SCHEMA,
        "role": reviewer, "lead_role": lead, "phase": phase,
        "request": {"sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "text": text},
        "candidate": {"path": "/x/status.json", "sha256": "a" * 64},
        "finding": {"path": "/x/review.json", "sha256": None,
                    "ledger_ids": []},
        "lead": {"controller": "codex", "provider_session_id": "lead-1",
                 "work_id": "W-1", "candidate_manifest_digest": "b" * 64,
                 "status": "ready_for_review"},
        "session_uuid": "S-1",
        "invalidation_seq_at_failure": 0, "round": 1, "failures": 2,
        "context_revision": 1, "epoch": 0, "created": 0.0,
    }
    record.update(overrides)
    return record


class _SessionsRootCase(unittest.TestCase):
    """An isolated sessions root and a scratch directory per test."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        patch = mock.patch.dict(os.environ,
                                {"COWORK_SESSIONS_ROOT": self.root})
        patch.start()
        self.addCleanup(patch.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def session_file(self, uuid_text="S-1", team=(PLANNER, ADVISOR)):
        path = os.path.join(self.dir, ".cowork", "session.json")
        state = state_store.ensure_session(path, None, uuid_text)
        config = cowork.default_config(list(team))
        state_store.save_config(path, list(team), config, prior=state)
        return path


# --------------------------------------------------------------------------- #
# State: the additive failed-turn record.                                      #
# --------------------------------------------------------------------------- #

class FailedTurnStateTests(_SessionsRootCase):
    def test_record_round_trips_through_disk_with_its_request_text(self):
        path = self.session_file()
        record = make_record()
        state_store.save_failed_turn(path, ADVISOR, record,
                                     prior=state_store.load(path))
        reloaded = state_store.load(path)
        self.assertEqual(state_store.read_failed_turn(reloaded, ADVISOR),
                         record)
        entry = state_store.read_pending_switch(reloaded, ADVISOR)
        self.assertEqual(entry["pending_turn"], record["request"]["text"])
        self.assertTrue(state_store.failed_turn_present(reloaded, ADVISOR))

    def test_malformed_record_is_present_but_not_readable(self):
        path = self.session_file()
        bad = make_record()
        del bad["lead"]
        state = state_store.load(path)
        state["pending_switches"] = {ADVISOR: {"failed_turn": bad}}
        state_store.save(path, state)
        reloaded = state_store.load(path)
        self.assertTrue(state_store.failed_turn_present(reloaded, ADVISOR))
        self.assertIsNone(state_store.read_failed_turn(reloaded, ADVISOR))

    def test_wrong_schema_or_types_are_not_readable(self):
        for override in ({"schema": 2}, {"round": "1"},
                         {"invalidation_seq_at_failure": True},
                         {"candidate": {"path": "/x"}}):
            with self.subTest(override=override):
                state = {"pending_switches": {
                    ADVISOR: {"failed_turn": make_record(**override)}}}
                self.assertIsNone(
                    state_store.read_failed_turn(state, ADVISOR))

    def test_old_session_state_has_no_record(self):
        path = self.session_file()
        state_store.save_pending_turn(path, ADVISOR, "legacy pending text",
                                      prior=state_store.load(path))
        state = state_store.load(path)
        self.assertFalse(state_store.failed_turn_present(state, ADVISOR))
        self.assertIsNone(state_store.read_failed_turn(state, ADVISOR))
        self.assertNotIn("failed_turn",
                         state_store.read_pending_switch(state, ADVISOR))
        self.assertNotIn(state_store.FAILED_TURN_RETIREMENTS_KEY, state)

    def test_retire_drops_the_record_its_request_and_an_empty_entry(self):
        path = self.session_file()
        record = make_record()
        state_store.save_failed_turn(path, ADVISOR, record,
                                     prior=state_store.load(path))
        state_store.retire_failed_turn(
            path, ADVISOR, "invalidation_record", "invalidation:0",
            prior=state_store.load(path))
        reloaded = state_store.load(path)
        self.assertIsNone(state_store.read_pending_switch(reloaded, ADVISOR))
        log = reloaded[state_store.FAILED_TURN_RETIREMENTS_KEY][ADVISOR]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["reason"], "invalidation_record")
        self.assertEqual(log[0]["reason_ref"], "invalidation:0")
        self.assertEqual(log[0]["request_sha256"],
                         record["request"]["sha256"])

    def test_retire_keeps_switch_fields_and_a_foreign_pending_turn(self):
        path = self.session_file()
        record = make_record()
        state = state_store.load(path)
        state["pending_switches"] = {ADVISOR: {
            "from_controller": "claude", "to_controller": "codex",
            "pending_turn": "some other turn", "failed_turn": record}}
        state_store.save(path, state)
        state_store.retire_failed_turn(path, ADVISOR, "pending_lead_decision",
                                       "r1", prior=state_store.load(path))
        entry = state_store.read_pending_switch(state_store.load(path),
                                                ADVISOR)
        self.assertEqual(entry["from_controller"], "claude")
        self.assertEqual(entry["pending_turn"], "some other turn")
        self.assertNotIn("failed_turn", entry)

    def test_retirement_log_is_bounded(self):
        path = self.session_file()
        for index in range(state_store.FAILED_TURN_RETIREMENTS_KEEP + 3):
            state_store.save_failed_turn(
                path, ADVISOR, make_record(), prior=state_store.load(path))
            state_store.retire_failed_turn(path, ADVISOR, "invalidation_record",
                                           "invalidation:%d" % index,
                                           prior=state_store.load(path))
        log = state_store.load(path)[
            state_store.FAILED_TURN_RETIREMENTS_KEY][ADVISOR]
        self.assertEqual(len(log), state_store.FAILED_TURN_RETIREMENTS_KEEP)
        self.assertEqual(log[-1]["reason_ref"], "invalidation:%d"
                         % (state_store.FAILED_TURN_RETIREMENTS_KEEP + 2))

    def test_record_survives_a_controller_switch_with_its_request(self):
        path = self.session_file()
        record = make_record()
        state_store.save_failed_turn(path, ADVISOR, record,
                                     prior=state_store.load(path))
        state_store.switch_role_controller(
            path, ADVISOR, "codex", prior=state_store.load(path),
            reason="cli", source="cli")
        entry = state_store.read_pending_switch(state_store.load(path),
                                                ADVISOR)
        self.assertEqual(entry["to_controller"], "codex")
        self.assertEqual(entry["failed_turn"], record)
        self.assertEqual(entry["pending_turn"], record["request"]["text"])

    def test_record_survives_the_multi_role_transition(self):
        path = self.session_file()
        record = make_record()
        state_store.save_failed_turn(path, ADVISOR, record,
                                     prior=state_store.load(path))
        state_store.apply_controller_transition(
            path, [(ADVISOR, "codex")], prior=state_store.load(path),
            source="cli", reason="cli",
            pending_turns={ADVISOR: record["request"]["text"]})
        entry = state_store.read_pending_switch(state_store.load(path),
                                                ADVISOR)
        self.assertEqual(entry["failed_turn"], record)
        self.assertEqual(entry["to_controller"], "codex")


# --------------------------------------------------------------------------- #
# Loop: `_role_loop` with a reviewer-first binding.                            #
# --------------------------------------------------------------------------- #

class _FakeLead:
    """A lead whose every send records itself and completes its candidate."""

    def __init__(self, case, role=PLANNER):
        self.case = case
        self.role = role
        self.sent = []

    def send(self, text):
        self.sent.append(str(text))
        self.case.log.append("lead")
        self.case.write_status("ready_for_review", body="v%d" % (
            len(self.sent) + 1))

    def close(self):
        pass


class _LoopCase(_SessionsRootCase):
    """Shared fixtures for driving `_role_loop` directly."""

    def setUp(self):
        super().setUp()
        self.status_path = os.path.join(self.dir, "plan.json")
        self.review_path = os.path.join(self.dir, "review.json")
        self.summary_path = os.path.join(self.dir, "summary.md")
        self.log = []

    def write_status(self, status="ready_for_review", body="v1"):
        write_json(self.status_path, {"status": status,
                                      "result": {"body": body}})

    def binding(self, candidate_sha=None, record=True, **overrides):
        sha = candidate_sha or sha256_file(self.status_path)
        binding = {
            "tier": "exact",
            "record": make_record() if record else None,
            "lead_role": PLANNER, "reviewer_role": ADVISOR,
            "candidate_sha256": sha, "summary_sha256": None, "round": 1}
        binding.update(overrides)
        return binding

    def loop(self, sess, review_fn, role=PLANNER, **kwargs):
        reviewer = ADVISOR if role == PLANNER else BUILD_REVIEWER
        return cowork._role_loop(
            sess, "seed", self.status_path, context="", io_out=io.StringIO(),
            role=role, review_fn=review_fn, reviewer_role=reviewer,
            artifact_noun="plan" if role == PLANNER else "build",
            handoff_enabled=True,
            phase="planning" if role == PLANNER else "building",
            review_path=self.review_path, **kwargs)

    def reviewer(self, *verdicts):
        script = list(verdicts)
        calls = []

        def review_fn(path, round_index):
            self.log.append("review")
            calls.append(round_index)
            return script.pop(0) if script else None
        review_fn.calls = calls
        return review_fn


class RoleLoopTests(_LoopCase):
    def test_the_reviewer_runs_first_and_the_lead_gets_no_send(self):
        self.write_status()
        sess = _FakeLead(self)
        rejected = []
        review_fn = self.reviewer({"verdict": "approve"})
        rc, outcome, payload = self.loop(
            sess, review_fn, reviewer_first=self.binding(),
            on_first_send_rejected=lambda: rejected.append(1))
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(sess.sent, [])
        self.assertEqual(self.log, ["review"])
        self.assertEqual(rejected, [1])

    def test_the_retry_is_the_same_review_round_the_failure_was_in(self):
        self.write_status()
        review_fn = self.reviewer({"verdict": "approve"})
        self.loop(_FakeLead(self), review_fn,
                  reviewer_first=self.binding(round=2))
        self.assertEqual(review_fn.calls, [2])

    def test_a_derived_binding_reviews_round_one_without_hash_checks(self):
        self.write_status()
        review_fn = self.reviewer({"verdict": "approve"})
        sess = _FakeLead(self)
        binding = {"tier": "derived", "record": None, "lead_role": PLANNER,
                   "reviewer_role": ADVISOR, "round": 1}
        rc, outcome, _payload = self.loop(sess, review_fn,
                                          reviewer_first=binding)
        self.assertEqual(outcome, "approved")
        self.assertEqual(review_fn.calls, [1])
        self.assertEqual(sess.sent, [])

    def assert_stops_before_any_send(self, binding, code, role=PLANNER,
                                     **kwargs):
        sess = _FakeLead(self)
        rejected = []
        review_fn = self.reviewer({"verdict": "approve"})
        rc, outcome, payload = self.loop(
            sess, review_fn, role=role, reviewer_first=binding,
            on_first_send_rejected=lambda: rejected.append(1), **kwargs)
        self.assertEqual((rc, outcome), (0, "ended"))
        self.assertEqual(payload["kind"], RECOVERY_KIND)
        self.assertEqual(payload["mismatch"], code)
        self.assertEqual(payload["requires"], "operator")
        self.assertIs(payload["approved"], False)
        self.assertEqual(sess.sent, [])
        self.assertEqual(self.log, [])
        self.assertEqual(rejected, [1])
        return payload

    def test_a_wrong_lead_or_reviewer_role_stops_before_any_send(self):
        self.write_status()
        self.assert_stops_before_any_send(
            self.binding(lead_role=BUILDER), "wrong_first_role")
        self.assert_stops_before_any_send(
            self.binding(reviewer_role=BUILD_REVIEWER), "wrong_first_role")

    def test_a_lead_that_is_not_ready_stops_before_any_send(self):
        self.write_status("needs_input")
        self.assert_stops_before_any_send(
            self.binding(candidate_sha="c" * 64), "lead_not_ready")

    def test_changed_candidate_bytes_stop_before_any_send(self):
        self.write_status()
        binding = self.binding()
        self.write_status(body="edited")
        payload = self.assert_stops_before_any_send(binding,
                                                    "candidate_changed")
        # The stop names identities only, so a supervisor can link a reopen.
        self.assertEqual(payload["work_id"], "W-1")
        self.assertEqual(payload["candidate_manifest_digest"], "b" * 64)
        self.assertEqual(payload["session_uuid"], "S-1")
        self.assertEqual(payload["invalidation_seq_at_failure"], 0)
        self.assertNotIn("edited", json.dumps(payload))

    def test_a_changed_build_summary_stops_before_any_send(self):
        self.write_status()
        write_text(self.summary_path, "summary one")
        binding = self.binding(
            lead_role=BUILDER, reviewer_role=BUILD_REVIEWER,
            summary_sha256=sha256_file(self.summary_path))
        write_text(self.summary_path, "summary two")
        self.assert_stops_before_any_send(
            binding, "candidate_changed", role=BUILDER,
            build_summary_path=self.summary_path)

    def test_a_genuine_revise_returns_to_the_lead_with_the_context_block(self):
        self.write_status()
        sess = _FakeLead(self)
        review_fn = self.reviewer({"verdict": "revise",
                                   "findings": ["tighten the plan"]},
                                  {"verdict": "approve"})
        block = cowork.context_update_block("fresh context", self.dir, 2)
        owed = []

        def context_block():
            owed.append(1)
            return block
        rc, outcome, _payload = self.loop(
            sess, review_fn, reviewer_first=self.binding(),
            lead_context_block_fn=context_block)
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(self.log, ["review", "lead", "review"])
        self.assertEqual(len(sess.sent), 1)
        self.assertIn("[reviewer handoff]", sess.sent[0])
        self.assertIn(str(block), sess.sent[0])
        self.assertLess(sess.sent[0].index(str(block)),
                        sess.sent[0].index("[reviewer handoff]"))
        self.assertEqual(owed, [1])
        # The failed round was round 1, so the re-review is round 2.
        self.assertEqual(review_fn.calls, [1, 2])

    def test_no_context_block_is_added_when_none_is_owed(self):
        self.write_status()
        sess = _FakeLead(self)
        review_fn = self.reviewer({"verdict": "revise", "findings": ["x"]},
                                  {"verdict": "approve"})
        rc, outcome, _payload = self.loop(
            sess, review_fn, reviewer_first=self.binding(),
            lead_context_block_fn=lambda: None)
        self.assertEqual(outcome, "approved")
        self.assertIn("[reviewer handoff]", sess.sent[0])
        self.assertNotIn("context.rev", sess.sent[0])

    def test_a_failing_reviewer_stops_again_and_reports_the_candidate(self):
        self.write_status()
        sess = _FakeLead(self)
        review_fn = self.reviewer()
        written = []
        rc, outcome, payload = self.loop(
            sess, review_fn, reviewer_first=self.binding(),
            failed_turn_fn=written.append)
        self.assertEqual((rc, outcome), (0, "ended"))
        self.assertEqual(payload["kind"], "reviewer_unavailable")
        self.assertEqual(payload["requires"], "reviewer")
        self.assertEqual(len(review_fn.calls), cowork.REVIEW_FAIL_CAP)
        self.assertEqual(sess.sent, [])
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["candidate_sha256"],
                         sha256_file(self.status_path))
        self.assertEqual(written[0]["round"], 1)

    def test_the_writer_receives_the_candidate_identities_on_a_lead_first_run(
            self):
        sess = _FakeLead(self)
        review_fn = self.reviewer()
        written = []
        rc, outcome, payload = self.loop(
            sess, review_fn, failed_turn_fn=written.append,
            role_work_id="W-loop")
        self.assertEqual(payload["kind"], "reviewer_unavailable")
        self.assertEqual(len(sess.sent), 1)
        ident, = written
        self.assertEqual(ident["lead_role"], PLANNER)
        self.assertEqual(ident["reviewer_role"], ADVISOR)
        self.assertEqual(ident["phase"], "planning")
        self.assertEqual(ident["status_path"], self.status_path)
        self.assertEqual(ident["review_path"], self.review_path)
        self.assertEqual(ident["candidate_sha256"],
                         sha256_file(self.status_path))
        self.assertEqual(ident["work_id"], "W-loop")
        self.assertEqual(ident["failures"], cowork.REVIEW_FAIL_CAP)
        self.assertIsNone(ident["finding_sha256"])
        self.assertIsNone(ident["summary_path"])

    def test_a_writer_error_never_masks_the_reviewer_stop(self):
        sess = _FakeLead(self)

        def boom(_ident):
            raise OSError("disk full")
        rc, outcome, payload = self.loop(sess, self.reviewer(),
                                         failed_turn_fn=boom)
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "reviewer_unavailable")

    def test_without_the_new_arguments_the_lead_is_sent_first(self):
        sess = _FakeLead(self)
        review_fn = self.reviewer({"verdict": "approve"})
        rc, outcome, _payload = self.loop(sess, review_fn)
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(self.log, ["lead", "review"])
        self.assertEqual(len(sess.sent), 1)


class BuilderRoleLoopTests(_LoopCase):
    """The builder's owned verification still runs before the reviewer gate;
    it is not a lead send."""

    TXN_ID = "T-green"
    MANIFEST = "ab" * 32
    INDEX = "cd" * 32

    def test_the_build_reviewer_runs_first_and_approves(self):
        self.write_status()
        write_text(self.summary_path, "summary")
        uuid_text = "S-builder-loop"
        trace_path = trace_store.trace_path_for(uuid_text)
        os.makedirs(os.path.dirname(trace_path), exist_ok=True)
        trace = trace_store.Trace(trace_path, session_uuid=uuid_text,
                                  run_id="R")
        sess = _FakeLead(self, BUILDER)
        review_fn = self.reviewer({"verdict": "approve"})
        green = {"transaction_id": self.TXN_ID, "verdict": "green",
                 "final_suite_label": "full_unit_suite",
                 "final_suite_binding": "ran_once",
                 "attempts": [{"label": "full_unit_suite"}],
                 "snapshot": {"manifest_digest": self.MANIFEST,
                              "index_digest": self.INDEX}}
        binding = self.binding(
            lead_role=BUILDER, reviewer_role=BUILD_REVIEWER,
            summary_sha256=sha256_file(self.summary_path))
        with mock.patch.object(
                cowork, "_run_owned_verification_transaction",
                return_value=(green, None)), \
                mock.patch.object(
                    cowork, "_record_readiness_from_transaction",
                    return_value={"state": "verified", "reason": None,
                                  "event_id": "E",
                                  "transaction_id": self.TXN_ID}), \
                mock.patch.object(
                    cowork.verification, "current_candidate_identity",
                    return_value=(self.MANIFEST, self.INDEX)):
            rc, outcome, _payload = self.loop(
                sess, review_fn, role=BUILDER, reviewer_first=binding,
                build_summary_path=self.summary_path, trace=trace,
                session_uuid=uuid_text)
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(sess.sent, [])
        self.assertEqual(self.log, ["review"])


# --------------------------------------------------------------------------- #
# Flow: production `run_flow` with the real run_planner / run_builder.         #
# --------------------------------------------------------------------------- #

def init_repo():
    repo = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", repo], check=True)
    for args in (("config", "user.email", "t@t"), ("config", "user.name", "t"),
                 ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", repo] + list(args), check=True)
    write_text(os.path.join(repo, "src.txt"), "one\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True)
    return repo


class _FlowLead:
    """A lead session: every send is logged and completes the candidate."""

    def __init__(self, env, lead):
        self.env = env
        self.lead = lead
        self.sent = []

    def send(self, text, meta=None):
        self.sent.append(str(text))
        self.env.log.append((self.lead, "send"))
        self.env.write_candidate(self.lead, body="turn-%d" % len(self.sent))
        return {"ok": True, "result": "ok"}

    def close(self):
        pass


class _FlowCase(unittest.TestCase):
    UUID = "RC-SESSION"
    TXN_ID = "T-green"
    MANIFEST = "ab" * 32
    INDEX = "cd" * 32

    def setUp(self):
        cowork.policy.deactivate()
        self.addCleanup(cowork.policy.deactivate)
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        patch = mock.patch.dict(os.environ,
                                {"COWORK_SESSIONS_ROOT": self.root})
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = init_repo()
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        prior = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, prior)
        self.log = []
        self.sessions = {PLANNER: [], BUILDER: []}
        self.reviewer_calls = []
        self.captured = {PLANNER: [], BUILDER: []}
        self.verdicts = {ADVISOR: [], BUILD_REVIEWER: []}
        self.during_review = None
        for patcher in (
                mock.patch.object(
                    cowork, "_run_owned_verification_transaction",
                    side_effect=lambda *a, **k: (self.green(), None)),
                mock.patch.object(
                    cowork, "_record_readiness_from_transaction",
                    return_value={"state": "verified", "reason": None,
                                  "event_id": "E",
                                  "transaction_id": self.TXN_ID}),
                mock.patch.object(
                    cowork.verification, "current_candidate_identity",
                    return_value=(self.MANIFEST, self.INDEX))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assets = state_store.session_assets_dir(self.UUID)
        self.plan_json = state_store.planner_plan_json_path_for(
            self.assets, self.UUID)
        self.plan_md = state_store.planner_plan_md_path_for(
            self.assets, self.UUID)
        self.plan_review = state_store.planner_review_path_for(
            self.assets, self.UUID)
        self.build_status = state_store.build_status_path_for(
            self.assets, self.UUID)
        self.build_summary = state_store.build_summary_path_for(
            self.assets, self.UUID)
        self.build_review = state_store.build_review_path_for(
            self.assets, self.UUID)

    def green(self):
        return {"transaction_id": self.TXN_ID, "verdict": "green",
                "final_suite_label": "full_unit_suite",
                "final_suite_binding": "ran_once",
                "attempts": [{"label": "full_unit_suite"}],
                "snapshot": {"manifest_digest": self.MANIFEST,
                             "index_digest": self.INDEX}}

    # -- fixtures ---------------------------------------------------------- #

    def paths(self, lead):
        if lead == PLANNER:
            return {"status": self.plan_json, "review": self.plan_review,
                    "summary": None}
        return {"status": self.build_status, "review": self.build_review,
                "summary": self.build_summary}

    def write_candidate(self, lead, status="ready_for_review", body="v1"):
        paths = self.paths(lead)
        write_json(paths["status"], {"status": status,
                                     "result": {"body": body, "repos": [
                                         {"path": self.repo,
                                          "selected": True}]}})
        if lead == PLANNER:
            write_text(self.plan_md, "# plan %s\n" % body)
        else:
            write_text(paths["summary"], "# summary %s\n" % body)

    def make_session(self, case, lead_session=True, with_candidate=True):
        lead, phase = case["lead"], case["phase"]
        team = ["scout", "scout-reviewer", PLANNER, ADVISOR]
        if lead == BUILDER:
            team += [BUILDER, BUILD_REVIEWER]
        self.spath = os.path.join(self.root, "proj", ".cowork", "session.json")
        state = state_store.ensure_session(self.spath, None, self.UUID)
        config = cowork.default_config(team)
        for role in (PLANNER, BUILDER):
            if role in config:
                config[role] = dict(config[role], controller="codex")
        state = state_store.save_config(self.spath, team, config, prior=state)
        state = state_store.save_phase(self.spath, phase, prior=state)
        if lead_session:
            state = state_store.save_role_session(
                self.spath, lead, "codex", "lead-thread-0", prior=state)
        if lead == BUILDER:
            # The approved plan the builder works from.
            write_json(self.plan_json, {"status": "ready_for_review",
                                        "result": {"repos": [
                                            {"path": self.repo,
                                             "selected": True}]}})
            write_text(self.plan_md, "# approved plan\n")
        if with_candidate:
            self.write_candidate(lead)
        return self.spath

    # -- fakes ------------------------------------------------------------- #

    def factory_for(self, lead):
        def factory(controller, **kwargs):
            sess = _FlowLead(self, lead)
            self.sessions[lead].append(sess)
            callback = kwargs.get("on_thread_id") or kwargs.get(
                "on_session_id")
            resumed = kwargs.get("resume_thread_id") or kwargs.get(
                "resume_id")
            if callback and not resumed:
                callback("%s-thread-new" % lead)
            return sess
        return factory

    def reviewer_runner_for(self, reviewer):
        def runner(config, context, selected, artifact_path, review_path,
                   **kwargs):
            self.log.append((reviewer, "review"))
            self.reviewer_calls.append({
                "role": reviewer, "context": str(context),
                "artifact_path": artifact_path, "review_path": review_path,
                "kwargs": dict(kwargs)})
            if os.path.exists(review_path):
                os.remove(review_path)
            if self.during_review is not None:
                self.during_review()
            script = self.verdicts[reviewer]
            verdict = script.pop(0) if script else None
            if verdict is None:
                return None
            write_json(review_path, verdict)
            return dict(verdict)
        return runner

    def lead_fn(self, lead):
        real = cowork.run_planner if lead == PLANNER else cowork.run_builder
        reviewer = ADVISOR if lead == PLANNER else BUILD_REVIEWER
        wrapped = functools.partial(
            real, session_factory=self.factory_for(lead),
            reviewer_runner=self.reviewer_runner_for(reviewer))

        def run(config, context, selected, **kwargs):
            self.captured[lead].append(dict(kwargs, context=str(context)))
            return wrapped(config, context, selected, **kwargs)
        return run

    def forbidden(self, role):
        def fake(*_a, **_k):
            self.fail("%s must not run" % role)
        return fake

    def run_flow(self, argv=()):
        out = io.StringIO()
        box = {}
        args = cowork.build_parser().parse_args(
            ["--session-file", self.spath, "--evaluation-policy", "off"]
            + list(argv))
        rc = cowork.run_flow(
            args, io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=self.forbidden("scout"),
            run_planner_fn=self.lead_fn(PLANNER),
            run_builder_fn=self.lead_fn(BUILDER), result_box=box)
        return rc, out.getvalue(), box

    # -- helpers ----------------------------------------------------------- #

    def fail_once(self, case):
        """One real run in which the reviewer cannot return a usable verdict."""
        self.verdicts[case["reviewer"]] = [None] * cowork.REVIEW_FAIL_CAP
        rc, _out, box = self.run_flow()
        self.assertEqual(rc, 1)
        self.assertEqual(box["last"]["payload"]["kind"],
                         "reviewer_unavailable")
        return state_store.load(self.spath)

    def record_of(self, case):
        return state_store.read_failed_turn(state_store.load(self.spath),
                                            case["reviewer"])

    def events(self):
        path = trace_store.trace_path_for(self.UUID)
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def lead_sends(self, lead, since=0):
        return sum(len(s.sent) for s in self.sessions[lead][since:])

    def stop_payload(self, rc, box):
        self.assertEqual(rc, 1)
        result = cowork.build_run_result(rc, box)
        self.assertEqual(result["stop"]["kind"], RECOVERY_KIND)
        self.assertEqual(result["stop"]["requires"], "operator")
        self.assertNotIn("decision_argv", result)
        return result["stop"]

    def invalidation_for(self, identities, **overrides):
        record = {
            "schema_version": 1, "package_id": "pkg-recovery",
            "invalidated_candidate_digest": identities["digest"],
            "invalidated_session_id": identities["session"],
            "invalidated_work_id": identities["work_id"],
            "invalidating_principal": "orchestrator",
            "reason": "candidate reopened",
            "evidence_refs": [{"path": "evidence.txt", "sha256": "c" * 64}],
            "issued_at": "2024-01-01T00:00:00Z"}
        record.update(overrides)
        return state_store.append_invalidation_record(self.UUID, record)

    def open_lead_request(self, case):
        """An open `needs_input` request for the lead, bound to its candidate's
        current bytes, as a stopped lead phase would have recorded it."""
        paths = self.paths(case["lead"])
        return state_store.open_decision_request(self.UUID, {
            "kind": "needs_input", "role": case["lead"],
            "phase": case["phase"], "requires": "answer", "work_id": None,
            "status_path": paths["status"],
            "status_sha256": sha256_file(paths["status"]),
            "question": "which option?", "handoff": None, "findings": None,
        })["request_id"]

    def route_events(self):
        return [e for e in self.events() if e["event"] == "recovery.route"]

    def identities_from_record(self, record):
        return {"digest": record["lead"]["candidate_manifest_digest"],
                "session": self.UUID, "work_id": record["lead"]["work_id"]}


class ReviewerFirstFlowTests(_FlowCase):
    """The failed reviewer resumes first on both recovery routes; its
    completed lead receives no send."""

    def reviewer_first(self, case, argv=(), switch=False):
        lead, reviewer = case["lead"], case["reviewer"]
        self.make_session(case)
        state = self.fail_once(case)
        record = state_store.read_failed_turn(state, reviewer)
        self.assertIsNotNone(record)
        status_sha = sha256_file(self.paths(lead)["status"])
        summary = self.paths(lead)["summary"]
        summary_sha = sha256_file(summary) if summary else None
        sessions_before = len(self.sessions[lead])
        self.log.clear()
        self.reviewer_calls.clear()
        self.verdicts[reviewer] = [{"verdict": "approve", "findings": []}]
        argv = list(argv)
        if switch:
            argv += ["--switch-controller", "%s=codex" % reviewer]
        rc, out, box = self.run_flow(argv)
        self.assertEqual(rc, 0, out)
        # The reviewer ran first and alone; the lead was never sent anything.
        self.assertEqual(self.log, [(reviewer, "review")])
        self.assertEqual(self.lead_sends(lead, sessions_before), 0)
        # The completed candidate is byte-identical and the lead session held.
        self.assertEqual(sha256_file(self.paths(lead)["status"]), status_sha)
        if summary:
            self.assertEqual(sha256_file(summary), summary_sha)
        final = state_store.load(self.spath)
        self.assertEqual(final["sessions"][lead]["id"], "lead-thread-0")
        # The recovery packet reached the reviewer by path.
        context = self.reviewer_calls[0]["context"]
        self.assertIn("failed pending turn", context)
        self.assertIn(os.path.abspath(self.paths(lead)["status"]), context)
        if switch:
            self.assertIn("[controller switch handoff]", context)
        else:
            self.assertNotIn("[controller switch handoff]", context)
        # The consumed entry (and its record) is gone with the approval.
        self.assertIsNone(state_store.read_pending_switch(final, reviewer))
        return record, final

    def test_planning_advisor_resumes_first_on_a_plain_resume(self):
        self.reviewer_first(CASES[0])

    def test_planning_advisor_resumes_first_on_a_controller_switch(self):
        self.reviewer_first(CASES[0], switch=True)

    def test_build_reviewer_resumes_first_on_a_plain_resume(self):
        self.reviewer_first(CASES[1])

    def test_build_reviewer_resumes_first_on_a_controller_switch(self):
        self.reviewer_first(CASES[1], switch=True)

    def test_the_failure_retains_exact_hashes_and_identities(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead = case["lead"]
                self.make_session(case)
                self.fail_once(case)
                record = self.record_of(case)
                paths = self.paths(lead)
                self.assertEqual(record["role"], case["reviewer"])
                self.assertEqual(record["lead_role"], lead)
                self.assertEqual(record["phase"], case["phase"])
                self.assertEqual(record["candidate"]["path"], paths["status"])
                self.assertEqual(record["candidate"]["sha256"],
                                 sha256_file(paths["status"]))
                if paths["summary"]:
                    self.assertEqual(record["candidate"]["summary_sha256"],
                                     sha256_file(paths["summary"]))
                self.assertEqual(record["finding"]["path"], paths["review"])
                text = record["request"]["text"]
                self.assertEqual(record["request"]["sha256"],
                                 hashlib.sha256(text.encode()).hexdigest())
                self.assertIn(record["candidate"]["sha256"], text)
                self.assertIn(paths["status"], text)
                self.assertEqual(record["lead"]["provider_session_id"],
                                 "lead-thread-0")
                self.assertEqual(record["lead"]["controller"], "codex")
                self.assertEqual(record["lead"]["status"], "ready_for_review")
                self.assertTrue(record["lead"]["work_id"])
                self.assertTrue(record["lead"]["candidate_manifest_digest"])
                self.assertEqual(record["invalidation_seq_at_failure"], 0)
                self.assertEqual(record["failures"], cowork.REVIEW_FAIL_CAP)
                entry = state_store.read_pending_switch(
                    state_store.load(self.spath), case["reviewer"])
                self.assertEqual(entry["pending_turn"], text)

    def test_the_record_survives_a_process_restart_and_a_switch(self):
        case = CASES[0]
        self.make_session(case)
        self.fail_once(case)
        record = self.record_of(case)
        state_store.switch_role_controller(
            self.spath, ADVISOR, "codex", prior=state_store.load(self.spath),
            reason="cli", source="cli")
        reloaded = state_store.load(self.spath)
        self.assertEqual(state_store.read_failed_turn(reloaded, ADVISOR),
                         record)

    def test_the_recovered_attempt_reaches_the_state_an_ordinary_approve_does(
            self):
        """The recovery runs under a new WorkUnit (the failed attempt stays
        terminal) and its approve drives that WorkUnit exactly as an approve
        in an ordinary lead-first run does."""
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                record, final = self.reviewer_first(case)
                failed_id = record["lead"]["work_id"]
                epoch = record["epoch"]
                new_id = cowork._role_work_id(self.UUID, lead, epoch, 1)
                self.assertNotEqual(failed_id, new_id)
                self.assertEqual(
                    state_store.current_phase_state(
                        self.UUID, failed_id)["state"], "failed")
                recovered = state_store.current_phase_state(
                    self.UUID, new_id)["state"]
                # Control: the same session shape, approved lead-first.
                self.setUp()
                self.make_session(case, with_candidate=False)
                self.verdicts[reviewer] = [
                    {"verdict": "approve", "findings": []}]
                rc, out, _box = self.run_flow()
                self.assertEqual(rc, 0, out)
                control = state_store.current_phase_state(
                    self.UUID, cowork._role_work_id(self.UUID, lead, epoch,
                                                    0))["state"]
                self.assertEqual(recovered, control)
                self.assertNotIn(recovered, ("pending", "preflighting",
                                             "running", "failed"))

    def test_a_context_bump_reaches_the_reviewer_and_leaves_the_lead_unacked(
            self):
        case = CASES[0]
        self.make_session(case)
        self.fail_once(case)
        self.log.clear()
        self.verdicts[ADVISOR] = [{"verdict": "approve", "findings": []}]
        before = len(self.sessions[PLANNER])
        rc, out, _box = self.run_flow(["--context", "updated goal"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log, [(ADVISOR, "review")])
        self.assertEqual(self.lead_sends(PLANNER, before), 0)
        final = state_store.load(self.spath)
        self.assertTrue(state_store.role_context_gap(final, PLANNER))
        delivered = [e for e in self.events()
                     if e["event"] == "context.gap" and e.get("role") == PLANNER
                     and e.get("delivered")]
        self.assertEqual(delivered, [])

    def test_a_genuine_revise_returns_to_the_lead_with_the_context_update(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                self.make_session(case)
                self.fail_once(case)
                self.log.clear()
                before = len(self.sessions[lead])
                self.verdicts[reviewer] = [
                    {"verdict": "revise", "findings": ["tighten it"]},
                    {"verdict": "approve", "findings": []}]
                rc, out, _box = self.run_flow(["--context", "updated goal"])
                self.assertEqual(rc, 0, out)
                self.assertEqual(
                    self.log, [(reviewer, "review"), (lead, "send"),
                               (reviewer, "review")])
                sent = [t for s in self.sessions[lead][before:]
                        for t in s.sent]
                self.assertEqual(len(sent), 1)
                self.assertIn("[reviewer handoff]", sent[0])
                self.assertIn("context.rev", sent[0])
                self.assertLess(sent[0].index("context.rev"),
                                sent[0].index("[reviewer handoff]"))

    def test_a_failing_reviewer_stops_again_without_a_lead_send(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                self.make_session(case)
                first = self.fail_once(case)
                first_record = state_store.read_failed_turn(first, reviewer)
                before = len(self.sessions[lead])
                self.log.clear()
                self.verdicts[reviewer] = []
                rc, _out, box = self.run_flow()
                self.assertEqual(rc, 1)
                self.assertEqual(box["last"]["payload"]["kind"],
                                 "reviewer_unavailable")
                self.assertEqual(self.lead_sends(lead, before), 0)
                self.assertEqual(self.log, [(reviewer, "review")]
                                 * cowork.REVIEW_FAIL_CAP)
                again = self.record_of(case)
                self.assertEqual(again["candidate"], first_record["candidate"])
                self.assertEqual(again["lead"]["provider_session_id"],
                                 "lead-thread-0")


class ReviewerFirstNeverApprovesByOmissionTests(_FlowCase):
    def recovered(self, case):
        self.make_session(case)
        self.fail_once(case)
        self.log.clear()
        return len(self.sessions[case["lead"]])

    def test_a_reviewer_question_stops_and_is_never_an_approval(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                before = self.recovered(case)
                self.verdicts[reviewer] = [
                    {"verdict": "needs_user", "user_question": "which one?"}]
                rc, _out, box = self.run_flow()
                result = cowork.build_run_result(rc, box)
                self.assertEqual(rc, cowork.AGENT_STOP_EXIT_CODE)
                self.assertIs(result["approved"], False)
                self.assertEqual(result["stop"]["kind"], "reviewer_question")
                self.assertEqual(self.log, [(reviewer, "review")])
                self.assertEqual(self.lead_sends(lead, before), 0)

    def test_a_candidate_that_changes_during_the_review_is_not_approved(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                before = self.recovered(case)
                self.verdicts[reviewer] = [
                    {"verdict": "approve", "findings": []}]
                self.during_review = functools.partial(
                    self.write_candidate, lead, body="changed-in-review")
                rc, _out, box = self.run_flow()
                self.assertEqual(rc, 1)
                self.assertEqual(box["last"]["payload"]["kind"],
                                 "review_candidate_changed")
                self.assertFalse(cowork.build_run_result(rc, box)["approved"])
                self.assertEqual(self.lead_sends(lead, before), 0)

    def test_recovery_leaves_policy_capacity_and_decisions_untouched(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                before = self.recovered(case)
                state_before = state_store.load(self.spath)
                self.verdicts[reviewer] = [
                    {"verdict": "approve", "findings": []}]
                rc, out, _box = self.run_flow()
                self.assertEqual(rc, 0, out)
                state_after = state_store.load(self.spath)
                self.assertEqual(state_after.get("controller_policy"),
                                 state_before.get("controller_policy"))
                for key in ("config", "team", "phase"):
                    self.assertEqual(state_after.get(key),
                                     state_before.get(key), key)
                self.assertIsNone(state_store.read_pending_turn_before_pause(
                    self.UUID, lead))
                self.assertEqual(
                    state_store.read_pending_decision_deliveries(
                        self.UUID, trusted_state=state_after), [])


class FailClosedTests(_FlowCase):
    """A recovery binding that does not hold stops before any send."""

    def prepared(self, case):
        self.make_session(case)
        self.fail_once(case)
        self.log.clear()
        self.verdicts[case["reviewer"]] = [
            {"verdict": "approve", "findings": []}]
        return self.record_of(case), len(self.sessions[case["lead"]])

    def edit_record(self, reviewer, **changes):
        state = state_store.load(self.spath)
        record = dict(state["pending_switches"][reviewer]["failed_turn"])
        record.update(changes)
        state["pending_switches"][reviewer]["failed_turn"] = record
        state_store.save(self.spath, state)

    def assert_stopped(self, code, case, before):
        rc, _out, box = self.run_flow()
        stop = self.stop_payload(rc, box)
        self.assertEqual(stop["mismatch"], code)
        self.assertEqual(self.log, [])
        self.assertEqual(self.lead_sends(case["lead"], before), 0)
        # The record is held for the supervisor, never silently dropped.
        self.assertTrue(state_store.failed_turn_present(
            state_store.load(self.spath), case["reviewer"]))
        return stop

    def test_a_record_naming_the_other_reviewer_stops(self):
        for case, other in ((CASES[0], BUILD_REVIEWER),
                            (CASES[1], ADVISOR)):
            with self.subTest(lead=case["lead"]):
                self.setUp()
                _record, before = self.prepared(case)
                self.edit_record(case["reviewer"], role=other)
                self.assert_stopped("wrong_first_role", case, before)

    def test_a_record_naming_a_lead_role_stops(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        self.edit_record(ADVISOR, role=PLANNER)
        self.assert_stopped("wrong_first_role", case, before)

    def test_a_record_for_another_phase_stops(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        self.edit_record(ADVISOR, phase="building")
        self.assert_stopped("phase_mismatch", case, before)

    def test_a_malformed_record_stops(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        state = state_store.load(self.spath)
        state["pending_switches"][ADVISOR]["failed_turn"] = {"schema": 1}
        state_store.save(self.spath, state)
        self.assert_stopped("malformed_record", case, before)

    def test_a_stray_record_for_the_other_reviewer_stops(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        state = state_store.load(self.spath)
        state["pending_switches"][BUILD_REVIEWER] = {
            "failed_turn": make_record(BUILD_REVIEWER, BUILDER, "building")}
        state_store.save(self.spath, state)
        self.assert_stopped("wrong_first_role", case, before)

    def test_changed_candidate_bytes_alone_stop(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                _record, before = self.prepared(case)
                self.write_candidate(case["lead"], body="edited")
                self.assert_stopped("candidate_changed", case, before)

    def test_a_lead_that_is_no_longer_ready_stops(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        self.write_candidate(PLANNER, status="needs_input")
        self.assert_stopped("lead_not_ready", case, before)

    def test_a_different_lead_session_stops(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        state_store.save_role_session(
            self.spath, PLANNER, "codex", "someone-elses-thread",
            prior=state_store.load(self.spath))
        self.assert_stopped("lead_session_mismatch", case, before)

    def test_a_lead_controller_switch_alone_does_not_reopen_the_lead(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        # The lead's controller changes after the failure: its saved provider
        # session no longer exists, which is not a reason to reopen the lead.
        state_store.switch_role_controller(
            self.spath, PLANNER, "opencode",
            prior=state_store.load(self.spath), reason="cli", source="cli")
        rc, _out, box = self.run_flow()
        stop = self.stop_payload(rc, box)
        self.assertEqual(stop["mismatch"], "lead_session_mismatch")
        self.assertEqual(self.lead_sends(PLANNER, before), 0)

    def test_an_unrelated_invalidation_with_changed_bytes_still_stops(self):
        case = CASES[0]
        record, before = self.prepared(case)
        ids = self.identities_from_record(record)
        self.write_candidate(PLANNER, body="edited")
        self.invalidation_for(dict(ids, work_id="some-other-work"))
        self.invalidation_for(dict(ids, digest="f" * 64))
        self.invalidation_for(dict(ids, session="another-session"))
        self.invalidation_for(dict(ids, session="lead-thread-0"))
        self.assert_stopped("candidate_changed", case, before)

    def test_the_new_attempts_work_id_is_not_a_reopen_reason(self):
        case = CASES[0]
        record, before = self.prepared(case)
        new_id = cowork._role_work_id(self.UUID, PLANNER, record["epoch"], 1)
        self.write_candidate(PLANNER, body="edited")
        self.invalidation_for(dict(self.identities_from_record(record),
                                   work_id=new_id))
        self.assert_stopped("candidate_changed", case, before)

    def test_an_invalidation_recorded_before_the_failure_point_is_ignored(self):
        case = CASES[0]
        record, before = self.prepared(case)
        # The failure was recorded after five invalidations; the matching
        # record below carries an earlier position in the append-only history.
        self.edit_record(ADVISOR, invalidation_seq_at_failure=5)
        self.write_candidate(PLANNER, body="edited")
        stored = self.invalidation_for(self.identities_from_record(record))
        self.assertLess(stored["sequence"], 5)
        self.assert_stopped("candidate_changed", case, before)

    def test_an_invalidation_cannot_link_a_record_without_a_manifest_digest(
            self):
        case = CASES[0]
        record, before = self.prepared(case)
        self.edit_record(ADVISOR, lead=dict(
            record["lead"], candidate_manifest_digest=None))
        self.write_candidate(PLANNER, body="edited")
        self.invalidation_for(dict(self.identities_from_record(record),
                                   digest="b" * 64))
        self.assert_stopped("candidate_changed", case, before)

    def test_a_context_bump_alone_does_not_dissolve_a_changed_candidate(self):
        case = CASES[0]
        _record, before = self.prepared(case)
        self.write_candidate(PLANNER, body="edited")
        rc, _out, box = self.run_flow(["--context", "new goal"])
        stop = self.stop_payload(rc, box)
        self.assertEqual(stop["mismatch"], "candidate_changed")
        self.assertEqual(self.lead_sends(PLANNER, before), 0)

    def test_a_structural_mismatch_is_not_overridden_by_a_linked_reopen(self):
        case = CASES[0]
        record, before = self.prepared(case)
        self.edit_record(ADVISOR, role=PLANNER)
        self.invalidation_for(self.identities_from_record(record))
        self.assert_stopped("wrong_first_role", case, before)


class LinkedReopenTests(_FlowCase):
    """A linked durable invalidation returns the lead; the stop carries the
    identities that build it."""

    def failed(self, case):
        self.make_session(case)
        self.fail_once(case)
        self.log.clear()
        self.verdicts[case["reviewer"]] = [
            {"verdict": "approve", "findings": []}]
        return self.record_of(case), len(self.sessions[case["lead"]])

    def test_a_linked_invalidation_reopens_the_lead_and_retires_the_record(
            self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                record, before = self.failed(case)
                # Identities come from the real writer, never hand-built.
                self.assertTrue(record["lead"]["candidate_manifest_digest"])
                self.assertNotEqual(
                    record["lead"]["work_id"],
                    cowork._role_work_id(self.UUID, lead, record["epoch"], 1))
                self.write_candidate(lead, body="edited")
                self.invalidation_for(self.identities_from_record(record))
                rc, out, _box = self.run_flow()
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.log[0], (lead, "send"))
                self.assertEqual(self.log[1], (reviewer, "review"))
                self.assertEqual(self.lead_sends(lead, before), 1)
                final = state_store.load(self.spath)
                self.assertFalse(state_store.failed_turn_present(final,
                                                                 reviewer))
                retired = final[state_store.FAILED_TURN_RETIREMENTS_KEY][
                    reviewer]
                self.assertEqual(retired[-1]["reason"], "invalidation_record")

    def test_a_reopened_lead_after_a_switch_of_the_lead_runs_first(self):
        case = CASES[0]
        record, before = self.failed(case)
        self.invalidation_for(self.identities_from_record(record))
        state_store.save_role_session(
            self.spath, PLANNER, "codex", "lead-thread-replacement",
            prior=state_store.load(self.spath))
        rc, out, _box = self.run_flow()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log[0], (PLANNER, "send"))

    def test_each_hold_stop_names_identities_that_build_the_exit(self):
        edits = (
            ("candidate_changed",
             lambda: self.write_candidate(PLANNER, body="edited")),
            ("lead_not_ready",
             lambda: self.write_candidate(PLANNER, status="needs_input")),
            ("lead_session_mismatch",
             lambda: state_store.save_role_session(
                 self.spath, PLANNER, "codex", "someone-elses-thread",
                 prior=state_store.load(self.spath))),
        )
        for code, edit in edits:
            with self.subTest(code=code):
                self.setUp()
                case = CASES[0]
                record, before = self.failed(case)
                edit()
                rc, _out, box = self.run_flow()
                stop = self.stop_payload(rc, box)
                self.assertEqual(stop["mismatch"], code)
                self.assertEqual(self.lead_sends(PLANNER, before), 0)
                # Only what the stop names builds the record that reopens.
                self.invalidation_for({
                    "digest": stop["candidate_manifest_digest"],
                    "session": stop["session_uuid"],
                    "work_id": stop["work_id"]})
                self.log.clear()
                rc, out, _box = self.run_flow()
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.log[0], (PLANNER, "send"))
                self.assertFalse(state_store.failed_turn_present(
                    state_store.load(self.spath), ADVISOR))
                self.assertEqual(
                    state_store.load(self.spath)[
                        state_store.FAILED_TURN_RETIREMENTS_KEY][ADVISOR][-1][
                            "reason"], "invalidation_record")

    def test_a_trusted_decision_still_owed_to_the_lead_reopens_it(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                record, before = self.failed(case)
                request_id = self.open_lead_request(case)
                rc, out, _box = self.run_flow(
                    ["--answer", request_id, "--context", "use option a"])
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.log[0], (lead, "send"))
                self.assertEqual(self.lead_sends(lead, before), 1)
                final = state_store.load(self.spath)
                self.assertFalse(state_store.failed_turn_present(final,
                                                                 reviewer))
                retired = final[state_store.FAILED_TURN_RETIREMENTS_KEY][
                    reviewer][-1]
                self.assertEqual(retired["reason"], "pending_lead_decision")
                self.assertEqual(retired["reason_ref"], request_id)
                self.assertEqual(retired["request_sha256"],
                                 record["request"]["sha256"])

    def test_a_stop_payload_carries_identities_and_no_content(self):
        case = CASES[1]
        record, _before = self.failed(case)
        self.write_candidate(BUILDER, body="very-private-body")
        rc, _out, box = self.run_flow()
        stop = self.stop_payload(rc, box)
        self.assertEqual(stop["session_uuid"], self.UUID)
        self.assertEqual(stop["work_id"], record["lead"]["work_id"])
        self.assertEqual(stop["candidate_manifest_digest"],
                         record["lead"]["candidate_manifest_digest"])
        self.assertEqual(stop["invalidation_seq_at_failure"], 0)
        self.assertNotIn("very-private-body", json.dumps(stop))


class OlderEntryRoutingTests(_FlowCase):
    """A reviewer entry written without a recorded failure (a bare pending
    turn or a switch marker) routes reviewer-first only while the lead's
    complete candidate has no usable verdict; nothing is persisted for it."""

    def entry_session(self, case, pending=True, **kwargs):
        self.make_session(case, **kwargs)
        if pending:
            state_store.save_pending_turn(
                self.spath, case["reviewer"], "Review the candidate.",
                prior=state_store.load(self.spath))

    def run_expecting(self, case, first, argv=()):
        self.verdicts[case["reviewer"]] = [
            {"verdict": "approve", "findings": []}]
        rc, out, _box = self.run_flow(argv)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log[0], first)

    def test_a_pending_reviewer_turn_reviews_first(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                self.entry_session(case)
                self.run_expecting(case, (case["reviewer"], "review"))
                self.assertEqual(self.lead_sends(case["lead"]), 0)
                self.assertFalse(state_store.failed_turn_present(
                    state_store.load(self.spath), case["reviewer"]))

    def test_a_switch_marker_reviews_first(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                self.entry_session(case, pending=False)
                self.run_expecting(
                    case, (case["reviewer"], "review"),
                    ["--switch-controller", "%s=codex" % case["reviewer"]])
                self.assertEqual(self.lead_sends(case["lead"]), 0)
                self.assertIn("[controller switch handoff]",
                              self.reviewer_calls[0]["context"])

    def test_a_failure_after_a_derived_route_writes_an_exact_record(self):
        case = CASES[0]
        self.entry_session(case)
        self.verdicts[ADVISOR] = []
        rc, _out, box = self.run_flow()
        self.assertEqual(rc, 1)
        self.assertEqual(box["last"]["payload"]["kind"],
                         "reviewer_unavailable")
        self.assertEqual(self.lead_sends(PLANNER), 0)
        record = self.record_of(case)
        self.assertEqual(record["candidate"]["sha256"],
                         sha256_file(self.plan_json))

    def test_each_unmet_condition_keeps_the_lead_first(self):
        def needs_input(case):
            self.write_candidate(case["lead"], status="needs_input")

        def approved_verdict(case):
            write_json(self.paths(case["lead"])["review"],
                       {"verdict": "approve", "findings": []})

        def revise_verdict(case):
            write_json(self.paths(case["lead"])["review"],
                       {"verdict": "revise", "findings": ["x"]})

        def question_verdict(case):
            write_json(self.paths(case["lead"])["review"],
                       {"verdict": "needs_user", "user_question": "which?"})

        def lead_pending(case):
            state_store.save_pending_turn(
                self.spath, case["lead"], "lead failed turn",
                prior=state_store.load(self.spath))
        for name, edit in (("not ready", needs_input),
                           ("approve verdict", approved_verdict),
                           ("revise verdict", revise_verdict),
                           ("question verdict", question_verdict),
                           ("lead pending", lead_pending)):
            with self.subTest(condition=name):
                self.setUp()
                case = CASES[0]
                self.entry_session(case)
                edit(case)
                self.verdicts[ADVISOR] = [
                    {"verdict": "approve", "findings": []}]
                rc, out, _box = self.run_flow()
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.log[0], (PLANNER, "send"))

    def test_an_unusable_review_file_still_reviews_first(self):
        # A verdict file the reviewer never finished writing is no usable
        # verdict: the candidate is still unreviewed.
        for name, content in (("unparseable", "{ not json"),
                              ("unknown verdict", '{"verdict": "maybe"}'),
                              ("question without text",
                               '{"verdict": "needs_user"}')):
            with self.subTest(review=name):
                self.setUp()
                case = CASES[0]
                self.entry_session(case)
                write_text(self.paths(PLANNER)["review"], content)
                self.run_expecting(case, (ADVISOR, "review"))
                self.assertEqual(self.lead_sends(PLANNER), 0)

    def test_a_lead_capacity_hold_never_routes_reviewer_first(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                self.entry_session(case)
                state_store.write_pending_turn_before_pause(
                    self.UUID, case["lead"], "a paused lead turn")
                self.verdicts[case["reviewer"]] = [
                    {"verdict": "approve", "findings": []}]
                self.run_flow()
                self.assertNotIn(
                    "reviewer_first",
                    [e["route"] for e in self.route_events()])
                self.assertNotEqual(self.log[:1],
                                    [(case["reviewer"], "review")])

    def test_a_trusted_lead_decision_still_owed_keeps_the_lead_first(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                self.entry_session(case)
                request_id = self.open_lead_request(case)
                self.verdicts[case["reviewer"]] = [
                    {"verdict": "approve", "findings": []}]
                rc, out, _box = self.run_flow(
                    ["--answer", request_id, "--context", "use option a"])
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.log[0], (case["lead"], "send"))
                self.assertNotIn(
                    "reviewer_first",
                    [e["route"] for e in self.route_events()])

    def test_a_missing_lead_session_keeps_the_lead_first(self):
        case = CASES[0]
        self.entry_session(case, lead_session=False)
        self.verdicts[ADVISOR] = [{"verdict": "approve", "findings": []}]
        # No saved lead session: the lead starts fresh from the approved intel.
        write_json(cowork.scout_intel_path(self.assets, self.UUID),
                   {"status": "ready_for_review", "result": {}})
        write_text(state_store.scout_intel_md_path_for(self.assets, self.UUID),
                   "# intel\n")
        rc, out, _box = self.run_flow()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log[0], (PLANNER, "send"))


class UnchangedPathTests(_FlowCase):
    def test_a_fresh_run_never_passes_a_binding(self):
        for case in CASES:
            with self.subTest(lead=case["lead"]):
                self.setUp()
                lead, reviewer = case["lead"], case["reviewer"]
                self.make_session(case, with_candidate=False)
                self.verdicts[reviewer] = [
                    {"verdict": "approve", "findings": []}]
                rc, out, _box = self.run_flow()
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.log, [(lead, "send"),
                                            (reviewer, "review")])
                for kwargs in self.captured[lead]:
                    self.assertNotIn("reviewer_first", kwargs)
                    self.assertNotIn("lead_context_block_fn", kwargs)
                    self.assertTrue(callable(kwargs["failed_turn_fn"]))

    def test_a_lead_failure_recovery_still_resumes_the_lead(self):
        case = CASES[0]
        self.make_session(case, with_candidate=False)
        state_store.save_pending_turn(
            self.spath, PLANNER, "the lead's own failed turn",
            prior=state_store.load(self.spath))
        self.verdicts[ADVISOR] = [{"verdict": "approve", "findings": []}]
        rc, out, _box = self.run_flow()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.log[0], (PLANNER, "send"))
        self.assertNotIn("reviewer_first", self.captured[PLANNER][0])

    def test_the_capacity_resume_trigger_does_not_consult_the_recovery_route(
            self):
        source = inspect.getsource(cowork.run_resume_trigger)
        for name in ("reviewer_first", "failed_turn", "recovery_route_for"):
            self.assertNotIn(name, source)
        self.assertEqual(cowork.RESUME_TRIGGER_EXIT_CODES["invalidated"], 7)
        self.assertEqual(cowork.RESUME_TRIGGER_EXIT_CODES["no_pending_turn"], 8)


if __name__ == "__main__":
    unittest.main()
