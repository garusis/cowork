#!/usr/bin/env python3
"""Tests for the strict recording of reviewer verdicts at the `_role_loop`
verdict boundary: one authority-chain round per usable verdict, its typed
findings recorded against the reviewed candidate, and the round surfaces
(trace, eval closures, round_epochs, ledger mirror) that carry that round.

Fake lead sessions and fake reviewer runners replace the controllers.
COWORK_SESSIONS_ROOT is pinned to a temp directory, session ids are fresh
UUIDs and every input is neutral and synthetic. No live provider is used.

Run: python3 scripts/cowork_offline_tests.py test_authority_flow_recording
"""

import ast
import hashlib
import inspect
import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_authority_candidate as candidate_mod  # noqa: E402
import cowork_authority_chain as chain  # noqa: E402
import cowork_eval  # noqa: E402
import cowork_finding_lifecycle as lifecycle  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_owner  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

PHASE_ROLES = candidate_mod.PHASE_ROLES
HEX = "0123456789abcdef" * 4
HEX2 = "fedcba9876543210" * 4
SCOUTING, PLANNING, BUILDING = "scouting", "planning", "building"
CAND = {"kind": "status_artifact", "status_sha256": HEX}
TXN = "T-green"


def _sha(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _finding(summary="a gap", severity="blocking", **extra):
    entry = {"summary": summary, "severity": severity, "criterion": "scope"}
    entry.update(extra)
    return entry


def _revise(*findings):
    return {"verdict": "revise", "findings": ["see typed findings"],
            "corrective_findings": list(findings)}


APPROVE = {"verdict": "approve"}
NEEDS_USER = {"verdict": "needs_user", "user_question": "which scope?"}
UNUSABLE = {}


class _LeadSession:
    """A lead whose every send leaves a fresh `ready_for_review` status."""

    def __init__(self, status_path, turns, on_send=None):
        self.status_path = status_path
        self.turns = turns
        self.on_send = on_send
        self.sent = []

    def send(self, text, meta=None):
        self.sent.append(text)
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        with open(self.status_path, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"turn": next(self.turns)}}, fh)
        if self.on_send is not None:
            self.on_send(len(self.sent))
        return {"ok": True, "result": "ok"}

    def close(self):
        pass


class _Baseline:
    """A reviewer hash-gate baseline: eligible once an approve was recorded."""

    def __init__(self):
        self.approved = False

    def compute_composite(self):
        return "composite"

    def eligible(self, composite):
        return self.approved

    def record(self, composite):
        self.approved = True


class _RejectingProfile:
    """An execution profile whose review contract rejects every verdict."""

    def on_verdict(self, reviewer_role, verdict):
        return None

    def screen_verdict(self, role, verdict):
        return "contract_misuse"

    def on_round_cap(self, role):
        return None


class FlowEnv(unittest.TestCase):
    """A pinned sessions root, one session, and a driver for `_role_loop`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sessions_root = os.path.join(self.tmp, "sessions")
        patcher = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.sessions_root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sid = str(uuid.uuid4())
        self.assets = state_store.session_assets_dir(self.sid)
        os.makedirs(self.assets)
        self.work = self.assets
        self.trace = trace_store.Trace(
            os.path.join(self.assets, "trace.jsonl"),
            session_uuid=self.sid, run_id="R")
        self.chain_path = state_store.authority_chain_path_for(self.sid)
        self.turns = itertools.count(1)

    # ---- paths and reads
    def status_path(self, phase):
        return os.path.join(self.work, "%s.status.json" % PHASE_ROLES[phase][0])

    def review_path(self, phase):
        return os.path.join(self.work, "%s.review.json" % PHASE_ROLES[phase][1])

    def events(self, name=None):
        path = os.path.join(self.assets, "trace.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if name is None or r["event"] == name]

    def chain_read(self):
        return chain.read_chain(self.chain_path)

    def folded(self):
        read = self.chain_read()
        self.assertTrue(read["ok"], read["error"])
        return read["folded"]

    def chain_records(self, kind=None):
        read = self.chain_read()
        self.assertTrue(read["ok"], read["error"])
        return [r for r in read["records"] if kind is None or r["kind"] == kind]

    def af_ids(self):
        return [r["id"] for r in self.chain_records("finding")]

    def queue(self, seat=None):
        rows = [q for q in cowork_eval.read_queue(
            state_store.evaluation_queue_path_for(self.sid))
            if q.get("state") == "pending"]
        return [q for q in rows if seat is None or q["evaluator_seat"] == seat]

    def ledger_findings(self):
        return [r for r in ledger.read_ledger(
            state_store.ledger_path_for(self.sid)) if r.get("kind") == "finding"]

    def epochs(self):
        path = os.path.join(self.assets, "round_epochs.json")
        if not os.path.exists(path):
            return {}
        with open(path) as fh:
            return json.load(fh)

    def write_pointer(self, pointer):
        path = state_store.current_receipt_pointer_path_for(self.sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            if isinstance(pointer, str):
                fh.write(pointer)
            else:
                json.dump(pointer, fh)

    def owned_pointer(self, **extra):
        pointer = {"transaction_id": TXN, "manifest_digest": HEX,
                   "index_digest": HEX2, "disposition": "pending_review"}
        pointer.update(extra)
        return pointer

    def status_sha(self, phase):
        with open(self.status_path(phase), "rb") as fh:
            return _sha(fh.read())

    def lose_chain(self):
        """Remove only the chain file and its lock."""
        for path in (self.chain_path, self.chain_path + ".lock"):
            if os.path.exists(path):
                os.unlink(path)

    def reset_session_authority(self):
        """Emulate a brand-new session: no chain, no trace, no ledger."""
        self.lose_chain()
        for path in (os.path.join(self.assets, "trace.jsonl"),
                     state_store.ledger_path_for(self.sid)):
            if os.path.exists(path):
                os.unlink(path)

    def seed_epochs(self, value, phase=SCOUTING):
        lead, reviewer = PHASE_ROLES[phase]
        with open(os.path.join(self.assets, "round_epochs.json"), "w") as fh:
            json.dump({"%s|%s" % (phase, lead): value,
                       "%s|%s" % (phase, reviewer): value}, fh)

    # ---- driving the loop
    def drive(self, phase, script, eval_policy="all_rounds", eval_on=True,
              session=True, loop_kwargs=None, pointer=None):
        """Run one `_role_loop` over scripted reviewer verdicts (a resume is a
        second call). A script entry is a verdict dict or a callable taking the
        status path. Returns (outcome, payload, lead session)."""
        lead, reviewer = PHASE_ROLES[phase]
        status_path = self.status_path(phase)
        review_path = self.review_path(phase)
        os.makedirs(self.work, exist_ok=True)
        sid = self.sid if session else None
        scratch = os.path.join(self.assets, "eval.%s.json" % reviewer)
        scores = state_store.scores_path_for(self.sid)
        script = list(script)

        def runner(config, context, selected, artifact_path, review_file,
                   resume_id=None, on_session=None, context_update=None,
                   eval_scratch_path=None, eval_specs=None, **kw):
            entry = script.pop(0)
            verdict = entry(status_path) if callable(entry) else entry
            if verdict:
                with open(review_file, "w") as fh:
                    json.dump(verdict, fh)
            return json.loads(json.dumps(verdict))

        review_fn = cowork.make_review_fn(
            cowork.default_config([lead, reviewer]), "ctx", [lead, reviewer],
            review_path, reviewer_runner=runner, reviewer_role=reviewer,
            phase=phase, trace=self.trace,
            eval_scratch_path=scratch if eval_on else None,
            scores_path=scores if eval_on else None,
            session_uuid=sid, evaluation_policy=eval_policy)
        evaluate_fn = None
        if eval_on and session:
            evaluate_fn = cowork._make_enqueue_eval_fn(
                lead, reviewer, phase, scratch, scores, sid,
                trace=self.trace, review_path=review_path,
                artifact_path=status_path, evaluation_policy=eval_policy)
        lead_session = _LeadSession(status_path, self.turns)
        kwargs = dict(role=lead, review_fn=review_fn, trace=self.trace,
                      reviewer_role=reviewer, evaluate_fn=evaluate_fn,
                      phase=phase, review_path=review_path, session_uuid=sid)
        kwargs.update(loop_kwargs or {})
        if pointer is not None:
            self.write_pointer(pointer)
        with mock.patch.object(
                cowork, "_run_owned_verification_transaction",
                return_value=(None, None)):
            rc, outcome, payload = cowork._role_loop(
                lead_session, "seed", status_path, "", io.StringIO(), **kwargs)
        self.assertEqual(rc, 0)
        return outcome, payload, lead_session


class CharacterizationTests(FlowEnv):
    """Neutral behavior that holds before and after the recording wiring:
    durable eval numbering for usable verdicts, sampled selection and the
    trace order of the fix handoff."""

    def test_eval_on_numbering_continues_across_a_resume(self):
        first = self.drive(
            SCOUTING, [_revise(_finding("one", "major")), NEEDS_USER])
        self.assertEqual(first[0], "stopped")
        second = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(second[0], "approved")
        for seat in ("scout", "scout-reviewer"):
            self.assertEqual([q["round"] for q in self.queue(seat)], [1, 2, 3])
        # the in-loop counter restarts at the resume; the durable one does not
        self.assertEqual(
            [q["loop_round"] for q in self.queue("scout-reviewer")], [1, 2, 1])

    def test_sampled_selection_follows_the_durable_round(self):
        for script in ([_revise(_finding("one", "major")),
                        _revise(_finding("two", "major")), NEEDS_USER],
                       [_revise(_finding("three", "major")), APPROVE]):
            self.drive(PLANNING, script, eval_policy="sampled")
        for seat in ("planner", "planning-advisor"):
            self.assertEqual([q["round"] for q in self.queue(seat)], [1, 3, 5])
        skipped = sorted((e["evaluator"], e["round"])
                         for e in self.events("eval.skipped"))
        self.assertEqual(skipped, [("planner", 2), ("planner", 4),
                                   ("planning-advisor", 2),
                                   ("planning-advisor", 4)])

    def test_eval_off_session_numbers_rounds_from_one(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")),
                              _revise(_finding("two", "major")), APPROVE],
                   eval_on=False)
        self.assertEqual([r["round"] for r in self.ledger_findings()], [1, 2])
        self.assertEqual(
            [e["round"] for e in self.events("review.handoff.recorded")],
            [1, 2])

    def test_eval_enqueue_precedes_the_fix_handoff(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")), APPROVE])
        names = [e["event"] for e in self.events()]
        self.assertIn("eval.enqueued", names)
        self.assertIn("review.handoff.recorded", names)
        self.assertLess(names.index("eval.enqueued"),
                        names.index("review.handoff.recorded"))


class StateHelperTests(FlowEnv):
    """The additive session-state helpers the recording relies on."""

    def test_authority_paths_live_in_the_session_assets(self):
        self.assertEqual(state_store.authority_chain_path_for(self.sid),
                         os.path.join(self.assets, "authority_chain.jsonl"))
        self.assertEqual(state_store.authority_verdict_copy_dir_for(self.sid),
                         os.path.join(self.assets, "authority_verdicts"))

    def test_sync_sets_every_seat_and_keeps_other_keys(self):
        with open(os.path.join(self.assets, "round_epochs.json"), "w") as fh:
            json.dump({"planning|planner": 3}, fh)
        self.assertTrue(state_store.sync_phase_round(
            self.sid, SCOUTING, ("scout", "scout-reviewer"), 4))
        self.assertEqual(self.epochs(), {"planning|planner": 3,
                                         "scouting|scout": 4,
                                         "scouting|scout-reviewer": 4})
        self.assertEqual(
            state_store.current_phase_round(self.sid, SCOUTING, "scout"), 4)
        self.assertEqual(
            state_store.next_phase_round(self.sid, SCOUTING, "scout"), 5)

    def test_sync_refuses_unusable_input(self):
        seats = ("scout", "scout-reviewer")
        self.assertFalse(state_store.sync_phase_round(
            None, SCOUTING, seats, 1))
        self.assertFalse(state_store.sync_phase_round(
            self.sid, SCOUTING, seats, "1"))
        self.assertFalse(state_store.sync_phase_round(
            self.sid, SCOUTING, seats, True))
        self.assertEqual(self.epochs(), {})


class GateCandidateTests(FlowEnv):
    """`_gate_candidate` is the single filler of the reviewed candidate."""

    def status(self, phase, turn):
        path = self.status_path(phase)
        with open(path, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {"turn": turn}},
                      fh)
        return path

    def status_candidate(self, phase):
        return {"kind": "status_artifact",
                "status_sha256": self.status_sha(phase)}

    def test_scout_and_planner_use_the_status_artifact(self):
        for phase in (SCOUTING, PLANNING):
            path = self.status(phase, 1)
            lead = PHASE_ROLES[phase][0]
            self.assertEqual(
                cowork._gate_candidate(self.sid, phase, lead, path),
                (self.status_candidate(phase), None))

    def test_builder_without_a_pointer_uses_the_status_artifact(self):
        path = self.status(BUILDING, 1)
        self.assertEqual(
            cowork._gate_candidate(self.sid, BUILDING, "builder", path),
            (self.status_candidate(BUILDING), "pointer_absent"))

    def test_checkpoint_pointer_is_ignored_without_a_stop(self):
        path = self.status(BUILDING, 1)
        self.write_pointer({"checkpoint_id": "C-1", "manifest_digest": HEX})
        self.assertEqual(
            cowork._gate_candidate(self.sid, BUILDING, "builder", path),
            (self.status_candidate(BUILDING), "checkpoint_pointer_ignored"))

    def test_owned_receipt_pointer_names_the_receipt_digests(self):
        path = self.status(BUILDING, 1)
        self.write_pointer(self.owned_pointer())
        self.assertEqual(
            cowork._gate_candidate(self.sid, BUILDING, "builder", path),
            ({"kind": "owned_receipt", "manifest_digest": HEX,
              "index_digest": HEX2}, "owned_receipt"))

    def test_a_rejected_transaction_pointer_still_yields_the_receipt(self):
        path = self.status(BUILDING, 1)
        self.write_pointer(self.owned_pointer(disposition="rejected"))
        candidate, reason = cowork._gate_candidate(
            self.sid, BUILDING, "builder", path)
        self.assertEqual((candidate["kind"], reason),
                         ("owned_receipt", "owned_receipt"))

    def test_a_corrupt_pointer_file_has_no_candidate(self):
        path = self.status(BUILDING, 1)
        self.write_pointer("{not json")
        self.assertEqual(
            cowork._gate_candidate(self.sid, BUILDING, "builder", path),
            (None, "pointer_malformed"))

    def test_dispatch_manifest_digest_never_moves_the_candidate(self):
        path = self.status(BUILDING, 1)
        self.write_pointer(self.owned_pointer())
        before = cowork._gate_candidate(self.sid, BUILDING, "builder", path)
        manifest = state_store.manifest_path_for(self.sid, "builder")
        os.makedirs(os.path.dirname(manifest), exist_ok=True)
        for digest in ("a" * 64, "b" * 64):
            with open(manifest, "w") as fh:
                json.dump({"digest": digest}, fh)
            self.assertEqual(
                cowork._gate_candidate(self.sid, BUILDING, "builder", path),
                before)

    def test_a_newly_bound_receipt_changes_the_candidate(self):
        path = self.status(BUILDING, 1)
        self.write_pointer(self.owned_pointer())
        first = cowork._gate_candidate(self.sid, BUILDING, "builder", path)
        self.write_pointer(self.owned_pointer(
            transaction_id="T-next", manifest_digest=HEX2, index_digest=HEX))
        second = cowork._gate_candidate(self.sid, BUILDING, "builder", path)
        self.assertNotEqual(first[0], second[0])

    def test_status_touch_up_moves_scout_but_not_a_bound_builder(self):
        builder_path = self.status(BUILDING, 1)
        self.write_pointer(self.owned_pointer())
        builder = cowork._gate_candidate(
            self.sid, BUILDING, "builder", builder_path)
        scout_path = self.status(SCOUTING, 1)
        scout = cowork._gate_candidate(self.sid, SCOUTING, "scout", scout_path)
        self.status(BUILDING, 2)
        self.status(SCOUTING, 2)
        self.assertEqual(
            cowork._gate_candidate(self.sid, BUILDING, "builder", builder_path),
            builder)
        self.assertNotEqual(
            cowork._gate_candidate(self.sid, SCOUTING, "scout", scout_path)[0],
            scout[0])

    def test_tree_state_and_dispatch_manifests_are_never_consulted(self):
        path = self.status(BUILDING, 1)
        self.write_pointer(self.owned_pointer())
        with mock.patch.object(cowork, "_current_tree_digest",
                               side_effect=AssertionError("tree digest")), \
                mock.patch.object(cowork.dispatch_manifest, "load_manifest",
                                  side_effect=AssertionError("manifest")):
            candidate, _reason = cowork._gate_candidate(
                self.sid, BUILDING, "builder", path)
        self.assertEqual(candidate["kind"], "owned_receipt")

    def test_a_role_outside_the_phase_pair_has_no_candidate(self):
        path = self.status(SCOUTING, 1)
        self.assertEqual(
            cowork._gate_candidate(self.sid, SCOUTING, "builder", path),
            (None, "candidate_unavailable"))

    def test_the_carried_approval_is_re_captured_against_fresh_bytes(self):
        baseline = _Baseline()
        kwargs = {"skip_baseline": baseline}
        self.drive(SCOUTING, [APPROVE], eval_on=False, loop_kwargs=kwargs)
        first = self.chain_records("round")[0]["candidate"]
        # a carried approval runs no reviewer and mints nothing
        outcome, _payload, _sess = self.drive(
            SCOUTING, [], eval_on=False, loop_kwargs=kwargs)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(self.chain_records("round")), 1)
        # once the baseline no longer matches, the reviewer judges fresh bytes
        baseline.approved = False
        self.drive(SCOUTING, [APPROVE], eval_on=False, loop_kwargs=kwargs)
        rounds = self.chain_records("round")
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[1]["candidate"]["status_sha256"],
                         self.status_sha(SCOUTING))
        self.assertNotEqual(rounds[1]["candidate"], first)


class ChainAppendAndOwnerFenceTests(FlowEnv):
    """`_chain_append` is the one owner-fenced writer of the chain."""

    def body(self):
        return {"kind": "round", "session_uuid": self.sid, "phase": SCOUTING,
                "round": None, "candidate": dict(CAND),
                "seat": "scout-reviewer", "verdict_sha256": HEX,
                "review_path": "/review.json", "verdict_copy_path": "/copy.json",
                "verdict_copy_sha256": HEX, "op_key": "k:0"}

    def revoked(self, armed):
        context = {"enforced": True, "session_uuid": self.sid,
                   "owner_id": "o", "epoch": 1, "provider_conflict": None,
                   "matched": True}

        def assert_owner(*args):
            if armed["revoked"]:
                raise cowork_owner.OwnerLeaseError("superseded")

        patcher = mock.patch.dict(cowork._OWNER_CONTEXT, context)
        patcher.start()
        self.addCleanup(patcher.stop)
        owner = mock.patch.object(
            cowork_owner, "assert_owner", side_effect=assert_owner)
        owner.start()
        self.addCleanup(owner.stop)

    def untouched(self):
        self.assertFalse(os.path.exists(self.chain_path))
        self.assertFalse(os.path.exists(self.chain_path + ".lock"))
        self.assertFalse(os.path.exists(
            state_store.authority_verdict_copy_dir_for(self.sid)))

    def test_append_commits_through_the_store(self):
        committed = cowork._chain_append(self.sid, [self.body()])
        self.assertEqual((committed[0]["id"], committed[0]["round"]),
                         ("RD-0001", 1))
        self.assertTrue(self.chain_read()["ok"])

    def test_a_superseded_lease_writes_no_chain_record(self):
        self.revoked({"revoked": True})
        with self.assertRaises(cowork_owner.OwnerLeaseError):
            cowork._chain_append(self.sid, [self.body()])
        self.untouched()

    def test_an_unenforced_lease_changes_nothing(self):
        committed = cowork._chain_append(self.sid, [self.body()])
        self.assertEqual(len(committed), 1)

    def test_a_superseded_lease_at_the_boundary_leaves_nothing_behind(self):
        armed = {"revoked": False}
        self.revoked(armed)

        def reviewer(path):
            armed["revoked"] = True
            return dict(APPROVE)

        with self.assertRaises(cowork_owner.OwnerLeaseError):
            self.drive(SCOUTING, [reviewer], eval_on=False)
        self.untouched()

    def test_a_lease_that_holds_records_the_round(self):
        armed = {"revoked": False}
        self.revoked(armed)
        outcome, _payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(self.chain_records("round")), 1)


class VerdictRecordingFlowTests(FlowEnv):
    """Every usable verdict mints exactly one round, whatever it decides."""

    def test_an_approve_mints_one_round_bound_to_the_reviewed_status(self):
        outcome, _payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")
        rounds = self.chain_records("round")
        self.assertEqual(len(rounds), 1)
        self.assertEqual(
            (rounds[0]["phase"], rounds[0]["seat"], rounds[0]["round"]),
            (SCOUTING, "scout-reviewer", 1))
        self.assertEqual(
            rounds[0]["candidate"],
            {"kind": "status_artifact",
             "status_sha256": self.status_sha(SCOUTING)})

    def test_the_round_names_the_review_file_and_a_verdict_copy(self):
        verdict = _revise(_finding("one", "major"))
        self.drive(SCOUTING, [verdict, APPROVE], eval_on=False)
        record = self.chain_records("round")[0]
        copy_path = record["verdict_copy_path"]
        self.assertEqual(
            os.path.dirname(copy_path),
            state_store.authority_verdict_copy_dir_for(self.sid))
        with open(copy_path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(_sha(raw), record["verdict_copy_sha256"])
        self.assertEqual(os.path.basename(copy_path),
                         record["verdict_copy_sha256"] + ".json")
        self.assertEqual(json.loads(raw.decode("utf-8")), verdict)
        self.assertEqual(record["review_path"], self.review_path(SCOUTING))

    def test_each_verdict_kind_mints_exactly_one_round(self):
        cases = (
            ("approve", [APPROVE], 1, "approved", None),
            ("revise", [_revise(_finding("x", "major")), APPROVE], 2,
             "approved", None),
            ("needs_user", [NEEDS_USER], 1, "stopped", "reviewer_question"),
            ("round_cap",
             [_revise(_finding("again"))] * cowork.REVIEW_ROUND_CAP,
             cowork.REVIEW_ROUND_CAP, "stopped", "review_round_cap"))
        for name, script, rounds, outcome, kind in cases:
            with self.subTest(verdict=name):
                self.tearDown_chain()
                got, payload, _sess = self.drive(
                    SCOUTING, script, eval_on=False)
                self.assertEqual(got, outcome)
                if kind:
                    self.assertEqual(payload["kind"], kind)
                self.assertEqual(len(self.chain_records("round")), rounds)

    def tearDown_chain(self):
        self.reset_session_authority()

    def test_a_profile_rejected_verdict_still_mints_its_round(self):
        outcome, payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False,
            loop_kwargs={"profile_session": _RejectingProfile()})
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "review_profile_rejected")
        self.assertEqual(len(self.chain_records("round")), 1)

    def test_unusable_verdicts_mint_nothing(self):
        outcome, payload, _sess = self.drive(
            SCOUTING, [UNUSABLE, UNUSABLE], eval_on=False)
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "reviewer_unavailable")
        self.assertFalse(os.path.exists(self.chain_path))

    def test_a_retry_after_a_failed_turn_mints_one_round(self):
        outcome, _payload, _sess = self.drive(
            SCOUTING, [UNUSABLE, APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(self.chain_records("round")), 1)

    def test_a_failed_turn_queues_no_reviewer_entry_and_burns_no_number(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")), UNUSABLE,
                              APPROVE])
        self.assertEqual(
            [q["round"] for q in self.queue("scout-reviewer")], [1, 2])
        self.assertEqual([q["round"] for q in self.queue("scout")], [1, 2])
        self.assertEqual(len(self.chain_records("round")), 2)

    def test_a_resume_continues_the_numbering(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")), NEEDS_USER],
                   eval_on=False)
        self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(
            [r["round"] for r in self.chain_records("round")], [1, 2, 3])

    def test_every_phase_records_in_its_own_numbering(self):
        for phase in (SCOUTING, PLANNING, BUILDING):
            self.drive(phase, [APPROVE], eval_on=False)
        rounds = self.chain_records("round")
        self.assertEqual([(r["phase"], r["seat"], r["round"]) for r in rounds],
                         [(SCOUTING, "scout-reviewer", 1),
                          (PLANNING, "planning-advisor", 1),
                          (BUILDING, "build-reviewer", 1)])

    def test_a_builder_with_a_checkpoint_pointer_is_not_stopped(self):
        # an earlier verdict makes the chain non-empty before the builder's
        self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(len(self.chain_records("round")), 1)
        challenge = _finding("the receipt is wrong")
        challenge["verification_challenge"] = {"reason_code": "no_evidence"}
        outcome, payload, _sess = self.drive(
            BUILDING, [_revise(challenge)], eval_on=False,
            pointer={"checkpoint_id": "C-1"})
        self.assertEqual((outcome, payload["kind"]),
                         ("stopped", "review_not_approved"))
        rounds = self.chain_records("round")
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[1]["phase"], BUILDING)
        self.assertEqual(
            rounds[1]["candidate"],
            {"kind": "status_artifact",
             "status_sha256": self.status_sha(BUILDING)})

    def test_typed_findings_carry_the_round_id_and_candidate(self):
        self.drive(SCOUTING, [_revise(_finding("one"), _finding("two", "minor")),
                              NEEDS_USER], eval_on=False)
        round_record = self.chain_records("round")[0]
        findings = self.chain_records("finding")
        self.assertEqual([f["summary"] for f in findings], ["one", "two"])
        for finding in findings:
            self.assertEqual(finding["round_id"], round_record["id"])
            self.assertEqual(finding["round"], 1)
            self.assertEqual(finding["candidate"], round_record["candidate"])
            self.assertEqual(finding["discoverer"], "scout-reviewer")
        self.assertEqual([f["blocking"] for f in findings], [True, False])


class NormalizationTests(FlowEnv):
    """Reviewer-typed findings become store-legal records without invention."""

    def plan(self, verdict, review_path=None, **kwargs):
        folded = {"rounds": [], "findings": {}}
        return cowork._plan_verdict_records(
            self.sid, SCOUTING, "scout-reviewer", verdict, folded, None, 1,
            CAND, review_path, "/copy.json", HEX, HEX, **kwargs)

    def bodies(self, plan, kind):
        return [b for b in plan["records"] if b["kind"] == kind]

    def test_only_typed_findings_become_records(self):
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            None, "prose", {}, {"unrelated": 1}, {"summary": "keep"}]})
        self.assertEqual([b["kind"] for b in plan["records"]],
                         ["round", "finding"])
        self.assertEqual(plan["new_ids"], {4: "AF-0002"})

    def test_missing_fields_get_neutral_defaults(self):
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            {"summary": "keep"}]})
        finding = self.bodies(plan, "finding")[0]
        self.assertEqual(finding["severity"], "unspecified")
        self.assertEqual(finding["criterion"], "")
        self.assertFalse(finding["blocking"])
        self.assertEqual(finding["discoverer"], "scout-reviewer")
        self.assertIsNone(finding["claim_class"])
        self.assertEqual(finding["evidence_path"], "/copy.json")
        self.assertEqual(finding["evidence_sha256"], HEX)

    def test_non_text_values_are_replaced_not_invented(self):
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            {"summary": 5, "severity": "blocking", "criterion": None},
            {"summary": "x", "severity": ""}]})
        first, second = self.bodies(plan, "finding")
        self.assertEqual((first["summary"], first["criterion"]), ("", ""))
        self.assertTrue(first["blocking"])
        self.assertEqual(second["severity"], "unspecified")

    def test_evidence_follows_the_reviewer_when_well_formed(self):
        path = os.path.join(self.tmp, "evidence.txt")
        with open(path, "w") as fh:
            fh.write("evidence bytes")
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            {"summary": "a", "severity": "major", "evidence_path": path,
             "evidence_sha256": "not-hex"},
            {"summary": "b", "severity": "major", "evidence_path": path,
             "evidence_sha256": HEX2}]})
        first, second = self.bodies(plan, "finding")
        self.assertEqual(first["evidence_path"], path)
        self.assertEqual(first["evidence_sha256"], _sha("evidence bytes"))
        self.assertEqual(second["evidence_sha256"], HEX2)

    def test_the_review_file_stands_in_for_missing_evidence(self):
        path = os.path.join(self.tmp, "review.json")
        with open(path, "w") as fh:
            fh.write('{"verdict": "revise"}')
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            {"summary": "a", "severity": "major"}]}, review_path=path)
        finding = self.bodies(plan, "finding")[0]
        self.assertEqual(finding["evidence_path"], path)
        self.assertEqual(finding["evidence_sha256"],
                         _sha('{"verdict": "revise"}'))

    def test_every_planned_record_is_legal_for_the_store(self):
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            {"summary": "a", "severity": "blocking"},
            {"severity": "minor"}, {"finding_ref": "AF-0099"}]})
        self.assertEqual(len(plan["records"]), 4)
        for body in plan["records"]:
            self.assertEqual(chain.validate_record(body), (True, None), body)

    def test_the_round_record_comes_first_with_its_own_op_keys(self):
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            {"summary": "a"}, {"summary": "b"}]})
        self.assertEqual(plan["records"][0]["kind"], "round")
        keys = [b["op_key"] for b in plan["records"]]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(plan["expected_head"], chain.EMPTY_HEAD)


class BlockingFlagTests(FlowEnv):
    """`blocking` follows the store's rule; only a defeated challenge cited
    against a real transaction is superseded."""

    def challenge(self, cited=None):
        entry = _finding("the receipt is wrong")
        entry["verification_challenge"] = (
            {"transaction_id": cited, "reason_code": "stale"} if cited
            else {"reason_code": "no_evidence"})
        return entry

    def record(self, phase, findings, pointer=None, script_tail=()):
        self.drive(phase, [_revise(*findings)] + list(script_tail),
                   eval_on=False, pointer=pointer)
        return self.chain_records("finding")

    def test_all_defeated_challenges_are_superseded_and_not_blocking(self):
        findings = self.record(BUILDING, [self.challenge()],
                               pointer=self.owned_pointer())
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0]["blocking"])
        self.assertEqual(findings[0]["superseded_by_transaction"], TXN)

    def test_a_contradicted_challenge_is_superseded_too(self):
        findings = self.record(BUILDING, [self.challenge("T-other")],
                               pointer=self.owned_pointer())
        self.assertEqual(
            [(f["blocking"], f["superseded_by_transaction"])
             for f in findings], [(False, TXN)])

    def test_a_validly_cited_challenge_stays_blocking(self):
        findings = self.record(
            BUILDING, [self.challenge(TXN)], pointer=self.owned_pointer(),
            script_tail=[UNUSABLE, UNUSABLE])
        self.assertEqual(
            [(f["blocking"], f["superseded_by_transaction"])
             for f in findings], [(True, None)])

    def test_a_mixed_revise_keeps_the_defeated_challenge_blocking(self):
        findings = self.record(
            BUILDING, [self.challenge(), _finding("out of plan")],
            pointer=self.owned_pointer(), script_tail=[UNUSABLE, UNUSABLE])
        self.assertEqual(
            [(f["blocking"], f["superseded_by_transaction"])
             for f in findings], [(True, None), (True, None)])

    def test_a_non_verification_blocking_finding_is_blocking(self):
        findings = self.record(
            BUILDING, [_finding("out of plan")], pointer=self.owned_pointer(),
            script_tail=[UNUSABLE, UNUSABLE])
        self.assertEqual([f["blocking"] for f in findings], [True])

    def test_a_lower_severity_is_never_blocking(self):
        findings = self.record(
            BUILDING, [_finding("nit", "minor"), _finding("big", "major")],
            pointer=self.owned_pointer(), script_tail=[UNUSABLE, UNUSABLE])
        self.assertEqual([f["blocking"] for f in findings], [False, False])

    def test_a_pointer_without_a_transaction_supersedes_nothing(self):
        findings = self.record(BUILDING, [self.challenge()],
                               pointer={"checkpoint_id": "C-1"})
        self.assertEqual(
            [(f["blocking"], f["superseded_by_transaction"])
             for f in findings], [(True, None)])

    def test_without_a_pointer_a_challenge_is_an_ordinary_finding(self):
        findings = self.record(BUILDING, [self.challenge()],
                               script_tail=[UNUSABLE, UNUSABLE])
        self.assertEqual(
            [(f["blocking"], f["superseded_by_transaction"])
             for f in findings], [(True, None)])

    def test_a_non_builder_role_never_supersedes(self):
        findings = self.record(SCOUTING, [self.challenge()],
                               script_tail=[NEEDS_USER])
        self.assertEqual(
            [(f["blocking"], f["superseded_by_transaction"])
             for f in findings], [(True, None)])

    def test_the_planned_rule_matches_the_store_rule(self):
        folded = {"rounds": [], "findings": {}}
        defeated = {"summary": "a", "severity": "blocking"}
        valid = {"summary": "b", "severity": "blocking"}
        verdict = {"verdict": "revise",
                   "corrective_findings": [defeated, valid]}
        plan = cowork._plan_verdict_records(
            self.sid, BUILDING, "build-reviewer", verdict, folded, None, 1,
            CAND, None, "/copy.json", HEX, HEX,
            supersede_ids={id(defeated)}, supersede_tx=TXN)
        first, second = [b for b in plan["records"] if b["kind"] == "finding"]
        self.assertEqual((first["blocking"], first["superseded_by_transaction"]),
                         (False, TXN))
        self.assertEqual((second["blocking"],
                          second["superseded_by_transaction"]), (True, None))
        for body in plan["records"]:
            self.assertEqual(chain.validate_record(body), (True, None))


class FindingRefTests(FlowEnv):
    """A `finding_ref` speaks about an earlier finding; it never closes one."""

    def two_findings_then(self, *marks, tail=(APPROVE,)):
        """Round 1 opens two findings; round 2 carries `marks` built from
        their ids; then `tail`."""
        def second(path):
            return _revise(*(mark(self.af_ids()) for mark in marks))
        self.drive(SCOUTING, [_revise(_finding("first", "major"),
                                      _finding("second", "major")),
                              second] + list(tail), eval_on=False)

    def state(self, finding_id):
        return self.folded()["findings"][finding_id]["state"]

    def kinds(self):
        return [r["kind"] for r in self.chain_records()]

    def test_withdrawn_retracts_an_open_finding(self):
        self.two_findings_then(lambda ids: {
            "finding_ref": ids[0], "disposition": "withdrawn"})
        retraction = self.chain_records("retraction")[0]
        self.assertEqual((retraction["reason"], retraction["duplicate_of"],
                          retraction["by"]), ("withdrawn", None, "reviewer"))
        self.assertEqual(self.state(self.af_ids()[0]), "withdrawn")
        self.assertEqual(len(self.af_ids()), 2)

    def test_duplicate_retracts_against_the_finding_it_duplicates(self):
        self.two_findings_then(lambda ids: {
            "finding_ref": ids[0], "disposition": "duplicate",
            "duplicate_of": ids[1]})
        ids = self.af_ids()
        retraction = self.chain_records("retraction")[0]
        self.assertEqual((retraction["reason"], retraction["duplicate_of"]),
                         ("duplicate", ids[1]))
        self.assertEqual(self.state(ids[0]), "duplicate")

    def test_a_duplicate_without_a_resolvable_twin_is_only_a_recommendation(self):
        self.two_findings_then(
            lambda ids: {"finding_ref": ids[0], "disposition": "duplicate"},
            lambda ids: {"finding_ref": ids[1], "disposition": "duplicate",
                         "duplicate_of": "AF-9999"})
        self.assertEqual(self.chain_records("retraction"), [])
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["duplicate", "duplicate"])
        for finding_id in self.af_ids():
            self.assertEqual(self.state(finding_id), "open")

    def test_fixed_is_a_recommendation_and_never_a_close(self):
        self.two_findings_then(lambda ids: {
            "finding_ref": ids[0], "closure": "fixed"})
        recommendation = self.chain_records("recommendation")[0]
        self.assertEqual(recommendation["recommend"], "closed")
        self.assertEqual(recommendation["finding_id"], self.af_ids()[0])
        self.assertNotIn("resolution", self.kinds())
        self.assertEqual(self.state(self.af_ids()[0]), "open")

    def test_still_open_and_confirmed_recommend_without_reopening(self):
        self.two_findings_then(
            lambda ids: {"finding_ref": ids[0], "closure": "still_open"},
            lambda ids: {"finding_ref": ids[1], "disposition": "confirmed"})
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["still_open", "still_open"])
        self.assertNotIn("reopen", self.kinds())

    def test_a_superseded_marking_is_only_a_recommendation(self):
        self.two_findings_then(lambda ids: {
            "finding_ref": ids[0], "closure": "superseded"})
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["still_open"])
        self.assertEqual(self.state(self.af_ids()[0]), "open")

    def test_a_referenced_finding_is_not_a_new_finding(self):
        self.two_findings_then(
            lambda ids: {"finding_ref": ids[0], "summary": "still wrong",
                         "severity": "blocking", "closure": "still_open"})
        self.assertEqual(len(self.af_ids()), 2)

    def test_an_unresolved_ref_is_a_new_finding_and_is_traced(self):
        self.drive(SCOUTING, [
            _revise({"finding_ref": "AF-9999", "summary": "ghost",
                     "severity": "major"}), APPROVE], eval_on=False)
        findings = self.chain_records("finding")
        self.assertEqual([f["summary"] for f in findings], ["ghost"])
        events = self.events("authority.finding_ref_unresolved")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["finding_ref"], "AF-9999")
        self.assertEqual(events[0]["finding_id"], findings[0]["id"])

    def test_a_ref_into_another_phase_does_not_resolve(self):
        self.drive(SCOUTING, [_revise(_finding("scouted", "major")), APPROVE],
                   eval_on=False)
        scouted = self.af_ids()[0]
        self.drive(PLANNING, [
            _revise({"finding_ref": scouted, "summary": "planned",
                     "severity": "major", "disposition": "withdrawn"}),
            APPROVE], eval_on=False)
        self.assertEqual(self.chain_records("retraction"), [])
        self.assertEqual(len(self.events("authority.finding_ref_unresolved")), 1)
        self.assertEqual(self.state(scouted), "open")

    def test_a_ref_to_a_finding_of_the_same_verdict_cannot_resolve(self):
        self.drive(SCOUTING, [
            _revise(_finding("one", "major"), {"finding_ref": "AF-0002",
                                               "summary": "two",
                                               "severity": "minor"}),
            APPROVE], eval_on=False)
        self.assertEqual(len(self.af_ids()), 2)
        self.assertEqual(len(self.events("authority.finding_ref_unresolved")), 1)

    def test_two_markings_of_one_finding_degrade_to_a_recommendation(self):
        self.two_findings_then(
            lambda ids: {"finding_ref": ids[0], "disposition": "withdrawn"},
            lambda ids: {"finding_ref": ids[0], "disposition": "withdrawn"})
        self.assertEqual(len(self.chain_records("retraction")), 1)
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["withdrawn"])
        self.assertEqual(self.state(self.af_ids()[0]), "withdrawn")

    def test_a_closed_finding_is_reopened_by_a_still_open_marking(self):
        body = {"session_uuid": self.sid, "phase": SCOUTING,
                "candidate": dict(CAND)}
        chain.append_batch(self.chain_path, [
            dict(body, kind="round", round=None, seat="scout-reviewer",
                 verdict_sha256=HEX, review_path="/r", verdict_copy_path="/c",
                 verdict_copy_sha256=HEX),
            dict(body, kind="finding", round=1, round_id="RD-0001",
                 summary="s", severity="blocking", blocking=True, criterion="",
                 evidence_path="/e", evidence_sha256=HEX, claim_class=None,
                 discoverer="scout-reviewer", superseded_by_transaction=None)])
        chain.append_batch(self.chain_path, [
            dict(body, kind="proposal", round=1, finding_ids=["AF-0002"],
                 changed_evidence={"paths": ["/p"], "sha256s": [HEX],
                                   "candidate": dict(CAND)},
                 author_role="scout", requires=[]),
            dict(body, kind="recommendation", round=1, finding_id="AF-0002",
                 recommend="closed", reviewer_seat="scout-reviewer",
                 round_id="RD-0001")])
        chain.append_batch(self.chain_path, [
            dict(body, kind="resolution", round=1, finding_id="AF-0002",
                 outcome="closed", decided_by="control_plane",
                 basis={"proposal_id": "AP-0003",
                        "recommendation_id": "AC-0004", "decision_id": None,
                        "witness_ids": []})])
        self.assertEqual(self.state("AF-0002"), "closed")
        self.drive(SCOUTING, [
            _revise({"finding_ref": "AF-0002", "closure": "still_open"}),
            NEEDS_USER], eval_on=False)
        self.assertEqual(self.state("AF-0002"), "open")
        self.assertEqual(self.chain_records("reopen")[0]["finding_id"],
                         "AF-0002")
        kinds = [e["kind"] for e in self.folded()["events"]]
        self.assertEqual(kinds, ["opened", "closed", "reopened"])


class ClosedSourceTests(FlowEnv):
    """`closed_source_findings` and closure markings are measurement."""

    def test_closed_source_findings_create_no_chain_record(self):
        verdict = _revise(_finding("one", "major"))
        verdict["closed_source_findings"] = ["SRC-1", "SRC-2"]
        self.drive(SCOUTING, [verdict, APPROVE], eval_on=False)
        self.assertEqual(
            sorted({r["kind"] for r in self.chain_records()}),
            ["finding", "round"])

    def test_closure_markings_leave_every_chain_finding_open(self):
        appended = {}

        def second(path):
            ids = self.af_ids()
            verdict = _revise({"finding_ref": ids[0], "closure": "fixed"},
                              _finding("new", "major", closure="fixed"))
            verdict["closed_source_findings"] = ids
            # the ledger closure exists before this verdict's own boundary and
            # before the later approving one
            appended["closure"] = ledger.append_closure(
                state_store.ledger_path_for(self.sid),
                closes=self.ledger_findings()[0]["id"],
                phase=SCOUTING, round_index=2)
            return verdict
        self.drive(SCOUTING, [_revise(_finding("one", "major")), second,
                              APPROVE], eval_on=False)
        closure = appended["closure"]
        self.assertIsNotNone(closure)
        for row in self.folded()["findings"].values():
            self.assertEqual(row["state"], "open")
        self.assertNotIn("resolution",
                         [r["kind"] for r in self.chain_records()])
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["closed"])


class RoundSurfaceTests(FlowEnv):
    """Every surface of a usable verdict carries the chain round."""

    def test_the_trace_carries_the_chain_round(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")), APPROVE])
        rounds = {r["round"]: r["id"] for r in self.chain_records("round")}
        recorded = self.events("review.round.recorded")
        self.assertEqual([e["authority_round"] for e in recorded], [1, 2])
        self.assertEqual([e["round_id"] for e in recorded],
                         [rounds[1], rounds[2]])
        handoff = self.events("review.handoff.recorded")
        self.assertEqual([(e["round"], e["authority_round"])
                          for e in handoff], [(1, 1)])

    def test_the_recorded_event_reports_the_new_finding_ids(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major"),
                                      _finding("two", "major")),
                              APPROVE], eval_on=False)
        event = self.events("review.round.recorded")[0]
        self.assertEqual(event["finding_ids"], self.af_ids())
        for key in ("withdrawn_ids", "duplicate_ids", "reopened_ids",
                    "closed_ids"):
            self.assertEqual(event[key], [])

    def test_the_eval_queue_and_events_carry_the_chain_round(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")), APPROVE])
        chain_rounds = [r["round"] for r in self.chain_records("round")]
        for seat in ("scout", "scout-reviewer"):
            self.assertEqual([q["round"] for q in self.queue(seat)],
                             chain_rounds)
        lead = [e for e in self.events("eval.enqueued")
                if e["evaluator"] == "scout"]
        self.assertEqual([e["authority_round"] for e in lead], chain_rounds)

    def test_the_ledger_mirror_carries_the_chain_identity(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major"),
                                      _finding("two", "major")),
                              APPROVE], eval_on=False)
        mirrored = self.ledger_findings()
        self.assertEqual([m["authority_id"] for m in mirrored], self.af_ids())
        self.assertEqual([m["authority_round"] for m in mirrored], [1, 1])
        self.assertEqual([m["round"] for m in mirrored], [1, 1])

    def test_a_referenced_finding_is_mirrored_with_its_reference(self):
        def second(path):
            return _revise({"finding_ref": self.af_ids()[0],
                            "summary": "again", "severity": "major",
                            "closure": "still_open"})
        self.drive(SCOUTING, [_revise(_finding("one", "major")), second,
                              APPROVE], eval_on=False)
        first, again = self.ledger_findings()
        self.assertEqual(again["authority_ref"], first["authority_id"])
        self.assertNotIn("authority_id", again)

    def test_approve_and_needs_user_findings_are_recorded_not_mirrored(self):
        approve = dict(APPROVE, corrective_findings=[_finding("note", "minor")])
        self.drive(SCOUTING, [approve], eval_on=False)
        self.assertEqual(len(self.af_ids()), 1)
        self.assertEqual(self.ledger_findings(), [])
        self.tearDown_state()
        question = dict(NEEDS_USER,
                        corrective_findings=[_finding("open point", "major")])
        self.drive(SCOUTING, [question], eval_on=False)
        self.assertEqual(len(self.af_ids()), 1)
        self.assertEqual(self.ledger_findings(), [])

    def tearDown_state(self):
        self.reset_session_authority()

    def test_round_epochs_equals_the_chain_round(self):
        for eval_on in (True, False):
            with self.subTest(eval_on=eval_on):
                self.tearDown_state()
                epochs = os.path.join(self.assets, "round_epochs.json")
                if os.path.exists(epochs):
                    os.unlink(epochs)
                self.drive(SCOUTING, [_revise(_finding("one", "major")),
                                      APPROVE], eval_on=eval_on)
                top = len(self.chain_records("round"))
                self.assertEqual(
                    self.epochs(), {"scouting|scout": top,
                                    "scouting|scout-reviewer": top})

    def test_eval_off_rounds_are_the_chain_rounds_across_a_resume(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")), NEEDS_USER],
                   eval_on=False)
        self.drive(SCOUTING, [_revise(_finding("two", "major")), APPROVE],
                   eval_on=False)
        self.assertEqual([m["round"] for m in self.ledger_findings()], [1, 3])
        self.assertEqual(
            [e["round"] for e in self.events("review.handoff.recorded")],
            [1, 3])
        self.assertEqual(
            [e["authority_round"]
             for e in self.events("review.handoff.recorded")], [1, 3])


class DivergenceFaultTests(FlowEnv):
    """The chain is authoritative; the epoch file is re-synced from it."""

    def test_a_failed_epoch_sync_is_repaired_by_the_next_verdict(self):
        real = state_store.sync_phase_round
        calls = []
        seen = {}

        def flaky(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("epoch file unwritable")
            return real(*args, **kwargs)

        def second(path):
            seen["epochs"] = self.epochs()
            seen["rounds"] = len(self.chain_records("round"))
            return dict(APPROVE)

        with mock.patch.object(state_store, "sync_phase_round",
                               side_effect=flaky):
            outcome, _payload, _sess = self.drive(
                SCOUTING, [_revise(_finding("one", "major")), second],
                eval_on=False)
        self.assertEqual(outcome, "approved")
        self.assertEqual(seen, {"epochs": {}, "rounds": 1})
        self.assertEqual(self.epochs(), {"scouting|scout": 2,
                                         "scouting|scout-reviewer": 2})

    def test_stale_epochs_never_override_the_chain(self):
        self.drive(SCOUTING, [NEEDS_USER])
        stale = os.path.join(self.assets, "round_epochs.json")
        with open(stale, "w") as fh:
            json.dump({"scouting|scout": 7, "scouting|scout-reviewer": 7}, fh)
        self.drive(SCOUTING, [APPROVE])
        for seat in ("scout", "scout-reviewer"):
            self.assertEqual([q["round"] for q in self.queue(seat)], [1, 2])
        self.assertEqual(self.epochs(), {"scouting|scout": 2,
                                         "scouting|scout-reviewer": 2})

    def test_a_ledger_mirror_failure_does_not_stop_the_gate(self):
        with mock.patch.object(ledger, "append_record", return_value=None):
            outcome, _payload, _sess = self.drive(
                SCOUTING, [_revise(_finding("one", "major")), APPROVE],
                eval_on=False)
        self.assertEqual(outcome, "approved")
        self.assertEqual(self.ledger_findings(), [])
        self.assertEqual(len(self.af_ids()), 1)


class FailClosedTests(FlowEnv):
    """A strict fault stops the gate; nothing approves after one."""

    def assert_stopped(self, result, reason):
        outcome, payload, _sess = result
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "authority_unavailable")
        self.assertEqual(payload["requires"], "operator")
        self.assertFalse(payload["approved"])
        self.assertEqual(payload["reason"], reason)
        decisions = [e for e in self.events("gate.decision")
                     if e.get("gate") == "authority_unavailable"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["reason"], reason)

    def test_a_chain_write_fault_stops_the_gate(self):
        with mock.patch.object(chain, "_write_tmp", side_effect=OSError("disk")):
            result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "chain_write_failed")
        self.assertFalse(os.path.exists(self.chain_path))

    def test_a_replace_fault_stops_the_gate(self):
        real = os.replace

        def fail_on_chain(src, dst, *args, **kwargs):
            if str(dst).endswith("authority_chain.jsonl"):
                raise OSError("replace refused")
            return real(src, dst, *args, **kwargs)

        with mock.patch.object(os, "replace", side_effect=fail_on_chain):
            result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "chain_write_failed")
        self.assertFalse(os.path.exists(self.chain_path))

    def test_a_readback_fault_stops_the_gate_and_restores_the_chain(self):
        with mock.patch.object(chain, "_readback", return_value=b"x"):
            result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "chain_write_failed")
        self.assertFalse(os.path.exists(self.chain_path))

    def test_an_unreadable_chain_stops_the_gate(self):
        unreadable = {"ok": False, "records": [], "head": None,
                      "error": "torn_line", "folded": None}
        with mock.patch.object(chain, "read_chain", return_value=unreadable):
            result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "chain_unreadable")

    def test_a_corrupt_chain_is_not_an_empty_one(self):
        with open(self.chain_path, "wb") as fh:
            fh.write(b"not a chain\n")
        result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "chain_unreadable")
        with open(self.chain_path, "rb") as fh:
            self.assertEqual(fh.read(), b"not a chain\n")

    def test_a_verdict_copy_fault_stops_the_gate(self):
        with open(state_store.authority_verdict_copy_dir_for(self.sid),
                  "w") as fh:
            fh.write("a file where the copy directory belongs")
        result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "verdict_copy_failed")
        self.assertFalse(os.path.exists(self.chain_path))

    def test_an_unopenable_chain_lock_stops_the_gate(self):
        os.makedirs(self.chain_path + ".lock")
        result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "chain_write_failed")

    def test_a_persistent_head_conflict_stops_after_one_replan(self):
        calls = []

        def conflict(session_uuid, records, expected_head=None):
            calls.append(expected_head)
            raise chain.AuthorityHeadConflict(expected_head, "other")

        with mock.patch.object(cowork, "_chain_append", side_effect=conflict):
            result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "head_conflict")
        self.assertEqual(len(calls), 2)

    def test_a_refused_batch_stops_the_gate(self):
        refused = chain.AuthorityRecordRefused("bad_value", "blocking")
        with mock.patch.object(cowork, "_chain_append", side_effect=refused):
            result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_stopped(result, "chain_refused")

    def test_an_uncertain_commit_is_resolved_without_a_duplicate(self):
        real = cowork._chain_append
        calls = []

        def commit_then_doubt(session_uuid, records, expected_head=None):
            calls.append(1)
            committed = real(session_uuid, records, expected_head)
            if len(calls) == 1:
                raise chain.AuthorityCommitUncertain("outcome unknown")
            return committed

        with mock.patch.object(cowork, "_chain_append",
                               side_effect=commit_then_doubt):
            outcome, _payload, _sess = self.drive(
                SCOUTING, [_revise(_finding("one", "major")), APPROVE],
                eval_on=False)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [r["round"] for r in self.chain_records("round")], [1, 2])
        self.assertEqual(len(self.af_ids()), 1)

    def test_an_authority_stop_runs_no_lead_evaluation(self):
        with mock.patch.object(chain, "_write_tmp", side_effect=OSError("disk")):
            result = self.drive(SCOUTING, [APPROVE])
        self.assert_stopped(result, "chain_write_failed")
        self.assertEqual(self.queue("scout"), [])

    def test_an_unwritable_sessions_root_stops_the_gate(self):
        blocker = os.path.join(self.tmp, "a-file")
        with open(blocker, "w") as fh:
            fh.write("not a directory")
        patcher = mock.patch.dict(
            os.environ,
            {"COWORK_SESSIONS_ROOT": os.path.join(blocker, "sessions")})

        def reviewer(path):
            patcher.start()
            self.addCleanup(patcher.stop)
            return dict(APPROVE)

        self.work = os.path.join(self.tmp, "work")
        result = self.drive(SCOUTING, [reviewer], eval_on=False)
        self.assert_stopped(result, "verdict_copy_failed")

    def test_no_candidate_stops_with_status_unavailable(self):
        cases = ((SCOUTING, None), (PLANNING, None), (BUILDING, None),
                 (BUILDING, {"checkpoint_id": "C-1"}))
        for phase, pointer in cases:
            with self.subTest(phase=phase, pointer=pointer):
                self.tearDown_state()

                def vanish(path):
                    os.remove(path)
                    return dict(UNUSABLE)

                result = self.drive(phase, [vanish, APPROVE], eval_on=False,
                                    pointer=pointer)
                self.assert_stopped(result, "candidate_unavailable")
                self.assertFalse(os.path.exists(self.chain_path))
                decision = [e for e in self.events("gate.decision")
                            if e.get("gate") == "authority_unavailable"][-1]
                self.assertEqual(decision["candidate_reason"],
                                 "status_unavailable")
                self.trace = trace_store.Trace(
                    os.path.join(self.assets, "trace.jsonl"),
                    session_uuid=self.sid, run_id="R")

    def tearDown_state(self):
        for path in (self.chain_path, self.chain_path + ".lock"):
            if os.path.exists(path):
                os.unlink(path)
        trace = os.path.join(self.assets, "trace.jsonl")
        if os.path.exists(trace):
            os.unlink(trace)

    def test_a_corrupt_receipt_pointer_stops_a_builder_gate(self):
        result = self.drive(BUILDING, [APPROVE], eval_on=False,
                            pointer="{not json")
        self.assert_stopped(result, "candidate_unavailable")
        decision = self.events("gate.decision")[-1]
        self.assertEqual(decision["candidate_reason"], "pointer_malformed")


class NoSessionTests(FlowEnv):
    """Without a session there is no chain activity at all."""

    def test_a_sessionless_loop_touches_no_authority_state(self):
        outcome, _payload, _sess = self.drive(
            SCOUTING, [_revise(_finding("one")), APPROVE], eval_on=False,
            session=False)
        self.assertEqual(outcome, "approved")
        self.assertFalse(os.path.exists(self.chain_path))
        self.assertFalse(os.path.exists(self.chain_path + ".lock"))
        self.assertFalse(os.path.exists(
            state_store.authority_verdict_copy_dir_for(self.sid)))
        self.assertFalse(os.path.exists(
            os.path.join(self.assets, "round_epochs.json")))

    def test_unregistered_roles_have_no_chain_activity(self):
        self.assertIsNone(cowork._authority_phase(SCOUTING, "scout", "other"))
        self.assertIsNone(cowork._authority_phase(SCOUTING, "planner",
                                                  "scout-reviewer"))
        self.assertIsNone(cowork._authority_phase(None, "other", "x"))


class LifecycleFixtureTests(FlowEnv):
    """The 4/2/2 fixture through the real boundary, derived two ways."""

    def test_four_two_two_matches_the_derived_lifecycle(self):
        def second(path):
            ids = self.af_ids()
            return _revise({"finding_ref": ids[0], "disposition": "withdrawn"},
                           _finding("e", "major"), _finding("f", "major"))

        def third(path):
            ids = self.af_ids()
            return _revise(_finding("g", "major"), _finding("h", "major"),
                           {"finding_ref": ids[4], "disposition": "duplicate",
                            "duplicate_of": ids[1]})

        outcome, _payload, _sess = self.drive(SCOUTING, [
            _revise(*[_finding(name, "major") for name in "abcd"]), second,
            third, APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")
        folded = self.folded()
        rounds = [1, 2, 3, 4]
        by_chain = lifecycle.deltas_from_transitions(folded["events"], rounds)
        self.assertEqual([r["newly_opened"] for r in by_chain], [4, 2, 2, 0])
        self.assertEqual([r["cumulative_unique"] for r in by_chain],
                         [4, 6, 8, 8])
        self.assertEqual([r["retracted"] for r in by_chain], [0, 1, 1, 0])
        self.assertEqual([r["still_open"] for r in by_chain], [4, 5, 6, 6])

        snapshots = []
        for event in self.events("review.round.recorded"):
            snapshots.append({
                "round_id": event["authority_round"], "phase": SCOUTING,
                "seat": "scout-reviewer",
                "verdict_copy_path": event["verdict_copy_path"],
                "verdict_copy_sha256": event["verdict_copy_sha256"],
                "reported": {key: event[key] for key in (
                    "finding_ids", "closed_ids", "withdrawn_ids",
                    "duplicate_ids", "reopened_ids")}})
        by_trace = lifecycle.deltas_from_snapshots(snapshots)
        self.assertEqual(lifecycle.reconcile_derivations(by_chain, by_trace),
                         [])


class StopAssertions:
    """The shape of an `authority_unavailable` stop."""

    def assert_authority_stop(self, result, reason):
        outcome, payload, _sess = result
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "authority_unavailable")
        self.assertEqual(payload["requires"], "operator")
        self.assertFalse(payload["approved"])
        self.assertEqual(payload["reason"], reason)
        decisions = [e for e in self.events("gate.decision")
                     if e.get("gate") == "authority_unavailable"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["reason"], reason)


class ChainGateNumberingTests(FlowEnv):
    """Inside a chain-active gate the reviewer seat numbers its evaluation
    entry from the chain, with or without a chain file."""

    def rounds(self, seat):
        return [q["round"] for q in self.queue(seat)]

    def test_seeded_epochs_without_a_chain_number_every_surface_alike(self):
        self.seed_epochs(3)
        outcome, _payload, _sess = self.drive(
            SCOUTING, [_revise(_finding("one", "major")), APPROVE])
        self.assertEqual(outcome, "approved")
        chain_rounds = [r["round"] for r in self.chain_records("round")]
        self.assertEqual(chain_rounds, [1, 2])
        self.assertEqual(self.rounds("scout-reviewer"), chain_rounds)
        self.assertEqual(self.rounds("scout"), chain_rounds)

    def test_a_first_write_fault_then_a_resume_keeps_the_numbers_aligned(self):
        with mock.patch.object(chain, "_write_tmp", side_effect=OSError("x")):
            first = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(first[1]["reason"], "chain_write_failed")
        self.assertFalse(os.path.exists(self.chain_path))
        outcome, _payload, _sess = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(outcome, "approved")
        chain_rounds = [r["round"] for r in self.chain_records("round")]
        self.assertEqual(chain_rounds, [1])
        # the orphan entry of the stopped verdict carries the round that
        # commit was about to mint; the resumed entry carries the same one
        self.assertEqual(self.rounds("scout-reviewer"), [1, 1])
        self.assertEqual(self.rounds("scout"), [1])

    def test_a_direct_review_fn_keeps_the_legacy_counter(self):
        self.seed_epochs(3)
        lead, reviewer = PHASE_ROLES[SCOUTING]
        status_path = self.status_path(SCOUTING)
        review_path = self.review_path(SCOUTING)
        with open(status_path, "w") as fh:
            json.dump({"status": "ready_for_review"}, fh)

        def runner(config, context, selected, artifact_path, review_file,
                   **kw):
            with open(review_file, "w") as fh:
                json.dump(APPROVE, fh)
            return dict(APPROVE)

        review_fn = cowork.make_review_fn(
            cowork.default_config([lead, reviewer]), "ctx", [lead, reviewer],
            review_path, reviewer_runner=runner, reviewer_role=reviewer,
            phase=SCOUTING, trace=self.trace,
            eval_scratch_path=os.path.join(self.assets, "eval.json"),
            scores_path=state_store.scores_path_for(self.sid),
            session_uuid=self.sid, evaluation_policy="all_rounds")
        review_fn(status_path, 1)
        self.assertEqual(self.rounds("scout-reviewer"), [4])
        self.assertIsNone(
            cowork._chain_round_peek(self.sid, SCOUTING, reviewer))

    def test_the_gate_context_is_scoped_to_the_reviewer_turn(self):
        seen = []

        def reviewer(path):
            seen.append(cowork._CHAIN_GATE_CTX.get())
            return dict(APPROVE)

        self.assertIsNone(cowork._CHAIN_GATE_CTX.get())
        self.drive(SCOUTING, [reviewer], eval_on=False)
        self.assertEqual(seen, [{"session_uuid": self.sid,
                                 "phase": SCOUTING, "seat": "scout-reviewer"}])
        self.assertIsNone(cowork._CHAIN_GATE_CTX.get())

    def test_the_gate_context_is_reset_when_the_reviewer_raises(self):
        class Boom(Exception):
            pass

        def reviewer(path):
            raise Boom()

        with self.assertRaises(Boom):
            self.drive(SCOUTING, [reviewer], eval_on=False)
        self.assertIsNone(cowork._CHAIN_GATE_CTX.get())

    def test_a_sessionless_loop_sets_no_gate(self):
        seen = []

        def reviewer(path):
            seen.append(cowork._CHAIN_GATE_CTX.get())
            return dict(APPROVE)

        self.drive(SCOUTING, [reviewer], eval_on=False, session=False)
        self.assertEqual(seen, [None])

    def test_an_unreadable_chain_queues_no_reviewer_entry(self):
        with open(self.chain_path, "wb") as fh:
            fh.write(b"not a chain\n")
        outcome, payload, _sess = self.drive(SCOUTING, [APPROVE])
        self.assertEqual((outcome, payload["reason"]),
                         ("ended", "chain_unreadable"))
        self.assertEqual(self.queue("scout-reviewer"), [])


class LostChainTests(StopAssertions, FlowEnv):
    """An absent or empty chain after a committed round stops the gate; a
    session that never committed one is not a lost chain."""

    def assert_lost(self, result):
        self.assert_authority_stop(result, "chain_lost")
        self.assertFalse(os.path.exists(self.chain_path))
        self.assertFalse(os.path.exists(self.chain_path + ".lock"))

    def test_trace_evidence_alone_stops_a_resume(self):
        self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(self.ledger_findings(), [])
        self.lose_chain()
        self.assert_lost(self.drive(SCOUTING, [APPROVE], eval_on=False))

    def test_ledger_evidence_alone_stops_a_resume(self):
        self.drive(SCOUTING, [_revise(_finding("one")), NEEDS_USER],
                   eval_on=False)
        original = self.ledger_findings()[0]["authority_id"]
        self.lose_chain()
        os.unlink(os.path.join(self.assets, "trace.jsonl"))
        self.assert_lost(self.drive(SCOUTING, [APPROVE], eval_on=False))
        self.assertEqual(
            [m["authority_id"] for m in self.ledger_findings()], [original])

    def test_a_zero_length_chain_after_commits_stops_a_resume(self):
        self.drive(SCOUTING, [APPROVE], eval_on=False)
        with open(self.chain_path, "wb"):
            pass
        outcome, payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual((outcome, payload["reason"]), ("ended", "chain_lost"))
        self.assertEqual(os.path.getsize(self.chain_path), 0)

    def test_a_lost_chain_queues_no_new_evaluation_entry(self):
        self.drive(SCOUTING, [APPROVE])
        reviewer_before = len(self.queue("scout-reviewer"))
        lead_before = len(self.queue("scout"))
        self.lose_chain()
        self.assert_lost(self.drive(SCOUTING, [APPROVE]))
        self.assertEqual(len(self.queue("scout-reviewer")), reviewer_before)
        self.assertEqual(len(self.queue("scout")), lead_before)

    def test_a_first_write_fault_is_not_a_lost_chain(self):
        with mock.patch.object(chain, "_write_tmp", side_effect=OSError("x")):
            first = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(first[1]["reason"], "chain_write_failed")
        enqueued = [e for e in self.events("eval.enqueued")
                    if e["evaluator"] == "scout-reviewer"]
        self.assertEqual([e["authority_round"] for e in enqueued], [1])
        outcome, _payload, _sess = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(outcome, "approved")
        self.assertEqual(
            [r["round"] for r in self.chain_records("round")], [1])

    def test_a_rolled_back_first_commit_is_not_a_lost_chain(self):
        with mock.patch.object(chain, "_readback", return_value=b"x"):
            first = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(first[1]["reason"], "chain_write_failed")
        self.assertFalse(os.path.exists(self.chain_path))
        self.assertTrue(self.events("eval.enqueued"))
        outcome, _payload, _sess = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(outcome, "approved")
        self.assertEqual(
            [r["round"] for r in self.chain_records("round")], [1])

    def test_fresh_and_pre_existing_epochs_sessions_proceed(self):
        self.seed_epochs(5)
        outcome, _payload, _sess = self.drive(SCOUTING, [APPROVE])
        self.assertEqual(outcome, "approved")
        self.assertEqual(
            [r["round"] for r in self.chain_records("round")], [1])

    def test_a_leftover_verdict_copy_alone_proceeds(self):
        directory = state_store.authority_verdict_copy_dir_for(self.sid)
        os.makedirs(directory)
        with open(os.path.join(directory, HEX + ".json"), "w") as fh:
            fh.write("{}")
        outcome, _payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")

    def test_signals_that_do_not_prove_a_commit_are_ignored(self):
        self.trace.event("review.verdict", verdict="approve")
        self.trace.event("review.round.recorded", authority_round=0)
        self.trace.event("review.round.recorded", authority_round=True)
        self.trace.event("review.round.recorded", authority_round="1")
        self.trace.event("eval.enqueued", evaluator="scout-reviewer",
                         authority_round=1)
        path = state_store.ledger_path_for(self.sid)
        ledger.append_finding(path, summary="legacy", severity="major")
        ledger.append_finding(path, summary="flag", severity="major",
                              authority_round=True)
        ledger.append_finding(path, summary="zero", severity="major",
                              authority_round=0)
        self.assertFalse(cowork._prior_authority_round_evidence(self.sid))
        outcome, _payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")

    def test_a_committed_round_signal_is_evidence(self):
        self.trace.event("review.round.recorded", authority_round=2)
        self.assertTrue(cowork._prior_authority_round_evidence(self.sid))
        os.unlink(os.path.join(self.assets, "trace.jsonl"))
        self.assertFalse(cowork._prior_authority_round_evidence(self.sid))
        ledger.append_finding(state_store.ledger_path_for(self.sid),
                              summary="x", severity="major",
                              authority_round=1)
        self.assertTrue(cowork._prior_authority_round_evidence(self.sid))

    def test_a_non_empty_chain_is_never_treated_as_lost(self):
        self.drive(SCOUTING, [_revise(_finding("one", "major")), NEEDS_USER],
                   eval_on=False)
        outcome, _payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")
        self.assertEqual(
            [r["round"] for r in self.chain_records("round")], [1, 2, 3])


class EvidencePairingTests(FlowEnv):
    """Every recorded evidence pair names the bytes its digest was taken from,
    or is the immutable verdict-copy pair."""

    def plan(self, verdict, review_path=None):
        folded = {"rounds": [], "findings": {}}
        return cowork._plan_verdict_records(
            self.sid, SCOUTING, "scout-reviewer", verdict, folded, None, 1,
            CAND, review_path, "/copy.json", HEX, HEX)

    def finding_for(self, entry, review_path=None):
        plan = self.plan({"verdict": "revise", "corrective_findings": [
            dict({"summary": "a", "severity": "major"}, **entry)]},
            review_path=review_path)
        for body in plan["records"]:
            self.assertEqual(chain.validate_record(body), (True, None), body)
        return [b for b in plan["records"] if b["kind"] == "finding"][0]

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_a_missing_or_unreadable_path_falls_back_to_the_copy_pair(self):
        unreadable = os.path.join(self.tmp, "a-directory")
        os.makedirs(unreadable)
        for path in (os.path.join(self.tmp, "absent.txt"), unreadable,
                     "bad\0path"):
            for sha in (None, "not-hex", HEX2):
                with self.subTest(path=path, sha=sha):
                    got = self.finding_for(
                        {"evidence_path": path, "evidence_sha256": sha})
                    self.assertEqual(
                        (got["evidence_path"], got["evidence_sha256"]),
                        ("/copy.json", HEX))

    def test_a_missing_path_falls_back_to_a_readable_review_file(self):
        review = self.write("review.json", '{"verdict": "revise"}')
        got = self.finding_for(
            {"evidence_path": os.path.join(self.tmp, "absent.txt"),
             "evidence_sha256": HEX2}, review_path=review)
        self.assertEqual((got["evidence_path"], got["evidence_sha256"]),
                         (review, _sha('{"verdict": "revise"}')))

    def test_an_orphan_digest_is_never_paired_with_a_path(self):
        got = self.finding_for({"evidence_sha256": HEX2})
        self.assertEqual((got["evidence_path"], got["evidence_sha256"]),
                         ("/copy.json", HEX))
        review = self.write("review.json", "review bytes")
        got = self.finding_for({"evidence_sha256": HEX2}, review_path=review)
        self.assertEqual((got["evidence_path"], got["evidence_sha256"]),
                         (review, _sha("review bytes")))

    def test_a_non_string_path_is_never_opened(self):
        with mock.patch.object(cowork, "_file_sha256",
                               side_effect=AssertionError("opened")):
            for path in (0, 3, ["x"], {"p": 1}, "", None, True):
                with self.subTest(path=path):
                    got = self.finding_for(
                        {"evidence_path": path, "evidence_sha256": HEX2})
                    self.assertEqual(
                        (got["evidence_path"], got["evidence_sha256"]),
                        ("/copy.json", HEX))

    def test_a_readable_path_pairs_with_the_digest_of_its_bytes(self):
        path = self.write("evidence.txt", "evidence bytes")
        for sha in (None, "not-hex"):
            with self.subTest(sha=sha):
                got = self.finding_for(
                    {"evidence_path": path, "evidence_sha256": sha})
                self.assertEqual(
                    (got["evidence_path"], got["evidence_sha256"]),
                    (path, _sha("evidence bytes")))

    def test_the_round_record_never_names_an_unreadable_review_file(self):
        verdict = _revise(_finding("one"))
        minted = cowork._record_verdict_round(
            self.sid, SCOUTING, "scout", "scout-reviewer", verdict, CAND,
            os.path.join(self.tmp, "absent-review.json"))
        record = self.chain_records("round")[0]
        self.assertEqual(record["review_path"], minted["verdict_copy_path"])
        self.assertEqual(record["verdict_sha256"],
                         minted["verdict_copy_sha256"])
        finding = self.chain_records("finding")[0]
        self.assertEqual((finding["evidence_path"], finding["evidence_sha256"]),
                         (minted["verdict_copy_path"],
                          minted["verdict_copy_sha256"]))

    def test_the_round_record_pairs_a_readable_review_file_with_its_bytes(self):
        review = self.write("review.json", "review bytes")
        cowork._record_verdict_round(
            self.sid, SCOUTING, "scout", "scout-reviewer", dict(APPROVE),
            CAND, review)
        record = self.chain_records("round")[0]
        self.assertEqual(record["review_path"], review)
        self.assertEqual(record["verdict_sha256"], _sha("review bytes"))

    def mirrored(self, entry, review_path):
        cowork._record_findings(
            self.sid, {"verdict": "revise", "corrective_findings": [
                dict({"summary": "a", "severity": "major"}, **entry)]},
            "scout-reviewer", SCOUTING, 1, review_path=review_path)
        return self.ledger_findings()[-1]

    def test_the_ledger_mirror_never_pairs_a_digest_with_a_foreign_path(self):
        review = self.write("review.json", "review bytes")
        cases = ({"evidence_sha256": HEX2},
                 {"evidence_path": os.path.join(self.tmp, "absent.txt"),
                  "evidence_sha256": HEX2},
                 {"evidence_path": 7, "evidence_sha256": HEX2})
        for entry in cases:
            with self.subTest(entry=entry):
                row = self.mirrored(entry, review)
                self.assertNotIn("evidence_path", row)
                self.assertEqual(row["evidence_sha256"], HEX2)

    def test_the_ledger_mirror_keeps_a_readable_pair_and_todays_default(self):
        path = self.write("evidence.txt", "evidence bytes")
        review = self.write("review.json", "review bytes")
        row = self.mirrored({"evidence_path": path,
                             "evidence_sha256": HEX2}, review)
        self.assertEqual((row["evidence_path"], row["evidence_sha256"]),
                         (path, HEX2))
        row = self.mirrored({}, review)
        self.assertEqual(row["evidence_path"], review)
        self.assertNotIn("evidence_sha256", row)
        row = self.mirrored({"evidence_sha256": "not-hex"}, review)
        self.assertEqual((row["evidence_path"], row["evidence_sha256"]),
                         (review, "not-hex"))


class VerdictCopyDurabilityTests(StopAssertions, FlowEnv):
    """A committed round never names a verdict copy a crash can lose."""

    def spy(self):
        events = []
        real_replace = os.replace
        real_fsync = cowork._fsync_directory
        real_append = cowork._chain_append

        def replace(src, dst, *args, **kwargs):
            if str(dst).endswith(".json") and "authority_verdicts" in str(dst):
                events.append("replace")
            return real_replace(src, dst, *args, **kwargs)

        def fsync(path):
            events.append("fsync")
            self.assertEqual(
                path, state_store.authority_verdict_copy_dir_for(self.sid))
            return real_fsync(path)

        def append(*args, **kwargs):
            events.append("append")
            return real_append(*args, **kwargs)

        for patcher in (
                mock.patch.object(os, "replace", side_effect=replace),
                mock.patch.object(cowork, "_fsync_directory",
                                  side_effect=fsync),
                mock.patch.object(cowork, "_chain_append", side_effect=append)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return events

    def test_the_directory_is_synced_after_the_copy_and_before_the_commit(self):
        events = self.spy()
        outcome, _payload, _sess = self.drive(
            SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(outcome, "approved")
        self.assertEqual(events, ["replace", "fsync", "append"])

    def test_an_existing_copy_is_synced_too(self):
        self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.reset_session_authority()
        events = self.spy()
        self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assertEqual(events, ["fsync", "append"])

    def test_a_directory_sync_fault_stops_before_the_commit(self):
        with mock.patch.object(cowork, "_fsync_directory",
                               side_effect=OSError("sync")):
            result = self.drive(SCOUTING, [APPROVE], eval_on=False)
        self.assert_authority_stop(result, "verdict_copy_failed")
        self.assertFalse(os.path.exists(self.chain_path))


class CandidateMutationTests(FlowEnv):
    """The candidate is the one captured before the reviewer ran."""

    def mutating(self, verdict, seen):
        def reviewer(path):
            seen["before"] = self.status_sha(SCOUTING)
            with open(path, "w") as fh:
                json.dump({"status": "ready_for_review",
                           "result": {"turn": "changed under review"}}, fh)
            seen["after"] = self.status_sha(SCOUTING)
            return verdict
        return reviewer

    def test_a_revise_records_the_pre_review_candidate(self):
        seen = {}
        self.drive(SCOUTING, [
            self.mutating(_revise(_finding("one", "major")), seen), APPROVE],
            eval_on=False)
        self.assertNotEqual(seen["before"], seen["after"])
        first = self.chain_records("round")[0]
        self.assertEqual(first["candidate"]["status_sha256"], seen["before"])
        finding = self.chain_records("finding")[0]
        self.assertEqual(finding["candidate"]["status_sha256"], seen["before"])

    def test_a_stale_approve_records_the_pre_review_candidate_and_stops(self):
        seen = {}
        outcome, payload, lead = self.drive(
            SCOUTING, [self.mutating(dict(APPROVE), seen)], eval_on=False)
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(payload["kind"], "review_candidate_changed")
        rounds = self.chain_records("round")
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["candidate"]["status_sha256"],
                         seen["before"])
        # the lead was sent only the seed, never told it was approved
        self.assertEqual(len(lead.sent), 1)


class DocContractTests(unittest.TestCase):
    """The role documents and README state the recording contract."""

    def read(self, *parts):
        with open(os.path.join(cowork.SKILL_ROOT, *parts)) as fh:
            return fh.read()

    def test_reviewer_roles_document_finding_references(self):
        for name in ("scout-reviewer", "planning-advisor", "build-reviewer"):
            with self.subTest(role=name):
                text = self.read("roles", name + ".md")
                self.assertIn("finding_ref", text)
                self.assertIn("duplicate_of", text)
                self.assertIn("never a close", text)

    def test_readme_separates_the_chain_from_the_measurement_ledger(self):
        text = self.read("README.md")
        start = text.index("### The ledgers")
        end = text.index("\n### ", start + 1)
        section = text[start:end]
        for needle in ("authority_chain.jsonl", "authority_unavailable",
                       "best-effort", "authority_id"):
            self.assertIn(needle, section)

    def test_readme_states_when_each_surface_follows_the_commit(self):
        text = self.read("README.md")
        start = text.index("### The ledgers")
        end = text.index("\n### ", start + 1)
        section = " ".join(text[start:end].split())
        for needle in ("chain_lost", "queued earlier",
                       "review.round.recorded"):
            self.assertIn(needle, section)
        self.assertNotIn("before the trace, the evaluation queue", section)


class StaticContractTests(unittest.TestCase):
    """The single-writer and single-filler contracts hold in the source."""

    @classmethod
    def source(cls):
        with open(cowork.__file__) as fh:
            return fh.read()

    @staticmethod
    def function(source, name):
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError("no function %s" % name)

    def test_one_chain_append_call_exists_and_it_is_owner_fenced(self):
        source = self.source()
        self.assertEqual(source.count("authority_chain.append_batch("), 1)
        self.assertEqual(source.count("authority_chain.append("), 0)
        node = self.function(source, "_chain_append")
        body = node.body[1:] if isinstance(
            node.body[0], ast.Expr) and isinstance(
                node.body[0].value, ast.Constant) else node.body
        first = body[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertEqual(first.value.func.id, "_require_owner")
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Attribute)
                 and n.attr == "append_batch"]
        self.assertEqual(len(calls), 1)

    def test_the_candidate_filler_reads_only_the_declared_sources(self):
        node = self.function(self.source(), "_gate_candidate")
        names = set()
        for statement in node.body[1:]:
            for item in ast.walk(statement):
                if isinstance(item, ast.Name):
                    names.add(item.id)
                if isinstance(item, ast.Attribute):
                    names.add(item.attr)
        for needed in ("read_current_receipt_pointer",
                       "current_receipt_pointer_path_for",
                       "fingerprint_status", "select_candidate"):
            self.assertIn(needed, names)
        for forbidden in ("_current_tree_digest", "load_manifest",
                          "manifest_path_for", "manifest_digest",
                          "candidate_manifest_digest"):
            self.assertNotIn(forbidden, names)

    def test_the_production_phase_triples_are_recognised(self):
        self.assertEqual(
            cowork._authority_phase(SCOUTING, "scout", cowork.SCOUT_REVIEWER),
            SCOUTING)
        self.assertEqual(
            cowork._authority_phase(PLANNING, "planner",
                                    cowork.PLANNING_ADVISOR), PLANNING)
        self.assertEqual(
            cowork._authority_phase(BUILDING, "builder", cowork.BUILD_REVIEWER),
            BUILDING)
        self.assertEqual(
            cowork._authority_phase(None, "builder", cowork.BUILD_REVIEWER),
            BUILDING)

    def test_the_verdict_is_recorded_before_any_evaluation_runs(self):
        source = inspect.getsource(cowork._role_loop)
        self.assertLess(
            source.index("_record_verdict_round("),
            source.index("evaluate_fn(session, verdict, review_rounds)"))


if __name__ == "__main__":
    unittest.main()
