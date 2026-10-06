#!/usr/bin/env python3
"""Claim classes, claim markers and the witness screen.

A criterion or step may carry a marker, `claim_classes`: a non-empty list drawn
from CLAIM_CLASSES (`source_of_truth`, `only`, `never`, `fail_closed`). A marked
claim is a universal statement, so approving it needs a witness: a probe
authored by someone other than the builder that tried to break the claim and
recorded what it found against the gate candidate. `screen_witnesses` decides
whether every marked claim has such a witness. Only an executed
`bypass_not_found` witness can approve a claim; an executed `bypass_found`
always blocks it; a probe that could not be executed yields
`insufficient_evidence`; everything else yields `revise`.

WITNESS ROW. The screen reads one closed row shape, never a folded view and
never a raw chain record:

    {"claim_id": str, "claim_class": str, "candidate": <candidate>,
     "probe": {"kind": str, "ref": str, "author_seat": str},
     "expected_witness": str,
     "result": None | {"outcome": str, "evidence_path": str,
                       "evidence_sha256": <64 lowercase hex>},
     "not_executable_reason": None | str,
     "id": str | int   # optional passthrough}

A caller that holds full chain records projects them down to this row first. A
row with any other key, including any record-envelope key, is malformed.

REJECTION ORDER. Each row gets at most one code; the first failing stage wins:
shape, claim class, candidate, builder seat, result consistency, outcome,
evidence. Every pure stage runs before the injected evidence reader, so the
reader is called once for an otherwise valid executed row and never for any
other row.

WHAT IS CHECKED AND WHAT IS NOT. Machine-checked: candidate equality with the
gate candidate, the probe author seat against the builder seat, the evidence
file hash against the recorded hash, and the closed set of outcomes. Judged by
the reviewer: whether the probe is adequate and whether its author is truly
independent. Cowork cannot verify that the probe actually ran against the tree;
it checks only that the row names the gate candidate and that the evidence hash
matches. The same split is carried in every screen result as `judgment_split`.

`scan_unmarked_claims` is a lexical aid only: it returns hits for work items
that read like universal claims but carry no valid marker. It never changes a
screen result, never blocks, and has false positives by design.

PURE BY CONSTRUCTION. Nothing here opens a file, reads the environment or a
clock; evidence hashes arrive through an injected `read_file_sha256(path)` that
returns 64 lowercase hex or None. The one dependency is
`cowork_authority_candidate`, whose `candidate_compare` is the only candidate
equality used. Every public function is total: bad input is returned as a coded
reason, never raised. No input is mutated and no result aliases an input or a
module constant.

Python 3.9+, stdlib only.
"""

import re

import cowork_authority_candidate

CLAIM_CLASSES = ("source_of_truth", "only", "never", "fail_closed")
WITNESS_OUTCOMES = ("bypass_not_found", "bypass_found")

REASON_CODES = (
    "claims_malformed",
    "claim_class_unknown",
    "gate_candidate_unavailable",
    "builder_seat_unavailable",
    "read_file_sha256_unavailable",
    "witness_missing",
    "witness_malformed",
    "witness_class_mismatch",
    "witness_candidate_unavailable",
    "witness_candidate_kind_mismatch",
    "witness_candidate_changed",
    "witness_builder_authored",
    "witness_result_inconsistent",
    "witness_outcome_invalid",
    "witness_evidence_missing",
    "witness_evidence_sha_mismatch",
    "witness_bypass_found",
    "witness_not_executable",
)

MARKER_CODES = (
    "marker_items_malformed",
    "marker_item_malformed",
    "marker_classes_malformed",
    "marker_unknown_class",
    "marker_duplicate_class",
)

MACHINE_CHECKED = (
    "candidate_equality",
    "author_seat",
    "evidence_file_sha256",
    "closed_outcomes",
)
REVIEWER_JUDGED = ("probe_adequacy", "author_independence")
UNVERIFIABLE_LIMIT = (
    "Cowork cannot verify that the probe actually ran against the tree: it "
    "checks only that the witness row names the gate candidate and that the "
    "evidence file hash matches the recorded hash."
)

SCAN_TERMS = (
    ("only", "only"),
    ("sole", "only"),
    ("solely", "only"),
    ("exclusive", "only"),
    ("exclusively", "only"),
    ("never", "never"),
    ("cannot", "never"),
    ("impossible", "never"),
    ("must not", "never"),
    ("fail closed", "fail_closed"),
    ("fail-closed", "fail_closed"),
    ("source of truth", "source_of_truth"),
    ("single source", "source_of_truth"),
)

_ROW_KEYS = frozenset({
    "claim_id", "claim_class", "candidate", "probe", "expected_witness",
    "result", "not_executable_reason",
})
_ROW_OPTIONAL_KEYS = frozenset({"id"})
_PROBE_KEYS = frozenset({"kind", "ref", "author_seat"})
_RESULT_KEYS = frozenset({"outcome", "evidence_path", "evidence_sha256"})

_CANDIDATE_CODES = {
    "candidate_unavailable": "witness_candidate_unavailable",
    "candidate_kind_mismatch": "witness_candidate_kind_mismatch",
    "candidate_changed": "witness_candidate_changed",
}

_HEX64 = re.compile(r"[0-9a-f]{64}")
_WORD_EDGE_BEFORE = r"(?<![A-Za-z0-9_])"
_WORD_EDGE_AFTER = r"(?![A-Za-z0-9_])"
_SCAN_PATTERNS = tuple(
    (
        term,
        cls,
        re.compile(
            _WORD_EDGE_BEFORE
            + r"\s+".join(re.escape(part) for part in term.split(" "))
            + _WORD_EDGE_AFTER,
            re.IGNORECASE,
        ),
    )
    for term, cls in SCAN_TERMS
)


def _is_hex64(value):
    # fullmatch, not "^...$": "$" would accept a trailing newline.
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _is_text(value):
    return isinstance(value, str) and value != ""


def judgment_split():
    """A fresh dict describing the machine-checked / reviewer-judged split."""
    return {
        "machine_checked": list(MACHINE_CHECKED),
        "reviewer_judged": list(REVIEWER_JUDGED),
        "unverifiable": UNVERIFIABLE_LIMIT,
    }


def _result(override, reasons):
    """The only screen-result builder. approvable is exactly `override is None`;
    reasons are copied and are empty exactly when the screen is approvable."""
    approvable = override is None
    return {
        "approvable": approvable,
        "verdict_override": override,
        "reasons": [] if approvable else [
            {"claim_id": claim_id, "code": code} for claim_id, code in reasons
        ],
        "judgment_split": judgment_split(),
    }


def _classes_problems(classes):
    """Marker codes for a claim_classes value, in fixed order, each at most once."""
    if not isinstance(classes, list) or not classes:
        return ["marker_classes_malformed"]
    problems = []
    texts = [c for c in classes if isinstance(c, str)]
    if len(texts) != len(classes):
        problems.append("marker_classes_malformed")
    if any(c not in CLAIM_CLASSES for c in texts):
        problems.append("marker_unknown_class")
    if len(set(texts)) != len(texts):
        problems.append("marker_duplicate_class")
    return problems


def validate_claim_markers(items):
    """[{index, code}] for marker problems in `items`; empty means valid. An item
    with no claim_classes (absent or None) is unmarked, which is valid."""
    if not isinstance(items, list):
        return [{"index": None, "code": "marker_items_malformed"}]
    problems = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append({"index": index, "code": "marker_item_malformed"})
            continue
        classes = item.get("claim_classes")
        if classes is None:
            continue
        for code in _classes_problems(classes):
            problems.append({"index": index, "code": code})
    return problems


def _scan_texts(item):
    for key, value in item.items():
        if key == "claim_classes":
            continue
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for element in value:
                if isinstance(element, str):
                    yield element


def scan_unmarked_claims(items):
    """Advisory hits [{index, classes, terms}] for dict items without a valid
    marker whose text reads like a universal claim. Never blocks anything."""
    if not isinstance(items, list):
        return []
    hits = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("claim_classes") is not None and not _classes_problems(
                item["claim_classes"]):
            continue
        texts = list(_scan_texts(item))
        found_terms = []
        found_classes = set()
        for term, cls, pattern in _SCAN_PATTERNS:
            if any(pattern.search(text) for text in texts):
                found_terms.append(term)
                found_classes.add(cls)
        if found_terms:
            hits.append({
                "index": index,
                "classes": [c for c in CLAIM_CLASSES if c in found_classes],
                "terms": found_terms,
            })
    return hits


def _row_shape_ok(row):
    keys = set(row.keys())
    if not (_ROW_KEYS <= keys <= (_ROW_KEYS | _ROW_OPTIONAL_KEYS)):
        return False
    if "id" in row:
        row_id = row["id"]
        if isinstance(row_id, bool) or not isinstance(row_id, (str, int)):
            return False
    if not _is_text(row["claim_id"]) or not _is_text(row["claim_class"]):
        return False
    if not _is_text(row["expected_witness"]):
        return False
    probe = row["probe"]
    if not isinstance(probe, dict) or set(probe.keys()) != _PROBE_KEYS:
        return False
    if not all(_is_text(probe[key]) for key in _PROBE_KEYS):
        return False
    result = row["result"]
    if result is not None:
        if not isinstance(result, dict) or set(result.keys()) != _RESULT_KEYS:
            return False
        if not _is_text(result["evidence_path"]):
            return False
        if not _is_hex64(result["evidence_sha256"]):
            return False
    reason = row["not_executable_reason"]
    if reason is not None and not isinstance(reason, str):
        return False
    return True


def _read_evidence(reader, path):
    try:
        return reader(path)
    except Exception:  # an unreadable file is a missing file, never a raise
        return None


def _classify(row, classes, gate_candidate, builder_seat, reader):
    """(code | None, pinned_class | None, kind | None) for one attributable row.

    kind is 'found', 'not_found' or 'not_executable' for an accepted row. A
    rejected row pinned to a class passed the shape and class stages."""
    if not _row_shape_ok(row):
        return ("witness_malformed", None, None)
    claim_class = row["claim_class"]
    if claim_class not in classes:
        return ("witness_class_mismatch", None, None)
    equal, why = cowork_authority_candidate.candidate_compare(
        row["candidate"], gate_candidate)
    if not equal:
        return (_CANDIDATE_CODES.get(why, "witness_candidate_unavailable"),
                claim_class, None)
    if row["probe"]["author_seat"] == builder_seat:
        return ("witness_builder_authored", claim_class, None)
    result = row["result"]
    reason = row["not_executable_reason"]
    if result is None:
        if not _is_text(reason):
            return ("witness_result_inconsistent", claim_class, None)
        return (None, claim_class, "not_executable")
    if reason is not None:
        return ("witness_result_inconsistent", claim_class, None)
    outcome = result["outcome"]
    if not isinstance(outcome, str) or outcome not in WITNESS_OUTCOMES:
        return ("witness_outcome_invalid", claim_class, None)
    actual = _read_evidence(reader, result["evidence_path"])
    if not _is_hex64(actual):
        return ("witness_evidence_missing", claim_class, None)
    if actual != result["evidence_sha256"]:
        return ("witness_evidence_sha_mismatch", claim_class, None)
    return (None, claim_class,
            "found" if outcome == "bypass_found" else "not_found")


def _claim_problem(claim, seen):
    """None for a usable claim entry, else (claim_id | None, fault code)."""
    if not isinstance(claim, dict):
        return (None, "claims_malformed")
    claim_id = claim.get("claim_id")
    if not _is_text(claim_id):
        return (None, "claims_malformed")
    if claim_id in seen:
        return (claim_id, "claims_malformed")
    seen.add(claim_id)
    classes = claim.get("claim_classes")
    if not isinstance(classes, list) or not classes:
        return (claim_id, "claims_malformed")
    if not all(isinstance(c, str) for c in classes) or len(set(classes)) != len(classes):
        return (claim_id, "claims_malformed")
    if any(c not in CLAIM_CLASSES for c in classes):
        return (claim_id, "claim_class_unknown")
    return None


def _faults(claims, witnesses, gate_candidate, builder_seat, reader):
    faults = []
    if not isinstance(claims, list):
        faults.append((None, "claims_malformed"))
    else:
        seen = set()
        for claim in claims:
            problem = _claim_problem(claim, seen)
            if problem is not None:
                faults.append(problem)
    if not cowork_authority_candidate.validate_candidate(gate_candidate)[0]:
        faults.append((None, "gate_candidate_unavailable"))
    if not _is_text(builder_seat):
        faults.append((None, "builder_seat_unavailable"))
    if not callable(reader):
        faults.append((None, "read_file_sha256_unavailable"))
    if not isinstance(witnesses, list):
        faults.append((None, "witness_malformed"))
    else:
        for row in witnesses:
            if not isinstance(row, dict) or not isinstance(row.get("claim_id"), str):
                faults.append((None, "witness_malformed"))
    return faults


def _decide_pair(classified, claim_class):
    """('approved' | 'insufficient' | 'revise', reason codes) for one pair.

    classified is the claim's rows as (code, pinned_class, kind) in row order."""
    mine = [c for c in classified if c[1] == claim_class and c[0] is None]
    kinds = [c[2] for c in mine]
    if "found" in kinds:
        return ("revise", ["witness_bypass_found"])
    if "not_found" in kinds:
        return ("approved", [])
    rejected = []
    for code, pinned, _kind in classified:
        if code is not None and pinned in (None, claim_class) and code not in rejected:
            rejected.append(code)
    if "not_executable" in kinds:
        if not rejected:
            return ("insufficient", ["witness_not_executable"])
        return ("revise", rejected + ["witness_not_executable"])
    if rejected:
        return ("revise", rejected)
    return ("revise", ["witness_missing"])


def screen_witnesses(claims, witnesses, gate_candidate, builder_seat,
                     read_file_sha256):
    """Decide whether every marked claim has an approving witness.

    claims: [{claim_id, claim_classes}] (extra keys ignored); witnesses: the
    closed rows described in the module docstring; gate_candidate: the
    candidate the gate is about; builder_seat: the builder's seat name;
    read_file_sha256: path -> 64 lowercase hex | None.

    Returns {approvable, verdict_override, reasons: [{claim_id, code}],
    judgment_split}. verdict_override is None exactly when approvable, else
    'revise' or 'insufficient_evidence'. Never raises."""
    try:
        return _screen(claims, witnesses, gate_candidate, builder_seat,
                       read_file_sha256)
    except Exception:  # fail closed: an unexpected fault is a revise
        return _result("revise", [(None, "witness_malformed")])


def _screen(claims, witnesses, gate_candidate, builder_seat, reader):
    faults = _faults(claims, witnesses, gate_candidate, builder_seat, reader)
    if faults:
        return _result("revise", faults)

    rows_by_claim = {claim["claim_id"]: [] for claim in claims}
    for row in witnesses:
        bucket = rows_by_claim.get(row["claim_id"])
        if bucket is not None:
            bucket.append(row)

    reasons = []
    seen = set()
    any_revise = False
    any_insufficient = False
    for claim in claims:
        claim_id = claim["claim_id"]
        classes = list(claim["claim_classes"])
        classified = [
            _classify(row, classes, gate_candidate, builder_seat, reader)
            for row in rows_by_claim[claim_id]
        ]
        for claim_class in classes:
            status, codes = _decide_pair(classified, claim_class)
            if status == "revise":
                any_revise = True
            elif status == "insufficient":
                any_insufficient = True
            for code in codes:
                if (claim_id, code) not in seen:
                    seen.add((claim_id, code))
                    reasons.append((claim_id, code))
    if any_revise:
        return _result("revise", reasons)
    if any_insufficient:
        return _result("insufficient_evidence", reasons)
    return _result(None, [])
