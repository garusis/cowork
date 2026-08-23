#!/usr/bin/env python3
"""M2 Package F: end-to-end negative-control suite.

Independent, fresh proof — written without editing any existing test file —
that every negative control the frozen brief names is refused by the REAL,
fully-integrated (Package A-E) production seam, not merely by an isolated
pure function. Each test below drives `cowork.run_flow`/`cowork.run_scout`/
`cowork._role_loop`, the real `cowork_bridge._guard_runtime` GuardBroker
socket, the real durable `cowork_state.append_graph_revision` seam Package B
built, or — for the dispatch-time graph-declaration shape (F-LIVE-GRAPH-
WIRING-1, resolved by the M2 Package E live-graph-wiring correction now on
the integrated base) — the real `cowork.run_scout` entry point, whose sole
`_compile_role_manifest` call site now applies
`cowork_preflight.check_dependency_graph_declaration` to the current
WorkUnit's own persisted graph declaration unconditionally, before
`cowork_preflight.run_manifest_preflight` is ever reached.

Named negative controls (one test each, exactly the brief's fixed list of 13
plus the two additional issue-#11/#30 controls Package E introduced):

    1.  uncorrelated children
    2.  guard disappearance
    3.  invalid policy transition
    4.  controller abort
    5.  EOF
    6.  external kill (positive durable-terminal-record assertion)
    7.  repeated identical repair
    8.  graph cycles
    9.  dangling predecessors
    10. duplicate work IDs
    11. self-edges
    12. cross-candidate fan-in
    13. cross-policy fan-in
    14. context-ack failure before first accepted send (issue #11)
    15. controller-switch interruption (issue #30)

Run standalone:

    python3 -m unittest scripts/test_m2_negative_controls.py -v
"""

import hashlib
import io
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_action_guard as action_guard  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_control_plane as control_plane  # noqa: E402
import cowork_dispatch_manifest as manifest_mod  # noqa: E402
import cowork_policy as policy  # noqa: E402
import cowork_recovery_breaker as recovery_breaker  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_workunit as workunit  # noqa: E402


# --------------------------------------------------------------------------- #
# Self-contained test doubles (deliberately NOT imported from test_cowork.py, #
# so this file's proof stands independent of that suite's own fixtures).      #
# --------------------------------------------------------------------------- #

class _InertProc:
    def __init__(self):
        self.stdout = io.StringIO("")
        self.stdin = io.StringIO()
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _RecordingPopen:
    """Records every argv a bridge tried to spawn; proves zero real spawn."""

    def __init__(self):
        self.calls = []

    def __call__(self, command, *args, **kwargs):
        self.calls.append(list(command))
        return _InertProc()


class _BridgeSubprocess:
    def __init__(self, real, popen):
        self._real = real
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(self._real, name)


def patch_bridge_popen(popen):
    return mock.patch.object(
        bridge, "subprocess", _BridgeSubprocess(bridge.subprocess, popen))


class _RecordingClaudeSpawn:
    def __init__(self):
        self.calls = []

    def __call__(self, command, stdin_text):
        self.calls.append(list(command))
        return [{"type": "result", "subtype": "success"}]


def _uuid():
    return str(uuid.uuid4())


class _M2E2EBase(unittest.TestCase):
    """Isolated COWORK_SESSIONS_ROOT + unconditional policy reset per test,
    and `gather_context_interactive` stubbed to fail fast rather than block
    on real stdin — mirrors the isolation discipline the E suite itself
    established, reproduced independently here."""

    def setUp(self):
        policy.deactivate()
        self.addCleanup(policy.deactivate)

        def _no_prompt(*a, **kw):
            raise AssertionError(
                "reached the interactive goal prompt; pass --context or "
                "resume a session with a saved lead session id")
        patcher = mock.patch.object(
            cowork, "gather_context_interactive", _no_prompt)
        patcher.start()
        self.addCleanup(patcher.stop)

        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore)

    def _dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def _tmp_session(self):
        return os.path.join(self._dir(), ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _sha(self, path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def _events(self, spath):
        saved = state_store.load(spath)
        trace_path = trace_store.trace_path_for(
            state_store.get_session_uuid(saved))
        if not os.path.exists(trace_path):
            return []
        with open(trace_path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _session(self, uuid_str, phase, controllers, team=None):
        spath = self._tmp_session()
        team = team or list(controllers)
        state = state_store.ensure_session(spath, None, uuid_str)
        cfg = cowork.default_config(team)
        for role, controller in controllers.items():
            cfg[role] = dict(cfg[role], controller=controller)
        state = state_store.save_config(spath, team, cfg, prior=state)
        state_store.save_phase(spath, phase, prior=state)
        return spath


# =============================================================================
# 1. Uncorrelated children
# =============================================================================

class UncorrelatedChildrenTest(_M2E2EBase):
    """A correlation-unavailable child dispatch attempt, exercised through
    the REAL production dispatch/broker flow -- a live GuardBroker thread
    spun up by the real `cowork_bridge._guard_runtime`, reached over its real
    AF_UNIX socket by the real `cowork_action_guard` hook transport -- must
    produce zero child dispatch plus a durable governed terminal record."""

    def test_uncorrelated_child_blocked_with_durable_terminal_record(self):
        session_uuid = _uuid()
        assets = state_store.session_assets_dir(session_uuid)
        os.makedirs(assets, exist_ok=True)
        trace = trace_store.Trace(
            trace_store.trace_path_for(session_uuid),
            session_uuid=session_uuid, run_id="R")

        with mock.patch.object(
                bridge, "kernel_write_boundary",
                return_value={"available": True, "platform": "darwin"}), \
                mock.patch.object(
                    bridge.controller_profiles, "reference_claude_session",
                    return_value={"cleanup_kind": None, "protected_paths": (),
                                 "credential_copied": False}):
            runtime = bridge._guard_runtime(
                trace, "builder", assets, "sonnet", "high", True,
                controller_session_id=_uuid())
        self.addCleanup(bridge._close_guard_runtime, runtime)
        self.assertFalse(runtime["delegation_allowed"])

        with open(runtime["context_path"]) as fh:
            context = json.load(fh)
        payload = action_guard.enrich_payload(
            {"tool_name": "Agent", "tool_use_id": "f-negctrl-tool",
             "tool_input": {"subagent_type": "Explore", "prompt": "go"}},
            context)
        response = action_guard.forward(
            payload, context["socket_path"], context["token"])

        self.assertEqual(
            response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "child_agent_correlation_unavailable",
            response["hookSpecificOutput"]["permissionDecisionReason"])
        child_work_id = response["child_work_id"]
        self.assertTrue(child_work_id)

        children_path = state_store.children_path_for(session_uuid)
        with open(children_path) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 1,
                         "zero child dispatch: exactly one durable (blocked) "
                         "record, never a started one")
        self.assertEqual(rows[0]["work_id"], child_work_id)
        self.assertEqual(rows[0]["state"], "blocked")
        self.assertNotEqual(rows[0]["state"], "started")
        self.assertEqual(rows[0]["reason"],
                         "child_agent_correlation_unavailable")


# =============================================================================
# Shared row-driven harness for controls 2, 4, 5: guard disappearance,
# controller abort, EOF -- each drives the real `run_flow`/`run_scout` seam
# to an explicit, non-completed terminal PhaseState.
# =============================================================================

class _RefusingSession:
    controller = "opencode"

    def __init__(self, error_type):
        self.error_type = error_type

    def send(self, text, meta=None):
        return {"ok": False, "result": "error", "error_type": self.error_type}

    def close(self):
        pass


class _KeyboardInterruptSession:
    controller = "opencode"

    def send(self, text, meta=None):
        raise KeyboardInterrupt()

    def close(self):
        pass


class _NoStatusSession:
    controller = "opencode"

    def send(self, text, meta=None):
        return {"ok": True, "result": "ok"}

    def close(self):
        pass


class _SigtermSession:
    controller = "opencode"

    def send(self, text, meta=None):
        os.kill(os.getpid(), signal.SIGTERM)
        return {"ok": True, "result": "ok"}  # unreachable

    def close(self):
        pass


class NonCompletionMatrixTest(_M2E2EBase):
    """Named negative controls 2 (guard disappearance), 4 (controller
    abort), and 5 (EOF): each drives the real production `run_flow`/
    `run_scout`/`_advance_phase` seam to an explicit terminal PhaseState
    other than `completed`, with no gate-validation evidence bound."""

    def _run_row(self, uuid_str, session, headless, io_in=None):
        spath = self._session(
            uuid_str, "scouting", {"scout": "opencode"}, team=["scout"])

        def fake_run_scout(config, context, selected, **kw):
            return cowork.run_scout(
                config, context, selected,
                session_factory=lambda *a, **k: session, **kw)

        argv = ["--session-file", spath, "--context", "f-negctrl context"]
        if headless:
            argv += ["--headless"]
        try:
            cowork.run_flow(
                self._args(argv), io_in=(io_in or io.StringIO()),
                io_out=io.StringIO(), which=lambda c: "/bin/" + c,
                run_scout_fn=fake_run_scout)
        except SystemExit as exc:
            self.assertEqual(exc.code, 128 + signal.SIGTERM)
        work_id = cowork._role_work_id(uuid_str, "scout", 0)
        return state_store.current_phase_state(uuid_str, work_id)

    def test_guard_disappearance_reaches_failed_never_completed(self):
        current = self._run_row(
            "f-negctrl-guard-disappearance",
            _RefusingSession("guard_unavailable"), True, None)
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "failed")
        self.assertIn(current["state"], control_plane.TERMINAL_STATES)
        self.assertNotEqual(current["state"], "completed")
        self.assertNotIn("gate_validation", current.get("evidence") or {})

    def test_controller_abort_reaches_aborted_never_completed(self):
        current = self._run_row(
            "f-negctrl-controller-abort", _KeyboardInterruptSession(), True,
            None)
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "aborted")
        self.assertIn(current["state"], control_plane.TERMINAL_STATES)
        self.assertNotEqual(current["state"], "completed")
        self.assertNotIn("gate_validation", current.get("evidence") or {})

    def test_eof_reaches_cancelled_never_completed(self):
        current = self._run_row(
            "f-negctrl-eof", _NoStatusSession(), False, io.StringIO(""))
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "cancelled")
        self.assertIn(current["state"], control_plane.TERMINAL_STATES)
        self.assertNotEqual(current["state"], "completed")
        self.assertNotIn("gate_validation", current.get("evidence") or {})


# =============================================================================
# 3. Invalid policy transition
# =============================================================================

class InvalidPolicyTransitionTest(_M2E2EBase):
    """A conflicting/invalid controller policy transition submitted through
    the REAL `--switch-controller` `run_flow` seam causes zero dispatch and
    leaves the persisted + active policy byte-identical to pre-attempt."""

    def test_rejected_transition_zero_dispatch_byte_identical(self):
        spath = self._session("F-NEGCTRL-POLICY", "planning",
                              {"planner": "claude"}, team=["scout", "planner"])
        before_bytes = self._sha(spath)
        before_active = policy.active_meta()

        popen = _RecordingPopen()
        claude_spawn = _RecordingClaudeSpawn()
        with patch_bridge_popen(popen), \
                mock.patch.object(bridge, "_real_claude_spawn", claude_spawn), \
                mock.patch.object(
                    policy, "decide_controller_policy_transition",
                    return_value={"outcome": "rejected",
                                 "reason": "stale_revision", "state": None}):
            rc = cowork.run_flow(
                self._args(["--session-file", spath,
                            "--switch-controller", "planner=codex"]),
                io_in=io.StringIO(), io_out=io.StringIO(),
                which=lambda c: "/bin/" + c,
                run_planner_fn=lambda *a, **k: 0)

        self.assertEqual(rc, 1)
        self.assertEqual(popen.calls, [], "zero controller spawns")
        self.assertEqual(claude_spawn.calls, [], "zero controller spawns")
        self.assertEqual(self._sha(spath), before_bytes,
                         "a rejected transition must persist nothing")
        after_active = policy.active_meta()
        for field in ("mode", "allowed", "raw"):
            self.assertEqual(after_active[field], before_active[field])


# =============================================================================
# 6. External kill -- positive durable-terminal-record assertion
# =============================================================================

class ExternalKillPositiveTerminalRecordTest(_M2E2EBase):
    """A real SIGTERM, delivered at the interactive gate read through the
    SAME production `run_flow` handler and the REAL (not bypassed)
    `cowork.run_scout` seam -- so a WorkUnit is genuinely minted -- must
    produce a POSITIVE durable terminal record, not merely the absence of
    `completed`: `aborted`/evidence.reason=='sigterm' on PhaseState, mirrored
    onto the SAME WorkUnit join key (MJ-4)."""

    class _GateSession:
        controller = "opencode"

        def __init__(self, intel_path):
            self.intel_path = intel_path

        def send(self, text, meta=None):
            with open(self.intel_path, "w") as fh:
                json.dump({"status": "ready_for_review",
                          "result": {"finding": "F1"}}, fh)
            return {"ok": True, "result": "ok"}

        def close(self):
            pass

    class _KillOnReadline(io.StringIO):
        def readline(self, *a, **kw):
            os.kill(os.getpid(), signal.SIGTERM)
            return super().readline(*a, **kw)

    def test_sigterm_at_gate_positive_durable_aborted_record(self):
        session_uuid = _uuid()
        spath = self._session(
            session_uuid, "scouting", {"scout": "opencode"}, team=["scout"])
        intel_path = os.path.join(
            state_store.session_assets_dir(session_uuid), "scout.intel.json")
        os.makedirs(os.path.dirname(intel_path), exist_ok=True)
        session = self._GateSession(intel_path)

        def fake_run_scout(config, context, selected, **kw):
            return cowork.run_scout(
                config, context, selected,
                session_factory=lambda *a, **k: session, **kw)

        with self.assertRaises(SystemExit) as ctx:
            cowork.run_flow(
                self._args(["--session-file", spath,
                           "--context", "f-negctrl gate sigterm"]),
                io_in=self._KillOnReadline(), io_out=io.StringIO(),
                which=lambda c: "/bin/" + c, run_scout_fn=fake_run_scout)
        self.assertEqual(ctx.exception.code, 128 + signal.SIGTERM)

        work_id = cowork._role_work_id(session_uuid, "scout", 0)

        # Positive durable-terminal-record assertions (not absence-only):
        current = state_store.current_phase_state(session_uuid, work_id)
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "aborted")
        self.assertEqual(current["evidence"].get("reason"), "sigterm")
        self.assertIsNotNone(current.get("reason_code"))
        self.assertNotEqual(current["state"], "running")
        self.assertNotEqual(current["state"], "completed")

        # MJ-4: the SAME WorkUnit join key positively mirrors terminal truth.
        work_unit = state_store.work_unit_from_history_record(
            state_store.current_work_unit_state(session_uuid, work_id))
        self.assertIsNotNone(work_unit)
        self.assertEqual(work_unit["lifecycle_state"], "aborted")
        self.assertIsNotNone(work_unit["terminal_reason"])
        self.assertNotEqual(work_unit["lifecycle_state"], "running")


# =============================================================================
# 7. Repeated identical repair
# =============================================================================

class RepeatedIdenticalRepairTest(_M2E2EBase):
    """D's durable recovery breaker, integrated into the live controller-
    failure retry gate: the (threshold+1)th identical-cause retry request is
    refused BEFORE another dispatch, with a stable, distinct reason code."""

    def _path(self):
        d = self._dir()
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def test_fourth_identical_cause_retry_blocked_before_dispatch(self):
        path = self._path()
        session_uuid = _uuid()
        manifest_dir = os.path.dirname(
            state_store.manifest_path_for(session_uuid, "scout"))
        os.makedirs(manifest_dir, exist_ok=True)
        manifest = manifest_mod.compile_manifest(
            "scout",
            {"inputs": [], "outputs": [], "runtime_roots": [],
             "private_paths": [], "guard_required": False, "socket": None,
             "kernel_boundary": {"crosses": []}, "artifact_writes": [],
             "action_classes": [], "command_adapters": {}},
            {"work_id": "scout", "controller": "claude", "model": None,
             "effort": None, "config_digest": "e" * 64,
             "instruction_digests": {}, "policy_snapshot": {},
             "worktree": None, "candidate_snapshot": None,
             "guard_snapshot": None})
        manifest = manifest_mod.manifest_proven(manifest, [])
        manifest_mod.persist_manifest(
            state_store.manifest_path_for(session_uuid, "scout"), manifest)

        class FailingSession:
            controller = "claude"

            def send(self, text, meta=None):
                return {"ok": False, "result": "error",
                       "error_type": "ProviderError"}

            def close(self):
                pass

        trace = trace_store.Trace(
            trace_store.trace_path_for(session_uuid),
            session_uuid=session_uuid, run_id="R")
        for _ in range(recovery_breaker.TRIP_THRESHOLD):
            recovery_breaker.attempt(
                state_store.ledger_path_for(session_uuid), "scout",
                "e" * 64, "claude", manifest["digest"], "controller_failure")

        rc, outcome, _ = cowork._role_loop(
            FailingSession(), "seed", path, context="",
            io_in=io.StringIO("retry\n"), io_out=io.StringIO(),
            trace=trace, session_uuid=session_uuid, role_work_id="scout-wu",
            role="scout", phase="scouting")
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "ended")
        events = self._trace_events(session_uuid)
        blocked = [e for e in events
                  if e.get("event") == "user.action"
                  and e.get("action") == "controller_failure_retry_blocked"]
        self.assertEqual(len(blocked), 1)
        tripped_decisions = [e for e in events
                             if e.get("event") == "recovery.breaker.decision"
                             and e.get("tripped")]
        self.assertGreaterEqual(len(tripped_decisions), 1)

    def _trace_events(self, session_uuid):
        tpath = trace_store.trace_path_for(session_uuid)
        with open(tpath, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# =============================================================================
# 8-13. Dependency-graph negative controls: graph cycles, dangling
# predecessors, duplicate work IDs, self-edges, cross-candidate fan-in,
# cross-policy fan-in.
#
# The six pure graph shapes below exercise Package B's public
# `append_graph_revision`/`read_graph_revisions` durable store (Package A's
# `validate_revision` underneath) directly -- the store-append boundary,
# never a bypassed, test-only reimplementation of the validators.
#
# The seventh test in this class (F-LIVE-GRAPH-WIRING-1) instead drives the
# real, now fully wired dispatch-time seam: the M2 Package E live-graph-
# wiring correction on the integrated base makes `cowork.py`'s
# `_compile_role_manifest` -- the sole live call site of
# `cowork_preflight.run_manifest_preflight` -- apply
# `cowork_preflight.check_dependency_graph_declaration` to the current,
# attempt-scoped WorkUnit's OWN persisted `graph_revision` declaration
# unconditionally, before that manifest is ever preflighted, session-
# constructed, or dispatched. That test drives `cowork.run_scout` itself
# (never `check_dependency_graph_declaration` called directly), so it proves
# the real end-to-end pre-dispatch behavior: a WorkUnit-bound
# `rejected_preflight` outcome plus zero manifest/session/send dispatch.
# =============================================================================

def _node(work_id, predecessors=(), digest=None, index=None, gcp="inherit"):
    return {
        "work_id": work_id, "candidate_manifest_digest": digest,
        "candidate_index": index, "governed_child_policy": gcp,
        "predecessor_work_ids": list(predecessors),
    }


class DependencyGraphNegativeControlsTest(_M2E2EBase):

    def _assert_revision_rejected(self, session_uuid, nodes, code):
        with self.assertRaises(workunit.GraphValidationError) as ctx:
            state_store.append_graph_revision(session_uuid, nodes)
        codes = {v["code"] for v in ctx.exception.violations}
        self.assertIn(code, codes)
        self.assertEqual(state_store.read_graph_revisions(session_uuid), (),
                         "a rejected revision must durably persist nothing")

    def test_graph_cycles_rejected_by_durable_store(self):
        a, b = _uuid(), _uuid()
        self._assert_revision_rejected(
            _uuid(), [_node(a, predecessors=[b]), _node(b, predecessors=[a])],
            "cycle")

    def test_dangling_predecessors_rejected_by_durable_store(self):
        self._assert_revision_rejected(
            _uuid(), [_node(_uuid(), predecessors=[_uuid()])],
            "dangling_predecessor")

    def test_duplicate_work_ids_rejected_by_durable_store(self):
        dup = _uuid()
        self._assert_revision_rejected(
            _uuid(), [_node(dup), _node(dup)], "duplicate_work_id")

    def test_self_edges_rejected_by_durable_store(self):
        wid = _uuid()
        self._assert_revision_rejected(
            _uuid(), [_node(wid, predecessors=[wid])], "self_edge")

    def test_cross_candidate_fan_in_rejected_by_durable_store(self):
        p1, p2, child = _uuid(), _uuid(), _uuid()
        self._assert_revision_rejected(
            _uuid(),
            [_node(p1, digest="a" * 64, index=0),
             _node(p2, digest="b" * 64, index=0),
             _node(child, predecessors=[p1, p2])],
            "cross_candidate_fan_in")

    def test_cross_policy_fan_in_rejected_by_durable_store(self):
        p1, p2, child = _uuid(), _uuid(), _uuid()
        self._assert_revision_rejected(
            _uuid(),
            [_node(p1, gcp="inherit"), _node(p2, gcp="isolated"),
             _node(child, predecessors=[p1, p2])],
            "cross_policy_fan_in")

    def test_invalid_declaration_rejects_real_dispatch_path(self):
        """F-LIVE-GRAPH-WIRING-1, real end-to-end proof: a scout WorkUnit
        whose OWN persisted graph declaration is a self-edge (invalid) is
        driven through the REAL `cowork.run_scout` entry point -- never
        `check_dependency_graph_declaration` called directly -- and must be
        rejected before `_compile_role_manifest` ever reaches
        `run_manifest_preflight`, leaving a WorkUnit-bound
        `rejected_preflight` record and zero manifest/session/send
        dispatch."""
        session_uuid = _uuid()
        role_work_id = cowork._role_work_id(session_uuid, "scout", None)
        other_work_id = _uuid()
        # A validly-appended revision that says nothing about role_work_id
        # -- the WorkUnit's OWN declaration below is what is stale/tampered:
        # `check_dependency_graph_declaration` projects it into this stored
        # revision's node set and re-validates the substituted whole, so a
        # self-edge the store itself never accepted is still caught here.
        state_store.append_graph_revision(session_uuid, [_node(other_work_id)])
        state_store.mint_work_unit({
            "schema_version": workunit.SCHEMA_VERSION, "record": "WorkUnit",
            "work_id": role_work_id, "session_id": session_uuid,
            "phase": None, "role": "scout", "seat": 0, "round": 0,
            "attempt": 0, "controller": "opencode", "provider": "opencode",
            "requested_model": None, "effective_model": None, "effort": None,
            "candidate_manifest_digest": None, "candidate_index": None,
            "prompt_digest": None, "pending_turn_digest": None,
            "parent_work_id": None, "governed_child_policy": None,
            "graph_revision": 1, "predecessor_work_ids": [role_work_id],
            "fan_join_id": None,
            "lifecycle_state": "pending", "terminal_reason": None,
        })

        config = {"scout": {"controller": "opencode", "model": None,
                            "effort": None, "yolo": True,
                            "mode": "implement"}}

        def _never_send(controller, resume_session_id=None,
                        on_session_id=None):
            raise AssertionError(
                "a rejected graph declaration must never construct a "
                "controller session")

        rc = cowork.run_scout(
            config, "goal", ["scout"], io_in=io.StringIO("\n"),
            io_out=io.StringIO(), session_factory=_never_send,
            session_uuid=session_uuid)

        self.assertEqual(rc, 1, "a rejected graph declaration must stop "
                         "dispatch before the scout loop starts")

        current = state_store.current_phase_state(session_uuid, role_work_id)
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "rejected_preflight")
        self.assertEqual(current["reason_code"], "preflight_rejected")
        self.assertIn("self_edge",
                      current["evidence"]["dependency_graph_declaration"])

        work_unit = state_store.work_unit_from_history_record(
            state_store.current_work_unit_state(session_uuid, role_work_id))
        self.assertEqual(work_unit["lifecycle_state"], "rejected_preflight")
        self.assertEqual(work_unit["terminal_reason"], "preflight_rejected")

        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, "scout"))
        self.assertIsNone(
            manifest,
            "zero manifest dispatch: the manifest must never be compiled "
            "or persisted for a rejected graph declaration")


# =============================================================================
# 14. Context-acknowledgment failure before first accepted send (issue #11)
# =============================================================================

class ContextAckFailureTest(_M2E2EBase):
    """A failed FIRST send never acknowledges the context that rode it -- a
    resumed session redelivers BOTH the unseen context and the saved pending
    role request, through the real `run_flow`/`run_scout` seam."""

    class _ScriptedSession:
        controller = "claude"

        def __init__(self, ok):
            self.ok = ok
            self.sent = []

        def send(self, text, meta=None):
            self.sent.append(text)
            if self.ok:
                return {"ok": True, "result": "ok"}
            return {"ok": False, "result": "error",
                   "error_type": "ProviderError"}

        def close(self):
            pass

    def test_first_send_failure_withholds_ack_resume_redelivers_both(self):
        spath = self._tmp_session()
        failing = self._ScriptedSession(ok=False)

        def fake_run_scout_1(config, context, selected, **kw):
            return cowork.run_scout(
                config, context, selected,
                session_factory=lambda *a, **k: failing, **kw)

        rc = cowork.run_flow(
            self._args(["--team", "scout",
                       "--config", "scout=claude,yolo,plan",
                       "--context", "F-NEGCTRL-ORIGINAL-CONTEXT",
                       "--session-file", spath, "--headless"]),
            io_in=io.StringIO(), io_out=io.StringIO(),
            which=lambda c: "/bin/" + c, run_scout_fn=fake_run_scout_1)
        self.assertEqual(rc, 0)

        events = self._events(spath)
        self.assertEqual(
            [e for e in events if e["event"] == "context.ack"], [],
            "a first send that never got accepted must never ack context")
        saved = state_store.load(spath)
        self.assertEqual(
            state_store.role_context_gap(saved, "scout"),
            "F-NEGCTRL-ORIGINAL-CONTEXT")
        pending = state_store.read_pending_switch(saved, "scout")
        self.assertIsNotNone(pending)
        self.assertTrue(pending.get("pending_turn"))

        succeeding = self._ScriptedSession(ok=True)

        def fake_run_scout_2(config, context, selected, **kw):
            return cowork.run_scout(
                config, context, selected,
                session_factory=lambda *a, **k: succeeding, **kw)

        rc2 = cowork.run_flow(
            self._args(["--session-file", spath]),
            io_in=io.StringIO("\n"), io_out=io.StringIO(),
            which=lambda c: "/bin/" + c, run_scout_fn=fake_run_scout_2)
        self.assertEqual(rc2, 0)

        first_sent_text = succeeding.sent[0]
        referenced_paths = re.findall(
            r"(/\S+\.(?:txt|md|json))", first_sent_text)
        combined = ""
        for path in referenced_paths:
            try:
                with open(path) as fh:
                    combined += fh.read()
            except OSError:
                continue
        self.assertIn("F-NEGCTRL-ORIGINAL-CONTEXT", combined)
        self.assertIn(pending["pending_turn"], combined)

        events2 = self._events(spath)
        self.assertTrue(
            any(e["event"] == "context.ack" for e in events2))


# =============================================================================
# 15. Controller-switch mid-transition interruption (issue #30)
# =============================================================================

class ControllerSwitchInterruptionTest(_M2E2EBase):
    """Interrupting exactly at the real controller-switch seam's single
    persisting write leaves only the prior byte-identical identity or the
    fully-committed new one -- zero dispatch either way -- and a subsequent
    plain resume proceeds normally."""

    def test_replace_failure_leaves_prior_identity_intact_zero_dispatch(self):
        spath = self._session(
            "F-NEGCTRL-SWITCH-INTERRUPT", "planning", {"planner": "claude"},
            team=["scout", "planner"])
        state = state_store.load(spath)
        state["config"]["planner"]["model"] = "opus"
        state["config"]["planner"]["effort"] = "high"
        state_store.save(spath, state)
        before_bytes = self._sha(spath)

        real_replace = os.replace

        def selective_boom(src, dst, *a, **kw):
            if os.path.abspath(dst) == os.path.abspath(spath):
                raise OSError("simulated crash between clearing and "
                             "confirming controller/model/effort")
            return real_replace(src, dst, *a, **kw)

        popen = _RecordingPopen()
        claude_spawn = _RecordingClaudeSpawn()
        with patch_bridge_popen(popen), \
                mock.patch.object(bridge, "_real_claude_spawn", claude_spawn), \
                mock.patch.object(state_store.os, "replace",
                                  side_effect=selective_boom):
            rc = cowork.run_flow(
                self._args(["--session-file", spath,
                           "--switch-controller", "planner=codex"]),
                io_in=io.StringIO(), io_out=io.StringIO(),
                which=lambda c: "/bin/" + c,
                run_planner_fn=lambda *a, **k: 0)

        self.assertEqual(rc, 1)
        self.assertEqual(popen.calls, [])
        self.assertEqual(claude_spawn.calls, [])
        self.assertEqual(self._sha(spath), before_bytes)
        recovered = state_store.load(spath)
        self.assertEqual(recovered["config"]["planner"]["controller"],
                         "claude")
        self.assertEqual(recovered["config"]["planner"]["model"], "opus")
        self.assertEqual(recovered["config"]["planner"]["effort"], "high")
        self.assertEqual(recovered["phase"], "planning")

        events = self._events(spath)
        rejected = [e for e in events
                   if e["event"] == "controller.policy.rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0]["persisted"])
        self.assertEqual(policy.active_meta()["mode"], "unrestricted")

        rc = cowork.run_flow(
            self._args(["--session-file", spath,
                       "--context", "post-recovery continuation"]),
            io_in=io.StringIO(), io_out=io.StringIO(),
            which=lambda c: "/bin/" + c,
            run_planner_fn=lambda *a, **k: 0)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
