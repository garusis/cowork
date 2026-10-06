#!/usr/bin/env python3
"""Principal authorization and escalation contract.

The sole owner of: the closed principal classes, the closed (request kind x
principal) authorization table, the capability vocabulary and the effective
resolution policy, the stop classes, the `EscalationPacket` schema with its
resume-token binding rule, and the parsers for adjudication and grant answer
bodies.

Everything here is a pure function over its arguments. Nothing reads state,
writes a record, reads a clock, emits a trace event or does I/O, and no cowork
module is imported: the set of request kinds the runtime knows is an explicit
`registry_kinds` argument of `authorize`, supplied by the caller.

Every check fails closed. An unknown kind, an unknown principal, a malformed
packet or a malformed answer body is never allowed or accepted. Contract
violations are returned, never raised: a violation is an `EscalationContractError`
(callers test it with `isinstance`), and no public function raises on bad input.

Python 3.9+, stdlib only.
"""

import copy
import json
from dataclasses import dataclass
from types import MappingProxyType

PRINCIPALS = (
    "lead",
    "reviewer",
    "supervisor_agent",
    "policy_principal",
    "runtime",
)

# Capability tokens a request may require and a principal may grant.
CAPABILITIES = ("scope_expansion", "policy_exception")

STOP_CLASSES = (
    "supervisor_judgment",
    "escalation",
    "lead_question",
    "authority_wait",
    "failure",
)

ADJUDICATION_OUTCOMES = ("close", "uphold")
GRANT_OUTCOMES = ("grant", "decline")

AUTHORIZE_REASONS = (
    "allowed",
    "legacy_default",
    "unknown_kind",
    "unknown_principal",
    "not_authorized",
)

ERROR_REASONS = (
    "packet_not_object",
    "packet_capability",
    "packet_target_principal",
    "packet_candidate",
    "packet_request_id",
    "packet_answer_digest_binding",
    "adjudication_schema",
    "adjudication_empty",
    "adjudication_duplicate_finding",
    "grant_not_object",
    "grant_missing_outcome",
    "grant_schema",
    "grant_empty_grants",
    "grant_decline_with_grants",
    "grant_duplicate_token",
    "grant_unknown_token",
)

# request kind -> principal -> the responses that principal may give.
AUTHORIZATION = MappingProxyType({
    "needs_input": MappingProxyType({"supervisor_agent": ("answer",)}),
    "reviewer_question": MappingProxyType({"supervisor_agent": ("answer",)}),
    "review_round_cap": MappingProxyType({"supervisor_agent": ("answer",)}),
    "review_not_approved": MappingProxyType({"supervisor_agent": ("answer",)}),
    "handoff_requested": MappingProxyType({
        "supervisor_agent": ("authorize_handoff", "decline_handoff"),
    }),
    "capability_escalation": MappingProxyType({"policy_principal": ("answer",)}),
})

# A kind the runtime knows but this table does not list behaves as it did
# before the table existed: the supervisor agent may consume it, nobody else.
LEGACY_DEFAULT_PRINCIPAL = "supervisor_agent"

# principal -> capability tokens that principal may grant.
EFFECTIVE_RESOLUTION_POLICY = MappingProxyType({
    "lead": frozenset(),
    "reviewer": frozenset(),
    "supervisor_agent": frozenset(),
    "policy_principal": frozenset(CAPABILITIES),
    "runtime": frozenset(),
})


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    reason: str
    # The responses the table grants. Empty under the legacy default: the
    # caller's registry owns the responses for a kind this table does not list.
    responses: tuple = ()


@dataclass(frozen=True)
class EscalationContractError:
    reason: str
    detail: str = ""
    ok = False


def _error(reason, detail=""):
    return EscalationContractError(reason, detail)


def _known_kinds(registry_kinds):
    if isinstance(registry_kinds, (str, bytes)):
        return frozenset()
    try:
        return frozenset(registry_kinds)
    except TypeError:
        return frozenset()


def authorize(request_kind, principal, registry_kinds):
    """May `principal` consume a request of `request_kind`?

    `registry_kinds` is the caller's collection of request kinds the runtime
    knows. Precedence: a kind absent from it is denied `unknown_kind`; an
    unknown principal is denied `unknown_principal`; a kind in the table allows
    exactly the principals listed there; a kind in the registry but not in the
    table follows the legacy default.
    """
    if not isinstance(request_kind, str) or request_kind not in _known_kinds(registry_kinds):
        return AuthorizationResult(False, "unknown_kind")
    if not isinstance(principal, str) or principal not in PRINCIPALS:
        return AuthorizationResult(False, "unknown_principal")
    row = AUTHORIZATION.get(request_kind)
    if row is None:
        if principal == LEGACY_DEFAULT_PRINCIPAL:
            return AuthorizationResult(True, "legacy_default")
        return AuthorizationResult(False, "not_authorized")
    responses = row.get(principal)
    if responses is None:
        return AuthorizationResult(False, "not_authorized")
    return AuthorizationResult(True, "allowed", responses)


def policy_covers(requires, grants):
    """True only when every required capability token is granted.

    An absent `requires` is empty and so covered. A non-collection, an unknown
    token or non-collection grants are not covered.
    """
    if requires is None:
        requires = []
    if isinstance(requires, (str, bytes)) or isinstance(grants, (str, bytes)):
        return False
    try:
        required = list(requires)
        granted = frozenset(grants)
    except TypeError:
        return False
    for token in required:
        if not isinstance(token, str) or token not in CAPABILITIES:
            return False
        if token not in granted:
            return False
    return True


# Stop pairs whose class depends on what the stop requires.
PAIR_STOP_CLASS = MappingProxyType({
    ("reviewer_question", "answer"): "supervisor_judgment",
    ("review_not_approved", "answer"): "supervisor_judgment",
    ("review_round_cap", "answer"): "supervisor_judgment",
    ("review_not_approved", "reviewer"): "failure",
})

# Stop kinds whose class does not depend on what the stop requires.
KIND_STOP_CLASS = MappingProxyType({
    "needs_input": "lead_question",
    "handoff_requested": "lead_question",
    "capability_escalation": "escalation",
    "reviewer_unavailable": "failure",
    "review_profile_rejected": "failure",
    "review_candidate_changed": "failure",
    "review_adjudication_ignored": "failure",
    "authority_unavailable": "failure",
    "recovery_budget_exhausted": "failure",
    "verification_not_current": "failure",
    "reviewer_absent": "failure",
    "role_turn_incomplete": "failure",
})

_AUTHORITY_REQUIRES = ("answer", "authorization")


def stop_class(kind, requires):
    """Classify a stop of `kind` that `requires` an answer from someone.

    Lookup order: an exact (kind, requires) pair, then the kind alone with
    `requires` ignored, then the unlisted-pair rule: requiring an answer or an
    authorization waits for authority, anything else is a failure.
    """
    if not isinstance(kind, str):
        return "failure"
    if isinstance(requires, str):
        pair = PAIR_STOP_CLASS.get((kind, requires))
        if pair is not None:
            return pair
    by_kind = KIND_STOP_CLASS.get(kind)
    if by_kind is not None:
        return by_kind
    if isinstance(requires, str) and requires in _AUTHORITY_REQUIRES:
        return "authority_wait"
    return "failure"


def build_escalation_packet(capability, target_principal, candidate, request_id,
                            answer_digest_binding):
    """Assemble an escalation packet. It is not validated here."""
    try:
        candidate = copy.deepcopy(candidate)
    except Exception:  # noqa: BLE001 - an uncopyable candidate must not validate
        candidate = None
    return {
        "capability": capability,
        "target_principal": target_principal,
        "candidate": candidate,
        "request_id": request_id,
        "answer_digest_binding": answer_digest_binding,
    }


def validate_escalation_packet(packet):
    """True for a valid packet, otherwise an `EscalationContractError`.

    `answer_digest_binding` must be present; None means the binding is attached
    when the answer arrives.
    """
    if not isinstance(packet, dict):
        return _error("packet_not_object")
    capability = packet.get("capability")
    if not isinstance(capability, str) or capability not in CAPABILITIES:
        return _error("packet_capability")
    target = packet.get("target_principal")
    if not isinstance(target, str) or target not in PRINCIPALS:
        return _error("packet_target_principal")
    candidate = packet.get("candidate")
    if not isinstance(candidate, dict) or not candidate:
        return _error("packet_candidate")
    request_id = packet.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return _error("packet_request_id")
    if "answer_digest_binding" not in packet:
        return _error("packet_answer_digest_binding")
    binding = packet["answer_digest_binding"]
    if binding is not None and (not isinstance(binding, str) or not binding):
        return _error("packet_answer_digest_binding")
    return True


def may_resume(packet, principal, request_id):
    """True only for the packet's target principal presenting its exact request id."""
    if validate_escalation_packet(packet) is not True:
        return False
    if not isinstance(principal, str) or not isinstance(request_id, str):
        return False
    return principal == packet["target_principal"] and request_id == packet["request_id"]


def _load_object(body):
    """The JSON object `body` holds, or None when it is not one."""
    if not isinstance(body, str):
        return None
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def parse_adjudication(answer_body):
    """Parse an adjudication answer body.

    Returns None for a body that is not a JSON object containing
    `adjudications` (a plain-text answer), an `EscalationContractError` for an
    object containing it that fails the schema, otherwise the normalized list
    of {finding_id, outcome, reason}.
    """
    body = _load_object(answer_body)
    if body is None or "adjudications" not in body:
        return None
    items = body["adjudications"]
    if not isinstance(items, list):
        return _error("adjudication_schema", "adjudications is not a list")
    if not items:
        return _error("adjudication_empty")
    parsed = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            return _error("adjudication_schema", "item is not an object")
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id:
            return _error("adjudication_schema", "finding_id")
        outcome = item.get("outcome")
        if not isinstance(outcome, str) or outcome not in ADJUDICATION_OUTCOMES:
            return _error("adjudication_schema", "outcome")
        reason = item.get("reason")
        if not isinstance(reason, str):
            return _error("adjudication_schema", "reason")
        if "note" in item and not isinstance(item["note"], str):
            return _error("adjudication_schema", "note")
        if finding_id in seen:
            return _error("adjudication_duplicate_finding", finding_id)
        seen.add(finding_id)
        parsed.append({"finding_id": finding_id, "outcome": outcome, "reason": reason})
    return parsed


def parse_grant(answer_body):
    """Parse a capability_escalation answer body.

    Requires a JSON object with `outcome`; anything else is an
    `EscalationContractError`. Success is {outcome, grants}: a grant names a
    non-empty list of unique capability tokens, a decline names none.
    """
    body = _load_object(answer_body)
    if body is None:
        return _error("grant_not_object")
    if "outcome" not in body:
        return _error("grant_missing_outcome")
    outcome = body["outcome"]
    if not isinstance(outcome, str) or outcome not in GRANT_OUTCOMES:
        return _error("grant_schema", "outcome")
    grants = body.get("grants", [])
    if not isinstance(grants, list):
        return _error("grant_schema", "grants is not a list")
    if "note" in body and not isinstance(body["note"], str):
        return _error("grant_schema", "note")
    seen = []
    for token in grants:
        if not isinstance(token, str) or token not in CAPABILITIES:
            return _error("grant_unknown_token")
        if token in seen:
            return _error("grant_duplicate_token", token)
        seen.append(token)
    if outcome == "grant" and not seen:
        return _error("grant_empty_grants")
    if outcome == "decline" and seen:
        return _error("grant_decline_with_grants")
    return {"outcome": outcome, "grants": seen}
