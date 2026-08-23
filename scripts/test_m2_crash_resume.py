#!/usr/bin/env python3
"""M2 Package F: end-to-end crash/resume suite.

Exercises every durable state-write boundary Package B introduced --
PhaseState, WorkUnit (mint + transition), dependency-graph revision, and the
atomic controller policy/config transition -- through the SAME production
functions `scripts/cowork.py` itself calls (`cowork._advance_phase`,
`cowork._ensure_work_unit`, `cowork._bind_candidate`, the real
`--switch-controller` `run_flow` seam), never a reimplemented or bypassed
write path. Package B's own suite (`test_cowork_state_m2.py`) already proves
every boundary exhaustively at the unit/primitive level; this file's job is
narrower and additive: prove each boundary ALSO survives a crash when struck
through the real integrated caller, and that resume reconstructs exactly what
a clean run would have produced -- never a torn record, never a fabricated
`completed`.

Every injected fault is a directly patched short write / fsync failure / CAS
write failure -- never a hoped-for real crash timing -- matching this
repo's own no-flake discipline.

Boundaries covered (one class each):

    1. PhaseState append: short write, fsync failure, torn-tail repair
       before the next live append.
    2. WorkUnit mint: short write, and idempotent live resume.
    3. WorkUnit transition (`_bind_candidate`): short write, torn-tail
       repair before the next live transition.
    4. Dependency-graph revision (Package B's real durable store -- the
       deepest live seam that exists; see test_m2_negative_controls.py's
       module-level FINDING for why no cowork.py call site appends one):
       short write, and resume producing the correctly numbered next
       revision.
    5. Atomic controller policy/config transition, exercised through the
       REAL `--switch-controller` `run_flow` CLI seam: a CAS write failure
       leaves the persisted + active policy byte-identical, and a plain
       resume commits exactly what a clean run would have.

Run standalone:

    python3 -m unittest scripts/test_m2_crash_resume.py -v
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_policy as policy  # noqa: E402
import cowork_state as state_store  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class _M2CrashEnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test, matching every other M2 crash
    suite's own isolation discipline (test_cowork_state_m2.py's own
    `_M2EnvMixin`), reproduced independently here."""

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

    def _raw_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()


def _truncate_tail_bytes(path, n):
    with open(path, "r+b") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.truncate(max(0, size - n))


# =============================================================================
# 1. PhaseState append boundary, through cowork._advance_phase -- the exact
#    seam the module docstring names as "the one seam every production
#    phase advance in this file passes through".
# =============================================================================

class PhaseStateCrashResumeTest(_M2CrashEnvMixin, unittest.TestCase):

    def _seed_preflighting(self, session_uuid, work_id):
        cowork._ensure_work_unit(session_uuid, work_id, "scout", "opencode")
        record = cowork._advance_phase(
            session_uuid, work_id, "preflight_started")
        self.assertEqual(record["state"], "preflighting")
        return state_store.phase_state_history_path_for(session_uuid, work_id)

    def test_short_write_crash_mid_advance_leaves_no_torn_tail_then_resumes(self):
        session_uuid, work_id = _uuid(), _uuid()
        path = self._seed_preflighting(session_uuid, work_id)
        before = self._raw_bytes(path)

        real_write_all = state_store._write_all_fd

        def failing_write_all(fd, data):
            os.write(fd, data[:4])  # a genuine partial write actually lands
            raise OSError("simulated short/interrupted write")

        state_store._write_all_fd = failing_write_all
        try:
            with self.assertRaises(OSError):
                cowork._advance_phase(session_uuid, work_id, "preflight_passed")
        finally:
            state_store._write_all_fd = real_write_all

        # No torn fragment survives the crash: the file is byte-identical
        # to the pre-attempt state, and the durable current state is still
        # the last genuinely completed transition, never a half-written
        # "running" record and never a fabricated "completed".
        self.assertEqual(self._raw_bytes(path), before)
        current = state_store.current_phase_state(session_uuid, work_id)
        self.assertEqual(current["state"], "preflighting")

        # RESUME: the exact same live call, un-faulted, reproduces exactly
        # what a clean run would have produced.
        resumed = cowork._advance_phase(
            session_uuid, work_id, "preflight_passed")
        self.assertEqual(resumed["state"], "running")
        self.assertEqual(
            [h["state"] for h in
             state_store.read_phase_state_history(session_uuid, work_id)],
            ["preflighting", "running"])

    def test_fsync_failure_crash_mid_advance_rolls_back_then_resumes(self):
        session_uuid, work_id = _uuid(), _uuid()
        path = self._seed_preflighting(session_uuid, work_id)
        before = self._raw_bytes(path)

        real_fsync = state_store.os.fsync

        def failing_fsync(fd):
            raise OSError("simulated fsync failure")

        state_store.os.fsync = failing_fsync
        try:
            with self.assertRaises(OSError):
                cowork._advance_phase(session_uuid, work_id, "preflight_passed")
        finally:
            state_store.os.fsync = real_fsync

        self.assertEqual(self._raw_bytes(path), before)
        self.assertEqual(
            state_store.current_phase_state(session_uuid, work_id)["state"],
            "preflighting")

        resumed = cowork._advance_phase(
            session_uuid, work_id, "preflight_passed")
        self.assertEqual(resumed["state"], "running")

    def test_torn_tail_repaired_before_next_live_advance(self):
        session_uuid, work_id = _uuid(), _uuid()
        path = self._seed_preflighting(session_uuid, work_id)

        # A crash left a torn, non-newline-terminated fragment for a
        # transition that never durably landed -- exactly what an
        # interrupted `os.write`/process kill mid-append leaves behind.
        with open(path, "ab") as fh:
            fh.write(b'{"state": "runni')
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))
        self.assertEqual(
            [h["state"] for h in
             state_store.read_phase_state_history(session_uuid, work_id)],
            ["preflighting"],
            "a reader must tolerate the torn tail, seeing only genuine "
            "prior history")

        # The NEXT real, live production append -- through the exact same
        # `cowork._advance_phase` seam, not a direct primitive call --
        # must repair the torn tail before landing its own record.
        resumed = cowork._advance_phase(
            session_uuid, work_id, "preflight_passed")
        self.assertEqual(resumed["state"], "running")
        raw_lines = self._raw_bytes(path).splitlines()
        self.assertEqual(len(raw_lines), 2)
        self.assertEqual(
            [h["state"] for h in
             state_store.read_phase_state_history(session_uuid, work_id)],
            ["preflighting", "running"])


# =============================================================================
# 2. WorkUnit mint boundary, through cowork._ensure_work_unit.
# =============================================================================

class WorkUnitMintCrashResumeTest(_M2CrashEnvMixin, unittest.TestCase):

    def test_short_write_crash_mid_mint_mints_nothing_then_resumes(self):
        session_uuid, work_id = _uuid(), _uuid()

        real_write_all = state_store._write_all_fd

        def failing_write_all(fd, data):
            os.write(fd, data[:4])
            raise OSError("simulated short/interrupted write")

        state_store._write_all_fd = failing_write_all
        try:
            with self.assertRaises(OSError):
                cowork._ensure_work_unit(
                    session_uuid, work_id, "builder", "claude",
                    model="sonnet", effort="high")
        finally:
            state_store._write_all_fd = real_write_all

        # A crash strictly inside the mint write must leave the work_id
        # genuinely un-minted -- never a partial/corrupt WorkUnit record a
        # reader could observe.
        self.assertIsNone(
            state_store.current_work_unit_state(session_uuid, work_id))

        # RESUME: the exact same live call, un-faulted, mints truthfully --
        # the genuine (never fabricated) model/effort identity survives the
        # crash and reappears intact.
        minted = cowork._ensure_work_unit(
            session_uuid, work_id, "builder", "claude",
            model="sonnet", effort="high")
        self.assertIsNotNone(minted)
        self.assertEqual(minted["lifecycle_state"], "pending")
        self.assertEqual(minted["requested_model"], "sonnet")
        self.assertEqual(minted["effort"], "high")
        self.assertEqual(
            len(state_store.read_work_unit_history(session_uuid, work_id)), 1,
            "exactly one mint landed -- the crashed attempt left nothing "
            "to accidentally double-mint against")

    def test_second_ensure_after_clean_mint_is_idempotent_not_a_remint(self):
        """The counterpart resume case: a process that mints successfully,
        then a caller (e.g. a resumed process re-deriving the same
        deterministic work_id) calls `_ensure_work_unit` again -- must
        return the SAME durable record, never a second mint attempt racing
        or duplicating the first."""
        session_uuid, work_id = _uuid(), _uuid()
        first = cowork._ensure_work_unit(
            session_uuid, work_id, "scout", "opencode")
        second = cowork._ensure_work_unit(
            session_uuid, work_id, "scout", "opencode")
        self.assertEqual(first["work_id"], second["work_id"])
        self.assertEqual(
            len(state_store.read_work_unit_history(session_uuid, work_id)), 1)


# =============================================================================
# 3. WorkUnit transition boundary, through cowork._bind_candidate.
# =============================================================================

class WorkUnitTransitionCrashResumeTest(_M2CrashEnvMixin, unittest.TestCase):

    def _seed_minted(self, session_uuid, work_id):
        cowork._ensure_work_unit(session_uuid, work_id, "scout", "opencode")
        return state_store.work_unit_history_path_for(session_uuid, work_id)

    def test_short_write_crash_mid_bind_leaves_prior_binding_then_resumes(self):
        session_uuid, work_id = _uuid(), _uuid()
        path = self._seed_minted(session_uuid, work_id)
        before = self._raw_bytes(path)
        digest = "a" * 64

        real_write_all = state_store._write_all_fd

        def failing_write_all(fd, data):
            os.write(fd, data[:4])
            raise OSError("simulated short/interrupted write")

        state_store._write_all_fd = failing_write_all
        try:
            with self.assertRaises(OSError):
                cowork._bind_candidate(session_uuid, work_id, digest)
        finally:
            state_store._write_all_fd = real_write_all

        self.assertEqual(self._raw_bytes(path), before)
        current = state_store.work_unit_from_history_record(
            state_store.current_work_unit_state(session_uuid, work_id))
        self.assertIsNone(current["candidate_manifest_digest"],
                          "a crashed bind must never leave a half-bound "
                          "candidate visible")

        resumed = cowork._bind_candidate(session_uuid, work_id, digest)
        self.assertEqual(resumed["candidate_manifest_digest"], digest)

    def test_torn_tail_in_work_unit_store_repaired_before_next_live_transition(self):
        session_uuid, work_id = _uuid(), _uuid()
        path = self._seed_minted(session_uuid, work_id)

        with open(path, "ab") as fh:
            fh.write(b'{"lifecycle_state": "runni')
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))

        digest = "b" * 64
        resumed = cowork._bind_candidate(session_uuid, work_id, digest)
        self.assertEqual(resumed["candidate_manifest_digest"], digest)
        raw_lines = self._raw_bytes(path).splitlines()
        self.assertEqual(len(raw_lines), 2)
        history = state_store.read_work_unit_history(session_uuid, work_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["candidate_manifest_digest"], digest)


# =============================================================================
# 4. Dependency-graph revision boundary, through B's real durable store.
# =============================================================================

class GraphRevisionCrashResumeTest(_M2CrashEnvMixin, unittest.TestCase):

    def _node(self, work_id):
        return {
            "work_id": work_id, "candidate_manifest_digest": None,
            "candidate_index": None, "governed_child_policy": "inherit",
            "predecessor_work_ids": [],
        }

    def test_short_write_crash_mid_append_persists_no_revision_then_resumes(self):
        session_uuid = _uuid()
        path = state_store.graph_revisions_path_for(session_uuid)

        real_write_all = state_store._write_all_fd

        def failing_write_all(fd, data):
            os.write(fd, data[:4])
            raise OSError("simulated short/interrupted write")

        state_store._write_all_fd = failing_write_all
        try:
            with self.assertRaises(OSError):
                state_store.append_graph_revision(
                    session_uuid, [self._node(_uuid())])
        finally:
            state_store._write_all_fd = real_write_all

        self.assertEqual(state_store.read_graph_revisions(session_uuid), (),
                         "a crashed append must persist zero revisions")
        self.assertFalse(os.path.exists(path) and self._raw_bytes(path))

        resumed = state_store.append_graph_revision(
            session_uuid, [self._node(_uuid())])
        self.assertEqual(resumed["graph_revision"], 1)
        self.assertEqual(len(state_store.read_graph_revisions(session_uuid)), 1)


# =============================================================================
# 5. Atomic controller policy/config transition boundary, through the REAL
#    `--switch-controller` `run_flow` CLI seam.
# =============================================================================

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


def _patch_bridge_popen(popen):
    return mock.patch.object(
        bridge, "subprocess", _BridgeSubprocess(bridge.subprocess, popen))


class PolicyConfigTransitionCrashResumeTest(_M2CrashEnvMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        policy.deactivate()
        self.addCleanup(policy.deactivate)

    def _dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def _session(self, uuid_str, phase, controllers, team=None):
        spath = os.path.join(self._dir(), ".cowork", "session.json")
        team = team or list(controllers)
        state = state_store.ensure_session(spath, None, uuid_str)
        cfg = cowork.default_config(team)
        for role, controller in controllers.items():
            cfg[role] = dict(cfg[role], controller=controller)
        state = state_store.save_config(spath, team, cfg, prior=state)
        state_store.save_phase(spath, phase, prior=state)
        return spath

    def test_cas_write_failure_leaves_byte_identical_state_then_resume_commits(self):
        session_uuid = "f-crash-resume-cas"
        spath = self._session(
            session_uuid, "planning", {"planner": "claude"},
            team=["scout", "planner"])
        before_bytes = self._raw_bytes(spath)
        before_transition = state_store.read_controller_transition(
            session_uuid)
        self.assertEqual(before_transition["revision"], 0)
        before_active = policy.active_meta()

        cas_path = state_store.controller_transition_path_for(session_uuid)
        real_write_json_atomic = state_store.write_json_atomic

        def selective_failure(path, data):
            if os.path.abspath(path) == os.path.abspath(cas_path):
                return False
            return real_write_json_atomic(path, data)

        popen = _RecordingPopen()
        with _patch_bridge_popen(popen), \
                mock.patch.object(state_store, "write_json_atomic",
                                  side_effect=selective_failure):
            rc = cowork.run_flow(
                cowork.build_parser().parse_args(
                    ["--session-file", spath,
                     "--switch-controller", "planner=codex"]),
                io_in=io.StringIO(), io_out=io.StringIO(),
                which=lambda c: "/bin/" + c,
                run_planner_fn=lambda *a, **k: 0)

        self.assertEqual(rc, 1)
        self.assertEqual(popen.calls, [], "a crashed CAS write must "
                         "dispatch nothing")
        self.assertEqual(self._raw_bytes(spath), before_bytes,
                         "a crashed CAS write must leave the legacy "
                         "session file byte-identical")
        after_transition = state_store.read_controller_transition(
            session_uuid)
        self.assertEqual(after_transition, before_transition,
                         "a crashed CAS write must leave the durable "
                         "transition store byte-identical (still revision "
                         "0) -- never a half-committed revision")
        after_active = policy.active_meta()
        for field in ("mode", "allowed", "raw"):
            self.assertEqual(after_active[field], before_active[field])

        # RESUME: the exact same live seam, un-faulted, commits exactly
        # what a clean run would have produced.
        popen2 = _RecordingPopen()
        with _patch_bridge_popen(popen2):
            rc2 = cowork.run_flow(
                cowork.build_parser().parse_args(
                    ["--session-file", spath,
                     "--switch-controller", "planner=codex"]),
                io_in=io.StringIO(), io_out=io.StringIO(),
                which=lambda c: "/bin/" + c,
                run_planner_fn=lambda *a, **k: 0)
        self.assertEqual(rc2, 0)
        committed = state_store.read_controller_transition(session_uuid)
        self.assertEqual(committed["revision"], 1)
        recovered = state_store.load(spath)
        self.assertEqual(recovered["config"]["planner"]["controller"],
                         "codex")


if __name__ == "__main__":
    unittest.main()
