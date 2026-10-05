#!/usr/bin/env python3
"""Closed bounded-correction building blocks: the compact correction packet
schema, the risk class of a correction, its fixed review/verification envelope
and the review-scope decision.

Pure stdlib apart from a read-only use of `cowork_execution_profiles`
(`classify_path`, `is_bytecode_byproduct`). Nothing here schedules, writes
files, mints ids or stamps times: a later stage owns when a packet is written
and who reads it. Profile gating is the CALLER's responsibility: the decision
helpers only ever resolve toward the full review scope, and `decide_review_scope`
takes the resolved profile policy as an explicit input (None = unprofiled).

## Packet kinds

- ``correction``: the lead-facing record of one correction round. Its risk
  class and review scope are COMPUTED here from the measured delta and the
  open findings; a caller cannot assert them.
- ``finding_import``: the first record of a replacement session, carrying the
  unresolved findings of a source session verbatim (their ids are never
  re-minted).

A packet links existing ids only (request, authority, candidate digests,
verification transaction, disposition). It cannot represent a closed finding
or an approval: the only entry state is ``open`` and the keys ``closed``,
``closes``, ``closure``, ``approve``, ``approved`` and ``verdict`` are rejected
by name. Closure and approval come only from the paired reviewer's verdict.

## Risk classes

Computed from the delta and the findings only. A role's own claim about the
class is ignored, and anything that cannot be measured resolves toward the
strongest class.
"""

import copy
import hashlib
import types

import cowork_execution_profiles as _profiles

CORRECTION_SCHEMA_VERSION = 1

KIND_CORRECTION = "correction"
KIND_FINDING_IMPORT = "finding_import"
PACKET_KINDS = (KIND_CORRECTION, KIND_FINDING_IMPORT)

PHASES = ("scouting", "planning", "building")
# Ordered most severe first.
SEVERITIES = ("blocking", "major", "minor")
EVIDENCE_STATES = ("verified", "missing", "sha_mismatch")
# The single entry state: a packet can only ever carry an OPEN finding.
FINDING_STATES = ("open",)
CORRECTION_OUTCOMES = ("pending", "addressed", "partial", "unresolved",
                       "scope_escape")
DISPOSITIONS = ("pending_review", "accepted", "superseded_by_finding",
                "rejected")

RISK_ARTIFACT_ONLY = "artifact_only"
RISK_DOCUMENTATION_METADATA = "documentation_metadata"
RISK_FOCUSED_CODE = "focused_code"
RISK_ARCHITECTURAL = "architectural"
# Ordered weakest to strongest.
RISK_CLASSES = (RISK_ARTIFACT_ONLY, RISK_DOCUMENTATION_METADATA,
                RISK_FOCUSED_CODE, RISK_ARCHITECTURAL)

SCOPE_TARGETED = "targeted"
SCOPE_FULL = "full"
REVIEW_SCOPES = (SCOPE_TARGETED, SCOPE_FULL)

REASON_TARGETED_OK = "targeted_ok"
REASON_ASSURANCE_FULL = "assurance_full"
REASON_ARCHITECTURAL_FULL = "architectural_full"
REASON_EXECUTABLE_DELTA_FULL = "executable_delta_full"
REASON_PRIOR_REF_MISSING_FULL = "prior_ref_missing_full"
REASON_PRIOR_REF_MISMATCH_FULL = "prior_ref_mismatch_full"
REASON_REVIEWER_FAILURE_FULL = "reviewer_failure_full"
REASON_MALFORMED_SIGNAL_FULL = "malformed_signal_full"
REASON_SCOPE_ESCAPE_FULL = "scope_escape_full"
REASON_SEVERITY_THRESHOLD_FULL = "severity_threshold_full"
REASON_CODES = (
    REASON_TARGETED_OK, REASON_ASSURANCE_FULL, REASON_ARCHITECTURAL_FULL,
    REASON_EXECUTABLE_DELTA_FULL, REASON_PRIOR_REF_MISSING_FULL,
    REASON_PRIOR_REF_MISMATCH_FULL, REASON_REVIEWER_FAILURE_FULL,
    REASON_MALFORMED_SIGNAL_FULL, REASON_SCOPE_ESCAPE_FULL,
    REASON_SEVERITY_THRESHOLD_FULL,
)

FORBIDDEN_PACKET_KEYS = ("closed", "closes", "closure", "approve", "approved",
                         "verdict")

PACKET_REJECT_CODES = (
    "packet_not_object", "schema_version_unknown", "kind_unknown",
    "field_unknown", "field_missing", "field_type", "phase_unknown",
    "outcome_unknown", "risk_class_unknown", "scope_unknown",
    "reason_code_unknown", "severity_unknown", "evidence_state_unknown",
    "state_unknown", "forbidden_field", "link_not_token",
    "kind_field_mismatch", "unresolved_basis_invalid",
)

_PROFILES_WITH_TARGETED_REVIEW = (_profiles.PROFILE_LIGHT,
                                  _profiles.PROFILE_STANDARD)
_REUSE_MODE_DEPENDENCY_DIGEST = "dependency_digest"
_UNRESOLVED_NONE = "none"

_MAX_TOKEN = 256
_MAX_CRITERION = 1000
_MAX_PATH = 4096
_HEX = frozenset("0123456789abcdef")


def _is_hex64(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX for c in value))


def _is_token(value):
    return (isinstance(value, str) and 0 < len(value) <= _MAX_TOKEN
            and not any(c.isspace() for c in value))


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0


# --------------------------------------------------------------------------- #
# Envelopes.                                                                  #
# --------------------------------------------------------------------------- #

def _envelope(ceiling, verification, binding_eligible, new_transaction,
              complete_final_validation):
    return types.MappingProxyType({
        "reviewer_verdict_required": True,
        "review_scope_ceiling": ceiling,
        "verification": verification,
        "binding_eligible": binding_eligible,
        "new_transaction": new_transaction,
        "complete_final_validation": complete_final_validation,
    })


# The reviewer verdict is required for every class: the ceiling only bounds
# how much is re-read, never whether the paired reviewer decides. The ceiling
# is an upper bound that `decide_review_scope` may lower and never exceeds.
CORRECTION_ENVELOPES = types.MappingProxyType({
    RISK_ARTIFACT_ONLY: _envelope(
        SCOPE_TARGETED, "prior_green_binding", True, False, False),
    RISK_DOCUMENTATION_METADATA: _envelope(
        SCOPE_TARGETED, "dependency_digest_reuse", False, True, True),
    RISK_FOCUSED_CODE: _envelope(
        SCOPE_TARGETED, "focused_labels_plus_complete_final_validation",
        False, True, True),
    RISK_ARCHITECTURAL: _envelope(
        SCOPE_FULL, "full_workflow", False, True, True),
})


def envelope_for(risk_class):
    """A copy of the fixed envelope of `risk_class`; ValueError when unknown."""
    if risk_class not in CORRECTION_ENVELOPES:
        raise ValueError("unknown risk class: %r" % (risk_class,))
    return dict(CORRECTION_ENVELOPES[risk_class])


# --------------------------------------------------------------------------- #
# Evidence state.                                                             #
# --------------------------------------------------------------------------- #

def evidence_state_for(path, expected_sha256):
    """``verified`` when the file at `path` hashes to `expected_sha256`,
    ``sha_mismatch`` when it hashes to something else, and ``missing`` when the
    path is not readable or the expected digest is not a 64-hex sha256. A
    finding is flagged by this state, never dropped for it."""
    if not _is_hex64(expected_sha256):
        return "missing"
    if not isinstance(path, str) or not path:
        return "missing"
    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except (OSError, ValueError):
        return "missing"
    return "verified" if digest == expected_sha256 else "sha_mismatch"


# --------------------------------------------------------------------------- #
# Risk class.                                                                 #
# --------------------------------------------------------------------------- #

def _measure_delta(delta):
    """The normalized delta, or None when it cannot be measured. An
    inconsistent delta is unmeasurable (never artifact_only): bytecode
    byproducts are dropped first, executable paths must be a subset of the
    changed paths, and `candidate_unchanged` must agree with them."""
    if not isinstance(delta, dict):
        return None
    changed = delta.get("changed_paths")
    executable = delta.get("executable_paths")
    unchanged = delta.get("candidate_unchanged")
    if not isinstance(changed, (list, tuple)):
        return None
    if not isinstance(executable, (list, tuple)):
        return None
    if not isinstance(unchanged, bool):
        return None
    if not all(isinstance(p, str) for p in tuple(changed) + tuple(executable)):
        return None
    changed = [p for p in changed if not _profiles.is_bytecode_byproduct(p)]
    executable = [p for p in executable
                  if not _profiles.is_bytecode_byproduct(p)]
    if not set(executable) <= set(changed):
        return None
    if unchanged and changed:
        return None
    if not unchanged and not changed:
        return None
    dependency_hit = delta.get("dependency_hit")
    return {"changed": changed, "executable": executable,
            "unchanged": unchanged,
            "dependency_hit": (dependency_hit
                               if isinstance(dependency_hit, bool) else None)}


def _assess(delta, findings, signals):
    """Everything the class and the scope decision read, in one place."""
    assessment = {"measured": _measure_delta(delta), "malformed": False,
                  "escape": False, "architectural": False}
    if signals is not None and not isinstance(signals, dict):
        assessment["malformed"] = True
    elif signals:
        if signals.get("signal_malformed"):
            assessment["malformed"] = True
        if signals.get("scope_escape"):
            assessment["escape"] = True
    if findings is None:
        findings = ()
    if not isinstance(findings, (list, tuple)):
        assessment["malformed"] = True
        findings = ()
    for finding in findings:
        if not isinstance(finding, dict):
            assessment["malformed"] = True
            continue
        if finding.get("severity") not in SEVERITIES:
            assessment["malformed"] = True
        tag = finding.get("risk_class")
        if tag is None:
            continue
        if tag == RISK_ARCHITECTURAL:
            assessment["architectural"] = True
        else:
            assessment["malformed"] = True
    return assessment


def _class_of(assessment):
    measured = assessment["measured"]
    if (assessment["malformed"] or assessment["escape"]
            or assessment["architectural"] or measured is None):
        return RISK_ARCHITECTURAL
    if measured["unchanged"]:
        return RISK_ARTIFACT_ONLY
    if measured["executable"]:
        return RISK_FOCUSED_CODE
    if (measured["dependency_hit"] is False and all(
            _profiles.classify_path(p) == "documentation"
            for p in measured["changed"])):
        return RISK_DOCUMENTATION_METADATA
    return RISK_FOCUSED_CODE


def classify_correction(delta, findings, signals=None):
    """The risk class of a correction, computed from the measured delta and the
    open findings only.

    `delta` is ``{changed_paths, executable_paths, candidate_unchanged,
    dependency_hit}`` (repo-relative paths; `candidate_unchanged` means the live
    candidate identity equals the last green verified identity). Any claim of a
    class carried by a finding or a signal is ignored; a typed ``architectural``
    tag can only raise the class. A delta that cannot be measured, a malformed
    finding or signal and a scope escape are all ``architectural``."""
    return _class_of(_assess(delta, findings, signals))


# --------------------------------------------------------------------------- #
# Review scope.                                                               #
# --------------------------------------------------------------------------- #

def _scope(scope, reason_code):
    return {"scope": scope, "reason_code": reason_code}


def decide_review_scope(policy, delta, findings, signals=None,
                        prior_reviewed=None, reviewer_failure=False):
    """``{scope: targeted|full, reason_code}`` for the next review of a
    correction. Targeted is returned only when every accepted condition holds;
    any doubt resolves to full. First match wins:

    reviewer failure; policy missing / not light-or-standard / not
    dependency-digest reuse; scope escape; malformed signal; an architectural
    class (the class whose envelope ceiling is full); an open finding whose
    severity is not in the policy's revise severities; an executable delta; a
    prior reviewed reference that is missing or does not verify.

    `policy` is the dict `cowork_execution_profiles.resolved_vertex_policy`
    returns, or None (unprofiled). `prior_reviewed` is
    ``{recorded_manifest_digest, verified_manifest_digest}``."""
    try:
        return _decide_review_scope(policy, delta, findings, signals,
                                    prior_reviewed, reviewer_failure)
    except Exception:  # noqa: BLE001 - any doubt resolves toward full scope
        return _scope(SCOPE_FULL, REASON_MALFORMED_SIGNAL_FULL)


def _decide_review_scope(policy, delta, findings, signals, prior_reviewed,
                         reviewer_failure):
    if reviewer_failure:
        return _scope(SCOPE_FULL, REASON_REVIEWER_FAILURE_FULL)
    if (not isinstance(policy, dict)
            or policy.get("profile") not in _PROFILES_WITH_TARGETED_REVIEW
            or policy.get("reuse_mode") != _REUSE_MODE_DEPENDENCY_DIGEST):
        return _scope(SCOPE_FULL, REASON_ASSURANCE_FULL)
    assessment = _assess(delta, findings, signals)
    if assessment["escape"]:
        return _scope(SCOPE_FULL, REASON_SCOPE_ESCAPE_FULL)
    if assessment["malformed"] or assessment["measured"] is None:
        return _scope(SCOPE_FULL, REASON_MALFORMED_SIGNAL_FULL)
    risk_class = _class_of(assessment)
    if envelope_for(risk_class)["review_scope_ceiling"] == SCOPE_FULL:
        return _scope(SCOPE_FULL, REASON_ARCHITECTURAL_FULL)
    thresholds = policy.get("thresholds")
    revise = (thresholds.get("revise_severities")
              if isinstance(thresholds, dict) else None)
    if not isinstance(revise, (list, tuple)):
        return _scope(SCOPE_FULL, REASON_SEVERITY_THRESHOLD_FULL)
    if any(f["severity"] not in revise for f in (findings or ())):
        return _scope(SCOPE_FULL, REASON_SEVERITY_THRESHOLD_FULL)
    if assessment["measured"]["executable"]:
        return _scope(SCOPE_FULL, REASON_EXECUTABLE_DELTA_FULL)
    prior = prior_reviewed if isinstance(prior_reviewed, dict) else {}
    recorded = prior.get("recorded_manifest_digest")
    verified = prior.get("verified_manifest_digest")
    if not _is_hex64(recorded) or not _is_hex64(verified):
        return _scope(SCOPE_FULL, REASON_PRIOR_REF_MISSING_FULL)
    if recorded != verified:
        return _scope(SCOPE_FULL, REASON_PRIOR_REF_MISMATCH_FULL)
    return _scope(SCOPE_TARGETED, REASON_TARGETED_OK)


# --------------------------------------------------------------------------- #
# Packet shape.                                                               #
# --------------------------------------------------------------------------- #

_TOP_KEYS = ("schema_version", "kind", "session_uuid", "phase", "role",
             "round", "risk_class", "outcome", "review_scope", "links",
             "unresolved_basis", "findings")
_LINK_KEYS = ("request_id", "authority_id", "candidate_manifest_digest",
              "candidate_index_digest", "verification_transaction_id",
              "disposition", "prior_reviewed_manifest_digest")
_LINK_DIGEST_KEYS = ("candidate_manifest_digest", "candidate_index_digest",
                     "prior_reviewed_manifest_digest")
_SCOPE_KEYS = ("scope", "reason_code")
_BASIS_KEYS = ("phase", "discoverer", "round", "rule_version")
_ENTRY_KEYS = ("finding_id", "source_finding_id", "source_session",
               "severity", "criterion", "evidence_path", "evidence_sha256",
               "evidence_state", "state")
_IMPORT_ENTRY_KEYS = ("discoverer", "round", "phase", "source_record_sha256")


class _Reject(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _check_keys(obj, required):
    forbidden = [k for k in obj if k in FORBIDDEN_PACKET_KEYS]
    if forbidden:
        raise _Reject("forbidden_field")
    if any(k not in required for k in obj):
        raise _Reject("field_unknown")
    if any(k not in obj for k in required):
        raise _Reject("field_missing")


def _check_links(links):
    if not isinstance(links, dict):
        raise _Reject("field_type")
    _check_keys(links, _LINK_KEYS)
    for key, value in links.items():
        if value is None:
            continue
        if key in _LINK_DIGEST_KEYS:
            ok = _is_hex64(value)
        elif key == "disposition":
            ok = value in DISPOSITIONS
        else:
            ok = _is_token(value)
        if not ok:
            raise _Reject("link_not_token")


def _check_review_scope(value):
    if not isinstance(value, dict):
        raise _Reject("field_type")
    _check_keys(value, _SCOPE_KEYS)
    if value["scope"] not in REVIEW_SCOPES:
        raise _Reject("scope_unknown")
    if value["reason_code"] not in REASON_CODES:
        raise _Reject("reason_code_unknown")


def _check_basis(value, findings):
    if value == _UNRESOLVED_NONE:
        if findings:
            raise _Reject("unresolved_basis_invalid")
        return
    if not isinstance(value, dict):
        raise _Reject("unresolved_basis_invalid")
    if set(value) != set(_BASIS_KEYS):
        raise _Reject("unresolved_basis_invalid")
    if (value["phase"] not in PHASES or not _is_token(value["discoverer"])
            or not _is_count(value["round"])
            or not _is_token(value["rule_version"])):
        raise _Reject("unresolved_basis_invalid")


def _check_entry(entry, kind):
    if not isinstance(entry, dict):
        raise _Reject("field_type")
    if any(k in FORBIDDEN_PACKET_KEYS for k in entry):
        raise _Reject("forbidden_field")
    if kind == KIND_CORRECTION and any(k in _IMPORT_ENTRY_KEYS
                                       for k in entry):
        raise _Reject("kind_field_mismatch")
    required = _ENTRY_KEYS + (_IMPORT_ENTRY_KEYS
                              if kind == KIND_FINDING_IMPORT else ())
    _check_keys(entry, required)
    if entry["severity"] not in SEVERITIES:
        raise _Reject("severity_unknown")
    if entry["evidence_state"] not in EVIDENCE_STATES:
        raise _Reject("evidence_state_unknown")
    if entry["state"] not in FINDING_STATES:
        raise _Reject("state_unknown")
    if kind == KIND_CORRECTION:
        if not _is_token(entry["finding_id"]):
            raise _Reject("field_type")
        for key in ("source_finding_id", "source_session"):
            if entry[key] is not None and not _is_token(entry[key]):
                raise _Reject("field_type")
    else:
        # The replacement ledger mints its own id later; an import entry never
        # carries one and always carries the verbatim source identity.
        if entry["finding_id"] is not None:
            raise _Reject("field_type")
        if (not _is_token(entry["source_finding_id"])
                or not _is_token(entry["source_session"])):
            raise _Reject("field_type")
        if (not _is_token(entry["discoverer"])
                or not _is_count(entry["round"])
                or not _is_hex64(entry["source_record_sha256"])):
            raise _Reject("field_type")
        if entry["phase"] not in PHASES:
            raise _Reject("phase_unknown")
    criterion = entry["criterion"]
    if criterion is not None and (not isinstance(criterion, str)
                                  or len(criterion) > _MAX_CRITERION):
        raise _Reject("field_type")
    path = entry["evidence_path"]
    if path is not None and (not isinstance(path, str) or not path
                             or len(path) > _MAX_PATH or "\x00" in path):
        raise _Reject("field_type")
    sha = entry["evidence_sha256"]
    if sha is not None and not _is_hex64(sha):
        raise _Reject("field_type")


def _validate(packet):
    if not isinstance(packet, dict):
        raise _Reject("packet_not_object")
    _check_keys(packet, _TOP_KEYS)
    version = packet["schema_version"]
    if (isinstance(version, bool) or not isinstance(version, int)
            or version != CORRECTION_SCHEMA_VERSION):
        raise _Reject("schema_version_unknown")
    kind = packet["kind"]
    if kind not in PACKET_KINDS:
        raise _Reject("kind_unknown")
    if not _is_token(packet["session_uuid"]) or not _is_token(packet["role"]):
        raise _Reject("field_type")
    if packet["phase"] not in PHASES:
        raise _Reject("phase_unknown")
    if not _is_count(packet["round"]):
        raise _Reject("field_type")
    findings = packet["findings"]
    if not isinstance(findings, list):
        raise _Reject("field_type")
    if kind == KIND_CORRECTION:
        if packet["risk_class"] is None:
            raise _Reject("kind_field_mismatch")
        if packet["risk_class"] not in RISK_CLASSES:
            raise _Reject("risk_class_unknown")
        if packet["outcome"] is None:
            raise _Reject("kind_field_mismatch")
        if packet["outcome"] not in CORRECTION_OUTCOMES:
            raise _Reject("outcome_unknown")
        if packet["review_scope"] is None:
            raise _Reject("kind_field_mismatch")
        _check_review_scope(packet["review_scope"])
        if packet["unresolved_basis"] is not None:
            raise _Reject("kind_field_mismatch")
    else:
        if (packet["risk_class"] is not None or packet["outcome"] is not None
                or packet["review_scope"] is not None):
            raise _Reject("kind_field_mismatch")
        _check_basis(packet["unresolved_basis"], findings)
    _check_links(packet["links"])
    for entry in findings:
        _check_entry(entry, kind)


def validate_packet(packet):
    """``(True, None)`` for a well-formed packet, else ``(False, code)`` with
    `code` in PACKET_REJECT_CODES. Never raises, whatever it is given."""
    try:
        _validate(packet)
    except _Reject as exc:
        return False, exc.code
    except Exception:  # noqa: BLE001 - a torn packet is a rejection
        return False, "field_type"
    return True, None


# --------------------------------------------------------------------------- #
# Builders (producers raise ValueError carrying the reject code).             #
# --------------------------------------------------------------------------- #

def _sha_or_none(value):
    return value if _is_hex64(value) else None


def _entry_from(finding, kind):
    path = finding.get("evidence_path")
    sha = finding.get("evidence_sha256")
    entry = {
        "finding_id": (finding.get("finding_id")
                       if kind == KIND_CORRECTION else None),
        "source_finding_id": finding.get("source_finding_id"),
        "source_session": finding.get("source_session"),
        "severity": finding.get("severity"),
        "criterion": finding.get("criterion"),
        "evidence_path": path if isinstance(path, str) and path else None,
        "evidence_sha256": _sha_or_none(sha),
        "evidence_state": evidence_state_for(path, sha),
        "state": "open",
    }
    if kind == KIND_FINDING_IMPORT:
        entry["discoverer"] = finding.get("discoverer")
        entry["round"] = finding.get("round")
        entry["phase"] = finding.get("phase")
        entry["source_record_sha256"] = finding.get("source_record_sha256")
    return entry


def _normalized_links(links):
    out = {key: None for key in _LINK_KEYS}
    if isinstance(links, dict):
        out.update(links)
    return out


def _finish(packet):
    ok, code = validate_packet(packet)
    if not ok:
        raise ValueError(code)
    return packet


def _entries(findings, kind):
    if not isinstance(findings, (list, tuple)):
        raise ValueError("field_type")
    entries = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("field_type")
        entries.append(_entry_from(finding, kind))
    return entries


def build_correction_packet(session_uuid, phase, role, round_index, delta,
                            findings, signals, links, outcome, policy,
                            prior_reviewed, reviewer_failure=False):
    """One ``correction`` packet. The risk class and the review scope are
    computed here from the inputs; no id is minted and no time is stamped.
    Every finding is kept, flagged by its evidence state when unverifiable.
    Raises ValueError carrying a PACKET_REJECT_CODES value for invalid input."""
    packet = {
        "schema_version": CORRECTION_SCHEMA_VERSION,
        "kind": KIND_CORRECTION,
        "session_uuid": session_uuid,
        "phase": phase,
        "role": role,
        "round": round_index,
        "risk_class": classify_correction(delta, findings, signals),
        "outcome": outcome,
        "review_scope": decide_review_scope(
            policy, delta, findings, signals, prior_reviewed,
            reviewer_failure),
        "links": copy.deepcopy(_normalized_links(links)),
        "unresolved_basis": None,
        "findings": _entries(findings, KIND_CORRECTION),
    }
    return _finish(packet)


def build_finding_import_packet(session_uuid, phase, role, findings,
                                unresolved_basis, links=None):
    """The ``finding_import`` packet of a replacement session (round 0). Each
    entry keeps `source_finding_id` and `source_session` verbatim and carries
    no `finding_id`: the replacement ledger mints its own. `unresolved_basis`
    is ``{phase, discoverer, round, rule_version}`` or the literal ``"none"``
    (only valid with zero findings). Raises ValueError carrying a
    PACKET_REJECT_CODES value for invalid input."""
    packet = {
        "schema_version": CORRECTION_SCHEMA_VERSION,
        "kind": KIND_FINDING_IMPORT,
        "session_uuid": session_uuid,
        "phase": phase,
        "role": role,
        "round": 0,
        "risk_class": None,
        "outcome": None,
        "review_scope": None,
        "links": copy.deepcopy(_normalized_links(links)),
        "unresolved_basis": copy.deepcopy(unresolved_basis),
        "findings": _entries(findings, KIND_FINDING_IMPORT),
    }
    return _finish(packet)
