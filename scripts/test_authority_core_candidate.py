#!/usr/bin/env python3
"""Tests for the candidate identity, equality and selection helpers in
`cowork_authority_candidate`.

The module is pure, so every input is a hand-built value: digests are short
repeated characters, pointer reads and status fingerprints are plain dicts.
Rows are table-driven and each pointer case is its own row with its own reason.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_authority_core_candidate
"""

import ast
import copy
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_authority_candidate as cand  # noqa: E402

A = "a" * 64
B = "b" * 64
C = "c" * 64

UNAVAILABLE = (False, "candidate_unavailable")


def _owned(manifest=A, index=B):
    return {"kind": "owned_receipt", "manifest_digest": manifest, "index_digest": index}


def _status(sha=A):
    return {"kind": "status_artifact", "status_sha256": sha}


def _fp(sha=A, exists=True):
    return {"exists": exists, "status": "ok", "sha256": sha, "size": 3, "mtime_ns": 1}


def _pointer(data, exists=True):
    return {"exists": exists, "data": data}


def _receipt_data(**over):
    data = {
        "transaction_id": "tx-1",
        "manifest_digest": A,
        "index_digest": B,
        "receipt_path": "/x/receipt.json",
        "disposition": "accepted",
    }
    data.update(over)
    return data


class ValidateCandidateTests(unittest.TestCase):
    def test_both_valid_shapes(self):
        for label, obj in (("owned", _owned()), ("status", _status())):
            with self.subTest(label):
                self.assertEqual(cand.validate_candidate(obj), (True, None))

    def test_refusals(self):
        rows = [
            ("empty dict", {}),
            ("owned missing index", {"kind": "owned_receipt", "manifest_digest": A}),
            ("owned missing manifest", {"kind": "owned_receipt", "index_digest": A}),
            ("owned missing kind", {"manifest_digest": A, "index_digest": B}),
            ("owned with extra key", dict(_owned(), extra=A)),
            ("owned with status_sha256", dict(_owned(), status_sha256=A)),
            ("status with manifest_digest", dict(_status(), manifest_digest=A)),
            ("status with index_digest", dict(_status(), index_digest=A)),
            ("status with extra key", dict(_status(), extra=A)),
            ("status holding owned keys", {"kind": "status_artifact",
                                           "manifest_digest": A, "index_digest": B}),
            ("owned holding status key", {"kind": "owned_receipt", "status_sha256": A}),
            ("status missing digest", {"kind": "status_artifact"}),
            ("unknown kind", {"kind": "tree_digest", "status_sha256": A}),
            ("empty kind", {"kind": "", "status_sha256": A}),
            ("None kind", {"kind": None, "status_sha256": A}),
            ("int kind", {"kind": 1, "status_sha256": A}),
            ("list kind", {"kind": ["owned_receipt"], "status_sha256": A}),
            ("uppercase status", _status("A" * 64)),
            ("mixed case status", _status("a" * 63 + "A")),
            ("short status", _status("a" * 63)),
            ("long status", _status("a" * 65)),
            ("empty status", _status("")),
            ("non-hex status", _status("g" * 64)),
            ("trailing newline status", _status("a" * 64 + "\n")),
            ("leading space status", _status(" " + "a" * 63)),
            ("bool status", _status(True)),
            ("int status", _status(0)),
            ("bytes status", _status(b"a" * 64)),
            ("None status", _status(None)),
            ("uppercase manifest", _owned(manifest="A" * 64)),
            ("short index", _owned(index="b" * 10)),
            ("newline index", _owned(index="b" * 64 + "\n")),
            ("None manifest", _owned(manifest=None)),
            ("int index", _owned(index=7)),
            ("None", None),
            ("list", [_status()]),
            ("str", "status_artifact"),
            ("int", 3),
            ("tuple of items", tuple(_status().items())),
        ]
        for label, obj in rows:
            with self.subTest(label):
                self.assertEqual(cand.validate_candidate(obj), UNAVAILABLE)

    def test_dict_subclass_with_exact_shape_is_valid(self):
        class D(dict):
            pass

        self.assertEqual(cand.validate_candidate(D(_status())), (True, None))

    def test_validation_does_not_mutate(self):
        obj = _owned()
        before = copy.deepcopy(obj)
        cand.validate_candidate(obj)
        self.assertEqual(obj, before)


class EqualCompareTests(unittest.TestCase):
    def test_same_kind_same_keys_equal(self):
        for label, a, b in (
            ("owned", _owned(), _owned()),
            ("status", _status(), _status()),
            ("owned distinct objects", _owned(C, A), _owned(C, A)),
        ):
            with self.subTest(label):
                self.assertTrue(cand.candidate_equal(a, b))
                self.assertEqual(cand.candidate_compare(a, b), (True, None))

    def test_each_identity_key_differing_is_changed(self):
        rows = [
            ("owned manifest", _owned(A, B), _owned(C, B)),
            ("owned index", _owned(A, B), _owned(A, C)),
            ("owned both", _owned(A, B), _owned(B, A)),
            ("status sha", _status(A), _status(B)),
        ]
        for label, a, b in rows:
            with self.subTest(label):
                self.assertFalse(cand.candidate_equal(a, b))
                self.assertEqual(cand.candidate_compare(a, b), (False, "candidate_changed"))
                self.assertEqual(cand.candidate_compare(b, a), (False, "candidate_changed"))

    def test_mixed_kinds_are_a_kind_mismatch(self):
        for label, a, b in (
            ("owned vs status", _owned(A, A), _status(A)),
            ("status vs owned", _status(A), _owned(A, A)),
        ):
            with self.subTest(label):
                self.assertFalse(cand.candidate_equal(a, b))
                self.assertEqual(cand.candidate_compare(a, b), (False, "candidate_kind_mismatch"))

    def test_none_absent_and_malformed_are_never_equal(self):
        rows = [
            ("None/None", None, None),
            ("None vs valid", None, _status()),
            ("valid vs None", _status(), None),
            ("malformed vs valid", _status("A" * 64), _status()),
            ("valid vs malformed", _owned(), {"kind": "owned_receipt"}),
            ("malformed vs itself", _status("A" * 64), _status("A" * 64)),
            ("unknown kind both", {"kind": "x"}, {"kind": "x"}),
            ("empty dicts", {}, {}),
            ("None vs malformed", None, {"kind": "status_artifact"}),
        ]
        for label, a, b in rows:
            with self.subTest(label):
                self.assertFalse(cand.candidate_equal(a, b))
                self.assertEqual(cand.candidate_compare(a, b), UNAVAILABLE)

    def test_a_valid_candidate_equals_itself(self):
        obj = _owned()
        self.assertTrue(cand.candidate_equal(obj, obj))

    def test_arguments_are_not_mutated(self):
        a, b = _owned(A, B), _owned(C, B)
        before = (copy.deepcopy(a), copy.deepcopy(b))
        cand.candidate_compare(a, b)
        cand.candidate_equal(a, b)
        self.assertEqual((a, b), before)


class SelectCandidateTests(unittest.TestCase):
    def test_scouting_and_planning_use_the_status_artifact(self):
        for phase, roles in (("scouting", ("scout", "scout-reviewer")),
                             ("planning", ("planner", "planning-advisor"))):
            for role in roles:
                with self.subTest((phase, role)):
                    got = cand.select_candidate(phase, role, _pointer(None, exists=False), _fp(A))
                    self.assertEqual(got, (_status(A), None))

    def test_scouting_and_planning_missing_status_is_unavailable(self):
        fingerprints = [
            ("None", None),
            ("exists False", _fp(None, exists=False)),
            ("exists False with sha", _fp(A, exists=False)),
            ("missing sha", {"exists": True, "sha256": None}),
            ("no sha key", {"exists": True}),
            ("uppercase sha", _fp("A" * 64)),
            ("short sha", _fp("a" * 10)),
            ("newline sha", _fp("a" * 64 + "\n")),
            ("not a dict", "abc"),
            ("empty dict", {}),
        ]
        for phase, role in (("scouting", "scout"), ("scouting", "scout-reviewer"),
                            ("planning", "planner"), ("planning", "planning-advisor")):
            for label, fp in fingerprints:
                with self.subTest((phase, role, label)):
                    got = cand.select_candidate(phase, role, _pointer(None, exists=False), fp)
                    self.assertEqual(got, (None, "status_unavailable"))

    def test_scouting_and_planning_ignore_the_pointer_entirely(self):
        pointers = [
            ("valid receipt", _pointer(_receipt_data())),
            ("malformed", _pointer(None)),
            ("non-dict read", None),
            ("checkpoint shaped", _pointer({"checkpoint_id": "c"})),
        ]
        for phase, role in (("scouting", "scout"), ("planning", "planner")):
            for label, pointer in pointers:
                with self.subTest((phase, role, label)):
                    got = cand.select_candidate(phase, role, pointer, _fp(B))
                    self.assertEqual(got, (_status(B), None))

    def test_building_owned_receipt(self):
        for role in ("builder", "build-reviewer"):
            with self.subTest(role):
                got = cand.select_candidate("building", role, _pointer(_receipt_data()), None)
                self.assertEqual(got, (_owned(A, B), "owned_receipt"))

    def test_building_owned_receipt_does_not_consult_status(self):
        for label, fp in (("None", None), ("present", _fp(C)), ("bad", {"exists": True})):
            with self.subTest(label):
                got = cand.select_candidate("building", "builder", _pointer(_receipt_data()), fp)
                self.assertEqual(got, (_owned(A, B), "owned_receipt"))

    def test_building_owned_receipt_leaks_no_pointer_fields(self):
        data = _receipt_data(extra_field="x")
        candidate, _ = cand.select_candidate("building", "builder", _pointer(data), None)
        self.assertEqual(set(candidate), {"kind", "manifest_digest", "index_digest"})

    def test_building_transaction_id_wins_over_checkpoint_id(self):
        data = _receipt_data(checkpoint_id="cp-1")
        got = cand.select_candidate("building", "builder", _pointer(data), _fp(C))
        self.assertEqual(got, (_owned(A, B), "owned_receipt"))

    def test_building_pointer_absent_row(self):
        for label, pointer in (
            ("no data", _pointer(None, exists=False)),
            ("data ignored when exists is false", _pointer(_receipt_data(), exists=False)),
            ("exists missing", {"data": None}),
            ("empty read", {}),
        ):
            for role in ("builder", "build-reviewer"):
                with self.subTest((label, role)):
                    got = cand.select_candidate("building", role, pointer, _fp(C))
                    self.assertEqual(got, (_status(C), "pointer_absent"))

    def test_building_checkpoint_pointer_ignored_row(self):
        for label, data in (
            ("checkpoint only", {"checkpoint_id": "cp-1"}),
            ("checkpoint with other fields", {"checkpoint_id": "cp-1", "receipt_path": "/x"}),
            ("falsy transaction_id", {"transaction_id": "", "checkpoint_id": "cp-1"}),
            ("None transaction_id", {"transaction_id": None, "checkpoint_id": "cp-1"}),
        ):
            for role in ("builder", "build-reviewer"):
                with self.subTest((label, role)):
                    got = cand.select_candidate("building", role, _pointer(data), _fp(C))
                    self.assertEqual(got, (_status(C), "checkpoint_pointer_ignored"))

    def test_building_pointer_malformed_when_file_exists_but_unreadable(self):
        for label, data in (("None", None), ("list", [1]), ("str", "x"), ("int", 3)):
            with self.subTest(label):
                got = cand.select_candidate("building", "builder", _pointer(data), _fp(C))
                self.assertEqual(got, (None, "pointer_malformed"))

    def test_building_pointer_malformed_for_invalid_digests(self):
        rows = [
            ("both missing", {"transaction_id": "tx"}),
            ("manifest missing", {"transaction_id": "tx", "index_digest": B}),
            ("index missing", {"transaction_id": "tx", "manifest_digest": A}),
            ("uppercase manifest", _receipt_data(manifest_digest="A" * 64)),
            ("short index", _receipt_data(index_digest="b" * 63)),
            ("long manifest", _receipt_data(manifest_digest="a" * 65)),
            ("non-hex index", _receipt_data(index_digest="z" * 64)),
            ("newline index", _receipt_data(index_digest="b" * 64 + "\n")),
            ("None digests", _receipt_data(manifest_digest=None, index_digest=None)),
            ("bytes manifest", _receipt_data(manifest_digest=b"a" * 64)),
            ("invalid digests beat checkpoint_id", _receipt_data(index_digest="b", checkpoint_id="cp")),
        ]
        for label, data in rows:
            with self.subTest(label):
                got = cand.select_candidate("building", "builder", _pointer(data), _fp(C))
                self.assertEqual(got, (None, "pointer_malformed"))

    def test_building_pointer_malformed_when_neither_id(self):
        for label, data in (
            ("empty dict", {}),
            ("digests only", {"manifest_digest": A, "index_digest": B}),
            ("falsy ids", {"transaction_id": "", "checkpoint_id": ""}),
            ("None ids", {"transaction_id": None, "checkpoint_id": None}),
        ):
            with self.subTest(label):
                got = cand.select_candidate("building", "builder", _pointer(data), _fp(C))
                self.assertEqual(got, (None, "pointer_malformed"))

    def test_building_non_dict_pointer_read_is_malformed(self):
        for label, pointer in (("None", None), ("list", []), ("str", "x")):
            with self.subTest(label):
                got = cand.select_candidate("building", "builder", pointer, _fp(C))
                self.assertEqual(got, (None, "pointer_malformed"))

    def test_building_missing_status_on_status_rows_is_unavailable(self):
        rows = [
            ("pointer absent", _pointer(None, exists=False)),
            ("checkpoint pointer", _pointer({"checkpoint_id": "cp"})),
        ]
        for label, pointer in rows:
            for fp_label, fp in (("None", None), ("exists False", _fp(None, exists=False)),
                                 ("bad sha", _fp("A" * 64))):
                with self.subTest((label, fp_label)):
                    got = cand.select_candidate("building", "builder", pointer, fp)
                    self.assertEqual(got, (None, "status_unavailable"))

    def test_building_malformed_pointer_is_reported_before_missing_status(self):
        for label, pointer in (
            ("unreadable", _pointer(None)),
            ("bad digests", _pointer({"transaction_id": "tx"})),
            ("neither id", _pointer({})),
        ):
            with self.subTest(label):
                got = cand.select_candidate("building", "builder", pointer, None)
                self.assertEqual(got, (None, "pointer_malformed"))

    def test_malformed_pointer_never_becomes_a_status_candidate(self):
        for pointer in (_pointer(None), _pointer({}), _pointer({"transaction_id": "tx"})):
            candidate, reason = cand.select_candidate("building", "builder", pointer, _fp(C))
            self.assertIsNone(candidate)
            self.assertEqual(reason, "pointer_malformed")

    def test_unknown_phase_or_role_is_unavailable(self):
        rows = [
            ("unknown phase", "shipping", "builder"),
            ("None phase", None, "builder"),
            ("None role", "building", None),
            ("empty role", "building", ""),
            ("builder in planning", "planning", "builder"),
            ("planner in building", "building", "planner"),
            ("scout in planning", "planning", "scout"),
            ("reviewer in wrong phase", "scouting", "build-reviewer"),
            ("list phase", ["building"], "builder"),
            ("list role", "building", ["builder"]),
        ]
        for label, phase, role in rows:
            with self.subTest(label):
                got = cand.select_candidate(phase, role, _pointer(_receipt_data()), _fp(A))
                self.assertEqual(got, (None, "candidate_unavailable"))

    def test_results_never_alias_inputs(self):
        fp = _fp(A)
        candidate, _ = cand.select_candidate("scouting", "scout", None, fp)
        candidate["status_sha256"] = B
        self.assertEqual(fp["sha256"], A)

        data = _receipt_data()
        candidate, _ = cand.select_candidate("building", "builder", _pointer(data), None)
        candidate["manifest_digest"] = C
        self.assertEqual(data["manifest_digest"], A)

    def test_inputs_are_not_mutated(self):
        pointer, fp = _pointer(_receipt_data()), _fp(A)
        before = (copy.deepcopy(pointer), copy.deepcopy(fp))
        for phase, role in (("scouting", "scout"), ("building", "builder")):
            cand.select_candidate(phase, role, pointer, fp)
        self.assertEqual((pointer, fp), before)

    def test_selected_candidates_validate(self):
        got = [
            cand.select_candidate("scouting", "scout", None, _fp(A))[0],
            cand.select_candidate("building", "builder", _pointer(_receipt_data()), None)[0],
            cand.select_candidate("building", "builder", _pointer(None, exists=False), _fp(B))[0],
        ]
        for candidate in got:
            self.assertEqual(cand.validate_candidate(candidate), (True, None))


class RegistryTests(unittest.TestCase):
    def test_reason_codes_are_the_closed_registry(self):
        self.assertEqual(
            set(cand.REASON_CODES),
            {
                "candidate_unavailable",
                "candidate_kind_mismatch",
                "candidate_changed",
                "owned_receipt",
                "pointer_absent",
                "checkpoint_pointer_ignored",
                "pointer_malformed",
                "status_unavailable",
            },
        )
        self.assertEqual(len(cand.REASON_CODES), len(set(cand.REASON_CODES)))

    def test_candidate_kinds(self):
        self.assertEqual(set(cand.CANDIDATE_KINDS), {"owned_receipt", "status_artifact"})

    def test_every_returned_reason_is_a_member_or_none(self):
        reasons = set()
        candidates = (None, _owned(), _status(), _owned(A, C), _status(B), {"kind": "x"})
        for a in candidates:
            reasons.add(cand.validate_candidate(a)[1])
            for b in candidates:
                reasons.add(cand.candidate_compare(a, b)[1])
        pointers = (None, _pointer(None), _pointer(None, exists=False), _pointer({}),
                    _pointer({"checkpoint_id": "c"}), _pointer(_receipt_data()),
                    _pointer({"transaction_id": "t"}))
        fingerprints = (None, _fp(A), _fp(None, exists=False))
        phases = ("scouting", "planning", "building", "other")
        roles = ("scout", "scout-reviewer", "planner", "planning-advisor",
                 "builder", "build-reviewer", "other")
        for phase in phases:
            for role in roles:
                for pointer in pointers:
                    for fp in fingerprints:
                        reasons.add(cand.select_candidate(phase, role, pointer, fp)[1])
        self.assertTrue(reasons - {None} <= set(cand.REASON_CODES))
        self.assertIn(None, reasons)


class PurityTests(unittest.TestCase):
    ALLOWED_IMPORTS = {"re"}
    FORBIDDEN_NAMES = {"open", "os", "sys", "subprocess", "socket", "time", "datetime",
                       "pathlib", "shutil", "tempfile", "environ", "getenv"}

    @classmethod
    def setUpClass(cls):
        path = os.path.join(_HERE, "cowork_authority_candidate.py")
        with open(path, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)

    def test_imports_are_stdlib_allowlist_only(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= self.ALLOWED_IMPORTS, imported)
        self.assertFalse([name for name in imported if name.startswith("cowork")])

    def test_no_io_or_environment_names_are_used(self):
        used = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        used |= {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(used & self.FORBIDDEN_NAMES, used & self.FORBIDDEN_NAMES)

    def test_docstring_states_the_dispatch_manifest_digest_exclusion(self):
        doc = ast.get_docstring(self.tree) or ""
        flat = " ".join(doc.split())
        self.assertIn("dispatch-manifest digest", flat)
        self.assertIn("NEVER candidates", flat)


if __name__ == "__main__":
    unittest.main()
