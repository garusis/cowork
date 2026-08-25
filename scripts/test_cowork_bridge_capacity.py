#!/usr/bin/env python3
"""Fixture-driven tests for M3 Package C: pure controller raw-failure
normalization in cowork_bridge.py.

Covers the frozen brief's required gates: fixture taxonomy coverage over
Package A's closed ControllerOutcome set (rate-limit, HTTP 529 overload,
authentication failure, transport error, malformed JSON, unrecognized text,
plus explicit policy_blocked/guard_unavailable/local-guard-shaped fixtures
and issue #61-style session-limit evidence), evidence-shape disambiguation
(not a string heuristic) for the two guard-related pairs, the negative
controls named in the plan, an import/purity boundary check, and
CapacityPacket-candidate construction.

No captured issue #14/#28/#61 provider evidence exists anywhere in this
repository -- the fixtures below are this package's own representative
shapes, deliberately aligned with the plan's gate wording AND with the
repository's own two ATTESTED raw claude failure shapes at
scripts/test_cowork.py:3079-3096 (see `test_attested_repo_shapes` below).

Run standalone:

    python3 -m unittest scripts/test_cowork_bridge_capacity.py -v
"""

import ast
import dis
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_bridge as bridge  # noqa: E402
import cowork_capacity as capacity  # noqa: E402


CLASSIFIERS = {
    "claude": bridge.classify_claude_failure,
    "codex": bridge.classify_codex_failure,
    "opencode": bridge.classify_opencode_failure,
}

# The shared local_guard/transport_error shapes are identical across every
# controller -- exercised once per controller to prove that.
_LOCAL_GUARD_UNREACHABLE = {"type": "local_guard", "status": "unreachable"}
_LOCAL_GUARD_DENIED = {"type": "local_guard", "status": "denied"}
_TRANSPORT_ERROR = {"type": "transport_error", "exception_type": "ConnectionResetError"}

# Per-controller raw fixtures naming every recognized ControllerOutcome that
# a raw provider failure can classify to (malformed_output is reached only
# via classify_role_reply_outcome, tested separately below).
CONTROLLER_FIXTURES = {
    "claude": {
        "quota_limited": {
            "type": "assistant", "isApiErrorMessage": True,
            "error": "rate_limit_error",
            "message": {"content": [{"type": "text", "text":
                                      "Rate limit exceeded, please retry later."}]},
        },
        "overloaded": {
            "type": "system", "subtype": "api_error",
            "error": {"formatted": "529 Overloaded: the server is overloaded",
                      "status": 529},
        },
        "authentication_failed": {
            "type": "system", "subtype": "api_error",
            "error": {"formatted": "401 OAuth token expired", "status": 401},
        },
        "policy_blocked": {
            "type": "assistant", "isApiErrorMessage": True,
            "error": "refusal",
            "message": {"content": [{"type": "text", "text":
                                      "I can't help with that request."}]},
        },
        "transport_failed": _TRANSPORT_ERROR,
        "guard_unavailable": _LOCAL_GUARD_UNREACHABLE,
        "local_guard_exhausted": _LOCAL_GUARD_DENIED,
        "unknown_provider_failure": {
            "type": "system", "subtype": "api_error",
            "error": {"formatted": "a brand new backend failure we've never seen",
                      "type": "brand_new_error_2027", "status": 599},
        },
    },
    "codex": {
        "quota_limited": {"type": "error", "message": "insufficient quota",
                          "code": "insufficient_quota"},
        "overloaded": {"type": "error", "message": "engine overloaded",
                       "status": 503},
        "authentication_failed": {"type": "error", "message": "invalid api key",
                                  "code": "invalid_api_key"},
        "policy_blocked": {"type": "error", "message": "content flagged",
                           "code": "content_policy_violation"},
        "transport_failed": _TRANSPORT_ERROR,
        "guard_unavailable": _LOCAL_GUARD_UNREACHABLE,
        "local_guard_exhausted": _LOCAL_GUARD_DENIED,
        "unknown_provider_failure": {"type": "error", "message": "wat",
                                     "code": "totally_unrecognized_code"},
    },
    "opencode": {
        "quota_limited": {"type": "error", "error": {
            "name": "rate_limit_exceeded",
            "data": {"message": "rate limited by upstream provider"}}},
        "overloaded": {"type": "error", "error": {
            "name": "server_overloaded", "data": {"message": "overloaded"}}},
        "authentication_failed": {"type": "error", "error": {
            "name": None, "data": {"message": "unauthorized", "status": 401}}},
        "policy_blocked": {"type": "error", "error": {
            "name": "policy_violation", "data": {"message": "blocked"}}},
        "transport_failed": _TRANSPORT_ERROR,
        "guard_unavailable": _LOCAL_GUARD_UNREACHABLE,
        "local_guard_exhausted": _LOCAL_GUARD_DENIED,
        "unknown_provider_failure": {"type": "error", "error": {
            "name": "unknownerror", "data": {"message": "?", "status": 555}}},
    },
}


class FixtureTaxonomyCoverageTest(unittest.TestCase):
    """Every named fixture classifies to exactly the expected, distinct
    ControllerOutcome, for all three controllers."""

    def test_every_fixture_classifies_correctly(self):
        for controller, fixtures in CONTROLLER_FIXTURES.items():
            classify = CLASSIFIERS[controller]
            for expected_outcome, raw in fixtures.items():
                with self.subTest(controller=controller, outcome=expected_outcome):
                    self.assertEqual(classify(raw), expected_outcome)

    def test_every_recognized_outcome_covered_per_controller(self):
        # malformed_output is reached only via classify_role_reply_outcome.
        expected = capacity.CONTROLLER_OUTCOME_SET - {"malformed_output"}
        for controller, fixtures in CONTROLLER_FIXTURES.items():
            with self.subTest(controller=controller):
                self.assertEqual(set(fixtures), expected)

    def test_issue_14_28_rate_limit_text_evidence(self):
        self.assertEqual(
            bridge.classify_claude_failure(CONTROLLER_FIXTURES["claude"]["quota_limited"]),
            "quota_limited")

    def test_issue_14_28_http_529_overload_evidence(self):
        self.assertEqual(
            bridge.classify_claude_failure(CONTROLLER_FIXTURES["claude"]["overloaded"]),
            "overloaded")

    def test_issue_61_session_limit_text_evidence(self):
        raw = {
            "type": "assistant", "isApiErrorMessage": True,
            "error": "session_limit_reached",
            "message": {"content": [{"type": "text", "text":
                                      "Your session limit will reset in 3 hours."}]},
        }
        self.assertEqual(bridge.classify_claude_failure(raw), "quota_limited")

    def test_attested_repo_shapes(self):
        # The ONLY two real, attested-in-this-repo raw claude failure shapes
        # (scripts/test_cowork.py:3079-3096), both of which must classify
        # correctly -- no captured issue #14/#28/#61 evidence exists, so
        # these are the closest thing to ground truth this repository has.
        assistant_shape = {
            "type": "assistant", "isApiErrorMessage": True,
            "error": "authentication_failed",
            "message": {"content": [{"type": "text", "text":
                                      "Failed to authenticate"}]},
        }
        system_shape = {
            "type": "system", "subtype": "api_error",
            "error": {"formatted": "401 OAuth token expired"},
        }
        self.assertEqual(bridge.classify_claude_failure(assistant_shape),
                         "authentication_failed")
        self.assertEqual(bridge.classify_claude_failure(system_shape),
                         "authentication_failed")

    def test_assistant_error_without_isapierrormessage_flag(self):
        # parse_claude_event's own trigger is `isApiErrorMessage OR error`;
        # classify_claude_failure mirrors that OR, not an AND.
        raw = {
            "type": "assistant", "error": "authentication_failed",
            "message": {"content": [{"type": "text", "text": "nope"}]},
        }
        self.assertEqual(bridge.classify_claude_failure(raw), "authentication_failed")


class MutualExclusivityAndTotalityTest(unittest.TestCase):
    """Recognized outcomes are mutually exclusive; unrecognized input is
    total and non-retryable, never a silent alias for another outcome."""

    def test_outcomes_are_pairwise_distinct_per_controller(self):
        for controller, fixtures in CONTROLLER_FIXTURES.items():
            with self.subTest(controller=controller):
                outcomes = list(fixtures.values())
                classify = CLASSIFIERS[controller]
                results = [classify(raw) for raw in outcomes]
                self.assertEqual(len(results), len(set(results)))

    def test_unrecognized_shape_is_unknown_provider_failure(self):
        for controller, classify in CLASSIFIERS.items():
            with self.subTest(controller=controller):
                outcome = classify({"type": "something_never_seen_before"})
                self.assertEqual(outcome, "unknown_provider_failure")
                self.assertNotEqual(outcome, "quota_limited")
                self.assertNotEqual(outcome, "malformed_output")

    def test_unknown_provider_failure_carries_no_retry_permission(self):
        # unknown_provider_failure is a NON_CAPACITY_TERMINAL_OUTCOME per
        # Package A -- it can never be treated as capacity evidence.
        self.assertIn("unknown_provider_failure", capacity.NON_CAPACITY_TERMINAL_OUTCOMES)
        self.assertNotIn("unknown_provider_failure", capacity.CAPACITY_ELIGIBLE_OUTCOMES)

    def test_local_guard_outcomes_are_never_capacity_eligible(self):
        # Package A names local_guard_exhausted explicitly among its
        # NON_CAPACITY_TERMINAL_OUTCOMES; guard_unavailable is simply outside
        # CAPACITY_ELIGIBLE_OUTCOMES entirely (an infra failure, not even a
        # terminal-non-capacity provider outcome). Neither is ever capacity
        # evidence.
        self.assertIn("local_guard_exhausted", capacity.NON_CAPACITY_TERMINAL_OUTCOMES)
        self.assertNotIn("local_guard_exhausted", capacity.CAPACITY_ELIGIBLE_OUTCOMES)
        self.assertNotIn("guard_unavailable", capacity.CAPACITY_ELIGIBLE_OUTCOMES)

    def test_recognized_shapes_span_the_full_outcome_set_across_controllers(self):
        # Behavioral: collects the ACTUAL classifier return values, not the
        # fixture dict's key names.
        seen = set()
        for controller, fixtures in CONTROLLER_FIXTURES.items():
            classify = CLASSIFIERS[controller]
            for raw in fixtures.values():
                seen.add(classify(raw))
        seen.add(bridge.classify_role_reply_outcome(True, False))
        self.assertEqual(seen, capacity.CONTROLLER_OUTCOME_SET)


class TokenStatusPrecedenceTest(unittest.TestCase):
    """When a machine-readable token and a numeric HTTP-like status disagree,
    the token always wins -- a specific, named discriminant is never
    silently overridden by a coincidental status code."""

    def test_policy_token_wins_over_conflicting_authentication_status(self):
        raw = {"type": "error", "error": {
            "name": "policy_violation", "data": {"status": 403}}}
        self.assertEqual(bridge.classify_opencode_failure(raw), "policy_blocked")

    def test_quota_token_wins_over_conflicting_authentication_status(self):
        raw = {"type": "error", "code": "insufficient_quota", "status": 401}
        self.assertEqual(bridge.classify_codex_failure(raw), "quota_limited")

    def test_unrecognized_token_never_falls_back_to_a_recognized_status(self):
        raw = {"type": "error", "code": "some_new_code_we_do_not_know",
              "status": 401}
        self.assertEqual(bridge.classify_codex_failure(raw), "unknown_provider_failure")


class LocalGuardTotalityTest(unittest.TestCase):
    """A malformed local_guard shape is unknown_provider_failure through
    every public per-controller classifier, never a raised exception."""

    def test_malformed_guard_status_is_unknown_not_an_exception(self):
        malformed_shapes = (
            {"type": "local_guard"},                       # status absent
            {"type": "local_guard", "status": None},
            {"type": "local_guard", "status": "exhausted"},  # not a real status
            {"type": "local_guard", "status": 1},
            {"type": "local_guard", "status": ["denied"]},
        )
        for controller, classify in CLASSIFIERS.items():
            for raw in malformed_shapes:
                with self.subTest(controller=controller, raw=raw):
                    self.assertEqual(classify(raw), "unknown_provider_failure")

    def test_classify_local_guard_evidence_still_raises_directly(self):
        # The low-level helper stays partial for a DIRECT caller-contract
        # violation; only the public per-controller classifiers degrade to
        # unknown_provider_failure.
        for bad in (None, "", "denied ", "DENIED", "exhausted", 1, ["denied"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    bridge.classify_local_guard_evidence(bad)


class EvidenceShapeDisambiguationTest(unittest.TestCase):
    """The two guard-related pairs are told apart by a structural `status`
    field alone -- never by inspecting free text, however quota/budget-like
    that text superficially reads."""

    def test_local_guard_exhausted_never_becomes_quota_limited(self):
        # Negative control: local-guard-shaped text that superficially
        # resembles a quota message must still classify local_guard_exhausted.
        quota_like_text_variants = (
            "Your quota has been exceeded, resets in 30s like a rate limit",
            "429 too many requests",
            "usage_limit_reached: overloaded_error",
        )
        for controller, classify in CLASSIFIERS.items():
            for detail in quota_like_text_variants:
                raw = {"type": "local_guard", "status": "denied", "detail": detail}
                with self.subTest(controller=controller, detail=detail):
                    self.assertEqual(classify(raw), "local_guard_exhausted")

    def test_guard_unavailable_never_becomes_local_guard_exhausted(self):
        # Negative control: guard-broker-unreachable text that superficially
        # resembles a local-budget-exhausted message must still classify
        # guard_unavailable.
        exhausted_like_text_variants = (
            "Local guard budget exhausted, please wait",
            "denied: local budget limit reached",
        )
        for controller, classify in CLASSIFIERS.items():
            for detail in exhausted_like_text_variants:
                raw = {"type": "local_guard", "status": "unreachable", "detail": detail}
                with self.subTest(controller=controller, detail=detail):
                    self.assertEqual(classify(raw), "guard_unavailable")

    def test_local_guard_field_presence_alone_decides_not_free_text(self):
        # Two records whose `detail` text is byte-identical classify
        # differently purely because `status` differs -- proof the decision
        # is a structural field lookup, not a text match.
        shared_detail = "budget exceeded"
        denied = {"type": "local_guard", "status": "denied", "detail": shared_detail}
        unreachable = {"type": "local_guard", "status": "unreachable",
                       "detail": shared_detail}
        self.assertEqual(bridge.classify_claude_failure(denied), "local_guard_exhausted")
        self.assertEqual(bridge.classify_claude_failure(unreachable), "guard_unavailable")

    def test_policy_blocked_distinct_from_authentication_failed(self):
        for controller, fixtures in CONTROLLER_FIXTURES.items():
            with self.subTest(controller=controller):
                self.assertNotEqual(
                    fixtures["policy_blocked"], fixtures["authentication_failed"])
                classify = CLASSIFIERS[controller]
                self.assertNotEqual(
                    classify(fixtures["policy_blocked"]),
                    classify(fixtures["authentication_failed"]))


class MalformedOutputTest(unittest.TestCase):
    """malformed_output is reachable ONLY for a successful turn with an
    invalid role reply, never for a failed turn."""

    def test_successful_turn_with_malformed_json_is_malformed_output(self):
        self.assertEqual(
            bridge.classify_role_reply_outcome(True, False), "malformed_output")

    def test_successful_turn_with_valid_json_has_no_outcome(self):
        self.assertIsNone(bridge.classify_role_reply_outcome(True, True))

    def test_malformed_output_is_never_a_capacity_outcome(self):
        outcome = bridge.classify_role_reply_outcome(True, False)
        self.assertNotIn(outcome, capacity.CAPACITY_ELIGIBLE_OUTCOMES)

    def test_failed_turn_is_never_eligible_for_malformed_output(self):
        with self.assertRaises(ValueError):
            bridge.classify_role_reply_outcome(False, False)
        with self.assertRaises(ValueError):
            bridge.classify_role_reply_outcome(None, False)

    def test_role_json_valid_must_be_bool(self):
        with self.assertRaises(ValueError):
            bridge.classify_role_reply_outcome(True, None)
        with self.assertRaises(ValueError):
            bridge.classify_role_reply_outcome(True, "false")


class RawInputValidationTest(unittest.TestCase):
    def test_non_dict_raw_raises(self):
        for classify in CLASSIFIERS.values():
            for bad in (None, "text", 42, ["a"]):
                with self.subTest(classify=classify.__name__, bad=bad):
                    with self.assertRaises(ValueError):
                        classify(bad)


class ExtractRetryEvidenceTest(unittest.TestCase):
    """Reset/retry evidence and its provenance are extracted structurally
    from the raw evidence itself -- never a caller assertion."""

    def test_extracts_well_shaped_retry_evidence(self):
        raw = {
            "type": "assistant", "isApiErrorMessage": True,
            "error": "rate_limit_error",
            "retry_evidence": {"source": "provider_header", "value": "30s"},
        }
        self.assertEqual(bridge.extract_retry_evidence(raw),
                         {"source": "provider_header", "value": "30s"})

    def test_missing_retry_evidence_is_unverified(self):
        raw = {"type": "assistant", "isApiErrorMessage": True,
              "error": "rate_limit_error"}
        self.assertEqual(bridge.extract_retry_evidence(raw),
                         {"source": "unverified", "value": None})

    def test_source_outside_trust_source_kinds_is_unverified(self):
        raw = {"retry_evidence": {"source": "cli_text_guess", "value": "30s"}}
        self.assertEqual(bridge.extract_retry_evidence(raw),
                         {"source": "unverified", "value": None})

    def test_missing_value_is_unverified_even_with_a_real_source(self):
        raw = {"retry_evidence": {"source": "provider_event", "value": None}}
        self.assertEqual(bridge.extract_retry_evidence(raw),
                         {"source": "unverified", "value": None})

    def test_non_dict_raw_is_unverified_not_an_exception(self):
        for bad in (None, "text", 42, ["a"]):
            with self.subTest(bad=bad):
                self.assertEqual(bridge.extract_retry_evidence(bad),
                                 {"source": "unverified", "value": None})

    def test_unverified_sentinel_classifies_untrustworthy(self):
        result = bridge.extract_retry_evidence({})
        self.assertEqual(capacity.classify_trust_source(result["source"]),
                         "untrustworthy")


class CapacityPacketCandidateTest(unittest.TestCase):
    """Unpersisted CapacityPacket candidate construction for quota_limited
    outcomes -- never a durable write, never a fabricated trust level, never
    built from caller-asserted (rather than extracted) evidence."""

    _ISSUED_AT = "2026-08-23T12:00:00Z"

    def _quota_raw(self, source=None, value=None):
        raw = dict(CONTROLLER_FIXTURES["claude"]["quota_limited"])
        if source is not None:
            raw = dict(raw)
            raw["retry_evidence"] = {"source": source, "value": value}
        return raw

    def test_scheduled_candidate_shape(self):
        raw_evidence = self._quota_raw(source="provider_event", value="30s")
        candidate = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=raw_evidence,
            issued_at=self._ISSUED_AT)
        self.assertEqual(candidate["schema_version"], capacity.SCHEMA_VERSION)
        self.assertEqual(candidate["provider"], "anthropic")
        self.assertEqual(candidate["provider_capacity_class"],
                         "subscription_quota_exhausted")
        self.assertEqual(candidate["resume_mode"], "scheduled")
        self.assertEqual(candidate["retry_after"], "30s")
        self.assertEqual(candidate["issued_at"], self._ISSUED_AT)
        self.assertEqual(candidate["trust"], "trustworthy")
        # capacity_source is exactly Package A's {"kind","sha256"} shape --
        # never a superset -- so Package E can copy it verbatim.
        normalized_source = capacity.validate_capacity_source(candidate["capacity_source"])
        self.assertEqual(normalized_source["kind"], "provider_event")
        self.assertEqual(len(normalized_source["sha256"]), 64)

    def test_manual_signal_candidate_when_retry_evidence_absent(self):
        raw_evidence = self._quota_raw()
        candidate = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=raw_evidence,
            issued_at=self._ISSUED_AT)
        self.assertEqual(candidate["resume_mode"], "manual_signal")
        self.assertIsNone(candidate["retry_after"])
        self.assertEqual(candidate["trust"], "untrustworthy")
        self.assertEqual(candidate["capacity_source"]["kind"], "unverified")
        # capacity_source stays shape-valid even for the unverified sentinel.
        capacity.validate_capacity_source(candidate["capacity_source"])

    def test_manual_signal_candidate_when_retry_after_unparseable(self):
        raw_evidence = self._quota_raw(source="provider_event",
                                       value="not a duration or timestamp")
        candidate = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=raw_evidence,
            issued_at=self._ISSUED_AT)
        self.assertEqual(candidate["resume_mode"], "manual_signal")
        self.assertIsNone(candidate["retry_after"])

    def test_evidence_source_never_a_caller_assertion(self):
        # Even a "trustworthy"-looking source has no effect unless it is
        # actually present, correctly shaped, inside raw_evidence itself.
        raw_evidence = self._quota_raw()  # no retry_evidence at all
        candidate = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=raw_evidence,
            issued_at=self._ISSUED_AT)
        self.assertEqual(candidate["trust"], "untrustworthy")

    def test_trust_source_kind_extracted_and_passed_through_unchanged(self):
        for kind in capacity.TRUST_SOURCE_KINDS:
            with self.subTest(kind=kind):
                raw_evidence = self._quota_raw(source=kind, value="30s")
                candidate = bridge.capacity_packet_candidate(
                    controller="claude", provider="anthropic",
                    raw_evidence=raw_evidence, issued_at=self._ISSUED_AT)
                self.assertEqual(candidate["capacity_source"]["kind"], kind)
                self.assertEqual(candidate["trust"],
                                 capacity.classify_trust_source(kind))

    def test_candidate_hashes_the_actual_raw_evidence(self):
        raw1 = self._quota_raw(source="provider_event", value="10s")
        raw2 = self._quota_raw(source="provider_event", value="20s")
        c1 = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=raw1,
            issued_at=self._ISSUED_AT)
        c2 = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=raw2,
            issued_at=self._ISSUED_AT)
        self.assertNotEqual(c1["capacity_source"]["sha256"],
                            c2["capacity_source"]["sha256"])
        c1_again = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=dict(raw1),
            issued_at=self._ISSUED_AT)
        self.assertEqual(c1["capacity_source"]["sha256"],
                         c1_again["capacity_source"]["sha256"])

    def test_non_json_serializable_evidence_raises_instead_of_coercing(self):
        # No `default=str` silent coercion: verbatim hashing means a
        # non-JSON-native value fails closed rather than producing a
        # non-reproducible digest.
        raw_evidence = self._quota_raw()
        raw_evidence["poison"] = {1, 2, 3}  # a set is not JSON-serializable
        with self.assertRaises(ValueError):
            bridge.capacity_packet_candidate(
                controller="claude", provider="anthropic",
                raw_evidence=raw_evidence, issued_at=self._ISSUED_AT)

    def test_candidate_is_deliberately_partial_never_a_full_capacity_packet(self):
        raw_evidence = self._quota_raw(source="provider_event", value="30s")
        candidate = bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic", raw_evidence=raw_evidence,
            issued_at=self._ISSUED_AT)
        self.assertNotIn("package_id", candidate)
        self.assertNotIn("binding", candidate)
        self.assertNotIn("wakeup", candidate)
        self.assertNotIn("manual_resume", candidate)
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(candidate)

    def test_provider_must_be_nonempty_string(self):
        raw_evidence = self._quota_raw()
        for bad in (None, "", 42):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    bridge.capacity_packet_candidate(
                        controller="claude", provider=bad,
                        raw_evidence=raw_evidence, issued_at=self._ISSUED_AT)

    def test_issued_at_must_be_rfc3339(self):
        raw_evidence = self._quota_raw()
        for bad in (None, "", "not-a-timestamp", "2026-13-40T99:99:99Z", 12345):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    bridge.capacity_packet_candidate(
                        controller="claude", provider="anthropic",
                        raw_evidence=raw_evidence, issued_at=bad)

    def test_unknown_controller_raises(self):
        raw_evidence = self._quota_raw()
        with self.assertRaises(ValueError):
            bridge.capacity_packet_candidate(
                controller="not_a_real_controller", provider="anthropic",
                raw_evidence=raw_evidence, issued_at=self._ISSUED_AT)

    def test_cross_check_rejects_evidence_that_does_not_classify_quota_limited(self):
        # M01 cross-check: capacity_packet_candidate must independently
        # re-classify raw_evidence via the matching classifier and refuse to
        # proceed (never calling A's trust/parser functions) unless the
        # result is exactly quota_limited.
        non_quota_raw = CONTROLLER_FIXTURES["claude"]["authentication_failed"]
        with self.assertRaises(ValueError):
            bridge.capacity_packet_candidate(
                controller="claude", provider="anthropic",
                raw_evidence=non_quota_raw, issued_at=self._ISSUED_AT)

    def test_cross_check_applies_per_controller(self):
        for controller in ("claude", "codex", "opencode"):
            quota_raw = CONTROLLER_FIXTURES[controller]["quota_limited"]
            with self.subTest(controller=controller):
                candidate = bridge.capacity_packet_candidate(
                    controller=controller, provider="anthropic",
                    raw_evidence=quota_raw, issued_at=self._ISSUED_AT)
                self.assertEqual(candidate["provider_capacity_class"],
                                 "subscription_quota_exhausted")


class ClaudeProviderRetryEvidenceExtractionTest(unittest.TestCase):
    """`_claude_provider_retry_evidence` -- the ONE structural, closed-
    grammar extraction point where a genuinely provider-attested retry-
    after value can survive out of a claude `system`/`api_error` event's
    own `error` dict (REV-BLK-01). Never inferred from message text,
    never fabricated for a shape that does not carry it."""

    def test_extracts_well_shaped_retry_after(self):
        error = {"type": "rate_limit_error", "status": 429,
                 "formatted": "429 Rate limited", "retry_after": "30s"}
        self.assertEqual(bridge._claude_provider_retry_evidence(error),
                         {"source": "provider_header", "value": "30s"})

    def test_extracts_rfc3339_timestamp_shaped_retry_after(self):
        error = {"type": "overloaded_error", "status": 529,
                 "retry_after": "2026-08-24T00:05:00Z"}
        self.assertEqual(bridge._claude_provider_retry_evidence(error),
                         {"source": "provider_header",
                          "value": "2026-08-24T00:05:00Z"})

    def test_missing_retry_after_field_is_none(self):
        error = {"type": "rate_limit_error", "status": 429,
                 "formatted": "429 Rate limited"}
        self.assertIsNone(bridge._claude_provider_retry_evidence(error))

    def test_two_attested_repo_shapes_carry_no_retry_after(self):
        # The repository's own only two ATTESTED raw claude failure shapes
        # (scripts/test_cowork.py:3079-3096) genuinely carry no such field
        # -- must degrade to None, never a guessed/fabricated value.
        self.assertIsNone(bridge._claude_provider_retry_evidence(
            {"formatted": "401 OAuth token expired"}))

    def test_wrong_type_value_is_none_not_coerced(self):
        for bad in (None, 30, 30.0, ["30s"], {"seconds": 30}, True):
            with self.subTest(bad=bad):
                self.assertIsNone(bridge._claude_provider_retry_evidence(
                    {"retry_after": bad}))

    def test_empty_string_value_is_none(self):
        self.assertIsNone(
            bridge._claude_provider_retry_evidence({"retry_after": ""}))

    def test_non_dict_error_is_none_not_an_exception(self):
        for bad in (None, "text", 42, ["a"]):
            with self.subTest(bad=bad):
                self.assertIsNone(bridge._claude_provider_retry_evidence(bad))

    def test_never_inferred_from_formatted_or_message_text(self):
        # A message that textually mentions a retry hint must NOT leak into
        # the extracted value -- only the structural `retry_after` field is
        # ever read.
        error = {"formatted": "429 Rate limited, retry_after=45s",
                 "message": "Please retry after 45 seconds"}
        self.assertIsNone(bridge._claude_provider_retry_evidence(error))

    def test_deterministic_pure_transform(self):
        error = {"type": "rate_limit_error", "retry_after": "30s"}
        self.assertEqual(bridge._claude_provider_retry_evidence(dict(error)),
                         bridge._claude_provider_retry_evidence(dict(error)))

    def test_extracted_shape_is_extract_retry_evidence_compatible(self):
        # The exact shape `_claude_provider_retry_evidence` returns, once
        # placed at raw["retry_evidence"], round-trips through Package C's
        # own `extract_retry_evidence` unchanged and classifies trustworthy.
        error = {"type": "rate_limit_error", "retry_after": "30s"}
        evidence = bridge._claude_provider_retry_evidence(error)
        raw = {"type": "assistant", "error": "rate_limit_error",
              "retry_evidence": evidence}
        self.assertEqual(bridge.extract_retry_evidence(raw), evidence)
        self.assertEqual(capacity.classify_trust_source(evidence["source"]),
                         "trustworthy")


class ParseClaudeEventRetryEvidenceTest(unittest.TestCase):
    """`parse_claude_event`'s `system`/`api_error` branch surfaces genuine
    retry evidence (when present) as `parsed["retry_evidence"]`, and stays
    silent (no key at all) when the raw event carries none -- proving the
    real event-reduction seam, not just the pure helper in isolation."""

    def test_system_api_error_with_retry_after_carries_retry_evidence(self):
        parsed = bridge.parse_claude_event({
            "type": "system", "subtype": "api_error",
            "error": {"type": "rate_limit_error", "status": 429,
                      "formatted": "429 Rate limited",
                      "retry_after": "30s"},
        })
        self.assertEqual(parsed["kind"], "error")
        self.assertEqual(parsed["retry_evidence"],
                         {"source": "provider_header", "value": "30s"})

    def test_system_api_error_without_retry_after_has_no_key(self):
        parsed = bridge.parse_claude_event({
            "type": "system", "subtype": "api_error",
            "error": {"formatted": "401 OAuth token expired"},
        })
        self.assertEqual(parsed["kind"], "error")
        self.assertNotIn("retry_evidence", parsed)

    def test_assistant_isapierrormessage_shape_never_carries_retry_evidence(self):
        # This shape structurally has no room for it (only the CLI's own
        # already-classified token) -- never fabricated here either.
        parsed = bridge.parse_claude_event({
            "type": "assistant", "isApiErrorMessage": True,
            "error": "rate_limit_error",
            "message": {"content": [{"type": "text", "text": "Rate limited"}]},
        })
        self.assertNotIn("retry_evidence", parsed)


class PurityAndImportBoundaryTest(unittest.TestCase):
    """Package C invariant: the new classification functions are pure --
    zero file writes, zero cowork_state.py calls, zero cowork.py imports."""

    NEW_FUNCTION_NAMES = (
        "classify_local_guard_evidence",
        "_classify_local_guard_or_unknown",
        "_extract_leading_http_status",
        "_classify_error_token",
        "_as_str_or_none",
        "_as_int_status_or_none",
        "classify_claude_failure",
        "classify_codex_failure",
        "classify_opencode_failure",
        "classify_role_reply_outcome",
        "extract_retry_evidence",
        "capacity_packet_candidate",
        "_claude_provider_retry_evidence",
    )

    _FORBIDDEN_GLOBAL_NAMES = frozenset({
        "state_store", "policy", "action_policy", "guard_broker",
        "controller_profiles", "trace_store", "ui", "probe_cache",
        "open", "subprocess", "os",
    })

    # A static, source-level scan (not merely a directory-listing snapshot)
    # for any I/O-shaped call anywhere in the new functions' source text --
    # catches in-place writes and writes outside scripts/, not just new
    # directory entries under this test's own directory.
    _FORBIDDEN_CALL_NAMES = frozenset({
        "open", "write", "writelines", "remove", "unlink", "rename",
        "replace", "rmtree", "copy", "copyfile", "move", "system", "popen",
        "Popen", "socket", "chmod", "mkdir", "makedirs", "truncate",
    })

    def test_new_functions_reference_no_forbidden_module_or_io(self):
        for name in self.NEW_FUNCTION_NAMES:
            func = getattr(bridge, name)
            with self.subTest(function=name):
                used_names = {
                    instr.argval for instr in dis.get_instructions(func)
                    if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_DEREF")
                }
                hit = used_names & self._FORBIDDEN_GLOBAL_NAMES
                self.assertFalse(hit, "function %s references forbidden name(s): %s"
                                 % (name, sorted(hit)))

    def test_module_source_never_imports_cowork_entrypoint(self):
        module_path = os.path.join(_HERE, "cowork_bridge.py")
        with open(module_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=module_path)
        top_level_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_level_names.add(node.module.split(".")[0])
        self.assertNotIn("cowork", top_level_names)

    def test_new_function_source_contains_no_io_calls(self):
        module_path = os.path.join(_HERE, "cowork_bridge.py")
        with open(module_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=module_path)
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.NEW_FUNCTION_NAMES:
                found[node.name] = node
        missing = set(self.NEW_FUNCTION_NAMES) - set(found)
        self.assertFalse(missing, "functions not found in module AST: %s" % sorted(missing))
        for name, func_node in found.items():
            with self.subTest(function=name):
                for node in ast.walk(func_node):
                    if isinstance(node, ast.Call):
                        callee = node.func
                        call_name = (
                            callee.id if isinstance(callee, ast.Name)
                            else callee.attr if isinstance(callee, ast.Attribute)
                            else None)
                        self.assertNotIn(
                            call_name, self._FORBIDDEN_CALL_NAMES,
                            "function %s makes a forbidden I/O-shaped call: %s"
                            % (name, call_name))

    def test_functions_perform_no_file_writes(self):
        before = set(os.listdir(_HERE))
        bridge.classify_claude_failure(CONTROLLER_FIXTURES["claude"]["quota_limited"])
        bridge.classify_codex_failure(CONTROLLER_FIXTURES["codex"]["overloaded"])
        bridge.classify_opencode_failure(CONTROLLER_FIXTURES["opencode"]["authentication_failed"])
        bridge.classify_local_guard_evidence("denied")
        bridge.classify_role_reply_outcome(True, False)
        bridge.extract_retry_evidence({"retry_evidence": {"source": "provider_event",
                                                          "value": "30s"}})
        bridge.capacity_packet_candidate(
            controller="claude", provider="anthropic",
            raw_evidence=CONTROLLER_FIXTURES["claude"]["quota_limited"],
            issued_at="2026-08-23T12:00:00Z")
        bridge._claude_provider_retry_evidence(
            {"type": "rate_limit_error", "retry_after": "30s"})
        after = set(os.listdir(_HERE))
        self.assertEqual(before, after)

    def test_functions_are_deterministic_pure_transforms(self):
        for controller, fixtures in CONTROLLER_FIXTURES.items():
            classify = CLASSIFIERS[controller]
            for raw in fixtures.values():
                with self.subTest(controller=controller, raw=raw):
                    self.assertEqual(classify(dict(raw)), classify(dict(raw)))


if __name__ == "__main__":
    unittest.main()
