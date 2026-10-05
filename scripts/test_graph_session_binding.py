#!/usr/bin/env python3
"""Tests for graph-bound child sessions: `--graph-vertex` binding at start
and the vertex fence at every child entry point (new session, plain resume,
take-over, resume-trigger), plus the unchanged behavior of unbound sessions.

Fake lead runners replace the controllers and a fake provider session
replaces the resume-trigger send. Every repository is a throwaway git
repository (with linked worktrees as vertex roots) in a temp directory and
COWORK_SESSIONS_ROOT is pinned to a temp directory. Ids are fresh UUIDs;
inputs are neutral and synthetic.

Run: python3 scripts/cowork_offline_tests.py test_graph_session_binding
"""

import contextlib
import datetime
import hashlib
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_capacity as capacity_contracts  # noqa: E402
import cowork_capacity_scheduler as capacity_scheduler  # noqa: E402
import cowork_graph_cli  # noqa: E402
import cowork_graph_store as store  # noqa: E402
import cowork_owner  # noqa: E402
import cowork_state as state_store  # noqa: E402

STANDARD = "standard"
IGNORE_RULE = ".cowork/\n"
FABRICATED_PID_START = "2020-01-01T00:00:00Z"
TURN_TEXT = "neutral paused turn"
PAUSE_ISSUED_AT = "2025-12-31T00:00:00Z"
PAUSE_NOT_BEFORE = "2025-12-31T01:00:00Z"
WAKE_NOW = "2026-01-02T00:00:00Z"
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Graph Fixture",
    "GIT_AUTHOR_EMAIL": "graph-fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Graph Fixture",
    "GIT_COMMITTER_EMAIL": "graph-fixture@example.invalid",
}


# --------------------------------------------------------------------------- #
# Module helpers (local; nothing is imported from another test module).       #
# --------------------------------------------------------------------------- #


def _uuid():
    return str(uuid.uuid4())


def _git(args, cwd):
    env = dict(os.environ)
    env.update(GIT_ENV)
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError("git %s failed: %s"
                             % (" ".join(args), proc.stderr.strip()))
    return proc.stdout.strip()


def _dead_pid():
    """The pid of a real child that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _snapshot(root):
    """{relative path: bytes} for files and {relative dir/: '<dir>'} for
    directories under `root`; {} when `root` does not exist."""
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel = os.path.relpath(dirpath, root)
        out[rel + "/"] = "<dir>"
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = fh.read()
    return out


def _bind_real_capacity_candidate(session_uuid, role, controller="claude"):
    """Compile a real dispatch manifest for (session_uuid, role) and bind it
    as the role's WorkUnit candidate, in production's preflighting ->
    running -> candidate-bound order. Returns (work_id, binding)."""
    work_id = cowork._role_work_id(session_uuid, role, 0, 0)
    manifest, _ = cowork._compile_role_manifest(
        role=role, session_uuid=session_uuid, work_id=role,
        controller=controller, mode="implement", model=None, effort=None,
        sessions_dir=state_store.session_assets_dir(session_uuid))
    cowork._ensure_work_unit(session_uuid, work_id, role, controller,
                             model=None, effort=None)
    cowork._advance_phase(session_uuid, work_id, "preflight_started")
    cowork._advance_phase(session_uuid, work_id, "preflight_passed")
    cowork._bind_candidate(session_uuid, work_id, manifest["digest"])
    return work_id, cowork._capacity_candidate_binding(session_uuid, work_id,
                                                       role)


class _FakeResumeSession(object):

    def __init__(self, sent):
        self.sent = sent

    def send(self, text, meta=None):
        self.sent.append(text)
        return {"ok": True, "result": "ok"}

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# Shared fixture mixin.                                                       #
# --------------------------------------------------------------------------- #


class _BindingEnvMixin(object):
    """A temp parent with a main repository (ignoring `.cowork/`), vertex
    roots as linked worktrees, a pinned sessions root, fake lead runners,
    owner-lease helpers and a capacity-pause fixture."""

    def setUp(self):
        super().setUp()
        self.parent = os.path.realpath(tempfile.mkdtemp(prefix="gb-"))
        self.addCleanup(shutil.rmtree, self.parent, True)
        prior = os.getcwd()
        self.addCleanup(os.chdir, prior)
        patch = mock.patch.dict(os.environ, {
            "COWORK_SESSIONS_ROOT": os.path.join(self.parent, "sessions")})
        patch.start()
        self.addCleanup(patch.stop)
        self.sessions = os.environ["COWORK_SESSIONS_ROOT"]
        self._counter = itertools.count(1)
        self.main_repo = self.make_repo("main")
        self.head = _git(["rev-parse", "HEAD"], self.main_repo)
        self.scout_calls = []

    # -- repositories and graphs -------------------------------------------- #

    def name(self, prefix):
        return "%s%d" % (prefix, next(self._counter))

    def make_repo(self, name):
        path = os.path.join(self.parent, name)
        os.mkdir(path)
        _git(["init", "-q"], path)
        with open(os.path.join(path, ".gitignore"), "w") as fh:
            fh.write(IGNORE_RULE)
        with open(os.path.join(path, "README.txt"), "w") as fh:
            fh.write("neutral fixture\n")
        _git(["add", "-A"], path)
        _git(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"],
             path)
        return os.path.realpath(path)

    def worktree(self):
        path = os.path.join(self.parent, self.name("wt"))
        _git(["worktree", "add", "--detach", path], self.main_repo)
        return os.path.realpath(path)

    def claimed(self, profile=STANDARD):
        """A one-vertex graph whose vertex is claimed (not yet bound)."""
        root = self.worktree()
        tag = self.name("authority-")
        data = json.dumps({"authority": tag}).encode("utf-8")
        authority = os.path.join(self.parent, "authority", tag + ".json")
        _write_bytes(authority, data)
        work_id = _uuid()
        graph_id = store.admit({
            "schema_version": 1, "max_parallel": 1,
            "vertices": [{"work_id": work_id, "root": root,
                          "base_commit": self.head,
                          "authority_path": authority,
                          "authority_digest": hashlib.sha256(
                              data).hexdigest(),
                          "profile": profile, "predecessors": []}]}
        )["graph_id"]
        info = store.claim(graph_id, work_id)
        return types.SimpleNamespace(
            graph_id=graph_id, work_id=work_id, root=root,
            epoch=info["lease_epoch"], launch_argv=info["launch_argv"],
            profile=profile)

    def token(self, v, epoch=None):
        return "%s:%s:%d" % (v.graph_id, v.work_id,
                             v.epoch if epoch is None else epoch)

    def graph_bytes(self, graph_id):
        return _read_bytes(state_store.graph_state_path_for(graph_id))

    def vertex_record(self, graph_id, work_id):
        return json.loads(self.graph_bytes(graph_id).decode("utf-8"))[
            "vertices"][work_id]

    def assert_running(self, v, session):
        record = self.vertex_record(v.graph_id, v.work_id)
        self.assertEqual((record["state"], record["session_uuid"],
                          record["lease_epoch"]),
                         ("running", session, v.epoch))
        view = store.status(v.graph_id)
        self.assertEqual([(s["work_id"], s["lease_epoch"])
                          for s in view["held_slots"]],
                         [(v.work_id, v.epoch)])

    # -- run_flow drivers --------------------------------------------------- #

    def forbidden(self, role):
        def fake(*_a, **_k):
            raise AssertionError("%s must not be dispatched" % role)
        return fake

    def fake_scout(self, outcome="ended", check=None):
        def run(config, context, selected, on_outcome=None, on_session=None,
                resume_id=None, **kw):
            self.scout_calls.append(resume_id)
            if check is not None:
                check()
            if on_session and resume_id is None:
                on_session("claude", "scout-" + _uuid())
            if on_outcome:
                on_outcome(outcome, None)
            return 0
        return run

    def run_flow(self, argv, scout=None, box=None):
        box = {} if box is None else box
        out = io.StringIO()
        rc = cowork.run_flow(
            cowork.build_parser().parse_args(argv), io_out=out,
            which=lambda c: "/bin/" + c,
            run_scout_fn=scout or self.forbidden("scout"),
            run_planner_fn=self.forbidden("planner"),
            run_builder_fn=self.forbidden("builder"),
            result_box=box)
        return rc, box

    def bound_run(self, check=None):
        """Claim a vertex and run its child from the root with claim's
        launch_argv; the fake scout ends the run (rc 1)."""
        v = self.claimed()
        os.chdir(v.root)
        box = {}
        rc, box = self.run_flow(v.launch_argv + ["--context", "neutral brief"],
                                scout=self.fake_scout(check=check), box=box)
        self.assertEqual(rc, 1)
        v.session = box["session_uuid"]
        v.spath = box["session_file"]
        v.box = box
        return v

    def expect_refused(self, argv, code, rc):
        before = len(self.scout_calls)
        got_rc, box = self.run_flow(argv)
        self.assertEqual((got_rc, box.get("reason")), (rc, code))
        self.assertEqual(len(self.scout_calls), before)
        return box

    # -- owner leases ------------------------------------------------------- #

    def live_owner(self, session):
        claimant = cowork_owner.owner_identity(session, "run_flow",
                                               self.parent, None)
        lease = cowork_owner.acquire_owner_lease(session, claimant)
        return lease["owner_id"], lease["epoch"]

    def dead_owner(self, session):
        """A lease past its deadline naming an exited pid on this host
        (classifies stale_dead_owner)."""
        claimant = cowork_owner.owner_identity(session, "run_flow",
                                               self.parent, None)
        claimant.update(pid=_dead_pid(), pid_start_at=FABRICATED_PID_START,
                        pid_start_source="ps_lstart")
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=1))
        cowork_owner.acquire_owner_lease(session, claimant, now=past)

    def request_cancel(self, v, session):
        """Record a durable cancel request while a test-held lease is live,
        then release that lease."""
        owner_id, epoch = self.live_owner(session)
        outcomes = store.cancel(v.graph_id, v.work_id)
        self.assertEqual([o["outcome"] for o in outcomes],
                         ["cancel_requested"])
        cowork_owner.release_owner_lease(session, owner_id, epoch,
                                         "normal_exit")

    def assert_unowned(self, session):
        self.assertEqual(cowork_owner.classify_owner_lease(session),
                         "unowned")

    # -- capacity pause and resume-trigger ---------------------------------- #

    def bind_new_session(self, v):
        session = _uuid()
        store.bind_session(v.graph_id, v.work_id, v.epoch, session, v.root,
                           v.profile)
        return session

    def enter_pause(self, root, session):
        """Durably pause `session`'s builder for provider capacity: a real
        candidate binding, a scheduled PauseLease, an acknowledged pending
        turn and the awaiting-capacity phase state."""
        os.chdir(root)
        spath = os.path.join(root, ".cowork", "session.json")
        state_store.ensure_session(spath, None, session)
        work_id, binding = _bind_real_capacity_candidate(session, "builder")
        lease_id = _uuid()
        automation_ref = "cowork.orchestration_resume/v%d" % (
            capacity_scheduler.SCHEDULER_DECISION_LAYER_VERSION)
        digest = binding["candidate_manifest_digest"]
        capacity_scheduler.start_new_episode(session, {
            "schema_version": capacity_contracts.SCHEMA_VERSION,
            "package_id": _uuid(), "lease_id": lease_id, "role": "builder",
            "provider_session_id": "prov-sess-1",
            "controller_policy_digest": binding["controller_policy_digest"],
            "candidate_digest": digest, "resume_mode": "scheduled",
            "not_before": PAUSE_NOT_BEFORE, "automation_ref": automation_ref,
            "artifact_hashes": {"manifest": digest},
            "consumption_state": "unclaimed", "failed_wake_attempts": 0,
            "issued_at": PAUSE_ISSUED_AT,
        })
        state_store.write_pending_turn_before_pause(
            session, "builder", TURN_TEXT, lease_id=lease_id)
        state_store.acknowledge_pending_turn_before_pause(
            session, "builder",
            hashlib.sha256(TURN_TEXT.encode("utf-8")).hexdigest())
        cowork._advance_phase(
            session, work_id, "capacity_reserved",
            evidence={"capacity_evidence": {
                "controller_outcome": "overloaded", "role": "builder",
                "provider_session_id": "prov-sess-1",
                "controller_policy_digest":
                    binding["controller_policy_digest"],
                "candidate_manifest_digest": digest,
                "candidate_index": binding["candidate_index"],
                "resume_mode": "scheduled", "model": None, "effort": None,
                "artifact_hashes": {"manifest": digest},
                "automation_ref": automation_ref}},
            source="test", expected_candidate={
                "candidate_manifest_digest": digest,
                "candidate_index": binding["candidate_index"]})
        state = state_store.load(spath)
        state.setdefault("config", {})["builder"] = {
            "controller": "claude", "model": None, "effort": None,
            "mode": "implement", "yolo": True}
        state.setdefault("sessions", {})["builder"] = {
            "controller": "claude", "id": "prov-sess-1"}
        state_store.save(spath, state)
        return types.SimpleNamespace(root=root, session=session,
                                     lease_id=lease_id,
                                     automation_ref=automation_ref)

    def paused_bound(self):
        v = self.claimed()
        v.session = self.bind_new_session(v)
        v.pause = self.enter_pause(v.root, v.session)
        return v

    def trigger(self, pause, sent, factory_calls):
        def factory(*args, **kwargs):
            factory_calls.append(args)
            return _FakeResumeSession(sent)

        out = []
        rc = cowork.run_resume_trigger([
            "--session-uuid", pause.session, "--role", "builder",
            "--lease-id", pause.lease_id, "--claimant-ref", "wake-1",
            "--automation-ref", pause.automation_ref, "--now", WAKE_NOW,
            "--cwd", pause.root], output=out.append, session_factory=factory)
        return rc, out


# --------------------------------------------------------------------------- #
# Binding at start.                                                           #
# --------------------------------------------------------------------------- #


class BindAtStartTests(_BindingEnvMixin, unittest.TestCase):

    def test_new_session_binds_before_any_dispatch(self):
        seen = {}

        def check():
            # Runs inside the first lead dispatch: the bind already landed.
            session = seen["box"]["session_uuid"]
            record = store.vertex_binding_for_session(session)
            seen["binding"] = (record["graph_id"], record["work_id"],
                               record["lease_epoch"])
            seen["vertex"] = self.vertex_record(seen["v"].graph_id,
                                                seen["v"].work_id)

        v = self.claimed()
        seen["v"] = v
        os.chdir(v.root)
        box = {}
        seen["box"] = box
        rc, box = self.run_flow(v.launch_argv + ["--context", "neutral brief"],
                                scout=self.fake_scout(check=check), box=box)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.scout_calls), 1)
        self.assertEqual(seen["binding"], (v.graph_id, v.work_id, v.epoch))
        self.assertEqual((seen["vertex"]["state"],
                          seen["vertex"]["session_uuid"]),
                         ("running", box["session_uuid"]))
        self.assertEqual(cowork.build_run_result(rc, box)["graph_vertex"],
                         {"graph_id": v.graph_id, "work_id": v.work_id,
                          "lease_epoch": v.epoch})
        self.assertEqual(os.path.dirname(box["session_file"]),
                         os.path.join(v.root, ".cowork"))
        self.assert_running(v, box["session_uuid"])
        self.assert_unowned(box["session_uuid"])

    def test_invalid_graph_vertex_invocations_write_nothing_and_dispatch_nothing(
            self):
        v = self.claimed()
        other_root = self.worktree()
        output_root = os.path.join(self.parent, self.name("out-"))
        os.mkdir(output_root)
        # An existing profiled session to name with --session-file.
        os.chdir(v.root)
        existing = os.path.join(self.parent, "anchors", ".cowork",
                                "session.json")
        rc, _box = self.run_flow(
            ["--profile", STANDARD, "--context", "neutral brief",
             "--session-file", existing], scout=self.fake_scout())
        self.assertEqual(rc, 1)
        self.scout_calls = []
        token = self.token(v)
        new = ["--new", "--profile", STANDARD]
        ctx = ["--context", "neutral brief"]
        cases = [
            ("two parts", v.root,
             new + ["--graph-vertex", "%s:%s" % (v.graph_id, v.work_id)]
             + ctx, "graph_vertex_malformed", 2),
            ("uppercase uuid", v.root,
             new + ["--graph-vertex", "%s:%s:%d" % (
                 v.graph_id.upper(), v.work_id, v.epoch)] + ctx,
             "graph_vertex_malformed", 2),
            ("signed epoch", v.root,
             new + ["--graph-vertex", "%s:%s:+%d" % (
                 v.graph_id, v.work_id, v.epoch)] + ctx,
             "graph_vertex_malformed", 2),
            ("no profile", v.root,
             ["--new", "--graph-vertex", token] + ctx,
             "graph_vertex_requires_profile", 2),
            ("resumed session", v.root,
             ["--profile", STANDARD, "--graph-vertex", token,
              "--session-file", existing],
             "graph_vertex_requires_new_session", 2),
            ("no session", v.root,
             ["--profile", STANDARD, "--graph-vertex", token,
              "--no-session"] + ctx, "profile_requires_session", 2),
            ("worktree", v.root,
             new + ["--graph-vertex", token, "--worktree", "child"] + ctx,
             "graph_vertex_flag_conflict", 2),
            ("team", v.root,
             new + ["--graph-vertex", token, "--team",
                    "scout,scout-reviewer"] + ctx,
             "profile_team_conflict", 2),
            ("output root", v.root,
             new + ["--graph-vertex", token, "--output-root", output_root]
             + ctx, "graph_vertex_flag_conflict", 2),
            ("stale epoch", v.root,
             new + ["--graph-vertex", self.token(v, v.epoch + 1)] + ctx,
             "vertex_lease_superseded", 3),
            ("root mismatch", other_root,
             new + ["--graph-vertex", token] + ctx, "bind_root_mismatch", 2),
            ("profile mismatch", v.root,
             ["--new", "--profile", "assurance", "--graph-vertex", token]
             + ctx, "bind_profile_mismatch", 2),
        ]
        for label, cwd, argv, code, rc in cases:
            with self.subTest(case=label):
                os.chdir(cwd)
                anchor = os.path.join(cwd, ".cowork")
                before = (_snapshot(self.sessions), _snapshot(anchor))
                self.expect_refused(argv, code, rc)
                self.assertEqual((_snapshot(self.sessions),
                                  _snapshot(anchor)), before)
        record = self.vertex_record(v.graph_id, v.work_id)
        self.assertEqual((record["state"], record["session_uuid"]),
                         ("claimed", None))

    @contextlib.contextmanager
    def watched_heartbeat(self):
        real = cowork._run_owner_heartbeat_loop
        seen = []

        def loop(stop_event, fire, interval_seconds):
            seen.append((stop_event, threading.current_thread()))
            return real(stop_event, fire, interval_seconds)

        with mock.patch.object(cowork, "_run_owner_heartbeat_loop",
                               side_effect=loop):
            yield seen

    def test_bind_refusal_after_owner_acquisition_releases_lease(self):
        # (a) a stale epoch that slipped past the pre-write check.
        v = self.claimed()
        os.chdir(v.root)
        with self.watched_heartbeat() as seen, mock.patch.object(
                store, "check_bind_preconditions", return_value=None):
            rc, box = self.run_flow(
                ["--new", "--profile", STANDARD, "--graph-vertex",
                 self.token(v, v.epoch + 1), "--context", "neutral brief"])
        self.assertEqual((rc, box["reason"]), (3, "vertex_lease_superseded"))
        self.assertEqual(self.scout_calls, [])
        self.assert_unowned(box["session_uuid"])
        self.assertEqual(len(seen), 1)
        stop_event, thread = seen[0]
        self.assertTrue(stop_event.is_set())
        thread.join(5)
        self.assertFalse(thread.is_alive())

        # (b) the session index already names another vertex for this
        # session (a race the pre-write check cannot see).
        w = self.claimed()
        os.chdir(w.root)
        real_bind = store.bind_session

        def racing_bind(graph_id, work_id, lease_epoch, session_uuid, cwd,
                        profile, now=None):
            _write_bytes(
                state_store.graph_session_index_path_for(session_uuid),
                json.dumps({"schema_version": 1, "graph_id": _uuid(),
                            "work_id": _uuid(), "lease_epoch": 1,
                            "bound_at": "2030-01-01T00:00:00Z"}).encode(
                                "utf-8"))
            return real_bind(graph_id, work_id, lease_epoch, session_uuid,
                             cwd, profile, now=now)

        with self.watched_heartbeat() as seen, mock.patch.object(
                store, "bind_session", side_effect=racing_bind):
            rc, box = self.run_flow(w.launch_argv
                                    + ["--context", "neutral brief"])
        self.assertEqual((rc, box["reason"]), (3, "session_already_bound"))
        self.assertEqual(self.scout_calls, [])
        self.assert_unowned(box["session_uuid"])
        self.assertEqual(len(seen), 1)
        stop_event, thread = seen[0]
        self.assertTrue(stop_event.is_set())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        record = self.vertex_record(w.graph_id, w.work_id)
        self.assertEqual((record["state"], record["session_uuid"]),
                         ("claimed", None))


# --------------------------------------------------------------------------- #
# Fences at later entry points.                                               #
# --------------------------------------------------------------------------- #


class FenceEntryPointTests(_BindingEnvMixin, unittest.TestCase):

    def test_plain_resume_of_bound_session_continues_same_vertex(self):
        v = self.bound_run()
        rc, box = self.run_flow(["--session-file", v.spath],
                                scout=self.fake_scout())
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.scout_calls), 2)
        self.assertEqual(box["session_uuid"], v.session)
        self.assertEqual(box["graph_vertex"], v.box["graph_vertex"])
        self.assertEqual(cowork.build_run_result(rc, box)["graph_vertex"],
                         {"graph_id": v.graph_id, "work_id": v.work_id,
                          "lease_epoch": v.epoch})
        self.assert_running(v, v.session)
        self.assert_unowned(v.session)

    def test_take_over_of_bound_session_fenced(self):
        v = self.bound_run()
        self.dead_owner(v.session)
        rc, box = self.run_flow(["--session-file", v.spath, "--take-over"],
                                scout=self.fake_scout())
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.scout_calls), 2)
        self.assertEqual(box["graph_vertex"], v.box["graph_vertex"])
        self.assert_running(v, v.session)
        self.assert_unowned(v.session)

        w = self.bound_run()
        self.dead_owner(w.session)
        self.assertEqual(store.reclaim(w.graph_id, w.work_id)[
            "holder_verdict"], "stale_dead_owner")
        calls = len(self.scout_calls)
        rc, box = self.run_flow(["--session-file", w.spath, "--take-over"])
        self.assertEqual((rc, box["reason"]), (3, "vertex_lease_superseded"))
        self.assertEqual(len(self.scout_calls), calls)
        self.assertNotIn("graph_vertex", box)
        self.assert_unowned(w.session)

    def test_reclaimed_session_refused_rc_3_zero_dispatch(self):
        v = self.bound_run()
        self.assertEqual(store.reclaim(v.graph_id, v.work_id)[
            "holder_verdict"], "unowned")
        rc, box = self.run_flow(["--session-file", v.spath])
        self.assertEqual((rc, box["reason"]), (3, "vertex_lease_superseded"))
        self.assertEqual(len(self.scout_calls), 1)
        result = cowork.build_run_result(rc, box)
        self.assertEqual(result["outcome"], "owner_conflict")
        self.assertNotIn("graph_vertex", result)
        self.assert_unowned(v.session)

    def test_cancel_requested_refused_at_next_start(self):
        v = self.bound_run()
        self.request_cancel(v, v.session)
        rc, box = self.run_flow(["--session-file", v.spath])
        self.assertEqual((rc, box["reason"]), (3, "vertex_cancel_requested"))
        self.assertEqual(len(self.scout_calls), 1)
        self.assert_unowned(v.session)

    def test_resume_trigger_fenced_before_capacity_claim(self):
        v = self.paused_bound()
        self.request_cancel(v, v.session)
        sent, factory_calls = [], []
        rc, out = self.trigger(v.pause, sent, factory_calls)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_OWNER_CONFLICT)
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(out[0]), {
            "outcome": "owner_conflict", "session_uuid": v.session,
            "reason": "vertex_cancel_requested"})
        lease = state_store.read_pause_lease(v.session, v.pause.lease_id)
        self.assertEqual(lease["consumption_state"], "unclaimed")
        self.assertEqual(lease["failed_wake_attempts"], 0)
        self.assertEqual((sent, factory_calls), ([], []))
        self.assertIsNotNone(state_store.read_pending_turn_before_pause(
            v.session, "builder"))
        self.assert_unowned(v.session)

    def test_every_fence_refusal_releases_owner_lease(self):
        # run_flow plain resume.
        plain = self.bound_run()
        store.reclaim(plain.graph_id, plain.work_id)
        rc, _box = self.run_flow(["--session-file", plain.spath])
        self.assertEqual(rc, 3)
        self.assert_unowned(plain.session)
        # run_flow take-over.
        taken = self.bound_run()
        self.request_cancel(taken, taken.session)
        self.dead_owner(taken.session)
        rc, _box = self.run_flow(["--session-file", taken.spath,
                                  "--take-over"])
        self.assertEqual(rc, 3)
        self.assert_unowned(taken.session)
        # resume-trigger.
        paused = self.paused_bound()
        self.request_cancel(paused, paused.session)
        rc, _out = self.trigger(paused.pause, [], [])
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_OWNER_CONFLICT)
        self.assert_unowned(paused.session)
        self.assertEqual(len(self.scout_calls), 2)


# --------------------------------------------------------------------------- #
# Capacity pause keeps the slot.                                              #
# --------------------------------------------------------------------------- #


class CapacityPauseSlotTests(_BindingEnvMixin, unittest.TestCase):

    def test_bound_child_pause_keeps_slot_and_binding(self):
        v = self.claimed()
        session = self.bind_new_session(v)
        index_path = state_store.graph_session_index_path_for(session)
        before = (self.graph_bytes(v.graph_id), _read_bytes(index_path))
        self.enter_pause(v.root, session)
        self.assertTrue(state_store.live_pause_lease_ids(session))
        self.assertEqual((self.graph_bytes(v.graph_id),
                          _read_bytes(index_path)), before)
        self.assert_running(v, session)
        self.assertEqual(store.status(v.graph_id)["statuses"][v.work_id],
                         "running")

    def test_reclaim_refused_while_paused(self):
        v = self.paused_bound()
        self.assert_unowned(v.session)
        before = self.graph_bytes(v.graph_id)
        out = []
        rc = cowork_graph_cli.main(["reclaim", "--graph-id", v.graph_id,
                                    "--work-id", v.work_id],
                                   output=out.append)
        line = json.loads("".join(out).splitlines()[-1])
        self.assertEqual((rc, line["reason"], line["outcome"]),
                         (3, "vertex_paused", "refused"))
        self.assertEqual(self.graph_bytes(v.graph_id), before)

    def test_resume_trigger_continues_same_vertex(self):
        v = self.paused_bound()
        sent, factory_calls = [], []
        rc, _out = self.trigger(v.pause, sent, factory_calls)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
        self.assertEqual(sent, [TURN_TEXT])
        self.assertEqual(len(factory_calls), 1)
        self.assert_running(v, v.session)
        self.assertEqual(store.vertex_binding_for_session(v.session)[
            "lease_epoch"], v.epoch)
        self.assert_unowned(v.session)


# --------------------------------------------------------------------------- #
# Unbound sessions are unchanged.                                             #
# --------------------------------------------------------------------------- #


class UnboundUnchangedTests(_BindingEnvMixin, unittest.TestCase):

    def anchor(self):
        return os.path.join(self.parent, self.name("anchor-"), ".cowork",
                            "session.json")

    def test_unbound_run_result_has_no_graph_vertex_key(self):
        os.chdir(self.main_repo)
        for label, first in (
                ("legacy", ["--team", "scout,scout-reviewer"]),
                ("profiled", ["--profile", STANDARD])):
            with self.subTest(session=label):
                spath = self.anchor()
                rc, box = self.run_flow(
                    first + ["--context", "neutral brief", "--session-file",
                             spath], scout=self.fake_scout())
                self.assertEqual(rc, 1)
                self.assertNotIn("graph_vertex", box)
                self.assertNotIn("graph_vertex",
                                 cowork.build_run_result(rc, box))
                rc, box = self.run_flow(["--session-file", spath],
                                        scout=self.fake_scout())
                self.assertEqual(rc, 1)
                self.assertNotIn("graph_vertex",
                                 cowork.build_run_result(rc, box))
        # Through cowork.main: the emitted run-result line has no key.
        real_run_flow = cowork.run_flow

        def run_flow(args, io_out=None, result_box=None, **_kw):
            return real_run_flow(
                args, io_out=io_out, which=lambda c: "/bin/" + c,
                run_scout_fn=self.fake_scout(),
                run_planner_fn=self.forbidden("planner"),
                run_builder_fn=self.forbidden("builder"),
                result_box=result_box)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cowork, "run_flow", side_effect=run_flow), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cowork.main(["--team", "scout,scout-reviewer", "--context",
                              "neutral brief", "--session-file",
                              self.anchor()])
        line = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual((line["cowork_result"], line["rc"]), (1, rc))
        self.assertNotIn("graph_vertex", line)

    def test_unbound_runs_create_no_graphs_dir(self):
        real_fence = store.fence
        calls = []

        def fence(session_uuid, now=None):
            result = real_fence(session_uuid, now=now)
            calls.append((session_uuid, result))
            return result

        os.chdir(self.main_repo)
        spath = self.anchor()
        with mock.patch.object(store, "fence", side_effect=fence):
            rc, box = self.run_flow(
                ["--team", "scout,scout-reviewer", "--context",
                 "neutral brief", "--session-file", spath],
                scout=self.fake_scout())
            self.assertEqual(rc, 1)
            session = box["session_uuid"]
            rc, _box = self.run_flow(["--session-file", spath],
                                     scout=self.fake_scout())
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [(session, None), (session, None)])
            rc, _box = self.run_flow(
                ["--no-session", "--team", "scout,scout-reviewer",
                 "--context", "neutral brief"], scout=self.fake_scout())
            self.assertEqual(rc, 1)
            self.assertEqual(len(calls), 2)
            pause = self.enter_pause(self.worktree(), _uuid())
            sent = []
            rc, _out = self.trigger(pause, sent, [])
            self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
            self.assertEqual(sent, [TURN_TEXT])
        self.assertEqual(calls[2:], [(pause.session, None)])
        self.assertFalse(os.path.exists(state_store.graphs_root()))

    def test_legacy_and_profiled_session_files_untouched(self):
        os.chdir(self.main_repo)
        legacy = self.anchor()
        rc, box = self.run_flow(
            ["--team", "scout,scout-reviewer", "--context", "neutral brief",
             "--session-file", legacy], scout=self.fake_scout())
        self.assertEqual(rc, 1)
        profiled = self.anchor()
        rc, pbox = self.run_flow(
            ["--profile", STANDARD, "--context", "neutral brief",
             "--session-file", profiled], scout=self.fake_scout())
        self.assertEqual(rc, 1)
        record_path = state_store.execution_profile_path_for(
            pbox["session_uuid"])
        for path in (legacy, profiled, record_path):
            with self.subTest(path=path):
                data = json.loads(_read_bytes(path).decode("utf-8"))
                self.assertNotIn("graph_vertex", data)
        # A pre-existing session file resumed by a plain run keeps every
        # key it had.
        before = json.loads(_read_bytes(legacy).decode("utf-8"))
        rc, rbox = self.run_flow(["--session-file", legacy],
                                 scout=self.fake_scout())
        self.assertEqual(rc, 1)
        after = json.loads(_read_bytes(legacy).decode("utf-8"))
        self.assertTrue(set(before) <= set(after))
        self.assertNotIn("graph_vertex", after)
        self.assertEqual(rbox["session_uuid"], box["session_uuid"])
        self.assertEqual(state_store.get_session_uuid(after),
                         state_store.get_session_uuid(before))
        self.assertFalse(os.path.exists(state_store.graphs_root()))


if __name__ == "__main__":
    unittest.main()
