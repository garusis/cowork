#!/usr/bin/env python3
"""Prior-green binding comparer for owned verification.

A correction that changes nothing the verification measured (only session
artifacts moved) may reuse the exact prior green terminal transaction instead
of running a new one. This module is the PURE comparer that decides whether a
stored transaction may be bound: the caller reads the pointer, the stored
terminal result and the stored request, computes the live candidate identity
and the current inventory identity, and passes them in.

`bind_prior_green` binds only the exact prior green terminal transaction of an
identical candidate and inventory, and otherwise returns a closed refusal code.
It never raises and never fabricates a result: on success the returned result
is a copy of the stored one. Profile gating (which sessions may enter this path
at all) and the writing of the pointer's `inventory_identity` field belong to
the caller.

Pure stdlib; it imports no other cowork module.
"""

import copy
import hashlib
import json

IDENTITY_VERSION = 1

BINDABLE_DISPOSITIONS = ("pending_review", "superseded_by_finding")
BINDABLE_FINAL_BINDINGS = ("ran_once", "components_ran_once",
                           "reused_dependency_bound")
ALLOWED_REUSE_MODES = ("dependency_digest",)
VERDICT_GREEN = "green"
# The closed tokens a caller computes for the open findings of the correction
# round. `verification_challenge` means a STILL-OPEN validly cited challenge
# (a defeated one is not tokenised); the other two are typed finding classes.
OPEN_FINDING_CLASSES = ("verification_challenge", "architectural",
                        "signal_malformed")

REFUSAL_CODES = (
    "no_pointer", "legacy_pointer_no_identity", "reuse_mode_not_allowed",
    "result_unreadable", "request_mismatch", "session_mismatch", "not_green",
    "deferred", "binding_not_final", "disposition_not_bindable",
    "verification_finding_open", "manifest_mismatch", "index_mismatch",
    "inventory_mismatch",
)

_HEX = frozenset("0123456789abcdef")


def _is_hex64(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX for c in value))


def compute_inventory_identity(schema, entries, suite_decl, configuration):
    """The sha256 hex identity of an approved inventory: canonical JSON of the
    schema, the normalized entries (order preserved: execution is serial), the
    suite declaration and the configuration. Callers pass the PRE-reuse
    normalized entries (a stored request holds only the executed ones). Returns
    None, never raises, for input that is not JSON-serializable; a None
    identity never binds."""
    try:
        blob = json.dumps(
            {"identity_version": IDENTITY_VERSION, "schema": schema,
             "entries": entries, "suite": suite_decl,
             "configuration": configuration},
            sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _snapshot_digests(document):
    snapshot = document.get("snapshot") if isinstance(document, dict) else None
    if not isinstance(snapshot, dict):
        return None
    return snapshot.get("manifest_digest"), snapshot.get("index_digest")


def _open_findings_refuse(open_finding_classes):
    """True unless `open_finding_classes` is an EMPTY list, tuple, set or
    frozenset. A closed token, an unknown token, None and any other container
    or non-container input all refuse (fail closed)."""
    if not isinstance(open_finding_classes, (list, tuple, set, frozenset)):
        return True
    return len(open_finding_classes) > 0


def _refuse(code):
    return None, code


def _bind(pointer, stored_result, stored_request, live_identity,
          current_inventory_identity, reuse_mode, session_uuid, disposition,
          open_finding_classes):
    if not isinstance(pointer, dict) or not pointer:
        return _refuse("no_pointer")
    if not _is_hex64(pointer.get("inventory_identity")):
        return _refuse("legacy_pointer_no_identity")
    if reuse_mode not in ALLOWED_REUSE_MODES:
        return _refuse("reuse_mode_not_allowed")
    transaction_id = pointer.get("transaction_id")
    if (not isinstance(stored_result, dict)
            or not isinstance(transaction_id, str) or not transaction_id
            or stored_result.get("transaction_id") != transaction_id):
        return _refuse("result_unreadable")
    result_digests = _snapshot_digests(stored_result)
    if (not isinstance(stored_request, dict)
            or stored_request.get("transaction_id") != transaction_id
            or result_digests is None
            or _snapshot_digests(stored_request) != result_digests):
        return _refuse("request_mismatch")
    if (not isinstance(session_uuid, str) or not session_uuid
            or stored_request.get("session_uuid") != session_uuid):
        return _refuse("session_mismatch")
    if (stored_result.get("verdict") != VERDICT_GREEN
            or stored_result.get("mutation")
            or stored_result.get("ledger_failure")
            or stored_result.get("startup_failure")
            or stored_result.get("result_persistence_failed")):
        return _refuse("not_green")
    if stored_result.get("deferred_reconciliation"):
        return _refuse("deferred")
    if stored_result.get("final_suite_binding") not in BINDABLE_FINAL_BINDINGS:
        return _refuse("binding_not_final")
    disposition = (disposition if disposition is not None
                   else pointer.get("disposition"))
    if disposition not in BINDABLE_DISPOSITIONS:
        return _refuse("disposition_not_bindable")
    if _open_findings_refuse(open_finding_classes):
        return _refuse("verification_finding_open")
    if (not isinstance(live_identity, (tuple, list)) or len(live_identity) != 2
            or not all(_is_hex64(d) for d in live_identity)):
        return _refuse("manifest_mismatch")
    live_manifest, live_index = live_identity
    if not (live_manifest == result_digests[0]
            == pointer.get("manifest_digest")):
        return _refuse("manifest_mismatch")
    if not (live_index == result_digests[1] == pointer.get("index_digest")):
        return _refuse("index_mismatch")
    if (not _is_hex64(current_inventory_identity)
            or current_inventory_identity != pointer["inventory_identity"]):
        return _refuse("inventory_mismatch")
    bound = copy.deepcopy(stored_result)
    bound["bound_reuse"] = True
    bound["bound_prior_transaction_id"] = transaction_id
    bound["reused_lock_result"] = False
    return bound, None


def bind_prior_green(pointer, stored_result, stored_request, live_identity,
                     current_inventory_identity, reuse_mode, session_uuid=None,
                     disposition=None, open_finding_classes=()):
    """``(bound_result, None)`` when the stored terminal transaction named by
    `pointer` may be reused for the live candidate, else ``(None, code)`` with
    `code` in REFUSAL_CODES. Pure and never raises: any unexpected failure on
    corrupt input is a refusal.

    - `pointer`: the current-receipt pointer dict (needs transaction_id,
      manifest_digest, index_digest, inventory_identity and, unless
      `disposition` is given, disposition).
    - `stored_result` / `stored_request`: the stored terminal result and
      request of that transaction as read from disk.
    - `live_identity`: the live candidate ``(manifest_digest, index_digest)``;
      it must equal the stored result snapshot AND the pointer.
    - `current_inventory_identity`: `compute_inventory_identity` of the CURRENT
      approved inventory; it must equal the pointer's `inventory_identity`.
    - `reuse_mode`: the resolved profile reuse mode; only ``dependency_digest``
      may bind.
    - `session_uuid`: the session the request must belong to.
    - `open_finding_classes`: tokens from OPEN_FINDING_CLASSES for the open
      findings of the round; any entry refuses.

    The bound result is a deep copy of the stored one marked `bound_reuse`,
    `bound_prior_transaction_id` and ``reused_lock_result`` False."""
    try:
        return _bind(pointer, stored_result, stored_request, live_identity,
                     current_inventory_identity, reuse_mode, session_uuid,
                     disposition, open_finding_classes)
    except Exception:  # noqa: BLE001 - corrupt input is a refusal, not a raise
        return _refuse("result_unreadable")
