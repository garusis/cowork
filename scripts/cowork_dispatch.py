#!/usr/bin/env python3
"""Pure stdlib DispatchContract seam — Package A foundation.

Public API:
    validate_dispatch_contract(record)
    validate_dispatch_decision(record, contract)
    validate_attempt_link(record)
    build_attempt_link_idempotency_key(role, kind, source_ref, ordinal)
    decide(contract, policy_result=None, preflight_result=None, probe_result=None)

All validators return a normalized copy or raise ValueError. They never mutate
input and reject missing or extra keys. schema_version is integer 1. Records
and input facts are ordinary JSON-native dictionaries.
"""

import math
import re
import uuid as _uuid_mod

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

_CONTRACT_KEYS = frozenset({
    "schema_version", "record", "contract_id", "role", "phase",
    "controller", "kind", "purpose", "site", "resume_session_id", "created",
})
_CONTRACT_KINDS = frozenset({"dispatch", "probe"})
_CONTRACT_PURPOSES = frozenset({
    "launch", "resume", "switch", "repair", "review", "worktree", "evaluator",
})

_DECISION_KEYS = frozenset({
    "schema_version", "record", "decision_id", "contract_id", "outcome",
    "refusal_code", "refusal_message", "source", "spawned", "trace_event_id",
})
_REFUSAL_CODES = frozenset({
    "controller_not_allowed", "controller_tool_missing",
    "probe_failed", "capability_missing",
})
_REFUSAL_SOURCES = frozenset({
    "policy_guard", "preflight", "probe", "bridge_backstop",
})

_ATTEMPT_LINK_KEYS = frozenset({
    "schema_version", "record", "attempt_id", "role", "phase", "kind",
    "source_ref", "delivery_ref", "idempotency_key", "created",
})
_ATTEMPT_LINK_KINDS = frozenset({"pending_replay", "gate_repair"})

_SOURCE_REF_KEYS = frozenset({
    "kind", "event_id", "event_name", "session_id", "prompt_sha256", "created",
})
_SOURCE_REF_KINDS = frozenset({
    "trace_event", "provider_session", "delivery_fingerprint", "legacy",
})
_SOURCE_DISCRIMINATOR_FIELDS = ("event_id", "event_name", "session_id", "prompt_sha256")

_DELIVERY_REF_KEYS = frozenset({"prompt_kind", "prompt_sha256", "prompt_bytes"})

_FACT_KEYS = frozenset({"allowed", "refusal_code", "refusal_message", "source"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_exact_keys(record, expected_keys, record_name):
    extra = set(record) - expected_keys
    missing = expected_keys - set(record)
    if missing:
        raise ValueError("%s missing keys: %s" % (record_name, sorted(missing)))
    if extra:
        raise ValueError("%s has extra keys: %s" % (record_name, sorted(extra)))


def _check_schema_version(record):
    v = record.get("schema_version")
    if isinstance(v, bool) or v != 1:
        raise ValueError("schema_version must be integer 1, got %r" % (v,))


def _check_uuid(value, field):
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ValueError("%s must be a UUID-shaped string, got %r" % (field, value))


def _check_nonempty_str(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a nonempty string, got %r" % (field, value))


def _check_str_or_null(value, field):
    if value is not None and not isinstance(value, str):
        raise ValueError("%s must be a string or null, got %r" % (field, value))


def _check_finite_epoch(value, field):
    if isinstance(value, bool):
        raise ValueError("%s must not be a boolean" % field)
    if not isinstance(value, (int, float)):
        raise ValueError(
            "%s must be a finite numeric epoch seconds, got %r" % (field, value))
    if not math.isfinite(value):
        raise ValueError("%s must be finite, got %r" % (field, value))


# ---------------------------------------------------------------------------
# DispatchContract
# ---------------------------------------------------------------------------

def validate_dispatch_contract(record):
    """Return a normalized copy of a DispatchContract record or raise ValueError.

    Never mutates input. Rejects missing or extra keys.
    """
    if not isinstance(record, dict):
        raise ValueError("DispatchContract must be a dict, got %r" % type(record))
    _check_exact_keys(record, _CONTRACT_KEYS, "DispatchContract")
    _check_schema_version(record)
    if record["record"] != "DispatchContract":
        raise ValueError(
            "record field must be 'DispatchContract', got %r" % record["record"])
    _check_uuid(record["contract_id"], "contract_id")
    _check_nonempty_str(record["role"], "role")
    _check_str_or_null(record["phase"], "phase")
    _check_nonempty_str(record["controller"], "controller")
    if record["kind"] not in _CONTRACT_KINDS:
        raise ValueError(
            "kind must be one of %s, got %r" % (sorted(_CONTRACT_KINDS), record["kind"]))
    if record["purpose"] not in _CONTRACT_PURPOSES:
        raise ValueError(
            "purpose must be one of %s, got %r"
            % (sorted(_CONTRACT_PURPOSES), record["purpose"]))
    _check_nonempty_str(record["site"], "site")
    _check_str_or_null(record["resume_session_id"], "resume_session_id")
    _check_finite_epoch(record["created"], "created")
    return dict(record)


# ---------------------------------------------------------------------------
# DispatchDecision
# ---------------------------------------------------------------------------

def validate_dispatch_decision(record, contract):
    """Return a normalized copy of a DispatchDecision record or raise ValueError.

    contract is a validated DispatchContract dict (or bare dict with
    contract_id). Rejects missing or extra keys and illegal allow/refuse
    field combinations.
    """
    if not isinstance(record, dict):
        raise ValueError("DispatchDecision must be a dict, got %r" % type(record))
    _check_exact_keys(record, _DECISION_KEYS, "DispatchDecision")
    _check_schema_version(record)
    if record["record"] != "DispatchDecision":
        raise ValueError(
            "record field must be 'DispatchDecision', got %r" % record["record"])
    _check_uuid(record["decision_id"], "decision_id")

    contract_id = contract["contract_id"] if isinstance(contract, dict) else contract
    if record["contract_id"] != contract_id:
        raise ValueError(
            "contract_id mismatch: decision has %r, contract has %r"
            % (record["contract_id"], contract_id))

    if record["outcome"] not in ("allow", "refuse"):
        raise ValueError(
            "outcome must be 'allow' or 'refuse', got %r" % record["outcome"])

    if record["outcome"] == "allow":
        if record["refusal_code"] is not None:
            raise ValueError("refusal_code must be null for allow outcome")
        if record["refusal_message"] is not None:
            raise ValueError("refusal_message must be null for allow outcome")
        if record["source"] is not None:
            raise ValueError("source must be null for allow outcome")
    else:
        if record["refusal_code"] not in _REFUSAL_CODES:
            raise ValueError(
                "refusal_code must be one of %s for refuse, got %r"
                % (sorted(_REFUSAL_CODES), record["refusal_code"]))
        if not isinstance(record["refusal_message"], str) or not record["refusal_message"]:
            raise ValueError(
                "refusal_message must be a nonempty string for refuse")
        if record["source"] not in _REFUSAL_SOURCES:
            raise ValueError(
                "source must be one of %s for refuse, got %r"
                % (sorted(_REFUSAL_SOURCES), record["source"]))

    if not isinstance(record["spawned"], bool):
        raise ValueError("spawned must be a boolean, got %r" % record["spawned"])
    _check_str_or_null(record["trace_event_id"], "trace_event_id")
    return dict(record)


# ---------------------------------------------------------------------------
# Reducer input fact validation
# ---------------------------------------------------------------------------

def _validate_fact(fact, name):
    """Validate and return a normalized copy of a reducer input fact dict."""
    if not isinstance(fact, dict):
        raise ValueError("%s must be a dict, got %r" % (name, type(fact)))
    _check_exact_keys(fact, _FACT_KEYS, name)
    if not isinstance(fact["allowed"], bool):
        raise ValueError(
            "%s.allowed must be a boolean, got %r" % (name, fact["allowed"]))
    if fact["allowed"]:
        if fact["refusal_code"] is not None:
            raise ValueError(
                "%s.refusal_code must be null when allowed=true" % name)
        if fact["refusal_message"] is not None:
            raise ValueError(
                "%s.refusal_message must be null when allowed=true" % name)
        if fact["source"] is not None:
            raise ValueError(
                "%s.source must be null when allowed=true" % name)
    else:
        if fact["refusal_code"] not in _REFUSAL_CODES:
            raise ValueError(
                "%s.refusal_code must be one of %s when allowed=false, got %r"
                % (name, sorted(_REFUSAL_CODES), fact["refusal_code"]))
        if not isinstance(fact["refusal_message"], str) or not fact["refusal_message"]:
            raise ValueError(
                "%s.refusal_message must be a nonempty string when allowed=false" % name)
        if fact["source"] not in _REFUSAL_SOURCES:
            raise ValueError(
                "%s.source must be one of %s when allowed=false, got %r"
                % (name, sorted(_REFUSAL_SOURCES), fact["source"]))
    return dict(fact)


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------

def decide(contract, policy_result=None, preflight_result=None, probe_result=None,
           manifest_id=None):
    """Validate contract and facts, then produce a DispatchDecision.

    Evaluates facts in strict order: policy_result, preflight_result,
    probe_result. The first fact with allowed=False maps its three refusal
    fields verbatim into a normalized refuse decision. With no refused fact,
    returns a normalized allow decision.

    `manifest_id` is optional traceability: when provided it is copied into
    `trace_event_id` and never changes the decision outcome.

    Never spawns, persists, emits, or performs I/O.
    """
    normalized = validate_dispatch_contract(contract)

    named_facts = [
        ("policy_result", policy_result),
        ("preflight_result", preflight_result),
        ("probe_result", probe_result),
    ]
    validated_facts = []
    for name, fact in named_facts:
        if fact is not None:
            validated_facts.append(_validate_fact(fact, name))
        else:
            validated_facts.append(None)

    for fact in validated_facts:
        if fact is not None and not fact["allowed"]:
            return {
                "schema_version": 1,
                "record": "DispatchDecision",
                "decision_id": str(_uuid_mod.uuid4()),
                "contract_id": normalized["contract_id"],
                "outcome": "refuse",
                "refusal_code": fact["refusal_code"],
                "refusal_message": fact["refusal_message"],
                "source": fact["source"],
                "spawned": False,
                "trace_event_id": manifest_id,
            }

    return {
        "schema_version": 1,
        "record": "DispatchDecision",
        "decision_id": str(_uuid_mod.uuid4()),
        "contract_id": normalized["contract_id"],
        "outcome": "allow",
        "refusal_code": None,
        "refusal_message": None,
        "source": None,
        "spawned": False,
        "trace_event_id": manifest_id,
    }


# ---------------------------------------------------------------------------
# AttemptLink
# ---------------------------------------------------------------------------

def _validate_source_ref(source_ref):
    """Validate and return a normalized copy of a source_ref dict."""
    if not isinstance(source_ref, dict):
        raise ValueError("source_ref must be a dict, got %r" % type(source_ref))
    _check_exact_keys(source_ref, _SOURCE_REF_KEYS, "source_ref")
    if source_ref["kind"] not in _SOURCE_REF_KINDS:
        raise ValueError(
            "source_ref.kind must be one of %s, got %r"
            % (sorted(_SOURCE_REF_KINDS), source_ref["kind"]))
    if "attempt_id" in source_ref:
        raise ValueError("source_ref must not contain 'attempt_id' key")
    _check_finite_epoch(source_ref["created"], "source_ref.created")
    if not any(source_ref.get(f) for f in _SOURCE_DISCRIMINATOR_FIELDS):
        raise ValueError(
            "source_ref must have at least one truthful discriminator field "
            "(%s)" % ", ".join(_SOURCE_DISCRIMINATOR_FIELDS))
    return dict(source_ref)


def _validate_delivery_ref(delivery_ref):
    """Validate and return a normalized copy of a delivery_ref dict."""
    if not isinstance(delivery_ref, dict):
        raise ValueError("delivery_ref must be a dict, got %r" % type(delivery_ref))
    _check_exact_keys(delivery_ref, _DELIVERY_REF_KEYS, "delivery_ref")
    _check_nonempty_str(delivery_ref.get("prompt_kind", ""), "delivery_ref.prompt_kind")
    sha = delivery_ref.get("prompt_sha256")
    if not isinstance(sha, str) or not re.match(r'^[0-9a-f]{64}$', sha):
        raise ValueError(
            "delivery_ref.prompt_sha256 must be 64 lowercase hex chars, got %r" % sha)
    pb = delivery_ref.get("prompt_bytes")
    if isinstance(pb, bool) or not isinstance(pb, int) or pb < 0:
        raise ValueError(
            "delivery_ref.prompt_bytes must be a nonnegative non-boolean integer, "
            "got %r" % pb)
    return dict(delivery_ref)


def build_attempt_link_idempotency_key(role, kind, source_ref, ordinal):
    """Build the deterministic idempotency key for an AttemptLink.

    Format: role + ":" + kind + ":" + discriminator_field=value + ":" + ordinal

    The discriminator is the first truthy field found in source_ref in the
    order: event_id, event_name, session_id, prompt_sha256. Same inputs always
    produce the same key; a different truthy source or ordinal changes the key.
    """
    for field in _SOURCE_DISCRIMINATOR_FIELDS:
        value = source_ref.get(field)
        if value:
            return "%s:%s:%s=%s:%s" % (role, kind, field, value, ordinal)
    raise ValueError(
        "source_ref has no truthful discriminator field; "
        "one of %s must be truthy" % ", ".join(_SOURCE_DISCRIMINATOR_FIELDS))


def validate_attempt_link(record):
    """Return a normalized copy of an AttemptLink record or raise ValueError.

    Never mutates input. Rejects missing or extra keys, nested attempt_id,
    source timestamp after link creation, and mismatched idempotency_key.
    """
    if not isinstance(record, dict):
        raise ValueError("AttemptLink must be a dict, got %r" % type(record))
    _check_exact_keys(record, _ATTEMPT_LINK_KEYS, "AttemptLink")
    _check_schema_version(record)
    if record["record"] != "AttemptLink":
        raise ValueError(
            "record field must be 'AttemptLink', got %r" % record["record"])
    _check_uuid(record["attempt_id"], "attempt_id")
    _check_nonempty_str(record["role"], "role")
    _check_str_or_null(record["phase"], "phase")
    if record["kind"] not in _ATTEMPT_LINK_KINDS:
        raise ValueError(
            "kind must be one of %s, got %r"
            % (sorted(_ATTEMPT_LINK_KINDS), record["kind"]))

    source_ref = _validate_source_ref(record["source_ref"])
    delivery_ref = _validate_delivery_ref(record["delivery_ref"])

    _check_finite_epoch(record["created"], "created")
    if record["created"] < source_ref["created"]:
        raise ValueError(
            "created (%r) must not be earlier than source_ref.created (%r)"
            % (record["created"], source_ref["created"]))

    # Validate idempotency_key matches builder output.
    # Extract ordinal as the last colon-separated segment of the stored key.
    stored_key = record["idempotency_key"]
    if not isinstance(stored_key, str):
        raise ValueError("idempotency_key must be a string, got %r" % stored_key)
    parts = stored_key.rsplit(":", 1)
    if len(parts) != 2:
        raise ValueError("idempotency_key has unexpected format: %r" % stored_key)
    ordinal = parts[1]
    expected_key = build_attempt_link_idempotency_key(
        record["role"], record["kind"], source_ref, ordinal)
    if stored_key != expected_key:
        raise ValueError(
            "idempotency_key mismatch: stored %r, expected %r"
            % (stored_key, expected_key))

    return dict(record)
