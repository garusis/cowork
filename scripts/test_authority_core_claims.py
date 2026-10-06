#!/usr/bin/env python3
"""Tests for the claim classes, marker validation and witness screen in
`cowork_claim_witness`.

The module is pure, so every input is a hand-built value: digests are repeated
characters, evidence hashes come from a dict-backed reader fake, and claim and
witness rows are plain dicts. Rows are table-driven and each rejection cause is
its own row with its own expected code.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_authority_core_claims
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
import cowork_claim_witness as cw  # noqa: E402

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64

BUILDER = "builder-seat"
AUTHOR = "witness-seat"
PATH = "evidence/one.txt"


def _owned(manifest=A, index=B):
    return {"kind": "owned_receipt", "manifest_digest": manifest, "index_digest": index}


def _status(sha=A):
    return {"kind": "status_artifact", "status_sha256": sha}


GATE = _owned()


def _claim(claim_id="c1", classes=("only",)):
    return {"claim_id": claim_id, "claim_classes": list(classes)}


def _row(**over):
    row = {
        "claim_id": "c1",
        "claim_class": "only",
        "candidate": _owned(),
        "probe": {"kind": "script", "ref": "probe-1", "author_seat": AUTHOR},
        "expected_witness": "no bypass exists",
        "result": {"outcome": "bypass_not_found", "evidence_path": PATH,
                   "evidence_sha256": D},
        "not_executable_reason": None,
    }
    row.update(over)
    return row


def _found_row(**over):
    row = _row(**over)
    if "result" not in over:
        row["result"] = dict(row["result"], outcome="bypass_found")
    return row


def _not_exec_row(**over):
    return _row(**dict({"result": None, "not_executable_reason": "no runnable probe"},
                       **over))


def _res(**over):
    base = {"outcome": "bypass_not_found", "evidence_path": PATH, "evidence_sha256": D}
    base.update(over)
    return base


def _probe(**over):
    base = {"kind": "script", "ref": "probe-1", "author_seat": AUTHOR}
    base.update(over)
    return base


def _reader(mapping=None):
    mapping = {PATH: D} if mapping is None else mapping
    return lambda path: mapping.get(path)


class _Spy:
    def __init__(self, mapping=None):
        self.mapping = {PATH: D} if mapping is None else mapping
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        return self.mapping.get(path)


def _screen(claims=None, rows=None, gate=None, seat=BUILDER, reader=None):
    return cw.screen_witnesses(
        [_claim()] if claims is None else claims,
        [_row()] if rows is None else rows,
        GATE if gate is None else gate,
        seat,
        _reader() if reader is None else reader,
    )


def _codes(result):
    return [(r["claim_id"], r["code"]) for r in result["reasons"]]


class RowOutcomeTests(unittest.TestCase):
    def test_missing_witness(self):
        result = _screen(rows=[])
        self.assertFalse(result["approvable"])
        self.assertEqual(result["verdict_override"], "revise")
        self.assertEqual(_codes(result), [("c1", "witness_missing")])

    def test_not_executable_alone_is_insufficient(self):
        result = _screen(rows=[_not_exec_row()])
        self.assertFalse(result["approvable"])
        self.assertEqual(result["verdict_override"], "insufficient_evidence")
        self.assertEqual(_codes(result), [("c1", "witness_not_executable")])

    def test_single_not_found_row_approves(self):
        result = _screen()
        self.assertTrue(result["approvable"])
        self.assertIsNone(result["verdict_override"])
        self.assertEqual(result["reasons"], [])

    def test_rows_for_unmarked_claim_ids_are_ignored(self):
        result = _screen(rows=[_row(), _row(claim_id="other", result=None)])
        self.assertTrue(result["approvable"])


class RejectionTableTests(unittest.TestCase):
    """One row per cause, each with its own code (single-claim, single-row)."""

    def _expect(self, row, code, reader=None):
        result = _screen(rows=[row], reader=reader)
        self.assertFalse(result["approvable"], code)
        self.assertEqual(result["verdict_override"], "revise", code)
        self.assertEqual(_codes(result), [("c1", code)])

    def test_candidate_causes(self):
        rows = (
            ("different kind", _status(A), "witness_candidate_kind_mismatch"),
            ("older candidate", _owned(C, B), "witness_candidate_changed"),
            ("other index", _owned(A, C), "witness_candidate_changed"),
            ("invalid candidate", {"kind": "owned_receipt"}, "witness_candidate_unavailable"),
            ("null candidate", None, "witness_candidate_unavailable"),
            ("non-dict candidate", "x", "witness_candidate_unavailable"),
        )
        for name, candidate, code in rows:
            with self.subTest(name):
                self._expect(_row(candidate=candidate), code)

    def test_builder_seat(self):
        self._expect(_row(probe=_probe(author_seat=BUILDER)), "witness_builder_authored")

    def test_evidence_missing_variants(self):
        for name, reader in (
            ("reader returns None", _reader({})),
            ("reader raises", lambda path: (_ for _ in ()).throw(OSError("gone"))),
            ("reader returns non-hex", _reader({PATH: "zz"})),
            ("reader returns non-str", _reader({PATH: 7})),
            ("reader returns trailing newline", _reader({PATH: D + "\n"})),
            ("reader returns upper hex", _reader({PATH: "D" * 64})),
        ):
            with self.subTest(name):
                self._expect(_row(), "witness_evidence_missing", reader=reader)

    def test_evidence_sha_mismatch(self):
        self._expect(_row(), "witness_evidence_sha_mismatch", reader=_reader({PATH: E}))

    def test_outcome_outside_closed_set(self):
        for outcome in ("bypass_maybe", "", "BYPASS_NOT_FOUND", None, 7, ["bypass_found"]):
            with self.subTest(repr(outcome)):
                self._expect(_row(result=_res(outcome=outcome)), "witness_outcome_invalid")

    def test_result_consistency(self):
        self._expect(_row(result=None, not_executable_reason=None), "witness_result_inconsistent")
        self._expect(_row(result=None, not_executable_reason=""), "witness_result_inconsistent")
        self._expect(_row(not_executable_reason="why"), "witness_result_inconsistent")
        self._expect(_row(not_executable_reason=""), "witness_result_inconsistent")

    def test_class_not_declared(self):
        self._expect(_row(claim_class="never"), "witness_class_mismatch")
        self._expect(_row(claim_class="not_a_class"), "witness_class_mismatch")

    def test_malformed_value_types(self):
        rows = (
            ("author_seat None", _row(probe=_probe(author_seat=None))),
            ("author_seat empty", _row(probe=_probe(author_seat=""))),
            ("author_seat int", _row(probe=_probe(author_seat=7))),
            ("author_seat list", _row(probe=_probe(author_seat=[AUTHOR]))),
            ("probe kind empty", _row(probe=_probe(kind=""))),
            ("probe kind int", _row(probe=_probe(kind=3))),
            ("probe ref empty", _row(probe=_probe(ref=""))),
            ("probe ref None", _row(probe=_probe(ref=None))),
            ("probe not dict", _row(probe="probe")),
            ("probe extra key", _row(probe=dict(_probe(), extra="x"))),
            ("probe missing key", _row(probe={"kind": "script", "ref": "r"})),
            ("expected_witness int", _row(expected_witness=3)),
            ("expected_witness empty", _row(expected_witness="")),
            ("evidence_path int", _row(result=_res(evidence_path=3))),
            ("evidence_path empty", _row(result=_res(evidence_path=""))),
            ("sha non-hex", _row(result=_res(evidence_sha256="zz"))),
            ("sha upper", _row(result=_res(evidence_sha256="D" * 64))),
            ("sha newline", _row(result=_res(evidence_sha256=D + "\n"))),
            ("result not dict", _row(result="x")),
            ("result extra key", _row(result=dict(_res(), extra="x"))),
            ("result missing key", _row(result={"outcome": "bypass_found"})),
            ("reason not str", _row(result=None, not_executable_reason=7)),
            ("claim_class empty", _row(claim_class="")),
            ("claim_class int", _row(claim_class=4)),
            ("id bool", _row(id=True)),
            ("id list", _row(id=[1])),
            ("unknown key", _row(note="x")),
        )
        for name, row in rows:
            with self.subTest(name):
                spy = _Spy()
                self._expect(row, "witness_malformed", reader=spy)
                self.assertEqual(spy.calls, [], name)

    def test_missing_required_key(self):
        for key in ("claim_class", "candidate", "probe", "expected_witness",
                    "result", "not_executable_reason"):
            with self.subTest(key):
                row = _row()
                del row[key]
                self._expect(row, "witness_malformed")

    def test_envelope_keys_are_malformed(self):
        for key in ("schema", "seq", "kind", "phase", "round", "session_uuid",
                    "prev_digest", "digest", "op_key"):
            with self.subTest(key):
                self._expect(_row(**{key: "x"}), "witness_malformed")

    def test_optional_id_passthrough(self):
        for row_id in ("w-1", 4):
            with self.subTest(repr(row_id)):
                self.assertTrue(_screen(rows=[_row(id=row_id)])["approvable"])

    def test_codes_are_distinct_per_cause(self):
        codes = {
            "witness_candidate_kind_mismatch", "witness_candidate_changed",
            "witness_builder_authored", "witness_evidence_missing",
            "witness_evidence_sha_mismatch", "witness_outcome_invalid",
            "witness_result_inconsistent", "witness_class_mismatch",
            "witness_malformed", "witness_missing", "witness_not_executable",
        }
        self.assertTrue(codes <= set(cw.REASON_CODES))


class NeutralNegativeTests(unittest.TestCase):
    def test_builder_authored_positive_as_only_witness(self):
        result = _screen(rows=[_row(probe=_probe(author_seat=BUILDER))])
        self.assertFalse(result["approvable"])
        self.assertEqual(_codes(result), [("c1", "witness_builder_authored")])

    def test_older_candidate_witness(self):
        result = _screen(rows=[_row(candidate=_owned(C, B))])
        self.assertFalse(result["approvable"])
        self.assertEqual(_codes(result), [("c1", "witness_candidate_changed")])

    def test_different_kind_witness(self):
        result = _screen(rows=[_row(candidate=_status(A))])
        self.assertFalse(result["approvable"])
        self.assertEqual(_codes(result), [("c1", "witness_candidate_kind_mismatch")])

    def test_evidence_edited_after_recording(self):
        result = _screen(reader=_reader({PATH: E}))
        self.assertFalse(result["approvable"])
        self.assertEqual(_codes(result), [("c1", "witness_evidence_sha_mismatch")])


class PrecedenceTests(unittest.TestCase):
    def _first(self, row, code, reader=None):
        result = _screen(rows=[row], reader=reader)
        self.assertEqual(_codes(result), [("c1", code)])

    def test_shape_before_class(self):
        self._first(_row(claim_class="never", probe=_probe(author_seat="")),
                    "witness_malformed")

    def test_class_before_candidate(self):
        self._first(_row(claim_class="never", candidate=_owned(C, B)),
                    "witness_class_mismatch")

    def test_candidate_before_seat(self):
        self._first(_row(candidate=_owned(C, B), probe=_probe(author_seat=BUILDER)),
                    "witness_candidate_changed")

    def test_seat_before_result_consistency(self):
        self._first(_row(probe=_probe(author_seat=BUILDER), result=None,
                         not_executable_reason=None),
                    "witness_builder_authored")

    def test_consistency_before_outcome(self):
        self._first(_row(result=_res(outcome="x"), not_executable_reason="why"),
                    "witness_result_inconsistent")

    def test_outcome_before_evidence(self):
        spy = _Spy({})
        self._first(_row(result=_res(outcome="x")), "witness_outcome_invalid", reader=spy)
        self.assertEqual(spy.calls, [])

    def test_evidence_missing_and_mismatch_are_separate_single_defects(self):
        self._first(_row(), "witness_evidence_missing", reader=_reader({}))
        self._first(_row(), "witness_evidence_sha_mismatch", reader=_reader({PATH: E}))


class ReaderSpyTests(unittest.TestCase):
    def test_called_once_with_path_for_valid_executed_row(self):
        spy = _Spy()
        self.assertTrue(_screen(reader=spy)["approvable"])
        self.assertEqual(spy.calls, [PATH])

    def test_not_called_for_not_executable_row(self):
        spy = _Spy()
        _screen(rows=[_not_exec_row()], reader=spy)
        self.assertEqual(spy.calls, [])

    def test_not_called_for_rows_rejected_by_a_pure_stage(self):
        rows = (
            _row(claim_class="never"),
            _row(candidate=_owned(C, B)),
            _row(candidate=_status(A)),
            _row(probe=_probe(author_seat=BUILDER)),
            _row(not_executable_reason="why"),
            _row(result=_res(outcome="x")),
            _row(probe=_probe(author_seat=None)),
            _row(result=_res(evidence_path=3)),
        )
        for index, row in enumerate(rows):
            with self.subTest(index):
                spy = _Spy()
                _screen(rows=[row], reader=spy)
                self.assertEqual(spy.calls, [])

    def test_not_called_when_a_whole_screen_fault_exists(self):
        spy = _Spy()
        cw.screen_witnesses([_claim()], [_row(), 5], GATE, BUILDER, spy)
        self.assertEqual(spy.calls, [])


class ProjectionContractTests(unittest.TestCase):
    def test_envelope_row_is_malformed_for_its_claim(self):
        row = _row(schema=1, seq=3, digest=A)
        result = _screen(rows=[row])
        self.assertFalse(result["approvable"])
        self.assertEqual(_codes(result), [("c1", "witness_malformed")])

    def test_folded_shaped_row_is_a_screen_fault_even_beside_an_approving_row(self):
        folded = {"claim": "c1", "result": "bypass_not_found", "candidate": _owned()}
        result = _screen(rows=[_row(), folded])
        self.assertFalse(result["approvable"])
        self.assertEqual(result["verdict_override"], "revise")
        self.assertEqual(_codes(result), [(None, "witness_malformed")])

    def test_non_dict_row_is_a_screen_fault(self):
        for bad in (None, "row", 3, [1], ()):
            with self.subTest(repr(bad)):
                result = _screen(rows=[_row(), bad])
                self.assertFalse(result["approvable"])
                self.assertEqual(result["verdict_override"], "revise")
                self.assertEqual(_codes(result), [(None, "witness_malformed")])

    def test_non_string_claim_id_row_is_a_screen_fault(self):
        for bad in (None, 3, ["c1"]):
            with self.subTest(repr(bad)):
                result = _screen(rows=[_row(), _row(claim_id=bad)])
                self.assertEqual(_codes(result), [(None, "witness_malformed")])

    def test_each_unattributable_row_is_reported(self):
        result = _screen(rows=[_row(), 1, None])
        self.assertEqual(_codes(result), [(None, "witness_malformed")] * 2)

    def test_string_claim_id_matching_no_claim_is_ignored(self):
        result = _screen(rows=[_row(), {"claim_id": "zzz", "junk": True}])
        self.assertTrue(result["approvable"])


class ApprovalTests(unittest.TestCase):
    def test_stale_and_bad_rows_beside_an_approving_row_are_silent(self):
        rows = [
            _row(candidate=_owned(C, B)),
            _row(probe=_probe(author_seat=BUILDER)),
            _row(result=_res(evidence_sha256=E)),
            _row(claim_class="never"),
            _row(note="x"),
            _row(),
            _not_exec_row(),
        ]
        result = _screen(rows=rows)
        self.assertTrue(result["approvable"])
        self.assertEqual(result["reasons"], [])

    def test_multi_class_claim_needs_each_class(self):
        claims = [_claim("c1", ("only", "never"))]
        only = _row(claim_class="only")
        never = _row(claim_class="never")
        self.assertTrue(_screen(claims=claims, rows=[only, never])["approvable"])
        partial = _screen(claims=claims, rows=[only])
        self.assertFalse(partial["approvable"])
        self.assertEqual(partial["verdict_override"], "revise")
        self.assertEqual(_codes(partial), [("c1", "witness_missing")])

    def test_each_marked_claim_needs_its_own_approval(self):
        claims = [_claim("c1"), _claim("c2", ("never",))]
        rows = [_row(), _row(claim_id="c2", claim_class="never")]
        self.assertTrue(_screen(claims=claims, rows=rows)["approvable"])
        result = _screen(claims=claims, rows=rows[:1])
        self.assertEqual(_codes(result), [("c2", "witness_missing")])

    def test_empty_claims_with_valid_inputs_is_approvable(self):
        result = _screen(claims=[], rows=[])
        self.assertTrue(result["approvable"])
        self.assertIsNone(result["verdict_override"])
        self.assertEqual(result["reasons"], [])

    def test_empty_claims_gets_no_exemption_for_hostile_inputs(self):
        for name, args, code in (
            ("gate None", ([], [], None, BUILDER, _reader()), "gate_candidate_unavailable"),
            ("seat empty", ([], [], GATE, "", _reader()), "builder_seat_unavailable"),
            ("seat int", ([], [], GATE, 5, _reader()), "builder_seat_unavailable"),
            ("witnesses not list", ([], "rows", GATE, BUILDER, _reader()), "witness_malformed"),
            ("unattributable row", ([], [3], GATE, BUILDER, _reader()), "witness_malformed"),
            ("reader not callable", ([], [], GATE, BUILDER, None), "read_file_sha256_unavailable"),
        ):
            with self.subTest(name):
                result = cw.screen_witnesses(*args)
                self.assertFalse(result["approvable"])
                self.assertEqual(result["verdict_override"], "revise")
                self.assertEqual(_codes(result), [(None, code)])


class DominanceTests(unittest.TestCase):
    def test_bypass_found_beats_bypass_not_found(self):
        result = _screen(rows=[_row(), _found_row()])
        self.assertFalse(result["approvable"])
        self.assertEqual(result["verdict_override"], "revise")
        self.assertEqual(_codes(result), [("c1", "witness_bypass_found")])
        reversed_rows = _screen(rows=[_found_row(), _row()])
        self.assertEqual(_codes(reversed_rows), [("c1", "witness_bypass_found")])

    def test_bypass_found_alone_is_not_approvable(self):
        result = _screen(rows=[_found_row()])
        self.assertFalse(result["approvable"])
        self.assertEqual(result["verdict_override"], "revise")

    def test_unverified_bypass_found_does_not_count_as_found(self):
        result = _screen(rows=[_row(), _found_row(result=_res(outcome="bypass_found",
                                                              evidence_sha256=E))])
        self.assertTrue(result["approvable"])

    def test_not_executable_with_rejected_row_is_revise(self):
        result = _screen(rows=[_not_exec_row(), _row(candidate=_owned(C, B))])
        self.assertEqual(result["verdict_override"], "revise")
        self.assertEqual(_codes(result), [("c1", "witness_candidate_changed"),
                                          ("c1", "witness_not_executable")])

    def test_builder_authored_not_executable_cannot_steer_to_insufficient(self):
        result = _screen(rows=[_not_exec_row(probe=_probe(author_seat=BUILDER))])
        self.assertEqual(result["verdict_override"], "revise")
        self.assertEqual(_codes(result), [("c1", "witness_builder_authored")])

    def test_stale_not_executable_cannot_steer_to_insufficient(self):
        result = _screen(rows=[_not_exec_row(candidate=_owned(C, B))])
        self.assertEqual(result["verdict_override"], "revise")

    def test_not_executable_beside_approving_row_is_silent(self):
        self.assertTrue(_screen(rows=[_not_exec_row(), _row()])["approvable"])

    def test_revise_dominates_insufficient_across_claims(self):
        claims = [_claim("c1"), _claim("c2")]
        rows = [_not_exec_row(claim_id="c1")]
        result = _screen(claims=claims, rows=rows)
        self.assertEqual(result["verdict_override"], "revise")
        self.assertEqual(_codes(result), [("c1", "witness_not_executable"),
                                          ("c2", "witness_missing")])

    def test_only_not_executable_gaps_are_insufficient(self):
        claims = [_claim("c1"), _claim("c2", ("never",))]
        rows = [_not_exec_row(claim_id="c1"),
                _not_exec_row(claim_id="c2", claim_class="never")]
        result = _screen(claims=claims, rows=rows)
        self.assertEqual(result["verdict_override"], "insufficient_evidence")
        self.assertEqual(_codes(result), [("c1", "witness_not_executable"),
                                          ("c2", "witness_not_executable")])

    def test_claim_level_rejection_applies_to_every_unapproved_pair(self):
        claims = [_claim("c1", ("only", "never"))]
        rows = [_row(claim_class="only"), _row(claim_class="never", note="x")]
        result = _screen(claims=claims, rows=rows)
        self.assertEqual(result["verdict_override"], "revise")
        self.assertEqual(_codes(result), [("c1", "witness_malformed")])

    def test_reasons_are_deduped_and_ordered_by_claim_class_and_row(self):
        claims = [_claim("c1", ("only", "never")), _claim("c2")]
        rows = [
            _row(claim_id="c2", candidate=_owned(C, B)),
            _row(claim_class="never", candidate=_status(A)),
            _row(claim_class="only", candidate=_owned(C, B)),
            _row(claim_class="only", candidate=_owned(C, A)),
        ]
        result = _screen(claims=claims, rows=rows)
        self.assertEqual(_codes(result), [
            ("c1", "witness_candidate_changed"),
            ("c1", "witness_candidate_kind_mismatch"),
            ("c2", "witness_candidate_changed"),
        ])


class FaultTests(unittest.TestCase):
    def _fault(self, result, code):
        self.assertFalse(result["approvable"])
        self.assertEqual(result["verdict_override"], "revise")
        self.assertIn(code, [r["code"] for r in result["reasons"]])

    def test_claims_faults(self):
        cases = (
            ("claims None", None, "claims_malformed"),
            ("claims tuple", (_claim(),), "claims_malformed"),
            ("claims str", "c1", "claims_malformed"),
            ("entry non-dict", [3], "claims_malformed"),
            ("entry no claim_id", [{"claim_classes": ["only"]}], "claims_malformed"),
            ("entry empty claim_id", [{"claim_id": "", "claim_classes": ["only"]}],
             "claims_malformed"),
            ("entry int claim_id", [{"claim_id": 1, "claim_classes": ["only"]}],
             "claims_malformed"),
            ("duplicate claim_id", [_claim("c1"), _claim("c1", ("never",))],
             "claims_malformed"),
            ("classes missing", [{"claim_id": "c1"}], "claims_malformed"),
            ("classes None", [{"claim_id": "c1", "claim_classes": None}], "claims_malformed"),
            ("classes empty", [_claim("c1", ())], "claims_malformed"),
            ("classes str", [{"claim_id": "c1", "claim_classes": "only"}], "claims_malformed"),
            ("classes non-str element", [{"claim_id": "c1", "claim_classes": [1]}],
             "claims_malformed"),
            ("classes duplicate", [_claim("c1", ("only", "only"))], "claims_malformed"),
            ("unknown class", [_claim("c1", ("bogus",))], "claim_class_unknown"),
            ("one known one unknown", [_claim("c1", ("only", "bogus"))], "claim_class_unknown"),
        )
        for name, claims, code in cases:
            with self.subTest(name):
                result = cw.screen_witnesses(claims, [_row()], GATE, BUILDER, _reader())
                self._fault(result, code)

    def test_unknown_class_names_the_claim(self):
        result = _screen(claims=[_claim("c1", ("bogus",))])
        self.assertEqual(_codes(result), [("c1", "claim_class_unknown")])

    def test_gate_candidate_faults(self):
        for gate in (None, "x", {}, {"kind": "owned_receipt"}, _owned("z", B),
                     dict(_owned(), extra=A)):
            with self.subTest(repr(gate)):
                result = cw.screen_witnesses([_claim()], [_row()], gate, BUILDER, _reader())
                self._fault(result, "gate_candidate_unavailable")

    def test_builder_seat_faults(self):
        for seat in (None, "", 5, ["b"], b"b"):
            with self.subTest(repr(seat)):
                result = cw.screen_witnesses([_claim()], [_row()], GATE, seat, _reader())
                self._fault(result, "builder_seat_unavailable")

    def test_reader_faults(self):
        for reader in (None, "reader", 5, {PATH: D}):
            with self.subTest(repr(reader)):
                result = cw.screen_witnesses([_claim()], [_row()], GATE, BUILDER, reader)
                self._fault(result, "read_file_sha256_unavailable")

    def test_witness_container_faults(self):
        for rows in (None, "rows", {}, (_row(),)):
            with self.subTest(repr(rows)):
                result = cw.screen_witnesses([_claim()], rows, GATE, BUILDER, _reader())
                self._fault(result, "witness_malformed")

    def test_raising_reader_is_a_row_reason_not_a_fault(self):
        def boom(path):
            raise RuntimeError("x")
        result = _screen(reader=boom)
        self.assertEqual(_codes(result), [("c1", "witness_evidence_missing")])

    def test_hostile_inputs_never_raise_and_never_approve(self):
        hostile = (None, 0, 1, True, "", "x", b"x", [], {}, (), [None], [[]], [{}],
                   {"a": 1}, object(), float("nan"))
        base = [[_claim()], [_row()], GATE, BUILDER, _reader()]
        for position in range(5):
            for value in hostile:
                if position == 0 and value == []:
                    continue  # an empty claims list is valid: nothing is marked
                if position == 3 and value == "x":
                    continue  # a non-empty string is a valid seat name
                args = list(base)
                args[position] = value
                with self.subTest(position=position, value=repr(value)):
                    result = cw.screen_witnesses(*args)
                    self.assertFalse(result["approvable"])
                    self.assertIn(result["verdict_override"],
                                  ("revise", "insufficient_evidence"))

    def test_hostile_row_fields_never_raise(self):
        hostile = (None, 0, "", "x", [], {}, [None], {"a": 1}, object())
        for key in ("claim_class", "candidate", "probe", "expected_witness",
                    "result", "not_executable_reason"):
            for value in hostile:
                with self.subTest(key=key, value=repr(value)):
                    result = _screen(rows=[_row(**{key: value})])
                    self.assertIn(result["verdict_override"],
                                  (None, "revise", "insufficient_evidence"))
        self.assertFalse(_screen(rows=[_row(result={"outcome": [], "evidence_path": PATH,
                                                    "evidence_sha256": D})])["approvable"])


class MarkerTests(unittest.TestCase):
    def test_valid_and_unmarked_items(self):
        items = [
            {"claim_classes": ["only"]},
            {"claim_classes": list(cw.CLAIM_CLASSES)},
            {"text": "no marker"},
            {"claim_classes": None},
            {},
        ]
        self.assertEqual(cw.validate_claim_markers(items), [])
        self.assertEqual(cw.validate_claim_markers([]), [])

    def test_each_problem_code_with_index(self):
        cases = (
            ("not a list", {"claim_classes": "only"}, "marker_classes_malformed"),
            ("empty list", {"claim_classes": []}, "marker_classes_malformed"),
            ("non-str", {"claim_classes": [1]}, "marker_classes_malformed"),
            ("tuple", {"claim_classes": ("only",)}, "marker_classes_malformed"),
            ("unknown", {"claim_classes": ["bogus"]}, "marker_unknown_class"),
            ("duplicate", {"claim_classes": ["only", "only"]}, "marker_duplicate_class"),
        )
        for name, item, code in cases:
            with self.subTest(name):
                self.assertEqual(cw.validate_claim_markers([{"ok": 1}, item]),
                                 [{"index": 1, "code": code}])

    def test_multiple_problems_in_one_item_are_ordered_once_each(self):
        item = {"claim_classes": ["bogus", "bogus", 3]}
        self.assertEqual(cw.validate_claim_markers([item]), [
            {"index": 0, "code": "marker_classes_malformed"},
            {"index": 0, "code": "marker_unknown_class"},
            {"index": 0, "code": "marker_duplicate_class"},
        ])

    def test_non_list_and_non_dict_items(self):
        for bad in (None, "x", {}, 3, ({},)):
            with self.subTest(repr(bad)):
                self.assertEqual(cw.validate_claim_markers(bad),
                                 [{"index": None, "code": "marker_items_malformed"}])
        self.assertEqual(cw.validate_claim_markers([3, None, "x"]), [
            {"index": 0, "code": "marker_item_malformed"},
            {"index": 1, "code": "marker_item_malformed"},
            {"index": 2, "code": "marker_item_malformed"},
        ])

    def test_codes_are_registered_and_input_is_unchanged(self):
        items = [3, {"claim_classes": ["x", "x", 1]}, {"claim_classes": []}]
        before = copy.deepcopy(items)
        problems = cw.validate_claim_markers(items)
        self.assertEqual(items, before)
        self.assertTrue({p["code"] for p in problems} <= set(cw.MARKER_CODES))

    def test_class_vocabulary(self):
        self.assertEqual(set(cw.CLAIM_CLASSES),
                         {"source_of_truth", "only", "never", "fail_closed"})
        self.assertEqual(set(cw.WITNESS_OUTCOMES), {"bypass_not_found", "bypass_found"})


class ScanTests(unittest.TestCase):
    def _hit(self, text, classes, terms):
        self.assertEqual(cw.scan_unmarked_claims([{"text": text}]),
                         [{"index": 0, "classes": classes, "terms": terms}], text)

    def test_term_table_rows(self):
        expected = {
            "only": "only", "sole": "only", "solely": "only", "exclusive": "only",
            "exclusively": "only", "never": "never", "cannot": "never",
            "impossible": "never", "must not": "never", "fail closed": "fail_closed",
            "fail-closed": "fail_closed", "source of truth": "source_of_truth",
            "single source": "source_of_truth",
        }
        self.assertEqual(dict(cw.SCAN_TERMS), expected)
        for term, cls in expected.items():
            with self.subTest(term):
                self._hit("this is %s here" % term, [cls], [term])

    def test_whole_word_and_case_insensitive(self):
        self.assertEqual(cw.scan_unmarked_claims([{"text": "a foonly onlyness"}]), [])
        self.assertEqual(cw.scan_unmarked_claims([{"text": "nevertheless"}]), [])
        self._hit("It NEVER fails", ["never"], ["never"])
        self._hit("The Source  Of Truth", ["source_of_truth"], ["source of truth"])

    def test_classes_and_terms_are_ordered(self):
        self._hit("never fail-closed, only here, the single source",
                  ["source_of_truth", "only", "never", "fail_closed"],
                  ["only", "never", "fail-closed", "single source"])

    def test_scans_top_level_strings_and_list_elements_only(self):
        item = {"a": ["only", 3], "b": {"nested": "never"}, "c": 7}
        self.assertEqual(cw.scan_unmarked_claims([item]),
                         [{"index": 0, "classes": ["only"], "terms": ["only"]}])

    def test_marked_items_yield_no_hit_and_invalid_markers_are_scanned(self):
        marked = {"claim_classes": ["only"], "text": "only this"}
        bad = {"claim_classes": ["bogus"], "text": "only this"}
        empty = {"claim_classes": [], "text": "never"}
        hits = cw.scan_unmarked_claims([marked, bad, empty, {"claim_classes": None, "t": "sole"}])
        self.assertEqual([h["index"] for h in hits], [1, 2, 3])

    def test_marker_values_are_not_scanned(self):
        self.assertEqual(cw.scan_unmarked_claims([{"claim_classes": ["only", "only"]}]), [])

    def test_non_list_and_non_dict_input(self):
        for bad in (None, "only", {}, 3):
            self.assertEqual(cw.scan_unmarked_claims(bad), [])
        self.assertEqual(cw.scan_unmarked_claims([None, 3, "only", ["only"]]), [])

    def test_input_unchanged_and_result_fresh(self):
        items = [{"text": "only"}]
        before = copy.deepcopy(items)
        first = cw.scan_unmarked_claims(items)
        self.assertEqual(items, before)
        first[0]["terms"].append("x")
        self.assertEqual(cw.scan_unmarked_claims(items)[0]["terms"], ["only"])

    def test_screen_result_is_identical_with_or_without_scan_hits(self):
        claims = [dict(_claim(), text="only and never and source of truth")]
        self.assertTrue(cw.scan_unmarked_claims([{"text": "only and never"}]))
        scenarios = ([_row()], [], [_not_exec_row()], [_found_row()])
        expected = [_screen(claims=claims, rows=rows) for rows in scenarios]
        calls = []

        def stub(items):
            calls.append(items)
            return [{"index": 0, "classes": ["only"], "terms": ["only"]}]

        with mock.patch.object(cw, "scan_unmarked_claims", stub):
            actual = [_screen(claims=claims, rows=rows) for rows in scenarios]
        self.assertEqual(calls, [])
        self.assertEqual(actual, expected)


class SplitConstantTests(unittest.TestCase):
    def _results(self):
        return (
            _screen(),
            _screen(rows=[]),
            _screen(rows=[_not_exec_row()]),
            _screen(claims=[]),
            _screen(gate={}),
            _screen(claims=[], rows=[], seat=""),
        )

    def test_split_is_in_every_result_shape(self):
        for result in self._results():
            split = result["judgment_split"]
            self.assertEqual(set(split), {"machine_checked", "reviewer_judged", "unverifiable"})
            self.assertEqual(split["machine_checked"], list(cw.MACHINE_CHECKED))
            self.assertEqual(split["reviewer_judged"], list(cw.REVIEWER_JUDGED))
            self.assertEqual(split["unverifiable"], cw.UNVERIFIABLE_LIMIT)

    def test_split_names_the_sets_and_the_limit(self):
        self.assertEqual(set(cw.MACHINE_CHECKED), {
            "candidate_equality", "author_seat", "evidence_file_sha256", "closed_outcomes"})
        self.assertEqual(set(cw.REVIEWER_JUDGED), {"probe_adequacy", "author_independence"})
        text = " ".join(cw.UNVERIFIABLE_LIMIT.split())
        self.assertIn("cannot verify that the probe actually ran against the tree", text)

    def test_results_do_not_share_split_state(self):
        constants = (cw.MACHINE_CHECKED, cw.REVIEWER_JUDGED, cw.UNVERIFIABLE_LIMIT)
        first, second = _screen(), _screen()
        first["judgment_split"]["machine_checked"].append("x")
        first["judgment_split"]["reviewer_judged"].clear()
        first["judgment_split"]["unverifiable"] = "x"
        self.assertEqual(second["judgment_split"], cw.judgment_split())
        self.assertEqual(cw.judgment_split()["machine_checked"], list(cw.MACHINE_CHECKED))
        self.assertEqual(constants, (cw.MACHINE_CHECKED, cw.REVIEWER_JUDGED,
                                     cw.UNVERIFIABLE_LIMIT))
        self.assertIsNot(cw.judgment_split(), cw.judgment_split())


class ResultInvariantTests(unittest.TestCase):
    def _scenarios(self):
        claims2 = [_claim("c1", ("only", "never")), _claim("c2")]
        return [
            dict(),
            dict(rows=[]),
            dict(rows=[_not_exec_row()]),
            dict(rows=[_found_row()]),
            dict(rows=[_row(), _found_row()]),
            dict(rows=[_row(note="x")]),
            dict(rows=[_row(), 5]),
            dict(claims=[]),
            dict(claims=claims2, rows=[_row(), _row(claim_class="never"),
                                       _not_exec_row(claim_id="c2")]),
            dict(claims=claims2, rows=[_row(candidate=_owned(C, B))]),
            dict(claims=[_claim("c1", ("bogus",))]),
            dict(claims=None),
            dict(gate={}),
            dict(seat=""),
            dict(reader=None),
        ]

    def _call(self, scenario):
        return (scenario["claims"] if "claims" in scenario else [_claim()],
                scenario["rows"] if "rows" in scenario else [_row()],
                scenario["gate"] if "gate" in scenario else GATE,
                scenario["seat"] if "seat" in scenario else BUILDER,
                scenario["reader"] if "reader" in scenario else _reader())

    def test_invariants_over_scenarios(self):
        for index, scenario in enumerate(self._scenarios()):
            with self.subTest(index):
                claims, rows, gate, seat, reader = self._call(scenario)
                snapshot = copy.deepcopy((claims, rows, gate, seat))
                result = cw.screen_witnesses(claims, rows, gate, seat, reader)
                self.assertEqual((claims, rows, gate, seat), snapshot)
                self.assertEqual(set(result), {"approvable", "verdict_override",
                                               "reasons", "judgment_split"})
                self.assertIsInstance(result["approvable"], bool)
                self.assertEqual(result["approvable"], result["reasons"] == [])
                self.assertEqual(result["approvable"], result["verdict_override"] is None)
                self.assertIn(result["verdict_override"],
                              (None, "revise", "insufficient_evidence"))
                for reason in result["reasons"]:
                    self.assertEqual(set(reason), {"claim_id", "code"})
                    self.assertIn(reason["code"], cw.REASON_CODES)
                    self.assertTrue(reason["claim_id"] is None
                                    or isinstance(reason["claim_id"], str))
                for obj in (claims, rows, gate, seat):
                    self.assertIsNot(result, obj)
                    self.assertIsNot(result["reasons"], obj)

    def test_results_are_deterministic(self):
        for index, scenario in enumerate(self._scenarios()):
            with self.subTest(index):
                args = self._call(scenario)
                self.assertEqual(cw.screen_witnesses(*args), cw.screen_witnesses(*args))

    def test_reason_codes_registry_is_closed_and_unique(self):
        self.assertEqual(len(cw.REASON_CODES), len(set(cw.REASON_CODES)))
        self.assertEqual(len(cw.MARKER_CODES), len(set(cw.MARKER_CODES)))


class PurityTests(unittest.TestCase):
    ALLOWED_IMPORTS = {"re", "cowork_authority_candidate"}
    FORBIDDEN_NAMES = {"open", "os", "sys", "subprocess", "socket", "time", "datetime",
                       "pathlib", "shutil", "tempfile", "environ", "getenv"}
    DYNAMIC_NAMES = {"__import__", "exec", "eval", "compile", "importlib"}
    DYNAMIC_ATTRS = {"import_module", "__import__"}
    IDENTITY_LITERALS = {"manifest_digest", "index_digest", "status_sha256",
                         "owned_receipt", "status_artifact"}

    @classmethod
    def setUpClass(cls):
        path = os.path.join(_HERE, "cowork_claim_witness.py")
        with open(path, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)

    def test_imports_are_the_declared_edge_only(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= self.ALLOWED_IMPORTS, imported)
        self.assertEqual({n for n in imported if n.startswith("cowork")},
                         {"cowork_authority_candidate"})

    def test_no_io_environment_or_dynamic_names(self):
        names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)}
        self.assertFalse((names | attrs) & self.FORBIDDEN_NAMES,
                         (names | attrs) & self.FORBIDDEN_NAMES)
        self.assertFalse(names & self.DYNAMIC_NAMES, names & self.DYNAMIC_NAMES)
        self.assertFalse(attrs & self.DYNAMIC_ATTRS, attrs & self.DYNAMIC_ATTRS)

    def test_no_candidate_identity_literals_or_kind_table(self):
        docstrings = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                        body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        literals = {n.value for n in ast.walk(self.tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings}
        self.assertFalse(literals & self.IDENTITY_LITERALS, literals & self.IDENTITY_LITERALS)
        names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn("CANDIDATE_KINDS", names | attrs)

    def test_equality_goes_through_the_installed_candidate_api(self):
        calls = {
            n.func.attr for n in ast.walk(self.tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "cowork_authority_candidate"
        }
        self.assertTrue(calls & {"candidate_compare", "candidate_equal"}, calls)
        self.assertTrue(hasattr(cand, "candidate_compare"))

    def test_docstring_states_the_probe_ran_limit(self):
        flat = " ".join((ast.get_docstring(self.tree) or "").split())
        self.assertIn("Cowork cannot verify that the probe actually ran against the tree", flat)


if __name__ == "__main__":
    unittest.main()
