#!/usr/bin/env python3
"""Tests for the authority chain store in `cowork_authority_chain`.

Every fixture is synthetic: digests are short repeated characters, candidates
are hand-built dicts and chains live in a temporary directory outside the
repository. Rows are table-driven and each refusal row names its own reason.
Faults are injected by patching `os.fsync`, `os.replace` and the module's
`_readback` seam inside a `with` scope, so no patch outlives its test.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_authority_core_store
"""

import ast
import copy
import errno
import fcntl
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_authority_candidate as p1a  # noqa: E402
import cowork_authority_chain as chain  # noqa: E402

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64

SESSION = "session-one"
SEAT = "build-reviewer"

C1 = {"kind": "owned_receipt", "manifest_digest": A, "index_digest": B}
C2 = {"kind": "owned_receipt", "manifest_digest": C, "index_digest": D}
S1 = {"kind": "status_artifact", "status_sha256": A}

_REAL_FSYNC = os.fsync
_REAL_REPLACE = os.replace
_REAL_READBACK = chain._readback

_EVENT_ROUND_KINDS = ("finding", "recommendation", "resolution", "retraction", "reopen")
_NULL_BY_DEFAULT = ("amendment", "escalation_resume")

_FIELDS = {
    "round": {
        "seat": SEAT,
        "verdict_sha256": A,
        "review_path": "review.md",
        "verdict_copy_path": "verdict.md",
        "verdict_copy_sha256": B,
    },
    "finding": {
        "round_id": "RD-0001",
        "summary": "a summary",
        "severity": "blocking",
        "blocking": True,
        "criterion": "a criterion",
        "evidence_path": "evidence.txt",
        "evidence_sha256": A,
        "claim_class": None,
        "discoverer": "reviewer",
        "superseded_by_transaction": None,
    },
    "proposal": {
        "finding_ids": ["AF-0002"],
        "changed_evidence": {"paths": ["a.py"], "sha256s": [A], "candidate": C2},
        "author_role": "builder",
        "requires": [],
    },
    "recommendation": {
        "finding_id": "AF-0002",
        "recommend": "closed",
        "reviewer_seat": SEAT,
        "round_id": "RD-0001",
    },
    "resolution": {
        "finding_id": "AF-0002",
        "outcome": "closed",
        "decided_by": "control_plane",
        "basis": {
            "proposal_id": "AP-0003",
            "recommendation_id": "AC-0004",
            "decision_id": None,
            "witness_ids": [],
        },
    },
    "retraction": {
        "finding_id": "AF-0002",
        "reason": "withdrawn",
        "duplicate_of": None,
        "by": "reviewer",
    },
    "reopen": {"finding_id": "AF-0002", "round_id": "RD-0001", "by": "reviewer"},
    "decision": {
        "request_id": "req-1",
        "request_kind": "reviewer_question",
        "request_role": SEAT,
        "principal": "supervisor_agent",
        "answer_sha256": C,
        "adjudicates": [],
        "null_candidate_basis": None,
    },
    "amendment": {
        "context_revision": 3,
        "context_sha256": A,
        "principal": "policy_principal",
        "applies_to_phase": "building",
        "supersedes": None,
        "grants": [],
    },
    "escalation": {
        "required_capability": "scope_expansion",
        "target_principal": "policy_principal",
        "proposal_id": "AP-0003",
        "request_id": "req-2",
        "resume_token_sha256": B,
    },
    "escalation_resume": {
        "escalation_id": "AE-0001",
        "request_id": "req-2",
        "principal": "policy_principal",
        "answer_sha256": C,
        "outcome": "grant",
        "grants": ["scope_expansion"],
    },
    "witness": {
        "claim_id": "claim-1",
        "claim_class": "only",
        "probe": {"kind": "grep", "ref": "needle", "author_seat": "scout"},
        "expected_witness": "no bypass",
        "result": {
            "outcome": "bypass_not_found",
            "evidence_path": "probe.txt",
            "evidence_sha256": A,
        },
        "not_executable_reason": None,
    },
    "reconciliation": {
        "role": "builder",
        "work_id": "work-1",
        "artifact_path": "status.json",
        "old_sha256": A,
        "new_sha256": B,
        "original_decision": "revise",
        "reconciled_outcome": "approve",
        "activity_ref": None,
    },
}


def _body(base_kind, **over):
    """A valid caller body for `base_kind`, with top-level overrides."""
    body = {
        "kind": base_kind,
        "session_uuid": SESSION,
        "phase": "building",
        "round": 1 if base_kind in _EVENT_ROUND_KINDS else None,
        "candidate": None if base_kind in _NULL_BY_DEFAULT else copy.deepcopy(C1),
    }
    body.update(copy.deepcopy(_FIELDS[base_kind]))
    body.update(copy.deepcopy(over))
    return body


def _decision_close(finding_id="AF-0002", candidate=C1, **over):
    return _body(
        "decision",
        candidate=candidate,
        adjudicates=[{"finding_id": finding_id, "outcome": "close"}],
        **over
    )


def _null_decision(**over):
    return _body(
        "decision", candidate=None, null_candidate_basis="status_absent", **over
    )


def _basis(proposal=None, recommendation=None, decision=None, witnesses=()):
    return {
        "proposal_id": proposal,
        "recommendation_id": recommendation,
        "decision_id": decision,
        "witness_ids": list(witnesses),
    }


def _standard_batch():
    """round RD-0001, finding AF-0002, proposal AP-0003, recommendation AC-0004."""
    return [
        _body("round"),
        _body("finding"),
        _body("proposal"),
        _body("recommendation"),
    ]


def _stamp_all(bodies, overrides=None):
    """Stored records for `bodies` with digests recomputed, built without the store."""
    overrides = overrides or {}
    records = []
    prev = ""
    rounds = {}
    for index, body in enumerate(bodies):
        rec = copy.deepcopy(body)
        rec["schema"] = 1
        rec["seq"] = index + 1
        rec["id"] = chain.record_id(rec["kind"], rec["seq"])
        rec["prev_digest"] = prev
        if rec["kind"] == "round":
            key = (rec["phase"], rec["seat"])
            rounds[key] = rounds.get(key, 0) + 1
            rec["round"] = rounds[key]
        rec.update(copy.deepcopy(overrides.get(index, {})))
        rec["digest"] = chain.record_digest(rec)
        prev = rec["digest"]
        records.append(rec)
    return records


def _lines(records):
    return "".join(chain.canonical_json(rec) + "\n" for rec in records).encode("ascii")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="authority-chain-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "authority_chain.jsonl")

    def raw(self):
        try:
            with open(self.path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def write_raw(self, data):
        with open(self.path, "wb") as handle:
            handle.write(data)

    def leftovers(self):
        return sorted(name for name in os.listdir(self.dir) if ".tmp-" in name)

    def seed(self):
        return chain.append_batch(self.path, _standard_batch())

    def head(self):
        return chain.read_chain(self.path)["head"]

    def count(self):
        return len(chain.read_chain(self.path)["records"])

    def append(self, body, **kw):
        return chain.append(self.path, body, **kw)

    def assertValid(self, body, committed=False):
        self.assertEqual(chain.validate_record(body, committed), (True, None), body)

    def assertInvalid(self, body, reason, committed=False):
        self.assertEqual(
            chain.validate_record(body, committed), (False, reason), body
        )

    def assertRefused(self, reason, fn, *args, **kwargs):
        with self.assertRaises(chain.AuthorityRecordRefused) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.reason, reason, str(ctx.exception))

    def assertAppendRefused(self, reason, body, **kw):
        before = self.raw()
        self.assertRefused(reason, chain.append, self.path, body, **kw)
        self.assertEqual(self.raw(), before)

    def assertReadCode(self, code):
        result = chain.read_chain(self.path)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], code)
        self.assertEqual(result["records"], [])
        self.assertIsNone(result["head"])
        self.assertIsNone(result["folded"])


# --------------------------------------------------------------------- schema


class RecordSchemaTests(unittest.TestCase):
    def assertValid(self, body, committed=False):
        self.assertEqual(chain.validate_record(body, committed), (True, None), body)

    def assertInvalid(self, body, reason, committed=False):
        self.assertEqual(
            chain.validate_record(body, committed), (False, reason), body
        )

    def test_valid_minimal_body_for_every_kind(self):
        self.assertEqual(set(chain.RECORD_KINDS), set(_FIELDS))
        for kind in chain.RECORD_KINDS:
            with self.subTest(kind):
                self.assertValid(_body(kind))

    def test_every_required_key_missing_is_refused(self):
        optional = {"op_key", "grants"}
        for kind in chain.RECORD_KINDS:
            without_kind = _body(kind)
            del without_kind["kind"]
            self.assertInvalid(without_kind, "unknown_kind")
            for key in _body(kind):
                if key in optional or key == "kind":
                    continue
                with self.subTest("%s/%s" % (kind, key)):
                    body = _body(kind)
                    del body[key]
                    self.assertInvalid(body, "missing_key")

    def test_unknown_key_refused_and_op_key_allowed_on_every_kind(self):
        for kind in chain.RECORD_KINDS:
            with self.subTest(kind):
                self.assertInvalid(_body(kind, extra="x"), "unknown_key")
                self.assertValid(_body(kind, op_key="k-1"))

    def test_optional_grants_only_on_amendment(self):
        body = _body("amendment")
        del body["grants"]
        self.assertValid(body)
        self.assertInvalid(_body("escalation", grants=[]), "unknown_key")

    def test_unknown_kind_and_non_object(self):
        for label, body in (
            ("mirror", _body("round", kind="mirror")),
            ("non-string kind", _body("round", kind=3)),
            ("empty kind", _body("round", kind="")),
        ):
            with self.subTest(label):
                self.assertInvalid(body, "unknown_kind")
        for label, value in (("list", []), ("none", None), ("string", "round")):
            with self.subTest(label):
                self.assertInvalid(value, "not_an_object")

    def test_non_string_keys_refused(self):
        body = _body("round")
        body[3] = "x"
        self.assertInvalid(body, "bad_value")

    def test_committed_view_requires_and_checks_store_keys(self):
        stored = _stamp_all([_body("round")])[0]
        self.assertValid(stored, committed=True)
        self.assertInvalid(stored, "unknown_key")
        for key in ("schema", "seq", "id", "prev_digest", "digest"):
            with self.subTest(key):
                broken = dict(stored)
                del broken[key]
                self.assertInvalid(broken, "missing_key", committed=True)
        for value in (2, True, "1", 1.0):
            with self.subTest(schema=value):
                self.assertInvalid(dict(stored, schema=value), "wrong_schema", True)
        self.assertInvalid(dict(stored, seq=0), "bad_value", True)
        self.assertInvalid(dict(stored, seq=True), "bad_value", True)
        self.assertInvalid(dict(stored, id="RD-0009"), "bad_id", True)
        self.assertInvalid(dict(stored, prev_digest="xyz"), "bad_value", True)
        self.assertInvalid(dict(stored, digest="XYZ"), "bad_value", True)
        self.assertInvalid(dict(stored, round=None), "bad_round", True)

    def test_round_record_number_is_minted_by_the_store(self):
        self.assertInvalid(_body("round", round=1), "bad_round")

    def test_common_field_type_rows(self):
        rows = [
            ("phase none", _body("round", phase=None), "bad_value"),
            ("phase unknown", _body("round", phase="verifying"), "bad_value"),
            ("session empty", _body("round", session_uuid=""), "bad_value"),
            ("session int", _body("round", session_uuid=7), "bad_value"),
            ("round bool", _body("finding", round=True), "bad_round"),
            ("round zero", _body("finding", round=0), "bad_round"),
            ("round float", _body("finding", round=1.0), "bad_round"),
            ("round string", _body("finding", round="1"), "bad_round"),
            ("op_key empty", _body("round", op_key=""), "bad_value"),
            ("op_key int", _body("round", op_key=1), "bad_value"),
        ]
        for label, body, reason in rows:
            with self.subTest(label):
                self.assertInvalid(body, reason)

    def test_claim_class_accepted_and_refused(self):
        for kind in ("finding", "witness"):
            for value in (None,) + chain.CLAIM_CLASSES:
                with self.subTest("%s ok %r" % (kind, value)):
                    self.assertValid(_body(kind, claim_class=value))
            for value in ("other", "", 1, ["only"]):
                with self.subTest("%s refused %r" % (kind, value)):
                    self.assertInvalid(_body(kind, claim_class=value), "bad_value")

    def test_witness_outcome_and_null_result_rules(self):
        for outcome in chain.WITNESS_OUTCOMES:
            body = _body("witness")
            body["result"]["outcome"] = outcome
            self.assertValid(body)
        body = _body("witness")
        body["result"]["outcome"] = "bypass_maybe"
        self.assertInvalid(body, "bad_value")
        self.assertValid(
            _body("witness", result=None, not_executable_reason="no probe possible")
        )
        self.assertInvalid(_body("witness", result=None), "bad_value")
        self.assertInvalid(
            _body("witness", not_executable_reason="but a result exists"), "bad_value"
        )
        self.assertInvalid(
            _body("witness", result=None, not_executable_reason=""), "bad_value"
        )

    def test_principal_enums_per_field(self):
        rows = [
            ("decision", "principal", ("supervisor_agent", "policy_principal")),
            ("retraction", "by", ("reviewer", "supervisor_agent", "policy_principal")),
            ("amendment", "principal", chain.PRINCIPALS),
            ("escalation_resume", "principal", chain.PRINCIPALS),
        ]
        for kind, key, allowed in rows:
            for value in allowed:
                with self.subTest("%s.%s ok %s" % (kind, key, value)):
                    self.assertValid(_body(kind, **{key: value}))
            for value in set(chain.PRINCIPALS + ("lead", "", None)) - set(allowed):
                with self.subTest("%s.%s refused %s" % (kind, key, value)):
                    self.assertInvalid(_body(kind, **{key: value}), "bad_value")
        self.assertInvalid(_body("reopen", by="supervisor_agent"), "bad_value")
        self.assertInvalid(
            _body("escalation", target_principal="reviewer"), "bad_value"
        )

    def test_capability_tokens(self):
        rows = [
            ("proposal", "requires"),
            ("amendment", "grants"),
            ("escalation_resume", "grants"),
        ]
        for kind, key in rows:
            for token in chain.CAPABILITIES:
                with self.subTest("%s ok %s" % (kind, token)):
                    self.assertValid(_body(kind, **{key: [token]}))
            with self.subTest("%s refused" % kind):
                self.assertInvalid(_body(kind, **{key: ["root"]}), "bad_value")
                self.assertInvalid(_body(kind, **{key: "scope_expansion"}), "bad_value")
        for token in chain.CAPABILITIES:
            self.assertValid(_body("escalation", required_capability=token))
        self.assertInvalid(_body("escalation", required_capability="root"), "bad_value")
        self.assertInvalid(
            _body("escalation_resume", outcome="decline"), "bad_value"
        )
        self.assertValid(
            _body("escalation_resume", outcome="decline", grants=[])
        )

    def test_closed_registries_are_exact(self):
        self.assertEqual(
            set(chain.RECORD_KINDS),
            {
                "round", "finding", "proposal", "recommendation", "resolution",
                "retraction", "reopen", "decision", "amendment", "escalation",
                "escalation_resume", "witness", "reconciliation",
            },
        )
        self.assertEqual(
            chain.ID_PREFIXES,
            {
                "round": "RD", "finding": "AF", "proposal": "AP",
                "recommendation": "AC", "resolution": "AR", "retraction": "AT",
                "reopen": "AO", "decision": "AD", "amendment": "AA",
                "escalation": "AE", "escalation_resume": "AS", "witness": "AV",
                "reconciliation": "AL",
            },
        )
        self.assertEqual(chain.PHASES, ("scouting", "planning", "building"))
        self.assertEqual(
            set(chain.PRINCIPALS),
            {"reviewer", "supervisor_agent", "policy_principal", "runtime"},
        )
        self.assertEqual(set(chain.CAPABILITIES), {"scope_expansion", "policy_exception"})
        self.assertEqual(
            set(chain.CLAIM_CLASSES), {"source_of_truth", "only", "never", "fail_closed"}
        )
        self.assertEqual(set(chain.WITNESS_OUTCOMES), {"bypass_not_found", "bypass_found"})
        self.assertEqual(
            set(chain.READ_ERROR_CODES),
            {
                "unreadable", "torn_line", "digest_break", "seq_gap", "unknown_kind",
                "unknown_key", "wrong_schema", "wrong_session_uuid", "duplicate_id",
                "illegal_record", "illegal_transition",
            },
        )
        self.assertEqual(
            set(chain.REFUSAL_REASONS),
            {
                "not_an_object", "unknown_kind", "missing_key", "unknown_key",
                "wrong_schema", "bad_value", "bad_candidate",
                "bad_null_candidate_basis", "bad_id", "bad_round", "round_mismatch",
                "unknown_reference", "illegal_transition", "illegal_resolution_basis",
                "null_candidate_decision_basis", "wrong_session_uuid",
                "candidate_mismatch_in_batch", "op_key_conflict", "op_key_partial",
                "duplicate_op_key", "empty_batch", "bad_expected_head",
            },
        )
        self.assertEqual(chain.EMPTY_HEAD, "")
        self.assertNotIn("mirror", chain.RECORD_KINDS)

    def test_nested_object_rows(self):
        def pop(*path):
            def run(body):
                target = body
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]

            return run

        def put(*path, value):
            def run(body):
                target = body
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value

            return run

        rows = [
            ("proposal", pop("changed_evidence", "paths"), "missing_key"),
            ("proposal", pop("changed_evidence", "sha256s"), "missing_key"),
            ("proposal", pop("changed_evidence", "candidate"), "missing_key"),
            ("proposal", put("changed_evidence", "extra", value=1), "unknown_key"),
            ("proposal", put("changed_evidence", value=[]), "bad_value"),
            ("proposal", put("changed_evidence", "paths", value="a.py"), "bad_value"),
            ("proposal", put("changed_evidence", "paths", value=[""]), "bad_value"),
            ("proposal", put("changed_evidence", "sha256s", value=["xyz"]), "bad_value"),
            ("proposal", put("changed_evidence", "candidate", value=None), "bad_candidate"),
            ("proposal", put("changed_evidence", "candidate", value={}), "bad_candidate"),
            ("proposal", put("finding_ids", value=[]), "bad_value"),
            ("proposal", put("finding_ids", value=[""]), "bad_value"),
            ("proposal", put("author_role", value="reviewer"), "bad_value"),
            ("resolution", pop("basis", "proposal_id"), "missing_key"),
            ("resolution", pop("basis", "witness_ids"), "missing_key"),
            ("resolution", put("basis", "extra", value=None), "unknown_key"),
            ("resolution", put("basis", value=None), "bad_value"),
            ("resolution", put("basis", "witness_ids", value=None), "bad_value"),
            ("resolution", put("basis", "proposal_id", value=""), "bad_value"),
            ("resolution", put("outcome", value="open"), "bad_value"),
            ("resolution", put("decided_by", value="reviewer"), "bad_value"),
            ("decision", put("adjudicates", value=[{"finding_id": "AF-0002"}]), "missing_key"),
            (
                "decision",
                put("adjudicates", value=[{"finding_id": "AF-0002", "outcome": "close", "x": 1}]),
                "unknown_key",
            ),
            (
                "decision",
                put("adjudicates", value=[{"finding_id": "AF-0002", "outcome": "reject"}]),
                "bad_value",
            ),
            ("decision", put("adjudicates", value={}), "bad_value"),
            ("decision", put("request_kind", value=""), "bad_value"),
            ("decision", put("request_role", value=None), "bad_value"),
            ("decision", put("answer_sha256", value="abc"), "bad_value"),
            ("witness", pop("probe", "ref"), "missing_key"),
            ("witness", put("probe", "extra", value=1), "unknown_key"),
            ("witness", put("probe", "author_seat", value=""), "bad_value"),
            ("witness", pop("result", "evidence_path"), "missing_key"),
            ("witness", put("result", "extra", value=1), "unknown_key"),
            ("witness", put("result", "evidence_sha256", value="zz"), "bad_value"),
            ("witness", put("expected_witness", value=1.5), "bad_value"),
            ("witness", put("expected_witness", value={"a": 1.5}), "bad_value"),
            ("witness", put("expected_witness", value={1: "a"}), "bad_value"),
            ("witness", put("expected_witness", value=""), "bad_value"),
            ("witness", put("expected_witness", value=["x"]), "bad_value"),
            ("amendment", put("context_revision", value=-1), "bad_value"),
            ("amendment", put("context_revision", value=True), "bad_value"),
            ("amendment", put("context_revision", value=1.0), "bad_value"),
            ("amendment", put("applies_to_phase", value="release"), "bad_value"),
            ("amendment", put("supersedes", value=""), "bad_value"),
            ("amendment", put("context_sha256", value="short"), "bad_value"),
            ("finding", put("evidence_sha256", value=A.upper()), "bad_value"),
            ("finding", put("discoverer", value=""), "bad_value"),
            ("finding", put("summary", value=None), "bad_value"),
            ("finding", put("severity", value=""), "bad_value"),
            ("finding", put("superseded_by_transaction", value=""), "bad_value"),
            ("round", put("verdict_sha256", value="nothex"), "bad_value"),
            ("round", put("seat", value=""), "bad_value"),
            ("recommendation", put("recommend", value="maybe"), "bad_value"),
            ("recommendation", put("reviewer_seat", value=""), "bad_value"),
            ("reconciliation", put("activity_ref", value=""), "bad_value"),
            ("reconciliation", put("old_sha256", value=1), "bad_value"),
            ("escalation", put("resume_token_sha256", value="x"), "bad_value"),
            ("escalation_resume", put("outcome", value="defer"), "bad_value"),
        ]
        for index, (kind, mutate, reason) in enumerate(rows):
            with self.subTest("%d %s" % (index, kind)):
                body = _body(kind)
                mutate(body)
                self.assertInvalid(body, reason)
        self.assertValid(_body("witness", expected_witness={"nested": [1, "a", None, {"k": True}]}))
        self.assertValid(_body("proposal", changed_evidence={"paths": [], "sha256s": [], "candidate": S1}))

    def test_finding_blocking_equals_severity_blocking_and_not_superseded(self):
        rows = [
            ("blocking and live and true", "blocking", True, None, True),
            ("blocking and live and false", "blocking", False, None, False),
            ("superseded and true", "blocking", True, "tx-1", False),
            ("superseded and false", "blocking", False, "tx-1", True),
            ("non-blocking and true", "advisory", True, None, False),
            ("non-blocking and false", "advisory", False, None, True),
        ]
        for label, severity, blocking, superseded, accepted in rows:
            with self.subTest(label):
                body = _body(
                    "finding",
                    severity=severity,
                    blocking=blocking,
                    superseded_by_transaction=superseded,
                )
                if accepted:
                    self.assertValid(body)
                else:
                    self.assertInvalid(body, "bad_value")

    def test_retraction_duplicate_pairing(self):
        self.assertValid(_body("retraction", reason="duplicate", duplicate_of="AF-0009"))
        self.assertInvalid(_body("retraction", reason="duplicate"), "bad_value")
        self.assertInvalid(
            _body("retraction", reason="withdrawn", duplicate_of="AF-0009"), "bad_value"
        )

    def test_floats_and_non_json_values_refused(self):
        self.assertInvalid(_body("finding", summary=1.5), "bad_value")
        self.assertInvalid(_body("amendment", context_revision=2.5), "bad_value")
        self.assertInvalid(
            _body("witness", expected_witness={"k": object()}), "bad_value"
        )


class ErrorHierarchyTests(StoreCase):
    def test_issubclass_relationships_are_pinned(self):
        E = chain
        self.assertTrue(issubclass(E.AuthorityError, Exception))
        for cls in (
            E.AuthorityWriteError,
            E.AuthorityReadError,
            E.AuthorityCommitUncertain,
            E.AuthorityRecordRefused,
            E.AuthorityHeadConflict,
        ):
            self.assertTrue(issubclass(cls, E.AuthorityError), cls)
        self.assertTrue(issubclass(E.AuthorityReadError, E.AuthorityWriteError))
        self.assertTrue(issubclass(E.AuthorityCommitUncertain, E.AuthorityWriteError))
        self.assertFalse(issubclass(E.AuthorityRecordRefused, E.AuthorityWriteError))
        self.assertFalse(issubclass(E.AuthorityHeadConflict, E.AuthorityWriteError))
        self.assertFalse(issubclass(E.AuthorityReadError, E.AuthorityCommitUncertain))
        self.assertFalse(issubclass(E.AuthorityCommitUncertain, E.AuthorityReadError))
        self.assertFalse(issubclass(E.AuthorityRecordRefused, E.AuthorityHeadConflict))

    def test_corrupt_chain_at_append_is_caught_as_write_error(self):
        self.write_raw(b"not a chain\n")
        try:
            self.append(_body("round"))
        except chain.AuthorityWriteError as err:
            self.assertIsInstance(err, chain.AuthorityReadError)
            self.assertEqual(err.code, "torn_line")
        else:
            self.fail("append on a corrupt chain returned")
        self.assertEqual(self.raw(), b"not a chain\n")

    def test_commit_uncertain_is_caught_as_write_error_and_distinguishable(self):
        self.seed()
        err = _uncertain_append(self, _body("round"))
        self.assertIsInstance(err, chain.AuthorityWriteError)
        self.assertIsInstance(err, chain.AuthorityCommitUncertain)
        self.assertNotIsInstance(err, chain.AuthorityReadError)


class IdsAndDigestTests(StoreCase):
    def test_record_id_prefix_table(self):
        for kind, prefix in chain.ID_PREFIXES.items():
            self.assertEqual(chain.record_id(kind, 7), "%s-0007" % prefix)
        self.assertEqual(chain.record_id("round", 12345), "RD-12345")

    def test_digest_matches_documented_rule(self):
        import hashlib

        committed = self.append(_body("round"))
        body = {k: v for k, v in committed.items() if k != "digest"}
        text = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        self.assertEqual(committed["digest"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(chain.record_digest(committed), committed["digest"])

    def test_stored_lines_are_canonical_json_and_newline(self):
        committed = self.seed()
        expected = b"".join(
            (chain.canonical_json(rec) + "\n").encode("ascii") for rec in committed
        )
        self.assertEqual(self.raw(), expected)

    def test_prev_digest_linkage_and_head(self):
        committed = self.seed()
        self.assertEqual(committed[0]["prev_digest"], "")
        for earlier, later in zip(committed, committed[1:]):
            self.assertEqual(later["prev_digest"], earlier["digest"])
        self.assertEqual(self.head(), committed[-1]["digest"])
        self.assertEqual([r["seq"] for r in committed], [1, 2, 3, 4])
        self.assertEqual(
            [r["id"] for r in committed], ["RD-0001", "AF-0002", "AP-0003", "AC-0004"]
        )

    def test_canonical_json_is_sorted_compact_ascii(self):
        self.assertEqual(
            chain.canonical_json({"b": 1, "a": "é"}), '{"a":"\\u00e9","b":1}'
        )
        with self.assertRaises(ValueError):
            chain.canonical_json({"a": float("nan")})


# ------------------------------------------------------------ candidate rules


class CandidateRuleTests(StoreCase):
    def test_every_kind_accepts_valid_candidate(self):
        for kind in chain.RECORD_KINDS:
            for label, cand in (("owned", C1), ("status", S1)):
                with self.subTest("%s %s" % (kind, label)):
                    self.assertValid(_body(kind, candidate=cand))

    def test_absent_candidate_key_refused_every_kind(self):
        for kind in chain.RECORD_KINDS:
            with self.subTest(kind):
                body = _body(kind)
                del body["candidate"]
                self.assertInvalid(body, "missing_key")

    def test_null_refused_on_kinds_that_do_not_allow_it(self):
        for kind in chain.RECORD_KINDS:
            if kind in ("amendment", "escalation_resume", "decision"):
                continue
            with self.subTest(kind):
                self.assertInvalid(_body(kind, candidate=None), "bad_candidate")
        self.assertInvalid(_body("decision", candidate=None), "bad_null_candidate_basis")

    def test_null_accepted_on_amendment_and_escalation_resume(self):
        self.assertValid(_body("amendment", candidate=None))
        self.assertValid(_body("escalation_resume", candidate=None))
        self.assertValid(_null_decision())

    def test_wrong_kind_or_shape_refused(self):
        rows = [
            ("empty dict", {}),
            ("string", "owned_receipt"),
            ("list", [C1]),
            ("unknown kind", {"kind": "other", "status_sha256": A}),
            ("owned missing index", {"kind": "owned_receipt", "manifest_digest": A}),
            ("owned extra key", dict(C1, extra=A)),
            ("owned with status key", dict(C1, status_sha256=A)),
            ("status extra key", dict(S1, index_digest=A)),
            ("uppercase hex", {"kind": "status_artifact", "status_sha256": A.upper()}),
            ("short hex", {"kind": "status_artifact", "status_sha256": "ab"}),
            ("non-str digest", {"kind": "status_artifact", "status_sha256": 5}),
        ]
        for kind in chain.RECORD_KINDS:
            for label, cand in rows:
                with self.subTest("%s %s" % (kind, label)):
                    body = _body(kind, candidate=cand)
                    expected = "bad_candidate"
                    self.assertInvalid(body, expected)

    def test_proposal_changed_evidence_candidate_is_required(self):
        body = _body("proposal")
        body["changed_evidence"]["candidate"] = None
        self.assertInvalid(body, "bad_candidate")


class DecisionMatrixTests(StoreCase):
    def test_status_absent_null_decision_accepted_and_folds_null(self):
        self.seed()
        committed = self.append(_null_decision())
        self.assertIsNone(committed["candidate"])
        folded = chain.read_chain(self.path)["folded"]
        row = folded["decisions"][committed["id"]]
        self.assertIsNone(row["candidate"])
        self.assertEqual(row["adjudicates"], [])

    def test_null_candidate_basis_null_or_missing_refused(self):
        self.assertInvalid(_body("decision", candidate=None), "bad_null_candidate_basis")
        body = _null_decision()
        del body["null_candidate_basis"]
        self.assertInvalid(body, "missing_key")

    def test_unknown_basis_literal_refused_both_candidate_states(self):
        for candidate in (None, C1):
            for literal in ("unknown", "", 1, True, ["status_absent"]):
                with self.subTest("%r %r" % (candidate, literal)):
                    self.assertInvalid(
                        _body("decision", candidate=candidate, null_candidate_basis=literal),
                        "bad_null_candidate_basis",
                    )

    def test_null_candidate_with_adjudicates_refused(self):
        for outcome in ("close", "uphold"):
            body = _null_decision(
                adjudicates=[{"finding_id": "AF-0002", "outcome": outcome}]
            )
            with self.subTest(outcome):
                self.assertInvalid(body, "bad_null_candidate_basis")

    def test_non_null_candidate_with_status_absent_refused(self):
        self.assertInvalid(
            _body("decision", candidate=C1, null_candidate_basis="status_absent"),
            "bad_null_candidate_basis",
        )

    def test_invalid_non_null_candidate_on_decision_refused(self):
        self.assertInvalid(_body("decision", candidate={"kind": "x"}), "bad_candidate")

    def test_basis_key_on_every_non_decision_kind_refused(self):
        for kind in chain.RECORD_KINDS:
            if kind == "decision":
                continue
            with self.subTest(kind):
                self.assertInvalid(
                    _body(kind, null_candidate_basis=None), "unknown_key"
                )

    def test_missing_request_role_refused(self):
        body = _body("decision")
        del body["request_role"]
        self.assertInvalid(body, "missing_key")

    def test_non_null_decision_with_close_adjudication_accepted(self):
        self.assertValid(_decision_close())


# --------------------------------------------------------------------- append


class AppendTests(StoreCase):
    def test_returns_committed_record_after_strict_reread(self):
        committed = self.append(_body("round"))
        stored = chain.read_chain(self.path)["records"]
        self.assertEqual(stored, [committed])
        self.assertEqual(committed["seq"], 1)
        self.assertEqual(committed["id"], "RD-0001")
        self.assertEqual(committed["round"], 1)
        self.assertEqual(committed["schema"], 1)
        self.assertEqual(committed["prev_digest"], "")

    def test_returned_dict_is_fresh_and_input_not_mutated(self):
        body = _body("round")
        snapshot = copy.deepcopy(body)
        committed = self.append(body)
        self.assertEqual(body, snapshot)
        committed["seat"] = "tampered"
        committed["candidate"]["kind"] = "tampered"
        self.assertEqual(chain.read_chain(self.path)["records"][0]["seat"], SEAT)
        self.assertEqual(chain.read_chain(self.path)["records"][0]["candidate"], C1)

    def test_every_failure_raises_never_none(self):
        self.write_raw(b"garbage\n")
        for label, call in (
            ("corrupt chain", lambda: self.append(_body("round"))),
            ("bad head", lambda: chain.append(self.path, _body("round"), expected_head=3)),
            ("not an object", lambda: chain.append(self.path, "round")),
            ("empty batch", lambda: chain.append_batch(self.path, [])),
        ):
            with self.subTest(label):
                with self.assertRaises(chain.AuthorityError):
                    call()
        os.unlink(self.path)
        with self.assertRaises(chain.AuthorityRecordRefused):
            self.append(_body("round", phase="nope"))

    def test_round_minting_per_phase_and_seat(self):
        rows = [
            (_body("round"), 1),
            (_body("round", seat="other-seat"), 1),
            (_body("round"), 2),
            (_body("round", phase="planning"), 1),
            (_body("round", phase="planning"), 2),
            (_body("round", seat="other-seat"), 2),
        ]
        for body, expected in rows:
            committed = self.append(body)
            self.assertEqual(committed["round"], expected, body)
        self.assertEqual(self.count(), 6)

    def test_session_mismatch_refused(self):
        self.append(_body("round"))
        self.assertAppendRefused("wrong_session_uuid", _body("round", session_uuid="other"))

    def test_corrupt_existing_chain_raises_read_error_and_leaves_bytes(self):
        self.seed()
        raw = self.raw()
        self.write_raw(raw[:-5])
        with self.assertRaises(chain.AuthorityReadError) as ctx:
            self.append(_body("round"))
        self.assertEqual(ctx.exception.code, "torn_line")
        self.assertEqual(self.raw(), raw[:-5])

    def test_directory_at_chain_path_raises_read_error(self):
        os.mkdir(self.path)
        with self.assertRaises(chain.AuthorityReadError) as ctx:
            self.append(_body("round"))
        self.assertEqual(ctx.exception.code, "unreadable")

    def test_missing_parent_directory_raises_write_error(self):
        path = os.path.join(self.dir, "missing", "chain.jsonl")
        with self.assertRaises(chain.AuthorityWriteError):
            chain.append(path, _body("round"))
        self.assertFalse(os.path.exists(os.path.dirname(path)))

    def test_lock_timeout_raises_write_error(self):
        fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(chain, "LOCK_TIMEOUT_S", 0.2), mock.patch.object(
            chain, "LOCK_POLL_S", 0.01
        ):
            started = time.monotonic()
            with self.assertRaises(chain.AuthorityWriteError):
                self.append(_body("round"))
            self.assertLess(time.monotonic() - started, 10)
        self.assertIsNone(self.raw())

    def test_only_the_lock_file_remains_beside_the_chain(self):
        self.seed()
        self.assertEqual(
            sorted(os.listdir(self.dir)),
            ["authority_chain.jsonl", "authority_chain.jsonl.lock"],
        )

    def test_kind_fields_round_trip(self):
        self.seed()
        for kind, body in (
            ("decision", _decision_close()),
            ("amendment", _body("amendment", grants=["policy_exception"])),
            ("witness", _body("witness")),
            ("reconciliation", _body("reconciliation")),
        ):
            committed = self.append(body)
            for key, value in body.items():
                if key == "round" and kind == "round":
                    continue
                self.assertEqual(committed[key], value, (kind, key))


# ---------------------------------------------------------------------- batch


class BatchTests(StoreCase):
    def test_same_candidate_batch_accepted(self):
        committed = self.seed()
        self.assertEqual(len(committed), 4)
        self.assertEqual(self.count(), 4)
        folded = chain.read_chain(self.path)["folded"]
        self.assertEqual(folded["rounds"][0]["round_id"], "RD-0001")
        self.assertEqual(folded["findings"]["AF-0002"]["opened_round"], 1)

    def test_batch_example_precomputed_ids_on_a_non_empty_chain(self):
        self.seed()
        head = self.head()
        batch = [
            _body("round"),
            _body("finding", round=2, round_id="RD-0005"),
            _body("recommendation", round=2, round_id="RD-0005", finding_id="AF-0006"),
        ]
        committed = chain.append_batch(self.path, batch, expected_head=head)
        self.assertEqual(
            [r["id"] for r in committed], ["RD-0005", "AF-0006", "AC-0007"]
        )
        self.assertEqual(committed[0]["round"], 2)

    def test_fault_mid_batch_leaves_bytes_identical(self):
        self.append(_body("round"))
        before = self.raw()
        with mock.patch.object(os, "replace", side_effect=OSError(errno.EIO, "x")):
            with self.assertRaises(chain.AuthorityWriteError):
                chain.append_batch(self.path, _standard_batch()[1:2] + [_body("round")])
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.leftovers(), [])

    def test_refused_record_in_batch_writes_nothing(self):
        for label, prefix in (("absent", None), ("existing", _body("round"))):
            with self.subTest(label):
                if os.path.exists(self.path):
                    os.unlink(self.path)
                if prefix:
                    self.append(prefix)
                before = self.raw()
                batch = [_body("round"), _body("finding", round_id="RD-0099")]
                self.assertRefused("unknown_reference", chain.append_batch, self.path, batch)
                self.assertEqual(self.raw(), before)

    def test_mixed_candidates_refused_for_round_finding_recommendation(self):
        self.append(_body("round"))
        self.append(_body("finding"))
        pairings = [
            ("round/finding", [_body("round"), _body("finding", candidate=C2, round_id="RD-0003")]),
            (
                "round/recommendation",
                [_body("round"), _body("recommendation", candidate=C2, round_id="RD-0003")],
            ),
            (
                "finding/recommendation",
                [_body("finding"), _body("recommendation", candidate=C2)],
            ),
            ("status vs owned", [_body("round"), _body("finding", candidate=S1)]),
        ]
        for label, batch in pairings:
            with self.subTest(label):
                before = self.raw()
                self.assertRefused(
                    "candidate_mismatch_in_batch", chain.append_batch, self.path, batch
                )
                self.assertEqual(self.raw(), before)

    def test_other_kinds_exempt_from_same_candidate_rule(self):
        committed = chain.append_batch(
            self.path,
            [
                _body("round"),
                _body("decision", candidate=C2),
                _body("amendment"),
                _body("proposal"),
                _body("witness", candidate=S1),
            ],
        )
        self.assertEqual(len(committed), 5)

    def test_forward_reference_refused(self):
        batch = [_body("finding", round_id="RD-0002"), _body("round")]
        self.assertRefused("bad_round", chain.append_batch, self.path, batch)
        self.assertIsNone(self.raw())

    def test_empty_and_malformed_batches_refused(self):
        self.assertRefused("empty_batch", chain.append_batch, self.path, [])
        self.assertRefused("not_an_object", chain.append_batch, self.path, "x")
        self.assertRefused("not_an_object", chain.append_batch, self.path, [_body("round"), 3])
        self.assertIsNone(self.raw())

    def test_per_record_op_key_semantics(self):
        batch = [_body("round", op_key="k1"), _body("decision", op_key="k2")]
        first = chain.append_batch(self.path, batch)
        before = self.raw()
        again = chain.append_batch(self.path, copy.deepcopy(batch))
        self.assertEqual(again, first)
        self.assertEqual(self.raw(), before)
        partial = copy.deepcopy(batch) + [_body("decision", op_key="k3", request_id="r2")]
        self.assertRefused("op_key_partial", chain.append_batch, self.path, partial)
        changed = [_body("round", op_key="k1"), _body("decision", op_key="k2", request_id="zz")]
        self.assertRefused("op_key_conflict", chain.append_batch, self.path, changed)
        dup = [_body("decision", op_key="k9"), _body("decision", op_key="k9")]
        self.assertRefused("duplicate_op_key", chain.append_batch, self.path, dup)
        self.assertEqual(self.raw(), before)

    def test_returned_batch_is_fresh_copies(self):
        body = [_body("round")]
        snapshot = copy.deepcopy(body)
        committed = chain.append_batch(self.path, body)
        committed[0]["seat"] = "x"
        self.assertEqual(body, snapshot)
        self.assertEqual(chain.read_chain(self.path)["records"][0]["seat"], SEAT)


# ---------------------------------------------------------------------- faults


def _fail_first_regular_fsync():
    state = {"n": 0}

    def fake(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode) and state["n"] == 0:
            state["n"] += 1
            raise OSError(errno.EIO, "injected tmp fsync")
        return _REAL_FSYNC(fd)

    return fake


def _fail_first_dir_fsync():
    state = {"n": 0}

    def fake(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and state["n"] == 0:
            state["n"] += 1
            raise OSError(errno.EIO, "injected directory fsync")
        return _REAL_FSYNC(fd)

    return fake


def _fail_first_replace():
    state = {"n": 0}

    def fake(src, dst):
        if state["n"] == 0:
            state["n"] += 1
            raise OSError(errno.EIO, "injected replace")
        return _REAL_REPLACE(src, dst)

    return fake


def _replace_then_fail_second(perform_second):
    """First replace passes; the second (the rollback) fails, optionally after acting."""
    state = {"n": 0}

    def fake(src, dst):
        state["n"] += 1
        if state["n"] == 1:
            return _REAL_REPLACE(src, dst)
        if perform_second:
            _REAL_REPLACE(src, dst)
        raise OSError(errno.EIO, "injected rollback replace")

    return fake


def _readback_oserror(path):
    raise OSError(errno.EIO, "injected readback")


def _readback_mismatch(path):
    return _REAL_READBACK(path) + b" "


def _uncertain_append(case, body, perform_second=False, expected_head=None, op_key=None):
    """Append under an injected post-replace fault whose rollback also fails."""
    with mock.patch.object(
        os, "replace", side_effect=_replace_then_fail_second(perform_second)
    ), mock.patch.object(chain, "_readback", _readback_oserror):
        with case.assertRaises(chain.AuthorityCommitUncertain) as ctx:
            chain.append(case.path, body, expected_head=expected_head, op_key=op_key)
    return ctx.exception


class FaultInjectionTests(StoreCase):
    def check_fault(self, patches):
        for label in ("existing", "absent"):
            with self.subTest(label):
                if os.path.exists(self.path):
                    os.unlink(self.path)
                if label == "existing":
                    self.seed()
                before = self.raw()
                with patches():
                    with self.assertRaises(chain.AuthorityWriteError) as ctx:
                        self.append(_body("round"))
                self.assertNotIsInstance(ctx.exception, chain.AuthorityCommitUncertain)
                self.assertEqual(self.raw(), before)
                self.assertEqual(self.leftovers(), [])
                self.append(_body("round", seat="after-fault"))

    def test_fsync_tmp_fault(self):
        self.check_fault(lambda: mock.patch.object(os, "fsync", _fail_first_regular_fsync()))

    def test_fsync_dir_fault(self):
        self.check_fault(lambda: mock.patch.object(os, "fsync", _fail_first_dir_fsync()))

    def test_replace_fault(self):
        self.check_fault(lambda: mock.patch.object(os, "replace", _fail_first_replace()))

    def test_readback_mismatch(self):
        self.check_fault(lambda: mock.patch.object(chain, "_readback", _readback_mismatch))

    def test_readback_oserror(self):
        self.check_fault(lambda: mock.patch.object(chain, "_readback", _readback_oserror))

    def test_no_tmp_leftovers(self):
        self.seed()
        for patches in (
            mock.patch.object(os, "fsync", _fail_first_regular_fsync()),
            mock.patch.object(os, "replace", _fail_first_replace()),
            mock.patch.object(chain, "_readback", _readback_oserror),
        ):
            with patches:
                with self.assertRaises(chain.AuthorityWriteError):
                    self.append(_body("round"))
            self.assertEqual(self.leftovers(), [])

    def test_uncertain_commit_then_read_chain_and_op_key_resolves_when_committed(self):
        self.seed()
        head = self.head()
        body = _body("decision", op_key="retry-1")
        _uncertain_append(self, body, expected_head=head)
        result = chain.read_chain(self.path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["records"][-1]["op_key"], "retry-1")
        before = self.raw()
        again = chain.append(self.path, body, expected_head=head)
        self.assertEqual(again, result["records"][-1])
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.leftovers(), [])

    def test_uncertain_commit_then_op_key_appends_once_when_not_committed(self):
        self.seed()
        head = self.head()
        before = self.raw()
        body = _body("decision", op_key="retry-2")
        _uncertain_append(self, body, perform_second=True, expected_head=head)
        self.assertEqual(self.raw(), before)
        committed = chain.append(self.path, body, expected_head=head)
        self.assertEqual(committed["seq"], 5)
        self.assertEqual(self.count(), 5)
        chain.append(self.path, body, expected_head=head)
        self.assertEqual(self.count(), 5)

    def test_rollback_with_unverifiable_bytes_is_uncertain(self):
        self.seed()
        state = {"n": 0}
        real_read = chain._read_bytes

        def read(path):
            state["n"] += 1
            if state["n"] > 1:
                return b"unrelated"
            return real_read(path)

        with mock.patch.object(chain, "_readback", _readback_oserror), mock.patch.object(
            chain, "_read_bytes", read
        ):
            with self.assertRaises(chain.AuthorityCommitUncertain):
                self.append(_body("round"))


# ------------------------------------------------------- head and idempotency


class HeadAndIdempotencyTests(StoreCase):
    def test_head_mismatch_conflict_writes_nothing(self):
        self.seed()
        before = self.raw()
        real = self.head()
        with self.assertRaises(chain.AuthorityHeadConflict) as ctx:
            self.append(_body("round"), expected_head=A)
        self.assertEqual(ctx.exception.expected, A)
        self.assertEqual(ctx.exception.actual, real)
        self.assertEqual(self.raw(), before)
        self.append(_body("round"), expected_head=real)

    def test_empty_head_asserts_empty_chain(self):
        self.append(_body("round"), expected_head=chain.EMPTY_HEAD)
        with self.assertRaises(chain.AuthorityHeadConflict) as ctx:
            self.append(_body("round"), expected_head=chain.EMPTY_HEAD)
        self.assertEqual(ctx.exception.expected, "")
        self.assertEqual(ctx.exception.actual, self.head())
        self.assertEqual(self.count(), 1)

    def test_none_head_is_unchecked(self):
        self.append(_body("round"))
        self.append(_body("round"), expected_head=None)
        self.assertEqual(self.count(), 2)

    def test_empty_head_on_a_zero_byte_chain(self):
        self.write_raw(b"")
        self.append(_body("round"), expected_head=chain.EMPTY_HEAD)
        self.assertEqual(self.count(), 1)

    def test_bad_expected_head_refused(self):
        for value in (5, b"", ["x"], True):
            with self.subTest(repr(value)):
                self.assertAppendRefused("bad_expected_head", _body("round"), expected_head=value)

    def test_same_op_key_same_body_returns_committed_after_head_moved(self):
        body = _body("round", op_key="op-1")
        first = self.append(body, expected_head=chain.EMPTY_HEAD)
        self.append(_body("round", seat="other-seat"))
        before = self.raw()
        again = self.append(copy.deepcopy(body), expected_head=chain.EMPTY_HEAD)
        self.assertEqual(again, first)
        self.assertEqual(self.raw(), before)

    def test_same_op_key_different_body_refused(self):
        self.append(_body("round", op_key="op-1"))
        self.assertAppendRefused("op_key_conflict", _body("round", op_key="op-1", seat="x"))

    def test_op_key_argument_is_merged_and_checked(self):
        committed = self.append(_body("round"), op_key="arg-1")
        self.assertEqual(committed["op_key"], "arg-1")
        self.assertAppendRefused("op_key_conflict", _body("round", op_key="a"), op_key="b")
        self.assertAppendRefused("bad_value", _body("round"), op_key="")

    def test_race_on_one_head_has_exactly_one_winner(self):
        workers = 6
        for label in ("non-empty head", "empty head"):
            with self.subTest(label):
                if os.path.exists(self.path):
                    os.unlink(self.path)
                if label == "non-empty head":
                    self.append(_body("round"))
                    expected = self.head()
                    count = 1
                else:
                    expected = chain.EMPTY_HEAD
                    count = 0
                barrier = threading.Barrier(workers, timeout=30)
                results = []
                guard = threading.Lock()

                def run(index):
                    body = _body("round", seat="seat-%d" % index)
                    try:
                        barrier.wait()
                        outcome = chain.append(self.path, body, expected_head=expected)
                    except Exception as err:  # noqa: BLE001 - recorded for assertions
                        outcome = err
                    with guard:
                        results.append(outcome)

                threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=60)
                    self.assertFalse(thread.is_alive())
                winners = [r for r in results if isinstance(r, dict)]
                losers = [r for r in results if isinstance(r, chain.AuthorityHeadConflict)]
                self.assertEqual(len(winners), 1, results)
                self.assertEqual(len(losers), workers - 1, results)
                self.assertEqual(self.count(), count + 1)
                self.assertTrue(chain.read_chain(self.path)["ok"])


# --------------------------------------------------- resolution and transitions


class ResolutionBasisTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def resolve(self, basis=None, **over):
        body = _body("resolution", candidate=C2, **over)
        if basis is not None:
            body["basis"] = basis
        return body

    def test_positive_closure_finding_c1_proposal_and_resolution_c2(self):
        folded = chain.read_chain(self.path)["folded"]
        self.assertEqual(folded["findings"]["AF-0002"]["candidate"], C1)
        self.assertEqual(folded["proposals"]["AP-0003"]["changed_evidence"]["candidate"], C2)
        committed = self.append(self.resolve())
        self.assertEqual(committed["candidate"], C2)
        folded = chain.read_chain(self.path)["folded"]
        self.assertEqual(folded["findings"]["AF-0002"]["state"], "closed")
        self.assertEqual(folded["findings"]["AF-0002"]["last_transition_id"], committed["id"])
        self.assertEqual(folded["findings"]["AF-0002"]["candidate"], C1)

    def test_decision_only_close_accepted(self):
        decision = self.append(_decision_close())
        self.append(self.resolve(_basis(decision=decision["id"])))
        self.assertEqual(
            chain.read_chain(self.path)["folded"]["findings"]["AF-0002"]["state"], "closed"
        )

    def test_proposal_with_close_decision_accepted(self):
        decision = self.append(_decision_close())
        self.append(self.resolve(_basis(proposal="AP-0003", decision=decision["id"])))

    def test_witness_ids_resolve_and_are_accepted(self):
        witness = self.append(_body("witness"))
        self.append(self.resolve(_basis("AP-0003", "AC-0004", witnesses=[witness["id"]])))

    def test_no_proposal_and_no_decision_refused(self):
        for label, basis in (
            ("empty", _basis()),
            ("proposal only", _basis(proposal="AP-0003")),
            ("recommendation only", _basis(recommendation="AC-0004")),
        ):
            with self.subTest(label):
                self.assertAppendRefused("illegal_resolution_basis", self.resolve(basis))

    def test_proposal_for_another_finding_refused(self):
        other = _body("proposal", finding_ids=["AF-0099"])
        self.append(other)
        self.assertAppendRefused(
            "illegal_resolution_basis", self.resolve(_basis("AP-0005", "AC-0004"))
        )

    def test_proposal_changed_evidence_on_another_candidate_refused(self):
        proposal = _body("proposal")
        proposal["changed_evidence"]["candidate"] = C1
        self.append(proposal)
        self.assertAppendRefused(
            "illegal_resolution_basis", self.resolve(_basis("AP-0005", "AC-0004"))
        )

    def test_record_candidate_equal_but_changed_evidence_candidate_differs_refused(self):
        proposal = _body("proposal", candidate=C2)
        proposal["changed_evidence"]["candidate"] = C1
        self.append(proposal)
        self.assertAppendRefused(
            "illegal_resolution_basis", self.resolve(_basis("AP-0005", "AC-0004"))
        )

    def test_changed_evidence_of_a_different_kind_refused(self):
        proposal = _body("proposal")
        proposal["changed_evidence"]["candidate"] = S1
        self.append(proposal)
        self.assertAppendRefused(
            "illegal_resolution_basis", self.resolve(_basis("AP-0005", "AC-0004"))
        )

    def test_finding_candidate_is_never_compared(self):
        folded = chain.read_chain(self.path)["folded"]
        self.assertNotEqual(folded["findings"]["AF-0002"]["candidate"], C2)
        self.append(self.resolve())

    def test_decided_by_reviewer_refused(self):
        body = self.resolve()
        body["decided_by"] = "reviewer"
        self.assertAppendRefused("bad_value", body)

    def test_outcome_other_than_closed_refused(self):
        self.assertAppendRefused("bad_value", self.resolve(outcome="withdrawn"))

    def test_decision_uphold_is_not_a_close_basis(self):
        decision = self.append(
            _body(
                "decision",
                adjudicates=[{"finding_id": "AF-0002", "outcome": "uphold"}],
            )
        )
        self.assertAppendRefused(
            "illegal_resolution_basis", self.resolve(_basis(decision=decision["id"]))
        )

    def test_decision_closing_another_finding_refused(self):
        decision = self.append(_decision_close(finding_id="AF-0099"))
        self.assertAppendRefused(
            "illegal_resolution_basis", self.resolve(_basis(decision=decision["id"]))
        )

    def test_null_candidate_decision_is_never_a_basis_with_proposal(self):
        decision = self.append(_null_decision())
        self.assertAppendRefused(
            "null_candidate_decision_basis",
            self.resolve(_basis("AP-0003", "AC-0004", decision["id"])),
        )

    def test_null_candidate_decision_is_never_a_basis_decision_only(self):
        decision = self.append(_null_decision())
        self.assertAppendRefused(
            "null_candidate_decision_basis", self.resolve(_basis(decision=decision["id"]))
        )

    def test_unknown_and_wrong_kind_ids_refused(self):
        rows = [
            ("unknown proposal", _basis("AP-0099", "AC-0004")),
            ("proposal id of a finding", _basis("AF-0002", "AC-0004")),
            ("unknown recommendation", _basis("AP-0003", "AC-0099")),
            ("recommendation id of a proposal", _basis("AP-0003", "AP-0003")),
            ("unknown decision", _basis(decision="AD-0099")),
            ("decision id of a round", _basis(decision="RD-0001")),
            ("unresolved witness", _basis("AP-0003", "AC-0004", witnesses=["AV-0099"])),
            ("witness id of a finding", _basis("AP-0003", "AC-0004", witnesses=["AF-0002"])),
        ]
        for label, basis in rows:
            with self.subTest(label):
                self.assertAppendRefused("unknown_reference", self.resolve(basis))

    def test_recommendation_must_say_closed_for_this_finding(self):
        self.append(_body("recommendation", recommend="still_open"))
        self.append(_body("recommendation", finding_id="AF-0099"))
        for label, recommendation in (("still_open", "AC-0005"), ("other finding", "AC-0006")):
            with self.subTest(label):
                self.assertAppendRefused(
                    "illegal_resolution_basis",
                    self.resolve(_basis("AP-0003", recommendation)),
                )

    def test_basis_key_shape_enforced(self):
        body = self.resolve()
        del body["basis"]["decision_id"]
        self.assertAppendRefused("missing_key", body)
        body = self.resolve()
        body["basis"]["extra"] = None
        self.assertAppendRefused("unknown_key", body)

    def test_resolving_non_open_or_unknown_findings_refused(self):
        self.append(_body("finding", round_id="RD-0001"))  # AF-0005
        self.append(_body("finding", round_id="RD-0001"))  # AF-0006
        self.append(_body("retraction", finding_id="AF-0005"))
        self.append(
            _body("retraction", finding_id="AF-0006", reason="duplicate", duplicate_of="AF-0002")
        )
        self.append(self.resolve())
        for label, finding in (
            ("closed", "AF-0002"),
            ("withdrawn", "AF-0005"),
            ("duplicate", "AF-0006"),
        ):
            with self.subTest(label):
                self.assertAppendRefused("illegal_transition", self.resolve(finding_id=finding))
        self.assertAppendRefused("unknown_reference", self.resolve(finding_id="AF-0099"))


class TransitionTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def state(self, finding="AF-0002"):
        return chain.read_chain(self.path)["folded"]["findings"][finding]

    def resolve(self):
        return _body("resolution", candidate=C2, basis=_basis("AP-0003", "AC-0004"))

    def test_open_to_closed_to_open_cycle(self):
        closed = self.append(self.resolve())
        self.assertEqual(self.state()["state"], "closed")
        reopened = self.append(_body("reopen"))
        self.assertEqual(self.state()["state"], "open")
        self.assertEqual(self.state()["last_transition_id"], reopened["id"])
        again = self.append(self.resolve())
        self.assertNotEqual(again["id"], closed["id"])
        self.assertEqual(self.state()["state"], "closed")
        kinds = [e["kind"] for e in chain.read_chain(self.path)["folded"]["events"]]
        self.assertEqual(kinds, ["opened", "closed", "reopened", "closed"])

    def test_retraction_only_from_open_and_terminal(self):
        self.append(_body("retraction"))
        self.assertEqual(self.state()["state"], "withdrawn")
        for body in (
            _body("retraction"),
            _body("retraction", reason="duplicate", duplicate_of="AF-0009"),
            self.resolve(),
            _body("reopen"),
        ):
            with self.subTest(body["kind"]):
                self.assertAppendRefused("illegal_transition", body)

    def test_duplicate_retraction_is_terminal_with_its_own_state(self):
        self.append(_body("retraction", reason="duplicate", duplicate_of="AF-0009"))
        self.assertEqual(self.state()["state"], "duplicate")
        self.assertAppendRefused("illegal_transition", _body("reopen"))

    def test_retraction_from_closed_refused(self):
        self.append(self.resolve())
        self.assertAppendRefused("illegal_transition", _body("retraction"))

    def test_reopen_only_from_closed(self):
        self.assertAppendRefused("illegal_transition", _body("reopen"))
        self.append(self.resolve())
        self.append(_body("reopen"))
        self.assertAppendRefused("illegal_transition", _body("reopen"))

    def test_unknown_finding_for_retraction_and_reopen(self):
        self.assertAppendRefused("unknown_reference", _body("retraction", finding_id="AF-0099"))
        self.assertAppendRefused("unknown_reference", _body("reopen", finding_id="AF-0099"))

    def test_no_mirror_kind(self):
        self.assertAppendRefused("unknown_kind", _body("round", kind="mirror"))

    def test_round_references_are_checked(self):
        other_phase = self.append(_body("round", phase="planning"))
        rows = [
            ("unknown round_id", _body("finding", round_id="RD-0099"), "unknown_reference"),
            ("round_id of a finding", _body("finding", round_id="AF-0002"), "unknown_reference"),
            ("round_id other phase", _body("finding", round_id=other_phase["id"]), "round_mismatch"),
            ("wrong int round", _body("finding", round=2), "bad_round"),
            ("round never minted", _body("finding", round=9), "bad_round"),
            ("null round on finding", _body("finding", round=None), "bad_round"),
            ("null round on resolution", self.resolve() | {"round": None}, "bad_round"),
            ("null round on retraction", _body("retraction", round=None), "bad_round"),
            ("null round on reopen", _body("reopen", round=None), "bad_round"),
            ("null round on recommendation", _body("recommendation", round=None), "bad_round"),
            ("decision int round never minted", _body("decision", round=4), "bad_round"),
            (
                "recommendation round_id mismatch",
                _body("recommendation", round_id=other_phase["id"]),
                "round_mismatch",
            ),
            (
                "int round only minted in another phase",
                _body("finding", phase="scouting", round=1),
                "bad_round",
            ),
        ]
        for label, body, reason in rows:
            with self.subTest(label):
                self.assertAppendRefused(reason, body)

    def test_round_int_must_match_round_id_round(self):
        self.append(_body("round"))  # RD-0005 is round 2
        self.assertAppendRefused("round_mismatch", _body("finding", round=1, round_id="RD-0005"))
        self.append(_body("finding", round=2, round_id="RD-0005"))

    def test_non_event_kinds_accept_null_or_minted_round(self):
        self.append(_body("decision", round=1))
        self.append(_body("witness", round=None))


# ----------------------------------------------------------------- corruption


class ReadChainCorruptionTests(StoreCase):
    def forge(self, bodies=None, overrides=None):
        bodies = _standard_batch() if bodies is None else bodies
        self.write_raw(_lines(_stamp_all(bodies, overrides)))

    def test_absent_and_zero_byte_files_are_ok_and_empty(self):
        for label in ("absent", "zero"):
            with self.subTest(label):
                if label == "zero":
                    self.write_raw(b"")
                result = chain.read_chain(self.path)
                self.assertTrue(result["ok"])
                self.assertEqual(result["records"], [])
                self.assertIsNone(result["head"])
                self.assertIsNone(result["error"])
                self.assertEqual(result["folded"], chain.fold([]))

    def test_valid_chain_reads_ok(self):
        self.seed()
        result = chain.read_chain(self.path)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["records"]), 4)
        self.assertEqual(result["head"], result["records"][-1]["digest"])
        self.assertEqual(set(result), {"ok", "records", "head", "error", "folded"})

    def test_directory_path_is_unreadable(self):
        os.mkdir(self.path)
        self.assertReadCode("unreadable")

    def test_truncated_mid_line(self):
        self.seed()
        raw = self.raw()
        first = raw.index(b"\n")
        for label, cut in (("mid first line", first // 2), ("mid last line", len(raw) - 7)):
            with self.subTest(label):
                self.write_raw(raw[:cut])
                self.assertReadCode("torn_line")

    def test_flipped_byte(self):
        self.seed()
        raw = self.raw()
        self.write_raw(raw.replace(b'"summary":"a summary"', b'"summary":"a summarz"', 1))
        self.assertReadCode("digest_break")

    def test_reordered_lines(self):
        self.seed()
        lines = self.raw().split(b"\n")
        lines[1], lines[2] = lines[2], lines[1]
        self.write_raw(b"\n".join(lines))
        self.assertReadCode("seq_gap")

    def test_duplicated_line(self):
        self.seed()
        lines = self.raw().split(b"\n")
        lines.insert(2, lines[1])
        self.write_raw(b"\n".join(lines))
        self.assertReadCode("seq_gap")

    def test_forged_duplicate_id(self):
        self.forge(overrides={2: {"id": "AF-0002"}})
        self.assertReadCode("duplicate_id")

    def test_digest_break_on_prev_digest_or_digest(self):
        self.seed()
        records = chain.read_chain(self.path)["records"]
        for label, index, key in (("prev", 2, "prev_digest"), ("digest", 1, "digest")):
            with self.subTest(label):
                edited = copy.deepcopy(records)
                edited[index][key] = D
                self.write_raw(_lines(edited))
                self.assertReadCode("digest_break")

    def test_seq_gap(self):
        self.forge(overrides={2: {"seq": 4}})
        self.assertReadCode("seq_gap")

    def test_unknown_kind(self):
        self.forge(overrides={2: {"kind": "mirror"}})
        self.assertReadCode("unknown_kind")

    def test_unknown_key(self):
        self.forge(overrides={1: {"extra": "x"}})
        self.assertReadCode("unknown_key")

    def test_wrong_schema(self):
        self.forge(overrides={1: {"schema": 2}})
        self.assertReadCode("wrong_schema")

    def test_wrong_session_uuid(self):
        self.forge(overrides={1: {"session_uuid": "other"}})
        self.assertReadCode("wrong_session_uuid")

    def test_illegal_transition_with_recomputed_digests(self):
        bodies = [
            _body("round"),
            _body("finding"),
            _body("resolution", candidate=C2, basis=_basis()),
        ]
        self.forge(bodies)
        self.assertReadCode("illegal_transition")

    def test_illegal_resolution_basis_and_null_decision_basis_are_transitions(self):
        bodies = [
            _body("round"),
            _body("finding"),
            _null_decision(),
            _body("resolution", candidate=C2, basis=_basis(decision="AD-0003")),
        ]
        self.forge(bodies)
        self.assertReadCode("illegal_transition")

    def test_illegal_record_codes(self):
        rows = [
            ("unknown round_id", [_body("round"), _body("finding", round_id="RD-0009")]),
            ("bad value", [_body("round"), _body("finding", blocking=False)]),
            ("bad candidate", [_body("round", candidate={"kind": "x"})]),
            ("wrong round number", [_body("round")]),
        ]
        overrides = {3: {}}
        for label, bodies in rows:
            with self.subTest(label):
                ovr = {0: {"round": 7}} if label == "wrong round number" else overrides
                self.forge(bodies, ovr)
                self.assertReadCode("illegal_record")

    def test_duplicate_op_key_in_stored_chain_is_illegal(self):
        self.forge(
            [_body("round", op_key="k"), _body("decision", op_key="k")],
        )
        self.assertReadCode("illegal_record")

    def test_non_canonical_and_malformed_bytes(self):
        self.seed()
        records = chain.read_chain(self.path)["records"]
        good = _lines(records)
        spaced = (json.dumps(records[0], sort_keys=True) + "\n").encode("ascii")
        rows = [
            ("non-canonical spacing", spaced + good.split(b"\n", 1)[1]),
            ("invalid utf-8", b"\xff\xfe\n" + good),
            ("blank line", good + b"\n"),
            ("blank first line", b"\n" + good),
            ("missing final newline", good[:-1]),
            ("duplicate json key", b'{"kind":"round","kind":"round"}\n'),
            ("nan constant", b'{"x":NaN}\n'),
            ("non-object", b"[1]\n"),
            ("scalar", b"7\n"),
            ("unicode escape not canonical", good.replace(b'"seat":"build-reviewer"', b'"seat":"build-\\u0072eviewer"', 1)),
        ]
        for label, data in rows:
            with self.subTest(label):
                self.write_raw(data)
                self.assertReadCode("torn_line")

    def test_floats_in_stored_records_are_illegal(self):
        self.seed()
        raw = self.raw().replace(b'"seq":1,', b'"seq":1.5,', 1)
        self.write_raw(raw)
        result = chain.read_chain(self.path)
        self.assertFalse(result["ok"])

    def test_boundary_truncation_is_a_valid_shorter_chain(self):
        """A cut exactly at a record boundary cannot be told from a shorter chain.

        This documents a limitation: only expected_head and a head recorded
        outside the chain can notice it.
        """
        self.seed()
        lines = self.raw().split(b"\n")
        self.write_raw(b"\n".join(lines[:2]) + b"\n")
        result = chain.read_chain(self.path)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["records"]), 2)

    def test_bad_chain_never_returns_partial_view(self):
        self.seed()
        self.write_raw(self.raw() + b"junk")
        result = chain.read_chain(self.path)
        self.assertEqual(result["records"], [])
        self.assertIsNone(result["folded"])

    def test_read_takes_no_lock(self):
        self.seed()
        fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertTrue(chain.read_chain(self.path)["ok"])


# ----------------------------------------------------------------------- fold


class FoldTests(StoreCase):
    def rich_chain(self):
        """A 22-record chain exercising every kind and every finding state."""
        step = self.append
        chain.append_batch(
            self.path,
            [
                _body("round"),
                _body("finding", claim_class="only"),
                _body("finding", severity="advisory", blocking=False),
                _body("finding"),
                _body("finding"),
            ],
        )
        step(_body("proposal"))  # AP-0006
        step(_body("recommendation"))  # AC-0007
        step(_body("witness"))  # AV-0008
        step(
            _body(
                "witness",
                result=None,
                not_executable_reason="no probe possible",
            )
        )  # AV-0009
        step(
            _body(
                "decision",
                adjudicates=[{"finding_id": "AF-0003", "outcome": "uphold"}],
            )
        )  # AD-0010
        step(_null_decision(request_id="req-null"))  # AD-0011
        step(
            _body(
                "resolution",
                candidate=C2,
                basis=_basis("AP-0006", "AC-0007", witnesses=["AV-0008"]),
            )
        )  # AR-0012
        step(_body("retraction", finding_id="AF-0004"))  # AT-0013
        step(
            _body(
                "retraction", finding_id="AF-0005", reason="duplicate", duplicate_of="AF-0002"
            )
        )  # AT-0014
        step(_body("reopen"))  # AO-0015
        step(_body("amendment", grants=["scope_expansion"]))  # AA-0016
        step(_body("escalation", proposal_id="AP-0006"))  # AE-0017
        step(_body("escalation_resume", escalation_id="AE-0017", grants=["policy_exception"]))
        step(  # AS-0019
            _body("escalation_resume", escalation_id="AE-0017", outcome="decline", grants=[])
        )
        step(_body("reconciliation"))  # AL-0020
        chain.append_batch(
            self.path,
            [
                _body("round"),
                _body("recommendation", round=2, round_id="RD-0021", finding_id="AF-0003",
                      recommend="still_open"),
            ],
        )  # RD-0021, AC-0022
        return chain.read_chain(self.path)

    def test_exact_folded_state_shape_and_values(self):
        result = self.rich_chain()
        self.assertTrue(result["ok"])
        folded = result["folded"]
        self.assertEqual(
            set(folded),
            {
                "decisions", "events", "findings", "grants", "head", "proposals",
                "recommendations", "rounds", "session_uuid", "witnesses",
            },
        )
        self.assertEqual(folded["head"], result["head"])
        self.assertEqual(folded["session_uuid"], SESSION)

        def finding(state, last, claim=None, blocking=True):
            return {
                "state": state,
                "phase": "building",
                "candidate": C1,
                "opened_round": 1,
                "last_transition_id": last,
                "blocking": blocking,
                "claim_class": claim,
                "session_uuid": SESSION,
            }

        self.assertEqual(
            folded["findings"],
            {
                "AF-0002": finding("open", "AO-0015", claim="only"),
                "AF-0003": finding("open", "AF-0003", blocking=False),
                "AF-0004": finding("withdrawn", "AT-0013"),
                "AF-0005": finding("duplicate", "AT-0014"),
            },
        )
        self.assertEqual(
            folded["rounds"],
            [
                {"round_id": "RD-0001", "phase": "building", "seat": SEAT, "round": 1,
                 "candidate": C1},
                {"round_id": "RD-0021", "phase": "building", "seat": SEAT, "round": 2,
                 "candidate": C1},
            ],
        )
        self.assertEqual(
            folded["proposals"],
            {
                "AP-0006": {
                    "finding_ids": ["AF-0002"],
                    "candidate": C1,
                    "changed_evidence": {"paths": ["a.py"], "sha256s": [A], "candidate": C2},
                    "author_role": "builder",
                    "requires": [],
                }
            },
        )
        self.assertEqual(
            folded["recommendations"],
            {
                "AF-0002": [
                    {"id": "AC-0007", "recommend": "closed", "round_id": "RD-0001",
                     "reviewer_seat": SEAT}
                ],
                "AF-0003": [
                    {"id": "AC-0022", "recommend": "still_open", "round_id": "RD-0021",
                     "reviewer_seat": SEAT}
                ],
            },
        )
        witness_result = {
            "outcome": "bypass_not_found",
            "evidence_path": "probe.txt",
            "evidence_sha256": A,
        }
        self.assertEqual(
            folded["witnesses"],
            {
                "claim-1": [
                    {"id": "AV-0008", "claim_class": "only", "candidate": C1,
                     "result": witness_result},
                    {"id": "AV-0009", "claim_class": "only", "candidate": C1, "result": None},
                ]
            },
        )
        self.assertEqual(
            folded["decisions"],
            {
                "AD-0010": {
                    "request_id": "req-1",
                    "principal": "supervisor_agent",
                    "answer_sha256": C,
                    "adjudicates": [{"finding_id": "AF-0003", "outcome": "uphold"}],
                    "candidate": C1,
                },
                "AD-0011": {
                    "request_id": "req-null",
                    "principal": "supervisor_agent",
                    "answer_sha256": C,
                    "adjudicates": [],
                    "candidate": None,
                },
            },
        )
        self.assertEqual(
            folded["grants"],
            [
                {"id": "AA-0016", "capability": "scope_expansion"},
                {"id": "AS-0018", "capability": "policy_exception"},
            ],
        )
        self.assertEqual(
            folded["events"],
            [
                {"seq": 2, "round": 1, "finding_id": "AF-0002", "kind": "opened", "id": "AF-0002"},
                {"seq": 3, "round": 1, "finding_id": "AF-0003", "kind": "opened", "id": "AF-0003"},
                {"seq": 4, "round": 1, "finding_id": "AF-0004", "kind": "opened", "id": "AF-0004"},
                {"seq": 5, "round": 1, "finding_id": "AF-0005", "kind": "opened", "id": "AF-0005"},
                {"seq": 12, "round": 1, "finding_id": "AF-0002", "kind": "closed", "id": "AR-0012"},
                {"seq": 13, "round": 1, "finding_id": "AF-0004", "kind": "withdrawn", "id": "AT-0013"},
                {"seq": 14, "round": 1, "finding_id": "AF-0005", "kind": "duplicate", "id": "AT-0014"},
                {"seq": 15, "round": 1, "finding_id": "AF-0002", "kind": "reopened", "id": "AO-0015"},
            ],
        )

    def test_fold_equals_read_chain_folded(self):
        result = self.rich_chain()
        self.assertEqual(chain.fold(result["records"]), result["folded"])

    def test_empty_fold(self):
        folded = chain.fold([])
        self.assertEqual(
            folded,
            {
                "decisions": {}, "events": [], "findings": {}, "grants": [], "head": None,
                "proposals": {}, "recommendations": {}, "rounds": [], "session_uuid": None,
                "witnesses": {},
            },
        )

    def test_reopen_changes_only_state_and_last_transition_id(self):
        self.rich_chain()
        after = chain.read_chain(self.path)["folded"]["findings"]["AF-0002"]
        self.assertEqual(after["state"], "open")
        self.assertEqual(after["last_transition_id"], "AO-0015")
        self.assertEqual(after["opened_round"], 1)
        self.assertEqual(after["candidate"], C1)
        self.assertEqual(after["phase"], "building")
        self.assertTrue(after["blocking"])
        self.assertEqual(after["claim_class"], "only")

    def test_opened_round_is_int_round_not_round_id(self):
        folded = self.rich_chain()["folded"]
        for row in folded["findings"].values():
            self.assertIsInstance(row["opened_round"], int)
        self.assertEqual(folded["rounds"][0]["round_id"], "RD-0001")
        self.assertEqual(folded["rounds"][0]["round"], 1)
        self.assertEqual(folded["recommendations"]["AF-0003"][0]["round_id"], "RD-0021")

    def test_events_kind_mapping(self):
        events = self.rich_chain()["folded"]["events"]
        self.assertEqual(
            {e["kind"] for e in events},
            {"opened", "closed", "withdrawn", "duplicate", "reopened"},
        )
        for event in events:
            self.assertEqual(set(event), {"seq", "round", "finding_id", "kind", "id"})

    def test_fold_raises_on_corrupt_input_and_non_dict_entries(self):
        records = self.rich_chain()["records"]
        with self.assertRaises(chain.AuthorityReadError) as ctx:
            chain.fold(records[:1] + [dict(records[1], digest=A)])
        self.assertEqual(ctx.exception.code, "digest_break")
        for label, value in (("non-dict", [1]), ("string", ["x"]), ("none", [None])):
            with self.subTest(label):
                with self.assertRaises(chain.AuthorityReadError) as ctx:
                    chain.fold(value)
                self.assertEqual(ctx.exception.code, "illegal_record")
        with self.assertRaises(chain.AuthorityReadError):
            chain.fold("not a list")
        with self.assertRaises(chain.AuthorityReadError) as ctx:
            chain.fold(records[1:])
        self.assertEqual(ctx.exception.code, "seq_gap")

    def test_no_mutation_no_aliasing(self):
        result = self.rich_chain()
        records = result["records"]
        snapshot = copy.deepcopy(records)
        first = chain.fold(records)
        self.assertEqual(records, snapshot)
        first["findings"]["AF-0002"]["candidate"]["kind"] = "tampered"
        first["proposals"]["AP-0006"]["changed_evidence"]["paths"].append("x")
        first["rounds"][0]["candidate"]["kind"] = "tampered"
        first["decisions"]["AD-0010"]["adjudicates"].append("x")
        first["witnesses"]["claim-1"][0]["result"]["outcome"] = "tampered"
        self.assertEqual(records, snapshot)
        second = chain.fold(records)
        self.assertEqual(second, result["folded"])
        self.assertIsNot(second["findings"]["AF-0002"]["candidate"], records[1]["candidate"])

    def test_findings_do_not_share_candidate_objects(self):
        folded = self.rich_chain()["folded"]
        first = folded["findings"]["AF-0002"]["candidate"]
        second = folded["findings"]["AF-0003"]["candidate"]
        self.assertIsNot(first, second)


# ----------------------------------------------------------------- delegation


class DelegationTests(StoreCase):
    def test_validation_path_goes_through_p1a_validate_candidate(self):
        with mock.patch.object(
            p1a, "validate_candidate", wraps=p1a.validate_candidate
        ) as spy:
            chain.validate_record(_body("proposal"))
        seen = [call.args[0] for call in spy.call_args_list]
        self.assertIn(C1, seen)
        self.assertIn(C2, seen)
        with mock.patch.object(p1a, "validate_candidate", return_value=(False, "x")):
            self.assertEqual(chain.validate_record(_body("round")), (False, "bad_candidate"))
            self.assertEqual(
                chain.validate_record(_body("proposal", candidate=C1)),
                (False, "bad_candidate"),
            )

    def test_batch_path_goes_through_p1a_candidate_equal(self):
        with mock.patch.object(p1a, "candidate_equal", wraps=p1a.candidate_equal) as spy:
            chain.append_batch(self.path, [_body("round"), _body("finding")])
        self.assertEqual([call.args for call in spy.call_args_list], [(C1, C1)])
        os.unlink(self.path)
        with mock.patch.object(p1a, "candidate_equal", return_value=False):
            self.assertRefused(
                "candidate_mismatch_in_batch",
                chain.append_batch,
                self.path,
                [_body("round"), _body("finding")],
            )

    def test_resolution_path_goes_through_p1a_candidate_equal(self):
        self.seed()
        resolution = _body("resolution", candidate=C2, basis=_basis("AP-0003", "AC-0004"))
        with mock.patch.object(p1a, "candidate_equal", return_value=False):
            self.assertAppendRefused("illegal_resolution_basis", copy.deepcopy(resolution))
        with mock.patch.object(p1a, "candidate_equal", wraps=p1a.candidate_equal) as spy:
            self.append(resolution)
        calls = [call.args for call in spy.call_args_list]
        self.assertTrue(calls)
        self.assertTrue(all(args == (C2, C2) for args in calls), calls)

    def test_read_path_revalidates_through_p1a(self):
        self.seed()
        with mock.patch.object(p1a, "validate_candidate", wraps=p1a.validate_candidate) as spy:
            self.assertTrue(chain.read_chain(self.path)["ok"])
        self.assertGreaterEqual(spy.call_count, 4)


# --------------------------------------------------------------------- purity


class PurityTests(unittest.TestCase):
    ALLOWED = {"copy", "fcntl", "hashlib", "json", "os", "re", "time"}

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_HERE, "cowork_authority_chain.py"), encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)

    def test_imports_are_stdlib_allowlist_plus_p1a(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, self.ALLOWED | {"cowork_authority_candidate"})
        for name in imported:
            if name.startswith("cowork_"):
                self.assertEqual(name, "cowork_authority_candidate")

    def test_p1a_imported_as_module_object(self):
        for node in ast.walk(self.tree):
            self.assertNotIsInstance(node, ast.ImportFrom)
        aliases = [
            alias.asname
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "cowork_authority_candidate"
        ]
        self.assertEqual(aliases, ["p1a"])
        used = set()
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "p1a"
            ):
                self.assertFalse(node.attr.startswith("_"), node.attr)
                used.add(node.attr)
        self.assertEqual(used, {"validate_candidate", "candidate_equal"})

    def test_docstring_states_the_single_writer_contract(self):
        doc = ast.get_docstring(self.tree)
        self.assertIn("ONLY writer, reader and validator", doc)
        self.assertIn("authority_chain.jsonl", doc)

    def test_module_has_no_candidate_equality_of_its_own(self):
        names = {
            node.name for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertFalse({n for n in names if "candidate_equal" in n or "candidate_compare" in n})


if __name__ == "__main__":
    unittest.main()
