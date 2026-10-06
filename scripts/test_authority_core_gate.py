#!/usr/bin/env python3
"""Tests for the pure blocking-gate and closure predicates in
`cowork_authority_gate`.

The module is pure, so every input is a hand-built plain dict: digests are
short repeated characters, ids are placeholders, and the folded state mirrors
the plain-dict shape the chain fold produces without importing it. Rows are
table-driven and assert the exact reasons, never only blocked/close.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_authority_core_gate
"""

import ast
import copy
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_authority_candidate as cand  # noqa: E402
import cowork_authority_gate as gate  # noqa: E402

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64

SESSION = "SESSION-A"
OTHER_SESSION = "SESSION-B"
PHASE = "building"
OTHER_PHASE = "planning"
HEAD = "e" * 64
OTHER_HEAD = "f" * 64
FID = "AF-0001"
FID2 = "AF-0002"
FID3 = "AF-0003"
PID = "AP-0001"
PID2 = "AP-0002"
RID = "RD-0001"
RID2 = "RD-0002"
REQ = "RQ-0001"
REQ2 = "RQ-0002"
TOKEN = "scope_expansion"
TOKEN2 = "policy_exception"


def _owned(manifest=A, index=B):
    return {"kind": "owned_receipt", "manifest_digest": manifest, "index_digest": index}


def _status(sha=A):
    return {"kind": "status_artifact", "status_sha256": sha}


GATE = _owned(A, B)
OLD = _owned(C, B)
KIND_MISMATCH = _status(A)
NEXT = _owned(D, B)


def _finding(state="open", candidate=GATE, blocking=True, claim_class=None,
             session=SESSION, phase=PHASE):
    return {"state": state, "phase": phase, "candidate": candidate, "blocking": blocking,
            "claim_class": claim_class, "session_uuid": session}


def _proposal(ids=(FID,), candidate=GATE, **extra):
    record = {"finding_ids": list(ids), "candidate": OLD,
              "changed_evidence": {"paths": [], "sha256s": [], "candidate": candidate},
              "author_role": "builder"}
    record.update(extra)
    return record


def _round(round_id=RID, phase=PHASE, candidate=GATE, seat="builder", number=1):
    return {"round_id": round_id, "phase": phase, "seat": seat, "round": number,
            "candidate": candidate}


def _rec(recommend="closed", round_id=RID, rec_id="AR-0001"):
    return {"id": rec_id, "recommend": recommend, "round_id": round_id,
            "reviewer_seat": "reviewer"}


def _decision(adjudicates=((FID, "close"),), request_id=REQ, answer=D,
              principal="supervisor_agent", candidate=GATE):
    return {"request_id": request_id, "principal": principal, "answer_sha256": answer,
            "adjudicates": [{"finding_id": f, "outcome": o} for f, o in adjudicates],
            "candidate": candidate}


def _folded(findings=None, proposals=None, recommendations=None, rounds=None,
            decisions=None, session=SESSION, head=HEAD):
    return {
        "findings": {FID: _finding()} if findings is None else findings,
        "proposals": {} if proposals is None else proposals,
        "recommendations": {} if recommendations is None else recommendations,
        "rounds": [] if rounds is None else rounds,
        "decisions": {} if decisions is None else decisions,
        "grants": [], "witnesses": {}, "events": [],
        "session_uuid": session, "head": head,
    }


def _meta(ok=True, head=HEAD):
    return {"ok": ok, "head": head}


def _scene(finding_candidate=GATE, proposal_candidate=GATE, round_candidate=GATE, **over):
    """An open finding, a valid proposal and a current-round closed recommendation."""
    base = dict(
        findings={FID: _finding(candidate=finding_candidate)},
        proposals={PID: _proposal(candidate=proposal_candidate)},
        rounds=[_round(candidate=round_candidate)],
        recommendations={FID: [_rec()]},
    )
    base.update(over)
    return _folded(**base)


def _block(folded=None, meta=None, session=SESSION, phase=PHASE, gate_candidate=GATE):
    return gate.blocking_decision(_meta() if meta is None else meta,
                                  _scene() if folded is None else folded,
                                  session, phase, gate_candidate)


def _close(folded, finding_id=FID, gate_candidate=GATE, screens=(), coverage=None,
           consumed=None):
    return gate.closure_decision(folded, finding_id, gate_candidate, list(screens),
                                 {} if coverage is None else coverage,
                                 {} if consumed is None else consumed)


def _pairs(result):
    return [(r["id"], r["code"]) for r in result["reasons"]]


def _screen(approvable=True):
    return {"approvable": approvable, "verdict_override": None, "reasons": []}


CONSUMED = {REQ: D}


class BlockingChainReadTests(unittest.TestCase):
    """A1: an unreadable chain is blocked as chain_unreadable."""

    def test_unreadable_rows(self):
        rows = [
            ("ok false", _meta(ok=False), _scene()),
            ("ok missing", {"head": HEAD}, _scene()),
            ("ok not True", {"ok": 1, "head": HEAD}, _scene()),
            ("None meta", None, _scene()),
            ("non-dict meta", "ok", _scene()),
            ("folded None", _meta(), None),
            ("folded non-dict", _meta(), [1]),
            ("findings not a dict", _meta(), dict(_scene(), findings=[])),
            ("findings missing", _meta(), {"session_uuid": SESSION, "head": HEAD}),
            ("failed meta beside usable folded", _meta(ok=False), _scene()),
        ]
        for label, meta, folded in rows:
            with self.subTest(label):
                got = gate.blocking_decision(meta, folded, SESSION, PHASE, GATE)
                self.assertTrue(got["blocked"])
                self.assertEqual(got["reasons"], [{"id": None, "code": "chain_unreadable"}])
                expected_head = meta.get("head") if isinstance(meta, dict) else None
                self.assertEqual(got["head"], expected_head)

    def test_readable_chain_takes_head_from_folded_state(self):
        got = _block()
        self.assertEqual(got["head"], HEAD)

    def test_empty_chain_is_not_blocked(self):
        folded = _folded(findings={}, head=None)
        got = gate.blocking_decision(_meta(head=None), folded, SESSION, PHASE, GATE)
        self.assertEqual(got, {"blocked": False, "reasons": [], "head": None})


class BlockingFindingBindingTests(unittest.TestCase):
    """A2: candidate_unavailable / unresolved / stale / foreign."""

    def test_gate_candidate_unavailable_rows(self):
        malformed = [None, {}, {"kind": "owned_receipt"}, _status("A" * 64), "x", [GATE],
                     dict(GATE, extra=A)]
        for bad in malformed:
            with self.subTest(repr(bad)[:40]):
                got = _block(gate_candidate=bad)
                self.assertTrue(got["blocked"])
                self.assertEqual(_pairs(got), [(None, "candidate_unavailable")])

    def test_open_blocking_finding_by_candidate(self):
        rows = [
            ("equal owned", GATE, GATE, "unresolved"),
            ("equal status", _status(A), _status(A), "unresolved"),
            ("different digest", OLD, GATE, "stale_candidate"),
            ("different status digest", _status(B), _status(A), "stale_candidate"),
            ("kind mismatch owned vs status", KIND_MISMATCH, GATE, "stale_candidate"),
            ("kind mismatch status vs owned", GATE, _status(A), "stale_candidate"),
            ("garbage record candidate", {"kind": "x"}, GATE, "stale_candidate"),
            ("None record candidate", None, GATE, "stale_candidate"),
        ]
        for label, record_candidate, gate_candidate, code in rows:
            with self.subTest(label):
                folded = _folded(findings={FID: _finding(candidate=record_candidate)})
                got = _block(folded, gate_candidate=gate_candidate)
                self.assertTrue(got["blocked"])
                self.assertEqual(_pairs(got), [(FID, code)])

    def test_foreign_binding_rows(self):
        rows = [
            ("other session record", _finding(session=OTHER_SESSION)),
            ("other phase record", _finding(phase=OTHER_PHASE)),
            ("missing session record", {k: v for k, v in _finding().items()
                                         if k != "session_uuid"}),
            ("non-dict record", "AF"),
            ("None record", None),
        ]
        for label, record in rows:
            for gate_candidate in (GATE, None):
                with self.subTest((label, gate_candidate is None)):
                    got = _block(_folded(findings={FID: record}), gate_candidate=gate_candidate)
                    expected = [(FID, "foreign_binding")]
                    if gate_candidate is None:
                        expected = [(None, "candidate_unavailable")] + expected
                    self.assertEqual(_pairs(got), expected)

    def test_folded_session_disagreement_is_chain_level_foreign_binding(self):
        for folded_session in (OTHER_SESSION, None, ""):
            with self.subTest(folded_session):
                got = _block(_folded(session=folded_session))
                self.assertEqual(_pairs(got)[0], (None, "foreign_binding"))
                self.assertTrue(got["blocked"])
        got = _block(_folded(session=OTHER_SESSION), gate_candidate=None)
        self.assertEqual(_pairs(got), [(None, "foreign_binding"),
                                       (None, "candidate_unavailable")])

    def test_non_gating_findings_never_block(self):
        rows = [
            ("closed", _finding(state="closed")),
            ("withdrawn", _finding(state="withdrawn")),
            ("duplicate", _finding(state="duplicate")),
            ("non-blocking open", _finding(blocking=False)),
        ]
        for label, record in rows:
            with self.subTest(label):
                got = _block(_folded(findings={FID: record}))
                self.assertEqual(got, {"blocked": False, "reasons": [], "head": HEAD})

    def test_unknown_state_and_non_bool_blocking_are_blocking(self):
        rows = [
            ("unknown state", _finding(state="resolved"), "unresolved"),
            ("unknown state non-blocking", _finding(state="resolved", blocking=False),
             "unresolved"),
            ("None state", _finding(state=None), "unresolved"),
            ("blocking None", _finding(blocking=None), "unresolved"),
            ("blocking string", _finding(blocking="no"), "unresolved"),
            ("blocking zero", _finding(blocking=0), "unresolved"),
        ]
        for label, record, code in rows:
            with self.subTest(label):
                got = _block(_folded(findings={FID: record}))
                self.assertEqual(_pairs(got), [(FID, code)])

    def test_findings_reported_in_folded_order(self):
        folded = _folded(findings={FID3: _finding(candidate=OLD),
                                   FID: _finding(),
                                   FID2: _finding(session=OTHER_SESSION)})
        got = _block(folded)
        self.assertEqual(_pairs(got), [(FID3, "stale_candidate"), (FID, "unresolved"),
                                       (FID2, "foreign_binding")])


class HeadChangedTests(unittest.TestCase):
    """A7: head_changed."""

    def test_differing_heads(self):
        got = gate.blocking_decision(_meta(head=OTHER_HEAD), _folded(findings={}),
                                     SESSION, PHASE, GATE)
        self.assertEqual(got, {"blocked": True,
                               "reasons": [{"id": None, "code": "head_changed"}],
                               "head": HEAD})

    def test_equal_heads_and_both_none(self):
        for head in (HEAD, None):
            with self.subTest(head):
                got = gate.blocking_decision(_meta(head=head), _folded(findings={}, head=head),
                                             SESSION, PHASE, GATE)
                self.assertFalse(got["blocked"])

    def test_one_side_none_is_a_change(self):
        for meta_head, folded_head in ((None, HEAD), (HEAD, None)):
            with self.subTest((meta_head, folded_head)):
                got = gate.blocking_decision(_meta(head=meta_head),
                                             _folded(findings={}, head=folded_head),
                                             SESSION, PHASE, GATE)
                self.assertEqual(_pairs(got), [(None, "head_changed")])

    def test_earlier_folded_state_with_fresh_meta_proves_movement(self):
        earlier = _folded(findings={}, head=HEAD)
        fresh = _meta(head=OTHER_HEAD)
        got = gate.blocking_decision(fresh, earlier, SESSION, PHASE, GATE)
        self.assertEqual(_pairs(got), [(None, "head_changed")])

    def test_accumulates_with_finding_reasons(self):
        got = gate.blocking_decision(_meta(head=OTHER_HEAD), _folded(), SESSION, PHASE, GATE)
        self.assertEqual(_pairs(got), [(None, "head_changed"), (FID, "unresolved")])


class ProposalValidityTests(unittest.TestCase):
    """A3: proposal_invalid scope and earlier-candidate cited findings."""

    def _validate(self, proposal, folded=None, **kw):
        kw.setdefault("gate_candidate", GATE)
        kw.setdefault("session_uuid", SESSION)
        kw.setdefault("phase", PHASE)
        kw.setdefault("proposal_id", PID)
        return gate.validate_proposal(proposal, _folded() if folded is None else folded, **kw)

    def _invalid(self, finding_id):
        return [{"id": finding_id, "code": "proposal_invalid"}]

    def test_valid_proposal_is_empty(self):
        self.assertEqual(self._validate(_proposal()), [])

    def test_cited_id_defects_are_keyed_by_the_finding(self):
        rows = [
            ("unknown", _folded(findings={}), FID),
            ("withdrawn", _folded(findings={FID: _finding(state="withdrawn")}), FID),
            ("duplicate", _folded(findings={FID: _finding(state="duplicate")}), FID),
            ("unknown state", _folded(findings={FID: _finding(state="resolved")}), FID),
            ("other session", _folded(findings={FID: _finding(session=OTHER_SESSION)}), FID),
            ("other phase", _folded(findings={FID: _finding(phase=OTHER_PHASE)}), FID),
            ("non-dict record", _folded(findings={FID: "x"}), FID),
        ]
        for label, folded, expected_id in rows:
            with self.subTest(label):
                self.assertEqual(self._validate(_proposal(), folded), self._invalid(expected_id))

    def test_changed_evidence_candidate_must_equal_the_gate_candidate(self):
        rows = [
            ("different digest", OLD),
            ("different kind", KIND_MISMATCH),
            ("None", None),
            ("garbage", {"kind": "x"}),
            ("string", "x"),
        ]
        for label, candidate in rows:
            with self.subTest(label):
                self.assertEqual(self._validate(_proposal(candidate=candidate)),
                                 self._invalid(PID))

    def test_one_bad_id_invalidates_the_whole_proposal_for_every_id(self):
        folded = _folded(findings={FID: _finding(), FID2: _finding(state="withdrawn")})
        proposal = _proposal(ids=(FID, FID2))
        self.assertEqual(self._validate(proposal, folded), self._invalid(FID2))
        scene = _scene(findings={FID: _finding(), FID2: _finding(state="withdrawn")},
                       proposals={PID: proposal})
        got = _close(scene)
        self.assertFalse(got["close"])
        self.assertEqual(got["reasons"], ["proposal_invalid", "no_resolution_basis"])

    def test_cited_findings_on_an_earlier_candidate_are_valid(self):
        for state in ("open", "closed"):
            with self.subTest(state):
                folded = _folded(findings={FID: _finding(state=state, candidate=OLD)})
                self.assertEqual(self._validate(_proposal(), folded), [])
        folded = _folded(findings={FID: _finding(candidate=KIND_MISMATCH)})
        self.assertEqual(self._validate(_proposal(), folded), [])

    def test_the_proposal_records_own_candidate_field_is_ignored(self):
        for own in (None, "garbage", OLD, GATE):
            with self.subTest(repr(own)):
                proposal = _proposal(candidate=GATE)
                proposal["candidate"] = own
                self.assertEqual(self._validate(proposal), [])

    def test_proposal_level_defects_are_keyed_by_the_proposal(self):
        rows = [
            ("non-dict proposal", "x"),
            ("None proposal", None),
            ("missing finding_ids", {k: v for k, v in _proposal().items() if k != "finding_ids"}),
            ("empty finding_ids", _proposal(ids=())),
            ("non-list finding_ids", dict(_proposal(), finding_ids=FID)),
            ("non-str finding id", dict(_proposal(), finding_ids=[FID, 7])),
            ("missing changed_evidence",
             {k: v for k, v in _proposal().items() if k != "changed_evidence"}),
            ("non-dict changed_evidence", dict(_proposal(), changed_evidence=[GATE])),
            ("present non-list requires", _proposal(requires=TOKEN)),
            ("non-str requires element", _proposal(requires=[TOKEN, 3])),
        ]
        for label, proposal in rows:
            with self.subTest(label):
                self.assertEqual(self._validate(proposal), self._invalid(PID))

    def test_unusable_folded_state(self):
        for label, folded in (("None", None), ("list", []), ("no findings", {"session_uuid": SESSION}),
                              ("findings list", {"findings": [], "session_uuid": SESSION})):
            with self.subTest(label):
                self.assertEqual(gate.validate_proposal(_proposal(), folded, gate_candidate=GATE,
                                                        proposal_id=PID), self._invalid(PID))

    def test_proposal_id_defaults_to_none(self):
        self.assertEqual(gate.validate_proposal("x", _folded()), self._invalid(None))

    def test_absent_or_none_requires_is_empty_and_valid(self):
        self.assertEqual(self._validate(_proposal()), [])
        self.assertEqual(self._validate(_proposal(requires=None)), [])
        self.assertEqual(self._validate(_proposal(requires=[])), [])
        self.assertEqual(self._validate(_proposal(requires=[TOKEN])), [])

    def test_missing_requires_proposal_with_recommendation_closes(self):
        got = _close(_scene())
        self.assertEqual(got, {"close": True, "reasons": [], "escalation_needed": None})

    def test_unresolvable_session_is_proposal_level(self):
        for folded_session in (None, ""):
            with self.subTest(folded_session):
                folded = _folded(session=folded_session)
                got = gate.validate_proposal(_proposal(), folded, gate_candidate=GATE,
                                             phase=PHASE, proposal_id=PID)
                self.assertEqual(got, self._invalid(PID))
        got = gate.validate_proposal(_proposal(), _folded(session=None), gate_candidate=GATE,
                                     session_uuid="", phase=PHASE, proposal_id=PID)
        self.assertEqual(got, self._invalid(PID))

    def test_session_argument_wins_over_folded_session(self):
        folded = _folded(session=OTHER_SESSION)
        self.assertEqual(self._validate(_proposal(), folded), [])

    def test_repeated_id_is_not_a_defect_and_defects_are_distinct(self):
        self.assertEqual(self._validate(_proposal(ids=(FID, FID))), [])
        folded = _folded(findings={})
        self.assertEqual(self._validate(_proposal(ids=(FID, FID)), folded), self._invalid(FID))

    def test_phase_defaults_to_the_first_existing_cited_finding(self):
        folded = _folded(findings={FID: _finding(), FID2: _finding(phase=OTHER_PHASE)})
        got = gate.validate_proposal(_proposal(ids=(FID, FID2)), folded, gate_candidate=GATE,
                                     proposal_id=PID)
        self.assertEqual(got, self._invalid(FID2))

    def test_blocking_decision_reports_an_invalid_proposal_citing_an_open_blocker(self):
        folded = _folded(proposals={PID: _proposal(candidate=OLD)})
        got = _block(folded)
        self.assertEqual(_pairs(got), [(FID, "unresolved"), (PID, "proposal_invalid")])


class ValidateProposalSignatureTests(unittest.TestCase):
    def test_two_argument_call_is_an_id_and_structure_check_only(self):
        stale = _proposal(candidate=OLD)
        self.assertEqual(gate.validate_proposal(stale, _folded()), [])
        self.assertEqual(
            gate.validate_proposal(stale, _folded(), gate_candidate=GATE, proposal_id=PID),
            [{"id": PID, "code": "proposal_invalid"}])

    def test_two_argument_call_still_checks_cited_ids(self):
        self.assertEqual(gate.validate_proposal(_proposal(), _folded(findings={})),
                         [{"id": FID, "code": "proposal_invalid"}])

    def test_keyword_only(self):
        with self.assertRaises(TypeError):
            gate.validate_proposal(_proposal(), _folded(), GATE)

    def _spy(self):
        return mock.patch.object(gate, "validate_proposal", wraps=gate.validate_proposal)

    def test_blocking_decision_passes_all_four_keywords(self):
        folded = _scene(proposals={PID: _proposal()})
        with self._spy() as spy:
            _block(folded)
        self.assertTrue(spy.called)
        for call in spy.call_args_list:
            self.assertEqual(call.kwargs, {"gate_candidate": GATE, "session_uuid": SESSION,
                                           "phase": PHASE, "proposal_id": PID})

    def test_closure_decision_passes_all_four_keywords(self):
        folded = _scene()
        with self._spy() as spy:
            _close(folded)
        self.assertTrue(spy.called)
        for call in spy.call_args_list:
            self.assertEqual(call.kwargs, {"gate_candidate": GATE, "session_uuid": SESSION,
                                           "phase": PHASE, "proposal_id": PID})


class CapabilityVetoTests(unittest.TestCase):
    """A4: uncovered requires forces capability_not_granted."""

    def _scene_requires(self, requires=(TOKEN,), **over):
        return _scene(proposals={PID: _proposal(requires=list(requires))}, **over)

    def test_uncovered_requires_vetoes_with_escalation(self):
        rows = [
            ("missing coverage entry", {}),
            ("False coverage", {PID: False}),
            ("non-bool coverage", {PID: "yes"}),
            ("one coverage", {PID: 1}),
            ("other proposal covered", {PID2: True}),
        ]
        for label, coverage in rows:
            with self.subTest(label):
                got = _close(self._scene_requires(), coverage=coverage)
                self.assertEqual(got, {"close": False, "reasons": ["capability_not_granted"],
                                       "escalation_needed": TOKEN})

    def test_non_dict_coverage_is_not_covered(self):
        for coverage in (None, [PID], "x", True):
            with self.subTest(repr(coverage)):
                got = gate.closure_decision(self._scene_requires(), FID, GATE, [], coverage, {})
                self.assertEqual(got["reasons"], ["capability_not_granted"])

    def test_escalation_is_the_first_required_token(self):
        got = _close(self._scene_requires(requires=(TOKEN2, TOKEN)))
        self.assertEqual(got["escalation_needed"], TOKEN2)

    def test_covered_proposal_with_recommendation_closes(self):
        got = _close(self._scene_requires(), coverage={PID: True})
        self.assertEqual(got, {"close": True, "reasons": [], "escalation_needed": None})

    def test_uncovered_proposal_beats_a_consumed_supervisor_close(self):
        folded = self._scene_requires(decisions={"AD-0001": _decision()})
        got = _close(folded, consumed=CONSUMED)
        self.assertEqual(got, {"close": False, "reasons": ["capability_not_granted"],
                               "escalation_needed": TOKEN})
        policy = self._scene_requires(decisions={"AD-0001": _decision(principal="policy_principal")})
        self.assertEqual(_close(policy, consumed=CONSUMED)["reasons"], ["capability_not_granted"])

    def test_a_different_covered_proposal_does_not_lift_the_veto(self):
        folded = _scene(proposals={PID: _proposal(requires=[TOKEN]),
                                   PID2: _proposal(requires=[TOKEN2])})
        got = _close(folded, coverage={PID2: True})
        self.assertEqual(got["reasons"], ["capability_not_granted"])
        self.assertEqual(got["escalation_needed"], TOKEN)
        got = _close(folded, coverage={PID: True, PID2: True})
        self.assertTrue(got["close"])

    def test_first_vetoing_proposal_in_folded_order_names_the_token(self):
        folded = _scene(proposals={PID2: _proposal(requires=[TOKEN2]),
                                   PID: _proposal(requires=[TOKEN])})
        self.assertEqual(_close(folded)["escalation_needed"], TOKEN2)

    def test_stale_proposal_exerts_no_veto(self):
        stale = _scene(proposals={PID: _proposal(candidate=OLD, requires=[TOKEN])})
        got = _close(stale)
        self.assertEqual(got["reasons"], ["proposal_invalid", "no_resolution_basis"])
        self.assertIsNone(got["escalation_needed"])
        adjudicated = _scene(proposals={PID: _proposal(candidate=OLD, requires=[TOKEN])},
                             decisions={"AD-0001": _decision()})
        self.assertTrue(_close(adjudicated, consumed=CONSUMED)["close"])

    def test_candidate_moving_removes_the_veto_without_deadlock(self):
        folded = self._scene_requires()
        self.assertEqual(_close(folded)["reasons"], ["capability_not_granted"])
        moved = _close(folded, gate_candidate=NEXT)
        self.assertEqual(moved["reasons"], ["stale_candidate", "proposal_invalid",
                                            "no_resolution_basis"])

    def test_invalid_proposal_exerts_no_veto(self):
        folded = _scene(proposals={PID: _proposal(ids=(FID, FID2), requires=[TOKEN])})
        got = _close(folded)
        self.assertEqual(got["reasons"], ["proposal_invalid", "no_resolution_basis"])

    def test_empty_missing_or_none_requires_has_no_veto(self):
        for requires in ({"requires": []}, {}, {"requires": None}):
            with self.subTest(requires):
                folded = _scene(proposals={PID: _proposal(**requires)})
                self.assertTrue(_close(folded, coverage={PID: False})["close"])

    def test_proposal_citing_another_finding_exerts_no_veto(self):
        folded = _scene(findings={FID: _finding(), FID2: _finding()},
                        proposals={PID: _proposal(), PID2: _proposal(ids=(FID2,), requires=[TOKEN])})
        self.assertTrue(_close(folded)["close"])


class ClosureBasisTests(unittest.TestCase):
    """A5: closure basis rules."""

    def test_proposal_with_current_round_recommendation_closes(self):
        self.assertEqual(_close(_scene()), {"close": True, "reasons": [], "escalation_needed": None})

    def test_proposal_with_consumed_decision_closes(self):
        folded = _scene(rounds=[], recommendations={}, decisions={"AD-0001": _decision()})
        self.assertTrue(_close(folded, consumed=CONSUMED)["close"])

    def test_consumed_authorized_close_alone_closes(self):
        for principal in gate.DECISION_PRINCIPALS:
            with self.subTest(principal):
                folded = _folded(decisions={"AD-0001": _decision(principal=principal)})
                self.assertEqual(_close(folded, consumed=CONSUMED),
                                 {"close": True, "reasons": [], "escalation_needed": None})

    def test_unauthorized_principal_is_not_a_basis(self):
        for principal in ("reviewer", "builder", "", None, "supervisor"):
            with self.subTest(principal):
                folded = _folded(decisions={"AD-0001": _decision(principal=principal)})
                got = _close(folded, consumed=CONSUMED)
                self.assertEqual(got["reasons"], ["no_resolution_basis"])

    def test_decision_that_is_not_consumed_is_inert(self):
        rows = [
            ("request missing from consumed", {}),
            ("different answer sha", {REQ: C}),
            ("other request consumed", {REQ2: D}),
            ("non-str consumed value", {REQ: 5}),
            ("None consumed value", {REQ: None}),
            ("non-dict consumed", ["RQ-0001"]),
        ]
        for label, consumed in rows:
            with self.subTest(label):
                folded = _folded(decisions={"AD-0001": _decision()})
                got = gate.closure_decision(folded, FID, GATE, [], {}, consumed)
                self.assertEqual(got["reasons"], ["no_resolution_basis"])

    def test_decision_with_malformed_ids_is_inert(self):
        rows = [
            _decision(request_id=None, answer=D),
            _decision(request_id="", answer=D),
            _decision(answer=None),
            _decision(answer=""),
            _decision(request_id=7),
            "decision",
        ]
        for record in rows:
            with self.subTest(repr(record)[:50]):
                got = gate.closure_decision(_folded(decisions={"AD-0001": record}), FID, GATE, [],
                                            {}, {REQ: D, "": "", 7: 7, None: None})
                self.assertEqual(got["reasons"], ["no_resolution_basis"])

    def test_decision_adjudicating_another_finding_is_not_a_basis(self):
        folded = _folded(findings={FID: _finding(), FID2: _finding()},
                         decisions={"AD-0001": _decision(adjudicates=((FID2, "close"),))})
        self.assertEqual(_close(folded, consumed=CONSUMED)["reasons"], ["no_resolution_basis"])
        self.assertTrue(_close(folded, finding_id=FID2, consumed=CONSUMED)["close"])

    def test_decision_candidate_need_not_equal_the_gate_candidate(self):
        for decision_candidate in (OLD, KIND_MISMATCH):
            with self.subTest(decision_candidate):
                folded = _folded(decisions={"AD-0001": _decision(candidate=decision_candidate)})
                self.assertTrue(_close(folded, consumed=CONSUMED)["close"])

    def test_proposal_alone_recommendation_alone_and_neither_do_not_close(self):
        proposal_only = _folded(proposals={PID: _proposal()})
        recommendation_only = _folded(rounds=[_round()], recommendations={FID: [_rec()]})
        neither = _folded()
        for label, folded in (("proposal", proposal_only), ("recommendation", recommendation_only),
                              ("neither", neither)):
            with self.subTest(label):
                got = _close(folded)
                self.assertEqual(got, {"close": False, "reasons": ["no_resolution_basis"],
                                       "escalation_needed": None})

    def test_finding_tiers(self):
        rows = [
            ("unknown id", _scene(), "AF-9999", GATE, ["finding_unknown"]),
            ("non-str id", _scene(), ["AF-0001"], GATE, ["finding_unknown"]),
            ("None id", _scene(), None, GATE, ["finding_unknown"]),
            ("non-dict record", _folded(findings={FID: "x"}), FID, GATE, ["finding_unknown"]),
            ("findings not a dict", dict(_scene(), findings=[]), FID, GATE, ["finding_unknown"]),
            ("folded None", None, FID, GATE, ["finding_unknown"]),
            ("closed", _scene(findings={FID: _finding(state="closed")}), FID, GATE,
             ["finding_not_open"]),
            ("withdrawn", _scene(findings={FID: _finding(state="withdrawn")}), FID, GATE,
             ["finding_not_open"]),
            ("duplicate", _scene(findings={FID: _finding(state="duplicate")}), FID, GATE,
             ["finding_not_open"]),
            ("other session", _scene(findings={FID: _finding(session=OTHER_SESSION)}), FID, GATE,
             ["foreign_binding"]),
            ("invalid gate", _scene(), FID, None, ["candidate_unavailable"]),
            ("malformed gate", _scene(), FID, {"kind": "x"}, ["candidate_unavailable"]),
        ]
        for label, folded, finding_id, gate_candidate, expected in rows:
            with self.subTest(label):
                got = gate.closure_decision(folded, finding_id, gate_candidate, [], {}, CONSUMED)
                self.assertEqual(got, {"close": False, "reasons": expected,
                                       "escalation_needed": None})

    def test_tier_order(self):
        both = _scene(findings={FID: _finding(state="closed", session=OTHER_SESSION)})
        self.assertEqual(_close(both, gate_candidate=None)["reasons"], ["finding_not_open"])
        foreign = _scene(findings={FID: _finding(session=OTHER_SESSION)})
        self.assertEqual(_close(foreign, gate_candidate=None)["reasons"], ["candidate_unavailable"])

    def test_unprovable_session_never_closes(self):
        for session in (None, "", 7):
            for label, extra in (
                ("consumed close", dict(rounds=[], recommendations={}, proposals={},
                                        decisions={"AD-0001": _decision()})),
                ("proposal and recommendation", {}),
            ):
                with self.subTest((session, label)):
                    folded = _scene(session=session, findings={FID: _finding(session=session)},
                                    **extra)
                    got = _close(folded, consumed=CONSUMED)
                    self.assertEqual(got, {"close": False, "reasons": ["foreign_binding"],
                                           "escalation_needed": None})
        folded = _scene()
        del folded["session_uuid"]
        folded["findings"][FID].pop("session_uuid")
        self.assertEqual(_close(folded)["reasons"], ["foreign_binding"])

    def test_no_basis_explanations(self):
        stale_finding = _folded(findings={FID: _finding(candidate=OLD)})
        self.assertEqual(_close(stale_finding)["reasons"], ["stale_candidate", "no_resolution_basis"])
        garbage = _folded(findings={FID: _finding(candidate={"kind": "x"})})
        self.assertEqual(_close(garbage)["reasons"], ["stale_candidate", "no_resolution_basis"])
        stale_proposal = _folded(proposals={PID: _proposal(candidate=OLD)})
        self.assertEqual(_close(stale_proposal)["reasons"],
                         ["proposal_invalid", "no_resolution_basis"])
        both = _folded(findings={FID: _finding(candidate=OLD)},
                       proposals={PID: _proposal(candidate=OLD)})
        self.assertEqual(_close(both)["reasons"],
                         ["stale_candidate", "proposal_invalid", "no_resolution_basis"])

    def test_sibling_invalid_proposal_adds_no_reason_on_close(self):
        folded = _scene(proposals={PID: _proposal(), PID2: _proposal(candidate=OLD)})
        self.assertEqual(_close(folded), {"close": True, "reasons": [], "escalation_needed": None})

    def test_a_valid_sibling_still_closes_beside_an_invalid_proposal(self):
        folded = _scene(proposals={PID2: _proposal(candidate=OLD), PID: _proposal()})
        self.assertTrue(_close(folded)["close"])

    def test_result_is_fresh_and_close_implies_empty_reasons(self):
        folded = _scene()
        first = _close(folded)
        first["reasons"].append("x")
        second = _close(folded)
        self.assertEqual(second["reasons"], [])
        for result in (_close(_scene()), _close(_folded())):
            self.assertEqual(result["close"], result["reasons"] == [] and
                             result["escalation_needed"] is None)

    def test_status_kind_gate_closes_on_a_matching_proposal(self):
        folded = _scene(finding_candidate=GATE, proposal_candidate=KIND_MISMATCH,
                        round_candidate=KIND_MISMATCH)
        self.assertTrue(_close(folded, gate_candidate=_status(A))["close"])


class UpholdOrderingTests(unittest.TestCase):
    def _folded(self, *decisions):
        return _folded(decisions={"AD-%04d" % i: d for i, d in enumerate(decisions, 1)})

    def test_orderings(self):
        close = _decision(adjudicates=((FID, "close"),), request_id=REQ)
        uphold = _decision(adjudicates=((FID, "uphold"),), request_id=REQ2)
        consumed = {REQ: D, REQ2: D}
        rows = [
            ("close only", [close], True),
            ("uphold only", [uphold], False),
            ("close then uphold", [close, uphold], False),
            ("uphold then close", [uphold, close], True),
        ]
        for label, decisions, expected in rows:
            with self.subTest(label):
                got = _close(self._folded(*decisions), consumed=consumed)
                self.assertEqual(got["close"], expected)
                if not expected:
                    self.assertEqual(got["reasons"], ["no_resolution_basis"])

    def test_unconsumed_later_uphold_does_not_supersede(self):
        close = _decision(request_id=REQ)
        uphold = _decision(adjudicates=((FID, "uphold"),), request_id=REQ2)
        self.assertTrue(_close(self._folded(close, uphold), consumed=CONSUMED)["close"])

    def test_null_candidate_later_uphold_does_not_supersede(self):
        close = _decision(request_id=REQ)
        uphold = _decision(adjudicates=((FID, "uphold"),), request_id=REQ2, candidate=None)
        self.assertTrue(_close(self._folded(close, uphold),
                               consumed={REQ: D, REQ2: D})["close"])

    def test_last_adjudicates_entry_within_one_decision_wins(self):
        for entries, expected in (
            (((FID, "close"), (FID, "uphold")), False),
            (((FID, "uphold"), (FID, "close")), True),
        ):
            with self.subTest(entries):
                folded = self._folded(_decision(adjudicates=entries))
                self.assertEqual(_close(folded, consumed=CONSUMED)["close"], expected)

    def test_uphold_does_not_veto_proposal_and_recommendation(self):
        uphold = _decision(adjudicates=((FID, "uphold"),))
        folded = _scene(decisions={"AD-0001": uphold})
        self.assertTrue(_close(folded, consumed=CONSUMED)["close"])

    def test_unknown_outcome_is_ignored(self):
        folded = self._folded(_decision(adjudicates=((FID, "maybe"),)))
        self.assertEqual(_close(folded, consumed=CONSUMED)["reasons"], ["no_resolution_basis"])


class CurrentRoundTests(unittest.TestCase):
    def _closes(self, rounds, recommendations, **over):
        folded = _scene(rounds=rounds, recommendations=recommendations, **over)
        return _close(folded)["close"]

    def test_latest_round_of_the_phase_on_the_gate_candidate(self):
        self.assertTrue(self._closes([_round()], {FID: [_rec()]}))

    def test_older_round_recommendation_does_not_count(self):
        rounds = [_round(round_id=RID), _round(round_id=RID2)]
        self.assertFalse(self._closes(rounds, {FID: [_rec(round_id=RID)]}))
        self.assertTrue(self._closes(rounds, {FID: [_rec(round_id=RID), _rec(round_id=RID2)]}))

    def test_round_bound_to_another_candidate_does_not_count(self):
        for candidate in (OLD, KIND_MISMATCH, None, {"kind": "x"}):
            with self.subTest(repr(candidate)):
                self.assertFalse(self._closes([_round(candidate=candidate)], {FID: [_rec()]}))

    def test_latest_round_must_be_current_not_just_any_round(self):
        rounds = [_round(round_id=RID, candidate=GATE), _round(round_id=RID2, candidate=OLD)]
        self.assertFalse(self._closes(rounds, {FID: [_rec(round_id=RID)]}))
        self.assertFalse(self._closes(rounds, {FID: [_rec(round_id=RID2)]}))

    def test_integer_rounds_repeat_across_seats_but_round_id_keys(self):
        rounds = [_round(round_id=RID, seat="builder", number=1),
                  _round(round_id=RID2, seat="reviewer", number=1)]
        self.assertFalse(self._closes(rounds, {FID: [_rec(round_id=RID)]}))
        self.assertTrue(self._closes(rounds, {FID: [_rec(round_id=RID2)]}))
        self.assertFalse(self._closes([_round(round_id=RID, number=1)],
                                      {FID: [dict(_rec(), round=1, round_id="other")]}))

    def test_same_round_dissent_beside_closed_blocks(self):
        for dissent in ("still_open", "withdrawn", "duplicate"):
            with self.subTest(dissent):
                for order in (("closed", dissent), (dissent, "closed")):
                    recs = [_rec(recommend=r, rec_id="AR-%d" % i) for i, r in enumerate(order)]
                    self.assertFalse(self._closes([_round()], {FID: recs}))

    def test_non_closed_recommendations_alone_do_not_close(self):
        for recommend in ("still_open", "withdrawn", "duplicate", "other", None):
            with self.subTest(recommend):
                self.assertFalse(self._closes([_round()], {FID: [_rec(recommend=recommend)]}))

    def test_rounds_of_other_phases_are_ignored(self):
        rounds = [_round(round_id=RID), _round(round_id=RID2, phase=OTHER_PHASE, candidate=OLD)]
        self.assertTrue(self._closes(rounds, {FID: [_rec(round_id=RID)]}))
        only_other = [_round(phase=OTHER_PHASE)]
        self.assertFalse(self._closes(only_other, {FID: [_rec()]}))

    def test_recommendation_for_another_finding_does_not_count(self):
        self.assertFalse(self._closes([_round()], {FID2: [_rec()]}))

    def test_malformed_rounds_and_recommendations_fail_closed(self):
        rows = [
            ([], {FID: [_rec()]}),
            ("x", {FID: [_rec()]}),
            ([None, "x"], {FID: [_rec()]}),
            ([dict(_round(), round_id=None)], {FID: [_rec(round_id=None)]}),
            ([_round()], {FID: "closed"}),
            ([_round()], {FID: [None, "x"]}),
            ([_round()], None),
            ([_round()], []),
        ]
        for rounds, recommendations in rows:
            with self.subTest(repr((rounds, recommendations))[:60]):
                self.assertFalse(self._closes(rounds, recommendations))

    def test_valid_proposal_is_also_required(self):
        folded = _scene(proposals={})
        self.assertEqual(_close(folded)["reasons"], ["no_resolution_basis"])


class WitnessScreenTests(unittest.TestCase):
    def _folded(self, claim_class, path):
        if path == "proposal":
            return _scene(findings={FID: _finding(claim_class=claim_class)})
        return _folded(findings={FID: _finding(claim_class=claim_class)},
                       decisions={"AD-0001": _decision()})

    def test_claim_class_none_ignores_screens(self):
        for path in ("proposal", "adjudication"):
            for screens in ([], [_screen(False)], None, "x"):
                with self.subTest((path, repr(screens))):
                    folded = self._folded(None, path)
                    got = gate.closure_decision(folded, FID, GATE, screens, {}, CONSUMED)
                    self.assertTrue(got["close"])

    def test_claim_class_requires_a_non_empty_all_approvable_set(self):
        rows = [
            ("missing", None),
            ("empty list", []),
            ("empty tuple", ()),
            ("empty dict", {}),
            ("non-approvable", [_screen(False)]),
            ("mix", [_screen(True), _screen(False)]),
            ("non-dict", ["approvable"]),
            ("approvable not True", [{"approvable": 1}]),
            ("approvable string", [{"approvable": "True"}]),
            ("missing approvable", [{}]),
            ("string", "approvable"),
            ("dict with bad value", {"c1": _screen(True), "c2": None}),
        ]
        for path in ("proposal", "adjudication"):
            for label, screens in rows:
                with self.subTest((path, label)):
                    folded = self._folded("source_of_truth", path)
                    got = gate.closure_decision(folded, FID, GATE, screens, {}, CONSUMED)
                    self.assertEqual(got, {"close": False, "reasons": ["witness_not_approvable"],
                                           "escalation_needed": None})

    def test_list_tuple_and_dict_shapes_with_all_approvable_close(self):
        shapes = [[_screen()], (_screen(), _screen()), {"c1": _screen(), "c2": _screen()}]
        for path in ("proposal", "adjudication"):
            for screens in shapes:
                with self.subTest((path, type(screens).__name__)):
                    folded = self._folded("only", path)
                    got = gate.closure_decision(folded, FID, GATE, screens, {}, CONSUMED)
                    self.assertTrue(got["close"])

    def test_every_claim_class_is_screened(self):
        for claim_class in ("source_of_truth", "only", "never", "fail_closed"):
            with self.subTest(claim_class):
                folded = self._folded(claim_class, "proposal")
                self.assertFalse(_close(folded)["close"])

    def test_witness_failure_without_a_basis_reports_no_basis(self):
        folded = _folded(findings={FID: _finding(claim_class="only")})
        self.assertEqual(_close(folded)["reasons"], ["no_resolution_basis"])

    def test_screens_are_not_mutated(self):
        screens = [_screen()]
        before = copy.deepcopy(screens)
        gate.closure_decision(self._folded("only", "proposal"), FID, GATE, screens, {}, {})
        self.assertEqual(screens, before)


class NullCandidateDecisionTests(unittest.TestCase):
    """A6/N6: a null-candidate decision is never a closure basis."""

    def test_consumed_null_candidate_decision_closes_nothing(self):
        rows = [
            ("empty adjudicates", _decision(adjudicates=(), candidate=None)),
            ("forged close", _decision(candidate=None)),
            ("forged close policy", _decision(candidate=None, principal="policy_principal")),
            ("malformed candidate", _decision(candidate={"kind": "x"})),
            ("status_absent shaped", dict(_decision(candidate=None), null_candidate_basis="status_absent")),
            ("candidate key missing", {k: v for k, v in _decision().items() if k != "candidate"}),
            ("string candidate", _decision(candidate="x")),
        ]
        for label, record in rows:
            with self.subTest(label):
                folded = _folded(decisions={"AD-0001": record})
                got = _close(folded, consumed=CONSUMED)
                self.assertEqual(got, {"close": False, "reasons": ["no_resolution_basis"],
                                       "escalation_needed": None})
                blocked = gate.blocking_decision(_meta(), folded, SESSION, PHASE, GATE)
                self.assertTrue(blocked["blocked"])
                self.assertEqual(_pairs(blocked), [(FID, "unresolved")])

    def test_null_decision_beside_a_proposal_does_not_close(self):
        folded = _scene(rounds=[], recommendations={},
                        decisions={"AD-0001": _decision(candidate=None)})
        self.assertEqual(_close(folded, consumed=CONSUMED)["reasons"], ["no_resolution_basis"])

    def test_null_decision_does_not_affect_a_proposal_and_recommendation_close(self):
        folded = _scene(decisions={"AD-0001": _decision(candidate=None)})
        self.assertTrue(_close(folded, consumed=CONSUMED)["close"])

    def test_null_candidate_decision_with_both_kinds_of_valid_candidates_alongside(self):
        for good in (GATE, _status(A)):
            with self.subTest(good):
                folded = _folded(decisions={
                    "AD-0001": _decision(candidate=None, request_id=REQ2),
                    "AD-0002": _decision(candidate=good)})
                self.assertTrue(_close(folded, consumed={REQ: D, REQ2: D})["close"])


class UnconsumedDecisionTests(unittest.TestCase):
    """N5: an unconsumed decision closes nothing."""

    def test_unconsumed_decision_closes_nothing(self):
        for label, consumed in (("absent", {}), ("different sha", {REQ: C})):
            for with_proposal in (False, True):
                with self.subTest((label, with_proposal)):
                    folded = _folded(decisions={"AD-0001": _decision()})
                    if with_proposal:
                        folded["proposals"] = {PID: _proposal()}
                    got = _close(folded, consumed=consumed)
                    self.assertEqual(got, {"close": False, "reasons": ["no_resolution_basis"],
                                           "escalation_needed": None})
                    blocked = gate.blocking_decision(_meta(), folded, SESSION, PHASE, GATE)
                    self.assertTrue(blocked["blocked"])

    def test_consumed_decision_is_the_only_difference(self):
        folded = _folded(decisions={"AD-0001": _decision()})
        self.assertFalse(_close(folded, consumed={})["close"])
        self.assertTrue(_close(folded, consumed=CONSUMED)["close"])


class ReviewerOnlyClosureTests(unittest.TestCase):
    """N4: reviewer-only, ledger-shaped or closed_source_findings-shaped input."""

    def test_reviewer_recommendation_alone_is_not_a_basis(self):
        folded = _folded(rounds=[_round()], recommendations={FID: [_rec()]})
        self.assertEqual(_close(folded)["reasons"], ["no_resolution_basis"])

    def test_extra_ledger_shaped_keys_are_not_a_basis(self):
        extras = [
            {"ledger": [{"finding_id": FID, "closed": True}]},
            {"closed_source_findings": [FID]},
            {"closed_source_findings": {FID: True}},
            {"resolved": [FID]},
            {"closed_ids": [FID]},
            {"snapshot_rows": [{"reported": {"closed_ids": [FID]}}]},
        ]
        for extra in extras:
            with self.subTest(list(extra)[0]):
                folded = dict(_folded(), **extra)
                self.assertEqual(_close(folded)["reasons"], ["no_resolution_basis"])
                self.assertTrue(gate.blocking_decision(_meta(), folded, SESSION, PHASE, GATE)["blocked"])

    def test_resolved_style_hint_on_a_finding_is_not_a_basis(self):
        for hint in ({"resolved": True}, {"closed": True}, {"resolution": "closed"},
                     {"closed_by": "reviewer"}):
            with self.subTest(hint):
                folded = _folded(findings={FID: dict(_finding(), **hint)})
                self.assertEqual(_close(folded)["reasons"], ["no_resolution_basis"])
                self.assertEqual(_pairs(gate.blocking_decision(_meta(), folded, SESSION,
                                                               PHASE, GATE)),
                                 [(FID, "unresolved")])

    def test_reviewer_principal_decision_is_not_a_basis(self):
        folded = _folded(decisions={"AD-0001": _decision(principal="reviewer")})
        self.assertFalse(_close(folded, consumed=CONSUMED)["close"])


class ProposalClaimDoesNotUnblockTests(unittest.TestCase):
    """N3: a proposal claiming all blockers addressed never unblocks."""

    def test_claiming_proposal_still_blocked(self):
        findings = {FID: _finding(), FID2: _finding(candidate=OLD),
                    FID3: _finding(session=OTHER_SESSION)}
        claim = _proposal(ids=(FID, FID2, FID3), author_claim="all blockers addressed",
                          addressed=[FID, FID2, FID3])
        got = _block(_folded(findings=findings, proposals={PID: claim}))
        self.assertTrue(got["blocked"])
        self.assertEqual(_pairs(got), [(FID, "unresolved"), (FID2, "stale_candidate"),
                                       (FID3, "foreign_binding"), (FID3, "proposal_invalid")])

    def test_valid_claiming_proposal_still_blocked(self):
        got = _block(_folded(proposals={PID: _proposal(author_claim="all blockers addressed")}))
        self.assertEqual(_pairs(got), [(FID, "unresolved")])

    def test_invalid_proposal_citing_no_open_blocking_finding_adds_no_reason(self):
        rows = [
            ("closed", _finding(state="closed")),
            ("non-blocking", _finding(blocking=False)),
        ]
        for label, record in rows:
            with self.subTest(label):
                folded = _folded(findings={FID: record}, proposals={PID: _proposal(candidate=OLD)})
                self.assertEqual(_block(folded), {"blocked": False, "reasons": [], "head": HEAD})

    def test_withdrawn_finding_cited_by_an_old_proposal_never_blocks(self):
        for state in ("withdrawn", "duplicate"):
            with self.subTest(state):
                folded = _folded(findings={FID: _finding(state=state)},
                                 proposals={PID: _proposal()})
                self.assertFalse(_block(folded)["blocked"])
        folded = _folded(findings={}, proposals={PID: _proposal()})
        self.assertFalse(_block(folded)["blocked"])

    def test_malformed_proposal_without_citing_a_blocker_is_ignored(self):
        for proposal in ("x", None, {"finding_ids": "AF-0001"}, {"finding_ids": [[FID]]}):
            with self.subTest(repr(proposal)):
                folded = _folded(findings={FID: _finding(state="closed")},
                                 proposals={PID: proposal})
                self.assertFalse(_block(folded)["blocked"])

    def test_proposals_are_not_evaluated_with_an_invalid_gate_candidate(self):
        folded = _folded(proposals={PID: _proposal()})
        self.assertEqual(_pairs(_block(folded, gate_candidate=None)),
                         [(None, "candidate_unavailable")])


class StaleProposalClosureTests(unittest.TestCase):
    """N2: a finding bound to an earlier candidate closes through a current proposal."""

    def test_finding_on_c1_proposal_on_gate_with_recommendation_closes(self):
        folded = _scene(finding_candidate=OLD)
        self.assertEqual(_pairs(_block(folded)), [(FID, "stale_candidate")])
        self.assertEqual(_close(folded), {"close": True, "reasons": [], "escalation_needed": None})

    def test_proposal_on_another_candidate_does_not_close(self):
        folded = _scene(finding_candidate=OLD, proposal_candidate=OLD)
        got = _close(folded)
        self.assertEqual(got["close"], False)
        self.assertEqual(got["reasons"], ["stale_candidate", "proposal_invalid", "no_resolution_basis"])
        blocked = _block(folded)
        self.assertEqual(_pairs(blocked), [(FID, "stale_candidate"), (PID, "proposal_invalid")])

    def test_sibling_proposals_across_rounds(self):
        c1, c2, c3 = OLD, _owned(C, C), _owned(D, D)
        folded = _folded(
            findings={FID: _finding(candidate=c1)},
            proposals={PID: _proposal(candidate=c2), PID2: _proposal(candidate=c3)},
            rounds=[_round(round_id=RID, candidate=c2), _round(round_id=RID2, candidate=c3)],
            recommendations={FID: [_rec(round_id=RID), _rec(round_id=RID2, rec_id="AR-0002")]},
        )
        got = _close(folded, gate_candidate=c3)
        self.assertEqual(got, {"close": True, "reasons": [], "escalation_needed": None})
        stale = _close(folded, gate_candidate=c2)
        self.assertFalse(stale["close"])
        self.assertEqual(stale["reasons"], ["stale_candidate", "proposal_invalid",
                                            "no_resolution_basis"])
        only_stale_round = _folded(
            findings={FID: _finding(candidate=c1)},
            proposals={PID: _proposal(candidate=c2)},
            rounds=[_round(round_id=RID, candidate=c2)],
            recommendations={FID: [_rec(round_id=RID)]},
        )
        self.assertFalse(_close(only_stale_round, gate_candidate=c3)["close"])
        self.assertEqual(_close(only_stale_round, gate_candidate=c3)["reasons"],
                         ["stale_candidate", "proposal_invalid", "no_resolution_basis"])

    def test_stale_round_recommendation_does_not_close_a_current_proposal(self):
        folded = _scene(finding_candidate=OLD, round_candidate=OLD)
        self.assertEqual(_close(folded)["reasons"], ["stale_candidate", "no_resolution_basis"])


class GateMatrixTests(unittest.TestCase):
    """N1: the gate matrix over proposal and recommendation presence."""

    def _matrix(self):
        def build(findings, with_proposal, with_rec, proposal_ids=(FID,), session=SESSION,
                  head=HEAD):
            return _folded(
                findings=findings,
                proposals={PID: _proposal(ids=proposal_ids)} if with_proposal else {},
                rounds=[_round()] if with_rec else [],
                recommendations={FID: [_rec()]} if with_rec else {},
                session=session, head=head)

        # (label, finding record, session arg, meta, gate candidate,
        #  reasons without proposal, reasons with proposal)
        return [
            ("unresolved", _finding(), SESSION, _meta(), GATE,
             [(FID, "unresolved")], [(FID, "unresolved")], build),
            ("stale", _finding(candidate=OLD), SESSION, _meta(), GATE,
             [(FID, "stale_candidate")], [(FID, "stale_candidate")], build),
            ("kind mismatch", _finding(candidate=KIND_MISMATCH), SESSION, _meta(), GATE,
             [(FID, "stale_candidate")], [(FID, "stale_candidate")], build),
            ("foreign", _finding(session=OTHER_SESSION), SESSION, _meta(), GATE,
             [(FID, "foreign_binding")], [(FID, "foreign_binding"), (FID, "proposal_invalid")],
             build),
            ("candidate unavailable", _finding(), SESSION, _meta(), None,
             [(None, "candidate_unavailable")], [(None, "candidate_unavailable")], build),
            ("head changed", _finding(), SESSION, _meta(head=OTHER_HEAD), GATE,
             [(None, "head_changed"), (FID, "unresolved")],
             [(None, "head_changed"), (FID, "unresolved")], build),
        ]

    def test_matrix(self):
        for label, record, session, meta, gate_candidate, plain, claimed, build in self._matrix():
            for with_proposal in (False, True):
                for with_rec in (False, True):
                    with self.subTest((label, with_proposal, with_rec)):
                        folded = build({FID: record}, with_proposal, with_rec)
                        got = gate.blocking_decision(meta, folded, session, PHASE, gate_candidate)
                        self.assertTrue(got["blocked"])
                        self.assertEqual(_pairs(got), claimed if with_proposal else plain)

    def test_unreadable_across_proposal_and_recommendation(self):
        for with_proposal in (False, True):
            for with_rec in (False, True):
                with self.subTest((with_proposal, with_rec)):
                    folded = _folded(
                        proposals={PID: _proposal()} if with_proposal else {},
                        rounds=[_round()] if with_rec else [],
                        recommendations={FID: [_rec()]} if with_rec else {})
                    got = gate.blocking_decision(_meta(ok=False), folded, SESSION, PHASE, GATE)
                    self.assertEqual(_pairs(got), [(None, "chain_unreadable")])

    def test_gate_candidate_moving_turns_unresolved_into_stale(self):
        folded = _folded()
        self.assertEqual(_pairs(_block(folded, gate_candidate=GATE)), [(FID, "unresolved")])
        self.assertEqual(_pairs(_block(folded, gate_candidate=NEXT)), [(FID, "stale_candidate")])
        self.assertEqual(_pairs(_block(folded, gate_candidate=_status(A))),
                         [(FID, "stale_candidate")])

    def test_close_then_move_the_candidate_reblocks_the_finding(self):
        folded = _scene()
        self.assertTrue(_close(folded)["close"])
        self.assertFalse(_close(folded, gate_candidate=NEXT)["close"])

    def test_no_evaluation_is_cached_across_calls(self):
        folded = _folded()
        first = _block(folded, gate_candidate=GATE)
        second = _block(folded, gate_candidate=NEXT)
        third = _block(folded, gate_candidate=GATE)
        self.assertEqual(_pairs(first), _pairs(third))
        self.assertNotEqual(_pairs(first), _pairs(second))


class DelegationTests(unittest.TestCase):
    """Every candidate comparison routes through the identity module."""

    def test_patched_compare_makes_an_equal_finding_stale(self):
        with mock.patch.object(cand, "candidate_compare", return_value=(False, "candidate_changed")):
            got = _block(_folded())
        self.assertEqual(_pairs(got), [(FID, "stale_candidate")])

    def test_patched_compare_makes_a_valid_proposal_invalid(self):
        with mock.patch.object(cand, "candidate_compare", return_value=(False, "candidate_changed")):
            got = gate.validate_proposal(_proposal(), _folded(), gate_candidate=GATE,
                                         session_uuid=SESSION, phase=PHASE, proposal_id=PID)
        self.assertEqual(got, [{"id": PID, "code": "proposal_invalid"}])

    def test_patched_compare_blocks_closure_on_the_proposal_path(self):
        with mock.patch.object(cand, "candidate_compare", return_value=(False, "candidate_changed")):
            got = _close(_scene())
        self.assertFalse(got["close"])

    def test_patched_compare_can_make_a_different_candidate_equal(self):
        with mock.patch.object(cand, "candidate_compare", return_value=(True, None)):
            got = _block(_folded(findings={FID: _finding(candidate=OLD)}))
        self.assertEqual(_pairs(got), [(FID, "unresolved")])

    def test_patched_validate_makes_the_gate_candidate_unavailable(self):
        with mock.patch.object(cand, "validate_candidate", return_value=(False, "candidate_unavailable")):
            blocked = _block(_folded())
            closed = _close(_scene())
        self.assertEqual(_pairs(blocked), [(None, "candidate_unavailable")])
        self.assertEqual(closed["reasons"], ["candidate_unavailable"])

    def test_patched_validate_filters_decisions(self):
        marker = _owned(D, D)
        folded = _folded(decisions={"AD-0001": _decision(candidate=marker)})
        self.assertTrue(_close(folded, consumed=CONSUMED)["close"])
        real = cand.validate_candidate

        def refuse_marker(obj):
            return (False, "candidate_unavailable") if obj == marker else real(obj)

        with mock.patch.object(cand, "validate_candidate", side_effect=refuse_marker):
            got = _close(folded, consumed=CONSUMED)
        self.assertEqual(got["reasons"], ["no_resolution_basis"])


class RegistryTests(unittest.TestCase):
    def test_gate_registry_is_the_contract_enum(self):
        self.assertEqual(set(gate.GATE_REASON_CODES), {
            "unresolved", "stale_candidate", "foreign_binding", "chain_unreadable",
            "head_changed", "proposal_invalid", "candidate_unavailable"})
        self.assertEqual(len(gate.GATE_REASON_CODES), len(set(gate.GATE_REASON_CODES)))

    def test_closure_registry_is_the_contract_enum(self):
        self.assertEqual(set(gate.CLOSURE_REASON_CODES), {
            "capability_not_granted", "candidate_unavailable", "stale_candidate",
            "proposal_invalid", "finding_unknown", "finding_not_open", "foreign_binding",
            "no_resolution_basis", "witness_not_approvable"})
        self.assertEqual(len(gate.CLOSURE_REASON_CODES), len(set(gate.CLOSURE_REASON_CODES)))

    def test_decision_principals(self):
        self.assertEqual(set(gate.DECISION_PRINCIPALS), {"supervisor_agent", "policy_principal"})

    def test_every_returned_code_is_a_member_and_every_member_is_reachable(self):
        gate_codes, closure_codes = set(), set()
        scenes = [
            _scene(), _folded(), _folded(session=OTHER_SESSION), _folded(session=""),
            _scene(finding_candidate=OLD), _scene(proposal_candidate=OLD),
            _scene(proposals={PID: _proposal(requires=[TOKEN])}),
            _folded(findings={FID: _finding(state="closed")}),
            _folded(findings={FID: _finding(session=OTHER_SESSION)}),
            _folded(findings={FID: _finding(claim_class="only")},
                    decisions={"AD-0001": _decision()}),
            _folded(findings={}), None, {"findings": []},
        ]
        metas = (_meta(), _meta(ok=False), _meta(head=OTHER_HEAD), None)
        for folded in scenes:
            for meta in metas:
                for gate_candidate in (GATE, None, OLD):
                    result = gate.blocking_decision(meta, folded, SESSION, PHASE, gate_candidate)
                    gate_codes.update(r["code"] for r in result["reasons"])
            for gate_candidate in (GATE, None, NEXT):
                for finding_id in (FID, "AF-9999"):
                    result = gate.closure_decision(folded, finding_id, gate_candidate, [], {},
                                                   CONSUMED)
                    closure_codes.update(result["reasons"])
        self.assertEqual(gate_codes, set(gate.GATE_REASON_CODES))
        self.assertEqual(closure_codes, set(gate.CLOSURE_REASON_CODES))

    def test_result_shapes(self):
        result = _block()
        self.assertEqual(set(result), {"blocked", "reasons", "head"})
        for reason in result["reasons"]:
            self.assertEqual(set(reason), {"id", "code"})
        closed = _close(_scene())
        self.assertEqual(set(closed), {"close", "reasons", "escalation_needed"})
        self.assertIsInstance(closed["close"], bool)
        self.assertIsInstance(result["blocked"], bool)


class MutationTests(unittest.TestCase):
    def _inputs(self):
        folded = _scene(
            findings={FID: _finding(claim_class="only"), FID2: _finding(candidate=OLD)},
            proposals={PID: _proposal(requires=[TOKEN]), PID2: _proposal(candidate=OLD)},
            decisions={"AD-0001": _decision(), "AD-0002": _decision(candidate=None)},
        )
        return folded, _meta(head=OTHER_HEAD), [_screen()], {PID: True}, dict(CONSUMED)

    def test_no_input_is_mutated(self):
        folded, meta, screens, coverage, consumed = self._inputs()
        snapshot = copy.deepcopy((folded, meta, screens, coverage, consumed))
        gate.blocking_decision(meta, folded, SESSION, PHASE, GATE)
        gate.closure_decision(folded, FID, GATE, screens, coverage, consumed)
        gate.closure_decision(folded, FID2, GATE, screens, coverage, consumed)
        gate.validate_proposal(folded["proposals"][PID], folded, gate_candidate=GATE,
                               session_uuid=SESSION, phase=PHASE, proposal_id=PID)
        gate.validate_proposal(folded["proposals"][PID], folded)
        self.assertEqual((folded, meta, screens, coverage, consumed), snapshot)

    def test_results_do_not_alias_inputs(self):
        folded, meta, screens, coverage, consumed = self._inputs()
        snapshot = copy.deepcopy(folded)
        blocked = gate.blocking_decision(meta, folded, SESSION, PHASE, GATE)
        closure = gate.closure_decision(folded, FID, GATE, screens, coverage, consumed)
        proposal_reasons = gate.validate_proposal("x", folded)
        blocked["reasons"].append("x")
        blocked["head"] = "changed"
        closure["reasons"].append("x")
        proposal_reasons.append("x")
        self.assertEqual(folded, snapshot)
        again = gate.blocking_decision(meta, folded, SESSION, PHASE, GATE)
        self.assertNotIn("x", again["reasons"])
        self.assertEqual(again["head"], folded["head"])


class PurityTests(unittest.TestCase):
    ALLOWED_IMPORTS = {"cowork_authority_candidate"}
    FORBIDDEN_NAMES = {"open", "os", "sys", "subprocess", "socket", "time", "datetime",
                       "pathlib", "shutil", "tempfile", "environ", "getenv", "random",
                       "input", "eval", "exec"}

    @classmethod
    def setUpClass(cls):
        path = os.path.join(_HERE, "cowork_authority_gate.py")
        with open(path, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)

    def test_imports_are_the_identity_module_only(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= self.ALLOWED_IMPORTS, imported)

    def test_no_io_or_environment_names_are_used(self):
        used = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        used |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(used & self.FORBIDDEN_NAMES, used & self.FORBIDDEN_NAMES)

    def test_candidates_are_only_compared_through_the_identity_module(self):
        compared = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Compare) and any(
                    isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                compared.append(ast.unparse(node))
        offenders = [text for text in compared if "candidate" in text or "kind" in text
                     or "digest" in text]
        self.assertEqual(offenders, [])

    def test_module_exports_the_three_predicates(self):
        for name in ("blocking_decision", "closure_decision", "validate_proposal"):
            self.assertTrue(callable(getattr(gate, name)))


if __name__ == "__main__":
    unittest.main()
