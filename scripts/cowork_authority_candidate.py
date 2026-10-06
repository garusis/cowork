#!/usr/bin/env python3
"""The ONE candidate object, its validation, equality and per-phase selection.

A candidate names the exact artifact a gate, decision or authority record is
about. It is one of two plain-dict shapes and nothing else:

    {"kind": "owned_receipt", "manifest_digest": <64 lowercase hex>,
     "index_digest": <64 lowercase hex>}
    {"kind": "status_artifact", "status_sha256": <64 lowercase hex>}

`owned_receipt` digests are the owned-verification snapshot/receipt manifest
and index digests. The dispatch-manifest digest
(`cowork_dispatch_manifest.manifest_digest`, bound to
`WorkUnit.candidate_manifest_digest`), `cowork_state.manifest_digest` and
`cowork._current_tree_digest` are NEVER candidates, and a caller must not read
the WorkUnit field named `candidate_manifest_digest` as one.

PURE BY CONSTRUCTION. Every input is an already-read value: nothing here opens
a file, reads the environment or a clock, or imports another cowork module. No
input is mutated and no result aliases an input; a candidate is always a fresh
dict holding identity keys only.

CLOSED REASONS. `REASON_CODES` is the whole registry. `validate_candidate`
refuses None, so a null candidate is never valid here; any record-level
exception that tolerates a null candidate belongs to the caller that writes
that record, not to this module.

Python 3.9+, stdlib only.
"""

import re

CANDIDATE_KINDS = ("owned_receipt", "status_artifact")

REASON_CODES = (
    "candidate_unavailable",
    "candidate_kind_mismatch",
    "candidate_changed",
    "owned_receipt",
    "pointer_absent",
    "checkpoint_pointer_ignored",
    "pointer_malformed",
    "status_unavailable",
)

_OWNED_RECEIPT_KEYS = frozenset({"kind", "manifest_digest", "index_digest"})
_STATUS_ARTIFACT_KEYS = frozenset({"kind", "status_sha256"})
_KEYS_BY_KIND = {
    "owned_receipt": _OWNED_RECEIPT_KEYS,
    "status_artifact": _STATUS_ARTIFACT_KEYS,
}

PHASE_ROLES = {
    "scouting": ("scout", "scout-reviewer"),
    "planning": ("planner", "planning-advisor"),
    "building": ("builder", "build-reviewer"),
}

_HEX64 = re.compile(r"[0-9a-f]{64}")


def _is_hex64(value):
    # fullmatch, not "^...$": "$" would accept a trailing newline.
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def validate_candidate(obj):
    """(True, None) for a valid candidate, else (False, 'candidate_unavailable')."""
    refused = (False, "candidate_unavailable")
    if not isinstance(obj, dict):
        return refused
    kind = obj.get("kind")
    if not isinstance(kind, str) or kind not in _KEYS_BY_KIND:
        return refused
    if set(obj.keys()) != _KEYS_BY_KIND[kind]:
        return refused
    for key in _KEYS_BY_KIND[kind]:
        if key != "kind" and not _is_hex64(obj[key]):
            return refused
    return (True, None)


def candidate_compare(a, b):
    """(equal, reason): (True, None), or (False, one of candidate_unavailable /
    candidate_kind_mismatch / candidate_changed)."""
    if not validate_candidate(a)[0] or not validate_candidate(b)[0]:
        return (False, "candidate_unavailable")
    if a["kind"] != b["kind"]:
        return (False, "candidate_kind_mismatch")
    for key in _KEYS_BY_KIND[a["kind"]]:
        if a[key] != b[key]:
            return (False, "candidate_changed")
    return (True, None)


def candidate_equal(a, b):
    return candidate_compare(a, b)[0]


def _status_candidate(fingerprint):
    """A fresh status_artifact candidate from a fingerprint_status-shaped dict,
    or None when it carries no usable status bytes."""
    if not isinstance(fingerprint, dict) or not fingerprint.get("exists"):
        return None
    sha = fingerprint.get("sha256")
    if not _is_hex64(sha):
        return None
    return {"kind": "status_artifact", "status_sha256": sha}


def _status_row(fingerprint, reason):
    candidate = _status_candidate(fingerprint)
    if candidate is None:
        return (None, "status_unavailable")
    return (candidate, reason)


def _select_building(pointer_read, status_fingerprint):
    if not isinstance(pointer_read, dict):
        return (None, "pointer_malformed")
    if not pointer_read.get("exists"):
        return _status_row(status_fingerprint, "pointer_absent")
    data = pointer_read.get("data")
    if not isinstance(data, dict):
        return (None, "pointer_malformed")
    if data.get("transaction_id"):
        manifest_digest = data.get("manifest_digest")
        index_digest = data.get("index_digest")
        if _is_hex64(manifest_digest) and _is_hex64(index_digest):
            return (
                {
                    "kind": "owned_receipt",
                    "manifest_digest": manifest_digest,
                    "index_digest": index_digest,
                },
                "owned_receipt",
            )
        return (None, "pointer_malformed")
    if data.get("checkpoint_id"):
        return _status_row(status_fingerprint, "checkpoint_pointer_ignored")
    return (None, "pointer_malformed")


def select_candidate(phase, role, pointer_read, status_fingerprint):
    """(candidate | None, reason) for a gate in `phase` held by `role`.

    pointer_read is {"exists": bool, "data": dict | None}; status_fingerprint is
    the fingerprint_status dict or None. Scouting and planning ignore the
    pointer and use the status artifact; success there carries reason None.
    Building prefers the owned receipt named by the pointer and falls back to
    the status artifact only for an absent or checkpoint-shaped pointer. A
    malformed pointer is reported before the status is consulted.
    """
    roles = PHASE_ROLES.get(phase) if isinstance(phase, str) else None
    if roles is None or not isinstance(role, str) or role not in roles:
        return (None, "candidate_unavailable")
    if phase == "building":
        return _select_building(pointer_read, status_fingerprint)
    candidate = _status_candidate(status_fingerprint)
    if candidate is None:
        return (None, "status_unavailable")
    return (candidate, None)
