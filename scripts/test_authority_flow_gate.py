#!/usr/bin/env python3
"""Tests for the blocking gate and closure over the authority chain at the
`_role_loop` verdict boundary and in the final defensive gate.

A lead may propose a resolution (`result.resolution_proposals`); the runtime
binds the proposal to the reviewed candidate, closes a finding only through the
pure `closure_decision`, and lets a phase advance only while the pure
`blocking_decision` reports nothing blocking -- at the verdict boundary (an
approve it blocks becomes a revise) and again, read-only, before any approval
effect or gate event.

Fake lead sessions and fake reviewer runners replace the controllers.
COWORK_SESSIONS_ROOT is pinned to a temp directory, session ids are fresh
UUIDs and every input is neutral and synthetic. No live provider is used.

Run: python3 scripts/cowork_offline_tests.py test_authority_flow_gate
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
import cowork_authority_gate as authority_gate  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_owner  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

PHASE_ROLES = candidate_mod.PHASE_ROLES
HEX = "0123456789abcdef" * 4
HEX2 = "fedcba9876543210" * 4
SCOUTING, PLANNING, BUILDING = "scouting", "planning", "building"
CAND = {"kind": "status_artifact", "status_sha256": HEX}
TXN = "T-green"
SETTLED = (verification.DISPOSITION_ACCEPTED, verification.DISPOSITION_REJECTED)


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


def _recommend(finding_id, closure="fixed"):
    return {"finding_ref": finding_id, "closure": closure}


APPROVE = {"verdict": "approve"}
NEEDS_USER = {"verdict": "needs_user", "user_question": "which scope?"}


class _LeadSession:
    """A lead whose every send leaves a fresh `ready_for_review` status. The
    optional `result_fn(send_number)` adds keys to the status `result`. With
    `keep_status` a send leaves the status file exactly as it is."""

    def __init__(self, status_path, turns, result_fn=None, on_send=None,
                 keep_status=False):
        self.status_path = status_path
        self.turns = turns
        self.result_fn = result_fn
        self.on_send = on_send
        self.keep_status = keep_status
        self.sent = []

    def send(self, text, meta=None):
        self.sent.append(text)
        number = len(self.sent)
        if self.keep_status:
            if self.on_send is not None:
                self.on_send(number)
            return {"ok": True, "result": "ok"}
        result = {"turn": next(self.turns)}
        if self.result_fn is not None:
            result.update(self.result_fn(number) or {})
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        with open(self.status_path, "w") as fh:
            json.dump({"status": "ready_for_review", "result": result}, fh)
        if self.on_send is not None:
            self.on_send(number)
        return {"ok": True, "result": "ok"}

    def close(self):
        pass


class _Baseline:
    """A reviewer hash-gate baseline: eligible once an approve was recorded."""

    def __init__(self, order=None, approved=False):
        self.order = order if order is not None else []
        self.approved = approved

    def compute_composite(self):
        return "composite"

    def eligible(self, composite):
        return self.approved

    def record(self, composite):
        self.order.append("baseline")
        self.approved = True


class _Profile:
    """The execution-profile calls `_role_loop` makes, recording approvals."""

    def __init__(self, order):
        self.order = order
        self.approvals = []

    def on_verdict(self, reviewer_role, verdict):
        return None

    def screen_verdict(self, role, verdict):
        return None

    def on_round_cap(self, role):
        return None

    def reuse_policy(self):
        return None

    def on_build_approved(self, verdict, round_index, manifest_digest):
        self.order.append("profile")
        self.approvals.append((round_index, manifest_digest))


class GateEnv(unittest.TestCase):
    """A pinned sessions root, one session, and a driver for `_role_loop`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afg-")
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
        self.evidence = os.path.join(self.tmp, "evidence.txt")
        with open(self.evidence, "w") as fh:
            fh.write("evidence bytes")
        self.order = []
        self.dispositions = []
        self.manifest_matches = True
        for patch in (
                mock.patch.object(cowork, "_emit_verification_disposition",
                                  side_effect=self._disposition),
                mock.patch.object(cowork, "_grant_gate_acceptance",
                                  side_effect=self._grant),
                mock.patch.object(cowork, "_accepted_manifest_matches",
                                  side_effect=self._matches)):
            patch.start()
            self.addCleanup(patch.stop)

    # ---- approval-effect spies
    def _disposition(self, session_uuid, trace, transaction_id, disposition,
                     **kwargs):
        self.order.append("disposition")
        self.dispositions.append(disposition)

    def _grant(self, *args):
        self.order.append("grant")

    def _matches(self, pointer, repo=None):
        return self.manifest_matches

    def settled_dispositions(self):
        return [d for d in self.dispositions if d in SETTLED]

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

    def ledger_findings(self):
        return [r for r in ledger.read_ledger(
            state_store.ledger_path_for(self.sid)) if r.get("kind") == "finding"]

    def status_sha(self, phase):
        with open(self.status_path(phase), "rb") as fh:
            return _sha(fh.read())

    def status_candidate(self, phase):
        return {"kind": "status_artifact",
                "status_sha256": self.status_sha(phase)}

    def write_pointer(self, pointer):
        path = state_store.current_receipt_pointer_path_for(self.sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(pointer, fh)

    def owned_pointer(self, **extra):
        pointer = {"transaction_id": TXN, "manifest_digest": HEX,
                   "index_digest": HEX2, "disposition": "pending_review"}
        pointer.update(extra)
        return pointer

    def lose_chain(self):
        for path in (self.chain_path, self.chain_path + ".lock"):
            if os.path.exists(path):
                os.unlink(path)

    def reset_session_authority(self):
        self.lose_chain()
        for path in (os.path.join(self.assets, "trace.jsonl"),
                     state_store.ledger_path_for(self.sid)):
            if os.path.exists(path):
                os.unlink(path)

    def inject_blocker(self, phase=SCOUTING, summary="late"):
        """Append an authoritative open blocking finding to the chain, as a
        late writer would."""
        read = self.chain_read()
        self.assertTrue(read["ok"], read["error"])
        seat = PHASE_ROLES[phase][1]
        minted = 1 + sum(1 for r in read["folded"]["rounds"]
                         if r["phase"] == phase and r["seat"] == seat)
        round_id = chain.record_id("round", len(read["records"]) + 1)
        body = {"session_uuid": self.sid, "phase": phase,
                "candidate": dict(CAND)}
        chain.append_batch(self.chain_path, [
            dict(body, kind="round", round=None, seat=seat,
                 verdict_sha256=HEX, review_path="/r",
                 verdict_copy_path="/c", verdict_copy_sha256=HEX),
            dict(body, kind="finding", round=minted, round_id=round_id,
                 summary=summary, severity="blocking", blocking=True,
                 criterion="", evidence_path="/e", evidence_sha256=HEX,
                 claim_class=None, discoverer=seat,
                 superseded_by_transaction=None)], read["head"])

    # ---- lead proposals
    def phase_af_ids(self, phase):
        return [r["id"] for r in self.chain_records("finding")
                if r["phase"] == phase]

    def proposing(self, turns, requires=None, extra=None, ids=None,
                  paths=None, phase=SCOUTING):
        """A `result_fn` that proposes, on the given lead send numbers,
        against the first finding of `phase` (or `ids`). `ids`, `paths` and
        `requires` are written exactly as given."""
        def result_fn(number):
            if number not in turns:
                return {}
            entry = {
                "authority_ids": (ids if ids is not None
                                  else self.phase_af_ids(phase)[:1]),
                "changed_evidence_paths": (
                    [self.evidence] if paths is None else paths)}
            if requires is not None:
                entry["requires"] = requires
            entry.update(extra or {})
            return {"resolution_proposals": [entry]}
        return result_fn

    # ---- driving the loop
    def drive(self, phase, script, loop_kwargs=None, pointer=None,
              result_fn=None, on_send=None, eval_on=False, session=True,
              baseline=None, profile=None, keep_status=False):
        """Run one `_role_loop` over scripted reviewer verdicts (a resume is a
        second call). A script entry is a verdict dict or a callable taking
        the status path. `keep_status` makes the lead leave the status file
        untouched. Returns (outcome, payload, lead session)."""
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
            session_uuid=sid, evaluation_policy="all_rounds")
        evaluate_fn = None
        if eval_on and session:
            evaluate_fn = cowork._make_enqueue_eval_fn(
                lead, reviewer, phase, scratch, scores, sid,
                trace=self.trace, review_path=review_path,
                artifact_path=status_path, evaluation_policy="all_rounds")
        lead_session = _LeadSession(
            status_path, self.turns, result_fn=result_fn, on_send=on_send,
            keep_status=keep_status)
        kwargs = dict(role=lead, review_fn=review_fn, trace=self.trace,
                      reviewer_role=reviewer, evaluate_fn=evaluate_fn,
                      phase=phase, review_path=review_path, session_uuid=sid)
        if baseline is not None:
            kwargs["skip_baseline"] = baseline
        if profile is not None:
            kwargs["profile_session"] = profile
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

    def closure_flow(self, requires=None, extra=None, tail=(APPROVE,)):
        """Round 1 opens a blocking finding; the lead proposes its closure; a
        revise carrying a closed recommendation closes it at its own boundary;
        `tail` follows."""
        return self.drive(
            SCOUTING,
            [_revise(_finding("gap")),
             lambda path: _revise(_recommend(self.af_ids()[0])), *tail],
            result_fn=self.proposing({2}, requires=requires, extra=extra))

    def assert_not_advanced(self, outcome, baseline=None):
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(self.settled_dispositions(), [])
        self.assertNotIn("grant", self.order)
        if baseline is not None:
            self.assertFalse(baseline.approved)
        self.assertEqual(
            [e for e in self.events("gate.decision")
             if e.get("action") == "approve"], [])
        self.assertEqual(
            [e for e in self.events("gate.show") if e.get("gate") == "done"],
            [])

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


class ScreenTests(GateEnv):
    """An approve that `blocking_decision` blocks never advances the phase."""

    def screened(self):
        return self.events("authority.approve_screened")

    def test_a_stale_blocker_screens_an_approve_to_a_revise(self):
        baseline = _Baseline(self.order)
        outcome, payload, lead = self.drive(
            SCOUTING, [_revise(_finding("gap")), APPROVE, NEEDS_USER],
            baseline=baseline)
        self.assertEqual(payload["kind"], "reviewer_question")
        self.assert_not_advanced(outcome, baseline)
        self.assertEqual(self.chain_records("resolution"), [])
        screened = self.screened()
        self.assertEqual(len(screened), 1)
        self.assertEqual(screened[0]["finding_ids"], self.af_ids())
        self.assertIn("stale_candidate", screened[0]["reason_codes"])
        # the screened approve handed the lead back, like any revise
        self.assertEqual(len(lead.sent), 3)
        self.assertEqual(
            len([e for e in self.events("review.handoff.recorded")
                 if e.get("kind") == "revise"]), 2)

    def test_an_unresolved_blocker_screens_an_approve(self):
        outcome, payload, _lead = self.drive(
            BUILDING, [_revise(_finding("gap")), APPROVE, NEEDS_USER],
            pointer=self.owned_pointer())
        self.assert_not_advanced(outcome)
        screened = self.screened()
        self.assertEqual(len(screened), 1)
        self.assertIn("unresolved", screened[0]["reason_codes"])
        self.assertEqual(self.chain_records("resolution"), [])

    def test_a_candidate_of_another_kind_screens_an_approve(self):
        def bind_receipt(number):
            if number == 2:
                self.write_pointer(self.owned_pointer())

        outcome, _payload, _lead = self.drive(
            BUILDING, [_revise(_finding("gap")), APPROVE, NEEDS_USER],
            on_send=bind_receipt)
        self.assert_not_advanced(outcome)
        rounds = self.chain_records("round")
        self.assertEqual(rounds[0]["candidate"]["kind"], "status_artifact")
        self.assertEqual(rounds[1]["candidate"]["kind"], "owned_receipt")
        screened = self.screened()
        self.assertEqual(len(screened), 1)
        self.assertIn("stale_candidate", screened[0]["reason_codes"])
        self.assertEqual(self.chain_records("resolution"), [])

    def test_a_blocker_of_another_phase_screens_an_approve(self):
        self.drive(PLANNING, [_revise(_finding("plan gap")), NEEDS_USER])
        foreign = self.af_ids()
        outcome, _payload, _lead = self.drive(
            SCOUTING, [APPROVE, NEEDS_USER])
        self.assert_not_advanced(outcome)
        screened = self.screened()
        self.assertEqual(len(screened), 1)
        self.assertEqual(screened[0]["reason_codes"], ["foreign_binding"])
        self.assertEqual(screened[0]["finding_ids"], foreign)

    def test_forged_closure_fields_close_nothing(self):
        def forged(path):
            return dict(
                APPROVE, closed_source_findings=self.af_ids(),
                corrective_findings=[dict(
                    _recommend(self.af_ids()[0]), decided_by="control_plane",
                    outcome="closed", resolution="closed")])

        baseline = _Baseline(self.order)
        outcome, _payload, _lead = self.drive(
            SCOUTING, [_revise(_finding("gap")), forged, NEEDS_USER],
            baseline=baseline)
        self.assert_not_advanced(outcome, baseline)
        self.assertEqual(self.chain_records("resolution"), [])
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["closed"])
        self.assertEqual(len(self.screened()), 1)
        self.assertEqual(self.folded()["findings"][self.af_ids()[0]]["state"],
                         "open")

    def test_the_round_cap_stops_a_screened_approve(self):
        baseline = _Baseline(self.order)
        script = [_revise(_finding("gap"))] + [APPROVE] * (
            cowork.REVIEW_ROUND_CAP - 1)
        outcome, payload, _lead = self.drive(
            SCOUTING, script, baseline=baseline)
        self.assertEqual(payload["kind"], "review_round_cap")
        self.assertEqual(payload["requires"], "answer")
        self.assertFalse(payload["approved"])
        self.assertEqual(payload["authority_blocking_ids"], self.af_ids())
        self.assert_not_advanced(outcome, baseline)
        self.assertEqual(self.chain_records("resolution"), [])
        self.assertEqual(len(self.screened()), cowork.REVIEW_ROUND_CAP - 1)

    def test_a_non_approve_is_never_promoted_to_an_approve(self):
        outcome, payload, lead = self.closure_flow(tail=(NEEDS_USER,))
        # everything was closable at round 2, yet that revise stayed a revise
        self.assertEqual(self.chain_records("resolution")[0]["round"], 2)
        self.assertEqual(payload["kind"], "reviewer_question")
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(len(lead.sent), 3)
        self.assertEqual(self.screened(), [])


class ProposalRecordingTests(GateEnv):
    """The runtime records the lead's proposals, bound to the reviewed
    candidate, and ignores everything else the lead writes there."""

    def run_proposal(self, result_fn, second=None):
        return self.drive(
            SCOUTING,
            [_revise(_finding("gap")),
             second or _revise(_finding("again", "major")), NEEDS_USER],
            result_fn=result_fn)

    def test_a_valid_entry_becomes_one_runtime_bound_proposal(self):
        missing = os.path.join(self.tmp, "missing.txt")
        self.run_proposal(self.proposing({2}, paths=[self.evidence, missing]))
        proposals = self.chain_records("proposal")
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        round_two = self.chain_records("round")[1]
        self.assertEqual(proposal["finding_ids"], self.af_ids()[:1])
        self.assertEqual(proposal["author_role"], "scout")
        self.assertEqual(proposal["requires"], [])
        self.assertEqual(proposal["candidate"], round_two["candidate"])
        evidence = proposal["changed_evidence"]
        self.assertEqual(evidence["candidate"], round_two["candidate"])
        self.assertEqual(evidence["paths"], [self.evidence])
        self.assertEqual(evidence["sha256s"], [_sha("evidence bytes")])
        dropped = self.events("authority.proposal.path_dropped")
        self.assertEqual([e["count"] for e in dropped], [1])
        recorded = self.events("authority.proposal.recorded")
        self.assertEqual([e["proposal_id"] for e in recorded],
                         [proposal["id"]])

    def test_lead_written_values_are_ignored_and_traced(self):
        extra = {"candidate": CAND, "sha256s": [HEX2],
                 "changed_evidence": {"paths": ["/x"], "sha256s": [HEX2],
                                      "candidate": CAND},
                 "author_role": "builder", "outcome": "closed",
                 "decided_by": "supervisor_agent", "closure": "closed"}
        self.run_proposal(self.proposing({2}, extra=extra))
        proposal = self.chain_records("proposal")[0]
        self.assertNotEqual(proposal["changed_evidence"]["candidate"], CAND)
        self.assertEqual(proposal["changed_evidence"]["candidate"],
                         self.chain_records("round")[1]["candidate"])
        self.assertEqual(proposal["changed_evidence"]["sha256s"],
                         [_sha("evidence bytes")])
        self.assertEqual(proposal["author_role"], "scout")
        ignored = self.events("authority.proposal.field_ignored")
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0]["keys"], sorted(extra))
        self.assertEqual(self.chain_records("resolution"), [])

    def test_unusable_entries_are_skipped_with_a_reason(self):
        self.drive(PLANNING, [_revise(_finding("plan gap")), NEEDS_USER])
        other_phase = self.af_ids()[0]
        cases = (
            ("unknown_or_foreign_id", {"ids": ["AF-9999"]}),
            ("unknown_or_foreign_id", {"ids": [other_phase]}),
            ("malformed_requires", {"requires": ["not_a_capability"]}),
            ("malformed_requires", {"requires": "scope_expansion"}),
            ("malformed_ids", {"ids": []}),
            ("malformed_paths", {"paths": "a-string"}))
        for reason, options in cases:
            with self.subTest(reason=reason):
                before = len(self.chain_records("proposal"))
                seen = len(self.events("authority.proposal.rejected"))
                self.run_proposal(self.proposing({2}, **options))
                self.assertEqual(len(self.chain_records("proposal")), before)
                rejected = self.events("authority.proposal.rejected")[seen:]
                self.assertEqual([e["reason"] for e in rejected], [reason])

    def test_a_list_that_is_not_a_list_is_skipped(self):
        self.run_proposal(
            lambda n: {"resolution_proposals": "nope"} if n == 2 else {})
        self.assertEqual(self.chain_records("proposal"), [])
        rejected = self.events("authority.proposal.rejected")
        self.assertEqual([e["reason"] for e in rejected], ["malformed_list"])

    def test_status_bytes_that_moved_since_review_skip_the_intake(self):
        def mutating(path):
            with open(path, "w") as fh:
                json.dump({"status": "ready_for_review", "result": {
                    "turn": "changed under review",
                    "resolution_proposals": [{
                        "authority_ids": self.af_ids()[:1],
                        "changed_evidence_paths": []}]}}, fh)
            return _revise(_finding("again", "major"))

        self.run_proposal(None, second=mutating)
        self.assertEqual(self.chain_records("proposal"), [])
        skipped = self.events("authority.proposal.skipped")
        self.assertEqual([e["reason"] for e in skipped], ["status_changed"])

    def test_an_identical_re_intake_replays_without_a_duplicate(self):
        self.run_proposal(self.proposing({2, 3}))
        before = len(self.chain_records("proposal"))
        path = self.status_path(SCOUTING)
        args = (self.sid, SCOUTING, "scout", path, self.status_sha(SCOUTING),
                self.status_candidate(SCOUTING), None)
        cowork._record_lead_proposals(*args)
        self.assertEqual(len(self.chain_records("proposal")), before + 1)
        cowork._record_lead_proposals(*args)
        self.assertEqual(len(self.chain_records("proposal")), before + 1)


class ClosureTests(GateEnv):
    """A finding closes only through `closure_decision`."""

    def test_the_two_round_path_closes_and_advances(self):
        outcome, _payload, lead = self.closure_flow()
        self.assertEqual(outcome, "approved")
        finding = self.chain_records("finding")[0]
        proposals = self.chain_records("proposal")
        recommendations = self.chain_records("recommendation")
        resolutions = self.chain_records("resolution")
        self.assertEqual(len(resolutions), 1)
        resolution = resolutions[0]
        self.assertEqual(resolution["basis"], {
            "proposal_id": proposals[0]["id"],
            "recommendation_id": recommendations[0]["id"],
            "decision_id": None, "witness_ids": []})
        self.assertEqual(
            (resolution["finding_id"], resolution["outcome"],
             resolution["decided_by"]),
            (finding["id"], "closed", "control_plane"))
        # raised on one candidate, closed on the one that was judged next
        self.assertEqual(resolution["candidate"],
                         self.chain_records("round")[1]["candidate"])
        self.assertNotEqual(resolution["candidate"], finding["candidate"])
        self.assertEqual(self.folded()["findings"][finding["id"]]["state"],
                         "closed")
        self.assertEqual(len(self.chain_records("round")), 3)
        # once closed, the lead is no longer told it lacks a basis
        self.assertNotIn(finding["id"], lead.sent[2])

    def test_every_resolution_equals_the_closure_decision_before_it(self):
        self.closure_flow()
        records = self.chain_read()["records"]
        seen = 0
        for index, record in enumerate(records):
            if record["kind"] != "resolution":
                continue
            seen += 1
            before = chain.fold(records[:index])
            basis = record["basis"]
            proposal = before["proposals"][basis["proposal_id"]]
            self.assertIn(record["finding_id"], proposal["finding_ids"])
            row = [r for r in before["recommendations"][record["finding_id"]]
                   if r["id"] == basis["recommendation_id"]][0]
            latest = [r for r in before["rounds"]
                      if r["phase"] == record["phase"]][-1]
            self.assertEqual(
                (row["recommend"], row["round_id"]),
                ("closed", latest["round_id"]))
            coverage = {pid: True for pid in before["proposals"]}
            decision = authority_gate.closure_decision(
                before, record["finding_id"], record["candidate"], {},
                coverage, {})
            self.assertTrue(decision["close"], decision)
        self.assertEqual(seen, 1)

    def test_a_stale_proposal_cannot_be_revived_by_a_lead_written_candidate(self):
        def result_fn(number):
            if number == 2:
                return self.proposing({2})(number)
            if number == 3:
                forged = self.chain_records("round")[1]["candidate"]
                return {"resolution_proposals": [{
                    "authority_ids": ["AF-9999"],
                    "changed_evidence": {"candidate": forged}}]}
            return {}

        outcome, _payload, _lead = self.drive(
            SCOUTING,
            [_revise(_finding("gap")),
             lambda path: _revise(_recommend(self.af_ids()[0], "still_open")),
             lambda path: _revise(_recommend(self.af_ids()[0])), NEEDS_USER],
            result_fn=result_fn)
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(self.chain_records("resolution"), [])
        self.assertEqual(self.folded()["findings"][self.af_ids()[0]]["state"],
                         "open")
        self.assertEqual(len(self.chain_records("proposal")), 1)

    def test_a_proposal_bound_to_another_candidate_is_invalid(self):
        def move_receipt(path):
            self.write_pointer(self.owned_pointer(
                transaction_id="T-next", manifest_digest=HEX2,
                index_digest=HEX))
            return _revise(_recommend(self.af_ids()[0]))

        outcome, _payload, _lead = self.drive(
            BUILDING,
            [_revise(_finding("gap")), move_receipt, NEEDS_USER],
            pointer=self.owned_pointer(),
            result_fn=self.proposing({2}, phase=BUILDING))
        self.assertNotEqual(outcome, "approved")
        proposal = self.chain_records("proposal")[0]
        self.assertEqual(proposal["changed_evidence"]["candidate"],
                         {"kind": "owned_receipt", "manifest_digest": HEX,
                          "index_digest": HEX2})
        self.assertEqual(self.chain_records("resolution"), [])

    def test_a_recommendation_that_does_not_say_closed_closes_nothing(self):
        cases = ({"closure": "still_open"}, {"disposition": "withdrawn"},
                 {"disposition": "duplicate"})
        for marking in cases:
            with self.subTest(marking=marking):
                self.reset_session_authority()
                self.drive(
                    SCOUTING,
                    [_revise(_finding("gap")),
                     lambda path, m=marking: _revise(
                         dict(finding_ref=self.af_ids()[0], **m)),
                     NEEDS_USER],
                    result_fn=self.proposing({2}))
                self.assertEqual(self.chain_records("resolution"), [])

    def test_a_recommendation_of_an_earlier_round_is_not_current(self):
        outcome, _payload, _lead = self.drive(
            SCOUTING,
            [_revise(_finding("gap")),
             lambda path: _revise(_recommend(self.af_ids()[0])),
             _revise(_finding("note", "major")), NEEDS_USER],
            result_fn=self.proposing({3}))
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(len(self.chain_records("proposal")), 1)
        self.assertEqual(self.chain_records("resolution"), [])
        self.assertEqual(self.folded()["findings"][self.af_ids()[0]]["state"],
                         "open")

    def test_a_replayed_closure_batch_writes_one_resolution(self):
        real = cowork._chain_append
        state = {"raised": False}

        def commit_then_doubt(session_uuid, records, expected_head=None):
            committed = real(session_uuid, records, expected_head)
            if (not state["raised"]
                    and any(r["kind"] == "resolution" for r in records)):
                state["raised"] = True
                raise chain.AuthorityCommitUncertain("outcome unknown")
            return committed

        with mock.patch.object(cowork, "_chain_append",
                               side_effect=commit_then_doubt):
            outcome, _payload, _lead = self.closure_flow()
        self.assertTrue(state["raised"])
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(self.chain_records("resolution")), 1)

    def test_a_second_closure_of_a_closed_finding_is_refused(self):
        self.closure_flow()
        candidate = self.chain_records("round")[1]["candidate"]
        wrote = cowork._close_resolved_findings(
            self.sid, SCOUTING, candidate, None)
        self.assertFalse(wrote)
        self.assertEqual(len(self.chain_records("resolution")), 1)
        decision = authority_gate.closure_decision(
            self.folded(), self.af_ids()[0], candidate, {}, {}, {})
        self.assertEqual(decision["reasons"], ["finding_not_open"])


class CoverageTests(GateEnv):
    """A `requires` the lead's policy does not cover keeps the finding open."""

    def test_an_uncovered_capability_keeps_the_finding_open(self):
        outcome, _payload, _lead = self.closure_flow(
            requires=["scope_expansion"], tail=(NEEDS_USER,))
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(self.chain_records("resolution"), [])
        af = self.af_ids()[0]
        needed = self.events("authority.escalation_needed")
        self.assertTrue(needed)
        for event in needed:
            self.assertEqual((event["finding_id"], event["capability"]),
                             (af, "scope_expansion"))
        folded = self.folded()
        coverage = cowork._lead_proposal_coverage(folded)
        self.assertEqual(set(coverage.values()), {False})
        decision = authority_gate.closure_decision(
            folded, af, self.chain_records("round")[1]["candidate"], {},
            coverage, {})
        self.assertEqual(
            (decision["close"], decision["reasons"],
             decision["escalation_needed"]),
            (False, ["capability_not_granted"], "scope_expansion"))
        self.assertEqual(folded["findings"][af]["state"], "open")

    def test_an_empty_or_absent_requires_is_covered(self):
        for requires in (None, []):
            with self.subTest(requires=requires):
                self.reset_session_authority()
                outcome, _payload, _lead = self.closure_flow(
                    requires=requires)
                self.assertEqual(outcome, "approved")
                self.assertEqual(len(self.chain_records("resolution")), 1)


class SupersededChallengeFlowTest(GateEnv):
    """A defeated verification challenge is non-blocking; after the stop and
    an answer the reviewer's approve completes the phase."""

    def test_a_superseded_challenge_never_blocks_the_next_approve(self):
        challenge = _finding("the receipt is wrong")
        challenge["verification_challenge"] = {"reason_code": "no_evidence"}
        pointer = self.owned_pointer()
        first = self.drive(BUILDING, [_revise(challenge)], pointer=pointer)
        self.assertEqual(first[1]["kind"], "review_not_approved")
        row = self.chain_records("finding")[0]
        self.assertFalse(row["blocking"])
        self.assertEqual(row["superseded_by_transaction"], TXN)
        # the supervisor answers: a new run, the reviewer approves
        outcome, _payload, _lead = self.drive(
            BUILDING, [APPROVE], pointer=pointer)
        self.assertEqual(outcome, "approved")
        folded_row = self.folded()["findings"][row["id"]]
        self.assertIs(folded_row["blocking"], False)
        self.assertEqual(self.chain_records("resolution"), [])
        self.assertEqual(self.screened_count(), 0)

    def screened_count(self):
        return len(self.events("authority.approve_screened"))


class ProposalReintakeAcrossRoundsTests(GateEnv):
    """A status that keeps carrying an already-recorded proposal is read again
    in every later round: a repeat inside a round writes nothing, a later round
    records anew, and neither is ever a chain fault."""

    def stopped_at_the_cap(self, phase=SCOUTING, pointer=None):
        """Drive the loop to its round-cap stop with a proposal recorded in
        every round from the second; returns the chain records."""
        cap = cowork.REVIEW_ROUND_CAP
        if phase == SCOUTING:
            script = ([_revise(_finding("gap"))] + [APPROVE] * (cap - 2)
                      + [lambda path: _revise(_recommend(self.af_ids()[0]))])
        else:
            script = ([_revise(_finding("gap"))]
                      + [_revise(_finding("more", "major"))] * (cap - 1))
        _outcome, payload, _lead = self.drive(
            phase, script, pointer=pointer,
            result_fn=self.proposing(set(range(2, cap + 1)), phase=phase))
        self.assertEqual(payload["kind"], "review_round_cap")
        self.assertEqual(payload["requires"], "answer")
        return self.chain_read()["records"]

    def resumed_unchanged(self, script=(APPROVE,)):
        """The answer-resume: a second loop whose lead leaves the status
        file, proposals included, exactly as it was."""
        return self.drive(SCOUTING, list(script), keep_status=True)

    def assert_no_chain_fault(self, payload):
        self.assertNotEqual((payload or {}).get("kind"),
                            "authority_unavailable")
        self.assertEqual(
            [e for e in self.events("gate.decision")
             if e.get("gate") == "authority_unavailable"], [])

    def distinct_keys(self):
        keys = [r.get("op_key") for r in self.chain_records("proposal")]
        self.assertNotIn(None, keys)
        self.assertEqual(len(keys), len(set(keys)))

    @staticmethod
    def of_kind(records, kind):
        return [r for r in records if r["kind"] == kind]

    def test_unchanged_status_after_a_cap_stop_resumes_to_approval(self):
        before = self.stopped_at_the_cap()
        earlier = self.of_kind(before, "proposal")
        self.assertEqual(len(self.of_kind(before, "resolution")), 1)
        outcome, payload, _lead = self.resumed_unchanged()
        self.assertEqual(outcome, "approved")
        self.assert_no_chain_fault(payload)
        after = self.chain_read()["records"]
        self.assertEqual(after[:len(before)], before)
        added = self.of_kind(after, "proposal")[len(earlier):]
        self.assertEqual(len(added), 1)
        new, last = added[0], earlier[-1]
        self.assertEqual(new["candidate"], last["candidate"])
        self.assertEqual(new["changed_evidence"], last["changed_evidence"])
        self.assertNotEqual(new["id"], last["id"])
        self.assertNotEqual(new["op_key"], last["op_key"])
        self.assertEqual(new["round"], self.of_kind(after, "round")[-1]["round"])
        self.assertNotEqual(new["round"], last["round"])
        self.assertEqual(len(self.of_kind(after, "resolution")), 1)
        self.distinct_keys()

    def test_changed_proposal_content_is_a_new_proposal(self):
        before = self.stopped_at_the_cap()
        earlier = self.of_kind(before, "proposal")
        with open(self.evidence, "w") as fh:
            fh.write("changed evidence bytes")
        outcome, payload, _lead = self.resumed_unchanged()
        self.assertEqual(outcome, "approved")
        self.assert_no_chain_fault(payload)
        after = self.chain_read()["records"]
        self.assertEqual(after[:len(before)], before)
        added = self.of_kind(after, "proposal")[len(earlier):]
        self.assertEqual(len(added), 1)
        digests = added[0]["changed_evidence"]["sha256s"]
        self.assertEqual(digests, [_sha("changed evidence bytes")])
        self.assertNotEqual(digests, earlier[-1]["changed_evidence"]["sha256s"])
        self.distinct_keys()

    def test_a_same_round_repeat_adds_no_record(self):
        self.stopped_at_the_cap()
        outcome, _payload, _lead = self.resumed_unchanged()
        self.assertEqual(outcome, "approved")
        count = len(self.chain_records("proposal"))
        head = self.chain_read()["head"]
        args = (self.sid, SCOUTING, "scout", self.status_path(SCOUTING),
                self.status_sha(SCOUTING), self.status_candidate(SCOUTING),
                None)
        for _ in range(2):
            cowork._record_lead_proposals(*args)
            self.assertEqual(len(self.chain_records("proposal")), count)
            self.assertEqual(self.chain_read()["head"], head)

    def test_a_repeated_entry_in_one_status_is_recorded_once(self):
        single = self.proposing({2})

        def doubled(number):
            result = single(number)
            if result:
                result["resolution_proposals"] *= 2
            return result

        _outcome, payload, _lead = self.drive(
            SCOUTING,
            [_revise(_finding("gap")), _revise(_finding("again", "major")),
             NEEDS_USER],
            result_fn=doubled)
        self.assert_no_chain_fault(payload)
        self.assertEqual(len(self.chain_records("proposal")), 1)
        skipped = self.events("authority.proposal.skipped")
        self.assertEqual([(e["index"], e["reason"]) for e in skipped],
                         [(1, "duplicate_entry")])
        self.distinct_keys()

    def test_a_moved_candidate_is_a_new_proposal_not_a_fault(self):
        before = self.stopped_at_the_cap(BUILDING, pointer=self.owned_pointer())
        earlier = self.of_kind(before, "proposal")
        moved = self.owned_pointer(manifest_digest=HEX2, index_digest=HEX)
        _outcome, payload, _lead = self.drive(
            BUILDING, [APPROVE, NEEDS_USER], pointer=moved,
            result_fn=self.proposing({1}, phase=BUILDING))
        self.assert_no_chain_fault(payload)
        after = self.chain_read()["records"]
        self.assertEqual(after[:len(before)], before)
        added = self.of_kind(after, "proposal")[len(earlier):]
        self.assertEqual(len(added), 1)
        self.assertEqual(
            added[0]["candidate"],
            {"kind": "owned_receipt", "manifest_digest": HEX2,
             "index_digest": HEX})
        self.assertNotEqual(added[0]["candidate"], earlier[-1]["candidate"])
        self.distinct_keys()


class FinalGateTests(GateEnv):
    """The read-only final gate runs before any approval effect or gate
    event, also for a carried hash-gate approval."""

    def gate_counts(self):
        return (len(self.events("gate.show")),
                len([e for e in self.events("gate.decision")
                     if e.get("action") == "approve"]))

    def test_a_carried_approval_with_an_open_blocker_is_blocked(self):
        baseline = _Baseline(self.order)
        self.drive(SCOUTING, [APPROVE], baseline=baseline)
        self.assertTrue(baseline.approved)
        self.inject_blocker(SCOUTING)
        before = self.gate_counts()
        grants = self.order.count("grant")
        outcome, payload, _lead = self.drive(SCOUTING, [], baseline=baseline)
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(
            (payload["kind"], payload["requires"], payload["stage"]),
            ("review_not_approved", "answer", "final_gate"))
        self.assertEqual(len(self.events("review.skipped")), 1)
        self.assertEqual(self.gate_counts(), before)
        self.assertEqual(self.order.count("grant"), grants)
        self.assertEqual(len(self.events("authority.final_gate_blocked")), 1)

    def race(self, phase):
        """Run the real final gate after a late authoritative append."""
        real = cowork._authority_final_gate

        def racing(*args, **kwargs):
            self.inject_blocker(phase)
            return real(*args, **kwargs)

        return mock.patch.object(
            cowork, "_authority_final_gate", side_effect=racing)

    def test_a_late_append_between_the_boundary_and_the_final_gate_blocks(self):
        baseline = _Baseline(self.order)
        with self.race(SCOUTING):
            outcome, payload, _lead = self.drive(
                SCOUTING, [APPROVE], baseline=baseline)
        self.assert_not_advanced(outcome, baseline)
        self.assertEqual((payload["kind"], payload["requires"]),
                         ("review_not_approved", "answer"))
        self.assertEqual(len(self.events("authority.head_moved")), 1)

    def test_a_blocked_final_gate_leaves_a_builder_with_no_effect(self):
        baseline = _Baseline(self.order)
        profile = _Profile(self.order)
        pointer = self.owned_pointer()
        with self.race(BUILDING):
            outcome, payload, _lead = self.drive(
                BUILDING, [APPROVE], pointer=pointer, baseline=baseline,
                profile=profile)
        self.assert_not_advanced(outcome, baseline)
        self.assertEqual(payload["kind"], "review_not_approved")
        self.assertEqual(profile.approvals, [])
        self.assertEqual(self.order, [])
        self.assertEqual(
            state_store.read_current_receipt_pointer(self.sid)["disposition"],
            "pending_review")

    def test_a_passing_final_gate_runs_the_effects_ahead_of_the_gate_events(self):
        baseline = _Baseline(self.order)
        profile = _Profile(self.order)
        real = self.trace.event

        def watching(name, *args, **kwargs):
            if name == "gate.show":
                self.order.append("gate.show")
            return real(name, *args, **kwargs)

        with mock.patch.object(self.trace, "event", side_effect=watching):
            outcome, _payload, _lead = self.drive(
                BUILDING, [APPROVE], pointer=self.owned_pointer(),
                baseline=baseline, profile=profile)
        self.assertEqual(outcome, "approved")
        self.assertEqual(self.order[:4],
                         ["profile", "disposition", "baseline", "gate.show"])
        self.assertEqual(self.order.count("disposition"), 1)
        self.assertEqual(self.order.count("grant"), 1)
        self.assertEqual(self.dispositions,
                         [verification.DISPOSITION_ACCEPTED])
        self.assertEqual(profile.approvals[0][1], HEX)

    def test_a_stale_verification_stop_keeps_the_effects_before_the_grant(self):
        self.manifest_matches = False
        baseline = _Baseline(self.order)
        outcome, payload, _lead = self.drive(
            BUILDING, [APPROVE], pointer=self.owned_pointer(),
            baseline=baseline, profile=_Profile(self.order))
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(payload["kind"], "verification_not_current")
        self.assertEqual(self.order.count("grant"), 1)
        self.assertLess(self.order.index("disposition"),
                        self.order.index("grant"))
        self.assertEqual(self.dispositions,
                         [verification.DISPOSITION_REJECTED])

    def test_the_final_gate_never_writes(self):
        self.drive(SCOUTING, [APPROVE])
        head = self.chain_read()["head"]
        result = cowork._authority_final_gate(
            self.sid, SCOUTING, "scout", self.status_path(SCOUTING))
        self.assertFalse(result["blocked"])
        self.assertEqual(self.chain_read()["head"], head)
        self.inject_blocker(SCOUTING)
        head = self.chain_read()["head"]
        result = cowork._authority_final_gate(
            self.sid, SCOUTING, "scout", self.status_path(SCOUTING))
        self.assertTrue(result["blocked"])
        self.assertEqual(self.chain_read()["head"], head)


class FailClosedTests(GateEnv):
    """A strict fault at the boundary or the final gate stops; nothing
    approves after one, and a lease loss propagates."""

    def approvals(self):
        return len([e for e in self.events("gate.decision")
                    if e.get("action") == "approve"])

    def assert_unavailable(self, result, reason, stage):
        """The stop's shape; no approval was decided beyond `self.seeded`
        (those of a carried-approval setup)."""
        outcome, payload, _lead = result
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "authority_unavailable")
        self.assertEqual(payload["requires"], "operator")
        self.assertFalse(payload["approved"])
        self.assertEqual(payload["reason"], reason)
        self.assertEqual(payload["stage"], stage)
        self.assertEqual(self.approvals(), getattr(self, "seeded", 0))
        self.assertEqual(self.settled_dispositions(), [])

    def seed_carried_approval(self):
        baseline = _Baseline(self.order)
        self.drive(SCOUTING, [APPROVE], baseline=baseline)
        self.assertTrue(baseline.approved)
        self.seeded = self.approvals()
        return baseline

    def test_an_unreadable_chain_stops_the_final_gate(self):
        baseline = self.seed_carried_approval()
        with open(self.chain_path, "wb") as fh:
            fh.write(b"not a chain\n")
        result = self.drive(SCOUTING, [], baseline=baseline)
        self.assert_unavailable(result, "chain_unreadable", "final_gate")
        with open(self.chain_path, "rb") as fh:
            self.assertEqual(fh.read(), b"not a chain\n")

    def test_a_lost_chain_stops_the_final_gate(self):
        baseline = self.seed_carried_approval()
        self.lose_chain()
        result = self.drive(SCOUTING, [], baseline=baseline)
        self.assert_unavailable(result, "chain_lost", "final_gate")
        self.assertFalse(os.path.exists(self.chain_path))

    def test_no_candidate_stops_the_final_gate(self):
        baseline = self.seed_carried_approval()
        with mock.patch.object(cowork, "_gate_candidate",
                               return_value=(None, "status_unavailable")):
            result = self.drive(SCOUTING, [], baseline=baseline)
        self.assert_unavailable(result, "candidate_unavailable", "final_gate")
        decision = [e for e in self.events("gate.decision")
                    if e.get("gate") == "authority_unavailable"][-1]
        self.assertEqual(decision["candidate_reason"], "status_unavailable")

    def fail_candidate_on(self, *numbers):
        real = cowork._gate_candidate
        calls = []

        def flaky(*args):
            calls.append(1)
            if len(calls) in numbers:
                return None, "status_unavailable"
            return real(*args)

        return mock.patch.object(cowork, "_gate_candidate", side_effect=flaky)

    def test_no_candidate_at_the_boundary_stops_the_gate(self):
        baseline = _Baseline(self.order)
        with self.fail_candidate_on(2):
            result = self.drive(SCOUTING, [APPROVE], baseline=baseline)
        self.assert_unavailable(result, "candidate_unavailable", "boundary")
        self.assertFalse(baseline.approved)
        self.assertEqual(self.chain_records("resolution"), [])

    def test_no_candidate_at_the_final_gate_stops_an_approve(self):
        baseline = _Baseline(self.order)
        with self.fail_candidate_on(3):
            result = self.drive(SCOUTING, [APPROVE], baseline=baseline)
        self.assert_unavailable(result, "candidate_unavailable", "final_gate")
        self.assertFalse(baseline.approved)
        self.assertEqual(self.events("gate.show"), [])

    def test_a_persistent_head_conflict_stops_the_boundary(self):
        real = cowork._chain_append
        batches = []

        def conflicting(session_uuid, records, expected_head=None):
            if any(r["kind"] in ("proposal", "resolution") for r in records):
                batches.append(1)
                raise chain.AuthorityHeadConflict(expected_head, "other")
            return real(session_uuid, records, expected_head)

        with mock.patch.object(cowork, "_chain_append",
                               side_effect=conflicting):
            result = self.drive(
                SCOUTING,
                [_revise(_finding("gap")),
                 lambda path: _revise(_recommend(self.af_ids()[0])),
                 NEEDS_USER],
                result_fn=self.proposing({2}))
        self.assert_unavailable(result, "head_conflict", "boundary")
        self.assertEqual(len(batches), 2)
        self.assertEqual(self.chain_records("proposal"), [])

    def test_a_lease_lost_before_the_final_read_propagates(self):
        armed = {"revoked": False}
        self.revoked(armed)
        baseline = self.seed_carried_approval()
        head = self.chain_read()["head"]
        counts = (len(self.events("gate.show")),)
        armed["revoked"] = True
        with self.assertRaises(cowork_owner.OwnerLeaseError):
            self.drive(SCOUTING, [], baseline=baseline)
        self.assertEqual(self.chain_read()["head"], head)
        self.assertEqual((len(self.events("gate.show")),), counts)

    def test_a_lease_lost_before_the_boundary_append_propagates(self):
        armed = {"revoked": False}
        self.revoked(armed)
        real = cowork._authority_boundary

        def arming(*args, **kwargs):
            armed["revoked"] = True
            return real(*args, **kwargs)

        with mock.patch.object(cowork, "_authority_boundary",
                               side_effect=arming):
            with self.assertRaises(cowork_owner.OwnerLeaseError):
                self.drive(
                    SCOUTING,
                    [_revise(_finding("gap")),
                     lambda path: _revise(_recommend(self.af_ids()[0])),
                     NEEDS_USER],
                    result_fn=self.proposing({2}))
        self.assertTrue(armed["revoked"])
        self.assertEqual(self.chain_records("proposal"), [])
        self.assertEqual(self.chain_records("resolution"), [])


class OrphanAuthorityTests(GateEnv):
    """Surfaces beside the chain, and a round left by a crash, never close or
    unblock a finding."""

    def test_surfaces_that_claim_a_closure_unblock_nothing(self):
        self.drive(SCOUTING, [_revise(_finding("gap")), NEEDS_USER],
                   eval_on=True)
        af = self.af_ids()[0]
        ledger.append_closure(
            state_store.ledger_path_for(self.sid),
            closes=self.ledger_findings()[0]["id"], phase=SCOUTING,
            round_index=1)
        with open(os.path.join(self.assets, "round_epochs.json"), "w") as fh:
            json.dump({"scouting|scout": 9, "scouting|scout-reviewer": 9}, fh)
        self.trace.event("review.round.recorded", role="scout-reviewer",
                         phase=SCOUTING, authority_round=1, closed_ids=[af])
        outcome, _payload, _lead = self.drive(
            SCOUTING, [APPROVE, NEEDS_USER], eval_on=True)
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(self.chain_records("resolution"), [])
        self.assertEqual(self.folded()["findings"][af]["state"], "open")
        self.assertEqual(len(self.events("authority.approve_screened")), 1)

    def test_a_round_left_by_a_crash_cannot_authorize_a_closure(self):
        self.drive(SCOUTING, [_revise(_finding("gap")), NEEDS_USER])
        af = self.af_ids()[0]
        # the commit of a verdict that recommended closure survived a crash
        cowork._record_verdict_round(
            self.sid, SCOUTING, "scout", "scout-reviewer",
            _revise(_recommend(af)), self.status_candidate(SCOUTING),
            self.review_path(SCOUTING))
        outcome, _payload, _lead = self.drive(
            SCOUTING, [APPROVE, NEEDS_USER], result_fn=self.proposing({1}))
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(self.chain_records("resolution"), [])
        self.assertEqual(len(self.chain_records("recommendation")), 1)
        self.assertEqual(len(self.chain_records("proposal")), 1)
        self.assertEqual(self.folded()["findings"][af]["state"], "open")


class LegacyRefTests(GateEnv):
    """A legacy finding reference resolves to the chain finding it names
    instead of opening a new blocker every round."""

    def test_a_ledger_reference_resolves_to_its_chain_finding(self):
        def cite(path):
            return _revise(_recommend(self.ledger_findings()[0]["id"],
                                      "still_open"))

        self.drive(SCOUTING, [_revise(_finding("gap")), cite, cite,
                              NEEDS_USER])
        self.assertEqual(len(self.af_ids()), 1)
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["still_open", "still_open"])
        self.assertEqual(self.events("authority.finding_ref_unresolved"), [])

    def test_a_repeated_unresolved_reference_opens_one_finding(self):
        ghost = _revise({"finding_ref": "F-99", "summary": "ghost",
                         "severity": "blocking"})
        with mock.patch.object(ledger, "append_record", return_value=None):
            self.drive(SCOUTING, [ghost, ghost, ghost, NEEDS_USER])
        self.assertEqual(len(self.af_ids()), 1)
        self.assertEqual(len(self.events("authority.finding_ref_unresolved")),
                         1)
        self.assertEqual(
            [r["recommend"] for r in self.chain_records("recommendation")],
            ["still_open", "still_open"])

    def test_an_alias_is_honoured_only_for_a_finding_of_the_same_phase(self):
        self.drive(PLANNING, [_revise(_finding("plan gap")), NEEDS_USER])
        planned = self.af_ids()[0]
        self.trace.event("authority.finding_ref_unresolved",
                         role="scout-reviewer", phase=SCOUTING, round=1,
                         finding_ref="F-7", finding_id=planned)
        self.trace.event("authority.finding_ref_unresolved",
                         role="scout-reviewer", phase=SCOUTING, round=1,
                         finding_ref="F-8", finding_id="AF-0099")
        self.drive(SCOUTING, [
            _revise({"finding_ref": "F-7", "summary": "a", "severity": "major"},
                    {"finding_ref": "F-8", "summary": "b", "severity": "major"}),
            NEEDS_USER])
        self.assertEqual(
            [f["summary"] for f in self.chain_records("finding")
             if f["phase"] == SCOUTING], ["a", "b"])
        self.assertEqual(self.chain_records("recommendation"), [])

    def test_a_reference_with_no_translation_source_is_a_new_finding(self):
        ghost = _revise({"finding_ref": "F-99", "summary": "ghost",
                         "severity": "major"})

        def without_sources(path):
            for target in (os.path.join(self.assets, "trace.jsonl"),
                           state_store.ledger_path_for(self.sid)):
                if os.path.exists(target):
                    os.unlink(target)
            return dict(ghost)

        self.drive(SCOUTING, [ghost, without_sources, NEEDS_USER])
        self.assertEqual(len(self.af_ids()), 2)
        self.assertEqual(self.chain_records("recommendation"), [])


class EmptyChainTests(GateEnv):
    """A provably empty chain holds no authority findings."""

    def test_a_carried_approval_over_an_empty_chain_is_not_blocked(self):
        baseline = _Baseline(self.order, approved=True)
        outcome, _payload, _lead = self.drive(
            SCOUTING, [], baseline=baseline)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(self.events("review.skipped")), 1)
        self.assertFalse(os.path.exists(self.chain_path))
        self.assertFalse(os.path.exists(self.chain_path + ".lock"))

    def test_a_sessionless_loop_has_no_chain_activity(self):
        outcome, _payload, _lead = self.drive(
            SCOUTING, [_revise(_finding("gap")), APPROVE], session=False)
        self.assertEqual(outcome, "approved")
        self.assertFalse(os.path.exists(self.chain_path))
        self.assertEqual(self.events("authority.approve_screened"), [])


class ExposureTests(GateEnv):
    """The authority ids reach the reviewer's brief and the lead's hand-back
    as static text; nothing else is added."""

    def test_the_revise_delivery_names_the_ids_without_the_findings(self):
        summary = "zzq unique summary"
        _outcome, _payload, lead = self.drive(
            SCOUTING, [_revise(_finding(summary)), NEEDS_USER])
        af = self.af_ids()[0]
        self.assertIn(af, lead.sent[1])
        self.assertIn(self.review_path(SCOUTING), lead.sent[1])
        self.assertNotIn(summary, lead.sent[1])

    def test_open_ids_follow_the_active_chain_gate(self):
        self.drive(SCOUTING, [_revise(_finding("gap")),
                              _revise(_finding("other", "major")),
                              NEEDS_USER])
        blocking = self.af_ids()[0]
        gate = {"session_uuid": self.sid, "phase": SCOUTING,
                "seat": "scout-reviewer"}
        self.assertEqual(
            cowork._reviewer_open_ids(self.sid, "scout-reviewer"), [])
        token = cowork._CHAIN_GATE_CTX.set(gate)
        try:
            self.assertEqual(
                cowork._reviewer_open_ids(self.sid, "scout-reviewer"),
                [blocking])
            self.assertEqual(
                cowork._reviewer_open_ids(self.sid, "planning-advisor"), [])
        finally:
            cowork._CHAIN_GATE_CTX.reset(token)
        fragment = cowork._reviewer_open_ids_fragment([blocking])
        self.assertIn(blocking, fragment)
        self.assertNotIn("gap", fragment)
        brief = cowork.assemble_reviewer_brief("/review.json")
        self.assertTrue(cowork._is_known_static_fragment(
            brief + "\n\n" + fragment))

    def test_run_reviewer_once_adds_the_fragment_to_fresh_and_resume_briefs(self):
        intel = os.path.join(self.tmp, "intel.json")
        review = os.path.join(self.tmp, "review.json")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {
                "success_criteria": [{"statement": "s"}]}}, fh)
        prompts = []

        def factory(controller, io_out):
            class Reviewer:
                def send(self, text, meta=None):
                    prompts.append(str(text))
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return Reviewer()

        cfg = cowork.default_config(["scout", cowork.SCOUT_REVIEWER])
        team = ["scout", cowork.SCOUT_REVIEWER]
        with mock.patch.object(cowork, "_reviewer_open_ids",
                               return_value=["AF-0002", "AF-0003"]):
            for resume in (None, "resumed-thread"):
                cowork.run_reviewer_once(
                    cfg, "goal", team, intel, review,
                    session_factory=factory, resume_id=resume)
        with mock.patch.object(cowork, "_reviewer_open_ids", return_value=[]):
            cowork.run_reviewer_once(
                cfg, "goal", team, intel, review, session_factory=factory)
        self.assertEqual(len(prompts), 3)
        for prompt in prompts[:2]:
            self.assertIn("AF-0002", prompt)
            self.assertIn("AF-0003", prompt)
            self.assertIn("Write your verdict", prompt)
        self.assertNotIn("AF-0002", prompts[2])


class DocContractTests(unittest.TestCase):
    """The role documents and README state the proposal and recommendation
    contract."""

    def read(self, *parts):
        with open(os.path.join(cowork.SKILL_ROOT, *parts)) as fh:
            return " ".join(fh.read().split())

    def test_lead_roles_document_resolution_proposals(self):
        for name in ("builder", "planner", "scout"):
            with self.subTest(role=name):
                text = self.read("roles", name + ".md")
                for needle in ("resolution_proposals", "authority_ids",
                               "changed_evidence_paths", "requires",
                               "never write a candidate",
                               "A proposal alone closes nothing",
                               "scope_expansion", "policy_exception",
                               "Any other token voids the whole entry"):
                    self.assertIn(needle, text)
                for token in chain.CAPABILITIES:
                    self.assertIn(token, text)

    def test_the_documented_requires_tokens_are_the_closed_set(self):
        self.assertEqual(tuple(sorted(chain.CAPABILITIES)),
                         ("policy_exception", "scope_expansion"))

    def test_reviewer_roles_document_the_recommendation_contract(self):
        for name in ("scout-reviewer", "planning-advisor", "build-reviewer"):
            with self.subTest(role=name):
                text = self.read("roles", name + ".md")
                for needle in ("Open blocking findings are listed in your brief",
                               "lists no open blocking id",
                               "carries the `closure=fixed` entry",
                               "returned to the"):
                    self.assertIn(needle, text)
        self.assertIn("execution profile",
                      self.read("roles", "build-reviewer.md"))

    def test_readme_states_the_gate_and_its_limits(self):
        text = self.read("README.md")
        for needle in ("result.resolution_proposals", "closure_decision",
                       "blocking_decision", "review_not_approved",
                       "authority.escalation_needed", "attestations"):
            self.assertIn(needle, text)


class StaticContractTests(unittest.TestCase):
    """The single call sites and the ordering hold in the source."""

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

    @staticmethod
    def called_names(node):
        names = set()
        for item in ast.walk(node):
            if isinstance(item, ast.Call):
                func = item.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_each_pure_decision_has_one_call_site(self):
        source = self.source()
        self.assertEqual(source.count("authority_gate.blocking_decision("), 1)
        self.assertEqual(source.count("authority_gate.closure_decision("), 1)
        decide = self.function(source, "_authority_decide")
        self.assertIn("blocking_decision", self.called_names(decide))
        close = self.function(source, "_close_resolved_findings")
        self.assertIn("closure_decision", self.called_names(close))
        for caller in ("_authority_boundary", "_authority_final_gate"):
            self.assertIn("_authority_decide",
                          self.called_names(self.function(source, caller)))

    def test_the_final_gate_only_reads(self):
        source = self.source()
        called = self.called_names(
            self.function(source, "_authority_final_gate"))
        for forbidden in ("_chain_append", "_commit_batch", "_append_planned",
                          "_record_lead_proposals", "_close_resolved_findings"):
            self.assertNotIn(forbidden, called)

    def test_the_gate_precedes_the_branches_and_the_approval_tail(self):
        source = inspect.getsource(cowork._role_loop)
        self.assertLess(source.index("_record_verdict_round("),
                        source.index("_authority_boundary("))
        self.assertLess(source.index("_authority_boundary("),
                        source.index('if v == "approve":'))
        final = source.index("_authority_final_gate(")
        effects = source.index("_apply_approval_effects(deferred_approval)",
                               final)
        self.assertLess(final, effects)
        self.assertLess(effects, source.rindex("_grant_gate_acceptance("))

    def test_approval_effects_live_only_in_the_deferred_function(self):
        tree = ast.parse(self.source())
        functions = {n.name: n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)}
        role_loop = functions["_role_loop"]
        nested = functions["_apply_approval_effects"]
        inside = {id(n) for n in ast.walk(nested)}
        for node in ast.walk(role_loop):
            if isinstance(node, ast.Attribute) and node.attr in (
                    "on_build_approved", "record"):
                if node.attr == "record" and not (
                        isinstance(node.value, ast.Name)
                        and node.value.id == "skip_baseline"):
                    continue
                self.assertIn(id(node), inside, node.attr)


if __name__ == "__main__":
    unittest.main()
