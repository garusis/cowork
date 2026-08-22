#!/usr/bin/env python3
"""Tests for cowork_workunit: WorkUnit schema and dependency-graph validators.

Run standalone:

    python3 -m unittest scripts/test_cowork_workunit.py -v
"""

import ast
import os
import sys
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_workunit as wu  # noqa: E402

# Runtime modules the reviewer_focus/import-boundary gate names explicitly.
_FORBIDDEN_RUNTIME_MODULES = frozenset({
    "cowork", "cowork_bridge", "cowork_state", "cowork_ledger",
    "cowork_preflight", "cowork_dispatch", "cowork_dispatch_manifest",
    "cowork_policy", "cowork_action_policy", "cowork_guard_broker",
    "cowork_trace", "cowork_measure",
})


def _uuid():
    return str(uuid.uuid4())


def _make_work_unit(**overrides):
    base = dict(
        schema_version=1, record="WorkUnit",
        work_id=_uuid(), session_id=_uuid(), phase="building",
        role="builder", seat=0, round=1, attempt=1,
        controller="claude", provider="anthropic",
        requested_model="sonnet", effective_model="sonnet", effort="high",
        candidate_manifest_digest="a" * 64, candidate_index=0,
        prompt_digest="b" * 64, pending_turn_digest=None,
        parent_work_id=None, governed_child_policy="inherit",
        graph_revision=1, predecessor_work_ids=[], fan_join_id=None,
        lifecycle_state="pending", terminal_reason=None,
    )
    base.update(overrides)
    return base


def _node(work_id, predecessors=(), candidate=None, index=None, policy=None):
    return {
        "work_id": work_id,
        "candidate_manifest_digest": candidate,
        "candidate_index": index,
        "governed_child_policy": policy,
        "predecessor_work_ids": list(predecessors),
    }


class WorkUnitValidatorTest(unittest.TestCase):
    def test_valid_work_unit_returns_normalized_copy(self):
        w = _make_work_unit()
        result = wu.validate_work_unit(w)
        self.assertEqual(dict(result, predecessor_work_ids=list(result["predecessor_work_ids"])), w)
        self.assertIsNot(result, w)

    def test_input_not_mutated(self):
        w = _make_work_unit()
        original = dict(w)
        wu.validate_work_unit(w)
        self.assertEqual(w, original)

    def test_predecessor_work_ids_normalized_to_tuple(self):
        pred = _uuid()
        w = _make_work_unit(predecessor_work_ids=[pred])
        result = wu.validate_work_unit(w)
        self.assertEqual(result["predecessor_work_ids"], (pred,))
        self.assertIsInstance(result["predecessor_work_ids"], tuple)

    def test_missing_key_rejected(self):
        w = _make_work_unit()
        del w["role"]
        with self.assertRaises(ValueError):
            wu.validate_work_unit(w)

    def test_extra_key_rejected(self):
        w = _make_work_unit()
        w["extra_field"] = "x"
        with self.assertRaises(ValueError):
            wu.validate_work_unit(w)

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit("not a dict")

    def test_wrong_schema_version_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(schema_version=2))

    def test_boolean_schema_version_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(schema_version=True))

    def test_wrong_record_tag_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(record="NotWorkUnit"))

    def test_non_uuid_work_id_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(work_id="not-a-uuid"))

    def test_non_uuid_session_id_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(session_id="not-a-uuid"))

    def test_phase_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(phase=None))
        self.assertIsNone(result["phase"])

    def test_phase_non_string_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(phase=42))

    def test_empty_role_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(role=""))

    def test_negative_seat_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(seat=-1))

    def test_negative_round_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(round=-1))

    def test_negative_attempt_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(attempt=-1))

    def test_boolean_seat_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(seat=True))

    def test_empty_controller_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(controller=""))

    def test_empty_provider_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(provider=""))

    def test_requested_and_effective_model_null_accepted(self):
        result = wu.validate_work_unit(
            _make_work_unit(requested_model=None, effective_model=None))
        self.assertIsNone(result["requested_model"])
        self.assertIsNone(result["effective_model"])

    def test_requested_and_effective_model_may_diverge(self):
        result = wu.validate_work_unit(
            _make_work_unit(requested_model="opus", effective_model="sonnet"))
        self.assertEqual(result["requested_model"], "opus")
        self.assertEqual(result["effective_model"], "sonnet")

    def test_effort_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(effort=None))
        self.assertIsNone(result["effort"])

    def test_candidate_manifest_digest_null_accepted(self):
        # candidate_index must also be null here: a non-null index paired
        # with a null digest is its own rejected combination (see
        # CandidateCompletionConsistencyTest).
        result = wu.validate_work_unit(
            _make_work_unit(candidate_manifest_digest=None, candidate_index=None))
        self.assertIsNone(result["candidate_manifest_digest"])

    def test_candidate_manifest_digest_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(candidate_manifest_digest="short"))

    def test_candidate_manifest_digest_uppercase_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(candidate_manifest_digest="A" * 64))

    def test_candidate_index_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(candidate_index=None))
        self.assertIsNone(result["candidate_index"])

    def test_candidate_index_negative_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(candidate_index=-1))

    def test_prompt_digest_and_pending_turn_digest_null_accepted(self):
        result = wu.validate_work_unit(
            _make_work_unit(prompt_digest=None, pending_turn_digest=None))
        self.assertIsNone(result["prompt_digest"])
        self.assertIsNone(result["pending_turn_digest"])

    def test_pending_turn_digest_malformed_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(pending_turn_digest="nothex"))

    def test_parent_work_id_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(parent_work_id=None))
        self.assertIsNone(result["parent_work_id"])

    def test_parent_work_id_non_uuid_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(parent_work_id="not-a-uuid"))

    def test_all_governed_child_policies_accepted(self):
        for policy in ("inherit", "isolated", "denied"):
            with self.subTest(policy=policy):
                result = wu.validate_work_unit(_make_work_unit(governed_child_policy=policy))
                self.assertEqual(result["governed_child_policy"], policy)

    def test_governed_child_policy_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(governed_child_policy=None))
        self.assertIsNone(result["governed_child_policy"])

    def test_unknown_governed_child_policy_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(governed_child_policy="bogus"))

    def test_graph_revision_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(graph_revision=None))
        self.assertIsNone(result["graph_revision"])

    def test_graph_revision_negative_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(graph_revision=-1))

    def test_predecessor_work_ids_empty_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(predecessor_work_ids=[]))
        self.assertEqual(result["predecessor_work_ids"], ())

    def test_predecessor_work_ids_non_uuid_entry_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(predecessor_work_ids=["not-a-uuid"]))

    def test_predecessor_work_ids_wrong_type_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(predecessor_work_ids="not-a-list"))

    def test_fan_join_id_null_and_string_accepted(self):
        r1 = wu.validate_work_unit(_make_work_unit(fan_join_id=None))
        self.assertIsNone(r1["fan_join_id"])
        r2 = wu.validate_work_unit(_make_work_unit(fan_join_id="join-1"))
        self.assertEqual(r2["fan_join_id"], "join-1")

    def test_every_lifecycle_state_accepted_with_matching_terminal_reason(self):
        terminal = {"completed", "rejected_preflight", "failed", "cancelled", "aborted"}
        for state in (
            "pending", "preflighting", "running", "awaiting_gate", "completed",
            "rejected_preflight", "needs_authority", "awaiting_capacity",
            "blocked", "failed", "cancelled", "aborted",
        ):
            with self.subTest(state=state):
                reason = "some_reason" if state in terminal else None
                result = wu.validate_work_unit(
                    _make_work_unit(lifecycle_state=state, terminal_reason=reason))
                self.assertEqual(result["lifecycle_state"], state)

    def test_unknown_lifecycle_state_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(lifecycle_state="bogus_state"))

    def test_terminal_state_without_terminal_reason_rejected(self):
        for state in ("completed", "rejected_preflight", "failed", "cancelled", "aborted"):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    wu.validate_work_unit(
                        _make_work_unit(lifecycle_state=state, terminal_reason=None))

    def test_non_terminal_state_with_terminal_reason_rejected(self):
        for state in ("pending", "preflighting", "running", "awaiting_gate",
                      "needs_authority", "awaiting_capacity", "blocked"):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    wu.validate_work_unit(
                        _make_work_unit(lifecycle_state=state, terminal_reason="x"))

    def test_terminal_state_with_empty_string_reason_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(
                _make_work_unit(lifecycle_state="completed", terminal_reason=""))

    def test_json_round_trip(self):
        import json
        w = _make_work_unit(predecessor_work_ids=[_uuid(), _uuid()])
        validated = wu.validate_work_unit(w)
        rt = json.loads(json.dumps(validated))
        revalidated = wu.validate_work_unit(rt)
        self.assertEqual(revalidated["work_id"], validated["work_id"])
        self.assertEqual(
            tuple(revalidated["predecessor_work_ids"]), validated["predecessor_work_ids"])


class CandidateCompletionConsistencyTest(unittest.TestCase):
    """MA-04: only a candidate-bound WorkUnit may be `completed`, and
    `candidate_index` may never be set without a `candidate_manifest_digest`."""

    def test_completed_with_null_candidate_manifest_digest_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(
                lifecycle_state="completed", terminal_reason="gate_validated",
                candidate_manifest_digest=None, candidate_index=None))

    def test_completed_with_non_null_candidate_manifest_digest_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(
            lifecycle_state="completed", terminal_reason="gate_validated",
            candidate_manifest_digest="a" * 64, candidate_index=0))
        self.assertEqual(result["lifecycle_state"], "completed")
        self.assertEqual(result["candidate_manifest_digest"], "a" * 64)

    def test_completed_with_non_null_digest_and_null_index_accepted(self):
        # An indexless manifest remains legal even for a completed unit.
        result = wu.validate_work_unit(_make_work_unit(
            lifecycle_state="completed", terminal_reason="gate_validated",
            candidate_manifest_digest="a" * 64, candidate_index=None))
        self.assertEqual(result["lifecycle_state"], "completed")
        self.assertIsNone(result["candidate_index"])

    def test_non_completed_state_with_null_candidate_manifest_digest_accepted(self):
        # The fail-closed rule is specific to `completed`; a candidate-free
        # unit may still legally exist in any non-completed state.
        for state in ("pending", "preflighting", "running", "awaiting_gate",
                      "blocked", "needs_authority", "awaiting_capacity"):
            with self.subTest(state=state):
                result = wu.validate_work_unit(_make_work_unit(
                    lifecycle_state=state, terminal_reason=None,
                    candidate_manifest_digest=None, candidate_index=None))
                self.assertIsNone(result["candidate_manifest_digest"])

    def test_non_completed_terminal_state_with_null_digest_accepted(self):
        # rejected_preflight/failed/cancelled/aborted are terminal but are
        # not the candidate-bound completion state, so they remain reachable
        # without a candidate.
        for state in ("rejected_preflight", "failed", "cancelled", "aborted"):
            with self.subTest(state=state):
                result = wu.validate_work_unit(_make_work_unit(
                    lifecycle_state=state, terminal_reason="some_reason",
                    candidate_manifest_digest=None, candidate_index=None))
                self.assertIsNone(result["candidate_manifest_digest"])

    def test_candidate_index_non_null_with_null_digest_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_work_unit(_make_work_unit(
                candidate_manifest_digest=None, candidate_index=0))

    def test_candidate_index_null_with_non_null_digest_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(
            candidate_manifest_digest="a" * 64, candidate_index=None))
        self.assertEqual(result["candidate_manifest_digest"], "a" * 64)
        self.assertIsNone(result["candidate_index"])

    def test_candidate_index_and_digest_both_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(
            candidate_manifest_digest=None, candidate_index=None))
        self.assertIsNone(result["candidate_manifest_digest"])
        self.assertIsNone(result["candidate_index"])

    def test_candidate_index_and_digest_both_non_null_accepted(self):
        result = wu.validate_work_unit(_make_work_unit(
            candidate_manifest_digest="b" * 64, candidate_index=3))
        self.assertEqual(result["candidate_manifest_digest"], "b" * 64)
        self.assertEqual(result["candidate_index"], 3)


class UuidCanonicalizationTest(unittest.TestCase):
    """MA-02: every UUID-shaped identity field normalizes to one canonical
    lowercase form, so case-insensitive-accept never means case-sensitive
    identity comparisons treat the same id as two different ones."""

    def test_work_id_uppercase_accepted_and_normalized_to_lowercase(self):
        wid = _uuid()
        result = wu.validate_work_unit(_make_work_unit(work_id=wid.upper()))
        self.assertEqual(result["work_id"], wid.lower())

    def test_work_id_mixed_case_normalized_to_lowercase(self):
        wid = _uuid()
        mixed = "".join(
            c.upper() if i % 2 == 0 else c for i, c in enumerate(wid))
        result = wu.validate_work_unit(_make_work_unit(work_id=mixed))
        self.assertEqual(result["work_id"], wid.lower())

    def test_session_id_uppercase_accepted_and_normalized_to_lowercase(self):
        sid = _uuid()
        result = wu.validate_work_unit(_make_work_unit(session_id=sid.upper()))
        self.assertEqual(result["session_id"], sid.lower())

    def test_parent_work_id_uppercase_accepted_and_normalized_to_lowercase(self):
        pid = _uuid()
        result = wu.validate_work_unit(_make_work_unit(parent_work_id=pid.upper()))
        self.assertEqual(result["parent_work_id"], pid.lower())

    def test_predecessor_work_ids_uppercase_accepted_and_normalized_to_lowercase(self):
        pred = _uuid()
        result = wu.validate_work_unit(
            _make_work_unit(predecessor_work_ids=[pred.upper()]))
        self.assertEqual(result["predecessor_work_ids"], (pred.lower(),))

    def test_graph_node_work_id_normalized_to_lowercase(self):
        wid = _uuid()
        node = wu.validate_graph_node(_node(wid.upper()))
        self.assertEqual(node["work_id"], wid.lower())

    def test_graph_node_predecessor_normalized_to_lowercase(self):
        a, b = _uuid(), _uuid()
        node = wu.validate_graph_node(_node(a, predecessors=[b.upper()]))
        self.assertEqual(node["predecessor_work_ids"], (b.lower(),))

    def test_same_identity_different_case_pair_raises_duplicate_work_id(self):
        wid = _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([_node(wid.lower()), _node(wid.upper())])
        codes = {v["code"] for v in ctx.exception.violations}
        self.assertIn("duplicate_work_id", codes)

    def test_predecessor_in_different_case_than_node_not_dangling(self):
        # A predecessor written in a different case than the node that
        # declared it must resolve to the same identity, not a false
        # dangling_predecessor.
        a, b = _uuid(), _uuid()
        result = wu.validate_revision([
            _node(a),
            _node(b, predecessors=[a.upper()]),
        ])
        self.assertEqual(len(result), 2)
        b_node = next(n for n in result if n["work_id"] == b.lower())
        self.assertEqual(b_node["predecessor_work_ids"], (a.lower(),))


class GraphNodeProjectionTest(unittest.TestCase):
    def test_graph_node_from_work_unit_projects_expected_fields(self):
        pred = _uuid()
        w = wu.validate_work_unit(_make_work_unit(predecessor_work_ids=[pred]))
        node = wu.graph_node_from_work_unit(w)
        self.assertEqual(node, {
            "work_id": w["work_id"],
            "candidate_manifest_digest": w["candidate_manifest_digest"],
            "candidate_index": w["candidate_index"],
            "governed_child_policy": w["governed_child_policy"],
            "predecessor_work_ids": (pred,),
        })

    def test_graph_node_from_work_unit_projects_candidate_index(self):
        w = wu.validate_work_unit(_make_work_unit(
            candidate_manifest_digest="c" * 64, candidate_index=7))
        node = wu.graph_node_from_work_unit(w)
        self.assertEqual(node["candidate_index"], 7)

    def test_validate_graph_node_missing_key_rejected(self):
        node = _node(_uuid())
        del node["work_id"]
        with self.assertRaises(ValueError):
            wu.validate_graph_node(node)

    def test_validate_graph_node_extra_key_rejected(self):
        node = _node(_uuid())
        node["extra"] = "x"
        with self.assertRaises(ValueError):
            wu.validate_graph_node(node)

    def test_graph_node_candidate_index_null_accepted(self):
        node = wu.validate_graph_node(_node(_uuid(), candidate="a" * 64, index=None))
        self.assertIsNone(node["candidate_index"])

    def test_graph_node_candidate_index_non_null_accepted(self):
        node = wu.validate_graph_node(_node(_uuid(), candidate="a" * 64, index=5))
        self.assertEqual(node["candidate_index"], 5)

    def test_graph_node_candidate_index_negative_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_graph_node(_node(_uuid(), candidate="a" * 64, index=-1))

    def test_graph_node_candidate_index_non_null_with_null_digest_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_graph_node(_node(_uuid(), candidate=None, index=0))

    def test_graph_node_candidate_index_boolean_rejected(self):
        with self.assertRaises(ValueError):
            wu.validate_graph_node(_node(_uuid(), candidate="a" * 64, index=True))


class DependencyGraphValidationErrorFormsTest(unittest.TestCase):
    """Every required invalid graph form is rejected with the matching code."""

    def _violation_codes(self, err):
        return {v["code"] for v in err.violations}

    def test_duplicate_work_id_rejected(self):
        wid = _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([_node(wid), _node(wid)])
        self.assertIn("duplicate_work_id", self._violation_codes(ctx.exception))

    def test_dangling_predecessor_rejected(self):
        wid = _uuid()
        missing = _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([_node(wid, predecessors=[missing])])
        self.assertIn("dangling_predecessor", self._violation_codes(ctx.exception))

    def test_self_edge_rejected(self):
        wid = _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([_node(wid, predecessors=[wid])])
        self.assertIn("self_edge", self._violation_codes(ctx.exception))

    def test_two_cycle_rejected(self):
        a, b = _uuid(), _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([
                _node(a, predecessors=[b]),
                _node(b, predecessors=[a]),
            ])
        codes = self._violation_codes(ctx.exception)
        self.assertIn("cycle", codes)
        cycle_ids = {v["work_id"] for v in ctx.exception.violations if v["code"] == "cycle"}
        self.assertEqual(cycle_ids, {a, b})

    def test_n_cycle_rejected(self):
        a, b, c = _uuid(), _uuid(), _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([
                _node(a, predecessors=[b]),
                _node(b, predecessors=[c]),
                _node(c, predecessors=[a]),
            ])
        codes = self._violation_codes(ctx.exception)
        self.assertIn("cycle", codes)
        cycle_ids = {v["work_id"] for v in ctx.exception.violations if v["code"] == "cycle"}
        self.assertEqual(cycle_ids, {a, b, c})

    def test_disjoint_component_without_cycle_not_flagged_as_cycle(self):
        # A diamond has no cycle; make sure the DFS doesn't false-positive.
        a, b, c, d = _uuid(), _uuid(), _uuid(), _uuid()
        cand = "a" * 64
        result = wu.validate_revision([
            _node(a, candidate=cand, policy="inherit"),
            _node(b, predecessors=[a], candidate=cand, policy="inherit"),
            _node(c, predecessors=[a], candidate=cand, policy="inherit"),
            _node(d, predecessors=[b, c], candidate=cand, policy="inherit"),
        ])
        self.assertEqual(len(result), 4)

    def test_cross_candidate_fan_in_rejected(self):
        a, b, c = _uuid(), _uuid(), _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([
                _node(a, candidate="a" * 64, policy="inherit"),
                _node(b, candidate="b" * 64, policy="inherit"),
                _node(c, predecessors=[a, b], candidate=None, policy="inherit"),
            ])
        self.assertIn("cross_candidate_fan_in", self._violation_codes(ctx.exception))

    def test_cross_candidate_fan_in_rejected_same_digest_different_index(self):
        # Same candidate_manifest_digest but a different candidate_index is
        # still a different candidate identity — the reducer's own
        # definition of candidate identity is the (digest, index) PAIR.
        a, b, c = _uuid(), _uuid(), _uuid()
        digest = "a" * 64
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([
                _node(a, candidate=digest, index=0, policy="inherit"),
                _node(b, candidate=digest, index=1, policy="inherit"),
                _node(c, predecessors=[a, b], candidate=None, policy="inherit"),
            ])
        self.assertIn("cross_candidate_fan_in", self._violation_codes(ctx.exception))

    def test_fan_in_same_digest_same_index_accepted(self):
        a, b, c = _uuid(), _uuid(), _uuid()
        digest = "d" * 64
        result = wu.validate_revision([
            _node(a, candidate=digest, index=2, policy="inherit"),
            _node(b, candidate=digest, index=2, policy="inherit"),
            _node(c, predecessors=[a, b], candidate=digest, index=2, policy="inherit"),
        ])
        self.assertEqual(len(result), 3)

    def test_cross_policy_fan_in_rejected(self):
        a, b, c = _uuid(), _uuid(), _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([
                _node(a, candidate="a" * 64, policy="inherit"),
                _node(b, candidate="a" * 64, policy="isolated"),
                _node(c, predecessors=[a, b], candidate=None, policy=None),
            ])
        self.assertIn("cross_policy_fan_in", self._violation_codes(ctx.exception))

    def test_fan_in_agreeing_candidate_and_policy_accepted(self):
        a, b, c = _uuid(), _uuid(), _uuid()
        cand = "c" * 64
        result = wu.validate_revision([
            _node(a, candidate=cand, policy="inherit"),
            _node(b, candidate=cand, policy="inherit"),
            _node(c, predecessors=[a, b], candidate=cand, policy="inherit"),
        ])
        self.assertEqual(len(result), 3)

    def test_single_predecessor_fan_in_never_flagged(self):
        a, b = _uuid(), _uuid()
        result = wu.validate_revision([
            _node(a, candidate="a" * 64, policy="inherit"),
            _node(b, predecessors=[a], candidate="b" * 64, policy="isolated"),
        ])
        self.assertEqual(len(result), 2)

    def test_all_violations_collected_not_just_first(self):
        wid = _uuid()
        with self.assertRaises(wu.GraphValidationError) as ctx:
            wu.validate_revision([_node(wid, predecessors=[wid]), _node(wid)])
        codes = self._violation_codes(ctx.exception)
        self.assertIn("duplicate_work_id", codes)
        self.assertIn("self_edge", codes)


class ValidRevisionAndMultiRevisionGraphTest(unittest.TestCase):
    def test_empty_revision_accepted(self):
        self.assertEqual(wu.validate_revision([]), ())

    def test_new_graph_is_empty_tuple(self):
        self.assertEqual(wu.new_graph(), ())

    def test_append_revision_numbers_sequentially(self):
        a, b, c = _uuid(), _uuid(), _uuid()
        graph = wu.new_graph()
        graph = wu.append_revision(graph, [_node(a)])
        graph = wu.append_revision(graph, [_node(b)])
        graph = wu.append_revision(graph, [_node(c)])
        self.assertEqual([r["graph_revision"] for r in graph], [1, 2, 3])

    def test_append_revision_never_mutates_prior_graph(self):
        a, b = _uuid(), _uuid()
        graph = wu.append_revision(wu.new_graph(), [_node(a)])
        before = graph
        graph2 = wu.append_revision(graph, [_node(b)])
        self.assertEqual(graph, before)
        self.assertEqual(len(graph), 1)
        self.assertEqual(len(graph2), 2)
        self.assertIsNot(graph2, graph)

    def test_valid_disjoint_multi_revision_graph_accepted(self):
        # Two revisions, each internally valid, sharing no vertices — later
        # revisions never reference or reinterpret an earlier one.
        a1, a2 = _uuid(), _uuid()
        b1, b2 = _uuid(), _uuid()
        cand_a = "a" * 64
        cand_b = "b" * 64
        graph = wu.new_graph()
        graph = wu.append_revision(graph, [
            _node(a1, candidate=cand_a, policy="inherit"),
            _node(a2, predecessors=[a1], candidate=cand_a, policy="inherit"),
        ])
        graph = wu.append_revision(graph, [
            _node(b1, candidate=cand_b, policy="isolated"),
            _node(b2, predecessors=[b1], candidate=cand_b, policy="isolated"),
        ])
        self.assertEqual(len(graph), 2)
        self.assertEqual(graph[0]["graph_revision"], 1)
        self.assertEqual(graph[1]["graph_revision"], 2)
        rev1_ids = {n["work_id"] for n in graph[0]["nodes"]}
        rev2_ids = {n["work_id"] for n in graph[1]["nodes"]}
        self.assertEqual(rev1_ids, {a1, a2})
        self.assertEqual(rev2_ids, {b1, b2})
        self.assertTrue(rev1_ids.isdisjoint(rev2_ids))

    def test_a_bad_revision_does_not_corrupt_a_prior_good_graph(self):
        a = _uuid()
        graph = wu.append_revision(wu.new_graph(), [_node(a)])
        dup = _uuid()
        with self.assertRaises(wu.GraphValidationError):
            wu.append_revision(graph, [_node(dup), _node(dup)])
        self.assertEqual(len(graph), 1)

    def test_append_revision_rejects_non_tuple_graph(self):
        with self.assertRaises(ValueError):
            wu.append_revision([], [_node(_uuid())])


class ImportAndIOBoundaryTest(unittest.TestCase):
    """cowork_workunit.py imports no runtime module and performs no I/O."""

    def _module_path(self):
        return os.path.join(_HERE, "cowork_workunit.py")

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
                          "cowork_workunit.py imports runtime module(s): %s"
                          % sorted(forbidden_hit))

    def test_imports_are_stdlib_plus_control_plane_only(self):
        imported = self._top_level_imports()
        self.assertEqual(imported, {"re", "cowork_control_plane"})

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

    def test_control_plane_dependency_is_itself_a_pure_package_a_module(self):
        # cowork_control_plane.py is one of this package's own two new
        # modules (also import/IO boundary tested), so depending on it does
        # not reach into runtime code.
        import cowork_control_plane  # noqa: F401
        self.assertTrue(
            os.path.exists(os.path.join(_HERE, "cowork_control_plane.py")))


if __name__ == "__main__":
    unittest.main()
