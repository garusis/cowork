#!/usr/bin/env python3
"""The strict, atomic, hash-chained authority record store.

This module is the ONLY writer, reader and validator of `authority_chain.jsonl`.
The chain path is always an argument: nothing here derives a session path, and
nothing here is wired into a runtime. It depends on the standard library and on
`cowork_authority_candidate` (imported as a module object) only; candidate
validation and equality are delegated there and never reimplemented.

RECORDS. One JSON object per line, exactly the canonical JSON of the record
(sorted keys, compact separators, ASCII) followed by a newline. A record is one
of 13 kinds with an exact key set: the caller supplies `kind`, `session_uuid`,
`phase`, `round`, `candidate`, the kind fields and an optional `op_key`; the
store assigns `schema`, `seq`, `id`, `prev_digest` and `digest`, and mints the
integer `round` of a `round` record. `digest` is the sha256 of the canonical
JSON of the record without its `digest` key, and `prev_digest` is the stored
digest of the previous record ('' for the first). The chain head is the last
record's digest, or None for an empty chain.

ONE LEGALITY WALKER. Structure, linkage, per-kind rules, finding transitions
and the resolution basis live in one walker. `append`, `append_batch`,
`read_chain` and `fold` all feed records through it, so a chain edited by hand
with recomputed digests but an illegal transition still reads as not ok.

COMMIT. Under an exclusive flock on a sidecar lock file the chain is strictly
read, the new records are validated against it, and the old bytes plus the new
lines are written to a temporary file, fsynced, moved over the chain with
`os.replace`, the directory is fsynced and the file is read back byte for byte
and parsed again. A failure after the replace restores the original bytes; when
that cannot be proven the outcome is `AuthorityCommitUncertain`. A retry that
carries the same `op_key` resolves either outcome without a duplicate record.
`read_chain` takes no lock: the atomic replace gives it a consistent snapshot.

HEAD CHECK. `expected_head=None` means unchecked. `EMPTY_HEAD` ('') asserts that
the chain is empty. Any other string must equal the current head digest. An
`op_key` replay is resolved before the head check.

LIMIT. A chain cut exactly at a record boundary is a valid shorter chain and is
not detectable by `read_chain` alone; only `expected_head` and a head recorded
outside the chain can notice it.

Python 3.9+, stdlib only.
"""

import copy
import fcntl
import hashlib
import json
import os
import re
import time

import cowork_authority_candidate as p1a

SCHEMA = 1

RECORD_KINDS = (
    "round",
    "finding",
    "proposal",
    "recommendation",
    "resolution",
    "retraction",
    "reopen",
    "decision",
    "amendment",
    "escalation",
    "escalation_resume",
    "witness",
    "reconciliation",
)

ID_PREFIXES = {
    "round": "RD",
    "finding": "AF",
    "proposal": "AP",
    "recommendation": "AC",
    "resolution": "AR",
    "retraction": "AT",
    "reopen": "AO",
    "decision": "AD",
    "amendment": "AA",
    "escalation": "AE",
    "escalation_resume": "AS",
    "witness": "AV",
    "reconciliation": "AL",
}

PHASES = ("scouting", "planning", "building")
PRINCIPALS = ("reviewer", "supervisor_agent", "policy_principal", "runtime")
CAPABILITIES = ("scope_expansion", "policy_exception")
CLAIM_CLASSES = ("source_of_truth", "only", "never", "fail_closed")
WITNESS_OUTCOMES = ("bypass_not_found", "bypass_found")

_DECISION_PRINCIPALS = ("supervisor_agent", "policy_principal")
_RETRACTION_BY = ("reviewer", "supervisor_agent", "policy_principal")
_AUTHOR_ROLES = ("builder", "planner", "scout")
_RECOMMEND = ("closed", "still_open", "withdrawn", "duplicate")
_RETRACT_REASONS = ("withdrawn", "duplicate")
_ADJUDICATION_OUTCOMES = ("close", "uphold")
_RESUME_OUTCOMES = ("grant", "decline")

READ_ERROR_CODES = (
    "unreadable",
    "torn_line",
    "digest_break",
    "seq_gap",
    "unknown_kind",
    "unknown_key",
    "wrong_schema",
    "wrong_session_uuid",
    "duplicate_id",
    "illegal_record",
    "illegal_transition",
)

REFUSAL_REASONS = (
    "not_an_object",
    "unknown_kind",
    "missing_key",
    "unknown_key",
    "wrong_schema",
    "bad_value",
    "bad_candidate",
    "bad_null_candidate_basis",
    "bad_id",
    "bad_round",
    "round_mismatch",
    "unknown_reference",
    "illegal_transition",
    "illegal_resolution_basis",
    "null_candidate_decision_basis",
    "wrong_session_uuid",
    "candidate_mismatch_in_batch",
    "op_key_conflict",
    "op_key_partial",
    "duplicate_op_key",
    "empty_batch",
    "bad_expected_head",
)

EMPTY_HEAD = ""
LOCK_TIMEOUT_S = 15.0
LOCK_POLL_S = 0.01

_HEX64 = re.compile(r"[0-9a-f]{64}")

_CALLER_COMMON = ("kind", "session_uuid", "phase", "round", "candidate")
_STORE_KEYS = ("schema", "seq", "id", "prev_digest", "digest")
_EVENT_KINDS = ("finding", "recommendation", "resolution", "retraction", "reopen")
_BATCH_SAME_CANDIDATE_KINDS = ("round", "finding", "recommendation")
_NULL_CANDIDATE_KINDS = ("amendment", "escalation_resume")

# reasons that a stored-record read reports under their own name; every other
# reason is folded into one of the two generic read codes below.
_DIRECT_READ_CODES = (
    "wrong_schema",
    "unknown_kind",
    "unknown_key",
    "wrong_session_uuid",
    "duplicate_id",
    "seq_gap",
    "digest_break",
)
_TRANSITION_REASONS = (
    "illegal_transition",
    "illegal_resolution_basis",
    "null_candidate_decision_basis",
)

_tmp_counter = [0]


class AuthorityError(Exception):
    """Base of every error this module raises."""


class AuthorityWriteError(AuthorityError):
    """The store could not complete a write; nothing was committed."""


class AuthorityReadError(AuthorityWriteError):
    """The chain is unreadable or corrupt. `.code` is a READ_ERROR_CODES member."""

    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = detail


class AuthorityCommitUncertain(AuthorityWriteError):
    """The commit outcome could not be established; retry with the same op_key."""


class AuthorityRecordRefused(AuthorityError):
    """A record was refused. `.reason` is a REFUSAL_REASONS member."""

    def __init__(self, reason, detail=""):
        super().__init__("%s: %s" % (reason, detail) if detail else reason)
        self.reason = reason
        self.detail = detail


class AuthorityHeadConflict(AuthorityError):
    """expected_head did not match; `.actual` is None for an empty chain."""

    def __init__(self, expected, actual):
        super().__init__("expected head %r, chain head is %r" % (expected, actual))
        self.expected = expected
        self.actual = actual


class _Bad(Exception):
    """Internal refusal carrying a reason and a detail; never leaves the module."""

    def __init__(self, reason, detail=""):
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail


def _bad(reason, path, note=""):
    raise _Bad(reason, "%s: %s" % (path, note) if note else path)


# ---------------------------------------------------------------- primitives


def canonical_json(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def record_digest(record):
    body = {key: value for key, value in record.items() if key != "digest"}
    return hashlib.sha256(canonical_json(body).encode("ascii")).hexdigest()


def record_id(kind, seq):
    return "%s-%04d" % (ID_PREFIXES[kind], seq)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nes(value):
    return isinstance(value, str) and value != ""


def _is_hex(value):
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _is_json(value):
    if value is None or isinstance(value, (bool, str)) or _is_int(value):
        return True
    if isinstance(value, list):
        return all(_is_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v) for k, v in value.items())
    return False


# ------------------------------------------------------------------ checkers
# Each checker is fn(value, path) and raises _Bad.


def _c_nes(value, path):
    if not _is_nes(value):
        _bad("bad_value", path)


def _c_nes_null(value, path):
    if value is not None and not _is_nes(value):
        _bad("bad_value", path)


def _c_str(value, path):
    if not isinstance(value, str):
        _bad("bad_value", path)


def _c_bool(value, path):
    if not isinstance(value, bool):
        _bad("bad_value", path)


def _c_hex(value, path):
    if not _is_hex(value):
        _bad("bad_value", path)


def _c_int0(value, path):
    if not _is_int(value) or value < 0:
        _bad("bad_value", path)


def _c_enum(values):
    def check(value, path):
        if not isinstance(value, str) or value not in values:
            _bad("bad_value", path)

    return check


def _c_const(expected):
    def check(value, path):
        if not isinstance(value, str) or value != expected:
            _bad("bad_value", path)

    return check


def _c_list(item, nonempty=False):
    def check(value, path):
        if not isinstance(value, list) or (nonempty and not value):
            _bad("bad_value", path)
        for index, entry in enumerate(value):
            item(entry, "%s[%d]" % (path, index))

    return check


def _c_obj(spec):
    def check(value, path):
        if not isinstance(value, dict):
            _bad("bad_value", path)
        if not all(isinstance(key, str) for key in value):
            _bad("bad_value", path, "non-string key")
        for key in spec:
            if key not in value:
                _bad("missing_key", "%s.%s" % (path, key))
        for key in value:
            if key not in spec:
                _bad("unknown_key", "%s.%s" % (path, key))
        for key, fn in spec.items():
            fn(value[key], "%s.%s" % (path, key))

    return check


def _c_claim_class(value, path):
    if value is not None and (not isinstance(value, str) or value not in CLAIM_CLASSES):
        _bad("bad_value", path)


def _c_candidate(value, path):
    if not p1a.validate_candidate(value)[0]:
        _bad("bad_candidate", path)


def _c_expected_witness(value, path):
    if _is_nes(value):
        return
    if isinstance(value, dict) and _is_json(value):
        return
    _bad("bad_value", path)


def _c_decision_basis(value, path):
    if value is not None and value != "status_absent":
        _bad("bad_null_candidate_basis", path)


_NES_LIST = _c_list(_c_nes)
_CAPABILITY_LIST = _c_list(_c_enum(CAPABILITIES))

_SPECS = {
    "round": {
        "seat": _c_nes,
        "verdict_sha256": _c_hex,
        "review_path": _c_nes,
        "verdict_copy_path": _c_nes,
        "verdict_copy_sha256": _c_hex,
    },
    "finding": {
        "round_id": _c_nes,
        "summary": _c_str,
        "severity": _c_nes,
        "blocking": _c_bool,
        "criterion": _c_str,
        "evidence_path": _c_nes,
        "evidence_sha256": _c_hex,
        "claim_class": _c_claim_class,
        "discoverer": _c_nes,
        "superseded_by_transaction": _c_nes_null,
    },
    "proposal": {
        "finding_ids": _c_list(_c_nes, nonempty=True),
        "changed_evidence": _c_obj(
            {
                "paths": _NES_LIST,
                "sha256s": _c_list(_c_hex),
                "candidate": _c_candidate,
            }
        ),
        "author_role": _c_enum(_AUTHOR_ROLES),
        "requires": _CAPABILITY_LIST,
    },
    "recommendation": {
        "finding_id": _c_nes,
        "recommend": _c_enum(_RECOMMEND),
        "reviewer_seat": _c_nes,
        "round_id": _c_nes,
    },
    "resolution": {
        "finding_id": _c_nes,
        "outcome": _c_const("closed"),
        "decided_by": _c_const("control_plane"),
        "basis": _c_obj(
            {
                "proposal_id": _c_nes_null,
                "recommendation_id": _c_nes_null,
                "decision_id": _c_nes_null,
                "witness_ids": _NES_LIST,
            }
        ),
    },
    "retraction": {
        "finding_id": _c_nes,
        "reason": _c_enum(_RETRACT_REASONS),
        "duplicate_of": _c_nes_null,
        "by": _c_enum(_RETRACTION_BY),
    },
    "reopen": {
        "finding_id": _c_nes,
        "round_id": _c_nes,
        "by": _c_const("reviewer"),
    },
    "decision": {
        "request_id": _c_nes,
        "request_kind": _c_nes,
        "request_role": _c_nes,
        "principal": _c_enum(_DECISION_PRINCIPALS),
        "answer_sha256": _c_hex,
        "adjudicates": _c_list(
            _c_obj(
                {
                    "finding_id": _c_nes,
                    "outcome": _c_enum(_ADJUDICATION_OUTCOMES),
                }
            )
        ),
        "null_candidate_basis": _c_decision_basis,
    },
    "amendment": {
        "context_revision": _c_int0,
        "context_sha256": _c_hex,
        "principal": _c_enum(PRINCIPALS),
        "applies_to_phase": _c_enum(PHASES),
        "supersedes": _c_nes_null,
        "grants": _CAPABILITY_LIST,
    },
    "escalation": {
        "required_capability": _c_enum(CAPABILITIES),
        "target_principal": _c_const("policy_principal"),
        "proposal_id": _c_nes,
        "request_id": _c_nes,
        "resume_token_sha256": _c_hex,
    },
    "escalation_resume": {
        "escalation_id": _c_nes,
        "request_id": _c_nes,
        "principal": _c_enum(PRINCIPALS),
        "answer_sha256": _c_hex,
        "outcome": _c_enum(_RESUME_OUTCOMES),
        "grants": _CAPABILITY_LIST,
    },
    "witness": {
        "claim_id": _c_nes,
        "claim_class": _c_claim_class,
        "probe": _c_obj({"kind": _c_nes, "ref": _c_nes, "author_seat": _c_nes}),
        "expected_witness": _c_expected_witness,
        "result": None,  # replaced below: null or an exact object
        "not_executable_reason": _c_nes_null,
    },
    "reconciliation": {
        "role": _c_nes,
        "work_id": _c_nes,
        "artifact_path": _c_nes,
        "old_sha256": _c_hex,
        "new_sha256": _c_hex,
        "original_decision": _c_nes,
        "reconciled_outcome": _c_nes,
        "activity_ref": _c_nes_null,
    },
}

_WITNESS_RESULT = _c_obj(
    {
        "outcome": _c_enum(WITNESS_OUTCOMES),
        "evidence_path": _c_nes,
        "evidence_sha256": _c_hex,
    }
)


def _c_witness_result(value, path):
    if value is not None:
        _WITNESS_RESULT(value, path)


_SPECS["witness"]["result"] = _c_witness_result

# keys a caller may omit, per kind, besides op_key
_OPTIONAL_KEYS = {"amendment": ("grants",)}


def _optional_keys(kind):
    return ("op_key",) + _OPTIONAL_KEYS.get(kind, ())


# ---------------------------------------------------------- record validation


def _structure(record, committed):
    if not isinstance(record, dict):
        _bad("not_an_object", "record")
    if not all(isinstance(key, str) for key in record):
        _bad("bad_value", "record", "non-string key")
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in _SPECS:
        _bad("unknown_kind", "kind")
    if committed and "schema" in record:
        schema = record["schema"]
        if not _is_int(schema) or schema != SCHEMA:
            _bad("wrong_schema", "schema")
    optional = _optional_keys(kind)
    required = set(_CALLER_COMMON)
    required.update(key for key in _SPECS[kind] if key not in optional)
    if committed:
        required.update(_STORE_KEYS)
    for key in sorted(required):
        if key not in record:
            _bad("missing_key", key)
    allowed = required.union(optional)
    for key in sorted(record):
        if key not in allowed:
            _bad("unknown_key", key)


def _values(record, committed):
    kind = record["kind"]
    if committed:
        seq = record["seq"]
        if not _is_int(seq) or seq < 1:
            _bad("bad_value", "seq")
        if record["id"] != record_id(kind, seq):
            _bad("bad_id", "id")
        prev = record["prev_digest"]
        if prev != "" and not _is_hex(prev):
            _bad("bad_value", "prev_digest")
        _c_hex(record["digest"], "digest")
    _c_nes(record["session_uuid"], "session_uuid")
    _c_enum(PHASES)(record["phase"], "phase")
    if "op_key" in record:
        _c_nes(record["op_key"], "op_key")
    rnd = record["round"]
    if kind == "round":
        if committed:
            if not _is_int(rnd) or rnd < 1:
                _bad("bad_round", "round")
        elif rnd is not None:
            _bad("bad_round", "round", "a round record's number is minted by the store")
    elif rnd is not None and (not _is_int(rnd) or rnd < 1):
        _bad("bad_round", "round")
    for key, fn in _SPECS[kind].items():
        if key in record:
            fn(record[key], key)
    _kind_rules(record)
    _candidate_rule(record)


def _kind_rules(record):
    kind = record["kind"]
    if kind == "finding":
        expected = (
            record["severity"] == "blocking"
            and record["superseded_by_transaction"] is None
        )
        if record["blocking"] != expected:
            _bad(
                "bad_value",
                "blocking",
                "must equal (severity == 'blocking' and not superseded)",
            )
    elif kind == "retraction":
        duplicate_of = record["duplicate_of"]
        if record["reason"] == "duplicate" and duplicate_of is None:
            _bad("bad_value", "duplicate_of", "required for a duplicate")
        if record["reason"] == "withdrawn" and duplicate_of is not None:
            _bad("bad_value", "duplicate_of", "must be null for a withdrawal")
    elif kind == "witness":
        if (record["result"] is None) != (record["not_executable_reason"] is not None):
            _bad(
                "bad_value",
                "result",
                "null iff not_executable_reason is set",
            )
    elif kind == "escalation_resume":
        if record["outcome"] == "decline" and record["grants"] != []:
            _bad("bad_value", "grants", "a decline grants nothing")


def _candidate_rule(record):
    kind = record["kind"]
    candidate = record["candidate"]
    if kind == "decision":
        basis = record["null_candidate_basis"]
        if candidate is None:
            if basis != "status_absent":
                _bad(
                    "bad_null_candidate_basis",
                    "null_candidate_basis",
                    "a null candidate needs basis 'status_absent'",
                )
            if record["adjudicates"] != []:
                _bad(
                    "bad_null_candidate_basis",
                    "adjudicates",
                    "a null-candidate decision adjudicates nothing",
                )
            return
        if basis is not None:
            _bad(
                "bad_null_candidate_basis",
                "null_candidate_basis",
                "basis 'status_absent' needs a null candidate",
            )
    elif candidate is None:
        if kind in _NULL_CANDIDATE_KINDS:
            return
        _bad("bad_candidate", "candidate", "null is not allowed on %s" % kind)
    if not p1a.validate_candidate(candidate)[0]:
        _bad("bad_candidate", "candidate")


def validate_record(record, committed=False):
    """(ok, reason): a pure schema check of one record with no chain context.

    committed=False validates a caller body (no store keys, a round record's
    number absent); committed=True validates a stored record.
    """
    try:
        _structure(record, committed)
        _values(record, committed)
    except _Bad as bad:
        return (False, bad.reason)
    return (True, None)


# --------------------------------------------------------------- the walker


class _Walker:
    """Chain-so-far state and the single place record legality lives."""

    def __init__(self):
        self.count = 0
        self.head = None
        self.session = None
        self.records = []
        self.ids = set()
        self.by_id = {}
        self.op_keys = {}
        self.round_counts = {}
        self.round_keys = set()
        self.round_rows = {}
        self.rounds = []
        self.findings = {}
        self.events = []
        self.proposals = {}
        self.recommendations = {}
        self.decisions = {}
        self.witnesses = {}
        self.grants = []

    # ---- feeding
    def feed(self, record):
        _structure(record, True)
        kind = record["kind"]
        seq = record["seq"]
        if not _is_int(seq) or seq != self.count + 1:
            _bad("seq_gap", "seq", "expected %d" % (self.count + 1))
        rid = record["id"]
        if isinstance(rid, str) and rid in self.ids:
            _bad("duplicate_id", "id", rid)
        if rid != record_id(kind, seq):
            _bad("bad_id", "id")
        if self.session is not None and record["session_uuid"] != self.session:
            _bad("wrong_session_uuid", "session_uuid")
        if record["prev_digest"] != (self.head or ""):
            _bad("digest_break", "prev_digest")
        try:
            digest = record_digest(record)
        except (TypeError, ValueError):
            _bad("bad_value", "record", "not canonical JSON")
        if record["digest"] != digest:
            _bad("digest_break", "digest")
        _values(record, True)
        self._check_round_refs(record)
        key = record.get("op_key")
        if key is not None and key in self.op_keys:
            _bad("duplicate_op_key", "op_key")
        getattr(self, "_do_" + kind)(record)
        self.count += 1
        self.head = record["digest"]
        self.session = record["session_uuid"]
        self.ids.add(rid)
        self.by_id[rid] = record
        if key is not None:
            self.op_keys[key] = record
        self.records.append(record)

    # ---- round references
    def _check_round_refs(self, record):
        kind = record["kind"]
        rnd = record["round"]
        phase = record["phase"]
        if kind == "round":
            minted = self.round_counts.get((phase, record["seat"]), 0) + 1
            if rnd != minted:
                _bad("bad_round", "round", "expected %d" % minted)
            return
        if kind in _EVENT_KINDS and rnd is None:
            _bad("bad_round", "round", "%s needs an integer round" % kind)
        if rnd is not None and (phase, rnd) not in self.round_keys:
            _bad("bad_round", "round", "no minted round %r in %s" % (rnd, phase))
        if kind in ("finding", "recommendation", "reopen"):
            row = self.round_rows.get(record["round_id"])
            if row is None:
                _bad("unknown_reference", "round_id", str(record["round_id"]))
            if row["phase"] != phase or row["round"] != rnd:
                _bad("round_mismatch", "round_id", str(record["round_id"]))

    # ---- lookups
    def _cited(self, ident, kind, path):
        record = self.by_id.get(ident)
        if record is None or record["kind"] != kind:
            _bad("unknown_reference", path, str(ident))
        return record

    def _finding(self, ident):
        row = self.findings.get(ident)
        if row is None:
            _bad("unknown_reference", "finding_id", str(ident))
        return row

    def _event(self, record, finding_id, kind):
        self.events.append(
            {
                "seq": record["seq"],
                "round": record["round"],
                "finding_id": finding_id,
                "kind": kind,
                "id": record["id"],
            }
        )

    # ---- per-kind legality and folding
    def _do_round(self, record):
        key = (record["phase"], record["seat"])
        self.round_counts[key] = record["round"]
        self.round_keys.add((record["phase"], record["round"]))
        row = {
            "round_id": record["id"],
            "phase": record["phase"],
            "seat": record["seat"],
            "round": record["round"],
            "candidate": copy.deepcopy(record["candidate"]),
        }
        self.round_rows[record["id"]] = row
        self.rounds.append(row)

    def _do_finding(self, record):
        self.findings[record["id"]] = {
            "state": "open",
            "phase": record["phase"],
            "candidate": copy.deepcopy(record["candidate"]),
            "opened_round": record["round"],
            "last_transition_id": record["id"],
            "blocking": record["blocking"],
            "claim_class": record["claim_class"],
            "session_uuid": record["session_uuid"],
        }
        self._event(record, record["id"], "opened")

    def _do_proposal(self, record):
        self.proposals[record["id"]] = {
            "finding_ids": list(record["finding_ids"]),
            "candidate": copy.deepcopy(record["candidate"]),
            "changed_evidence": copy.deepcopy(record["changed_evidence"]),
            "author_role": record["author_role"],
            "requires": list(record["requires"]),
        }

    def _do_recommendation(self, record):
        self.recommendations.setdefault(record["finding_id"], []).append(
            {
                "id": record["id"],
                "recommend": record["recommend"],
                "round_id": record["round_id"],
                "reviewer_seat": record["reviewer_seat"],
            }
        )

    def _do_resolution(self, record):
        row = self._finding(record["finding_id"])
        if row["state"] != "open":
            _bad("illegal_transition", "finding_id", "resolution needs an open finding")
        self._check_basis(record)
        row["state"] = "closed"
        row["last_transition_id"] = record["id"]
        self._event(record, record["finding_id"], "closed")

    def _check_basis(self, record):
        finding_id = record["finding_id"]
        basis = record["basis"]
        proposal_ok = recommendation_ok = decision_ok = False
        if basis["proposal_id"] is not None:
            proposal = self._cited(basis["proposal_id"], "proposal", "basis.proposal_id")
            if finding_id not in proposal["finding_ids"]:
                _bad("illegal_resolution_basis", "basis.proposal_id", "other finding")
            if not p1a.candidate_equal(
                proposal["changed_evidence"]["candidate"], record["candidate"]
            ):
                _bad(
                    "illegal_resolution_basis",
                    "basis.proposal_id",
                    "changed_evidence.candidate differs from the resolution candidate",
                )
            proposal_ok = True
        if basis["recommendation_id"] is not None:
            rec = self._cited(
                basis["recommendation_id"], "recommendation", "basis.recommendation_id"
            )
            if rec["finding_id"] != finding_id or rec["recommend"] != "closed":
                _bad(
                    "illegal_resolution_basis",
                    "basis.recommendation_id",
                    "not a closed recommendation for this finding",
                )
            recommendation_ok = True
        if basis["decision_id"] is not None:
            decision = self._cited(basis["decision_id"], "decision", "basis.decision_id")
            if decision["candidate"] is None:
                _bad(
                    "null_candidate_decision_basis",
                    "basis.decision_id",
                    "a null-candidate decision is never a basis",
                )
            if {"finding_id": finding_id, "outcome": "close"} not in decision[
                "adjudicates"
            ]:
                _bad(
                    "illegal_resolution_basis",
                    "basis.decision_id",
                    "the decision does not close this finding",
                )
            decision_ok = True
        for index, witness_id in enumerate(basis["witness_ids"]):
            self._cited(witness_id, "witness", "basis.witness_ids[%d]" % index)
        if not (decision_ok or (proposal_ok and recommendation_ok)):
            _bad(
                "illegal_resolution_basis",
                "basis",
                "needs a decision, or a proposal with a recommendation",
            )

    def _do_retraction(self, record):
        row = self._finding(record["finding_id"])
        if row["state"] != "open":
            _bad("illegal_transition", "finding_id", "retraction needs an open finding")
        row["state"] = record["reason"]
        row["last_transition_id"] = record["id"]
        self._event(record, record["finding_id"], record["reason"])

    def _do_reopen(self, record):
        row = self._finding(record["finding_id"])
        if row["state"] != "closed":
            _bad("illegal_transition", "finding_id", "reopen needs a closed finding")
        row["state"] = "open"
        row["last_transition_id"] = record["id"]
        self._event(record, record["finding_id"], "reopened")

    def _do_decision(self, record):
        self.decisions[record["id"]] = {
            "request_id": record["request_id"],
            "principal": record["principal"],
            "answer_sha256": record["answer_sha256"],
            "adjudicates": copy.deepcopy(record["adjudicates"]),
            "candidate": copy.deepcopy(record["candidate"]),
        }

    def _do_amendment(self, record):
        for token in record.get("grants", []):
            self.grants.append({"id": record["id"], "capability": token})

    def _do_escalation(self, record):
        pass

    def _do_escalation_resume(self, record):
        if record["outcome"] == "grant":
            for token in record["grants"]:
                self.grants.append({"id": record["id"], "capability": token})

    def _do_witness(self, record):
        self.witnesses.setdefault(record["claim_id"], []).append(
            {
                "id": record["id"],
                "claim_class": record["claim_class"],
                "candidate": copy.deepcopy(record["candidate"]),
                "result": copy.deepcopy(record["result"]),
            }
        )

    def _do_reconciliation(self, record):
        pass

    # ---- result
    def folded(self):
        return copy.deepcopy(
            {
                "decisions": self.decisions,
                "events": self.events,
                "findings": self.findings,
                "grants": self.grants,
                "head": self.head,
                "proposals": self.proposals,
                "recommendations": self.recommendations,
                "rounds": self.rounds,
                "session_uuid": self.session,
                "witnesses": self.witnesses,
            }
        )


def _read_code(reason):
    if reason in _DIRECT_READ_CODES:
        return reason
    if reason in _TRANSITION_REASONS:
        return "illegal_transition"
    return "illegal_record"


def _feed_stored(walker, record):
    try:
        walker.feed(record)
    except _Bad as bad:
        raise AuthorityReadError(_read_code(bad.reason), bad.detail) from None


# ------------------------------------------------------------- read and fold


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %r" % key)
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError("non-JSON constant %s" % name)


def _parse_chain_bytes(raw):
    if raw == b"":
        return []
    lines = raw.split(b"\n")
    if lines[-1] != b"":
        raise AuthorityReadError("torn_line", "missing final newline")
    records = []
    for number, line in enumerate(lines[:-1], start=1):
        try:
            text = line.decode("utf-8")
            record = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
            canonical = canonical_json(record)
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            raise AuthorityReadError("torn_line", "line %d" % number) from None
        if not isinstance(record, dict) or canonical != text:
            raise AuthorityReadError("torn_line", "line %d is not canonical" % number)
        records.append(record)
    return records


def _load(raw):
    """(records, walker) for chain bytes; raises AuthorityReadError."""
    walker = _Walker()
    records = _parse_chain_bytes(raw)
    for record in records:
        _feed_stored(walker, record)
    return records, walker


def _read_bytes(path):
    """The chain bytes, or None when the file is absent."""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _readback(path):
    """The post-replace readback only; the file must exist."""
    with open(path, "rb") as handle:
        return handle.read()


def fold(records):
    """The folded state of a list of stored records; raises AuthorityReadError."""
    if not isinstance(records, list):
        raise AuthorityReadError("illegal_record", "records must be a list")
    walker = _Walker()
    for record in copy.deepcopy(records):
        _feed_stored(walker, record)
    return walker.folded()


def read_chain(path):
    """{ok, records, head, error, folded}; ok False never carries a partial view."""
    try:
        raw = _read_bytes(path)
    except OSError:
        return _read_failed("unreadable")
    try:
        records, walker = _load(raw if raw is not None else b"")
    except AuthorityReadError as err:
        return _read_failed(err.code)
    return {
        "ok": True,
        "records": records,
        "head": walker.head,
        "error": None,
        "folded": walker.folded(),
    }


def _read_failed(code):
    return {"ok": False, "records": [], "head": None, "error": code, "folded": None}


# ------------------------------------------------------------ lock and commit


def _acquire_lock(path):
    try:
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as err:
        raise AuthorityWriteError("cannot open the chain lock: %s" % err) from err
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise AuthorityWriteError("timed out waiting for the chain lock")
            time.sleep(LOCK_POLL_S)
        except OSError as err:
            os.close(fd)
            raise AuthorityWriteError("cannot lock the chain: %s" % err) from err


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tmp_path(path):
    _tmp_counter[0] += 1
    directory, name = os.path.split(os.path.abspath(path))
    return os.path.join(directory, ".%s.tmp-%d-%d" % (name, os.getpid(), _tmp_counter[0]))


def _write_tmp(tmp, data):
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _rollback(path, old_raw, directory, cause):
    """Restore the original bytes; AuthorityWriteError if proven, else uncertain."""
    tmp = None
    try:
        if old_raw is None:
            os.unlink(path)
        else:
            tmp = _tmp_path(path)
            _write_tmp(tmp, old_raw)
            os.replace(tmp, path)
            tmp = None
        try:
            _fsync_dir(directory)
        except OSError:
            pass
        if _read_bytes(path) != old_raw:
            raise OSError("restored bytes differ from the original")
    except Exception as err:
        if tmp is not None:
            _unlink_quiet(tmp)
        raise AuthorityCommitUncertain(
            "commit outcome unknown (%s); rollback failed (%s)" % (cause, err)
        ) from cause
    raise AuthorityWriteError("commit failed and was rolled back: %s" % cause) from cause


def _write_atomically(path, old_raw, new_raw, new_head, new_count):
    """Replace the chain with new_raw; return the stored new records, re-read."""
    directory = os.path.dirname(os.path.abspath(path))
    tmp = _tmp_path(path)
    try:
        _write_tmp(tmp, new_raw)
        os.replace(tmp, path)
    except Exception as err:
        _unlink_quiet(tmp)
        raise AuthorityWriteError("commit failed before the replace: %s" % err) from err
    try:
        _fsync_dir(directory)
        raw = _readback(path)
        if raw != new_raw:
            raise OSError("readback differs from the written bytes")
        records, walker = _load(raw)
        if walker.head != new_head or len(records) < new_count:
            raise OSError("readback chain head differs")
    except Exception as err:
        _rollback(path, old_raw, directory, err)
    return records[len(records) - new_count :]


def _refuse(bad):
    raise AuthorityRecordRefused(bad.reason, bad.detail) from None


def _prepare(bodies):
    if not isinstance(bodies, list):
        raise AuthorityRecordRefused("not_an_object", "a batch is a list of records")
    if not bodies:
        raise AuthorityRecordRefused("empty_batch", "nothing to append")
    prepared = []
    seen = set()
    for body in bodies:
        if not isinstance(body, dict):
            raise AuthorityRecordRefused("not_an_object", "record")
        body = copy.deepcopy(body)
        try:
            _structure(body, False)
            _values(body, False)
        except _Bad as bad:
            _refuse(bad)
        key = body.get("op_key")
        if key is not None:
            if key in seen:
                raise AuthorityRecordRefused("duplicate_op_key", key)
            seen.add(key)
        prepared.append(body)
    return prepared


def _body_view(committed):
    view = {k: v for k, v in committed.items() if k not in _STORE_KEYS}
    if view["kind"] == "round":
        view["round"] = None
    return view


def _replay(walker, bodies):
    """Committed copies when every record is an op_key replay, else None."""
    matched = 0
    for body in bodies:
        key = body.get("op_key")
        stored = walker.op_keys.get(key) if key is not None else None
        if stored is None:
            continue
        if _body_view(stored) != body:
            raise AuthorityRecordRefused(
                "op_key_conflict", "op_key %s was committed with a different body" % key
            )
        matched += 1
    if matched == 0:
        return None
    if matched != len(bodies):
        raise AuthorityRecordRefused(
            "op_key_partial", "only some records of the batch were committed"
        )
    return [copy.deepcopy(walker.op_keys[body["op_key"]]) for body in bodies]


def _check_head(walker, expected_head):
    if expected_head is None:
        return
    if expected_head == EMPTY_HEAD:
        if walker.head is not None:
            raise AuthorityHeadConflict(expected_head, walker.head)
    elif expected_head != walker.head:
        raise AuthorityHeadConflict(expected_head, walker.head)


def _check_batch_candidates(bodies):
    reference = None
    for body in bodies:
        if body["kind"] not in _BATCH_SAME_CANDIDATE_KINDS:
            continue
        if reference is None:
            reference = body["candidate"]
        elif not p1a.candidate_equal(reference, body["candidate"]):
            raise AuthorityRecordRefused(
                "candidate_mismatch_in_batch",
                "round/finding/recommendation records bind different candidates",
            )


def _commit(path, bodies, expected_head):
    if expected_head is not None and not isinstance(expected_head, str):
        raise AuthorityRecordRefused("bad_expected_head", repr(expected_head))
    prepared = _prepare(bodies)
    lock_fd = _acquire_lock(path)
    try:
        try:
            old_raw = _read_bytes(path)
        except OSError as err:
            raise AuthorityReadError("unreadable", str(err)) from err
        _, walker = _load(old_raw if old_raw is not None else b"")
        replayed = _replay(walker, prepared)
        if replayed is not None:
            return replayed
        _check_head(walker, expected_head)
        _check_batch_candidates(prepared)
        lines = []
        for body in prepared:
            record = dict(body)
            record["schema"] = SCHEMA
            record["seq"] = walker.count + 1
            record["id"] = record_id(record["kind"], record["seq"])
            record["prev_digest"] = walker.head or ""
            if record["kind"] == "round":
                key = (record["phase"], record["seat"])
                record["round"] = walker.round_counts.get(key, 0) + 1
            record["digest"] = record_digest(record)
            try:
                walker.feed(record)
            except _Bad as bad:
                _refuse(bad)
            lines.append(canonical_json(record) + "\n")
        new_raw = (old_raw or b"") + "".join(lines).encode("ascii")
        return _write_atomically(path, old_raw, new_raw, walker.head, len(prepared))
    finally:
        os.close(lock_fd)


def append(path, record, expected_head=None, op_key=None):
    """Commit one record; return the committed record. Never returns None."""
    if op_key is not None:
        if not isinstance(op_key, str) or op_key == "":
            raise AuthorityRecordRefused("bad_value", "op_key")
        if isinstance(record, dict):
            if "op_key" in record and record["op_key"] != op_key:
                raise AuthorityRecordRefused(
                    "op_key_conflict", "record op_key differs from the argument"
                )
            record = dict(record, op_key=op_key)
    return _commit(path, [record], expected_head)[0]


def append_batch(path, records, expected_head=None):
    """Commit records all or nothing; return the committed records in order."""
    return _commit(path, records, expected_head)
