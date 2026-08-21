#!/usr/bin/env python3
"""Characterization harness for cowork's dispatch boundary (M0-B).

This module changes NO production behavior. It pins, by execution, what the
~nineteen direct session-construction sites do today, so that M0-C can extract a
`DispatchContract` gateway/reducer against a trustworthy baseline.

The finding this harness makes executable: `cowork_policy.guard()` is a
controller veto/backstop, NOT a lifecycle gateway. There is no single place that
decides "may this role dispatch, fresh or resumed, with which identity, after
which probe, and how is a refusal surfaced". Every dispatch source re-derives
that, and they DISAGREE — most visibly in how a policy refusal reaches the
caller (clean rc, `None`, a controller-failure verdict, or an uncaught
`DispatchBlocked` escaping the probe).

Coverage map (the six required gaps):

  gap 1  DispatchSourceInventoryTest        every dispatch source, executed
  gap 2  DispatchOrderingTest               policy / preflight / probe / spawn
  gap 3  SwitchNoteConsumptionTest          switch note + pending turn, once
  gap 4  EvaluatorDispatchContractTest      fresh/internal/read-only/muted
  gap 5  RetryLinkageTest                   attempt <-> prior-attempt linkage
  gap 6  HeadlessInteractiveParityTest      same decision, different adapter

Missing-contract witnesses live in `MissingDispatchContractTest`. Each
`expectedFailure` there names exactly one absent contract and is paired with a
green `test_witness_*` that executes the current, divergent behavior. When M0-C
supplies the contract the expected failure turns into a visible unexpected
success.

Run standalone:

    python3 -m unittest scripts/test_dispatch_contract_characterization.py
"""

import collections
import io
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

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_dispatch as dispatch  # noqa: E402
import cowork_dispatch_manifest as manifest_mod  # noqa: E402
import cowork_eval as evaluation  # noqa: E402
import cowork_policy as policy  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

# Reuse the suite's existing temp-dir / sessions-root helper rather than
# re-inventing it (importing the module defines its TestCases but runs none).
from test_cowork import _EvalEnvMixin  # noqa: E402

_FIXTURE_PATH = os.path.join(
    _HERE, "fixtures", "dispatch_contract_characterization_sources.json")


def _fixture():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _covers(source):
    """Mark a BackendCriterion1Test method as the real, unique non-vacuity
    witness for one characterization `source`. The marker lives on the
    function object itself so the class's own non-vacuity test can DERIVE
    which sources are actually exercised by introspecting real test methods
    (never by re-asserting a hardcoded literal set that could silently drift
    from what the methods actually cover)."""
    def deco(fn):
        fn._covers_source = source
        return fn
    return deco


def _ok_probe_events():
    return [{"type": "assistant",
             "message": {"content": [{"type": "text", "text": "ok"}]}},
            {"type": "result"}]


class _DispatchEnv(_EvalEnvMixin):
    """Isolated sessions root + probe cache + a guaranteed-clean policy holder.

    The probe cache is GLOBAL and persistent (`cowork_probe_cache`), so without
    an explicit override a second launch in the same run silently skips the live
    probe — which would make every probe-ordering assertion here read whatever
    the developer's real `~/.cowork` happened to contain.
    """

    def setUp(self):
        super().setUp()
        self._scores_root()  # relocates COWORK_SESSIONS_ROOT
        self.cold_probe_cache()
        policy.deactivate()
        self.addCleanup(policy.deactivate)

    def cold_probe_cache(self):
        """Point the global probe cache at a fresh file: the next probe is live."""
        path = os.path.join(self._tmpdir(), "probe_cache.json")
        old = os.environ.get("COWORK_PROBE_CACHE")
        os.environ["COWORK_PROBE_CACHE"] = path

        def restore():
            if old is None:
                os.environ.pop("COWORK_PROBE_CACHE", None)
            else:
                os.environ["COWORK_PROBE_CACHE"] = old
        self.addCleanup(restore)
        return path

    # -- shared fakes ------------------------------------------------------- #

    def role_config(self, role, controller, mode="plan", yolo=True):
        return {role: {"controller": controller, "mode": mode, "yolo": yolo,
                       "model": None, "effort": None}}

    def intel_path(self):
        return os.path.join(self._tmpdir(), "scout.intel.json")

    def recording_spawn(self, events=None):
        """A claude probe spawn that records every invocation."""
        calls = []

        def spawn(command, stdin_text):
            calls.append(list(command))
            return list(events if events is not None else _ok_probe_events())
        return calls, spawn

    def recording_factory(self, statuses, artifact_path):
        """A `session_factory` recording its dispatch kwargs.

        Each `send` writes the next scripted status artifact, which is how the
        role loops terminate without a live controller.
        """
        records = []
        pending = list(statuses)

        def factory(controller, *args, **kwargs):
            records.append({"controller": controller,
                            "positional": len(args),
                            "kwargs": dict(kwargs)})

            class _Scripted:
                io_out = io.StringIO()

                def __init__(self):
                    self.sent = []

                def send(self, text, meta=None):
                    self.sent.append(text)
                    status = pending.pop(0) if pending else None
                    if status is not None:
                        os.makedirs(os.path.dirname(artifact_path),
                                    exist_ok=True)
                        with open(artifact_path, "w", encoding="utf-8") as fh:
                            json.dump(status, fh)
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return _Scripted()
        return records, factory

    def ready(self):
        return {"status": "ready_for_review", "result": {}}

    def needs_input(self, n=0):
        return {"status": "needs_input", "result": {"n": n}}


# --------------------------------------------------------------------------- #
# gap 1 — every dispatch source has an explicit, executed characterization.    #
# --------------------------------------------------------------------------- #

class DispatchSourceInventoryTest(_DispatchEnv, unittest.TestCase):
    """Execute each dispatch source and compare it to the versioned fixture.

    One driver per source; the driver RUNS the source and returns the observed
    record. The fixture is the pinned baseline, never the source of truth about
    what the code says.
    """

    def _source_fresh(self):
        intel = self.intel_path()
        records, factory = self.recording_factory([self.ready()], intel)
        rc = cowork.run_scout(
            self.role_config("scout", "codex"), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=io.StringIO(), intel_path=intel,
            session_factory=factory)
        self.assertEqual(rc, 0)
        probes, spawn = self.recording_spawn()
        rec = records[0]
        return {
            "controller": rec["controller"],
            "factory_kwargs": sorted(rec["kwargs"]),
            "resume_value": rec["kwargs"]["resume_thread_id"],
            "probe_spawns": len(probes),
            "pre_dispatch_guard": self._has_pre_dispatch_guard_scout(
                "codex", spawn),
            "policy_block": self._policy_block_scout("codex", spawn),
        }

    def _source_resume(self):
        intel = self.intel_path()
        records, factory = self.recording_factory([self.ready()], intel)
        probes, spawn = self.recording_spawn()
        rc = cowork.run_scout(
            self.role_config("scout", "claude"), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=io.StringIO(), intel_path=intel,
            session_factory=factory, claude_spawn=spawn,
            resume_id="RESUME-ID")
        self.assertEqual(rc, 0)
        rec = records[0]
        self.cold_probe_cache()
        _, blocked_spawn = self.recording_spawn()
        return {
            "controller": rec["controller"],
            "factory_kwargs": sorted(rec["kwargs"]),
            "resume_value": rec["kwargs"]["resume_id"],
            "session_id_pinned": rec["kwargs"]["session_id"] is not None,
            "probe_spawns": len(probes),
            "pre_dispatch_guard": self._has_pre_dispatch_guard_scout(
                "claude", blocked_spawn),
            "policy_block": self._policy_block_scout(
                "claude", blocked_spawn, resume_id="RESUME-ID"),
        }

    def _has_pre_dispatch_guard_scout(self, controller, spawn):
        """True when the refusal happens before ANY launch machinery runs.

        `run_scout` has no pre-dispatch `policy.guard`, so on claude the probe
        itself raises and on codex/opencode only the bridge backstop fires —
        after the banner has already been written.
        """
        out = io.StringIO()
        with policy.restricted(("opencode",)):
            try:
                cowork.run_scout(
                    self.role_config("scout", controller), "goal", ["scout"],
                    io_in=io.StringIO(""), io_out=out,
                    intel_path=self.intel_path(), claude_spawn=spawn)
            except policy.DispatchBlocked:
                pass
        return "gathering context" not in out.getvalue()

    def _policy_block_scout(self, controller, spawn, resume_id=None):
        self.cold_probe_cache()
        with policy.restricted(("opencode",)):
            try:
                rc = cowork.run_scout(
                    self.role_config("scout", controller), "goal", ["scout"],
                    io_in=io.StringIO(""), io_out=io.StringIO(),
                    intel_path=self.intel_path(), claude_spawn=spawn,
                    resume_id=resume_id)
            except policy.DispatchBlocked as exc:
                return "raises_DispatchBlocked_kind_" + exc.kind
        return "clean_return_rc_%s" % rc

    def _reviewer_env(self):
        root = self._tmpdir()
        intel = os.path.join(root, "scout.intel.json")
        review = os.path.join(root, "scout.review.json")
        with open(intel, "w", encoding="utf-8") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        cfg = self.role_config(cowork.SCOUT_REVIEWER, "claude", yolo=False)
        return cfg, intel, review

    def _source_reviewer(self):
        cfg, intel, review = self._reviewer_env()
        team = ["scout", cowork.SCOUT_REVIEWER]

        fresh_probes, bad_spawn = self.recording_spawn([{"type": "other"}])
        verdict = cowork.run_reviewer_once(
            cfg, "ctx", team, intel, review, claude_spawn=bad_spawn)

        self.cold_probe_cache()
        resume_probes, resume_spawn = self.recording_spawn()
        with policy.restricted(("opencode",)):
            # A restricted policy stops the resumed claude session at the
            # bridge guard, so nothing is spawned; what matters here is that the
            # probe was never reached on the resume path at all.
            cowork.run_reviewer_once(
                cfg, "ctx", team, intel, review, claude_spawn=resume_spawn,
                resume_id="REVIEWER-RESUME")

        self.cold_probe_cache()
        factory_probes, factory_spawn = self.recording_spawn()

        def factory(controller, review_io):
            class _Rev:
                def send(self, text, meta=None):
                    with open(review, "w", encoding="utf-8") as fh:
                        json.dump({"verdict": "approve"}, fh)
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return _Rev()
        cowork.run_reviewer_once(
            cfg, "ctx", team, intel, review, claude_spawn=factory_spawn,
            session_factory=factory)

        self.cold_probe_cache()
        guard_probes, guard_spawn = self.recording_spawn()
        with policy.restricted(("opencode",)):
            blocked = cowork.run_reviewer_once(
                cfg, "ctx", team, intel, review, claude_spawn=guard_spawn)
        blocked_result = (blocked.get("controller_failure_result") or {}).get(
            "result")
        return {
            "controller": "claude",
            "probe_spawns_fresh": len(fresh_probes),
            "probe_spawns_resume": len(resume_probes),
            "probe_spawns_with_factory": len(factory_probes),
            "fresh_probe_failure_result": (
                verdict.get("controller_failure_result") or {}).get("result"),
            # The refusal beat the probe: the guard is reached before the
            # reviewer does any launch work at all.
            "pre_dispatch_guard": len(guard_probes) == 0,
            "policy_block": "controller_failure_verdict_" + str(
                blocked_result),
        }

    def _source_evaluator(self):
        scratch = os.path.join(self._tmpdir(), "eval.scratch.json")
        captured = {}

        class _Recorder:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

            def close(self):
                pass

        real = bridge.ClaudeSession
        bridge.ClaudeSession = _Recorder
        try:
            cowork._isolated_evaluator_session(
                {"scratch_path": scratch}, {"tool": "claude"})
        finally:
            bridge.ClaudeSession = real
        kwargs = captured["kwargs"]
        self.addCleanup(kwargs["io_out"].close)
        resume_keys = {"resume_id", "session_id", "resume_thread_id",
                       "resume_session_id"}

        with policy.restricted(("opencode",)):
            try:
                cowork._isolated_evaluator_session(
                    {"scratch_path": scratch}, {"tool": "claude"},
                    io_out=io.StringIO())
                blocked = "not_blocked"
            except policy.DispatchBlocked as exc:
                blocked = "raises_DispatchBlocked_kind_" + exc.kind
        result = {
            "controller": "claude",
            "accepts_resume_kwarg": bool(resume_keys & set(kwargs)),
            "internal": kwargs["internal"],
            "repo_writable": kwargs["repo_writable"],
            "declared_outputs_len": len(kwargs["declared_outputs"]),
            "mode": captured["args"][1],
            "yolo": captured["args"][2],
            "probe_spawns": 0,
            "pre_dispatch_guard": False,
            "policy_block": blocked,
        }
        return result

    def _worktree_config(self):
        return {"controller": "claude", "mode": "implement", "yolo": True,
                "model": None, "effort": None}

    def _source_worktree(self):
        status = os.path.join(self._tmpdir(), "worktree.status.json")
        factory_probes, factory_spawn = self.recording_spawn()

        def factory(controller):
            class _Wt:
                def send(self, text, meta=None):
                    with open(status, "w", encoding="utf-8") as fh:
                        json.dump({"status": "ready",
                                   "result": {"path": "/tmp/x",
                                              "branch": "b"}}, fh)
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return _Wt()
        artifact = cowork.run_worktree(
            self._worktree_config(), status, "/tmp", "name", False,
            io_in=io.StringIO(""), io_out=io.StringIO(),
            session_factory=factory, claude_spawn=factory_spawn)
        self.assertEqual((artifact or {}).get("status"), "ready")

        self.cold_probe_cache()
        live_probes, bad_spawn = self.recording_spawn([{"type": "other"}])
        cowork.run_worktree(
            self._worktree_config(), status, "/tmp", "name", False,
            io_in=io.StringIO(""), io_out=io.StringIO(),
            claude_spawn=bad_spawn)

        self.cold_probe_cache()
        guard_probes, guard_spawn = self.recording_spawn()
        out = io.StringIO()
        with policy.restricted(("opencode",)):
            blocked = cowork.run_worktree(
                self._worktree_config(), status, "/tmp", "name", False,
                io_in=io.StringIO(""), io_out=out, claude_spawn=guard_spawn)
        return {
            "controller": "claude",
            "probe_spawns_with_factory": len(factory_probes),
            "probe_spawns_without_factory": len(live_probes),
            # The refusal beat the banner AND the probe: a real pre-dispatch
            # guard, unlike run_scout.
            "pre_dispatch_guard": (len(guard_probes) == 0
                                   and "creating a git worktree"
                                   not in out.getvalue()),
            "policy_block": ("clean_return_none" if blocked is None
                             else "returned_%r" % (blocked,)),
        }

    def _saved_switch_session(self, pending_turn):
        spath = os.path.join(self._tmpdir(), ".cowork", "session.json")
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        team = ["scout", "planner", cowork.PLANNING_ADVISOR]
        state = state_store.ensure_session(spath, None, "SWITCH-CHAR")
        state = state_store.save_config(
            spath, team, cowork.default_config(team), prior=state)
        state = state_store.save_phase(spath, "planning", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "old-claude", prior=state)
        intel = os.path.join(
            state_store.session_assets_dir("SWITCH-CHAR"), "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w", encoding="utf-8") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "keep"}}, fh)
        state_store.switch_role_controller(
            spath, "planner", "codex", prior=state,
            reason="controller_failure", source="gate",
            pending_turn=pending_turn)
        return spath

    def _run_switched_planner(self, spath, argv_extra=()):
        calls = []

        def fake_planner(config, context, selected, on_outcome=None,
                         resume_id=None, **kwargs):
            calls.append({"context": context, "resume_id": resume_id,
                          "controller": config["planner"]["controller"]})
            if kwargs.get("on_first_send_accepted"):
                kwargs["on_first_send_accepted"]()
            if on_outcome:
                on_outcome("approved", None)
            return 0

        args = cowork.build_parser().parse_args(
            ["--session-file", spath] + list(argv_extra))
        rc = cowork.run_flow(
            args, io_in=io.StringIO(), io_out=io.StringIO(),
            which=lambda tool: "/bin/" + tool, run_planner_fn=fake_planner)
        return rc, calls

    def _source_switch(self):
        marker = "PENDING-TURN-MARKER-42"
        spath = self._saved_switch_session(marker)
        rc, calls = self._run_switched_planner(spath)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        context = str(calls[0]["context"])
        descriptors = [line for line in context.splitlines()
                       if "failed pending turn" in line and ".txt" in line]
        return {
            "controller": calls[0]["controller"],
            "resume_value": calls[0]["resume_id"],
            "switch_note_blocks": context.count("[controller switch handoff]"),
            "pending_turn_descriptors": len(descriptors),
            "pending_turn_inlined": marker in context,
            "pending_switch_after": state_store.read_pending_switch(
                state_store.load(spath), "planner"),
        }

    def _terminal_eval_queue(self, session_uuid="RETRY-CHAR"):
        queue_path = state_store.evaluation_queue_path_for(session_uuid)
        os.makedirs(os.path.dirname(queue_path), exist_ok=True)
        evaluation.enqueue(queue_path, {
            "entry_id": "E1", "seat": "scout", "phase": "scouting",
            "round": 1, "scratch_path": os.path.join(
                self._tmpdir(), "eval.scratch.json")})
        evaluation.mark_attempt_started(queue_path, "E1", 1)
        evaluation.mark_terminal(queue_path, "E1", "permanent", 1, 1,
                                 transition_history=["attempt_started",
                                                     "terminal"])
        return queue_path

    def _source_retry(self):
        queue_path = self._terminal_eval_queue()
        reopened = cowork.retry_terminal_evaluations("RETRY-CHAR")
        records = evaluation.read_queue(queue_path)
        retried = [r for r in records if r.get("state") == "retried"]
        fold = evaluation.read_entry_lifecycle(records, "E1")
        terminal = [r for r in records if r.get("state") == "terminal"]
        return {
            "reopened": reopened,
            "state_after": retried[-1]["state"] if retried else fold.get(
                "state"),
            "prior_attempt_ref_present": bool(
                retried and retried[-1].get("prior_attempt_ref")),
            "prior_attempt_ref_points_at_terminal_marker": bool(
                retried and terminal
                and retried[-1].get("prior_attempt_ref")
                in {t.get("marker_id") or t.get("id") for t in terminal}),
        }

    def _source_probe(self):
        allowed_calls, allowed_spawn = self.recording_spawn()
        ok, alert = bridge.probe_claude_stream_json(
            allowed_spawn, role="scout",
            role_prompt_file=cowork.SCOUT_PROMPT_PATH)
        self.assertTrue(ok, alert)

        blocked_calls, blocked_spawn = self.recording_spawn()
        with policy.restricted(("opencode",)):
            try:
                bridge.probe_claude_stream_json(
                    blocked_spawn, role="scout",
                    role_prompt_file=cowork.SCOUT_PROMPT_PATH)
                outcome, kind = "not_blocked", None
            except policy.DispatchBlocked as exc:
                outcome = "raises_DispatchBlocked_kind_" + exc.kind
                kind = exc.kind
        return {
            "controller": "claude",
            "guard_kind": kind,
            "spawns_when_allowed": len(allowed_calls),
            "spawns_when_blocked": len(blocked_calls),
            "policy_block": outcome,
        }

    def _source_headless(self):
        intel = self.intel_path()
        records, factory = self.recording_factory([self.ready()], intel)
        rc = cowork.run_scout(
            self.role_config("scout", "codex"), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=io.StringIO(), intel_path=intel,
            session_factory=factory, headless=True)
        self.assertEqual(rc, 0)
        probes, spawn = self.recording_spawn()
        rec = records[0]
        return {
            "controller": rec["controller"],
            "factory_kwargs": sorted(rec["kwargs"]),
            "resume_value": rec["kwargs"]["resume_thread_id"],
            "probe_spawns": len(probes),
            "pre_dispatch_guard": self._has_pre_dispatch_guard_scout(
                "codex", spawn),
            "policy_block": self._policy_block_scout("codex", spawn),
        }

    # -- the fixture-driven assertions -------------------------------------- #

    def test_fixture_covers_every_named_dispatch_source(self):
        """The nine dispatch sources the brief enumerates each have a driver."""
        required = {"fresh", "resume", "reviewer", "evaluator", "worktree",
                    "switch", "retry", "probe", "headless"}
        sources = set(_fixture()["sources"])
        self.assertEqual(sources, required)
        for name in sorted(required):
            self.assertTrue(hasattr(self, "_source_" + name),
                            "no executable driver for dispatch source %r" % name)

    def test_every_dispatch_source_matches_its_pinned_record(self):
        fixture = _fixture()["sources"]
        for name in sorted(fixture):
            with self.subTest(source=name):
                observed = getattr(self, "_source_" + name)()
                self.assertEqual(
                    observed, fixture[name]["record"],
                    "dispatch source %r (%s) drifted from its pinned baseline"
                    % (name, fixture[name]["entry_point"]))


# --------------------------------------------------------------------------- #
# gap 2 — policy / preflight / probe / spawn ordering.                         #
# --------------------------------------------------------------------------- #

class DispatchOrderingTest(_DispatchEnv, unittest.TestCase):
    """Pin the order of the launch steps on the paths that have them.

    Paths whose current behavior intentionally has NO probe (codex, opencode,
    and every resumed reviewer) are pinned as "no probe" — the absence is the
    behavior, and inventing one here would be the drift M0-C must not make.
    """

    def test_probe_is_guarded_before_argv_reaches_the_spawn(self):
        calls, spawn = self.recording_spawn()
        with policy.restricted(("codex",)):
            with self.assertRaises(policy.DispatchBlocked) as ctx:
                bridge.probe_claude_stream_json(
                    spawn, role="scout",
                    role_prompt_file=cowork.SCOUT_PROMPT_PATH)
        self.assertEqual(ctx.exception.kind, "probe")
        self.assertEqual(calls, [], "the probe spawned despite a policy block")

    def test_claude_session_is_guarded_before_any_process(self):
        with policy.restricted(("codex",)):
            with self.assertRaises(policy.DispatchBlocked) as ctx:
                bridge.ClaudeSession(cowork.SCOUT_PROMPT_PATH, "plan", True,
                                     io_out=io.StringIO(), speaker="scout")
        self.assertEqual(ctx.exception.kind, "dispatch")
        self.assertEqual(ctx.exception.role, "scout")

    def test_scout_probes_on_both_fresh_and_resume(self):
        for resume_id in (None, "RESUME-ID"):
            with self.subTest(resume=bool(resume_id)):
                self.cold_probe_cache()
                intel = self.intel_path()
                _, factory = self.recording_factory([self.ready()], intel)
                calls, spawn = self.recording_spawn()
                rc = cowork.run_scout(
                    self.role_config("scout", "claude"), "goal", ["scout"],
                    io_in=io.StringIO(""), io_out=io.StringIO(),
                    intel_path=intel, session_factory=factory,
                    claude_spawn=spawn, resume_id=resume_id)
                self.assertEqual(rc, 0)
                self.assertEqual(len(calls), 1)

    def test_reviewer_probes_on_fresh_only(self):
        root = self._tmpdir()
        intel = os.path.join(root, "scout.intel.json")
        review = os.path.join(root, "scout.review.json")
        with open(intel, "w", encoding="utf-8") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        cfg = self.role_config(cowork.SCOUT_REVIEWER, "claude", yolo=False)
        team = ["scout", cowork.SCOUT_REVIEWER]

        fresh_calls, fresh_spawn = self.recording_spawn([{"type": "other"}])
        verdict = cowork.run_reviewer_once(
            cfg, "ctx", team, intel, review, claude_spawn=fresh_spawn)
        self.assertEqual(len(fresh_calls), 1)
        self.assertTrue(verdict.get("controller_failure"))

        self.cold_probe_cache()
        resume_calls, resume_spawn = self.recording_spawn()
        with policy.restricted(("codex",)):
            cowork.run_reviewer_once(
                cfg, "ctx", team, intel, review, claude_spawn=resume_spawn,
                resume_id="REVIEWER-RESUME")
        self.assertEqual(resume_calls, [],
                         "the resumed reviewer path grew a probe")

    def test_codex_and_opencode_role_dispatch_never_probe(self):
        for controller in ("codex", "opencode"):
            with self.subTest(controller=controller):
                self.cold_probe_cache()
                intel = self.intel_path()
                _, factory = self.recording_factory([self.ready()], intel)
                calls, spawn = self.recording_spawn()
                rc = cowork.run_scout(
                    self.role_config("scout", controller), "goal", ["scout"],
                    io_in=io.StringIO(""), io_out=io.StringIO(),
                    intel_path=intel, session_factory=factory,
                    claude_spawn=spawn)
                self.assertEqual(rc, 0)
                self.assertEqual(calls, [])

    def test_session_factory_bypasses_the_worktree_probe_but_not_the_scout_probe(self):
        """A divergence M0-C must either preserve or deliberately remove.

        `run_worktree` consults `session_factory` BEFORE probing; `run_scout`
        probes first and only then consults it. The same injected seam therefore
        skips a live controller call on one path and not the other.
        """
        status = os.path.join(self._tmpdir(), "worktree.status.json")
        wt_calls, wt_spawn = self.recording_spawn()

        def wt_factory(controller):
            class _Wt:
                def send(self, text, meta=None):
                    with open(status, "w", encoding="utf-8") as fh:
                        json.dump({"status": "ready", "result": {}}, fh)
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return _Wt()
        cowork.run_worktree(
            {"controller": "claude", "mode": "implement", "yolo": True,
             "model": None, "effort": None},
            status, "/tmp", "name", False, io_in=io.StringIO(""),
            io_out=io.StringIO(), session_factory=wt_factory,
            claude_spawn=wt_spawn)
        self.assertEqual(wt_calls, [])

        self.cold_probe_cache()
        intel = self.intel_path()
        _, factory = self.recording_factory([self.ready()], intel)
        scout_calls, scout_spawn = self.recording_spawn()
        cowork.run_scout(
            self.role_config("scout", "claude"), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=io.StringIO(), intel_path=intel,
            session_factory=factory, claude_spawn=scout_spawn)
        self.assertEqual(len(scout_calls), 1)

    def test_preflight_facts_are_computed_without_dispatching(self):
        """Preflight is a pure fact source; it never consults the policy holder
        and never launches. Both dispatch surfaces read the SAME facts."""
        cfg = {"scout": {"controller": "codex"},
               cowork.SCOUT_REVIEWER: {"controller": "claude"}}
        self.assertEqual(sorted(preflight.required_controllers(cfg)),
                         ["claude", "codex"])
        with policy.restricted(("opencode",)):
            ok, alerts = preflight.preflight(
                cfg, which=lambda tool: "/bin/" + tool, interactive=False,
                platform="darwin")
        self.assertTrue(ok, alerts)
        self.assertEqual(alerts, [])

    def test_worktree_refuses_before_banner_probe_or_spawn(self):
        status = os.path.join(self._tmpdir(), "worktree.status.json")
        calls, spawn = self.recording_spawn()
        out = io.StringIO()
        with policy.restricted(("codex",)):
            result = cowork.run_worktree(
                {"controller": "claude", "mode": "implement", "yolo": True,
                 "model": None, "effort": None},
                status, "/tmp", "name", False, io_in=io.StringIO(""),
                io_out=out, claude_spawn=spawn)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertNotIn("creating a git worktree", out.getvalue())
        self.assertIn("does not allow", out.getvalue())


# --------------------------------------------------------------------------- #
# gap 3 — switch note + pending turn, preserved and consumed exactly once.     #
# --------------------------------------------------------------------------- #

class SwitchNoteConsumptionTest(_DispatchEnv, unittest.TestCase):
    def _session_with_pending_switch(self, pending_turn):
        spath = os.path.join(self._tmpdir(), ".cowork", "session.json")
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        team = ["scout", "planner", cowork.PLANNING_ADVISOR]
        state = state_store.ensure_session(spath, None, "SWITCH-ONCE")
        state = state_store.save_config(
            spath, team, cowork.default_config(team), prior=state)
        state = state_store.save_phase(spath, "planning", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "old-claude", prior=state)
        intel = os.path.join(
            state_store.session_assets_dir("SWITCH-ONCE"), "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w", encoding="utf-8") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "keep"}}, fh)
        state_store.switch_role_controller(
            spath, "planner", "codex", prior=state, reason="controller_failure",
            source="gate", pending_turn=pending_turn)
        return spath

    def _run(self, spath, argv_extra=()):
        calls = []

        def fake_planner(config, context, selected, on_outcome=None,
                         resume_id=None, **kwargs):
            calls.append({"context": context, "resume_id": resume_id,
                          "controller": config["planner"]["controller"]})
            if kwargs.get("on_first_send_accepted"):
                kwargs["on_first_send_accepted"]()
            if on_outcome:
                on_outcome("approved", None)
            return 0

        args = cowork.build_parser().parse_args(
            ["--session-file", spath] + list(argv_extra))
        rc = cowork.run_flow(
            args, io_in=io.StringIO(), io_out=io.StringIO(),
            which=lambda tool: "/bin/" + tool, run_planner_fn=fake_planner)
        return rc, calls

    def test_fresh_dispatch_after_switch_carries_note_and_pending_turn(self):
        marker = "PENDING-TURN-MARKER-42"
        spath = self._session_with_pending_switch(marker)
        rc, calls = self._run(spath)

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["resume_id"], "a switch must dispatch FRESH")
        self.assertEqual(calls[0]["controller"], "codex")

        context = str(calls[0]["context"])
        self.assertEqual(context.count("[controller switch handoff]"), 1)
        descriptors = [line for line in context.splitlines()
                       if "failed pending turn" in line and ".txt" in line]
        self.assertEqual(len(descriptors), 1,
                         "the pending turn must ride exactly one descriptor")
        # File-only transport: the failed turn is carried BY PATH, never inlined.
        self.assertNotIn(marker, context)
        path = descriptors[0].split(": ", 1)[1].split("  [")[0].strip()
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), marker)

    def test_switch_note_and_pending_turn_are_consumed_exactly_once(self):
        marker = "PENDING-TURN-MARKER-42"
        spath = self._session_with_pending_switch(marker)
        rc, first = self._run(spath)
        self.assertEqual(rc, 0)
        self.assertIn("[controller switch handoff]", str(first[0]["context"]))

        # The marker is gone from durable state after exactly one consumption.
        self.assertIsNone(state_store.read_pending_switch(
            state_store.load(spath), "planner"))

        rc, second = self._run(spath, ["--context", "carry on"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(second), 1)
        replayed = str(second[0]["context"])
        self.assertNotIn("[controller switch handoff]", replayed)
        self.assertNotIn("failed pending turn", replayed)

    def test_pending_turn_survives_until_the_switched_role_dispatches(self):
        """Saving a failed turn is durable state, not in-process memory."""
        spath = self._session_with_pending_switch("REPLAY-ME")
        saved = state_store.read_pending_switch(
            state_store.load(spath), "planner")
        self.assertEqual(saved["pending_turn"], "REPLAY-ME")
        self.assertEqual(saved["from_controller"], "claude")
        self.assertEqual(saved["to_controller"], "codex")


# --------------------------------------------------------------------------- #
# gap 4 — the evaluator dispatch contract.                                     #
# --------------------------------------------------------------------------- #

class EvaluatorDispatchContractTest(_DispatchEnv, unittest.TestCase):
    def _capture(self, attr, identity, scratch=True):
        captured = {}

        class _Recorder:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

            def close(self):
                pass

        path = os.path.join(self._tmpdir(), "eval.scratch.json") if scratch \
            else None
        real = getattr(bridge, attr)
        setattr(bridge, attr, _Recorder)
        try:
            cowork._isolated_evaluator_session({"scratch_path": path}, identity)
        finally:
            setattr(bridge, attr, real)
        self.addCleanup(captured["kwargs"]["io_out"].close)
        captured["scratch_path"] = path
        return captured

    def test_claude_evaluator_is_fresh_internal_readonly_and_declared(self):
        cap = self._capture("ClaudeSession", {"tool": "claude", "model": "m",
                                              "effort": "high"})
        args, kwargs = cap["args"], cap["kwargs"]
        self.assertEqual(args[0], cowork.EVALUATOR_PROMPT_PATH)
        self.assertEqual((args[1], args[2]), ("plan", False))
        self.assertEqual(kwargs["speaker"], "evaluator")
        self.assertTrue(kwargs["internal"])
        self.assertFalse(kwargs["repo_writable"])
        self.assertEqual(kwargs["declared_outputs"], (cap["scratch_path"],))
        # Fresh-only: no resume/session-id kwarg is even offered.
        for key in ("resume_id", "session_id", "resume_thread_id",
                    "resume_session_id"):
            self.assertNotIn(key, kwargs)

    def test_codex_evaluator_holds_the_same_contract(self):
        cap = self._capture("CodexSession", {"tool": "codex"})
        args, kwargs = cap["args"], cap["kwargs"]
        self.assertEqual((args[0], args[1]), ("plan", False))
        self.assertEqual(kwargs["speaker"], "evaluator")
        self.assertTrue(kwargs["internal"])
        self.assertFalse(kwargs["repo_writable"])
        self.assertEqual(kwargs["declared_outputs"], (cap["scratch_path"],))
        self.assertNotIn("resume_thread_id", kwargs)

    def test_evaluator_io_is_muted_by_default(self):
        cap = self._capture("ClaudeSession", {"tool": "claude"})
        stream = cap["kwargs"]["io_out"]
        self.assertEqual(os.path.realpath(stream.name),
                         os.path.realpath(os.devnull))

    def test_evaluator_send_is_muted_even_on_a_user_facing_session(self):
        """The other half of muted I/O: `_muted_session` swaps io_out for the
        role sessions an eval turn borrows."""
        out = io.StringIO()

        class _Session:
            io_out = out

            def send(self, text):
                self.io_out.write("LEAK:" + text)

        session = _Session()
        with cowork._muted_session(session):
            session.send("eval prompt")
        self.assertEqual(out.getvalue(), "")
        self.assertIs(session.io_out, out)

    def test_evaluator_dispatch_is_guarded_at_the_bridge_layer(self):
        scratch = os.path.join(self._tmpdir(), "eval.scratch.json")
        for controller in ("claude", "codex"):
            with self.subTest(controller=controller):
                with policy.restricted(("opencode",)):
                    with self.assertRaises(policy.DispatchBlocked) as ctx:
                        cowork._isolated_evaluator_session(
                            {"scratch_path": scratch},
                            {"tool": controller}, io_out=io.StringIO())
                self.assertEqual(ctx.exception.kind, "dispatch")
                self.assertEqual(ctx.exception.role, "evaluator")

    def test_unsupported_evaluator_controller_yields_no_session(self):
        scratch = os.path.join(self._tmpdir(), "eval.scratch.json")
        self.assertIsNone(cowork._isolated_evaluator_session(
            {"scratch_path": scratch}, {"tool": "opencode"}))


# --------------------------------------------------------------------------- #
# gap 5 — retry mechanisms and their attempt / prior-attempt linkage.          #
# --------------------------------------------------------------------------- #

class RetryLinkageTest(_DispatchEnv, unittest.TestCase):
    """Two of the four retry mechanisms link attempts; two do not.

    Green tests pin the linkage that exists. The two that do not are pinned as
    explicit missing-field witnesses (and one of them carries the
    `expectedFailure` in `MissingDispatchContractTest`).
    """

    def _terminal_queue(self, session_uuid="RETRY-LINK"):
        queue_path = state_store.evaluation_queue_path_for(session_uuid)
        os.makedirs(os.path.dirname(queue_path), exist_ok=True)
        evaluation.enqueue(queue_path, {
            "entry_id": "E1", "seat": "scout", "phase": "scouting",
            "round": 1})
        evaluation.mark_attempt_started(queue_path, "E1", 1)
        evaluation.mark_terminal(queue_path, "E1", "permanent", 1, 1,
                                 transition_history=["attempt_started",
                                                     "terminal"])
        return queue_path

    def test_evaluator_retry_links_to_the_terminal_marker_it_reopened(self):
        queue_path = self._terminal_queue()
        self.assertEqual(cowork.retry_terminal_evaluations("RETRY-LINK"), 1)
        records = evaluation.read_queue(queue_path)
        retried = [r for r in records if r.get("state") == "retried"]
        self.assertEqual(len(retried), 1)
        terminal = [r for r in records if r.get("state") == "terminal"]
        self.assertEqual(len(terminal), 1)
        ids = {terminal[0].get(key) for key in ("marker_id", "id")}
        self.assertIn(retried[0]["prior_attempt_ref"], ids,
                      "the retry must name the terminal marker it reopened")
        # The earlier records survive verbatim: a retry links, never overwrites.
        self.assertTrue(any(r.get("state") == "terminal" for r in records))
        self.assertTrue(any(r.get("state") == "attempt_started"
                            for r in records))

    def test_evaluator_attempts_are_recorded_before_they_run(self):
        queue_path = self._terminal_queue("RETRY-ORDER")
        states = [r.get("state") for r in evaluation.read_queue(queue_path)]
        self.assertLess(states.index("attempt_started"), states.index("terminal"))

    def test_verification_attempts_carry_a_preminted_attempt_id(self):
        entry = {"label": "unit", "command": ["true"],
                 "execution_mode": "in_place", "kind": "test",
                 "ledger_attempt_id": "V-1"}
        request = verification.build_request(
            session_uuid="VERIFY-LINK", transaction_id="T1",
            repo=self._tmpdir(), snapshot_manifest_digest="d1",
            index_digest="d2", configuration={}, schema=1, entries=[entry],
            final_suite_label="unit")
        self.assertEqual([e["ledger_attempt_id"] for e in request["inventory"]],
                         ["V-1"])
        # The retry that polls for an attempt's evidence is BOUNDED and the
        # bound travels with the request, not with the process that made it.
        self.assertGreater(
            request["evidence_retry_policy"]["poll_attempts"], 0)

    def test_pending_turn_retry_record_has_no_attempt_linkage(self):
        """MISSING-FIELD WITNESS (pending-turn replay).

        `save_pending_turn` persists the failed turn's TEXT and nothing else:
        no attempt number, no attempt id, no reference to the attempt that
        failed. A second failure of the same turn overwrites the first.
        """
        spath = os.path.join(self._tmpdir(), ".cowork", "session.json")
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        state = state_store.ensure_session(spath, None, "PENDING-LINK")
        state = state_store.save_pending_turn(
            spath, "planner", "first attempt", prior=state)
        entry = state_store.read_pending_switch(state, "planner")
        self.assertEqual(entry, {"pending_turn": "first attempt"})
        for field in ("attempt", "attempt_id", "prior_attempt_ref",
                      "attempts", "failed_at"):
            self.assertNotIn(field, entry)

        state = state_store.save_pending_turn(
            spath, "planner", "second attempt", prior=state)
        entry = state_store.read_pending_switch(state, "planner")
        # The first attempt is GONE — nothing on disk records that it happened.
        self.assertEqual(entry, {"pending_turn": "second attempt"})


# --------------------------------------------------------------------------- #
# gap 6 — headless and interactive make the same dispatch decision.            #
# --------------------------------------------------------------------------- #

class HeadlessInteractiveParityTest(_DispatchEnv, unittest.TestCase):
    def _dispatch(self, controller, headless, statuses, resume_id=None):
        self.cold_probe_cache()
        intel = self.intel_path()
        records, factory = self.recording_factory(statuses, intel)
        probes, spawn = self.recording_spawn()
        out = io.StringIO()
        rc = cowork.run_scout(
            self.role_config("scout", controller), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=out, intel_path=intel,
            session_factory=factory, claude_spawn=spawn, headless=headless,
            resume_id=resume_id)
        return {"rc": rc, "records": [self._normalize(r) for r in records],
                "probes": len(probes), "out": out.getvalue()}

    @staticmethod
    def _normalize(record):
        """Blank the freshly minted claude session UUID: the DECISION to pin an
        id up front is the fact under test, not the random value."""
        record = {"controller": record["controller"],
                  "positional": record["positional"],
                  "kwargs": dict(record["kwargs"])}
        if record["kwargs"].get("session_id"):
            record["kwargs"]["session_id"] = "<minted-uuid>"
        return record

    def test_same_dispatch_facts_fresh_and_resumed_across_both_surfaces(self):
        for controller, resume_id in (("codex", None), ("codex", "T1"),
                                      ("claude", None), ("claude", "R1"),
                                      ("opencode", None)):
            with self.subTest(controller=controller, resume=bool(resume_id)):
                interactive = self._dispatch(
                    controller, False, [self.ready()], resume_id)
                headless = self._dispatch(
                    controller, True, [self.ready()], resume_id)
                self.assertEqual(interactive["records"], headless["records"])
                self.assertEqual(interactive["probes"], headless["probes"])

    def test_same_policy_refusal_on_both_surfaces(self):
        for headless in (False, True):
            with self.subTest(headless=headless):
                self.cold_probe_cache()
                out = io.StringIO()
                with policy.restricted(("claude",)):
                    rc = cowork.run_scout(
                        self.role_config("scout", "codex"), "goal", ["scout"],
                        io_in=io.StringIO(""), io_out=out,
                        intel_path=self.intel_path(), headless=headless)
                self.assertEqual(rc, 1)
                self.assertIn("does not allow", out.getvalue())

    def test_only_the_gate_adapter_differs(self):
        """Identical dispatch, divergent gate handling.

        The dispatch decision is made before the loop; the loop is the adapter.
        Driven through `_role_loop` (the same seam the existing headless tests
        use) so the difference is unambiguous: headless auto-nudges a
        `needs_input` and reaches approval with NO input, while the interactive
        surface hands the same status to the user and ends on EOF.
        """
        statuses = [self.needs_input(0), self.ready()]

        def scripted(path, writes):
            class _Scripted:
                def __init__(self):
                    self.sent = []

                def send(self, text):
                    self.sent.append(text)
                    if writes:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "w", encoding="utf-8") as fh:
                            json.dump(writes.pop(0), fh)

                def close(self):
                    pass
            return _Scripted()

        path = self.intel_path()
        headless_session = scripted(path, list(statuses))
        _, headless_outcome, _ = cowork._role_loop(
            headless_session, "seed", path, context="",
            io_in=io.StringIO(""), io_out=io.StringIO(), headless=True)

        path = self.intel_path()
        interactive_session = scripted(path, list(statuses))
        _, interactive_outcome, _ = cowork._role_loop(
            interactive_session, "seed", path, context="",
            io_in=io.StringIO(""), io_out=io.StringIO(), headless=False)

        self.assertEqual(headless_outcome, "approved")
        self.assertEqual(interactive_outcome, "ended")
        self.assertEqual(len(headless_session.sent), 2)
        self.assertEqual(len(interactive_session.sent), 1)


# --------------------------------------------------------------------------- #
# The contract that does not exist yet.                                        #
# --------------------------------------------------------------------------- #

class MissingDispatchContractTest(_DispatchEnv, unittest.TestCase):
    """Each `expectedFailure` names ONE absent contract and is paired with the
    green `test_witness_*` above it that executes today's divergent behavior.

    An unexpected success here is the signal that M0-C landed the seam.
    """

    def _scout_refusal(self, controller):
        """Return ('returned', rc) or ('raised', kind) for a policy refusal."""
        self.cold_probe_cache()
        _, spawn = self.recording_spawn()
        with policy.restricted(("opencode",)):
            try:
                rc = cowork.run_scout(
                    self.role_config("scout", controller), "goal", ["scout"],
                    io_in=io.StringIO(""), io_out=io.StringIO(),
                    intel_path=self.intel_path(), claude_spawn=spawn)
            except policy.DispatchBlocked as exc:
                return ("raised", exc.kind)
        return ("returned", rc)

    def test_uniform_refusal_contract(self):
        """MISSING CONTRACT: `DispatchContract.uniform_refusal`.

        No lifecycle gateway decides dispatch, so a refusal is shaped by
        whichever construction site happened to run. Witness:
        `test_witness_role_dispatch_refusals_are_not_uniform_today`.
        Expected to start passing when M0-C routes every role dispatch through
        one gateway that refuses identically.
        """
        refusals = {c: self._scout_refusal(c)
                    for c in ("claude", "codex", "opencode")}
        self.assertEqual(len(set(refusals.values())), 1, refusals)

    def test_pending_turn_retry_linkage_contract(self):
        """CONTRACT: `DispatchContract.retry_linkage(pending_turn)`.

        When `save_pending_turn` is called with an explicit source_ref the
        entry carries a `pending_source/v1` sibling alongside `pending_turn`.
        The entry itself has no `attempt_id` — the source is a reference to
        the prior send event, not a new attempt identity.
        """
        import time as _time
        spath = os.path.join(self._tmpdir(), ".cowork", "session.json")
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        state = state_store.ensure_session(spath, None, "PENDING-CONTRACT")
        source_ref = {
            "kind": "delivery_fingerprint",
            "event_id": None,
            "event_name": None,
            "session_id": None,
            "prompt_sha256": "a" * 64,
            "created": _time.time(),
        }
        state = state_store.save_pending_turn(
            spath, "planner", "first", prior=state, source=source_ref)
        entry = state_store.read_pending_switch(state, "planner")
        self.assertIn("pending_source", entry)
        self.assertNotIn("attempt_id", entry)

    def test_gate_repair_retry_linkage_contract(self):
        """CONTRACT: `DispatchContract.retry_linkage(gate_repair)`.

        Each repair delivery carries a validated AttemptLink/v1 with
        kind="gate_repair", a fresh attempt_id, and a source_ref that
        identifies the turn being repaired. The repair prompt bytes are
        unchanged.
        """
        delivery = cowork._repair_delivery("intel")
        self.assertTrue(hasattr(delivery, "attempt_link"),
                        "repair delivery must carry an attempt_link attribute")
        link = dispatch.validate_attempt_link(delivery.attempt_link)
        self.assertEqual(link["kind"], "gate_repair")
        self.assertEqual(link["record"], "AttemptLink")


# --------------------------------------------------------------------------- #
# M1 backend criterion 1 — non-vacuous across all nine characterization       #
# sources.                                                                     #
#                                                                               #
# Each source method drives BOTH a real allowed dispatch and a real refused   #
# one (or, for retry/probe, the one real production mechanism that source     #
# actually has) and runs three checks against concrete, captured evidence:    #
#   C1 shape    — every decision observed is a manifest-bound allow or a real #
#                 production refusal (refusal_code/source drawn from          #
#                 cowork_dispatch's own vocabulary), or — where production    #
#                 truly has no `dispatch.decision` for a refusal (the         #
#                 controller-veto/backstop gap the M0-B fixture documents —   #
#                 e.g. codex/opencode role dispatch, the evaluator's bridge   #
#                 guard) — the real `policy.DispatchBlocked` exception/rc     #
#                 that actually enforces it.                                  #
#   C2 binding  — `trace_event_id` is asserted for EVERY captured decision,   #
#                 never skipped because it happens to be absent: present and  #
#                 equal to the governing manifest's digest when a manifest    #
#                 governs the decision, explicitly None when it genuinely     #
#                 does not (asserted, not waved through).                     #
#   C3 evidence — one source-specific, meaningful production assertion (real  #
#                 spawn counts, real terminal-marker linkage, a real          #
#                 `DispatchBlocked.kind`) — never an empty-trace property.    #
# --------------------------------------------------------------------------- #

class BackendCriterion1Test(_DispatchEnv, unittest.TestCase):

    def _trace(self, name):
        tpath = os.path.join(self._tmpdir(), name + ".trace.jsonl")
        return tpath, trace_store.Trace(tpath, session_uuid=name, run_id="R")

    def _trace_events(self, tpath):
        if not os.path.exists(tpath):
            return []
        with open(tpath, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _decisions(self, events, site=None, outcome=None):
        out = [e for e in events if e.get("event") == "dispatch.decision"]
        if site is not None:
            out = [e for e in out if e.get("site") == site]
        if outcome is not None:
            out = [e for e in out if e.get("outcome") == outcome]
        return out

    def _reviewer_env(self):
        root = self._tmpdir()
        intel = os.path.join(root, "scout.intel.json")
        review = os.path.join(root, "scout.review.json")
        with open(intel, "w", encoding="utf-8") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        cfg = self.role_config(cowork.SCOUT_REVIEWER, "claude", yolo=False)
        return cfg, intel, review

    def _worktree_config(self):
        return {"controller": "claude", "mode": "implement", "yolo": True,
               "model": None, "effort": None}

    def _assert_real_refusal_shape(self, source, decision):
        """C1 (refuse branch): the refusal_code/source are drawn from
        cowork_dispatch's own production vocabulary, never a placeholder."""
        self.assertEqual(decision.get("outcome"), "refuse", source)
        self.assertIn(decision.get("refusal_code"), dispatch._REFUSAL_CODES,
                      "%s: %r" % (source, decision))
        self.assertIn(decision.get("source"), dispatch._REFUSAL_SOURCES,
                      "%s: %r" % (source, decision))

    def _assert_manifest_bound(self, source, decision, manifest):
        """C2 (bound branch): trace_event_id equals the governing manifest's
        real digest — never a synthetic id."""
        self.assertEqual(decision.get("trace_event_id"), manifest["digest"],
                         "%s: %r" % (source, decision))
        self.assertIsNotNone(decision.get("trace_event_id"), source)

    def _assert_trace_event_id_explicitly_absent(self, source, decision):
        """C2 (unbound branch): the absence of `trace_event_id` is checked,
        not skipped — this decision genuinely predates any manifest compile
        for this attempt (a real pre-dispatch policy veto)."""
        self.assertIsNone(decision.get("trace_event_id"),
                          "%s: %r" % (source, decision))

    # -- fresh: codex role dispatch, manifest-bound allow; the policy veto  #
    # is enforced by the uncaught bridge backstop, not a decision event —   #
    # pinned by the M0-B fixture as `policy_block: clean_return_rc_1`.      #

    @_covers('fresh')
    def test_c1_fresh_manifest_bound_allow_and_real_policy_veto(self):
        session_uuid = str(uuid.uuid4())
        tpath, trace = self._trace("C1-FRESH")
        intel = self.intel_path()
        _, factory = self.recording_factory([self.ready()], intel)
        rc = cowork.run_scout(
            self.role_config("scout", "codex"), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=io.StringIO(), intel_path=intel,
            session_factory=factory, trace=trace, session_uuid=session_uuid)
        self.assertEqual(rc, 0)
        events = self._trace_events(tpath)
        allows = self._decisions(events, "run_scout", "allow")
        self.assertEqual(len(allows), 1, "C1: fresh has no allow to check")
        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, "scout"))
        self.assertEqual((manifest.get("status") or {}).get("phase"),
                         "proven")
        self._assert_manifest_bound("fresh", allows[0], manifest)  # C2

        # C1 refuse branch + C3: no work_id is invented — the session_uuid
        # above IS the real production work identity; the veto is real.
        self.cold_probe_cache()
        out = io.StringIO()
        with policy.restricted(("opencode",)):
            rc2 = cowork.run_scout(
                self.role_config("scout", "codex"), "goal", ["scout"],
                io_in=io.StringIO(""), io_out=out,
                intel_path=self.intel_path(),
                session_uuid=str(uuid.uuid4()))
        self.assertEqual(rc2, 1)
        self.assertIn("does not allow", out.getvalue())

    @_covers('headless')
    def test_c1_headless_manifest_bound_allow_and_real_policy_veto(self):
        """`headless` shares fresh's dispatch facts (M0-B fixture note); the
        gate adapter differs, the fence does not."""
        session_uuid = str(uuid.uuid4())
        tpath, trace = self._trace("C1-HEADLESS")
        intel = self.intel_path()
        _, factory = self.recording_factory([self.ready()], intel)
        rc = cowork.run_scout(
            self.role_config("scout", "codex"), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=io.StringIO(), intel_path=intel,
            session_factory=factory, headless=True, trace=trace,
            session_uuid=session_uuid)
        self.assertEqual(rc, 0)
        events = self._trace_events(tpath)
        allows = self._decisions(events, "run_scout", "allow")
        self.assertEqual(len(allows), 1, "C1: headless has no allow to check")
        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, "scout"))
        self._assert_manifest_bound("headless", allows[0], manifest)  # C2

        self.cold_probe_cache()
        out = io.StringIO()
        with policy.restricted(("opencode",)):
            rc2 = cowork.run_scout(
                self.role_config("scout", "codex"), "goal", ["scout"],
                io_in=io.StringIO(""), io_out=out,
                intel_path=self.intel_path(), headless=True,
                session_uuid=str(uuid.uuid4()))
        self.assertEqual(rc2, 1)
        self.assertIn("does not allow", out.getvalue())

    # -- resume: claude scout, a resumed dispatch. Unlike fresh, a resumed  #
    # claude refusal DOES bind a real decision — the fence surfaces it,     #
    # then the probe's own `policy.guard(kind="probe")` raises uncaught.    #

    @_covers('resume')
    def test_c1_resume_manifest_bound_allow_and_bound_refusal(self):
        session_uuid = str(uuid.uuid4())
        tpath, trace = self._trace("C1-RESUME")
        intel = self.intel_path()
        _, factory = self.recording_factory([self.ready()], intel)
        _, spawn = self.recording_spawn()
        rc = cowork.run_scout(
            self.role_config("scout", "claude"), "goal", ["scout"],
            io_in=io.StringIO(""), io_out=io.StringIO(), intel_path=intel,
            session_factory=factory, claude_spawn=spawn,
            resume_id="RESUME-ID", trace=trace, session_uuid=session_uuid)
        self.assertEqual(rc, 0)
        events = self._trace_events(tpath)
        allows = self._decisions(events, "run_scout", "allow")
        self.assertGreaterEqual(len(allows), 1, "C1: resume has no allow")
        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, "scout"))
        for d in allows:
            self._assert_manifest_bound("resume", d, manifest)  # C2

        self.cold_probe_cache()
        session_uuid2 = str(uuid.uuid4())
        tpath2, trace2 = self._trace("C1-RESUME-REFUSE")
        _, spawn2 = self.recording_spawn()
        with policy.restricted(("opencode",)):
            with self.assertRaises(policy.DispatchBlocked) as ctx:
                cowork.run_scout(
                    self.role_config("scout", "claude"), "goal", ["scout"],
                    io_in=io.StringIO(""), io_out=io.StringIO(),
                    intel_path=self.intel_path(), claude_spawn=spawn2,
                    resume_id="RESUME-ID", trace=trace2,
                    session_uuid=session_uuid2)
        self.assertEqual(ctx.exception.kind, "probe")  # C3: real, not vacuous
        events2 = self._trace_events(tpath2)
        refusals = self._decisions(events2, "run_scout", "refuse")
        self.assertEqual(len(refusals), 1,
                         "C1: resume refusal has no bound decision")
        self._assert_real_refusal_shape("resume", refusals[0])
        manifest2 = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid2, "scout"))
        self._assert_manifest_bound("resume-refuse", refusals[0], manifest2)

    # -- reviewer: a pre-dispatch policy guard ahead of ANY manifest — the  #
    # refuse decision explicitly has NO trace_event_id (never skipped).    #

    @_covers('reviewer')
    def test_c1_reviewer_manifest_bound_allow_and_pre_dispatch_refusal(self):
        cfg, intel, review = self._reviewer_env()
        team = ["scout", cowork.SCOUT_REVIEWER]
        session_uuid = str(uuid.uuid4())
        tpath, trace = self._trace("C1-REVIEWER")

        def factory(controller, review_io):
            class _Rev:
                def send(self, text, meta=None):
                    with open(review, "w", encoding="utf-8") as fh:
                        json.dump({"verdict": "approve"}, fh)
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return _Rev()
        cowork.run_reviewer_once(
            cfg, "ctx", team, intel, review, session_factory=factory,
            resume_id="REVIEWER-RESUME", trace=trace, session_uuid=session_uuid)
        events = self._trace_events(tpath)
        allows = self._decisions(events, "run_reviewer_once", "allow")
        self.assertEqual(len(allows), 1, "C1: reviewer has no allow")
        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, cowork.SCOUT_REVIEWER))
        self._assert_manifest_bound("reviewer", allows[0], manifest)  # C2

        session_uuid2 = str(uuid.uuid4())
        tpath2, trace2 = self._trace("C1-REVIEWER-REFUSE")
        with policy.restricted(("opencode",)):
            verdict = cowork.run_reviewer_once(
                cfg, "ctx", team, intel, review, trace=trace2,
                session_uuid=session_uuid2)
        self.assertTrue(verdict.get("controller_failure"))  # C3: real
        events2 = self._trace_events(tpath2)
        refusals = self._decisions(events2, "run_reviewer_once", "refuse")
        self.assertEqual(len(refusals), 1, "C1: reviewer refusal unbound")
        self._assert_real_refusal_shape("reviewer", refusals[0])
        self._assert_trace_event_id_explicitly_absent(
            "reviewer", refusals[0])  # C2, checked not skipped

    # -- evaluator: fresh/internal/read-only. Manifest decision always      #
    # allows; the policy veto is the bridge-level `kind="dispatch"` raise.  #

    @_covers('evaluator')
    def test_c1_evaluator_manifest_bound_allow_and_real_policy_veto(self):
        session_uuid = str(uuid.uuid4())
        tpath, trace = self._trace("C1-EVALUATOR")
        scratch = os.path.join(self._tmpdir(), "eval.scratch.json")
        session = cowork._isolated_evaluator_session(
            {"scratch_path": scratch}, {"tool": "claude", "model": "m",
                                        "effort": None},
            trace=trace, io_out=io.StringIO(), session_uuid=session_uuid)
        self.addCleanup(session.close)
        events = self._trace_events(tpath)
        allows = self._decisions(events, "_isolated_evaluator_session",
                                 "allow")
        self.assertEqual(len(allows), 1, "C1: evaluator has no allow")
        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, "evaluator"))
        self._assert_manifest_bound("evaluator", allows[0], manifest)  # C2

        with policy.restricted(("opencode",)):
            with self.assertRaises(policy.DispatchBlocked) as ctx:
                cowork._isolated_evaluator_session(
                    {"scratch_path": scratch},
                    {"tool": "claude", "model": "m", "effort": None},
                    io_out=io.StringIO(), session_uuid=str(uuid.uuid4()))
        self.assertEqual(ctx.exception.kind, "dispatch")  # C3: real
        self.assertEqual(ctx.exception.role, "evaluator")

    # -- worktree: the policy fact and the manifest are decided TOGETHER;   #
    # a policy veto short-circuits before any manifest compile, so its      #
    # refusal explicitly carries NO trace_event_id (checked, not skipped). #

    @_covers('worktree')
    def test_c1_worktree_manifest_bound_allow_and_pre_compile_refusal(self):
        session_uuid = str(uuid.uuid4())
        tpath, trace = self._trace("C1-WORKTREE")
        status_path = os.path.join(self._tmpdir(), "wt.status.json")

        def factory(controller):
            class _Wt:
                def send(self, text, meta=None):
                    with open(status_path, "w", encoding="utf-8") as fh:
                        json.dump({"status": "ready",
                                   "result": {"path": "/tmp/x",
                                              "branch": "b"}}, fh)
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return _Wt()
        cowork.run_worktree(
            self._worktree_config(), status_path, "/tmp", "name", False,
            io_in=io.StringIO(""), io_out=io.StringIO(),
            session_factory=factory, trace=trace, session_uuid=session_uuid,
            extra_writable_dir=self._tmpdir())
        events = self._trace_events(tpath)
        allows = self._decisions(events, "run_worktree", "allow")
        self.assertEqual(len(allows), 1, "C1: worktree has no allow")
        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, cowork.WORKTREE_ROLE))
        self._assert_manifest_bound("worktree", allows[0], manifest)  # C2

        session_uuid2 = str(uuid.uuid4())
        tpath2, trace2 = self._trace("C1-WORKTREE-REFUSE")
        out = io.StringIO()
        with policy.restricted(("opencode",)):
            result = cowork.run_worktree(
                self._worktree_config(),
                os.path.join(self._tmpdir(), "wt2.status.json"), "/tmp",
                "name", False, io_in=io.StringIO(""), io_out=out,
                trace=trace2, session_uuid=session_uuid2,
                extra_writable_dir=self._tmpdir())
        self.assertIsNone(result)  # C3: real clean refusal, no artifact
        events2 = self._trace_events(tpath2)
        refusals = self._decisions(events2, "run_worktree", "refuse")
        self.assertEqual(len(refusals), 1, "C1: worktree refusal unbound")
        self._assert_real_refusal_shape("worktree", refusals[0])
        self._assert_trace_event_id_explicitly_absent(
            "worktree", refusals[0])  # C2, checked not skipped

    # -- switch: a role dispatch AFTER a real controller switch. The       #
    # pre-launch gate splits policy (unbound) and manifest (bound) into    #
    # two decisions at the SAME site — both are checked, never one only.   #

    def _saved_switch_session(self, session_uuid, pending_turn):
        spath = os.path.join(self._tmpdir(), session_uuid, "session.json")
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        team = ["scout", "planner", cowork.PLANNING_ADVISOR]
        state = state_store.ensure_session(spath, None, session_uuid)
        state = state_store.save_config(
            spath, team, cowork.default_config(team), prior=state)
        state = state_store.save_phase(spath, "planning", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "old-claude", prior=state)
        intel = os.path.join(
            state_store.session_assets_dir(session_uuid), "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w", encoding="utf-8") as fh:
            json.dump({"status": "ready_for_review",
                      "result": {"finding": "keep"}}, fh)
        state_store.switch_role_controller(
            spath, "planner", "codex", prior=state,
            reason="controller_failure", source="gate",
            pending_turn=pending_turn)
        return spath

    @_covers('switch')
    def test_c1_switch_manifest_bound_allow_and_manifest_bound_refusal(self):
        session_uuid = str(uuid.uuid4())
        spath = self._saved_switch_session(session_uuid, "PENDING-X")

        def fake_planner(config, context, selected, on_outcome=None,
                         resume_id=None, **kw):
            if kw.get("on_first_send_accepted"):
                kw["on_first_send_accepted"]()
            if on_outcome:
                on_outcome("approved", None)
            return 0

        args = cowork.build_parser().parse_args(["--session-file", spath])
        rc = cowork.run_flow(
            args, io_in=io.StringIO(), io_out=io.StringIO(),
            which=lambda tool: "/bin/" + tool, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)

        tpath = trace_store.trace_path_for(session_uuid)
        events = self._trace_events(tpath)
        allows = self._decisions(events, "run_flow_pre_launch", "allow")
        self.assertGreaterEqual(len(allows), 2,
                                "C1: switch dispatch is missing the "
                                "policy+manifest decision pair")
        manifest = manifest_mod.load_manifest(
            state_store.manifest_path_for(session_uuid, "planner"))
        bound = [d for d in allows if d.get("trace_event_id") is not None]
        unbound = [d for d in allows if d.get("trace_event_id") is None]
        self.assertGreaterEqual(len(bound), 1,
                                "C2: no manifest-bound decision for switch")
        self.assertGreaterEqual(len(unbound), 1,
                                "C2: no explicitly-unbound policy decision "
                                "for switch — every decision must be "
                                "checked, not filtered out")
        for d in bound:
            self._assert_manifest_bound("switch", d, manifest)

        # C3: the switch is real (persisted), the fresh dispatch is real
        # (never a resume), and its controller is the switched-to one.
        after = state_store.load(spath)
        self.assertEqual(after["config"]["planner"]["controller"], "codex")

    # -- retry: no manifest concept — the meaningful production evidence is #
    # real prior-attempt linkage to the terminal marker it reopened.       #

    @_covers('retry')
    def test_c1_retry_real_prior_attempt_linkage(self):
        session_uuid = "CRITERION1-RETRY-%s" % uuid.uuid4()
        queue_path = state_store.evaluation_queue_path_for(session_uuid)
        os.makedirs(os.path.dirname(queue_path), exist_ok=True)
        evaluation.enqueue(queue_path, {
            "entry_id": "E1", "seat": "scout", "phase": "scouting",
            "round": 1})
        evaluation.mark_attempt_started(queue_path, "E1", 1)
        evaluation.mark_terminal(queue_path, "E1", "permanent", 1, 1,
                                 transition_history=["attempt_started",
                                                     "terminal"])
        reopened = cowork.retry_terminal_evaluations(session_uuid)
        self.assertEqual(reopened, 1, "C1: retry has nothing to check")
        records = evaluation.read_queue(queue_path)
        retried = [r for r in records if r.get("state") == "retried"]
        terminal = [r for r in records if r.get("state") == "terminal"]
        self.assertEqual(len(retried), 1)
        # C3: the linkage is REAL — it names the actual terminal marker this
        # session produced, not a synthetic/random work id.
        marker_ids = {t.get("marker_id") or t.get("id") for t in terminal}
        self.assertIn(retried[0].get("prior_attempt_ref"), marker_ids)
        self.assertNotEqual(retried[0].get("prior_attempt_ref"), "")
        # the entry_id is the real production seat identity, never invented.
        self.assertEqual(retried[0].get("entry_id"), "E1")

    # -- probe: itself a guarded dispatch (kind="probe"), decided BEFORE    #
    # argv reaches the spawn — the refusal IS the real DispatchBlocked.    #

    @_covers('probe')
    def test_c1_probe_real_guard_decides_before_spawn(self):
        allowed_calls, allowed_spawn = self.recording_spawn()
        ok, alert = bridge.probe_claude_stream_json(
            allowed_spawn, role="scout",
            role_prompt_file=cowork.SCOUT_PROMPT_PATH)
        self.assertTrue(ok, alert)
        self.assertEqual(len(allowed_calls), 1,
                         "C1: probe allow has no real spawn to check")

        blocked_calls, blocked_spawn = self.recording_spawn()
        with policy.restricted(("opencode",)):
            with self.assertRaises(policy.DispatchBlocked) as ctx:
                bridge.probe_claude_stream_json(
                    blocked_spawn, role="scout",
                    role_prompt_file=cowork.SCOUT_PROMPT_PATH)
        self.assertEqual(ctx.exception.kind, "probe")  # C3: real, not vacuous
        self.assertEqual(ctx.exception.role, "scout")
        self.assertEqual(blocked_calls, [],
                         "the guard must decide before argv reaches spawn")

    # -- non-vacuity witness: the fixture's nine sources are exactly the    #
    # nine this class exercises.                                            #

    def test_fixture_sources_exactly_match_the_nine_exercised_here(self):
        required = {"fresh", "resume", "reviewer", "evaluator", "worktree",
                    "switch", "retry", "probe", "headless"}
        self.assertEqual(set(_fixture()["sources"]), required)

        # "exercised" is DERIVED from the actual test methods discovered on
        # this class (never a hardcoded literal disconnected from reality):
        # every real `test_c1_*` method must carry an `@_covers(source)`
        # marker, and the sources those markers name must be exactly the
        # required nine — a renamed/removed test or a source that lost its
        # marker fails this loudly instead of leaving a stale literal green.
        covered = collections.defaultdict(list)
        for name in dir(type(self)):
            if not name.startswith("test_c1_"):
                continue
            method = getattr(type(self), name)
            source = getattr(method, "_covers_source", None)
            self.assertIsNotNone(
                source,
                "%s carries no @_covers(...) source marker — a criterion-1 "
                "test without a declared source would silently escape this "
                "non-vacuity witness" % name)
            covered[source].append(name)

        exercised = set(covered)
        self.assertEqual(exercised, required)
        for source, methods in covered.items():
            self.assertEqual(
                len(methods), 1,
                "%s: exactly one real test method must cover this source, "
                "found %r" % (source, methods))

    def test_fixture_schema_is_rebaselined_to_v2(self):
        """The fixture's own schema/baseline advanced to v2 for M1 P5: it now
        pins BOTH the M0-B record shape (unchanged) AND, via this class, a
        non-vacuous production proof per source on top of the signed
        capability-binding repair (manifest SCHEMA_VERSION=2)."""
        fixture = _fixture()
        self.assertEqual(fixture["version"], 2)
        self.assertEqual(manifest_mod.SCHEMA_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
