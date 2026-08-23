#!/usr/bin/env python3
"""Focused tests for M2 Package C: identity and dispatch primitives.

Covers, in isolation (fake callers only — nothing here wires cowork.py):

  * the durable, WorkUnit-bound terminal governed outcome now recorded for
    the previously trace-only SubagentStop branch (guard_broker.py:181-185,
    issue #48's residual gap);
  * the guard-lifecycle `ended` constant rename shared between
    cowork_guard_broker.py and cowork_measure.py (value-equality, zero
    behavior change);
  * cowork_trace.py's new_work_id/work_meta/identity_meta signature and
    output-shape stability under the additive-only change;
  * the dispatch-time dependency-graph declaration check (dangling
    predecessor, self-edge, cycle, cross-candidate fan-in, cross-policy
    fan-in — all fail closed);
  * child-dispatch inheritance failing closed when any one of
    controller/model/effort/governed_child_policy is missing;
  * the R2 residual: trace identity primitives are one join-key component of
    WorkUnit, never a competing identifier.

Run standalone:

    python3 -m unittest scripts/test_cowork_dispatch_identity.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_action_policy as action_policy  # noqa: E402
import cowork_control_plane as control_plane  # noqa: E402
import cowork_guard_broker as guard_broker  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_workunit as workunit  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


def _make_work_unit(**overrides):
    base = dict(
        schema_version=1, record="WorkUnit",
        work_id=_uuid(), session_id=_uuid(), phase="building",
        role="builder", seat=0, round=1, attempt=1,
        controller="claude", provider="anthropic",
        requested_model="sonnet", effective_model="sonnet", effort="high",
        candidate_manifest_digest=None, candidate_index=None,
        prompt_digest=None, pending_turn_digest=None,
        parent_work_id=None, governed_child_policy="inherit",
        graph_revision=None, predecessor_work_ids=[], fan_join_id=None,
        lifecycle_state="preflighting", terminal_reason=None,
    )
    base.update(overrides)
    return base


class _M2EnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test (mirrors test_cowork_state_m2's
    own mixin) so nothing here ever touches the real home dir."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root


# --------------------------------------------------------------------------- #
# Issue #48's residual gap: durable, WorkUnit-bound terminal governed        #
# outcome for the trace-only SubagentStop branch.                           #
# --------------------------------------------------------------------------- #


class SubagentStopDurableOutcomeTest(unittest.TestCase):
    def _broker(self, root, session_id=None):
        parent = {
            "controller": "claude", "controller_source": "config_pinned",
            "model": "sonnet", "model_source": "config_pinned",
            "effort": "high", "effort_source": "config_pinned",
        }
        return guard_broker.GuardBroker(
            os.path.join(root, "guard.sock"), "token",
            action_policy.OwnedScope(repo_roots=(root,)),
            os.path.join(root, "actions.jsonl"),
            os.path.join(root, "children.jsonl"),
            os.path.join(root, "trace.jsonl"),
            parent, session_id=session_id)

    def test_uncorrelated_subagent_stop_is_durable_not_trace_only(self):
        with tempfile.TemporaryDirectory() as root:
            broker = self._broker(root)
            parent_work_id = _uuid()
            response = broker.handle({
                "guard_attempt_id": "stop-1", "token": "token",
                "payload": {
                    "hook_event_name": "SubagentStop",
                    "agent_id": "uncorrelated-agent",
                    "parent_work_id": parent_work_id,
                }})
            self.assertEqual(response, {})

            children_path = os.path.join(root, "children.jsonl")
            self.assertTrue(os.path.exists(children_path),
                            "the outcome must be durable, not merely traced")
            with open(children_path) as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertTrue(row["work_id"])
            self.assertEqual(row["parent_work_id"], parent_work_id)
            self.assertEqual(row["agent_id"], "uncorrelated-agent")
            self.assertEqual(
                row["state"], guard_broker.CHILD_LIFECYCLE_UNGOVERNED_TERMINAL)
            self.assertNotEqual(row["state"], guard_broker.CHILD_LIFECYCLE_ENDED)
            self.assertEqual(row["terminal_source"], "subagent_stop")
            self.assertEqual(row["reason"], "child_agent_correlation_unavailable")

            # The trace event is still emitted, now carrying the SAME
            # work_id/parent_work_id as the durable record -- not a second,
            # independently-drifting identity.
            with open(os.path.join(root, "trace.jsonl")) as fh:
                trace_rows = [json.loads(line) for line in fh]
            self.assertEqual(len(trace_rows), 1)
            self.assertEqual(trace_rows[0]["event"], "child.ungoverned")
            self.assertEqual(trace_rows[0]["work_id"], row["work_id"])
            self.assertEqual(trace_rows[0]["parent_work_id"], parent_work_id)

    def test_outcome_is_terminal_and_never_re_finalized(self):
        with tempfile.TemporaryDirectory() as root:
            broker = self._broker(root)
            work_id = broker._record_ungoverned_terminal(
                "attempt-1", "agent-x", _uuid())
            self.assertIsNotNone(work_id)
            self.assertIn(work_id, broker._finalized)
            # A duplicate finalize attempt for the same fabricated work_id
            # is refused as a duplicate terminal, exactly like any other
            # already-finalized child.
            result = broker.finalize_child(work_id)
            self.assertEqual(result["state"], "duplicate_terminal")

    # ----------------------------------------------------------------- #
    # M2: the durable ungoverned outcome must be bound to a validated,   #
    # non-null parent_work_id; malformed/missing identity fails closed   #
    # and cannot persist junk.                                          #
    # ----------------------------------------------------------------- #

    def test_missing_parent_work_id_fails_closed_no_durable_record(self):
        with tempfile.TemporaryDirectory() as root:
            broker = self._broker(root)
            work_id = broker._record_ungoverned_terminal(
                "attempt-2", "agent-y", None)
            self.assertIsNone(work_id)
            children_path = os.path.join(root, "children.jsonl")
            self.assertFalse(
                os.path.exists(children_path),
                "a null parent_work_id must never persist a durable row")
            with open(os.path.join(root, "trace.jsonl")) as fh:
                trace_rows = [json.loads(line) for line in fh]
            self.assertEqual(len(trace_rows), 1)
            self.assertEqual(trace_rows[0]["event"], "child.ungoverned")
            self.assertEqual(trace_rows[0]["reason"], "parent_work_id_unbound")
            self.assertNotIn("work_id", trace_rows[0])

    def test_malformed_parent_work_id_fails_closed_no_durable_record(self):
        with tempfile.TemporaryDirectory() as root:
            broker = self._broker(root)
            work_id = broker._record_ungoverned_terminal(
                "attempt-3", "agent-z", "../../not-a-uuid")
            self.assertIsNone(work_id)
            self.assertFalse(
                os.path.exists(os.path.join(root, "children.jsonl")))

    def test_handle_subagent_stop_with_malformed_parent_persists_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            broker = self._broker(root)
            response = broker.handle({
                "guard_attempt_id": "stop-3", "token": "token",
                "payload": {
                    "hook_event_name": "SubagentStop",
                    "agent_id": "uncorrelated-agent",
                    "parent_work_id": "../../not-a-uuid",
                }})
            self.assertEqual(response, {})
            self.assertFalse(
                os.path.exists(os.path.join(root, "children.jsonl")))

    def test_session_bound_parent_must_be_a_real_minted_work_unit(self):
        """When the broker knows its session_id, a UUID-shaped
        parent_work_id that names no minted WorkUnit still fails closed --
        shape alone is not a genuine binding."""
        with tempfile.TemporaryDirectory() as root:
            session_root = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, session_root, ignore_errors=True)
            old_env = os.environ.get("COWORK_SESSIONS_ROOT")
            os.environ["COWORK_SESSIONS_ROOT"] = session_root
            try:
                session_id = _uuid()
                broker = self._broker(root, session_id=session_id)
                work_id = broker._record_ungoverned_terminal(
                    "attempt-4", "agent-w", _uuid())
                self.assertIsNone(work_id)
                self.assertFalse(
                    os.path.exists(os.path.join(root, "children.jsonl")))
            finally:
                if old_env is None:
                    os.environ.pop("COWORK_SESSIONS_ROOT", None)
                else:
                    os.environ["COWORK_SESSIONS_ROOT"] = old_env

    def test_session_bound_parent_succeeds_for_a_real_minted_work_unit(self):
        with tempfile.TemporaryDirectory() as root:
            session_root = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, session_root, ignore_errors=True)
            old_env = os.environ.get("COWORK_SESSIONS_ROOT")
            os.environ["COWORK_SESSIONS_ROOT"] = session_root
            try:
                session_id = _uuid()
                parent_work_id = _uuid()
                state_store.mint_work_unit(_make_work_unit(
                    work_id=parent_work_id, session_id=session_id))
                broker = self._broker(root, session_id=session_id)
                work_id = broker._record_ungoverned_terminal(
                    "attempt-5", "agent-v", parent_work_id)
                self.assertIsNotNone(work_id)
                with open(os.path.join(root, "children.jsonl")) as fh:
                    rows = [json.loads(line) for line in fh]
                self.assertEqual(rows[0]["parent_work_id"], parent_work_id)
            finally:
                if old_env is None:
                    os.environ.pop("COWORK_SESSIONS_ROOT", None)
                else:
                    os.environ["COWORK_SESSIONS_ROOT"] = old_env

    def test_correlated_subagent_stop_still_uses_the_ended_path(self):
        """A CORRELATED SubagentStop is unaffected by this change: it still
        finalizes via `finalize_child`, never the new ungoverned path."""
        with tempfile.TemporaryDirectory() as root:
            broker = self._broker(root)
            work_id = _uuid()
            broker._children_by_agent["known-agent"] = work_id
            broker._snapshots[work_id] = None
            broker._child_started_at[work_id] = "2024-01-01T00:00:00Z"
            response = broker.handle({
                "guard_attempt_id": "stop-2", "token": "token",
                "payload": {
                    "hook_event_name": "SubagentStop",
                    "agent_id": "known-agent",
                }})
            self.assertEqual(response, {})
            with open(os.path.join(root, "children.jsonl")) as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["work_id"], work_id)
            self.assertEqual(rows[0]["state"], guard_broker.CHILD_LIFECYCLE_ENDED)


# --------------------------------------------------------------------------- #
# B2: an ungoverned_terminal record must never populate governed             #
# `_children_by_agent` on restart, nor permit a previously uncorrelated      #
# child; its terminality must rehydrate without phantom `ended` records.     #
# Two-broker restart proof.                                                  #
# --------------------------------------------------------------------------- #


class UngovernedTerminalRestartTest(unittest.TestCase):
    def _broker(self, root):
        parent = {
            "controller": "claude", "controller_source": "config_pinned",
            "model": "sonnet", "model_source": "config_pinned",
            "effort": "high", "effort_source": "config_pinned",
        }
        return guard_broker.GuardBroker(
            os.path.join(root, "guard.sock"), "token",
            action_policy.OwnedScope(repo_roots=(root,)),
            os.path.join(root, "actions.jsonl"),
            os.path.join(root, "children.jsonl"),
            os.path.join(root, "trace.jsonl"),
            parent)

    def _pre_tool_use(self, agent_id, root):
        return {
            "guard_attempt_id": _uuid(), "token": "token",
            "payload": {
                "hook_event_name": "PreToolUse",
                "agent_id": agent_id,
                "tool_name": "Read",
                "file_path": os.path.join(root, "f.txt"),
                "parent_work_id": _uuid(),
            },
        }

    def test_deny_survives_a_broker_restart(self):
        """The exact B2 reproduction: an uncorrelated agent_id is denied,
        recorded as ungoverned_terminal on SubagentStop, and a SECOND
        GuardBroker constructed over the SAME children.jsonl (simulating a
        restart) must still deny the identical PreToolUse -- never flip
        into an allow -- and must never mint a phantom `ended` record on a
        subsequent SubagentStop."""
        with tempfile.TemporaryDirectory() as root:
            agent_id = "rogue-agent"
            broker1 = self._broker(root)

            first_request = self._pre_tool_use(agent_id, root)
            first_response = broker1.handle(first_request)
            self.assertEqual(
                first_response["hookSpecificOutput"]["permissionDecision"],
                "deny")
            self.assertEqual(
                first_response["hookSpecificOutput"][
                    "permissionDecisionReason"].split()[0],
                "child_agent_correlation_unavailable")

            stop_work_id = broker1._record_ungoverned_terminal(
                _uuid(), agent_id, _uuid())
            self.assertIsNotNone(stop_work_id)
            self.assertNotIn(agent_id, broker1._children_by_agent)

            with open(os.path.join(root, "children.jsonl")) as fh:
                rows_after_first = [json.loads(line) for line in fh]
            self.assertEqual(len(rows_after_first), 1)
            self.assertEqual(
                rows_after_first[0]["state"],
                guard_broker.CHILD_LIFECYCLE_UNGOVERNED_TERMINAL)

            # Restart: a fresh broker rehydrates from the SAME children.jsonl.
            broker2 = self._broker(root)
            self.assertNotIn(
                agent_id, broker2._children_by_agent,
                "an ungoverned_terminal record must never populate "
                "_children_by_agent on rehydration")
            self.assertIn(stop_work_id, broker2._finalized)

            second_request = self._pre_tool_use(agent_id, root)
            second_response = broker2.handle(second_request)
            self.assertEqual(
                second_response["hookSpecificOutput"]["permissionDecision"],
                "deny",
                "the deny must survive the restart, never flip to allow")

            # A later SubagentStop for the same never-correlated agent_id
            # must record another ungoverned_terminal outcome, never a
            # phantom `ended` record via finalize_child on the minted
            # (never-started) work_id.
            stop_response = broker2.handle({
                "guard_attempt_id": _uuid(), "token": "token",
                "payload": {
                    "hook_event_name": "SubagentStop", "agent_id": agent_id,
                    "parent_work_id": _uuid(),
                }})
            self.assertEqual(stop_response, {})
            with open(os.path.join(root, "children.jsonl")) as fh:
                rows_after_restart = [json.loads(line) for line in fh]
            states = [row["state"] for row in rows_after_restart]
            self.assertNotIn(
                guard_broker.CHILD_LIFECYCLE_ENDED, states,
                "no phantom 'ended' record may be minted for an agent_id "
                "that was never a correlated, started child")
            self.assertEqual(
                states.count(guard_broker.CHILD_LIFECYCLE_UNGOVERNED_TERMINAL),
                2)

    def test_correlated_agent_still_reproduces_across_restart(self):
        """Negative control: a genuinely correlated, ended child's agent_id
        DOES legitimately survive rehydration (unaffected by the B2 fix,
        which excludes only CHILD_LIFECYCLE_UNGOVERNED_TERMINAL)."""
        with tempfile.TemporaryDirectory() as root:
            broker1 = self._broker(root)
            work_id = _uuid()
            broker1._children_by_agent["known-agent"] = work_id
            broker1._snapshots[work_id] = None
            broker1._child_started_at[work_id] = "2024-01-01T00:00:00Z"
            broker1.finalize_child(work_id, agent_id="known-agent")

            broker2 = self._broker(root)
            self.assertEqual(
                broker2._children_by_agent.get("known-agent"), work_id)
            self.assertIn(work_id, broker2._finalized)


# --------------------------------------------------------------------------- #
# Rename-safety: guard-lifecycle `ended` shared constant.                    #
# --------------------------------------------------------------------------- #


class EndedConstantRenameTest(unittest.TestCase):
    def test_rename_ended_constant_value_equality(self):
        self.assertEqual(guard_broker.CHILD_LIFECYCLE_ENDED, "ended")

    def test_measure_consumes_the_identical_shared_constant(self):
        self.assertIs(measure.guard_broker.CHILD_LIFECYCLE_ENDED,
                      guard_broker.CHILD_LIFECYCLE_ENDED)

    def test_child_work_from_ledger_still_recognizes_ended_records(self):
        records = [{
            "work_id": "w1", "state": "started", "ts": "t0",
        }, {
            "work_id": "w1", "state": guard_broker.CHILD_LIFECYCLE_ENDED,
            "ts": "t1", "duration_ms": 42,
        }]
        rebuilt = measure.child_work_from_ledger(records)
        self.assertEqual(rebuilt["w1"]["work_state"], "complete")
        self.assertEqual(rebuilt["w1"]["duration_ms"], 42)

    def test_ungoverned_terminal_is_not_recognized_as_complete(self):
        """The new terminal marker is deliberately distinct: a
        child_work_from_ledger reader that only knows `started`/`blocked`/
        `ended` never mis-reads an ungoverned-terminal record as a normal
        completion."""
        records = [{
            "work_id": "w2",
            "state": guard_broker.CHILD_LIFECYCLE_UNGOVERNED_TERMINAL,
            "ts": "t0", "parent_work_id": "p2",
        }]
        rebuilt = measure.child_work_from_ledger(records)
        self.assertNotEqual(rebuilt["w2"].get("work_state"), "complete")


# --------------------------------------------------------------------------- #
# cowork_trace.py: signature/output-shape stability (additive-only).         #
# --------------------------------------------------------------------------- #


class TraceSignatureStabilityTest(unittest.TestCase):
    def test_new_work_id_unchanged(self):
        wid = trace_store.new_work_id()
        self.assertTrue(workunit._UUID_RE.match(wid))

    def test_work_meta_old_call_shape_unchanged(self):
        meta = trace_store.work_meta("w1", "productive", usage_scope="turn_native",
                                     duration_ms=10, parent_work_id="p1",
                                     work_kind="child")
        self.assertEqual(meta, {
            "work_id": "w1", "work_class": "productive",
            "usage_scope": "turn_native", "duration_ms": 10,
            "parent_work_id": "p1", "work_kind": "child",
        })

    def test_work_meta_minimal_call_shape_unchanged(self):
        meta = trace_store.work_meta("w1", "productive")
        self.assertEqual(meta, {"work_id": "w1", "work_class": "productive"})

    def test_work_meta_new_kwargs_are_additive_only(self):
        meta = trace_store.work_meta(
            "w1", "productive", governed_child_policy="inherit",
            graph_revision=3)
        self.assertEqual(meta["governed_child_policy"], "inherit")
        self.assertEqual(meta["graph_revision"], 3)
        without = trace_store.work_meta("w1", "productive")
        self.assertNotIn("governed_child_policy", without)
        self.assertNotIn("graph_revision", without)

    def test_identity_meta_old_call_shape_unchanged(self):
        meta = trace_store.identity_meta(
            controller="claude", provider="anthropic", model="sonnet",
            model_source="live_event", controller_session_id="cs1",
            effort="high", effort_source="config_pinned")
        self.assertEqual(set(meta), {
            "controller", "provider", "model", "model_source", "effort",
            "effort_source", "controller_session_id",
        })
        self.assertEqual(meta["controller"], "claude")
        self.assertEqual(meta["model_source"], "live_event")

    def test_identity_meta_new_kwargs_are_additive_only(self):
        meta = trace_store.identity_meta(
            controller="claude", candidate_manifest_digest="a" * 64,
            candidate_index=0)
        self.assertEqual(meta["candidate_manifest_digest"], "a" * 64)
        self.assertEqual(meta["candidate_index"], 0)
        without = trace_store.identity_meta(controller="claude")
        self.assertNotIn("candidate_manifest_digest", without)
        self.assertNotIn("candidate_index", without)


class WorkUnitJoinKeyResidualTest(unittest.TestCase):
    """Frozen-plan residual R2: trace identity primitives remain one
    join-key component of WorkUnit, never a competing identifier."""

    def test_new_work_id_output_is_a_valid_work_unit_work_id(self):
        wid = trace_store.new_work_id()
        wu = _make_work_unit(work_id=wid)
        validated = workunit.validate_work_unit(wu)
        self.assertEqual(validated["work_id"], wid.lower())

    def test_work_meta_work_id_field_name_matches_work_unit(self):
        meta = trace_store.work_meta("w1", "productive")
        self.assertIn("work_id", meta)
        wu = _make_work_unit()
        self.assertIn("work_id", wu)

    def test_identity_meta_fields_match_work_unit_field_names(self):
        meta = trace_store.identity_meta(
            controller="claude", provider="anthropic", effort="high")
        wu = _make_work_unit()
        for field in ("controller", "provider", "effort"):
            self.assertIn(field, meta)
            self.assertIn(field, wu)


# --------------------------------------------------------------------------- #
# Dispatch-time dependency-graph declaration check.                          #
# --------------------------------------------------------------------------- #


class GraphDeclarationCheckTest(_M2EnvMixin, unittest.TestCase):
    def _append(self, session_id, nodes):
        return state_store.append_graph_revision(session_id, nodes)

    def _node(self, work_id, predecessors=(), candidate=None, index=None,
             gpolicy="inherit"):
        return {
            "work_id": work_id,
            "candidate_manifest_digest": candidate,
            "candidate_index": index,
            "governed_child_policy": gpolicy,
            "predecessor_work_ids": list(predecessors),
        }

    def test_no_declared_revision_trivially_passes(self):
        wu = _make_work_unit(graph_revision=None)
        result = preflight.check_dependency_graph_declaration(wu, _uuid())
        self.assertTrue(result["ok"])

    def test_declared_revision_never_stored_fails_closed(self):
        wu = _make_work_unit(graph_revision=1)
        result = preflight.check_dependency_graph_declaration(wu, _uuid())
        self.assertFalse(result["ok"])
        self.assertEqual(result["capability"], "dependency_graph_declaration")

    def test_valid_declaration_passes(self):
        session_id = _uuid()
        work_id = _uuid()
        self._append(session_id, [self._node(work_id)])
        wu = _make_work_unit(work_id=work_id, graph_revision=1)
        result = preflight.check_dependency_graph_declaration(wu, session_id)
        self.assertTrue(result["ok"])

    def test_self_edge_fails_closed(self):
        session_id = _uuid()
        work_id = _uuid()
        wu = _make_work_unit(work_id=work_id, graph_revision=1,
                             predecessor_work_ids=[work_id])
        # Store a distinct valid revision 1 first (so the declared revision
        # exists), then declare a self-edge against it.
        other = _uuid()
        self._append(session_id, [self._node(other)])
        result = preflight.check_dependency_graph_declaration(wu, session_id)
        self.assertFalse(result["ok"])
        self.assertIn("self_edge", result["reason"])

    def test_dangling_predecessor_fails_closed(self):
        session_id = _uuid()
        work_id = _uuid()
        missing_pred = _uuid()
        self._append(session_id, [self._node(_uuid())])
        wu = _make_work_unit(work_id=work_id, graph_revision=1,
                             predecessor_work_ids=[missing_pred])
        result = preflight.check_dependency_graph_declaration(wu, session_id)
        self.assertFalse(result["ok"])
        self.assertIn("dangling_predecessor", result["reason"])

    def test_cycle_fails_closed(self):
        session_id = _uuid()
        a, b = _uuid(), _uuid()
        # a -> b -> a is a genuine cycle once both are in the same revision;
        # store b naming a as predecessor, then declare a naming b.
        self._append(session_id, [self._node(a), self._node(b, predecessors=[a])])
        wu = _make_work_unit(work_id=a, graph_revision=1,
                             predecessor_work_ids=[b])
        result = preflight.check_dependency_graph_declaration(wu, session_id)
        self.assertFalse(result["ok"])
        self.assertIn("cycle", result["reason"])

    def test_cross_candidate_fan_in_fails_closed(self):
        session_id = _uuid()
        p1, p2, child = _uuid(), _uuid(), _uuid()
        self._append(session_id, [
            self._node(p1, candidate="a" * 64, index=0),
            self._node(p2, candidate="b" * 64, index=0),
        ])
        wu = _make_work_unit(work_id=child, graph_revision=1,
                             predecessor_work_ids=[p1, p2])
        result = preflight.check_dependency_graph_declaration(wu, session_id)
        self.assertFalse(result["ok"])
        self.assertIn("cross_candidate_fan_in", result["reason"])

    def test_cross_policy_fan_in_fails_closed(self):
        session_id = _uuid()
        p1, p2, child = _uuid(), _uuid(), _uuid()
        self._append(session_id, [
            self._node(p1, gpolicy="inherit"),
            self._node(p2, gpolicy="isolated"),
        ])
        wu = _make_work_unit(work_id=child, graph_revision=1,
                             predecessor_work_ids=[p1, p2])
        result = preflight.check_dependency_graph_declaration(wu, session_id)
        self.assertFalse(result["ok"])
        self.assertIn("cross_policy_fan_in", result["reason"])


class DecideWorkUnitPreflightTest(_M2EnvMixin, unittest.TestCase):
    def test_graph_rejection_advances_to_rejected_preflight(self):
        session_id = _uuid()
        wu = _make_work_unit(graph_revision=1)  # never stored
        manifest = {"status": {"phase": "compiling"}}
        new_manifest, new_wu, reason_code = preflight.decide_work_unit_preflight(
            wu, manifest, session_id)
        self.assertIs(new_manifest, manifest)
        self.assertEqual(new_wu["lifecycle_state"], "rejected_preflight")
        self.assertTrue(new_wu["terminal_reason"])
        self.assertEqual(reason_code, "preflight_rejected")
        # never persisted -- this function performs no I/O of its own.
        self.assertFalse(state_store.read_work_unit_history(
            session_id, wu["work_id"]))

    def test_manifest_failure_advances_to_rejected_preflight(self):
        session_id = _uuid()
        wu = _make_work_unit(graph_revision=None)
        manifest = {
            "capability": {
                "inputs": [], "outputs": [], "runtime_roots": ["/nonexistent-x"],
                "private_paths": [], "guard_required": False, "socket": None,
                "kernel_boundary": {}, "artifact_writes": [],
                "action_classes": [], "command_adapters": {},
            },
            "binding": {"controller": "claude", "worktree": None},
        }
        _, new_wu, reason_code = preflight.decide_work_unit_preflight(
            wu, manifest, session_id, platform="darwin")
        self.assertEqual(new_wu["lifecycle_state"], "rejected_preflight")
        self.assertEqual(reason_code, "preflight_rejected")

    # ------------------------------------------------------------------- #
    # M1: illegal preflight transitions must fail closed without          #
    # returning a deceptively unchanged nonterminal WorkUnit or           #
    # rewriting an existing terminal reason. State-matrix regressions.    #
    # ------------------------------------------------------------------- #

    def test_illegal_transition_from_running_returns_unchanged_work_unit(self):
        """A WorkUnit that is NOT in 'preflighting' (the only state either
        preflight event legally applies to) must come back byte-for-byte
        unchanged, with reason_code=='illegal_transition' -- never a
        deceptively unchanged, still-nonterminal WorkUnit indistinguishable
        from a pass."""
        session_id = _uuid()
        wu = _make_work_unit(lifecycle_state="running", graph_revision=None)
        manifest = {"status": {"phase": "compiling"}}
        new_manifest, new_wu, reason_code = preflight.decide_work_unit_preflight(
            wu, manifest, session_id)
        self.assertEqual(reason_code, "illegal_transition")
        self.assertIs(new_wu, wu)
        self.assertEqual(new_wu["lifecycle_state"], "running")
        self.assertIsNone(new_wu["terminal_reason"])

    def test_illegal_transition_from_pending_via_graph_check_unchanged(self):
        """Same guarantee when the graph-declaration check is what would
        have failed: the graph rejection reason must never be applied to a
        WorkUnit outside 'preflighting'."""
        session_id = _uuid()
        wu = _make_work_unit(lifecycle_state="pending", graph_revision=1)
        manifest = {"status": {"phase": "compiling"}}
        _, new_wu, reason_code = preflight.decide_work_unit_preflight(
            wu, manifest, session_id)
        self.assertEqual(reason_code, "illegal_transition")
        self.assertIs(new_wu, wu)
        self.assertIsNone(new_wu["terminal_reason"])

    def test_illegal_transition_never_rewrites_existing_terminal_reason(self):
        """A WorkUnit that is already terminal (e.g. 'failed') and is
        illegally re-offered to preflight must keep its ORIGINAL
        terminal_reason -- never overwritten by a graph-declaration or
        manifest-refusal message from a transition that never legally
        happened."""
        session_id = _uuid()
        wu = _make_work_unit(lifecycle_state="failed", graph_revision=1,
                             terminal_reason="original cause: OOM")
        manifest = {"status": {"phase": "compiling"}}
        _, new_wu, reason_code = preflight.decide_work_unit_preflight(
            wu, manifest, session_id)
        self.assertEqual(reason_code, "illegal_transition")
        self.assertIs(new_wu, wu)
        self.assertEqual(new_wu["lifecycle_state"], "failed")
        self.assertEqual(new_wu["terminal_reason"], "original cause: OOM")

    def test_illegal_transition_from_awaiting_gate_unchanged(self):
        session_id = _uuid()
        wu = _make_work_unit(lifecycle_state="awaiting_gate",
                             graph_revision=None)
        manifest = {"status": {"phase": "compiling"}}
        _, new_wu, reason_code = preflight.decide_work_unit_preflight(
            wu, manifest, session_id)
        self.assertEqual(reason_code, "illegal_transition")
        self.assertIs(new_wu, wu)


# --------------------------------------------------------------------------- #
# Child-dispatch inheritance: controller/model/effort/policy, fail-closed    #
# on any missing field.                                                      #
# --------------------------------------------------------------------------- #


_FULL_PARENT = {
    "controller": "claude", "controller_source": "config_pinned",
    "model": "sonnet", "model_source": "config_pinned",
    "effort": "high", "effort_source": "config_pinned",
    "governed_child_policy": "inherit",
}


class ChildInheritanceFailClosedTest(unittest.TestCase):
    def test_all_four_fields_present_allows(self):
        decision = action_policy.decide_child_governed(
            {}, _FULL_PARENT, ("claude",))
        self.assertTrue(decision["allow"])
        self.assertEqual(decision["governed_child_policy"], "inherit")

    def test_missing_controller_fails_closed(self):
        parent = dict(_FULL_PARENT)
        parent.pop("controller")
        decision = action_policy.decide_child_governed({}, parent, ("claude",))
        self.assertFalse(decision["allow"])

    def test_missing_model_fails_closed(self):
        parent = dict(_FULL_PARENT)
        parent.pop("model")
        decision = action_policy.decide_child_governed({}, parent, ("claude",))
        self.assertFalse(decision["allow"])

    def test_missing_effort_fails_closed(self):
        parent = dict(_FULL_PARENT)
        parent.pop("effort")
        decision = action_policy.decide_child_governed({}, parent, ("claude",))
        self.assertFalse(decision["allow"])

    def test_missing_governed_child_policy_fails_closed(self):
        parent = dict(_FULL_PARENT)
        parent.pop("governed_child_policy")
        decision = action_policy.decide_child_governed({}, parent, ("claude",))
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "parent_policy_unresolved")

    def test_denied_policy_fails_closed_even_with_other_three_fields(self):
        parent = dict(_FULL_PARENT, governed_child_policy="denied")
        decision = action_policy.decide_child_governed({}, parent, ("claude",))
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "governed_child_denied")

    def test_isolated_policy_permits_like_inherit(self):
        parent = dict(_FULL_PARENT, governed_child_policy="isolated")
        decision = action_policy.decide_child_governed({}, parent, ("claude",))
        self.assertTrue(decision["allow"])

    def test_policy_check_never_loosens_a_decide_child_denial(self):
        # controller not in allowed set -> decide_child itself denies; the
        # policy layer must not override that with an allow.
        decision = action_policy.decide_child_governed(
            {}, _FULL_PARENT, ("codex",))
        self.assertFalse(decision["allow"])

    def test_allowed_child_policies_restricts_further(self):
        decision = action_policy.decide_child_governed(
            {}, _FULL_PARENT, ("claude",), allowed_child_policies=("isolated",))
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "child_policy_not_permitted")

    def test_parent_effective_from_work_unit_projection(self):
        wu = _make_work_unit(controller="claude", effective_model="sonnet",
                             effort="high", governed_child_policy="inherit")
        projected = action_policy.parent_effective_from_work_unit(
            wu, controller_source="config_pinned",
            model_source="live_event", effort_source="config_pinned")
        decision = action_policy.decide_child_governed(
            {}, projected, ("claude",))
        self.assertTrue(decision["allow"])
        self.assertEqual(decision["effective"]["model_source"], "live_event")

    def test_parent_effective_from_work_unit_missing_model_fails_closed(self):
        wu = _make_work_unit(effective_model=None)
        projected = action_policy.parent_effective_from_work_unit(
            wu, controller_source="config_pinned",
            model_source="config_pinned", effort_source="config_pinned")
        decision = action_policy.decide_child_governed(
            {}, projected, ("claude",))
        self.assertFalse(decision["allow"])

    def test_parent_effective_from_work_unit_never_fabricates_provenance(self):
        """M3 regression: omitting the explicit `*_source` arguments must
        fail closed, never silently label a present WorkUnit field
        'config_pinned' from its mere presence."""
        wu = _make_work_unit(controller="claude", effective_model="sonnet",
                             effort="high", governed_child_policy="inherit")
        projected = action_policy.parent_effective_from_work_unit(wu)
        self.assertIsNone(projected["controller_source"])
        self.assertIsNone(projected["model_source"])
        self.assertIsNone(projected["effort_source"])
        decision = action_policy.decide_child_governed(
            {}, projected, ("claude",))
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "parent_identity_unresolved")

    def test_parent_effective_from_work_unit_rejects_unknown_source(self):
        """An unrecognized source string (not in KNOWN_IDENTITY_SOURCES)
        must also fail closed, never pass through as a fabricated label."""
        wu = _make_work_unit(controller="claude", effective_model="sonnet",
                             effort="high", governed_child_policy="inherit")
        projected = action_policy.parent_effective_from_work_unit(
            wu, controller_source="made_up_source",
            model_source="config_pinned", effort_source="config_pinned")
        self.assertIsNone(projected["controller_source"])
        decision = action_policy.decide_child_governed(
            {}, projected, ("claude",))
        self.assertFalse(decision["allow"])


if __name__ == "__main__":
    unittest.main()
