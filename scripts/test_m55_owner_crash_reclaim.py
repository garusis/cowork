#!/usr/bin/env python3
"""Focused suite for issue #64 implementation package **P5** -- deterministic
owner fixtures.

What this module owns is the RUNTIME, REAL-OS-PROCESS evidence the accepted
fixture set demands and that no in-process suite can carry -- a child that is
really SIGKILLed mid-turn, a predecessor that is really signalled while a
successor is really taking over, a pid really re-bound to a live process, and
a takeover that really escalates against a victim that really survives:

  - **F2 (clean restart).** A real separate-process flow runs to a clean exit;
    the durable lease is `released`; the next REAL `run_flow` restart takes
    epoch 2 through the ordinary acquire path, with `take_over` a raise-on-call
    double and `select_takeover_mode` never consulted.

  - **F3 (a crashed owner).** A real child is SIGKILLed mid-turn. BEFORE the
    deadline the verdict is `live_owner` and a plain acquire is refused; AFTER
    an injected-clock deadline the verdict is `stale_dead_owner`, a plain
    acquire is STILL refused, `select_takeover_mode` returns exactly
    `proved_dead` (asserted explicitly, never inferred from a takeover
    succeeding), and the explicit takeover bumps the epoch with
    `predecessor.evidence == "pid_absent"`.

  - **F4 (SIGTERM under a concurrent takeover).** A real `run_flow` child owns
    the session and parks mid-turn; a successor takes over; the predecessor is
    then really SIGTERMed. Its FULL consequence set is asserted -- the
    per-owner sidecar with `owner_matched` False, the aborted PhaseState, the
    `run.external_kill` trace event and exit status 143 -- alongside the
    property those effects exist to make safe: the successor's
    `owner/lease.json` is BYTE-IDENTICAL across the signal.

  - **F5 (pid reuse).** The lease names a pid bound to a genuinely NEWLY
    SPAWNED, still-running child whose recorded `pid_start_at` is a fabricated
    past instant (`_PS_LSTART_FORMAT` has one-SECOND resolution, so relying on
    two real start times differing is a latent flake in exactly the fixture
    that exists to prove pid reuse is never mistaken for liveness). The verdict
    is `stale_dead_owner`, never `live_owner`, and the innocent live process is
    never signalled.

  - **F11 (takeover preserves the session).** Ten artifact classes are
    sha256-identical across a REAL takeover, enumerated as an exact allowlist,
    and the whole-tree per-path digest map differs only at `owner/lease.json`
    and `owner/history.jsonl`.

  - **F12 (an aborted takeover).** A real live victim survives both escalation
    steps; both signals are recorded for that exact pid; the takeover raises
    `OwnerLeaseConflict('live_owner')`; and the lease file's bytes are
    identical before and after.

  - **F13 / gate G8 (the SIGTERM-versus-takeover race).** 52 real processes:
    50 randomized interleavings -- randomized in BOTH the delay and which side
    moves first, with the test asserting it really drove both orderings -- plus
    BOTH deterministic write orders driven through the full production
    `_handle_external_kill`. The test reports its own interleaving count, the
    orderings it drove, the routes the race actually took, and the per-arm
    mechanism, and asserts the count is >= 50 -- never sampled, never quietly
    reduced.

Every fixture redirects `COWORK_SESSIONS_ROOT` into a fresh `tempfile.mkdtemp`
(so nothing here touches the real home dir and nothing is written inside the
worktree), injects `now=` rather than sleeping toward a deadline, registers
every child it spawns and kills any survivor unconditionally in cleanup, and
spawns no provider and no network client. `COWORK_LIVE` is never set.

Run standalone:

    python3 -m unittest scripts.test_m55_owner_crash_reclaim -v
"""

import datetime
import hashlib
import io
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_owner as owner  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

# A fabricated `pid_start_at` far enough in the past that no real process can
# carry it. Used wherever a fixture needs "this pid is alive but is NOT the
# process the lease named" to be DETERMINISTIC rather than dependent on two
# real start times happening to differ at one-second resolution.
FABRICATED_PID_START = "2020-01-01T00:00:00Z"

# How far ahead of the real clock an injected instant is pushed to put a fresh
# lease past its own deadline. DEFAULT_HEARTBEAT_INTERVAL_S +
# DEFAULT_LEASE_GRACE_S is 150s; an hour is an order of magnitude beyond it and
# is never compared against anything but the lease's own derived deadline.
EXPIRED_AHEAD_S = 3600

# G8's accepted depth. Reported by the test itself and asserted, so a reduced
# run cannot pass quietly. The delay seed is FIXED so the interleaving pattern
# is reproducible from the recorded output -- randomized across the run, not
# across reruns of a failure.
G8_MIN_INTERLEAVINGS = 50
G8_MAX_RACE_DELAY_S = 0.040
G8_SEED = 6405

# Bounded waits. Nothing here sleeps toward a deadline; these only bound how
# long a fixture will wait for a real child to reach a gate or to die.
CHILD_READY_TIMEOUT_S = 60
CHILD_REAP_TIMEOUT_S = 60


# --------------------------------------------------------------------------- #
# Shared helpers, RE-DECLARED LOCALLY.                                          #
#                                                                              #
# Deliberately not imported from a frozen suite: `unittest.TestLoader.          #
# loadTestsFromModule` collects any `TestCase` subclass that is an ATTRIBUTE of #
# the module under test, so `from <frozen suite> import ...` would silently     #
# re-run another package's suite inside this one -- inflating this module's     #
# reported test count and importing another package's pass/fail into P5's gate. #
# --------------------------------------------------------------------------- #


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as fh:
        return _sha256(fh.read())


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _expired_instant():
    """An injected instant far enough ahead of the real clock that a lease
    written 'now' is already past its own derived deadline."""
    return _utcnow() + datetime.timedelta(seconds=EXPIRED_AHEAD_S)


def _lease_file(session_uuid):
    """The lease file, spelled WITHOUT `owner_lease_path_for` and without the
    literal `"owner/lease.json"`, on purpose.

    G3d's repo-wide sweep asserts that neither of those two spellings ever
    reaches an `open(...)` call anywhere in the repository -- INCLUDING in a
    new test file. A fixture that needs the lease's raw bytes therefore
    composes the path from `owner_dir_for`, which is a plain directory helper
    carrying none of the lease's single-writer contract. Precedent and reason:
    scripts/test_m55_owner_store.py:260-269."""
    return os.path.join(state_store.owner_dir_for(session_uuid), "lease.json")


def _read_raw_lease_bytes(session_uuid):
    with open(_lease_file(session_uuid), "rb") as fh:
        return fh.read()


def _child_env(root):
    env = dict(os.environ)
    env["COWORK_SESSIONS_ROOT"] = root
    env["COWORK_SCRIPTS"] = _HERE
    env["PYTHONPATH"] = _HERE + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("COWORK_LIVE", None)
    return env


def _dead_pid():
    """A pid that has genuinely exited -- a real crashed owner's pid, obtained
    by letting a real child run and reaping it, never a guessed number."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class _Raises(object):
    """A double that fails the test if anything ever calls it. Used wherever
    the assertion is the ABSENCE of an action."""

    def __init__(self, label):
        self.label = label

    def __call__(self, *args, **kwargs):
        raise AssertionError("%s was invoked" % self.label)


class _Recorder(object):
    """A passthrough double that records every call it forwards."""

    def __init__(self, real):
        self._real = real
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._real(*args, **kwargs)


# --------------------------------------------------------------------------- #
# The two real-child programs.                                                  #
#                                                                              #
# `_LIGHT_OWNER_CHILD` imports ONLY `cowork_owner`, takes the lease, and (in    #
# `mark_on_term` mode) calls the real `mark_owner_terminal_unlocked` with the   #
# production argument set -- including `owner_matched` COMPUTED from a real     #
# `assert_owner`, exactly as the advisory `_require_owner` computes it, rather  #
# than hardcoded.                                                               #
#                                                                              #
# WHAT IT SUBSTITUTES, DISCLOSED EXACTLY: `run_flow`'s surrounding frame, and   #
# nothing else. Specifically, the light child does NOT perform the three        #
# effects `_handle_external_kill` commits before the mark -- the durable        #
# `aborted` PhaseState write, the `run.external_kill` trace event, and the      #
# `SystemExit` unwinding through `run_flow`'s own handler chain and release     #
# `finally`. All three are driven by the two deterministic G8 orders and by F4, #
# both of which use `_FLOW_CHILD` and the real production handler.              #
# --------------------------------------------------------------------------- #

_LIGHT_OWNER_CHILD = r'''
import json, os, signal, sys, time
sys.path.insert(0, os.environ["COWORK_SCRIPTS"])
import cowork_owner as owner

session_uuid, ready, mode, fabricate = sys.argv[1:5]

if fabricate:
    # A CLOCK/IDENTITY INJECTION, not an assertion weakening: this process is
    # genuinely alive and genuinely holds the lease; only the start time the
    # lease RECORDS is fabricated, which is what makes "this pid is alive but
    # is not the process the lease named" deterministic at `ps -o lstart=`'s
    # one-second resolution.
    owner.read_process_start_time = lambda pid, _f=fabricate: _f

claimant = owner.owner_identity(session_uuid, "run_flow", os.getcwd(), None)
record = owner.acquire_owner_lease(session_uuid, claimant)


def _on_term(signum, frame):
    # `owner_matched` is COMPUTED the way production computes it, not
    # hardcoded: the advisory verdict from a real `assert_owner`, which is
    # exactly what `_require_owner(advisory=True)` records in the owner
    # context for `_handle_external_kill` to pass on. In the takeover arm this
    # is genuinely False, and a constant True here would have written an audit
    # field claiming a match the process no longer had.
    try:
        owner.assert_owner(session_uuid, record["owner_id"], record["epoch"])
        matched = True
    except owner.OwnerLeaseError:
        matched = False
    owner.mark_owner_terminal_unlocked(
        session_uuid, record["owner_id"], record["epoch"], "sigterm",
        owner_matched=matched)
    raise SystemExit(128 + signum)


if mode == "mark_on_term":
    signal.signal(signal.SIGTERM, _on_term)
elif mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

with open(ready + ".tmp", "w") as fh:
    json.dump({"pid": os.getpid(), "owner_id": record["owner_id"],
               "epoch": record["epoch"],
               "pid_start_at": record.get("pid_start_at")}, fh)
os.replace(ready + ".tmp", ready)

_deadline = time.time() + 300
while time.time() < _deadline:
    time.sleep(0.02)
'''

# A real `cowork.run_flow` in a real child process, with a fake lead role so no
# provider is ever contacted. It installs the REAL production SIGTERM handler
# (`run_flow`'s own `_handle_external_kill`), so everything asserted about the
# signal path here is asserted about production code.
_FLOW_CHILD = r'''
import io, json, os, sys, time
sys.path.insert(0, os.environ["COWORK_SCRIPTS"])
import cowork_owner as owner

_fabricate = os.environ.get("COWORK_P5_FAKE_PID_START", "")
if _fabricate:
    owner.read_process_start_time = lambda pid, _f=_fabricate: _f

import cowork

spath, ready, park = sys.argv[1:4]
# A new session at an explicit path is `--session-file` alone.
_extra = [a for a in sys.argv[4:] if a != "--new"]


def _scout(config, context, selected, io_in=None, io_out=None,
           on_outcome=None, **kwargs):
    ctx = cowork._current_owner_context()
    with open(ready + ".tmp", "w") as fh:
        json.dump({"pid": os.getpid(), "owner_id": ctx["owner_id"],
                   "epoch": ctx["epoch"],
                   "session_uuid": ctx["session_uuid"]}, fh)
    os.replace(ready + ".tmp", ready)
    if park:
        _deadline = time.time() + 300
        while not os.path.exists(park) and time.time() < _deadline:
            time.sleep(0.02)
    if on_outcome is not None:
        on_outcome("approved", None)
    return 0


_args = cowork.build_parser().parse_args(
    ["--team", "scout,scout-reviewer", "--config", "scout=claude", "--context", "goal",
     "--session-file", spath] + _extra)
_rc = cowork.run_flow(_args, io_out=sys.stdout,
                      which=lambda c: "/bin/" + c, run_scout_fn=_scout)
sys.stdout.write("\nP5_CHILD_RC=%d\n" % _rc)
sys.stdout.flush()
raise SystemExit(_rc)
'''


# --------------------------------------------------------------------------- #
# Base case.                                                                    #
# --------------------------------------------------------------------------- #


class CrashReclaimTestCase(unittest.TestCase):
    """Per-test isolation plus a child registry.

    Every test gets its own `COWORK_SESSIONS_ROOT` and its own project
    directory, so no fixture can touch the real home dir, write inside the
    worktree, or observe another test's lease. The module owner context is
    process-global by design, so it is restored VERBATIM after every test. And
    every child this case spawns is registered: cleanup kills any survivor
    unconditionally and reaps it with a bounded wait, so a fixture that fails
    mid-race cannot leak a process.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cowork-owner-p5-root-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = tempfile.mkdtemp(prefix="cowork-owner-p5-proj-")
        self.addCleanup(shutil.rmtree, self.project, True)
        patcher = mock.patch.dict(os.environ,
                                  {"COWORK_SESSIONS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spath = os.path.join(self.project, ".cowork", "session.json")
        self.session_uuid = str(uuid.uuid4())
        self._children = []
        self.addCleanup(self._kill_every_child)
        prior = dict(cowork._OWNER_CONTEXT)
        self.addCleanup(cowork._restore_owner_context, prior)

    # -- child processes ---------------------------------------------------- #

    def _kill_every_child(self):
        for proc in self._children:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:  # pragma: no cover - already reaped
                    pass
            try:
                proc.wait(timeout=CHILD_REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass
            if proc.stdout is not None and not proc.stdout.closed:
                try:
                    proc.stdout.close()
                except OSError:  # pragma: no cover - defensive
                    pass

    def _spawn(self, program, argv, env=None):
        proc = subprocess.Popen(
            [sys.executable, "-c", program] + [str(a) for a in argv],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=env if env is not None else _child_env(self.root))
        self._children.append(proc)
        return proc

    def _await_gate(self, path, proc=None):
        """Wait for a child to publish its gate file, bounded.

        The ordering primitive every fixture here uses INSTEAD of timing: the
        file's existence is a durable fact about how far the child got, so no
        assertion in this module ever depends on a sleep being long enough."""
        deadline = time.time() + CHILD_READY_TIMEOUT_S
        while time.time() < deadline:
            if os.path.exists(path):
                with open(path, "r") as fh:
                    return json.load(fh)
            if proc is not None and proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                self.fail("child exited (rc=%s) before publishing %s:\n%s"
                          % (proc.returncode, path, out))
            time.sleep(0.01)
        self.fail("child never published %s within %ss"
                  % (path, CHILD_READY_TIMEOUT_S))

    def _reap(self, proc):
        try:
            out, _err = proc.communicate(timeout=CHILD_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.communicate(timeout=CHILD_REAP_TIMEOUT_S)
            self.fail("child did not exit within %ss" % CHILD_REAP_TIMEOUT_S)
        return proc.returncode, out or ""

    def spawn_light_owner(self, session_uuid, mode="plain", fabricate=""):
        """A real child that takes the lease for `session_uuid` and stays
        alive. Returns `(proc, published_gate_payload)`."""
        ready = os.path.join(self.root, "ready-%s" % uuid.uuid4())
        proc = self._spawn(_LIGHT_OWNER_CHILD,
                           [session_uuid, ready, mode, fabricate])
        return proc, self._await_gate(ready, proc)

    def spawn_flow_child(self, park=None, fabricate="", extra=("--new",),
                         spath=None):
        """A real `cowork.run_flow` child that reaches the scout turn and
        (when `park` is given) holds there until the gate file appears."""
        ready = os.path.join(self.root, "ready-%s" % uuid.uuid4())
        env = _child_env(self.root)
        if fabricate:
            env["COWORK_P5_FAKE_PID_START"] = fabricate
        proc = self._spawn(
            _FLOW_CHILD,
            [spath or self.spath, ready, park or ""] + list(extra), env=env)
        return proc, self._await_gate(ready, proc)

    # -- an in-process owned flow (the production restart path) ------------- #

    @staticmethod
    def _approving_scout(config, context, selected, io_out=None,
                         on_outcome=None, **kwargs):
        if on_outcome is not None:
            on_outcome("approved", None)
        return 0

    def run_owned_flow(self, extra=(), spath=None, scout=None):
        out = io.StringIO()
        args = cowork.build_parser().parse_args(
            ["--team", "scout,scout-reviewer", "--config", "scout=claude", "--context",
             "goal", "--session-file", spath or self.spath]
            + [a for a in extra if a != "--new"])
        rc = cowork.run_flow(
            args, io_out=out,
            which=lambda c: "/bin/" + c,
            run_scout_fn=scout if scout is not None else self._approving_scout)
        return rc, out.getvalue()

    # -- lease seeding ------------------------------------------------------ #

    def claimant(self, session_uuid=None, pid=None, pid_start_at=None):
        record = owner.owner_identity(session_uuid or self.session_uuid,
                                      "run_flow", self.project, self.spath)
        if pid is not None:
            record["pid"] = pid
            record["pid_start_at"] = pid_start_at
            record["pid_start_source"] = ("ps_lstart" if pid_start_at
                                          else "unavailable")
        return record

    def seed_dead_owner(self, session_uuid, age_seconds=7200):
        """A crashed owner: a pid that has genuinely exited, and a heartbeat
        old enough that the REAL clock is already past the deadline."""
        stale = _utcnow() - datetime.timedelta(seconds=age_seconds)
        return owner.acquire_owner_lease(
            session_uuid,
            self.claimant(session_uuid, pid=_dead_pid(),
                          pid_start_at=FABRICATED_PID_START),
            now=stale)

    # -- durable-artifact digests ------------------------------------------ #

    def digest_tree(self):
        """An exact per-path sha256 map of the sandboxed assets home and the
        project-local anchor directory, so a fixture can assert an ALLOWLIST of
        what may change rather than a prefix match."""
        out = {}
        for base in (self.root, os.path.join(self.project, ".cowork")):
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    full = os.path.join(dirpath, name)
                    try:
                        out[os.path.relpath(full, base)] = _sha256_file(full)
                    except OSError:  # pragma: no cover - a vanished temp file
                        continue
        return out

    def trace_events(self, session_uuid):
        path = trace_store.trace_path_for(session_uuid)
        if not os.path.exists(path):
            return []
        events = []
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:  # pragma: no cover - a torn tail
                    continue
        return events


# --------------------------------------------------------------------------- #
# F2 -- a real separate-process clean exit, and the restart after it.           #
# --------------------------------------------------------------------------- #


class CleanRestartTests(CrashReclaimTestCase):
    """F2. The in-process halves of this fixture are already carried by
    `test_m55_owner_gate.py::LifecycleTests::
    test_a_restart_after_a_clean_exit_acquires_the_next_epoch` (run-level) and
    by `test_m55_owner_store.py` (store-level). What P5 adds is the REAL
    separate-process clean exit: the predecessor is a different OS process that
    really ran and really returned."""

    def test_f2_a_real_child_clean_exit_releases_and_the_next_run_takes_epoch_two(self):
        proc, gate = self.spawn_flow_child(park=None)
        rc, out = self._reap(proc)
        self.assertEqual(rc, 0, out)
        self.assertIn("P5_CHILD_RC=0", out)

        session_uuid = gate["session_uuid"]
        released = owner.read_owner_lease(session_uuid)
        self.assertEqual(released["state"], "released")
        self.assertEqual(released["epoch"], 1)
        self.assertEqual(released["terminal_reason"], "normal_exit")

        # The restart is a REAL `run_flow` and goes through the ORDINARY
        # acquire path: no takeover mode is ever selected, and `take_over` is a
        # raise-on-call double for the whole run.
        select_spy = _Recorder(owner.select_takeover_mode)
        with mock.patch.object(owner, "take_over", _Raises("take_over")), \
                mock.patch.object(owner, "select_takeover_mode", select_spy):
            restart_rc, _restart_out = self.run_owned_flow()
        self.assertEqual(restart_rc, 0)
        self.assertEqual(select_spy.calls, [])

        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["epoch"], 2)
        self.assertIsNone(record["predecessor"])
        self.assertEqual(record["state"], "released")


# --------------------------------------------------------------------------- #
# F3 -- a REAL SIGKILL mid-turn.                                                #
# --------------------------------------------------------------------------- #


class CrashedOwnerTests(CrashReclaimTestCase):
    """F3. A real `run_flow` child is SIGKILLed while it is inside its scout
    turn -- no handler runs, no sidecar is written, nothing is released. The
    durable record is exactly what a real crash leaves behind."""

    def _crash_a_real_owner(self):
        park = os.path.join(self.root, "release-gate")
        proc, gate = self.spawn_flow_child(park=park)
        proc.kill()
        proc.wait(timeout=CHILD_REAP_TIMEOUT_S)
        session_uuid = gate["session_uuid"]
        # A SIGKILL cannot run a handler, so no terminal sidecar exists: this
        # is the crash case, not the signalled case.
        self.assertIsNone(owner.read_terminal_mark(session_uuid,
                                                   gate["owner_id"]))
        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["state"], "live")
        self.assertEqual(record["epoch"], 1)
        return session_uuid, gate, record

    def test_f3_a_real_sigkill_leaves_a_live_lease_until_the_deadline(self):
        session_uuid, _gate, _record = self._crash_a_real_owner()
        before_deadline = _utcnow()
        self.assertEqual(
            owner.classify_owner_lease(session_uuid, now=before_deadline),
            "live_owner")
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            owner.acquire_owner_lease(session_uuid,
                                      self.claimant(session_uuid),
                                      now=before_deadline)
        self.assertEqual(caught.exception.reason, "live_owner")

    def test_f3_after_the_deadline_take_over_selects_proved_dead_and_bumps_the_epoch(self):
        session_uuid, gate, record = self._crash_a_real_owner()
        after = _expired_instant()

        self.assertEqual(owner.classify_owner_lease(session_uuid, now=after),
                         "stale_dead_owner")
        # A crashed owner is NEVER implicitly reclaimed.
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            owner.acquire_owner_lease(session_uuid,
                                      self.claimant(session_uuid), now=after)
        self.assertEqual(caught.exception.reason, "stale_dead_owner")

        # Asserted EXPLICITLY rather than inferred from the takeover working.
        self.assertEqual(owner.select_takeover_mode(session_uuid, now=after),
                         "proved_dead")

        successor = owner.take_over(session_uuid, self.claimant(session_uuid),
                                    "proved_dead", now=after)
        self.assertEqual(successor["epoch"], record["epoch"] + 1)
        self.assertEqual(successor["predecessor"]["owner_id"],
                         gate["owner_id"])
        self.assertEqual(successor["predecessor"]["evidence"], "pid_absent")


# --------------------------------------------------------------------------- #
# F4 -- SIGTERM delivered to a predecessor UNDER a concurrent takeover.         #
# --------------------------------------------------------------------------- #


class SigtermUnderTakeoverTests(CrashReclaimTestCase):
    """F4. `test_m55_owner_store.py::TerminalMarkTests` already carries the
    terminal-mark independence property at store level. What P5 adds is the
    full RUN-LEVEL consequence set, under a real concurrent takeover: the
    sidecar, the aborted PhaseState, the `run.external_kill` trace event, exit
    143, AND the successor's byte identity -- all from one real signal."""

    def test_f4_the_predecessor_marks_terminal_and_the_successor_lease_is_byte_identical(self):
        park = os.path.join(self.root, "release-gate")
        proc, gate = self.spawn_flow_child(park=park,
                                           fabricate=FABRICATED_PID_START)
        session_uuid = gate["session_uuid"]

        # The successor takes the session over while the predecessor is still
        # running. The predecessor's lease records a fabricated `pid_start_at`,
        # so the classifier's PID-REUSE limb proves the recorded process is
        # gone without anything having to signal the live one -- which is
        # precisely the state a takeover races a SIGTERM in.
        after = _expired_instant()
        self.assertEqual(owner.classify_owner_lease(session_uuid, now=after),
                         "stale_dead_owner")
        successor = owner.take_over(session_uuid, self.claimant(session_uuid),
                                    "proved_dead", now=after)
        self.assertEqual(successor["epoch"], gate["epoch"] + 1)
        lease_before_signal = _read_raw_lease_bytes(session_uuid)

        # NOW the predecessor is really signalled.
        os.kill(gate["pid"], signal.SIGTERM)
        rc, out = self._reap(proc)

        # (a) exit status 128 + SIGTERM.
        self.assertEqual(rc, 128 + int(signal.SIGTERM), out)

        # (b) the per-owner terminal sidecar, with `owner_matched` FALSE -- the
        # advisory `_require_owner` on the unlocked path recorded that this
        # process no longer held the lease and REPORTED it, rather than
        # suppressing the mark or any other handler effect.
        mark = owner.read_terminal_mark(session_uuid, gate["owner_id"])
        self.assertIsNotNone(mark)
        self.assertEqual(mark["terminal_reason"], "sigterm")
        self.assertIs(mark["owner_matched"], False)
        self.assertEqual(mark["owner_id"], gate["owner_id"])
        self.assertEqual(mark["epoch"], gate["epoch"])

        # (c) the trace event and (d) the durable aborted PhaseState.
        events = self.trace_events(session_uuid)
        kills = [e for e in events if e.get("event") == "run.external_kill"]
        self.assertEqual(len(kills), 1, [e.get("event") for e in events])
        work_id = kills[0].get("role_work_id")
        self.assertTrue(work_id, kills)
        phase_state = state_store.current_phase_state(session_uuid, work_id)
        self.assertEqual((phase_state or {}).get("state"), "aborted")

        # (e) the property all of that has to be safe for: the SUCCESSOR's
        # lease is byte-identical across the predecessor's whole death.
        self.assertEqual(_read_raw_lease_bytes(session_uuid),
                         lease_before_signal)
        self.assertEqual(owner.classify_owner_lease(session_uuid),
                         "live_owner")

        # (f) and an ordinary restart after the successor releases takes the
        # NEXT epoch, with no takeover involved.
        owner.release_owner_lease(session_uuid, successor["owner_id"],
                                  successor["epoch"], "normal_exit")
        with mock.patch.object(owner, "take_over", _Raises("take_over")):
            third = owner.acquire_owner_lease(session_uuid,
                                              self.claimant(session_uuid))
        self.assertEqual(third["epoch"], successor["epoch"] + 1)
        self.assertIsNone(third["predecessor"])


# --------------------------------------------------------------------------- #
# F5 -- a pid re-bound to a genuinely NEW, still-running process.               #
# --------------------------------------------------------------------------- #


class PidReuseTests(CrashReclaimTestCase):
    """F5. `test_m55_owner_store.py` carries pid reuse against a synthetic
    record. What P5 adds is a pid bound to a NEWLY SPAWNED, still-running
    process: the innocent inheritor of a recycled pid is a real thing that can
    really be signalled, and the assertion here is that it never is."""

    def test_f5_a_live_reused_pid_classifies_stale_dead_owner_and_is_never_signalled(self):
        proc, gate = self.spawn_light_owner(self.session_uuid,
                                            fabricate=FABRICATED_PID_START)
        self.assertIsNone(proc.poll(), "the victim must be genuinely alive")
        self.assertEqual(gate["pid_start_at"], FABRICATED_PID_START)

        after = _expired_instant()
        verdict = owner.classify_owner_lease(self.session_uuid, now=after)
        self.assertEqual(verdict, "stale_dead_owner")
        self.assertNotEqual(verdict, "live_owner")

        record = owner.read_owner_lease(self.session_uuid)
        self.assertEqual(record["pid"], gate["pid"])

        signalled = []

        def recording_kill(pid, sig):
            signalled.append((pid, sig))
            raise AssertionError(
                "a live process inheriting a recycled pid was signalled: %r"
                % ((pid, sig),))

        with mock.patch.object(os, "kill", recording_kill):
            gone = owner._terminate_prior_owner(record)

        self.assertIs(gone, False)
        self.assertEqual(signalled, [])
        self.assertIsNone(proc.poll(),
                          "the innocent process must still be running")


# --------------------------------------------------------------------------- #
# F11 -- a real takeover preserves the session it inherits.                     #
# --------------------------------------------------------------------------- #


class TakeoverPreservationTests(CrashReclaimTestCase):
    """F11, entirely new. `take_over`'s docstring promises that pending turns,
    candidate bindings, work units, manifests, pause leases, phase state, graph
    revisions, the trace, the evaluation queue and `provider-bindings/` records
    are ALL left untouched. This is that promise measured: ten artifact classes
    as an exact allowlist, each sha256-identical across a real takeover, plus a
    whole-tree digest map that may differ at exactly two paths.

    Each artifact's PATH comes from its own production helper -- never a
    literal -- so a helper that moved would fail here rather than silently
    measure a path nothing writes.

    DISCLOSED: a fake-role run does not exercise all ten classes, so the
    classes it does not produce are seeded at those production paths directly.
    The property under test is the takeover's BLAST RADIUS, and for that a
    seeded file at the production path is exactly as real as a produced one.
    """

    WORK_ID = "p5-work-1"
    LEASE_ID = "p5-lease-1"
    BINDING = ("claude", "p5-provider-session")

    def test_f11_ten_artifact_classes_are_sha256_identical_across_a_real_takeover(self):
        rc, out = self.run_owned_flow(["--new"])
        self.assertEqual(rc, 0, out)
        session_uuid = state_store.get_session_uuid(
            state_store.load(self.spath))

        paths = self._populate_ten_artifact_classes(session_uuid)
        self.assertEqual(len(paths), 10)
        for label, path in sorted(paths.items()):
            self.assertTrue(os.path.exists(path),
                            "%s was never written at %s" % (label, path))
        before_each = {label: _sha256_file(path)
                       for label, path in paths.items()}

        prior = self.seed_dead_owner(session_uuid)
        before_tree = self.digest_tree()

        successor = owner.take_over(session_uuid, self.claimant(session_uuid),
                                    "proved_dead")
        self.assertEqual(successor["epoch"], prior["epoch"] + 1)

        after_each = {label: _sha256_file(path)
                      for label, path in paths.items()}
        self.assertEqual(after_each, before_each)

        after_tree = self.digest_tree()
        changed = {p for p in set(before_tree) | set(after_tree)
                   if before_tree.get(p) != after_tree.get(p)}
        allowed = {
            os.path.join(session_uuid, "owner", "lease.json"),
            os.path.join(session_uuid, "owner", "history.jsonl"),
        }
        self.assertEqual(changed, allowed)

    def _populate_ten_artifact_classes(self, session_uuid):
        controller, provider_session_id = self.BINDING
        paths = {
            "pending_turn": state_store.pending_turn_before_pause_path_for(
                session_uuid, "scout"),
            "controller_transition":
                state_store.controller_transition_path_for(session_uuid),
            "work_unit": state_store.work_unit_history_path_for(
                session_uuid, self.WORK_ID),
            "manifest": state_store.manifest_path_for(session_uuid,
                                                      self.WORK_ID),
            "pause_lease": state_store.pause_lease_path_for(session_uuid,
                                                            self.LEASE_ID),
            "phase_state": state_store.phase_state_history_path_for(
                session_uuid, self.WORK_ID),
            "graph_revision": state_store.graph_revisions_path_for(
                session_uuid),
            "trace": trace_store.trace_path_for(session_uuid),
            "evaluation_queue": state_store.evaluation_queue_path_for(
                session_uuid),
            "provider_binding":
                state_store.provider_session_binding_path_for(
                    controller, provider_session_id),
        }
        for label, path in paths.items():
            if os.path.exists(path):
                continue
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(json.dumps({"p5_fixture": label,
                                     "session_uuid": session_uuid},
                                    sort_keys=True) + "\n")
        return paths


# --------------------------------------------------------------------------- #
# F12 -- a takeover that ABORTS because the prior owner survives.               #
# --------------------------------------------------------------------------- #


class TakeoverAbortTests(CrashReclaimTestCase):
    """F12. `test_m55_owner_store.py::
    test_terminate_prior_aborts_and_leaves_the_lease_byte_identical` carries
    this at store level against a synthetic victim. What P5 adds is a REAL live
    process that really survives BOTH escalation steps.

    `TAKEOVER_TERM_GRACE_S`/`TAKEOVER_KILL_GRACE_S` are patched to 1.0/0.5.
    Both are read as module globals inside `_terminate_prior_owner`
    (cowork_owner.py:1156/:1164), so patching them is a CLOCK INJECTION, not an
    assertion weakening: the asserted property -- the takeover aborts and the
    lease is byte-identical -- is unchanged, while 15s of unavoidable real-time
    polling becomes 1.5s.
    """

    def test_f12_a_prior_owner_that_survives_term_and_kill_aborts_the_takeover(self):
        proc, gate = self.spawn_light_owner(self.session_uuid,
                                            mode="ignore_term")
        self.assertIsNone(proc.poll())
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "live_owner")
        before = _read_raw_lease_bytes(self.session_uuid)

        signalled = []
        real_kill = os.kill

        def recording_kill(pid, sig):
            signalled.append((pid, sig))
            if pid == gate["pid"]:
                # SWALLOWED on purpose: the victim must genuinely survive both
                # escalation steps, which is the state this fixture exists to
                # drive `_terminate_prior_owner` through. Every other pid is
                # signalled for real.
                return None
            return real_kill(pid, sig)

        with mock.patch.object(owner, "TAKEOVER_TERM_GRACE_S", 1.0), \
                mock.patch.object(owner, "TAKEOVER_KILL_GRACE_S", 0.5), \
                mock.patch.object(os, "kill", recording_kill):
            with self.assertRaises(owner.OwnerLeaseConflict) as caught:
                owner.take_over(self.session_uuid,
                                self.claimant(self.session_uuid),
                                "terminate_prior")

        self.assertEqual(caught.exception.reason, "live_owner")
        self.assertIn((gate["pid"], signal.SIGTERM), signalled)
        self.assertIn((gate["pid"], signal.SIGKILL), signalled)
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)
        self.assertIsNone(proc.poll(), "the victim must still be alive")


# --------------------------------------------------------------------------- #
# F13 / gate G8 -- the SIGTERM-versus-takeover race, at depth.                   #
# --------------------------------------------------------------------------- #


class SigtermTakeoverRaceTests(CrashReclaimTestCase):
    """F13 / G8, entirely new.

    TWO MECHANISMS, both DISCLOSED in the test's own output:

      - 50 RANDOMIZED INTERLEAVINGS, each a real OS process receiving a real
        SIGTERM, racing an in-process successor acquisition. BOTH the 0-40 ms
        delay AND which side moves first are randomized, and the test asserts
        it really drove both orderings -- randomizing only the delay after the
        signal would have made every iteration a repetition of the mark-first
        ordering under jitter. The child is LIGHT: it imports only
        `cowork_owner`, takes the lease, and on SIGTERM calls the real
        `mark_owner_terminal_unlocked` with the production argument set,
        `owner_matched` included and computed rather than hardcoded. What it
        stands in for -- `run_flow`'s surrounding frame, i.e. the aborted
        PhaseState write, the trace event and the `SystemExit` unwinding
        through run_flow's own handlers -- is covered by the two orders below
        and by F4.

      - BOTH DETERMINISTIC WRITE ORDERS, driven through the FULL production
        `_handle_external_kill` in a real `run_flow` child.

    WHICH SIDE WINS is an observation, never an assertion: the test reports the
    route split and asserts only the depth (>= 50), the orderings it drove, and
    the four properties below. Asserting a particular winner would make a
    correct candidate fail on timing.

    Per interleaving, four properties: the successor's `owner/lease.json` bytes
    are unchanged by the predecessor's SIGTERM mark; `classify_owner_lease`
    returns `live_owner`; a THIRD acquirer is refused with
    `OwnerLeaseConflict('live_owner')`; and the multiset of every epoch ever
    written for that session contains no duplicate.
    """

    def test_g8_fifty_randomized_interleavings_plus_both_deterministic_orders(self):
        rng = random.Random(G8_SEED)
        interleavings = 0
        via_takeover = 0
        via_acquire = 0
        signal_first = 0
        successor_first = 0

        for index in range(G8_MIN_INTERLEAVINGS):
            session_uuid = str(uuid.uuid4())
            proc, gate = self.spawn_light_owner(
                session_uuid, mode="mark_on_term",
                fabricate=FABRICATED_PID_START)
            epochs = [gate["epoch"]]

            # BOTH the delay AND WHICH SIDE MOVES FIRST are randomized.
            # Randomizing only the delay after the signal would make every
            # iteration a repetition of the mark-first ordering under jitter --
            # fifty runs of one interleaving rather than fifty interleavings --
            # because the successor could then never start before the signal.
            delay = rng.uniform(0.0, G8_MAX_RACE_DELAY_S)
            signal_leads = rng.random() < 0.5
            if signal_leads:
                signal_first += 1
                os.kill(gate["pid"], signal.SIGTERM)
                time.sleep(delay)
                successor, route = self._acquire_successor(session_uuid)
            else:
                successor_first += 1
                time.sleep(delay)
                successor, route = self._acquire_successor(session_uuid)
                os.kill(gate["pid"], signal.SIGTERM)

            epochs.append(successor["epoch"])
            if route == "take_over":
                via_takeover += 1
            else:
                via_acquire += 1

            lease_after_successor = _read_raw_lease_bytes(session_uuid)
            rc, out = self._reap(proc)
            self.assertEqual(rc, 128 + int(signal.SIGTERM), out)

            with self.subTest(interleaving=index, route=route,
                              signal_leads=signal_leads):
                # (1) the predecessor's terminal mark never touches the
                #     successor's record -- even when it lands afterwards.
                self.assertEqual(_read_raw_lease_bytes(session_uuid),
                                 lease_after_successor)
                # (2) the successor is live.
                self.assertEqual(owner.classify_owner_lease(session_uuid),
                                 "live_owner")
                # (3) a third acquirer is refused, typed.
                with self.assertRaises(owner.OwnerLeaseConflict) as caught:
                    owner.acquire_owner_lease(session_uuid,
                                              self.claimant(session_uuid))
                self.assertEqual(caught.exception.reason, "live_owner")
                # (4) `epoch` never repeats within a session.
                self.assertEqual(len(set(epochs)), len(epochs), epochs)
                self.assertEqual(epochs, [1, 2])
            interleavings += 1

        order_mark_first = self._deterministic_order_mark_first()
        order_takeover_first = self._deterministic_order_takeover_first()

        # The counts and the mechanisms are REPORTED by the test itself, so a
        # reduced run or a degenerate distribution is visible in the output
        # rather than only in a claim. Two different splits are reported and
        # they mean different things: `drove` is which side this test MOVED
        # first (fully under the seeded rng's control, and asserted below), and
        # `routes` is which side actually WON the race in the store (an
        # observation, never asserted -- asserting it would make a correct
        # candidate fail on timing).
        sys.stderr.write(
            "\n[G8] interleavings=%d (light child + real SIGTERM + in-process "
            "successor, randomized 0-%dms delay AND randomized order; drove: "
            "signal_first=%d successor_first=%d; observed successor routes: "
            "take_over=%d acquire=%d) deterministic_orders=2 (full production "
            "_handle_external_kill in a real run_flow child: mark_first=%s "
            "takeover_first=%s)\n"
            % (interleavings, int(G8_MAX_RACE_DELAY_S * 1000), signal_first,
               successor_first, via_takeover, via_acquire, order_mark_first,
               order_takeover_first))

        self.assertGreaterEqual(interleavings, G8_MIN_INTERLEAVINGS)
        # The randomized arm really did drive BOTH orderings, so it is fifty
        # interleavings rather than fifty repetitions of one.
        self.assertGreater(signal_first, 0)
        self.assertGreater(successor_first, 0)
        self.assertEqual(signal_first + successor_first, interleavings)
        self.assertEqual(order_mark_first, "ok")
        self.assertEqual(order_takeover_first, "ok")

    # -- the successor, however the race fell out --------------------------- #

    def _acquire_successor(self, session_uuid):
        """Take the session over, or acquire it, depending on WHICH SIDE OF
        THE RACE WON -- and report which.

        Both routes are correct, and which one is available is exactly the
        thing the race decides. If the predecessor's MATCHING sidecar landed
        first it is folded under the lock and the lease reads `unowned`, so an
        ordinary acquire is the right call and an explicit `proved_dead`
        takeover must refuse. If it has not landed yet, the lease is still
        `live` with a demonstrably-gone recorded process, so `proved_dead` is
        the right call. Collapsing the two would hide the race rather than
        measure it.
        """
        after = _expired_instant()
        try:
            record = owner.take_over(session_uuid,
                                     self.claimant(session_uuid),
                                     "proved_dead", now=after)
            return record, "take_over"
        except owner.OwnerLeaseConflict as exc:
            self.assertEqual(
                exc.reason, "unowned",
                "the only refusal a folded predecessor mark can produce is "
                "`unowned`")
            return (owner.acquire_owner_lease(session_uuid,
                                              self.claimant(session_uuid)),
                    "acquire")

    # -- the two deterministic orders, through the real handler -------------- #

    def _deterministic_order_mark_first(self):
        """ORDER A: the predecessor's SIGTERM mark is fully committed BEFORE
        the successor acquires. The matching sidecar folds under the lock, so
        the lease reads `unowned`, an ordinary acquire takes epoch 2, and the
        now-superseded sidecar is swept."""
        spath = os.path.join(self.project, ".cowork", "order-a.json")
        park = os.path.join(self.root, "order-a-gate")
        proc, gate = self.spawn_flow_child(park=park, spath=spath,
                                           fabricate=FABRICATED_PID_START)
        session_uuid = gate["session_uuid"]
        os.kill(gate["pid"], signal.SIGTERM)
        rc, out = self._reap(proc)
        self.assertEqual(rc, 128 + int(signal.SIGTERM), out)

        mark = owner.read_terminal_mark(session_uuid, gate["owner_id"])
        self.assertIsNotNone(mark)
        self.assertEqual(mark["terminal_reason"], "sigterm")
        self.assertEqual(owner.classify_owner_lease(session_uuid), "unowned")

        successor = owner.acquire_owner_lease(session_uuid,
                                              self.claimant(session_uuid))
        self.assertEqual(successor["epoch"], gate["epoch"] + 1)
        self.assertEqual(owner.classify_owner_lease(session_uuid),
                         "live_owner")
        # Rule W5: the predecessor's sidecar is swept once a successor record
        # exists, so it cannot accumulate and cannot be re-folded.
        self.assertIsNone(owner.read_terminal_mark(session_uuid,
                                                   gate["owner_id"]))
        return "ok"

    def _deterministic_order_takeover_first(self):
        """ORDER B: the successor commits BEFORE the predecessor is signalled.
        The predecessor's mark then names an owner the lease no longer has, so
        rule W4 ignores it entirely and the successor's record is
        byte-identical across the signal."""
        spath = os.path.join(self.project, ".cowork", "order-b.json")
        park = os.path.join(self.root, "order-b-gate")
        proc, gate = self.spawn_flow_child(park=park, spath=spath,
                                           fabricate=FABRICATED_PID_START)
        session_uuid = gate["session_uuid"]

        after = _expired_instant()
        successor = owner.take_over(session_uuid, self.claimant(session_uuid),
                                    "proved_dead", now=after)
        self.assertEqual(successor["epoch"], gate["epoch"] + 1)
        before_signal = _read_raw_lease_bytes(session_uuid)

        os.kill(gate["pid"], signal.SIGTERM)
        rc, out = self._reap(proc)
        self.assertEqual(rc, 128 + int(signal.SIGTERM), out)

        mark = owner.read_terminal_mark(session_uuid, gate["owner_id"])
        self.assertIsNotNone(mark)
        self.assertEqual(mark["owner_id"], gate["owner_id"])
        self.assertNotEqual(mark["owner_id"], successor["owner_id"])
        self.assertEqual(_read_raw_lease_bytes(session_uuid), before_signal)
        self.assertEqual(owner.classify_owner_lease(session_uuid),
                         "live_owner")
        return "ok"


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
