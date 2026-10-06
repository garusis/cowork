#!/usr/bin/env python3
"""Focused permanent tests for the principal authorization and escalation
contract (`cowork_escalation`): the (request kind x principal) table, the
effective-resolution policy, stop classes, the escalation packet and its resume
binding, and the adjudication and grant parsers.

Every input is neutral and synthetic; nothing touches state or the filesystem.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_authority_core_escalation
"""

import ast
import json
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_escalation as escalation  # noqa: E402
import cowork_state as state_store  # noqa: E402

MODULE_PATH = os.path.join(_HERE, "cowork_escalation.py")

LEGACY_KINDS = (
    "needs_input",
    "reviewer_question",
    "review_round_cap",
    "review_not_approved",
    "handoff_requested",
)
ESCALATION_KIND = "capability_escalation"
REGISTRY = frozenset(LEGACY_KINDS + (ESCALATION_KIND,))

# Independent of the module's PRINCIPALS on purpose.
PRINCIPAL_LITERALS = ("lead", "reviewer", "supervisor_agent", "policy_principal", "runtime")
UNKNOWN_PRINCIPALS = ("operator", "", "SUPERVISOR_AGENT")

# (kind, principal) -> the responses the table grants. Anything else is denied.
EXPECTED_ALLOWED = {
    ("needs_input", "supervisor_agent"): ("answer",),
    ("reviewer_question", "supervisor_agent"): ("answer",),
    ("review_round_cap", "supervisor_agent"): ("answer",),
    ("review_not_approved", "supervisor_agent"): ("answer",),
    ("handoff_requested", "supervisor_agent"): ("authorize_handoff", "decline_handoff"),
    (ESCALATION_KIND, "policy_principal"): ("answer",),
}

STAND_IN_KIND = "stand_in_authorization_kind"
UNREGISTERED_KIND = "stand_in_unregistered_kind"
PACKET_CANDIDATE = {"candidate_id": "candidate-a"}
OTHER_CANDIDATE = {"candidate_id": "candidate-b"}


def _packet(**overrides):
    packet = escalation.build_escalation_packet(
        "scope_expansion", "policy_principal", PACKET_CANDIDATE, "request-1", None)
    packet.update(overrides)
    return packet


def _matrix_mismatches():
    """(kind, principal) pairs where authorize disagrees with EXPECTED_ALLOWED."""
    bad = []
    principals = PRINCIPAL_LITERALS + UNKNOWN_PRINCIPALS
    for kind in LEGACY_KINDS + (ESCALATION_KIND,):
        for principal in principals:
            result = escalation.authorize(kind, principal, REGISTRY)
            expected = EXPECTED_ALLOWED.get((kind, principal))
            if result.allowed != (expected is not None):
                bad.append((kind, principal))
            elif expected is not None and result.responses != expected:
                bad.append((kind, principal))
    return bad


class PurityTests(unittest.TestCase):
    FORBIDDEN = {"os", "sys", "time", "datetime", "subprocess", "socket", "pathlib",
                 "io", "shutil", "tempfile", "logging", "random"}

    def _imports(self):
        with open(MODULE_PATH, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append((node.module or "").split(".")[0])
        return names

    def test_imports_no_cowork_module(self):
        self.assertEqual([n for n in self._imports() if n.startswith("cowork")], [])

    def test_imports_no_io_or_clock_module(self):
        self.assertEqual(sorted(set(self._imports()) & self.FORBIDDEN), [])

    def test_tables_are_immutable(self):
        for table in (escalation.AUTHORIZATION,
                      escalation.AUTHORIZATION["needs_input"],
                      escalation.EFFECTIVE_RESOLUTION_POLICY,
                      escalation.PAIR_STOP_CLASS,
                      escalation.KIND_STOP_CLASS):
            with self.assertRaises(TypeError):
                table["x"] = "y"

    def test_principals_match_independent_literal(self):
        self.assertEqual(set(escalation.PRINCIPALS), set(PRINCIPAL_LITERALS))
        self.assertEqual(len(escalation.PRINCIPALS), len(set(escalation.PRINCIPALS)))

    def test_public_functions_accept_synthetic_inputs(self):
        escalation.authorize("needs_input", "lead", REGISTRY)
        escalation.stop_class("needs_input", "answer")
        escalation.policy_covers([], [])
        packet = _packet()
        escalation.validate_escalation_packet(packet)
        escalation.may_resume(packet, "policy_principal", "request-1")
        escalation.parse_adjudication("{}")
        escalation.parse_grant("{}")

    def test_hostile_inputs_never_raise(self):
        hostile = (None, 0, 1.5, [], {}, (), object(), b"x", float("nan"), ["a"], {"a": 1})
        for left in hostile:
            for right in hostile:
                escalation.authorize(left, right, left)
                escalation.authorize(left, right, REGISTRY)
                escalation.stop_class(left, right)
                escalation.policy_covers(left, right)
                escalation.may_resume(left, right, left)
            escalation.validate_escalation_packet(left)
            escalation.parse_adjudication(left)
            escalation.parse_grant(left)
            escalation.build_escalation_packet(left, left, left, left, left)


class AuthorizeMatrixTests(unittest.TestCase):
    def test_matrix_matches_table(self):
        self.assertEqual(_matrix_mismatches(), [])

    def test_matrix_check_catches_an_unlisted_pair(self):
        widened = {kind: dict(row) for kind, row in escalation.AUTHORIZATION.items()}
        widened["needs_input"]["lead"] = ("answer",)
        with mock.patch.object(escalation, "AUTHORIZATION", widened):
            self.assertIn(("needs_input", "lead"), _matrix_mismatches())

    def test_matrix_check_catches_a_dropped_pair(self):
        narrowed = {kind: dict(row) for kind, row in escalation.AUTHORIZATION.items()}
        del narrowed["needs_input"]["supervisor_agent"]
        with mock.patch.object(escalation, "AUTHORIZATION", narrowed):
            self.assertIn(("needs_input", "supervisor_agent"), _matrix_mismatches())

    def test_every_existing_kind_allows_supervisor_agent(self):
        existing = set(state_store.DECISION_RESPONSES) - {ESCALATION_KIND}
        self.assertTrue(set(LEGACY_KINDS) <= existing)
        registry = set(state_store.DECISION_RESPONSES) | {ESCALATION_KIND}
        for kind in existing:
            result = escalation.authorize(kind, "supervisor_agent", registry)
            self.assertTrue(result.allowed, kind)

    def test_handoff_requested_carries_both_responses(self):
        result = escalation.authorize("handoff_requested", "supervisor_agent", REGISTRY)
        self.assertEqual(set(result.responses), {"authorize_handoff", "decline_handoff"})

    def test_legacy_kinds_literal_rows(self):
        for kind in LEGACY_KINDS:
            self.assertEqual(set(escalation.AUTHORIZATION[kind]), {"supervisor_agent"})

    def test_capability_escalation_belongs_to_policy_principal_only(self):
        self.assertTrue(escalation.authorize(ESCALATION_KIND, "policy_principal", REGISTRY).allowed)
        denied = escalation.authorize(ESCALATION_KIND, "supervisor_agent", REGISTRY)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "not_authorized")

    def test_policy_principal_denied_for_legacy_kinds(self):
        for kind in LEGACY_KINDS:
            result = escalation.authorize(kind, "policy_principal", REGISTRY)
            self.assertFalse(result.allowed, kind)

    def test_authorization_rows_are_known(self):
        for kind, row in escalation.AUTHORIZATION.items():
            for principal in row:
                self.assertIn(principal, escalation.PRINCIPALS)


class AuthorizeRegistryCaseTests(unittest.TestCase):
    def test_kind_absent_from_registry_is_denied_for_every_principal(self):
        for principal in PRINCIPAL_LITERALS:
            result = escalation.authorize(UNREGISTERED_KIND, principal, REGISTRY)
            self.assertFalse(result.allowed, principal)
            self.assertEqual(result.reason, "unknown_kind")

    def test_table_kind_absent_from_registry_is_denied(self):
        registry = frozenset(LEGACY_KINDS)
        result = escalation.authorize(ESCALATION_KIND, "policy_principal", registry)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "unknown_kind")

    def test_registered_kind_absent_from_table_follows_legacy_default(self):
        registry = REGISTRY | {STAND_IN_KIND}
        result = escalation.authorize(STAND_IN_KIND, "supervisor_agent", registry)
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "legacy_default")
        self.assertEqual(result.responses, ())
        for principal in ("policy_principal", "lead", "reviewer", "runtime"):
            denied = escalation.authorize(STAND_IN_KIND, principal, registry)
            self.assertFalse(denied.allowed, principal)
            self.assertEqual(denied.reason, "not_authorized")

    def test_table_kind_with_unlisted_principal_is_denied(self):
        for principal in ("lead", "reviewer", "runtime", "policy_principal"):
            result = escalation.authorize("needs_input", principal, REGISTRY)
            self.assertFalse(result.allowed, principal)
            self.assertEqual(result.reason, "not_authorized")

    def test_unknown_principal_is_denied(self):
        registry = REGISTRY | {STAND_IN_KIND}
        for kind in tuple(registry):
            for principal in UNKNOWN_PRINCIPALS + (None, 7):
                result = escalation.authorize(kind, principal, registry)
                self.assertFalse(result.allowed, (kind, principal))
                self.assertEqual(result.reason, "unknown_principal")

    def test_double_unknown_is_unknown_kind(self):
        result = escalation.authorize(UNREGISTERED_KIND, "operator", REGISTRY)
        self.assertEqual(result.reason, "unknown_kind")

    def test_malformed_registry_is_empty(self):
        for registry in (None, "needs_input", b"needs_input", 5, [[]]):
            result = escalation.authorize("needs_input", "supervisor_agent", registry)
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "unknown_kind")

    def test_registry_may_be_any_collection(self):
        for registry in (list(REGISTRY), tuple(REGISTRY), set(REGISTRY),
                         dict.fromkeys(REGISTRY)):
            self.assertTrue(
                escalation.authorize("needs_input", "supervisor_agent", registry).allowed)

    def test_non_str_kind_is_unknown_kind(self):
        for kind in (None, 3, ["needs_input"], {"needs_input"}):
            result = escalation.authorize(kind, "supervisor_agent", REGISTRY)
            self.assertEqual(result.reason, "unknown_kind")

    def test_reasons_are_closed(self):
        registry = REGISTRY | {STAND_IN_KIND}
        for kind in tuple(registry) + (UNREGISTERED_KIND,):
            for principal in PRINCIPAL_LITERALS + UNKNOWN_PRINCIPALS:
                reason = escalation.authorize(kind, principal, registry).reason
                self.assertIn(reason, escalation.AUTHORIZE_REASONS)


class StopClassTests(unittest.TestCase):
    def test_supervisor_judgment_pairs(self):
        for pair in (("reviewer_question", "answer"), ("review_not_approved", "answer"),
                     ("review_round_cap", "answer")):
            self.assertEqual(escalation.stop_class(*pair), "supervisor_judgment", pair)

    def test_review_not_approved_reviewer_is_failure(self):
        self.assertEqual(escalation.stop_class("review_not_approved", "reviewer"), "failure")

    def test_lead_question_kinds(self):
        for kind in ("needs_input", "handoff_requested"):
            self.assertEqual(escalation.stop_class(kind, "answer"), "lead_question", kind)

    def test_capability_escalation_is_escalation(self):
        self.assertEqual(escalation.stop_class(ESCALATION_KIND, "answer"), "escalation")

    def test_failure_kinds(self):
        for kind in ("reviewer_unavailable", "review_profile_rejected",
                     "review_candidate_changed", "review_adjudication_ignored",
                     "authority_unavailable", "recovery_budget_exhausted",
                     "verification_not_current", "reviewer_absent",
                     "role_turn_incomplete"):
            self.assertEqual(escalation.stop_class(kind, "answer"), "failure", kind)

    def test_kind_only_classes_ignore_requires(self):
        for requires in ("answer", "authorization", "reviewer", "operator", None, 5, []):
            self.assertEqual(escalation.stop_class("needs_input", requires), "lead_question")
            self.assertEqual(escalation.stop_class(ESCALATION_KIND, requires), "escalation")
            self.assertEqual(escalation.stop_class("reviewer_absent", requires), "failure")

    def test_non_str_kind_is_failure(self):
        for kind in (None, 3, ["needs_input"], ("needs_input",)):
            self.assertEqual(escalation.stop_class(kind, "answer"), "failure")

    def test_unlisted_pairs_follow_the_authority_rule(self):
        for pair, expected in (
            (("reviewer_question", "authorization"), "authority_wait"),
            (("review_not_approved", "authorization"), "authority_wait"),
            (("review_round_cap", "authorization"), "authority_wait"),
            (("review_round_cap", "reviewer"), "failure"),
            (("review_not_approved", "operator"), "failure"),
            (("reviewer_question", "reviewer"), "failure"),
        ):
            self.assertEqual(escalation.stop_class(*pair), expected, pair)

    def test_stand_in_authorization_kind(self):
        self.assertEqual(escalation.stop_class("context_budget", "authorization"), "authority_wait")
        self.assertEqual(escalation.stop_class("context_budget", "answer"), "authority_wait")
        self.assertEqual(escalation.stop_class("context_budget", "reviewer"), "failure")
        self.assertEqual(escalation.stop_class("context_budget", "operator"), "failure")

    def test_unlisted_kind_with_odd_requires_is_failure(self):
        for requires in (None, 5, [], ["answer"]):
            self.assertEqual(escalation.stop_class("context_budget", requires), "failure")

    def test_authority_blocked_is_an_ordinary_unlisted_kind(self):
        self.assertEqual(escalation.stop_class("authority_blocked", "answer"), "authority_wait")
        self.assertEqual(escalation.stop_class("authority_blocked", "reviewer"), "failure")
        self.assertNotIn("authority_blocked", escalation.KIND_STOP_CLASS)

    def test_result_is_always_a_stop_class(self):
        kinds = tuple(escalation.KIND_STOP_CLASS) + LEGACY_KINDS + ("context_budget", None)
        for kind in kinds:
            for requires in ("answer", "authorization", "reviewer", "operator", None):
                self.assertIn(escalation.stop_class(kind, requires), escalation.STOP_CLASSES)

    def test_pair_table_is_load_bearing(self):
        without = {k: v for k, v in escalation.PAIR_STOP_CLASS.items()
                   if k != ("reviewer_question", "answer")}
        with mock.patch.object(escalation, "PAIR_STOP_CLASS", without):
            self.assertEqual(escalation.stop_class("reviewer_question", "answer"),
                             "authority_wait")


class PolicyTests(unittest.TestCase):
    def test_only_policy_principal_grants(self):
        policy = escalation.EFFECTIVE_RESOLUTION_POLICY
        for principal in ("lead", "reviewer", "supervisor_agent", "runtime"):
            self.assertEqual(policy[principal], frozenset(), principal)
        self.assertEqual(policy["policy_principal"],
                         frozenset({"scope_expansion", "policy_exception"}))

    def test_policy_names_every_principal(self):
        self.assertEqual(set(escalation.EFFECTIVE_RESOLUTION_POLICY), set(PRINCIPAL_LITERALS))

    def test_covered_only_when_every_token_is_granted(self):
        both = frozenset({"scope_expansion", "policy_exception"})
        self.assertTrue(escalation.policy_covers(["scope_expansion"], both))
        self.assertTrue(escalation.policy_covers(["scope_expansion", "policy_exception"], both))
        self.assertFalse(escalation.policy_covers(
            ["scope_expansion", "policy_exception"], frozenset({"scope_expansion"})))
        self.assertFalse(escalation.policy_covers(["scope_expansion"], frozenset()))

    def test_lead_reviewer_and_supervisor_cover_nothing(self):
        for principal in ("lead", "reviewer", "supervisor_agent"):
            grants = escalation.EFFECTIVE_RESOLUTION_POLICY[principal]
            for token in escalation.CAPABILITIES:
                self.assertFalse(escalation.policy_covers([token], grants), (principal, token))

    def test_policy_principal_covers_both(self):
        grants = escalation.EFFECTIVE_RESOLUTION_POLICY["policy_principal"]
        self.assertTrue(escalation.policy_covers(list(escalation.CAPABILITIES), grants))

    def test_empty_and_absent_requires_are_covered(self):
        self.assertTrue(escalation.policy_covers([], frozenset()))
        self.assertTrue(escalation.policy_covers(None, frozenset()))
        self.assertTrue(escalation.policy_covers((), ()))

    def test_malformed_input_is_not_covered(self):
        both = frozenset(escalation.CAPABILITIES)
        self.assertFalse(escalation.policy_covers(["unknown_token"], both | {"unknown_token"}))
        self.assertFalse(escalation.policy_covers("scope_expansion", both))
        self.assertFalse(escalation.policy_covers(5, both))
        self.assertFalse(escalation.policy_covers([["scope_expansion"]], both))
        self.assertFalse(escalation.policy_covers(["scope_expansion"], "scope_expansion"))
        self.assertFalse(escalation.policy_covers(["scope_expansion"], None))
        self.assertFalse(escalation.policy_covers(["scope_expansion"], 5))


class PacketTests(unittest.TestCase):
    def test_valid_packet_validates(self):
        self.assertIs(escalation.validate_escalation_packet(_packet()), True)
        self.assertIs(escalation.validate_escalation_packet(
            _packet(capability="policy_exception", answer_digest_binding="digest-1")), True)

    def test_invalid_packets_return_a_typed_error(self):
        cases = (
            ("packet_capability", {"capability": ""}),
            ("packet_capability", {"capability": "unlisted_capability"}),
            ("packet_capability", {"capability": None}),
            ("packet_target_principal", {"target_principal": "operator"}),
            ("packet_target_principal", {"target_principal": ""}),
            ("packet_candidate", {"candidate": None}),
            ("packet_candidate", {"candidate": {}}),
            ("packet_candidate", {"candidate": ["candidate-a"]}),
            ("packet_request_id", {"request_id": ""}),
            ("packet_request_id", {"request_id": None}),
            ("packet_answer_digest_binding", {"answer_digest_binding": ""}),
            ("packet_answer_digest_binding", {"answer_digest_binding": 5}),
        )
        for reason, overrides in cases:
            result = escalation.validate_escalation_packet(_packet(**overrides))
            self.assertIsInstance(result, escalation.EscalationContractError, overrides)
            self.assertEqual(result.reason, reason, overrides)
            self.assertIn(result.reason, escalation.ERROR_REASONS)

    def test_missing_keys_do_not_validate(self):
        for key, reason in (("capability", "packet_capability"),
                            ("target_principal", "packet_target_principal"),
                            ("candidate", "packet_candidate"),
                            ("request_id", "packet_request_id"),
                            ("answer_digest_binding", "packet_answer_digest_binding")):
            packet = _packet()
            del packet[key]
            result = escalation.validate_escalation_packet(packet)
            self.assertIsInstance(result, escalation.EscalationContractError, key)
            self.assertEqual(result.reason, reason, key)

    def test_non_dict_packet_does_not_validate(self):
        for packet in (None, [], "packet", 5):
            result = escalation.validate_escalation_packet(packet)
            self.assertIsInstance(result, escalation.EscalationContractError)
            self.assertEqual(result.reason, "packet_not_object")

    def test_error_is_not_ok_and_frozen(self):
        error = escalation.validate_escalation_packet(None)
        self.assertFalse(error.ok)
        with self.assertRaises(AttributeError):
            error.reason = "other"

    def test_build_deep_copies_the_candidate(self):
        candidate = {"candidate_id": "candidate-a", "files": ["a"]}
        packet = escalation.build_escalation_packet(
            "scope_expansion", "policy_principal", candidate, "request-1", None)
        candidate["files"].append("b")
        self.assertEqual(packet["candidate"]["files"], ["a"])

    def test_build_does_not_validate(self):
        packet = escalation.build_escalation_packet("", "", None, "", None)
        self.assertIsInstance(escalation.validate_escalation_packet(packet),
                              escalation.EscalationContractError)

    def test_uncopyable_candidate_does_not_validate(self):
        class Uncopyable(dict):
            def __deepcopy__(self, memo):
                raise RuntimeError("no copy")
        packet = escalation.build_escalation_packet(
            "scope_expansion", "policy_principal", Uncopyable(a=1), "request-1", None)
        self.assertIsInstance(escalation.validate_escalation_packet(packet),
                              escalation.EscalationContractError)


class MayResumeTests(unittest.TestCase):
    def test_target_principal_with_exact_request_id_may_resume(self):
        self.assertTrue(escalation.may_resume(_packet(), "policy_principal", "request-1"))

    def test_wrong_principal_with_right_request_id(self):
        for principal in ("lead", "reviewer", "supervisor_agent", "runtime"):
            self.assertFalse(escalation.may_resume(_packet(), principal, "request-1"), principal)

    def test_right_principal_with_stale_request_id(self):
        self.assertFalse(escalation.may_resume(_packet(), "policy_principal", "request-0"))
        self.assertFalse(escalation.may_resume(_packet(), "policy_principal", ""))
        self.assertFalse(escalation.may_resume(_packet(), "policy_principal", "request-1 "))

    def test_right_principal_with_another_candidates_request_id(self):
        mine = _packet()
        other = escalation.build_escalation_packet(
            "scope_expansion", "policy_principal", OTHER_CANDIDATE, "request-2", None)
        self.assertTrue(escalation.may_resume(other, "policy_principal", "request-2"))
        self.assertFalse(escalation.may_resume(mine, "policy_principal", "request-2"))
        self.assertFalse(escalation.may_resume(other, "policy_principal", "request-1"))

    def test_unknown_principal_may_not_resume(self):
        packet = _packet(target_principal="operator")
        self.assertFalse(escalation.may_resume(packet, "operator", "request-1"))

    def test_invalid_packet_may_not_resume(self):
        for overrides in ({"capability": ""}, {"candidate": None}, {"candidate": {}}):
            packet = _packet(**overrides)
            self.assertFalse(escalation.may_resume(packet, "policy_principal", "request-1"))
        self.assertFalse(escalation.may_resume(None, "policy_principal", "request-1"))

    def test_non_str_arguments_may_not_resume(self):
        packet = _packet()
        self.assertFalse(escalation.may_resume(packet, None, "request-1"))
        self.assertFalse(escalation.may_resume(packet, "policy_principal", None))
        self.assertFalse(escalation.may_resume(packet, ["policy_principal"], "request-1"))


class ParserTests(unittest.TestCase):
    VALID_ITEM = {"finding_id": "finding-1", "outcome": "close", "reason": "addressed"}

    def _adjudication(self, *items, **extra):
        body = {"adjudications": list(items)}
        body.update(extra)
        return json.dumps(body)

    def test_adjudication_none_for_a_body_that_is_not_one(self):
        for body in ("please continue", "", "{not json", "[1, 2]", "5", "null", '"text"',
                     "{}", '{"other": []}', None, 5, ["adjudications"], b"{}"):
            result = escalation.parse_adjudication(body)
            self.assertIsNone(result, body)
            self.assertNotIsInstance(result, escalation.EscalationContractError)

    def test_adjudication_error_when_the_key_is_present_but_invalid(self):
        cases = (
            ("adjudication_schema", '{"adjudications": "x"}'),
            ("adjudication_schema", '{"adjudications": null}'),
            ("adjudication_schema", '{"adjudications": {}}'),
            ("adjudication_empty", '{"adjudications": []}'),
            ("adjudication_schema", self._adjudication("not-an-object")),
            ("adjudication_schema", self._adjudication({**self.VALID_ITEM, "finding_id": ""})),
            ("adjudication_schema", self._adjudication({**self.VALID_ITEM, "finding_id": 3})),
            ("adjudication_schema", self._adjudication(
                {k: v for k, v in self.VALID_ITEM.items() if k != "finding_id"})),
            ("adjudication_schema", self._adjudication({**self.VALID_ITEM, "outcome": "defer"})),
            ("adjudication_schema", self._adjudication({**self.VALID_ITEM, "outcome": ["close"]})),
            ("adjudication_schema", self._adjudication({**self.VALID_ITEM, "reason": 5})),
            ("adjudication_schema", self._adjudication(
                {k: v for k, v in self.VALID_ITEM.items() if k != "reason"})),
            ("adjudication_schema", self._adjudication({**self.VALID_ITEM, "note": 5})),
            ("adjudication_duplicate_finding", self._adjudication(
                self.VALID_ITEM, {**self.VALID_ITEM, "outcome": "uphold"})),
        )
        for reason, body in cases:
            result = escalation.parse_adjudication(body)
            self.assertIsInstance(result, escalation.EscalationContractError, body)
            self.assertEqual(result.reason, reason, body)
            self.assertIn(result.reason, escalation.ERROR_REASONS)

    def test_valid_adjudication_is_normalized(self):
        body = self._adjudication(
            {**self.VALID_ITEM, "note": "n", "extra": 1},
            {"finding_id": "finding-2", "outcome": "uphold", "reason": "still open"},
            unrelated="ignored")
        self.assertEqual(escalation.parse_adjudication(body), [
            {"finding_id": "finding-1", "outcome": "close", "reason": "addressed"},
            {"finding_id": "finding-2", "outcome": "uphold", "reason": "still open"},
        ])

    def test_grant_error_for_a_body_that_is_not_an_outcome_object(self):
        for body in ("approved", "", "{not json", "[1]", "5", "null", "{}",
                     '{"grants": ["scope_expansion"]}', None, 5, {"outcome": "grant"}):
            result = escalation.parse_grant(body)
            self.assertIsInstance(result, escalation.EscalationContractError, body)
            self.assertIn(result.reason, escalation.ERROR_REASONS)

    def test_grant_missing_outcome_is_its_own_reason(self):
        result = escalation.parse_grant('{"grants": ["scope_expansion"]}')
        self.assertEqual(result.reason, "grant_missing_outcome")

    def test_grant_schema_failures(self):
        cases = (
            ("grant_schema", {"outcome": "approve", "grants": ["scope_expansion"]}),
            ("grant_schema", {"outcome": 1}),
            ("grant_schema", {"outcome": "grant", "grants": "scope_expansion"}),
            ("grant_schema", {"outcome": "grant", "grants": ["scope_expansion"], "note": 5}),
            ("grant_unknown_token", {"outcome": "grant", "grants": ["unlisted_capability"]}),
            ("grant_unknown_token", {"outcome": "grant", "grants": [1]}),
            ("grant_empty_grants", {"outcome": "grant", "grants": []}),
            ("grant_empty_grants", {"outcome": "grant"}),
            ("grant_decline_with_grants", {"outcome": "decline", "grants": ["scope_expansion"]}),
            ("grant_duplicate_token", {
                "outcome": "grant", "grants": ["scope_expansion", "scope_expansion"]}),
        )
        for reason, body in cases:
            result = escalation.parse_grant(json.dumps(body))
            self.assertIsInstance(result, escalation.EscalationContractError, body)
            self.assertEqual(result.reason, reason, body)

    def test_valid_grant_and_decline(self):
        self.assertEqual(
            escalation.parse_grant(json.dumps({
                "outcome": "grant", "grants": ["scope_expansion", "policy_exception"],
                "note": "ok"})),
            {"outcome": "grant", "grants": ["scope_expansion", "policy_exception"]})
        self.assertEqual(escalation.parse_grant('{"outcome": "decline"}'),
                         {"outcome": "decline", "grants": []})
        self.assertEqual(escalation.parse_grant('{"outcome": "decline", "grants": []}'),
                         {"outcome": "decline", "grants": []})

    def test_deeply_nested_body_is_not_an_adjudication(self):
        body = "[" * 100000
        self.assertIsNone(escalation.parse_adjudication(body))
        self.assertIsInstance(escalation.parse_grant(body), escalation.EscalationContractError)


if __name__ == "__main__":
    unittest.main()
