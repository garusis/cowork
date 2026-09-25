#!/usr/bin/env python3
"""Typed Git worktree-inventory failures (cowork-internal #81).

A timeout, nonzero exit, or unavailable Git while cowork proves worktree
isolation must become a durable, typed, fail-closed outcome: never an
uncaught traceback, never a provider launch, and never a pinned session id
for a conversation that was never started. Fakes only; no provider CLI and no
slow Git. Run through the offline barrier:

    python3 scripts/cowork_offline_tests.py test_git_scope_failure
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_policy as policy  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402


def _completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout,
                                       stderr="")


def _git_error(kind="timeout", stage="worktree_list", returncode=None):
    return bridge.GitWorktreeScopeError(kind, stage, returncode)


class _SessionsRootTest(unittest.TestCase):
    """Isolated sessions root, no active controller policy, cwd restored."""

    def setUp(self):
        policy.deactivate()
        self.addCleanup(policy.deactivate)
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        env = mock.patch.dict(os.environ, {"COWORK_SESSIONS_ROOT": root})
        env.start()
        self.addCleanup(env.stop)
        cwd = os.getcwd()
        self.addCleanup(lambda: os.chdir(cwd))

    def _dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def _trace(self):
        path = os.path.join(self._dir(), "trace.jsonl")
        return trace_store.Trace(path, session_uuid="GIT81", run_id="R"), path

    @staticmethod
    def _events(path, name):
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        return [e for e in events if e.get("event") == name]

    def _nested_guard(self):
        prior = bridge.set_nested_guard_active(True)
        self.addCleanup(bridge.set_nested_guard_active, prior)

    def _no_popen(self):
        popen = mock.patch.object(
            bridge.subprocess, "Popen",
            side_effect=AssertionError("a provider process was spawned"))
        started = popen.start()
        self.addCleanup(popen.stop)
        return started


def _recording_saver():
    """An on_session saver exposing `unpin`, like run_flow's role_saver."""
    saved, unpinned = [], []

    def on_session(controller, sid):
        saved.append((controller, sid))

    on_session.unpin = lambda controller, sid: unpinned.append(
        (controller, sid))
    on_session.saved = saved
    on_session.unpinned = unpinned
    return on_session


# --------------------------------------------------------------------------- #
# T1 / T1b: classification in _git_worktree_scope.                             #
# --------------------------------------------------------------------------- #

class GitScopeClassificationTest(unittest.TestCase):
    def _scope(self, side_effect):
        with mock.patch.object(bridge.subprocess, "run",
                               side_effect=side_effect) as run:
            try:
                bridge._git_worktree_scope("/repo/main")
            except bridge.GitWorktreeScopeError as exc:
                return exc, run
        self.fail("_git_worktree_scope did not fail closed")

    def _assert(self, exc, kind, stage, returncode=None, cause=None):
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual((exc.kind, exc.stage, exc.returncode),
                         (kind, stage, returncode))
        reason = ("git_toplevel_unavailable" if stage == "toplevel"
                  else "git_worktree_inventory_unavailable")
        self.assertEqual(str(exc), reason)
        self.assertEqual(exc.fact(), {"kind": kind, "stage": stage,
                                      "returncode": returncode,
                                      "reason": reason})
        if cause is None:
            self.assertIsNone(exc.__cause__)
        else:
            self.assertIsInstance(exc.__cause__, cause)

    def test_toplevel_failures_are_typed(self):
        cases = [
            (subprocess.TimeoutExpired(["git"], 10), "timeout", None,
             subprocess.TimeoutExpired),
            (_completed(128), "nonzero_exit", 128, None),
            (FileNotFoundError("git"), "unavailable", None,
             FileNotFoundError),
            (_completed(0, "  \n"), "nonzero_exit", 0, None),
        ]
        for effect, kind, rc, cause in cases:
            with self.subTest(kind=kind, rc=rc):
                exc, run = self._scope([effect])
                self._assert(exc, kind, "toplevel", rc, cause)
                self.assertEqual(run.call_count, 1)

    def test_worktree_list_failures_are_typed(self):
        cases = [
            (subprocess.TimeoutExpired(["git"], 10), "timeout", None,
             subprocess.TimeoutExpired),
            (_completed(128), "nonzero_exit", 128, None),
            (OSError("exec failed"), "unavailable", None, OSError),
        ]
        for effect, kind, rc, cause in cases:
            with self.subTest(kind=kind):
                exc, run = self._scope([_completed(0, "/repo/main\n"), effect])
                self._assert(exc, kind, "worktree_list", rc, cause)
                self.assertEqual(run.call_count, 2)

    def test_timeout_and_argv_are_unchanged(self):
        exc, run = self._scope([_completed(0, "/repo/main\n"),
                                subprocess.TimeoutExpired(["git"], 10)])
        first, second = run.call_args_list
        self.assertEqual(first.args[0][-2:], ["rev-parse", "--show-toplevel"])
        self.assertEqual(second.args[0][-3:],
                         ["worktree", "list", "--porcelain"])
        self.assertEqual(first.kwargs["timeout"], 10)
        self.assertEqual(second.kwargs["timeout"], 10)

    def test_success_path_is_unchanged(self):
        with mock.patch.object(
                bridge.subprocess, "run",
                side_effect=[_completed(0, "/repo/main\n"),
                             _completed(0, "worktree /repo/main\n\n"
                                        "worktree /repo/main/.worktrees/o\n")]):
            active, siblings = bridge._git_worktree_scope("/repo/main")
        self.assertEqual(active, "/repo/main")
        self.assertEqual(siblings, ("/repo/main/.worktrees/o",))


# --------------------------------------------------------------------------- #
# T2: the codex auth reference is released when the Git scope fails.           #
# --------------------------------------------------------------------------- #

class GuardRuntimeProfileCleanupTest(_SessionsRootTest):
    def test_codex_profile_reference_released_on_git_failure(self):
        if sys.platform.startswith("linux"):
            self.skipTest("_guard_runtime refuses earlier on linux by design")

        class Trace:
            session_uuid = "GIT81-T2"

            def event(self, *a, **k):
                pass

        assets = state_store.session_assets_dir("GIT81-T2")
        os.makedirs(assets, exist_ok=True)
        profile = {"protected_paths": ()}
        with mock.patch.object(bridge.controller_profiles,
                               "reference_codex_auth", return_value=profile), \
                mock.patch.object(bridge, "_git_worktree_scope",
                                  side_effect=_git_error()), \
                mock.patch.object(bridge.controller_profiles,
                                  "cleanup_claude_session_reference") as clean:
            with self.assertRaises(bridge.GitWorktreeScopeError):
                bridge._guard_runtime(Trace(), "builder", assets, None, None,
                                      False, controller="codex")
        clean.assert_called_once_with(profile)


class SessionConstructorsFailBeforeSpawnTest(_SessionsRootTest):
    """The typed failure is raised by the runtime guard inside the session
    constructors strictly before any provider process exists."""

    def test_claude_and_codex_constructors_raise_before_popen(self):
        self._nested_guard()
        popen = self._no_popen()
        with mock.patch.object(bridge, "_guard_runtime",
                               side_effect=_git_error("nonzero_exit",
                                                      "toplevel", 128)):
            with self.assertRaises(bridge.GitWorktreeScopeError):
                bridge.ClaudeSession(cowork.BUILDER_PROMPT_PATH, "implement",
                                     True, io_out=io.StringIO(),
                                     speaker="builder")
            with self.assertRaises(bridge.GitWorktreeScopeError):
                bridge.CodexSession("implement", True, io_out=io.StringIO(),
                                    speaker="builder")
        popen.assert_not_called()


# --------------------------------------------------------------------------- #
# T8: compare-and-clear of a pinned role session.                              #
# --------------------------------------------------------------------------- #

class ClearRoleSessionTest(_SessionsRootTest):
    def _spath(self):
        spath = os.path.join(self._dir(), ".cowork", "session.json")
        state = state_store.ensure_session(spath, None, "GIT81-T8")
        state_store.save_role_session(spath, "builder", "claude", "sid-1",
                                      prior=state)
        return spath

    def test_mismatch_leaves_state_and_file_untouched(self):
        spath = self._spath()
        with open(spath, "rb") as fh:
            before = fh.read()
        for controller, sid in (("claude", "other"), ("codex", "sid-1")):
            with self.subTest(controller=controller, sid=sid):
                with mock.patch.object(state_store, "save") as save:
                    state = state_store.clear_role_session(
                        spath, "builder", controller, sid)
                save.assert_not_called()
                self.assertEqual(state_store.get_role_session(
                    state, "builder", "claude"), "sid-1")
        with open(spath, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_match_removes_and_persists(self):
        spath = self._spath()
        state = state_store.clear_role_session(
            spath, "builder", "claude", "sid-1",
            prior=state_store.load(spath))
        self.assertNotIn("builder", state["sessions"])
        saved = state_store.load(spath)
        self.assertNotIn("builder", saved.get("sessions") or {})
        self.assertIsNone(
            state_store.get_role_session(saved, "builder", "claude"))


# --------------------------------------------------------------------------- #
# T3: lead launches (scout, planner, builder).                                 #
# --------------------------------------------------------------------------- #

class LeadLaunchGitFailureTest(_SessionsRootTest):
    ROLES = ("scout", "planner", "builder")

    def _config(self, role, controller):
        return {role: {"controller": controller, "model": None,
                       "effort": None, "yolo": True, "mode": "implement"}}

    def _run(self, role, controller, session_factory=None, resume_id=None,
             on_session=None):
        suid = str(uuid.uuid4())
        out = io.StringIO()
        spawned = []
        kwargs = dict(io_out=out, session_uuid=suid, resume_id=resume_id,
                      on_session=on_session, session_factory=session_factory,
                      claude_spawn=lambda *a, **k: spawned.append(a) or [],
                      reviewer_runner=lambda *a, **k: {"verdict": "approve"})
        if role == "scout":
            intel = os.path.join(state_store.session_assets_dir(suid),
                                 "scout.intel.json")
            os.makedirs(os.path.dirname(intel), exist_ok=True)
            kwargs["intel_path"] = intel
        fn = {"scout": cowork.run_scout, "planner": cowork.run_planner,
              "builder": cowork.run_builder}[role]
        rc = fn(self._config(role, controller), "goal", [role], **kwargs)
        work_id = cowork._role_work_id(suid, role, None)
        return rc, out.getvalue(), state_store.current_phase_state(
            suid, work_id), spawned

    def _assert_rejected(self, rc, text, current, reason, kind, stage):
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", text)
        self.assertIn("git worktree isolation could not be verified", text)
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "rejected_preflight")
        self.assertEqual(current["evidence"]["reason"], reason)
        self.assertEqual(current["evidence"]["git_failure"]["kind"], kind)
        self.assertEqual(current["evidence"]["git_failure"]["stage"], stage)

    def test_claude_probe_git_failure_is_a_durable_probe_rejection(self):
        for role in self.ROLES:
            with self.subTest(role=role):
                constructed = []
                with mock.patch.object(
                        bridge, "probe_claude_stream_json",
                        side_effect=_git_error("timeout", "worktree_list")):
                    rc, text, current, _ = self._run(
                        role, "claude",
                        session_factory=lambda *a, **k: constructed.append(a))
                self._assert_rejected(rc, text, current, "probe_failed",
                                      "timeout", "worktree_list")
                self.assertEqual(constructed, [])

    def test_fresh_claude_start_failure_unpins_the_never_started_id(self):
        for role in self.ROLES:
            with self.subTest(role=role):
                pinned = []

                def factory(controller, session_id=None, **_k):
                    pinned.append(session_id)
                    raise _git_error("nonzero_exit", "toplevel", 128)
                saver = _recording_saver()
                with mock.patch.object(bridge, "probe_claude_stream_json",
                                       return_value=(True, None)):
                    rc, text, current, _ = self._run(
                        role, "claude", session_factory=factory,
                        on_session=saver)
                self._assert_rejected(rc, text, current, "start_failed",
                                      "nonzero_exit", "toplevel")
                self.assertEqual(current["evidence"]["error_type"],
                                 "GitWorktreeScopeError")
                self.assertEqual(saver.saved, [("claude", pinned[0])])
                self.assertEqual(saver.unpinned, [("claude", pinned[0])])

    def test_resumed_claude_start_failure_keeps_the_saved_id(self):
        saver = _recording_saver()

        def factory(*_a, **_k):
            raise _git_error("unavailable", "toplevel")
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               return_value=(True, None)):
            rc, text, current, _ = self._run(
                "builder", "claude", resume_id="live-conversation",
                on_session=saver, session_factory=factory)
        self._assert_rejected(rc, text, current, "start_failed",
                              "unavailable", "toplevel")
        self.assertEqual(saver.unpinned, [])

    def test_codex_and_opencode_start_failures_are_contained(self):
        def factory(*_a, **_k):
            raise _git_error("timeout", "toplevel")
        for controller in ("codex", "opencode"):
            for role in self.ROLES:
                with self.subTest(controller=controller, role=role):
                    saver = _recording_saver()
                    rc, text, current, spawned = self._run(
                        role, controller, session_factory=factory,
                        on_session=saver)
                    self._assert_rejected(rc, text, current, "start_failed",
                                          "timeout", "toplevel")
                    self.assertEqual(spawned, [])
                    self.assertEqual(saver.saved, [])
                    self.assertEqual(saver.unpinned, [])

    def test_non_git_start_failure_is_unchanged(self):
        saver = _recording_saver()

        def factory(*_a, **_k):
            raise RuntimeError("boom")
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               return_value=(True, None)):
            rc, text, current, _ = self._run(
                "planner", "claude", session_factory=factory,
                on_session=saver)
        self.assertEqual(rc, 1)
        self.assertIn("failed to start planner controller: RuntimeError",
                      text)
        self.assertEqual(current["evidence"]["reason"], "start_failed")
        self.assertEqual(current["evidence"]["error_type"], "RuntimeError")
        self.assertNotIn("git_failure", current["evidence"])
        self.assertEqual(len(saver.saved), 1)
        self.assertEqual(saver.unpinned, [])


# --------------------------------------------------------------------------- #
# T4 / T4b: reviewer launches and the reviewer auto-retry.                     #
# --------------------------------------------------------------------------- #

class ReviewerGitFailureTest(_SessionsRootTest):
    def _paths(self):
        d = self._dir()
        intel = os.path.join(d, "scout.intel.json")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"success_criteria": ["x"]}}, fh)
        return intel, os.path.join(d, "review.json")

    def _config(self, controller):
        return {cowork.SCOUT_REVIEWER: {"controller": controller,
                                        "model": None, "effort": None,
                                        "yolo": True, "mode": "implement"}}

    def _review(self, controller, trace, **kw):
        intel, review = self._paths()
        return cowork.run_reviewer_once(
            self._config(controller), "goal",
            ["scout", cowork.SCOUT_REVIEWER], intel, review, trace=trace,
            **kw)

    def _assert_failure(self, verdict, path, result, kind, stage):
        self.assertTrue(cowork._is_review_failure(verdict))
        self.assertTrue(verdict["controller_failure"])
        self.assertNotEqual(verdict.get("verdict"), "approve")
        fact = verdict["controller_failure_result"]["git_failure"]
        self.assertEqual((fact["kind"], fact["stage"]), (kind, stage))
        self.assertIn("git worktree isolation could not be verified",
                      verdict["controller_failure_alert"])
        ends = self._events(path, "review.run.end")
        self.assertEqual(ends[-1]["result"], result)
        self.assertEqual(ends[-1]["git_failure"]["kind"], kind)
        self.assertEqual(ends[-1]["git_failure"]["stage"], stage)

    def test_fresh_claude_probe_failure(self):
        trace, path = self._trace()
        popen = self._no_popen()
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               side_effect=_git_error("timeout",
                                                      "worktree_list")):
            verdict = self._review("claude", trace)
        self._assert_failure(verdict, path, "probe_failed", "timeout",
                             "worktree_list")
        popen.assert_not_called()

    def test_fresh_claude_construction_unpins_and_resume_does_not(self):
        self._nested_guard()
        popen = self._no_popen()
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               return_value=(True, None)), \
                mock.patch.object(bridge, "_guard_runtime",
                                  side_effect=_git_error("nonzero_exit",
                                                         "worktree_list",
                                                         128)):
            trace, path = self._trace()
            fresh = _recording_saver()
            verdict = self._review("claude", trace, on_session=fresh)
            self._assert_failure(verdict, path, "git_scope_failed",
                                 "nonzero_exit", "worktree_list")
            self.assertEqual(len(fresh.saved), 1)
            self.assertEqual(fresh.unpinned, fresh.saved)

            trace, path = self._trace()
            resumed = _recording_saver()
            verdict = self._review("claude", trace, on_session=resumed,
                                   resume_id="live-reviewer")
            self._assert_failure(verdict, path, "git_scope_failed",
                                 "nonzero_exit", "worktree_list")
            self.assertEqual(resumed.unpinned, [])
        popen.assert_not_called()

    def test_codex_construction_failure(self):
        self._nested_guard()
        popen = self._no_popen()
        trace, path = self._trace()
        with mock.patch.object(bridge, "_guard_runtime",
                               side_effect=_git_error("unavailable",
                                                      "toplevel")):
            verdict = self._review("codex", trace)
        self._assert_failure(verdict, path, "git_scope_failed",
                             "unavailable", "toplevel")
        popen.assert_not_called()

    def test_opencode_construction_failure(self):
        trace, path = self._trace()

        def factory(*_a, **_k):
            raise _git_error("timeout", "toplevel")
        verdict = self._review("opencode", trace, session_factory=factory)
        self._assert_failure(verdict, path, "git_scope_failed", "timeout",
                             "toplevel")

    def test_reviewer_retry_starts_fresh_and_never_approves(self):
        """T4b: the REVIEW_FAIL_CAP auto-retry must not resume the id pinned
        by the attempt whose launch failed the Git check."""
        self._nested_guard()
        intel, review = self._paths()
        resume_ids = []
        persisted = _recording_saver()

        def runner(config, context, selected, artifact_path, review_path,
                   **kw):
            resume_ids.append(kw.get("resume_id"))
            return cowork.run_reviewer_once(
                config, context, selected, artifact_path, review_path, **kw)

        review_fn = cowork.make_review_fn(
            self._config("claude"), "goal", ["scout", cowork.SCOUT_REVIEWER],
            review, reviewer_runner=runner,
            on_reviewer_session=persisted)

        class Scout:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                with open(intel, "w") as fh:
                    json.dump({"status": "ready_for_review", "result": {}},
                              fh)

            def close(self):
                pass

        outcomes = []
        guard = mock.MagicMock(side_effect=_git_error("timeout",
                                                      "worktree_list"))
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               return_value=(True, None)), \
                mock.patch.object(bridge, "_guard_runtime", guard):
            rc = cowork._scout_loop(
                Scout(), "seed", intel, context="", io_out=io.StringIO(),
                review_fn=review_fn,
                on_outcome=lambda o, p=None: outcomes.append((o, p)))
        self.assertEqual(rc, 0)
        self.assertEqual(len(resume_ids), cowork.REVIEW_FAIL_CAP)
        self.assertGreaterEqual(len(resume_ids), 2)
        self.assertEqual(resume_ids, [None] * len(resume_ids))
        pinned = [sid for _c, sid in persisted.saved]
        self.assertEqual(len(pinned), len(resume_ids))
        self.assertEqual(persisted.unpinned, persisted.saved)
        self.assertGreaterEqual(guard.call_count, 1)
        self.assertEqual(outcomes[-1][0], "ended")
        self.assertEqual(outcomes[-1][1]["kind"], "reviewer_unavailable")


# --------------------------------------------------------------------------- #
# T5: the worktree role.                                                       #
# --------------------------------------------------------------------------- #

class WorktreeGitFailureTest(_SessionsRootTest):
    def _run(self, controller, trace, **kw):
        d = self._dir()
        out = io.StringIO()
        artifact = cowork.run_worktree(
            {"controller": controller, "model": None, "effort": None,
             "yolo": True, "mode": "implement"},
            os.path.join(d, "worktree.status.json"), d, "feat", True,
            io_out=out, trace=trace, **kw)
        return artifact, out.getvalue()

    def _assert(self, artifact, text, path, result, kind, stage):
        self.assertIsNone(artifact)
        self.assertNotIn("Traceback", text)
        self.assertIn("git worktree isolation could not be verified", text)
        end = self._events(path, "worktree.run.end")[-1]
        self.assertEqual(end["result"], result)
        self.assertEqual((end["git_failure"]["kind"],
                          end["git_failure"]["stage"]), (kind, stage))

    def test_claude_probe_failure(self):
        popen = self._no_popen()
        trace, path = self._trace()
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               side_effect=_git_error("timeout", "toplevel")):
            artifact, text = self._run("claude", trace)
        self._assert(artifact, text, path, "probe_failed", "timeout",
                     "toplevel")
        popen.assert_not_called()

    def test_claude_and_codex_construction_failures(self):
        self._nested_guard()
        popen = self._no_popen()
        for controller in ("claude", "codex"):
            with self.subTest(controller=controller):
                trace, path = self._trace()
                with mock.patch.object(bridge, "probe_claude_stream_json",
                                       return_value=(True, None)), \
                        mock.patch.object(
                            bridge, "_guard_runtime",
                            side_effect=_git_error("nonzero_exit",
                                                   "worktree_list", 1)):
                    artifact, text = self._run(controller, trace)
                self._assert(artifact, text, path, "git_scope_failed",
                             "nonzero_exit", "worktree_list")
        popen.assert_not_called()

    def test_opencode_construction_failure(self):
        trace, path = self._trace()

        def factory(*_a, **_k):
            raise _git_error("unavailable", "worktree_list")
        artifact, text = self._run("opencode", trace, session_factory=factory)
        self._assert(artifact, text, path, "git_scope_failed", "unavailable",
                     "worktree_list")

    def test_run_flow_reports_worktree_failed_without_chdir(self):
        repo = os.path.realpath(self._dir())
        for argv in (["init", "-q", repo],
                     ["-C", repo, "config", "user.email", "t@t"],
                     ["-C", repo, "config", "user.name", "t"],
                     ["-C", repo, "config", "commit.gpgsign", "false"]):
            subprocess.run(["git"] + argv, check=True, capture_output=True)
        with open(os.path.join(repo, "f.txt"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "-C", repo, "add", "."], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "init"],
                       check=True, capture_output=True)
        os.chdir(repo)
        before = os.path.realpath(os.getcwd())
        calls = []

        def factory(*_a, **_k):
            raise _git_error("timeout", "worktree_list")

        def run_worktree_fn(*a, **k):
            calls.append(1)
            return cowork.run_worktree(*a, session_factory=factory, **k)

        def unscripted(*_a, **_k):
            raise AssertionError("no lead may run after a failed worktree")
        box = {}
        out = io.StringIO()
        rc = cowork.run_flow(
            cowork.build_parser().parse_args(
                ["--worktree", "--wt-controller", "codex", "--team",
                 "scout,scout-reviewer", "--context", "x", "--no-session"]),
            io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=unscripted, run_planner_fn=unscripted,
            run_builder_fn=unscripted, run_worktree_fn=run_worktree_fn,
            result_box=box)
        self.assertEqual((rc, box.get("reason")), (1, "worktree_failed"))
        self.assertEqual(calls, [1])
        self.assertEqual(os.path.realpath(os.getcwd()), before)
        self.assertNotIn("Traceback", out.getvalue())


# --------------------------------------------------------------------------- #
# T6 / T7: run_flow recovery and the controller-switch probe.                  #
# --------------------------------------------------------------------------- #

class RunFlowGitFailureTest(_SessionsRootTest):
    TEAM = ["scout", "scout-reviewer", "planner", "planning-advisor",
            "builder", "build-reviewer"]

    def setUp(self):
        super().setUp()
        runtime = mock.patch.object(cowork.preflight,
                                    "check_governed_runtime",
                                    return_value=(True, []))
        runtime.start()
        self.addCleanup(runtime.stop)

    def _session(self, suid, phase, controllers, team):
        spath = os.path.join(self._dir(), ".cowork", "session.json")
        state = state_store.ensure_session(spath, None, suid)
        cfg = cowork.default_config(team)
        for role, controller in controllers.items():
            cfg[role] = dict(cfg[role], controller=controller)
        state = state_store.save_config(spath, team, cfg, prior=state)
        state_store.save_phase(spath, phase, prior=state)
        return spath

    @staticmethod
    def _sha(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def _trace_events(self, suid, name):
        return self._events(trace_store.trace_path_for(suid), name)

    def test_builder_git_failure_keeps_plan_and_rerun_starts_fresh(self):
        suid = "GIT81-T6"
        spath = self._session(suid, "building", {"builder": "claude"},
                              self.TEAM)
        assets = state_store.session_assets_dir(suid)
        os.makedirs(assets, exist_ok=True)
        plan = state_store.planner_plan_json_path_for(assets, suid)
        plan_md = state_store.planner_plan_md_path_for(assets, suid)
        plan_review = state_store.planner_review_path_for(assets, suid)
        with open(plan, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"step": "S1"}}, fh)
        with open(plan_md, "w") as fh:
            fh.write("# PLAN\n")
        with open(plan_review, "w") as fh:
            json.dump({"verdict": "approve"}, fh)
        shas = {p: self._sha(p) for p in (plan, plan_md, plan_review)}
        planner_calls = []

        def run_planner_fn(*a, **k):
            planner_calls.append(1)
            return 0

        def unscripted(*_a, **_k):
            raise AssertionError("scouting must not rerun")
        pinned = []

        def factory(controller, session_id=None, **_k):
            pinned.append(session_id)
            raise _git_error("timeout", "worktree_list")

        def failing_builder(config, context, selected, **kw):
            return cowork.run_builder(config, context, selected,
                                      session_factory=factory, **kw)

        box = {}
        out = io.StringIO()
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               return_value=(True, None)):
            rc = cowork.run_flow(
                cowork.build_parser().parse_args(["--session-file", spath]),
                io_out=out, which=lambda c: "/bin/" + c,
                run_scout_fn=unscripted, run_planner_fn=run_planner_fn,
                run_builder_fn=failing_builder, result_box=box)
        self.assertEqual(rc, 1)
        self.assertEqual(box.get("reason"), "controller_startup_failed")
        self.assertNotIn("Traceback", out.getvalue())
        self.assertEqual(len(pinned), 1)
        self.assertTrue(pinned[0])
        saved = state_store.load(spath)
        self.assertEqual(state_store.get_phase(saved), "building")
        self.assertNotEqual(
            ((saved.get("sessions") or {}).get("builder") or {}).get("id"),
            pinned[0])
        self.assertIsNone(
            state_store.get_role_session(saved, "builder", "claude"))
        self.assertEqual({p: self._sha(p) for p in shas}, shas)
        unpinned = self._trace_events(suid, "role.session_unpinned")
        self.assertEqual([(e["role"], e["session_id"]) for e in unpinned],
                         [("builder", pinned[0])])

        resumes = []

        def healthy_builder(config, context, selected, on_outcome=None,
                            resume_id=None, **kw):
            resumes.append(resume_id)
            if on_outcome:
                on_outcome("approved", None)
            return 0
        rc = cowork.run_flow(
            cowork.build_parser().parse_args(["--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=unscripted, run_planner_fn=run_planner_fn,
            run_builder_fn=healthy_builder)
        self.assertEqual(rc, 0)
        self.assertEqual(resumes, [None])
        self.assertEqual(planner_calls, [])

    def test_switch_probe_git_failure_rejects_without_writes(self):
        suid = "GIT81-T7"
        team = ["scout", "scout-reviewer", "planner", "planning-advisor"]
        spath = self._session(suid, "planning", {"planner": "codex"}, team)
        before = state_store.load(spath)

        def unscripted(*_a, **_k):
            raise AssertionError("no role may launch after a failed switch")
        out = io.StringIO()
        with mock.patch.object(bridge, "probe_claude_stream_json",
                               side_effect=_git_error("timeout",
                                                      "worktree_list")):
            rc = cowork.run_flow(
                cowork.build_parser().parse_args(
                    ["--session-file", spath,
                     "--switch-controller", "planner=claude"]),
                io_out=out, which=lambda c: "/bin/" + c,
                run_scout_fn=unscripted, run_planner_fn=unscripted)
        self.assertEqual(rc, 1)
        self.assertIn("cannot switch planner to claude", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())
        saved = state_store.load(spath)
        self.assertEqual(saved["config"], before["config"])
        self.assertEqual(saved.get("sessions"), before.get("sessions"))
        failed = self._trace_events(suid, "controller.switch.probe_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["git_failure"]["kind"], "timeout")
        self.assertEqual(failed[0]["git_failure"]["stage"], "worktree_list")


if __name__ == "__main__":
    unittest.main()
