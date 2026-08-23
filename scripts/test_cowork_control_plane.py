#!/usr/bin/env python3
"""Tests for cowork_control_plane: PhaseState taxonomy, reducer, fingerprint.

Run standalone:

    python3 -m unittest scripts/test_cowork_control_plane.py -v
"""

import ast
import itertools
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_control_plane as cp  # noqa: E402

_VALID_GATE_EVIDENCE = {
    "gate_validation": {
        "candidate_manifest_digest": "a" * 64,
        "candidate_index": 0,
        "verdict": "pass",
    },
}

# M3 Package A additions: well-shaped evidence for the three new
# evidence-gated events, mirroring _VALID_GATE_EVIDENCE above so the
# pre-existing exhaustive-matrix test can supply matching evidence per
# event exactly as it already does for gate_validated (see
# test_every_state_event_pair_is_legal_or_explicitly_rejected's evidence
# selection below).
_VALID_CAPACITY_EVIDENCE = {
    "capacity_evidence": {
        "controller_outcome": "quota_limited",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": "a" * 64,
        "candidate_manifest_digest": "b" * 64,
        "candidate_index": 0,
        "resume_mode": "scheduled",
        "model": "claude-x",
        "effort": "high",
        "artifact_hashes": {"manifest": "c" * 64},
        "automation_ref": "scheduler-ref-1",
    },
}
_VALID_CAPACITY_WAKE_EVIDENCE = {
    "capacity_wake_evidence": {
        "kind": "trustworthy_reset",
        "lease_id": "lease-1",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": "a" * 64,
        "candidate_manifest_digest": "b" * 64,
        "candidate_index": 0,
        "consumption_state": "consumed",
        "not_before": "2026-08-23T00:00:00Z",
        "current_clock": "2026-08-23T01:00:00Z",
    },
}
_VALID_CAPACITY_WAKE_PREFLIGHT_FAILURE_EVIDENCE = {
    "capacity_wake_preflight_failure": {
        "lease_id": "lease-1",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": "a" * 64,
        "candidate_manifest_digest": "b" * 64,
        "candidate_index": 0,
        "failure_kind": "stale_lease",
    },
}

# Maps each evidence-gated M3/M2 event to well-shaped evidence for it; every
# other event supplies no evidence (None), matching the pre-M3 ternary this
# extends.
_VALID_EVIDENCE_BY_EVENT = {
    "gate_validated": _VALID_GATE_EVIDENCE,
    "capacity_reserved": _VALID_CAPACITY_EVIDENCE,
    "capacity_wake_claimed": _VALID_CAPACITY_WAKE_EVIDENCE,
    "capacity_wake_preflight_failed": _VALID_CAPACITY_WAKE_PREFLIGHT_FAILURE_EVIDENCE,
}

# M3A-REV-001-RESIDUAL: the three M3 capacity events REQUIRE a genuine,
# well-formed expected_candidate (unlike gate_validated, which honors an
# omitted one as a deliberate skip) — the candidate identity every
# _VALID_CAPACITY_*_EVIDENCE block above already names ("b" * 64, index 0).
_VALID_EXPECTED_CANDIDATE = {"candidate_manifest_digest": "b" * 64, "candidate_index": 0}
_VALID_EXPECTED_CANDIDATE_BY_EVENT = {
    "capacity_reserved": _VALID_EXPECTED_CANDIDATE,
    "capacity_wake_claimed": _VALID_EXPECTED_CANDIDATE,
    "capacity_wake_preflight_failed": _VALID_EXPECTED_CANDIDATE,
}

# Runtime modules the reviewer_focus/import-boundary gate names explicitly.
_FORBIDDEN_RUNTIME_MODULES = frozenset({
    "cowork", "cowork_bridge", "cowork_state", "cowork_ledger",
    "cowork_preflight", "cowork_dispatch", "cowork_dispatch_manifest",
    "cowork_policy", "cowork_action_policy", "cowork_guard_broker",
    "cowork_trace", "cowork_measure",
})


class PhaseStateTaxonomyTest(unittest.TestCase):
    def test_taxonomy_is_closed_and_matches_spec(self):
        self.assertEqual(cp.PHASE_STATES, (
            "pending", "preflighting", "running", "awaiting_gate", "completed",
            "rejected_preflight", "needs_authority", "awaiting_capacity",
            "blocked", "failed", "cancelled", "aborted",
        ))
        self.assertEqual(cp.PHASE_STATE_SET, frozenset(cp.PHASE_STATES))
        self.assertEqual(len(cp.PHASE_STATES), len(cp.PHASE_STATE_SET),
                          "PHASE_STATES must have no duplicate members")

    def test_terminal_states_are_closed_taxonomy_members(self):
        self.assertTrue(cp.TERMINAL_STATES.issubset(cp.PHASE_STATE_SET))
        self.assertEqual(cp.TERMINAL_STATES, frozenset({
            "completed", "rejected_preflight", "failed", "cancelled", "aborted",
        }))

    def test_event_vocabulary_excludes_raw_signals(self):
        # The reducer's event vocabulary is entirely domain-typed; a raw exit
        # code, EOF marker, or status-file-present signal must never appear.
        forbidden_substrings = ("exit_code", "eof", "status_file", "returncode")
        for event in cp.EVENTS:
            lowered = event.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(forbidden, lowered,
                                  "event %r looks like a raw signal" % event)


class ReducerExhaustiveMatrixTest(unittest.TestCase):
    """Every (state, event) pair is either an asserted legal transition or an
    asserted explicit rejection — the exhaustiveness required by the gate."""

    LEGAL = {
        ("pending", "preflight_started"): ("preflighting", "preflight_started"),
        ("pending", "cancelled"): ("cancelled", "cancelled"),
        ("pending", "aborted"): ("aborted", "aborted"),

        ("preflighting", "preflight_passed"): ("running", "preflight_passed"),
        ("preflighting", "preflight_rejected"): ("rejected_preflight", "preflight_rejected"),
        ("preflighting", "capability_missing"): ("needs_authority", "capability_missing"),
        ("preflighting", "cancelled"): ("cancelled", "cancelled"),
        ("preflighting", "aborted"): ("aborted", "aborted"),
        ("preflighting", "capacity_reserved"): ("awaiting_capacity", "capacity_reserved"),
        ("preflighting", "capacity_wake_preflight_failed"): (
            "awaiting_capacity", "capacity_wake_preflight_failed"),

        ("running", "turn_completed"): ("awaiting_gate", "turn_completed"),
        ("running", "dependency_blocked"): ("blocked", "dependency_blocked"),
        ("running", "capability_missing"): ("needs_authority", "capability_missing"),
        ("running", "execution_failed"): ("failed", "execution_failed"),
        ("running", "cancelled"): ("cancelled", "cancelled"),
        ("running", "aborted"): ("aborted", "aborted"),
        ("running", "capacity_reserved"): ("awaiting_capacity", "capacity_reserved"),

        ("awaiting_gate", "gate_validated"): ("completed", "gate_validated"),
        ("awaiting_gate", "gate_rejected"): ("failed", "gate_rejected"),
        ("awaiting_gate", "cancelled"): ("cancelled", "cancelled"),
        ("awaiting_gate", "aborted"): ("aborted", "aborted"),

        ("blocked", "dependency_unblocked"): ("running", "dependency_unblocked"),
        ("blocked", "cancelled"): ("cancelled", "cancelled"),
        ("blocked", "aborted"): ("aborted", "aborted"),

        ("needs_authority", "cancelled"): ("cancelled", "cancelled"),
        ("needs_authority", "aborted"): ("aborted", "aborted"),

        ("awaiting_capacity", "capacity_wake_claimed"): ("preflighting", "capacity_wake_claimed"),
        ("awaiting_capacity", "cancelled"): ("cancelled", "cancelled"),
        ("awaiting_capacity", "aborted"): ("aborted", "aborted"),
    }

    def test_legal_transitions_match_spec_exactly(self):
        self.assertEqual(dict(cp.TRANSITIONS), self.LEGAL,
                          "TRANSITIONS table drifted from the specified legal set")

    def test_every_state_event_pair_is_legal_or_explicitly_rejected(self):
        all_pairs = list(itertools.product(cp.PHASE_STATES, cp.EVENTS))
        self.assertEqual(len(all_pairs), len(cp.PHASE_STATES) * len(cp.EVENTS))

        legal_count = 0
        illegal_count = 0
        for state, event in all_pairs:
            with self.subTest(state=state, event=event):
                # M3 note: this line was extended (beyond the frozen brief's
                # named LEGAL-table/reachability-test edits) from
                # `_VALID_GATE_EVIDENCE if event == "gate_validated" else
                # None` to `_VALID_EVIDENCE_BY_EVENT.get(event)`, purely
                # additively (new evidence branches only) — required so this
                # pre-existing exhaustive matrix test still supplies
                # matching evidence for the three new evidence-gated events,
                # exactly as it already did for gate_validated.
                evidence = _VALID_EVIDENCE_BY_EVENT.get(event)
                expected_candidate = _VALID_EXPECTED_CANDIDATE_BY_EVENT.get(event)
                new_state, reason_code = cp.advance(
                    state, event, evidence, expected_candidate=expected_candidate)
                if (state, event) in self.LEGAL:
                    legal_count += 1
                    expected_state, expected_reason = self.LEGAL[(state, event)]
                    self.assertEqual(new_state, expected_state)
                    self.assertEqual(reason_code, expected_reason)
                else:
                    illegal_count += 1
                    self.assertEqual(new_state, state,
                                      "illegal transition must not change state")
                    self.assertEqual(reason_code, "illegal_transition")

        self.assertEqual(legal_count, len(self.LEGAL))
        self.assertEqual(legal_count + illegal_count, len(all_pairs))

    def test_gate_validated_without_evidence_stays_in_awaiting_gate(self):
        new_state, reason_code = cp.advance("awaiting_gate", "gate_validated", None)
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "gate_evidence_missing")

    def test_gate_validated_with_malformed_evidence_rejected(self):
        bad_shapes = [
            {},
            {"gate_validation": None},
            {"gate_validation": {
                "candidate_manifest_digest": None, "candidate_index": 0, "verdict": "pass"}},
            {"gate_validation": {
                "candidate_manifest_digest": "short", "candidate_index": 0, "verdict": "pass"}},
            {"gate_validation": {
                "candidate_manifest_digest": "Z" * 64, "candidate_index": 0, "verdict": "pass"}},
            {"gate_validation": {
                "candidate_manifest_digest": "a" * 64, "candidate_index": 0, "verdict": "fail"}},
            {"gate_validation": {
                "candidate_manifest_digest": "a" * 64, "candidate_index": -1, "verdict": "pass"}},
            {"gate_validation": {
                "candidate_manifest_digest": "a" * 64, "candidate_index": True, "verdict": "pass"}},
            {"gate_validation": {
                "candidate_manifest_digest": "a" * 64, "candidate_index": "0", "verdict": "pass"}},
        ]
        for evidence in bad_shapes:
            with self.subTest(evidence=evidence):
                new_state, reason_code = cp.advance("awaiting_gate", "gate_validated", evidence)
                self.assertEqual(new_state, "awaiting_gate")
                self.assertEqual(reason_code, "gate_evidence_missing")

    def test_gate_validated_with_null_candidate_index_accepted(self):
        evidence = {"gate_validation": {
            "candidate_manifest_digest": "a" * 64, "candidate_index": None, "verdict": "pass"}}
        new_state, reason_code = cp.advance("awaiting_gate", "gate_validated", evidence)
        self.assertEqual(new_state, "completed")
        self.assertEqual(reason_code, "gate_validated")

    def test_gate_validated_with_valid_evidence_completes(self):
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated", _VALID_GATE_EVIDENCE)
        self.assertEqual(new_state, "completed")
        self.assertEqual(reason_code, "gate_validated")

    def test_completed_unreachable_via_any_other_event(self):
        # A state that is already "completed" trivially stays "completed" on
        # an illegal event (it does not change state) — that is not "reaching"
        # completed, so it is excluded here; every genuine transition INTO
        # completed is checked below.
        for state, event in itertools.product(cp.PHASE_STATES, cp.EVENTS):
            if state == "completed":
                continue
            if event == "gate_validated" and state == "awaiting_gate":
                continue
            with self.subTest(state=state, event=event):
                new_state, _ = cp.advance(state, event, _VALID_GATE_EVIDENCE)
                self.assertNotEqual(new_state, "completed")

    def test_explicit_illegal_transitions(self):
        illegal_examples = [
            ("pending", "gate_validated"),
            ("pending", "turn_completed"),
            ("completed", "aborted"),
            ("completed", "preflight_started"),
            ("rejected_preflight", "cancelled"),
            ("failed", "cancelled"),
            ("cancelled", "aborted"),
            ("aborted", "cancelled"),
            ("running", "preflight_passed"),
            ("running", "gate_rejected"),
            ("awaiting_gate", "turn_completed"),
            ("awaiting_gate", "dependency_blocked"),
            ("blocked", "turn_completed"),
            ("needs_authority", "preflight_passed"),
            ("needs_authority", "capability_missing"),
        ]
        for state, event in illegal_examples:
            with self.subTest(state=state, event=event):
                new_state, reason_code = cp.advance(state, event)
                self.assertEqual(new_state, state)
                self.assertEqual(reason_code, "illegal_transition")

    def test_needs_authority_reachable_only_from_capability_missing(self):
        for state, event in itertools.product(cp.PHASE_STATES, cp.EVENTS):
            if state == "needs_authority" and (state, event) not in cp.TRANSITIONS:
                continue  # staying put on an illegal event is not "reaching" it
            new_state, _ = cp.advance(
                state, event,
                _VALID_GATE_EVIDENCE if event == "gate_validated" else None)
            if new_state == "needs_authority":
                self.assertEqual(event, "capability_missing",
                                  "only capability_missing may target needs_authority")

    def test_awaiting_capacity_m3_narrowly_reachable(self):
        """Named per the frozen brief (replacing test_awaiting_capacity_
        unreachable_m2): asserts the ONLY (state, event) pairs whose target
        is awaiting_capacity are exactly (running, capacity_reserved),
        (preflighting, capacity_reserved), and (preflighting,
        capacity_wake_preflight_failed); every other (state, event) pair in
        the full PHASE_STATE_SET x EVENT_SET product still never targets
        awaiting_capacity."""
        self.assertIn("awaiting_capacity", cp.PHASE_STATES)
        self.assertIn("capacity_reserved", cp.EVENTS)
        self.assertIn("capacity_wake_claimed", cp.EVENTS)
        self.assertIn("capacity_wake_preflight_failed", cp.EVENTS)

        expected_inbound_pairs = frozenset({
            ("running", "capacity_reserved"),
            ("preflighting", "capacity_reserved"),
            ("preflighting", "capacity_wake_preflight_failed"),
        })

        # Table-level check: exactly these three keys target awaiting_capacity.
        actual_inbound_pairs = frozenset(
            pair for pair, (new_state, _reason) in cp.TRANSITIONS.items()
            if new_state == "awaiting_capacity"
        )
        self.assertEqual(actual_inbound_pairs, expected_inbound_pairs)

        # Behavioral check over the full PHASE_STATE_SET x EVENT_SET product,
        # with well-shaped evidence supplied per event so a genuinely legal
        # transition is not miscounted as unreachable merely for lacking
        # evidence.
        for state, event in itertools.product(cp.PHASE_STATES, cp.EVENTS):
            if state == "awaiting_capacity" and (state, event) not in expected_inbound_pairs:
                continue  # staying put on an illegal event is not "reaching" it
            with self.subTest(state=state, event=event):
                evidence = _VALID_EVIDENCE_BY_EVENT.get(event)
                expected_candidate = _VALID_EXPECTED_CANDIDATE_BY_EVENT.get(event)
                new_state, _ = cp.advance(
                    state, event, evidence, expected_candidate=expected_candidate)
                if (state, event) in expected_inbound_pairs:
                    self.assertEqual(new_state, "awaiting_capacity")
                else:
                    self.assertNotEqual(
                        new_state, "awaiting_capacity",
                        "(%r, %r) must never transition into awaiting_capacity" % (state, event))

    def test_unknown_state_raises(self):
        with self.assertRaises(ValueError):
            cp.advance("not_a_state", "cancelled")

    def test_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            cp.advance("pending", "not_an_event")

    def test_non_dict_evidence_raises(self):
        with self.assertRaises(ValueError):
            cp.advance("awaiting_gate", "gate_validated", "not-a-dict")

    def test_reducer_is_pure_repeated_calls_agree(self):
        for _ in range(3):
            self.assertEqual(
                cp.advance("preflighting", "capability_missing"),
                ("needs_authority", "capability_missing"))

    def test_reducer_never_raises_for_known_state_event_pairs(self):
        for state, event in itertools.product(cp.PHASE_STATES, cp.EVENTS):
            evidence = _VALID_GATE_EVIDENCE if event == "gate_validated" else None
            cp.advance(state, event, evidence)  # must not raise

    def test_awaiting_gate_cannot_become_blocked_m2(self):
        """Named per orchestrator finding MA-03: a unit that already reached
        awaiting_gate (finished its turn) can never become blocked, so
        blocked's single resume edge back to running can never be reached
        having discarded a turn still awaiting gate validation."""
        new_state, reason_code = cp.advance("awaiting_gate", "dependency_blocked")
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "illegal_transition")

        # No (state, event) pair anywhere in the table targets blocked except
        # from running — blocked has exactly one legal origin.
        blocked_origins = {
            state for (state, _event), (new, _reason) in cp.TRANSITIONS.items()
            if new == "blocked"
        }
        self.assertEqual(blocked_origins, {"running"})

        # Therefore blocked's only resume edge, dependency_unblocked, can
        # only ever be reversing a block that started in running — it never
        # silently re-executes (or skips) a turn that had already completed
        # and was waiting at awaiting_gate for gate validation.
        self.assertEqual(
            cp.TRANSITIONS[("blocked", "dependency_unblocked")],
            ("running", "dependency_unblocked"))


class GateEvidenceCandidateBindingTest(unittest.TestCase):
    """MA-01: gate evidence must genuinely bind to the WorkUnit's own
    candidate identity, not merely to some well-formed candidate."""

    def _candidate(self, digest="a" * 64, index=0):
        return {"candidate_manifest_digest": digest, "candidate_index": index}

    def _evidence(self, digest="a" * 64, index=0):
        return {"gate_validation": {
            "candidate_manifest_digest": digest, "candidate_index": index,
            "verdict": "pass",
        }}

    def test_matching_candidate_completes(self):
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated",
            evidence=self._evidence(), expected_candidate=self._candidate())
        self.assertEqual(new_state, "completed")
        self.assertEqual(reason_code, "gate_validated")

    def test_mismatched_candidate_manifest_digest_rejected(self):
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated",
            evidence=self._evidence(digest="a" * 64),
            expected_candidate=self._candidate(digest="b" * 64))
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "gate_evidence_candidate_mismatch")

    def test_mismatched_candidate_index_rejected(self):
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated",
            evidence=self._evidence(index=0),
            expected_candidate=self._candidate(index=1))
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "gate_evidence_candidate_mismatch")

    def test_mismatch_takes_priority_over_neither_missing(self):
        # A candidate mismatch is a distinct reason_code from
        # gate_evidence_missing — the evidence itself is well-formed, it
        # simply names the wrong candidate.
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated",
            evidence=self._evidence(digest="c" * 64),
            expected_candidate=self._candidate(digest="d" * 64))
        self.assertNotEqual(reason_code, "gate_evidence_missing")
        self.assertEqual(reason_code, "gate_evidence_candidate_mismatch")
        self.assertEqual(new_state, "awaiting_gate")

    def test_missing_evidence_reported_before_candidate_is_ever_compared(self):
        # Malformed evidence is refused as gate_evidence_missing even when an
        # expected_candidate is supplied — shape is checked first.
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated",
            evidence=None, expected_candidate=self._candidate())
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "gate_evidence_missing")

    def test_no_expected_candidate_skips_binding_check(self):
        # Omitting expected_candidate (the default) accepts evidence for any
        # well-formed candidate — this is the caller's documented opt-out,
        # not an implicit guarantee.
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated", evidence=self._evidence())
        self.assertEqual(new_state, "completed")
        self.assertEqual(reason_code, "gate_validated")

    def test_expected_candidate_null_fields_must_match_exactly(self):
        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated",
            evidence=self._evidence(index=None),
            expected_candidate=self._candidate(index=None))
        self.assertEqual(new_state, "completed")
        self.assertEqual(reason_code, "gate_validated")

        new_state, reason_code = cp.advance(
            "awaiting_gate", "gate_validated",
            evidence=self._evidence(index=0),
            expected_candidate=self._candidate(index=None))
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "gate_evidence_candidate_mismatch")

    def test_expected_candidate_non_dict_raises(self):
        with self.assertRaises(ValueError):
            cp.advance(
                "awaiting_gate", "gate_validated",
                evidence=self._evidence(), expected_candidate="not-a-dict")

    def test_expected_candidate_ignored_for_non_gate_events(self):
        # expected_candidate only has meaning for gate_validated; supplying
        # it for another event neither raises nor changes the outcome.
        new_state, reason_code = cp.advance(
            "pending", "preflight_started", expected_candidate=self._candidate())
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "preflight_started")


class FingerprintTest(unittest.TestCase):
    def _args(self, **overrides):
        base = dict(role="builder", config_digest="cfg-" + "a" * 8,
                    provider="anthropic", candidate="candidate-1", reason="policy_denied")
        base.update(overrides)
        return base

    def test_deterministic_for_identical_inputs(self):
        a = cp.fingerprint(**self._args())
        b = cp.fingerprint(**self._args())
        self.assertEqual(a, b)

    def test_is_sha256_hex(self):
        digest = cp.fingerprint(**self._args())
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises ValueError if not hex

    def test_each_field_change_changes_digest(self):
        base = cp.fingerprint(**self._args())
        for field, new_value in (
            ("role", "reviewer"),
            ("config_digest", "cfg-" + "b" * 8),
            ("provider", "openai"),
            ("candidate", "candidate-2"),
            ("reason", "capability_missing"),
        ):
            with self.subTest(field=field):
                changed = cp.fingerprint(**self._args(**{field: new_value}))
                self.assertNotEqual(base, changed)

    def test_candidate_accepts_dict(self):
        digest = cp.fingerprint(**self._args(candidate={"work_id": "w1", "index": 0}))
        self.assertEqual(len(digest), 64)

    def test_candidate_accepts_none(self):
        digest = cp.fingerprint(**self._args(candidate=None))
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, cp.fingerprint(**self._args(candidate="candidate-1")))

    def test_candidate_dict_key_order_does_not_change_digest(self):
        a = cp.fingerprint(**self._args(candidate={"a": 1, "b": 2}))
        b = cp.fingerprint(**self._args(candidate={"b": 2, "a": 1}))
        self.assertEqual(a, b)

    def test_empty_role_rejected(self):
        with self.assertRaises(ValueError):
            cp.fingerprint(**self._args(role=""))

    def test_non_string_config_digest_rejected(self):
        with self.assertRaises(ValueError):
            cp.fingerprint(**self._args(config_digest=123))

    def test_empty_reason_rejected(self):
        with self.assertRaises(ValueError):
            cp.fingerprint(**self._args(reason=""))

    def test_candidate_wrong_type_rejected(self):
        with self.assertRaises(ValueError):
            cp.fingerprint(**self._args(candidate=42))

    def test_empty_string_candidate_rejected(self):
        with self.assertRaises(ValueError):
            cp.fingerprint(**self._args(candidate=""))


class ImportAndIOBoundaryTest(unittest.TestCase):
    """cowork_control_plane.py imports no runtime module and performs no I/O."""

    def _module_path(self):
        return os.path.join(_HERE, "cowork_control_plane.py")

    def _top_level_imports(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=self._module_path())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_imports_no_runtime_module(self):
        imported = self._top_level_imports()
        forbidden_hit = imported & _FORBIDDEN_RUNTIME_MODULES
        self.assertFalse(forbidden_hit,
                          "cowork_control_plane.py imports runtime module(s): %s"
                          % sorted(forbidden_hit))

    def test_imports_are_stdlib_only(self):
        imported = self._top_level_imports()
        self.assertEqual(imported, {"hashlib", "json", "re"})

    def test_source_contains_no_io_primitives(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=self._module_path())
        forbidden_calls = {"open", "socket"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls,
                                  "found forbidden I/O call: %s" % node.func.id)
            if isinstance(node, ast.Attribute) and node.attr in ("system", "popen"):
                self.fail("found forbidden I/O attribute access: %s" % node.attr)

    def test_module_has_no_os_import(self):
        self.assertNotIn("os", self._top_level_imports())


if __name__ == "__main__":
    unittest.main()
