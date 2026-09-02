#!/usr/bin/env python3
"""Focused suite for M5 global P4R v1: durable stranded-claim classification
(M5F-CLAIM-CRASH-1 / A-C5-CRASH-STRAND), on the exact signed base
`ae8b54ceeab5b611eba72c31a5dbe349ee57513e`.

The proven criterion-5 defect: a checkpoint claim left behind by a claimant
that CRASHED is byte-for-byte indistinguishable, on disk, from a claim whose
executor is still running -- `reconstruct_checkpoint_state` reports
`"claimed"` for both -- so a stranded checkpoint could only ever be
classified as ongoing work or as no-evidence silence, never as the literal
`process_crash`/`hung_descendant` it actually is.

What is proven here, from DURABLE ARTIFACTS ALONE (no terminal output, no
live process handle, no in-memory state):

  - one REAL crashed claimant (a subprocess that claims and then SIGKILLs
    itself, exit status -9) and one REAL live claimant (a subprocess that
    claims and keeps running) are distinguishable at a SINGLE instant while
    holding IDENTICAL lease lengths -- the verdict turns on whether that
    claimant's own lease actually lapsed, never on one side being handed a
    longer lease than the other;
  - the strand classifies `process_crash`/`hung_descendant`, and NEVER
    `productive_model_work`, `provider_wait`, or `no_evidence_silence`;
  - the live claim never classifies as either crash class;
  - EQUAL-LEASE NEGATIVE CONTROLS (F1): a healthy live claimant at -- and
    past -- its raw command timeout, but still inside its total lease, is
    never `process_crash`; the bounded grace that saves it is derived from
    this module's own worker-deadline policy constants; and a request that
    declares no `timeout_s` at all gets NO finite expiry, so a healthy
    unbounded live claimant can never be falsely indicted by elapsed time;
  - classification has no terminal-output dependency: it is re-derived
    identically in a fresh process whose stdin/stdout/stderr are /dev/null,
    with no PTY anywhere;
  - kernel-exclusive `O_CREAT | O_EXCL` once-only claiming is unchanged,
    including duplicate refusal for the SAME `executor_identity`, and the
    loser writes nothing;
  - mechanical path/AST/byte-identity scope gates: only the two authorized
    production regions changed, `reconstruct_checkpoint_state` is byte
    -identical to the signed base, every excluded production path and every
    pre-existing test file is byte-identical to the signed base, the dead
    `CHECKPOINT_CLAIM_ABANDONED` vocabulary is never written into a claim,
    and `cowork_activity.py` is never imported by the production module.

Run standalone:

    python3 -m unittest scripts/test_m5_claim_crash_reclaim.py -v
"""

import ast
import datetime
import hashlib
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_REPO_ROOT = os.path.dirname(_HERE)

import cowork_activity as activity  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

# The exact signed base this package's frozen brief is bound to.
BASE_SHA = "ae8b54ceeab5b611eba72c31a5dbe349ee57513e"

# The exact, frozen three-path write authority this package has (the bounded
# correction narrows what it actually uses to the two paths below).
ALLOWED_CHANGED_PATHS = frozenset({
    "scripts/cowork_verification.py",
    "scripts/cowork_state.py",
    "scripts/test_m5_claim_crash_reclaim.py",
})

# Production paths this package is explicitly forbidden from touching.
EXCLUDED_PATHS = (
    "scripts/cowork_activity.py",
    "scripts/cowork.py",
    "scripts/cowork_control_plane.py",
    "scripts/cowork_handoff.py",
    "scripts/cowork_measure.py",
    "scripts/cowork_verification_worker.py",
    "scripts/cowork_verification_evidence.py",
    "scripts/cowork_action_policy.py",
    "scripts/cowork_action_guard.py",
    "scripts/cowork_ledger.py",
)

# The exact production symbols/regions this package may add or change.
AUTHORIZED_ADDED_SYMBOLS = frozenset({
    "CHECKPOINT_CLAIM_LEASE_GRACE_S",
    "classify_checkpoint_claim_liveness",
})
AUTHORIZED_CHANGED_SYMBOLS = frozenset({"claim_checkpoint"})

# The criterion-5 classes a stranded claim must land in, and the three it may
# never land in. Cross-checked against the REAL closed taxonomy in
# `cowork_activity.ACTIVITY_CLASSES` -- which the production module itself
# never imports (proven in `ProductionScopeConfinementTests`).
CRASH_CLASSES = ("process_crash", "hung_descendant")
FORBIDDEN_FOR_A_STRAND = ("productive_model_work", "provider_wait",
                          "no_evidence_silence")

GRACE_S = verification.CHECKPOINT_CLAIM_LEASE_GRACE_S


def _git_show_bytes(rev, rel_path):
    return subprocess.run(
        ["git", "show", "%s:%s" % (rev, rel_path)],
        cwd=_REPO_ROOT, capture_output=True, check=True).stdout


def _git_show_text(rev, rel_path):
    return _git_show_bytes(rev, rel_path).decode("utf-8")


def _read_local_bytes(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), "rb") as fh:
        return fh.read()


def _working_tree_changed_paths():
    """Every path the LIVE working tree changes relative to the signed base,
    including untracked files -- this package's own diff is uncommitted by
    contract (the brief forbids committing), so a commit-pinned gate could
    not see it at all."""
    tracked = subprocess.run(
        ["git", "diff", "--name-only", BASE_SHA],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True).stdout
    return {p.strip() for p in (tracked + untracked).splitlines() if p.strip()}


def _top_level_index(source):
    """`(named, unnamed)` for one module's top-level statements.

    `named` maps a top-level symbol name (function, class, or single-target
    module-level assignment) to its exact source text; `unnamed` is the
    ordered list of `(kind, text)` for every other top-level statement
    (imports, `try` blocks, `if __name__` guards, ...). Splitting the two
    lets an ADDITIVE change be checked precisely: a new symbol appears in
    `named` without disturbing `unnamed` at all, while any edit to an
    existing region shows up as a changed entry rather than being masked by
    a positional shift."""
    lines = source.splitlines(keepends=True)
    named, unnamed = {}, []
    for node in ast.parse(source).body:
        text = "".join(lines[node.lineno - 1:node.end_lineno])
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign) and len(
                node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name is None and isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name):
            name = node.target.id
        if name is None:
            unnamed.append((type(node).__name__, text))
        else:
            named[name] = text
    return named, unnamed


def _non_docstring_strings(source):
    """Every string literal in one module EXCEPT docstrings -- prose in a
    docstring may freely NAME a module without importing or referencing it,
    but a live string constant carrying that name (an `__import__` argument,
    an `importlib` target, a copied taxonomy tuple) is a real coupling."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(
                        body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings]


def _function_node(source, name):
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == name):
            return node
    raise AssertionError("no top-level function %r in source" % (name,))


def _parse_instant(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


# --------------------------------------------------------------------------- #
# Real claimant subprocesses: a genuine crash and a genuine live executor.     #
# --------------------------------------------------------------------------- #

# Persists a real CheckpointRequest, takes the real once-only claim through
# the real `claim_checkpoint`, announces its pid, then either SIGKILLs itself
# (an uncatchable, genuinely uncleaned crash -- no atexit, no finally, no
# chance to mark anything) or blocks forever as a live executor would.
# `timeout_s` of the literal string "none" persists a request with NO declared
# timeout at all: an unbounded command, exactly as `run_checkpoint` would run
# it (`subprocess.run(..., timeout=None)`).
_CLAIMANT_SRC = r'''
import os
import signal
import sys
import time

sys.path.insert(0, sys.argv[1])
import cowork_verification as verification

_scripts, session_uuid, checkpoint_id, identity, timeout_s, cwd, \
    ready_path, mode = sys.argv[1:9]

verification.build_and_persist_checkpoint_request(
    session_uuid, checkpoint_id, "W-claim-crash", "build",
    "candidate-digest-0", ["/bin/sh", "-c", "sleep 600"], cwd,
    verification.MUTATION_CLASS_READ_ONLY,
    timeout_s=(None if timeout_s == "none" else float(timeout_s)))
claimed, record = verification.claim_checkpoint(
    session_uuid, checkpoint_id, identity)
if not claimed:
    raise SystemExit("claim unexpectedly refused")
with open(ready_path, "w") as fh:
    fh.write(str(os.getpid()))
    fh.flush()
    os.fsync(fh.fileno())
if mode == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
while True:
    time.sleep(3600)
'''

# Re-derives one classification in a FRESH process with no terminal at all:
# stdin/stdout/stderr are /dev/null (never a PTY), and the verdict is handed
# back through a file, not through any stream a terminal could carry.
_HEADLESS_CLASSIFIER_SRC = r'''
import json
import sys

sys.path.insert(0, sys.argv[1])
import cowork_verification as verification

_scripts, session_uuid, checkpoint_id, now, out_path = sys.argv[1:6]
verdict = verification.classify_checkpoint_claim_liveness(
    session_uuid, checkpoint_id, now=(now or None))
with open(out_path, "w") as fh:
    json.dump({"classification": verdict}, fh)
'''


class _ArtifactFixture(unittest.TestCase):
    """An isolated `COWORK_SESSIONS_ROOT` plus helpers to spawn real
    claimant subprocesses and to author claim artifacts directly."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.workdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.workdir, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self.root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        self.checkpoint_id = "cp-" + uuid.uuid4().hex[:8]

    # -- helpers ---------------------------------------------------------- #

    def _child_env(self):
        env = dict(os.environ)
        env["COWORK_SESSIONS_ROOT"] = self.root
        return env

    def spawn_claimant(self, checkpoint_id, identity, timeout_s, mode):
        """Spawn a REAL claimant subprocess; return its `Popen` once the
        claim is durably on disk. `mode="crash"` kills itself with SIGKILL
        immediately after claiming; `mode="live"` stays running.
        `timeout_s=None` persists an unbounded request."""
        ready_path = os.path.join(self.workdir, "ready-%s" % checkpoint_id)
        proc = subprocess.Popen(
            [sys.executable, "-c", _CLAIMANT_SRC, _HERE, self.session_uuid,
             checkpoint_id, identity,
             "none" if timeout_s is None else str(timeout_s), self.workdir,
             ready_path, mode],
            env=self._child_env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.addCleanup(self._reap, proc)
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if os.path.exists(ready_path):
                return proc
            if proc.poll() is not None and not os.path.exists(ready_path):
                out, err = proc.communicate()
                raise AssertionError(
                    "claimant exited before claiming: rc=%r stderr=%s"
                    % (proc.returncode, err.decode("utf-8", "replace")))
            time.sleep(0.02)
        raise AssertionError("claimant never announced its claim")

    def _reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def equal_lease_pair(self, timeout_s=1.0, gap_s=3.0):
        """One REAL crashed claimant and one REAL live claimant holding
        EQUAL lease lengths, plus the single instant at which they are
        compared.

        The crashed claimant is spawned first and killed; `gap_s` of real
        time then passes; the live claimant claims its own checkpoint with
        the IDENTICAL `timeout_s`, so both leases are exactly
        `timeout_s + CHECKPOINT_CLAIM_LEASE_GRACE_S` long. The comparison
        instant is one second past the CRASHED claim's own deadline -- at
        which the live claim, being younger by `gap_s`, provably has not
        expired. Neither side is handed a longer lease than the other; the
        verdict can only turn on whose lease actually lapsed."""
        crashed_id = "cp-crashed-" + uuid.uuid4().hex[:8]
        live_id = "cp-live-" + uuid.uuid4().hex[:8]
        crashed = self.spawn_claimant(crashed_id, "executor-crashed",
                                      timeout_s, "crash")
        crashed.wait(timeout=30)
        self.assertEqual(crashed.returncode, -signal.SIGKILL,
                         "the crashed claimant must die by SIGKILL, proving "
                         "no cleanup path could have marked its claim")
        time.sleep(gap_s)
        live = self.spawn_claimant(live_id, "executor-live", timeout_s, "live")
        self.assertIsNone(live.poll(),
                          "the live claimant must still be running")
        crashed_claim = self.claim_artifact(crashed_id)
        live_claim = self.claim_artifact(live_id)
        # EQUAL LEASES: identical declared timeout and identical grace, so
        # the two leases are the same length to the second.
        self.assertEqual(crashed_claim["lease_timeout_s"],
                         live_claim["lease_timeout_s"])
        self.assertEqual(crashed_claim["lease_grace_s"],
                         live_claim["lease_grace_s"])
        crashed_deadline = _parse_instant(crashed_claim["lease_deadline_at"])
        live_deadline = _parse_instant(live_claim["lease_deadline_at"])
        self.assertEqual(
            (live_deadline - _parse_instant(live_claim["claimed_at"])),
            (crashed_deadline
             - _parse_instant(crashed_claim["claimed_at"])),
            "both claimants must hold leases of exactly equal length")
        at = crashed_deadline + datetime.timedelta(seconds=1)
        self.assertGreater(live_deadline, at,
                           "the live claimant's equal-length lease must not "
                           "have expired at the comparison instant")
        return crashed_id, live_id, _iso(at), crashed_claim, live_claim

    def claim_artifact(self, checkpoint_id):
        return state_store.read_json_tolerant(
            state_store.checkpoint_claim_path_for(self.session_uuid,
                                                  checkpoint_id))

    def classify(self, checkpoint_id, now=None):
        return verification.classify_checkpoint_claim_liveness(
            self.session_uuid, checkpoint_id, now=now)

    def classify_headless(self, checkpoint_id, now=None):
        """The SAME classification, re-derived in a fresh process with
        /dev/null for stdin, stdout, and stderr -- no terminal, no PTY, no
        inherited stream of any kind."""
        out_path = os.path.join(self.workdir, "verdict-%s.json"
                                % uuid.uuid4().hex)
        with open(os.devnull, "rb") as devnull_in, \
                open(os.devnull, "wb") as devnull_out, \
                open(os.devnull, "wb") as devnull_err:
            completed = subprocess.run(
                [sys.executable, "-c", _HEADLESS_CLASSIFIER_SRC, _HERE,
                 self.session_uuid, checkpoint_id, now or "", out_path],
                env=self._child_env(), stdin=devnull_in, stdout=devnull_out,
                stderr=devnull_err)
        self.assertEqual(completed.returncode, 0,
                         "headless classifier subprocess failed")
        with open(out_path) as fh:
            return json.load(fh)["classification"]

    @staticmethod
    def instant(offset_s):
        return _iso(datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=offset_s))


# =========================================================================== #
# The defect itself: a crashed claimant's strand vs a genuinely live claim.    #
# =========================================================================== #


class CrashedClaimantStrandTests(_ArtifactFixture):

    def test_a_real_crashed_claimant_and_a_real_live_claimant_differ(self):
        """M5F-CLAIM-CRASH-1 / A-C5-CRASH-STRAND, end to end and non
        -vacuous: two REAL claimant processes holding EQUAL-LENGTH leases,
        one SIGKILLed the instant it held its claim and one still running,
        produce claim artifacts that the pre-existing reconstruction cannot
        tell apart -- and the new classifier can, at one single instant,
        reading nothing but durable artifacts."""
        crashed_id, live_id, at, _c, _l = self.equal_lease_pair()

        # Both strands look identical to every pre-existing artifact reader:
        # a claim in state "claimed", no result, no receipt.
        for cid in (crashed_id, live_id):
            state = verification.reconstruct_checkpoint_state(
                self.session_uuid, cid)
            self.assertEqual(state["state"], "claimed")
            self.assertIsNone(state["result"])
            self.assertIsNone(state["receipt"])
            self.assertEqual(state["claim"]["state"],
                             verification.CHECKPOINT_CLAIM_CLAIMED)

        strand_verdict = self.classify(crashed_id, now=at)
        live_verdict = self.classify(live_id, now=at)
        self.assertIn(strand_verdict, CRASH_CLASSES)
        self.assertNotIn(live_verdict, CRASH_CLASSES)
        self.assertNotEqual(
            strand_verdict, live_verdict,
            "a crashed claimant's strand and an equal-lease live claim must "
            "be distinguishable from durable artifacts alone")

    def test_the_strand_expires_on_the_real_wall_clock_with_no_injected_now(
            self):
        """The same verdict with NO injected clock at all: a short lease is
        allowed to lapse in real time, so nothing about the result depends
        on a test-supplied `now`."""
        crashed_id = "cp-crashed-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(crashed_id, "executor-crashed", 0.5,
                                   "crash")
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        claim = self.claim_artifact(crashed_id)
        deadline = _parse_instant(claim["lease_deadline_at"])
        remaining = (deadline - datetime.datetime.now(
            datetime.timezone.utc)).total_seconds()
        # Before its lease lapses the strand is NOT yet a crash, on the real
        # clock -- expiry, not identity, is what makes it actionable.
        self.assertEqual(self.classify(crashed_id), "owned_verification")
        time.sleep(max(0.0, remaining) + 0.5)
        self.assertEqual(self.classify(crashed_id), "process_crash")

    def test_the_strand_is_never_productive_wait_or_silence(self):
        crashed_id = "cp-crashed-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(crashed_id, "executor-crashed", 1.0,
                                   "crash")
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        verdict = self.classify(crashed_id, now=self.instant(GRACE_S + 600))
        self.assertIn(verdict, CRASH_CLASSES)
        for forbidden in FORBIDDEN_FOR_A_STRAND:
            self.assertNotEqual(
                verdict, forbidden,
                "a stranded claim must never classify as %r" % forbidden)

    def test_a_live_claim_is_not_a_crash_anywhere_inside_its_lease(self):
        live_id = "cp-live-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(live_id, "executor-live", 600.0, "live")
        self.assertIsNone(proc.poll())
        claimed_at = _parse_instant(self.claim_artifact(live_id)["claimed_at"])
        for offset in (0, 1, 60, 599, 600, 600 + GRACE_S):
            at = _iso(claimed_at + datetime.timedelta(seconds=offset))
            verdict = self.classify(live_id, now=at)
            self.assertNotIn(
                verdict, CRASH_CLASSES,
                "a live claim %ss into a %ss lease must not classify as a "
                "crash (got %r)" % (offset, 600 + GRACE_S, verdict))
            self.assertEqual(verdict, "owned_verification")

    def test_every_returned_class_is_in_the_real_closed_taxonomy(self):
        """The literal strings this classifier returns are exactly members
        of `cowork_activity.ACTIVITY_CLASSES` -- vocabulary alignment proven
        HERE, in the test, precisely because the production module must not
        import that module."""
        for name in CRASH_CLASSES + FORBIDDEN_FOR_A_STRAND + (
                "owned_verification",):
            self.assertIn(name, activity.ACTIVITY_CLASS_SET)
        seen = set()
        seen.add(self.classify("cp-never-claimed"))
        crashed_id = "cp-crashed-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(crashed_id, "executor-crashed", 1.0,
                                   "crash")
        proc.wait(timeout=30)
        seen.add(self.classify(crashed_id, now=self.instant(GRACE_S + 600)))
        seen.add(self.classify(crashed_id, now=self.instant(-120)))
        for verdict in seen:
            self.assertIn(verdict, activity.ACTIVITY_CLASS_SET)


# =========================================================================== #
# F1 equal-lease negative controls: the lease is not the command timeout.      #
# =========================================================================== #


class EqualLeaseNegativeControlTests(_ArtifactFixture):
    """A healthy claimant's obligation is its command timeout PLUS the
    startup/publish grace. Charging it only the raw timeout would classify
    a live process that used its full approved runtime -- and is now
    persisting its result and receipt -- as crashed."""

    def test_the_grace_is_derived_from_the_existing_worker_deadline_policy(
            self):
        expected = float(
            verification.DEFAULT_STARTUP_ALLOWANCE_S
            + 2 * verification.DEFAULT_TERM_GRACE_S
            + verification.DEFAULT_CLEANUP_ALLOWANCE_S
            + verification.DEFAULT_EVIDENCE_ALLOWANCE_S)
        self.assertEqual(GRACE_S, expected,
                         "the lease grace must be composed from this "
                         "module's own existing timeout-policy constants, "
                         "never a fresh magic number")
        self.assertTrue(0 < GRACE_S <= 3600,
                        "the grace must stay bounded, got %r" % (GRACE_S,))

    def test_a_healthy_live_claimant_past_its_raw_command_timeout_is_not_a_crash(
            self):
        """The F1 control, on the REAL wall clock with a REAL live process:
        the command timeout has genuinely elapsed, the claimant is healthy
        and still running, and the verdict is not `process_crash`."""
        live_id = "cp-live-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(live_id, "executor-live", 1.0, "live")
        claim = self.claim_artifact(live_id)
        claimed_at = _parse_instant(claim["claimed_at"])
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "the claimant must still be healthy")
        elapsed = (datetime.datetime.now(datetime.timezone.utc)
                   - claimed_at).total_seconds()
        self.assertGreater(elapsed, claim["lease_timeout_s"],
                           "this control is only meaningful past the raw "
                           "command timeout")
        self.assertEqual(self.classify(live_id), "owned_verification")
        self.assertNotIn(self.classify(live_id), CRASH_CLASSES)

    def test_expiry_bites_only_after_the_whole_lease_not_the_raw_timeout(self):
        """Equal-lease boundary sweep on a real crashed strand: at the raw
        command timeout, and anywhere inside the grace, the verdict is still
        `owned_verification`; only past `timeout + grace` does it become
        `process_crash`."""
        crashed_id = "cp-crashed-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(crashed_id, "executor-crashed", 30.0,
                                   "crash")
        proc.wait(timeout=30)
        claim = self.claim_artifact(crashed_id)
        claimed_at = _parse_instant(claim["claimed_at"])
        self.assertEqual(claim["lease_timeout_s"], 30.0)
        self.assertEqual(claim["lease_grace_s"], GRACE_S)
        self.assertEqual(
            _parse_instant(claim["lease_deadline_at"]),
            claimed_at + datetime.timedelta(seconds=30.0 + GRACE_S),
            "the persisted deadline must be timeout PLUS the bounded grace")
        for offset, expected in (
                (29.0, "owned_verification"),
                (30.0, "owned_verification"),
                (30.0 + GRACE_S / 2, "owned_verification"),
                (30.0 + GRACE_S, "owned_verification"),
                (30.0 + GRACE_S + 1.0, "process_crash")):
            at = _iso(claimed_at + datetime.timedelta(seconds=offset))
            self.assertEqual(self.classify(crashed_id, now=at), expected,
                             "at claimed_at+%ss the verdict must be %r"
                             % (offset, expected))

    def test_an_unbounded_request_records_no_finite_expiry(self):
        live_id = "cp-live-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(live_id, "executor-live", None, "live")
        self.assertIsNone(proc.poll())
        claim = self.claim_artifact(live_id)
        self.assertIsNone(claim["lease_timeout_s"],
                          "an unbounded request must not be given an "
                          "invented finite timeout")
        self.assertIsNone(claim["lease_deadline_at"],
                          "an unbounded request must record NO finite "
                          "expiry at all")

    def test_a_healthy_unbounded_live_claimant_is_never_falsely_crashed(self):
        """F1's core false-positive control: a legitimately unbounded
        command has no deadline to miss, so no amount of elapsed time may
        indict it."""
        live_id = "cp-live-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(live_id, "executor-live", None, "live")
        self.assertIsNone(proc.poll())
        for offset in (0, 3600, 86400, 86400 * 365 * 10):
            verdict = self.classify(live_id, now=self.instant(offset))
            self.assertEqual(verdict, "owned_verification",
                             "an unbounded healthy claimant %ss in must "
                             "never classify as %r" % (offset, verdict))
            self.assertNotIn(verdict, CRASH_CLASSES)

    def test_an_unbounded_lease_is_conservative_by_design(self):
        """Disclosed tradeoff, asserted rather than left implicit: because
        an unbounded request records no expiry, even a genuinely crashed
        unbounded claimant stays `owned_verification`. Never falsely
        crashing a healthy claimant is the direction this package chooses;
        recovering unbounded strands would need a liveness signal beyond
        durable artifacts, which is out of scope."""
        crashed_id = "cp-crashed-" + uuid.uuid4().hex[:8]
        proc = self.spawn_claimant(crashed_id, "executor-crashed", None,
                                   "crash")
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(self.classify(crashed_id,
                                       now=self.instant(86400 * 365)),
                         "owned_verification")

    def test_a_bounded_strand_and_an_unbounded_live_claim_still_differ(self):
        crashed_id = "cp-crashed-" + uuid.uuid4().hex[:8]
        live_id = "cp-live-" + uuid.uuid4().hex[:8]
        crashed = self.spawn_claimant(crashed_id, "executor-crashed", 1.0,
                                      "crash")
        crashed.wait(timeout=30)
        live = self.spawn_claimant(live_id, "executor-live", None, "live")
        self.assertIsNone(live.poll())
        at = self.instant(GRACE_S + 600)
        self.assertIn(self.classify(crashed_id, now=at), CRASH_CLASSES)
        self.assertNotIn(self.classify(live_id, now=at), CRASH_CLASSES)


# =========================================================================== #
# Artifact-only classification of every reachable branch.                     #
# =========================================================================== #


class ArtifactOnlyClassificationTests(_ArtifactFixture):

    def _persist_request(self, timeout_s=None):
        return verification.build_and_persist_checkpoint_request(
            self.session_uuid, self.checkpoint_id, "W-1", "build",
            "candidate-digest-0", ["/bin/sh", "-c", "true"], self.workdir,
            verification.MUTATION_CLASS_READ_ONLY, timeout_s=timeout_s)

    def test_no_claim_at_all_is_silence_not_a_crash(self):
        self._persist_request(timeout_s=5.0)
        self.assertEqual(self.classify(self.checkpoint_id),
                         "no_evidence_silence")

    def test_an_unreadable_claim_file_is_a_crash_not_silence(self):
        # A claimant killed mid-claim-write leaves a claim FILE that does
        # not read back as a record. That is durable evidence of a claimant,
        # so it must never be collapsed into silence.
        self._persist_request(timeout_s=5.0)
        claim_path = state_store.checkpoint_claim_path_for(
            self.session_uuid, self.checkpoint_id)
        os.makedirs(os.path.dirname(claim_path), exist_ok=True)
        with open(claim_path, "w") as fh:
            fh.write('{"checkpoint_id": "cp-')
        self.assertIsNone(state_store.read_json_tolerant(claim_path))
        verdict = self.classify(self.checkpoint_id)
        self.assertEqual(verdict, "process_crash")
        self.assertNotIn(verdict, FORBIDDEN_FOR_A_STRAND)

    def test_within_deadline_is_owned_verification(self):
        self._persist_request(timeout_s=300.0)
        claimed, _record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(claimed)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(299)),
                         "owned_verification")

    def test_past_deadline_with_no_result_and_no_receipt_is_a_crash(self):
        self._persist_request(timeout_s=300.0)
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")
        self.assertIsNone(state_store.read_json_tolerant(
            state_store.checkpoint_result_path_for(self.session_uuid,
                                                   self.checkpoint_id)))
        self.assertIsNone(state_store.read_json_tolerant(
            state_store.checkpoint_receipt_path_for(self.session_uuid,
                                                    self.checkpoint_id)))
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(301 + GRACE_S)),
                         "process_crash")

    def test_past_deadline_with_a_timed_out_result_is_a_hung_descendant(self):
        # The claimant persisted a result recording that the executed
        # descendant overran its own timeout, then died before publishing a
        # receipt: a hung descendant, not a bare process crash.
        self._persist_request(timeout_s=300.0)
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")
        result = verification.normalize_checkpoint_result({
            "checkpoint_schema_version":
                verification.CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": self.checkpoint_id,
            "executor_identity": "executor-1",
            "argv": ["/bin/sh", "-c", "true"],
            "cwd": self.workdir,
            "exit_code": None,
            "evidence_state": verification.EVIDENCE_UNRESOLVED,
            "timed_out": True,
        })
        state_store.write_json_atomic_durable(
            state_store.checkpoint_result_path_for(self.session_uuid,
                                                   self.checkpoint_id),
            result)
        verdict = self.classify(self.checkpoint_id,
                                now=self.instant(301 + GRACE_S))
        self.assertEqual(verdict, "hung_descendant")
        self.assertIn(verdict, CRASH_CLASSES)
        for forbidden in FORBIDDEN_FOR_A_STRAND:
            self.assertNotEqual(verdict, forbidden)

    def test_a_published_receipt_is_never_a_crash_however_late(self):
        self._persist_request(timeout_s=1.0)
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")
        receipt = verification.normalize_checkpoint_receipt({
            "checkpoint_schema_version":
                verification.CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": self.checkpoint_id,
            "session_uuid": self.session_uuid,
            "phase": "build",
            "candidate_digest": "candidate-digest-0",
            "verdict": verification.CHECKPOINT_ACCEPTED,
            "terminal": True,
        })
        published, _stored = verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, receipt)
        self.assertTrue(published)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(86400)),
                         "owned_verification")

    def test_a_durable_receipt_without_the_terminal_marker_is_not_a_crash(self):
        # `publish_checkpoint_receipt` writes the receipt FIRST; a crash
        # between the two writes leaves a durable receipt behind a claim
        # still marked "claimed". The work completed -- never a crash class.
        self._persist_request(timeout_s=1.0)
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")
        receipt = verification.normalize_checkpoint_receipt({
            "checkpoint_schema_version":
                verification.CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": self.checkpoint_id,
            "session_uuid": self.session_uuid,
            "phase": "build",
            "candidate_digest": "candidate-digest-0",
            "verdict": verification.CHECKPOINT_ACCEPTED,
            "terminal": True,
        })
        state_store.write_json_atomic_durable(
            state_store.checkpoint_receipt_path_for(self.session_uuid,
                                                    self.checkpoint_id),
            receipt)
        self.assertEqual(self.claim_artifact(self.checkpoint_id)["state"],
                         verification.CHECKPOINT_CLAIM_CLAIMED)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(86400)),
                         "owned_verification")

    def test_the_deadline_basis_is_the_request_timeout_plus_the_grace(self):
        self._persist_request(timeout_s=42.0)
        _claimed, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertEqual(record["lease_timeout_s"], 42.0)
        self.assertEqual(record["lease_grace_s"], GRACE_S)
        on_disk = self.claim_artifact(self.checkpoint_id)
        self.assertEqual(on_disk["lease_timeout_s"], 42.0)
        claimed_at = _parse_instant(on_disk["claimed_at"])
        deadline_at = _parse_instant(on_disk["lease_deadline_at"])
        self.assertEqual((deadline_at - claimed_at).total_seconds(),
                         42.0 + GRACE_S)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(41)),
                         "owned_verification")
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(43)),
                         "owned_verification")
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(43 + GRACE_S)),
                         "process_crash")

    def test_an_absent_request_timeout_yields_an_unbounded_lease(self):
        self._persist_request(timeout_s=None)
        _claimed, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertIsNone(record["lease_timeout_s"])
        self.assertIsNone(record["lease_deadline_at"])
        self.assertEqual(record["lease_grace_s"], GRACE_S)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(86400 * 365)),
                         "owned_verification")

    def test_a_claim_with_no_request_at_all_is_unbounded_not_expired(self):
        # Package A's own fixtures claim checkpoints with no request
        # persisted; nothing durable bounds such a claim, so it must never
        # acquire an invented expiry.
        _claimed, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertIsNone(record["lease_deadline_at"])
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(86400 * 365)),
                         "owned_verification")

    def test_a_legacy_claim_without_a_deadline_still_classifies(self):
        # A claim written before the deadline basis existed: the basis is
        # rebuilt from `claimed_at` plus the request's own timeout AND the
        # same bounded grace, so an upgraded reader neither loses the
        # distinction on old artifacts nor charges them a shorter lease.
        self._persist_request(timeout_s=30.0)
        claim_path = state_store.checkpoint_claim_path_for(
            self.session_uuid, self.checkpoint_id)
        state_store.write_json_atomic_durable(claim_path, {
            "checkpoint_id": self.checkpoint_id,
            "executor_identity": "executor-legacy",
            "claimed_at": self.instant(0),
            "state": verification.CHECKPOINT_CLAIM_CLAIMED,
        })
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(29)),
                         "owned_verification")
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(31)),
                         "owned_verification")
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(31 + GRACE_S)),
                         "process_crash")

    def test_a_legacy_claim_with_no_durable_timeout_is_unbounded(self):
        self._persist_request(timeout_s=None)
        claim_path = state_store.checkpoint_claim_path_for(
            self.session_uuid, self.checkpoint_id)
        state_store.write_json_atomic_durable(claim_path, {
            "checkpoint_id": self.checkpoint_id,
            "executor_identity": "executor-legacy",
            "claimed_at": self.instant(0),
            "state": verification.CHECKPOINT_CLAIM_CLAIMED,
        })
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(86400 * 365)),
                         "owned_verification")

    def test_classification_never_writes_deletes_or_abandons_anything(self):
        """Classification only: the artifact tree is byte-for-byte identical
        before and after, and the dead `CHECKPOINT_CLAIM_ABANDONED`
        vocabulary is never written into a claim."""
        self._persist_request(timeout_s=1.0)
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")

        def snapshot():
            root = state_store.checkpoint_root_for(self.session_uuid)
            out = {}
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    with open(path, "rb") as fh:
                        out[os.path.relpath(path, root)] = hashlib.sha256(
                            fh.read()).hexdigest()
            return out

        before = snapshot()
        self.assertTrue(before)
        for offset in (-10, 0, 10, 100000):
            self.classify(self.checkpoint_id, now=self.instant(offset))
        self.assertEqual(snapshot(), before,
                         "classification must never write, delete, or "
                         "rewrite any checkpoint artifact")
        claim = self.claim_artifact(self.checkpoint_id)
        self.assertEqual(claim["state"],
                         verification.CHECKPOINT_CLAIM_CLAIMED)
        self.assertNotIn(verification.CHECKPOINT_CLAIM_ABANDONED,
                         json.dumps(claim))

    def test_reconstruct_checkpoint_state_still_reports_claimed(self):
        # The classifier is strictly additive: the pre-existing state machine
        # keeps its exact four-value vocabulary, with no "abandoned" branch.
        self._persist_request(timeout_s=1.0)
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")
        state = verification.reconstruct_checkpoint_state(
            self.session_uuid, self.checkpoint_id)
        self.assertEqual(state["state"], "claimed")
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(3600 + GRACE_S)),
                         "process_crash")
        self.assertEqual(
            verification.reconstruct_checkpoint_state(
                self.session_uuid, self.checkpoint_id)["state"], "claimed")


# =========================================================================== #
# M5C5-LEASE-FIDELITY-1: an unreadable request is never an unbounded lease.    #
# =========================================================================== #


class ClaimLeaseFidelityTests(_ArtifactFixture):
    """`claim_checkpoint` derives the lease by RE-READING the request that
    `run_checkpoint` already read successfully a moment earlier. That reread
    goes through `state_store.read_json_tolerant`, which swallows every
    `OSError`/`ValueError` and answers a single overloaded `None` for both
    "never persisted" and "persisted but unreadable right now".

    Collapsing those two is durably irreversible: the claim records its own
    bound verbatim, and `classify_checkpoint_claim_liveness` only consults
    the request when the `lease_timeout_s` KEY IS ABSENT -- a persisted
    `null` MEANS unbounded and is never re-checked. So one transient miss
    against a request that durably declares `timeout_s: 30` would pin that
    checkpoint in `owned_verification` forever, the one class no elapsed
    time can indict.

    These tests fix the split: only a definitive `ENOENT` yields an
    unbounded lease; every other unreadable/malformed outcome fails closed
    before anything is persisted; and a request that IS read and explicitly
    declares no timeout keeps its documented unbounded behavior."""

    def _persist_request(self, timeout_s=None, checkpoint_id=None):
        return verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id or self.checkpoint_id, "W-1",
            "build", "candidate-digest-0", ["/bin/sh", "-c", "true"],
            self.workdir, verification.MUTATION_CLASS_READ_ONLY,
            timeout_s=timeout_s)

    def request_path(self, checkpoint_id=None):
        return state_store.checkpoint_request_path_for(
            self.session_uuid, checkpoint_id or self.checkpoint_id)

    def claim_path(self, checkpoint_id=None):
        return state_store.checkpoint_claim_path_for(
            self.session_uuid, checkpoint_id or self.checkpoint_id)

    def blind_one_request_read(self):
        """Patch the tolerant reader to answer `None` for THIS checkpoint's
        request path only -- the exact value `read_json_tolerant` already
        returns for any swallowed `OSError`/`ValueError` -- while every
        other path still reads normally."""
        real_read = state_store.read_json_tolerant
        target = self.request_path()

        def blinded(path):
            if path == target:
                return None
            return real_read(path)

        return mock.patch.object(state_store, "read_json_tolerant",
                                 side_effect=blinded)

    # -- the blocker itself ----------------------------------------------- #

    def test_a_transient_request_reread_failure_never_persists_a_claim(self):
        self._persist_request(timeout_s=30.0)
        with self.blind_one_request_read():
            with self.assertRaises(verification.CheckpointError) as ctx:
                verification.claim_checkpoint(
                    self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertEqual(ctx.exception.code, "checkpoint_request_unreadable")
        # Fail CLOSED, and closed means nothing durable happened at all.
        self.assertFalse(os.path.exists(self.claim_path()),
                         "a claim must never be persisted once the lease "
                         "basis could not be derived")
        self.assertIsNone(self.claim_artifact(self.checkpoint_id))
        # The request itself is untouched and still declares its bound.
        self.assertEqual(state_store.read_json_tolerant(
            self.request_path())["timeout_s"], 30.0)

    def test_the_declared_bound_survives_a_transient_failure_and_a_retry(self):
        # The whole point: the bound is not lost. A retry after the blip
        # produces the correct BOUNDED lease, and the checkpoint remains
        # indictable once that lease lapses.
        self._persist_request(timeout_s=30.0)
        with self.blind_one_request_read():
            with self.assertRaises(verification.CheckpointError):
                verification.claim_checkpoint(
                    self.session_uuid, self.checkpoint_id, "executor-1")
        claimed, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(claimed)
        self.assertEqual(record["lease_timeout_s"], 30.0)
        self.assertIsNotNone(record["lease_deadline_at"])
        on_disk = self.claim_artifact(self.checkpoint_id)
        self.assertEqual(on_disk["lease_timeout_s"], 30.0)
        self.assertEqual(
            (_parse_instant(on_disk["lease_deadline_at"])
             - _parse_instant(on_disk["claimed_at"])).total_seconds(),
            30.0 + GRACE_S)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(29)),
                         "owned_verification")
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(31 + GRACE_S)),
                         "process_crash")

    def test_a_bounded_request_can_never_persist_a_null_lease_timeout(self):
        # The durable invariant stated directly, over every failure mode a
        # bounded request can encounter at claim time.
        self._persist_request(timeout_s=30.0)
        with self.blind_one_request_read():
            self.assertRaises(verification.CheckpointError,
                              verification.claim_checkpoint,
                              self.session_uuid, self.checkpoint_id,
                              "executor-1")
        self.assertIsNone(self.claim_artifact(self.checkpoint_id))
        claimed, _record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(claimed)
        self.assertIsNotNone(
            self.claim_artifact(self.checkpoint_id)["lease_timeout_s"],
            "a request declaring timeout_s=30 must never yield a durable "
            "claim with lease_timeout_s=null")

    # -- every unreadable/malformed shape fails closed --------------------- #

    def test_a_torn_request_file_fails_closed_deterministically(self):
        self._persist_request(timeout_s=30.0)
        with open(self.request_path(), "w") as fh:
            fh.write('{"checkpoint_id": "cp-')
        self.assertIsNone(state_store.read_json_tolerant(self.request_path()))
        codes = []
        for identity in ("executor-1", "executor-2", "executor-3"):
            with self.assertRaises(verification.CheckpointError) as ctx:
                verification.claim_checkpoint(
                    self.session_uuid, self.checkpoint_id, identity)
            codes.append(ctx.exception.code)
        self.assertEqual(codes, ["checkpoint_request_unreadable"] * 3,
                         "the refusal must be explicit and deterministic, "
                         "not order- or attempt-dependent")
        self.assertFalse(os.path.exists(self.claim_path()))

    def test_a_request_that_is_not_a_json_object_fails_closed(self):
        self._persist_request(timeout_s=30.0)
        with open(self.request_path(), "w") as fh:
            fh.write("[1, 2, 3]")
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.claim_checkpoint(
                self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertEqual(ctx.exception.code, "checkpoint_request_unreadable")
        self.assertFalse(os.path.exists(self.claim_path()))

    def test_an_unopenable_request_path_fails_closed(self):
        # A non-ENOENT `OSError` from the read itself (here `IsADirectoryError`
        # -- no chmod, so the result does not depend on who runs the suite).
        request_path = self.request_path()
        os.makedirs(request_path, exist_ok=True)
        self.assertIsNone(state_store.read_json_tolerant(request_path))
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.claim_checkpoint(
                self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertEqual(ctx.exception.code, "checkpoint_request_unreadable")
        self.assertFalse(os.path.exists(self.claim_path()))

    def test_an_unanswerable_existence_check_also_fails_closed(self):
        # The narrow remaining window: the read failed AND the follow-up
        # existence question cannot be answered either. Absence is only ever
        # inferred from a definitive ENOENT, never from a failed probe.
        self._persist_request(timeout_s=30.0)
        with self.blind_one_request_read():
            with mock.patch.object(os, "stat",
                                   side_effect=OSError(5, "simulated EIO")):
                with self.assertRaises(verification.CheckpointError) as ctx:
                    verification.claim_checkpoint(
                        self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertEqual(ctx.exception.code, "checkpoint_request_unreadable")
        self.assertFalse(os.path.exists(self.claim_path()))

    def test_a_persisted_but_malformed_timeout_is_refused_not_coerced(self):
        # `normalize_checkpoint_request` rejects this shape at write time, so
        # it can only appear via corruption -- and silently coercing it to
        # `None` would be the same durable downgrade by another route.
        for bad in ("30", True, -5, 0, [30]):
            checkpoint_id = "cp-bad-" + uuid.uuid4().hex[:8]
            state_store.write_json_atomic_durable(
                self.request_path(checkpoint_id),
                {"checkpoint_schema_version":
                     verification.CHECKPOINT_SCHEMA_VERSION,
                 "checkpoint_id": checkpoint_id,
                 "session_uuid": self.session_uuid,
                 "timeout_s": bad})
            with self.assertRaises(verification.CheckpointError) as ctx:
                verification.claim_checkpoint(
                    self.session_uuid, checkpoint_id, "executor-1")
            self.assertEqual(ctx.exception.code,
                             "checkpoint_request_bad_timeout_s",
                             "timeout_s=%r must be refused" % (bad,))
            self.assertFalse(os.path.exists(self.claim_path(checkpoint_id)))

    # -- explicitly unbounded requests keep their documented behavior ------ #

    def test_an_explicitly_null_timeout_still_yields_an_unbounded_lease(self):
        # Requirement 3: a request that IS read successfully and explicitly
        # declares no timeout is unbounded by design -- the fail-closed path
        # must not have swept this into an error.
        self._persist_request(timeout_s=None)
        self.assertIn("timeout_s",
                      state_store.read_json_tolerant(self.request_path()))
        self.assertIsNone(state_store.read_json_tolerant(
            self.request_path())["timeout_s"])
        claimed, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(claimed)
        self.assertIsNone(record["lease_timeout_s"])
        self.assertIsNone(record["lease_deadline_at"])
        self.assertEqual(record["lease_grace_s"], GRACE_S)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(86400 * 365 * 100)),
                         "owned_verification")

    def test_a_request_omitting_timeout_s_entirely_is_still_unbounded(self):
        checkpoint_id = "cp-nokey-" + uuid.uuid4().hex[:8]
        state_store.write_json_atomic_durable(
            self.request_path(checkpoint_id),
            {"checkpoint_schema_version":
                 verification.CHECKPOINT_SCHEMA_VERSION,
             "checkpoint_id": checkpoint_id,
             "session_uuid": self.session_uuid})
        claimed, record = verification.claim_checkpoint(
            self.session_uuid, checkpoint_id, "executor-1")
        self.assertTrue(claimed)
        self.assertIsNone(record["lease_timeout_s"])
        self.assertIsNone(record["lease_deadline_at"])

    def test_no_request_artifact_at_all_is_unbounded_not_a_failure(self):
        # Package A's own fixtures claim with no request persisted at all.
        # A definitive ENOENT is the ONLY licence for the unbounded fallback,
        # and it still grants it.
        self.assertFalse(os.path.exists(self.request_path()))
        claimed, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(claimed)
        self.assertIsNone(record["lease_timeout_s"])
        self.assertIsNone(record["lease_deadline_at"])
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(86400 * 365)),
                         "owned_verification")

    # -- every other claim semantic is preserved --------------------------- #

    def test_the_durable_claim_schema_is_unchanged(self):
        self._persist_request(timeout_s=30.0)
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")
        self.assertEqual(
            set(self.claim_artifact(self.checkpoint_id)),
            {"checkpoint_id", "executor_identity", "claimed_at", "state",
             "lease_timeout_s", "lease_grace_s", "lease_deadline_at"})

    def test_fencing_and_idempotence_survive_the_fail_closed_path(self):
        self._persist_request(timeout_s=30.0)
        claimed, first = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(claimed)
        with open(self.claim_path(), "rb") as fh:
            before = fh.read()
        # A second claimant hitting the transient read is refused by the
        # fail-closed path -- and still writes nothing over the winner.
        with self.blind_one_request_read():
            with self.assertRaises(verification.CheckpointError):
                verification.claim_checkpoint(
                    self.session_uuid, self.checkpoint_id, "executor-2")
        with open(self.claim_path(), "rb") as fh:
            self.assertEqual(fh.read(), before,
                             "a refused claimant must write nothing at all")
        # And once the read recovers, the duplicate is refused the ordinary
        # way: the original claimant still owns the fence.
        again, existing = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-2")
        self.assertFalse(again)
        self.assertEqual(existing["executor_identity"], "executor-1")
        self.assertEqual(existing["claimed_at"], first["claimed_at"])
        self.assertEqual(existing["state"],
                         verification.CHECKPOINT_CLAIM_CLAIMED)

    def test_the_failure_is_a_checkpoint_error_carrying_a_stable_code(self):
        # Explicit: a typed `CheckpointError` with a parseable code, the same
        # discipline `run_checkpoint`'s `checkpoint_request_missing` uses --
        # never a bare exception and never a silent `(False, None)`.
        self._persist_request(timeout_s=30.0)
        with self.blind_one_request_read():
            with self.assertRaises(verification.CheckpointError) as ctx:
                verification.claim_checkpoint(
                    self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertEqual(ctx.exception.code, "checkpoint_request_unreadable")
        self.assertIn(self.checkpoint_id, str(ctx.exception))


# =========================================================================== #
# No terminal-output dependency.                                              #
# =========================================================================== #


class NoTerminalDependencyTests(_ArtifactFixture):

    def test_the_same_verdicts_are_derived_with_no_terminal_at_all(self):
        """A fresh process, stdin/stdout/stderr all /dev/null, no PTY
        anywhere: the equal-lease crashed strand and live claim classify
        exactly as they do in-process."""
        crashed_id, live_id, at, _c, _l = self.equal_lease_pair()
        self.assertEqual(self.classify_headless(crashed_id, now=at),
                         self.classify(crashed_id, now=at))
        self.assertIn(self.classify_headless(crashed_id, now=at),
                      CRASH_CLASSES)
        self.assertEqual(self.classify_headless(live_id, now=at),
                         self.classify(live_id, now=at))
        self.assertNotIn(self.classify_headless(live_id, now=at),
                         CRASH_CLASSES)

    def test_the_classifier_touches_no_stream_process_or_environment(self):
        """Structural, not by eye: the classifier's own AST references no
        stream, no subprocess, no process table, and no environment -- and
        the only `state_store` entry points it uses are the four durable
        checkpoint artifact paths plus the tolerant reader."""
        node = _function_node(
            inspect.getsource(verification).replace("\r\n", "\n"),
            "classify_checkpoint_claim_liveness")
        attrs, names, state_store_attrs = set(), set(), set()
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute):
                attrs.add(child.attr)
                if isinstance(child.value, ast.Name) and (
                        child.value.id == "state_store"):
                    state_store_attrs.add(child.attr)
            elif isinstance(child, ast.Name):
                names.add(child.id)
        forbidden_attrs = {"stdout", "stderr", "stdin", "isatty", "fileno",
                           "Popen", "run", "check_output", "communicate",
                           "system", "popen", "environ", "getenv", "kill",
                           "waitpid", "read", "write", "remove", "unlink",
                           "rename", "makedirs"}
        self.assertFalse(attrs & forbidden_attrs,
                         "classifier references forbidden attribute(s): %s"
                         % sorted(attrs & forbidden_attrs))
        forbidden_names = {"print", "subprocess", "sys", "input", "open",
                           "signal", "socket", "time", "shutil",
                           "cowork_activity", "activity"}
        self.assertFalse(names & forbidden_names,
                         "classifier references forbidden name(s): %s"
                         % sorted(names & forbidden_names))
        self.assertEqual(
            state_store_attrs,
            {"read_json_tolerant", "checkpoint_claim_path_for",
             "checkpoint_request_path_for", "checkpoint_result_path_for",
             "checkpoint_receipt_path_for"},
            "the classifier must read exactly the durable request/claim/"
            "result/receipt artifacts and nothing else")


# =========================================================================== #
# Kernel-exclusive once-only claiming is unchanged.                           #
# =========================================================================== #


class OnceOnlyClaimUnchangedTests(_ArtifactFixture):

    def test_a_fresh_claim_succeeds_and_records_the_executor(self):
        ok, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(ok)
        self.assertEqual(record["executor_identity"], "executor-1")
        self.assertEqual(record["state"],
                         verification.CHECKPOINT_CLAIM_CLAIMED)

    def test_duplicate_claim_by_the_same_executor_identity_is_refused(self):
        ok, first = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(ok)
        claim_path = state_store.checkpoint_claim_path_for(
            self.session_uuid, self.checkpoint_id)
        with open(claim_path, "rb") as fh:
            before = fh.read()
        again, existing = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertFalse(again, "a duplicate claim by the SAME executor "
                                "identity must still be refused")
        self.assertEqual(existing["claimed_at"], first["claimed_at"])
        with open(claim_path, "rb") as fh:
            self.assertEqual(fh.read(), before,
                             "a refused claimant must write nothing at all")

    def test_duplicate_claim_by_a_different_executor_is_refused(self):
        verification.claim_checkpoint(self.session_uuid, self.checkpoint_id,
                                      "executor-1")
        ok, existing = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-2")
        self.assertFalse(ok)
        self.assertEqual(existing["executor_identity"], "executor-1")

    def test_concurrent_claimants_exactly_one_wins(self):
        winners = []
        winners_lock = threading.Lock()
        claimant_count = 12
        barrier = threading.Barrier(claimant_count)

        def attempt(i):
            barrier.wait()
            ok, _record = verification.claim_checkpoint(
                self.session_uuid, self.checkpoint_id, "executor-%d" % i)
            if ok:
                with winners_lock:
                    winners.append(i)

        threads = [threading.Thread(target=attempt, args=(i,))
                   for i in range(claimant_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(winners), 1,
                         "exactly one concurrent claimant must win, got %r"
                         % winners)
        self.assertEqual(self.claim_artifact(self.checkpoint_id)[
            "executor_identity"], "executor-%d" % winners[0])

    def test_a_stranded_claim_is_still_never_re_granted(self):
        # Classification is not reclaim: a past-lease strand stays claimed,
        # and a second claimant is still refused.
        proc = self.spawn_claimant(self.checkpoint_id, "executor-crashed",
                                   1.0, "crash")
        proc.wait(timeout=30)
        self.assertEqual(self.classify(self.checkpoint_id,
                                       now=self.instant(GRACE_S + 600)),
                         "process_crash")
        ok, existing = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-successor")
        self.assertFalse(ok, "a stranded claim must never be re-granted -- "
                             "reclaim/takeover is out of scope")
        self.assertEqual(existing["executor_identity"], "executor-crashed")
        self.assertEqual(existing["state"],
                         verification.CHECKPOINT_CLAIM_CLAIMED)

    def test_acquisition_still_goes_through_the_kernel_exclusive_primitive(self):
        src = inspect.getsource(verification.claim_checkpoint)
        self.assertIn("_create_checkpoint_claim_exclusive", src)
        create_src = inspect.getsource(
            verification._create_checkpoint_claim_exclusive)
        self.assertIn("O_CREAT", create_src)
        self.assertIn("O_EXCL", create_src)
        # And the primitive itself is byte-identical to the signed base.
        base_named, _unnamed = _top_level_index(
            _git_show_text(BASE_SHA, "scripts/cowork_verification.py"))
        self.assertEqual(
            base_named["_create_checkpoint_claim_exclusive"],
            _top_level_index(inspect.getsource(verification))[0][
                "_create_checkpoint_claim_exclusive"])


# =========================================================================== #
# Mechanical scope confinement: paths, symbols, byte identity.                #
# =========================================================================== #


class ProductionScopeConfinementTests(unittest.TestCase):
    """Detects any edit outside the exact authorized production symbols --
    measured against the signed base in the LIVE working tree, because this
    package's diff is uncommitted by contract."""

    def setUp(self):
        self.base_source = _git_show_text(
            BASE_SHA, "scripts/cowork_verification.py")
        self.current_source = _read_local_bytes(
            "scripts/cowork_verification.py").decode("utf-8")
        self.base_named, self.base_unnamed = _top_level_index(self.base_source)
        self.cur_named, self.cur_unnamed = _top_level_index(
            self.current_source)

    def test_changed_paths_are_within_the_three_path_write_authority(self):
        """Commit-pinned path authority, restated cumulatively across the
        authorized serial repair history AND the authorized skill release.

        The claim-crash package's own range -- the signed base through its
        bounded correction -- changed only paths inside the frozen
        three-path write authority. That range is now history, so it is
        compared commit to commit rather than against a live tree that
        every authorized later commit would otherwise invalidate. That
        historical authority is unchanged and is still checked in full.

        Past the signed release the user authorized three further,
        serially integrated, separately reviewed test-only repairs. They
        are named by commit and pinned to the exact path each one wrote --
        the scope-gate restatement, the release-pin repair and the serial
        release-confinement repair wrote this suite's own file, the
        handoff-facts repair wrote `scripts/test_cowork.py` -- and
        together they define a CLOSED two-path M5 repair set.
        `scripts/test_cowork.py` is admitted by that separate serial
        authority, NOT by this package's frozen three-path authority,
        which is why it is named here rather than folded into
        `ALLOWED_CHANGED_PATHS`.

        The current signed release then carries one further, separately
        authorized commit that is NOT an M5 repair at all: the skill
        alignment `d994c2f...`, which wrote exactly fourteen `skills/`
        paths, followed by the empty release-boundary commit
        `a89b6037...`, which changed nothing and carries the alignment's
        own tree. Those fourteen paths are enumerated LITERALLY as their
        own CLOSED release-evolution set -- never derived from whatever
        git reports today, never a `skills/**` prefix wildcard, and never
        merged into the M5 repair authority. Real tracked sibling paths
        under `skills/` that the alignment commit did NOT write are still
        rejected, which is what makes it a set rather than a prefix.

        The live tree is held to the union of those two closed sets and to
        nothing wider: any further path fails closed, committed, staged,
        dirty or untracked. Nothing is blessed for merely differing --
        every admitted path is named, and each is proven to be exactly
        what one named authorized commit actually wrote."""
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        release = "3f5724ac24c19b0d8b9243b81d31d4cc0cc906f2"
        scope_gates = "c5bd555ba74db91b162a5d3d470862d29b8e85da"
        pin_repair = "1645ae22611caa8e333e4c3bdca2bdd62067cef6"
        handoff = "698cda13a62372c0aef683657b4a9a93837616d5"
        serial_repair = "554ae1d849c3c78726e08cb6fd639eb52d8140ac"
        skill_alignment = "d994c2f36618302207a201bdcf6f0dd67202af5d"
        boundary = "a89b6037cbbd4424dca7ffb2b138a8cc3eeea8da"
        this_suite = "scripts/test_m5_claim_crash_reclaim.py"
        handoff_suite = "scripts/test_cowork.py"
        repair_paths = {this_suite, handoff_suite}
        # The exact, literal fourteen paths the authorized skill-alignment
        # commit wrote -- a closed release-evolution set, NOT M5 repair
        # authority and NOT a `skills/**` wildcard.
        skill_paths = {
            "skills/cowork-cli/SKILL.md",
            "skills/cowork-cli/references/live-supervision.md",
            "skills/cowork-debug/SKILL.md",
            "skills/cowork-orchestrate/SKILL.md",
            "skills/cowork-orchestrate/references/backend-gate-pointer.md",
            "skills/cowork-orchestrate/references/backend-gate.md",
            "skills/cowork-orchestrate/tests/test_select_backend.py",
            "skills/cowork-refactor-orchestrator/SKILL.md",
            "skills/cowork-refactor-orchestrator/agents/openai.yaml",
            "skills/cowork-refactor-orchestrator/references/"
            "artifact-contract-schema.md",
            "skills/cowork-refactor-orchestrator/references/"
            "bootstrap-backend.md",
            "skills/cowork-refactor-orchestrator/references/"
            "cowork-backend-gate.md",
            "skills/cowork-refactor-orchestrator/scripts/orchestrate.py",
            "skills/cowork-refactor-orchestrator/tests/test_orchestrate.py",
        }
        # Everything the current signed release may carry on top of the
        # prior one: two closed sets, kept distinct and never widened.
        release_paths = repair_paths | skill_paths
        # A path no M5 repair package may write under any allowance.
        unrelated = "scripts/cowork.py"
        # Real tracked siblings under `skills/` that the alignment commit
        # did NOT write -- the control that proves the fourteen are a set.
        unauthorized_skills = {
            "skills/cowork-internal/SKILL.md",
            "skills/cowork-orchestrate/agents/openai.yaml",
            "skills/cowork-orchestrate/scripts/select_backend.py",
        }

        def git(*args):
            return subprocess.run(
                ["git"] + list(args), cwd=_REPO_ROOT, capture_output=True,
                text=True, check=True).stdout

        def changed(*args):
            return {p.strip()
                    for p in git("diff", "--name-only", *args).splitlines()
                    if p.strip()}

        def offenders(actual, allowed):
            """THE confinement predicate -- one implementation for every
            layer: whatever falls outside the closed set handed to it. It
            is applied identically to committed ranges, the index, the
            working tree and the untracked set, so each of them fails
            closed on an unadmitted path in exactly the same way."""
            return set(actual) - set(allowed)

        self.assertEqual(len(skill_paths), 14,
                         "the authorized skill-release set is exactly "
                         "fourteen literal paths")
        self.assertTrue(repair_paths.isdisjoint(skill_paths),
                        "the M5 repair authority and the skill-release set "
                        "must stay distinct closed sets")

        package_changed = changed(BASE_SHA, correction)
        self.assertTrue(package_changed,
                        "the claim-crash package must not be an empty range")
        self.assertFalse(
            offenders(package_changed, ALLOWED_CHANGED_PATHS),
            "paths changed outside the frozen three-path write authority: %s"
            % sorted(offenders(package_changed, ALLOWED_CHANGED_PATHS)))

        # The authorized serial repairs, pinned commit by commit to the
        # exact path each one wrote. Their git-measured union -- not the
        # literals above -- is what defines the closed M5 repair set.
        self.assertEqual(changed(release, scope_gates), {this_suite},
                         "the scope-gate restatement must have written this "
                         "suite's own file alone")
        self.assertEqual(changed(scope_gates, pin_repair), {this_suite},
                         "the release-pin repair must have written this "
                         "suite's own file alone")
        self.assertEqual(changed(pin_repair, handoff), {handoff_suite},
                         "the handoff-facts repair must have written %s "
                         "alone" % handoff_suite)
        self.assertEqual(changed(handoff, serial_repair), {this_suite},
                         "the serial release-confinement repair must have "
                         "written this suite's own file alone")
        self.assertEqual(changed(release, serial_repair), repair_paths,
                         "the authorized serial repair range must change "
                         "exactly the closed two-path set")

        # The separately authorized skill release, pinned the same way:
        # exactly the fourteen literal paths, then an empty boundary
        # commit that changed nothing and carries the alignment's tree.
        self.assertEqual(changed(serial_repair, skill_alignment), skill_paths,
                         "the skill-alignment commit must have written "
                         "exactly the fourteen authorized skill paths")
        self.assertEqual(changed(skill_alignment, boundary), set(),
                         "the release-boundary commit must be empty")
        self.assertEqual(
            git("rev-parse", boundary + "^{tree}").strip(),
            git("rev-parse", skill_alignment + "^{tree}").strip(),
            "the empty release-boundary commit must carry the "
            "skill-alignment commit's own tree")
        self.assertEqual(changed(release, boundary), release_paths,
                         "the current signed release must change exactly "
                         "the closed repair set plus the closed "
                         "skill-release set")

        # The skill-release set is a SET, not a `skills/**` prefix: real
        # tracked siblings under `skills/` that the alignment commit never
        # wrote are still rejected by the widest allowance in this test.
        tracked_skills = {p.strip() for p
                          in git("ls-files", "--", "skills/").splitlines()
                          if p.strip()}
        for path in sorted(unauthorized_skills):
            self.assertIn(path, tracked_skills,
                          "%s must be a real tracked path for this control "
                          "to mean anything" % path)
            self.assertNotIn(path, skill_paths,
                             "%s is not part of the authorized skill set"
                             % path)
            self.assertEqual(offenders({path}, release_paths), {path},
                             "%s must fail closed -- the skill set is not a "
                             "prefix wildcard" % path)

        # The live tree against the prior signed release: only the two
        # closed sets may differ, and nothing else.
        dirty = changed(release)
        self.assertFalse(
            offenders(dirty, release_paths),
            "tracked path(s) dirty outside the closed serial repair set and "
            "the closed skill-release set: %s"
            % sorted(offenders(dirty, release_paths)))
        # ... and against the release boundary: this successor may write
        # this suite's own file and nothing else, staged or unstaged.
        head_delta = changed("HEAD")
        self.assertFalse(
            offenders(head_delta, {this_suite}),
            "the index and working tree may differ from HEAD only in this "
            "suite's own file: %s"
            % sorted(offenders(head_delta, {this_suite})))
        staged = changed("--cached")
        self.assertFalse(
            offenders(staged, {this_suite}),
            "nothing but this suite's own file may ever be staged: %s"
            % sorted(offenders(staged, {this_suite})))
        untracked = {p.strip() for p in git(
            "ls-files", "--others", "--exclude-standard").splitlines()
            if p.strip()}
        stray = {p for p in untracked
                 if not (p.endswith(".pyc") and "__pycache__/" in p)}
        self.assertFalse(stray,
                         "untracked path(s) that are not Python bytecode "
                         "caches: %s" % sorted(stray))

        # Cumulative view, still measured from the signed base: every path
        # that differs is one the prior signed release itself already
        # carries, or one the authorized serial repairs wrote, or one of
        # the fourteen the authorized skill release wrote.
        live_changed = {p for p in _working_tree_changed_paths()
                        if not (p.endswith(".pyc") and "__pycache__/" in p)}
        self.assertEqual(
            live_changed, changed(BASE_SHA, release) | release_paths,
            "the live tree must change exactly the paths the prior signed "
            "release changes relative to the signed base, plus the closed "
            "two-path serial repair set and the closed fourteen-path "
            "skill-release set")

        # Real negative control, not a fabrication: a range that genuinely
        # DID write an unrelated production path must be rejected by both
        # closed sets, and each live set this test guards must fail closed
        # on that one path -- flagging it and nothing else.
        real_range = changed(BASE_SHA, release)
        self.assertIn(unrelated, real_range,
                      "the real negative-control range must actually have "
                      "changed %s" % unrelated)
        self.assertIn(unrelated, offenders(real_range, release_paths),
                      "the closed sets must flag an unrelated production "
                      "path that a real range actually changed")
        for label, real, allowed in (("dirty", dirty, release_paths),
                                     ("head", head_delta, {this_suite}),
                                     ("staged", staged, {this_suite}),
                                     ("untracked", stray, {this_suite})):
            self.assertEqual(
                offenders(real | {unrelated}, allowed), {unrelated},
                "the %s check must fail closed for an unrelated path, and "
                "flag nothing else" % label)

    def test_the_correction_touched_only_its_two_authorized_paths(self):
        """The bounded correction is one named commit sitting directly on
        the fix commit, and it MODIFIED exactly two paths -- adding and
        deleting nothing. Pinned to that commit and its parent, so no
        authorized later commit can dilute the measurement."""
        fix = "d2e484af75a1959eb21d0e45c1c8c8f34fc4745e"
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        parent = subprocess.run(
            ["git", "rev-parse", correction + "^"], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(parent, fix,
                         "the bounded correction must sit directly on the "
                         "fix commit it corrects")
        entries = {tuple(line.split("\t")) for line in subprocess.run(
            ["git", "diff", "--name-status", fix, correction], cwd=_REPO_ROOT,
            capture_output=True, text=True,
            check=True).stdout.splitlines() if line.strip()}
        self.assertEqual(
            entries,
            {("M", "scripts/cowork_verification.py"),
             ("M", "scripts/test_m5_claim_crash_reclaim.py")},
            "the bounded correction must modify exactly its two authorized "
            "paths, adding and deleting nothing")
        self.assertFalse({entry[-1] for entry in entries}
                         - ALLOWED_CHANGED_PATHS)

    def test_the_new_test_module_is_the_only_new_file(self):
        """Commit-pinned file creation: across the whole claim-crash
        package this suite is the only file added and nothing is deleted;
        the only other file added anywhere between the signed base and the
        signed release is the claim-liveness wiring package's own new
        suite, added by that one named commit. In the live tree nothing
        untracked survives except Python bytecode caches."""
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        wiring = "f909122e1cc6d67098e65a443b57a7257c63d3f4"
        release = "3f5724ac24c19b0d8b9243b81d31d4cc0cc906f2"

        def filtered(kind, a, b):
            return {line.split("\t")[-1].strip() for line in subprocess.run(
                ["git", "diff", "--name-status", "--diff-filter=" + kind,
                 a, b], cwd=_REPO_ROOT, capture_output=True, text=True,
                check=True).stdout.splitlines() if line.strip()}

        self.assertEqual(filtered("A", BASE_SHA, correction),
                         {"scripts/test_m5_claim_crash_reclaim.py"},
                         "this suite must be the only file the claim-crash "
                         "package adds")
        self.assertEqual(filtered("A", wiring + "^", wiring),
                         {"scripts/test_m5_claim_liveness_wiring.py"},
                         "the wiring package's new suite must belong to that "
                         "one named commit")
        self.assertEqual(filtered("A", BASE_SHA, release),
                         {"scripts/test_m5_claim_crash_reclaim.py",
                          "scripts/test_m5_claim_liveness_wiring.py"},
                         "no other new file may appear between the signed "
                         "base and the signed release")
        self.assertFalse(filtered("D", BASE_SHA, release),
                         "no file may be deleted between the signed base and "
                         "the signed release")
        untracked = {p.strip() for p in subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            check=True).stdout.splitlines() if p.strip()}
        stray = {p for p in untracked
                 if not (p.endswith(".pyc") and "__pycache__/" in p)}
        self.assertFalse(stray,
                         "no new untracked file may appear beside this "
                         "suite's own authorized restatement: %s"
                         % sorted(stray))

    def test_excluded_production_paths_are_byte_identical_to_the_base(self):
        """Commit-pinned negative control, restated cumulatively.

        Across its whole range the claim-crash package left every forbidden
        production path byte-identical to the signed base. Exactly one
        excluded path ever changed afterwards -- `scripts/cowork.py`, under
        the claim-liveness wiring package's own separate authority -- and it
        is pinned to that single commit: untouched right up to its parent,
        untouched again after it. Every other excluded path is STILL
        byte-identical to the signed base at the signed release, and the
        live tree adds no dirty production edit on top of any of them."""
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        wiring = "f909122e1cc6d67098e65a443b57a7257c63d3f4"
        release = "3f5724ac24c19b0d8b9243b81d31d4cc0cc906f2"
        for rel in EXCLUDED_PATHS:
            base_bytes = _git_show_bytes(BASE_SHA, rel)
            self.assertEqual(
                _git_show_bytes(correction, rel), base_bytes,
                "%s must be byte-identical to the signed base across the "
                "whole claim-crash package" % rel)
            if rel == "scripts/cowork.py":
                self.assertEqual(
                    _git_show_bytes(wiring + "^", rel), base_bytes,
                    "%s must be byte-identical to the signed base right up "
                    "to the wiring package that owns it" % rel)
                self.assertNotEqual(
                    _git_show_bytes(wiring, rel), base_bytes,
                    "the wiring package must be the commit that actually "
                    "changed %s -- otherwise this control is vacuous" % rel)
                self.assertEqual(
                    _git_show_bytes(release, rel),
                    _git_show_bytes(wiring, rel),
                    "no commit after the wiring package may touch %s" % rel)
            else:
                self.assertEqual(
                    _git_show_bytes(release, rel), base_bytes,
                    "%s must still be byte-identical to the signed base at "
                    "the signed release" % rel)
            self.assertEqual(
                _read_local_bytes(rel), _git_show_bytes(release, rel),
                "%s must match the signed release byte for byte in the live "
                "tree" % rel)

    def test_every_pre_existing_test_file_is_byte_identical_to_the_base(self):
        """Commit-pinned and cumulative: the claim-crash package and the
        claim-liveness wiring package each left every pre-existing test file
        byte-identical to the signed base. At the signed release the only
        pre-existing test files that differ are the two rewritten by the one
        named test-only restatement commit -- which touched no production
        file at all, so a test-only follow-up can never be mistaken for a
        production change. Every one of those historical checks is kept
        exactly as it was, at exactly the commits it was pinned to.

        The two authorized serial repairs that follow are then modelled
        EXPLICITLY, commit by commit, rather than by blessing whatever
        happens to differ in the live tree:

          - the scope-gate restatement and the release-pin repair each
            wrote this suite's own file, which is not a pre-existing test
            file at all, so at both of those commits EVERY pre-existing
            test file must still be byte-identical to the signed release;
          - the handoff-facts repair wrote exactly `scripts/test_cowork.py`,
            so that one NAMED file -- and no other -- may differ from the
            signed release afterwards, and it must actually differ, or the
            exception would be vacuous;
          - the live tree is then pinned to that last authorized repair, so
            any drift in any pre-existing test file, in that named file
            included, still fails closed.

        Each of the three serial commits is also held to the exact path set
        it wrote, which is what proves all of them test-only: none touched
        a production path."""
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        wiring = "f909122e1cc6d67098e65a443b57a7257c63d3f4"
        release = "3f5724ac24c19b0d8b9243b81d31d4cc0cc906f2"
        scope_gates = "c5bd555ba74db91b162a5d3d470862d29b8e85da"
        pin_repair = "1645ae22611caa8e333e4c3bdca2bdd62067cef6"
        handoff = "698cda13a62372c0aef683657b4a9a93837616d5"
        this_suite = "scripts/test_m5_claim_crash_reclaim.py"
        restated = {"scripts/test_m5_package_a_contracts.py",
                    "scripts/test_m5_package_b_worker_capture.py"}
        # The one pre-existing test file the authorized serial repairs
        # rewrote, named rather than discovered.
        serial_restated = {"scripts/test_cowork.py"}
        listed = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", BASE_SHA, "scripts/"],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            check=True).stdout.splitlines()
        test_files = [p for p in listed
                      if os.path.basename(p).startswith("test_")
                      and p.endswith(".py")]
        self.assertGreaterEqual(len(test_files), 20)
        self.assertTrue(restated.issubset(set(test_files)))
        self.assertTrue(
            serial_restated.issubset(set(test_files)),
            "the serially repaired file must itself be a pre-existing test "
            "file, or this exception is guarding nothing")
        self.assertNotIn(this_suite, test_files,
                         "this suite is new in the claim-crash package, so "
                         "it is never a pre-existing test file")
        for rel in test_files:
            base_bytes = _git_show_bytes(BASE_SHA, rel)
            self.assertEqual(
                _git_show_bytes(correction, rel), base_bytes,
                "existing test file %s must not change" % rel)
            self.assertEqual(
                _git_show_bytes(wiring, rel), base_bytes,
                "existing test file %s must not change through the wiring "
                "package either" % rel)
            release_bytes = _git_show_bytes(release, rel)
            if rel in restated:
                self.assertNotEqual(
                    release_bytes, base_bytes,
                    "%s is claimed as restated, so it must actually differ "
                    "from the signed base" % rel)
            else:
                self.assertEqual(
                    release_bytes, base_bytes,
                    "existing test file %s must still be byte-identical to "
                    "the signed base at the signed release" % rel)

            # The two authorized serial repairs, modelled commit by commit.
            self.assertEqual(
                _git_show_bytes(scope_gates, rel), release_bytes,
                "the scope-gate restatement must leave pre-existing test "
                "file %s byte-identical to the signed release" % rel)
            self.assertEqual(
                _git_show_bytes(pin_repair, rel), release_bytes,
                "the release-pin repair must leave pre-existing test file "
                "%s byte-identical to the signed release" % rel)
            handoff_bytes = _git_show_bytes(handoff, rel)
            if rel in serial_restated:
                self.assertNotEqual(
                    handoff_bytes, release_bytes,
                    "%s is claimed as serially repaired, so it must "
                    "actually differ from the signed release" % rel)
            else:
                self.assertEqual(
                    handoff_bytes, release_bytes,
                    "existing test file %s must still be byte-identical to "
                    "the signed release at the last authorized repair" % rel)
            self.assertEqual(
                _read_local_bytes(rel), handoff_bytes,
                "existing test file %s must match the last authorized "
                "repair byte for byte in the live tree" % rel)

        def changed(a, b):
            return {p.strip() for p in subprocess.run(
                ["git", "diff", "--name-only", a, b], cwd=_REPO_ROOT,
                capture_output=True, text=True,
                check=True).stdout.splitlines() if p.strip()}

        self.assertEqual(
            changed(release + "^", release), restated,
            "the restatement commit must be test-only: it may touch no "
            "production path at all")
        self.assertEqual(
            changed(release, scope_gates), {this_suite},
            "the scope-gate restatement must be test-only: it wrote this "
            "suite's own file alone")
        self.assertEqual(
            changed(scope_gates, pin_repair), {this_suite},
            "the release-pin repair must be test-only: it wrote this "
            "suite's own file alone")
        self.assertEqual(
            changed(pin_repair, handoff), serial_restated,
            "the handoff-facts repair must be test-only: it wrote exactly "
            "%s" % sorted(serial_restated))

    def test_cowork_state_is_byte_identical_to_the_base(self):
        # Writable only "if genuinely necessary" -- it was not: the
        # checkpoint request/claim path helpers already expose everything
        # the deadline basis and the classifier need.
        self.assertEqual(_read_local_bytes("scripts/cowork_state.py"),
                         _git_show_bytes(BASE_SHA, "scripts/cowork_state.py"))

    def test_only_the_authorized_symbols_were_added(self):
        """Symbol-exact and cumulative. Across its own commit-pinned range
        the claim-crash package added exactly `AUTHORIZED_ADDED_SYMBOLS` and
        removed nothing. The only further top-level symbol in the live
        module is `reconstruct_checkpoint_state_with_liveness`, and it is
        pinned to the one named claim-liveness wiring commit that added it
        under that package's own authority."""
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        wiring = "f909122e1cc6d67098e65a443b57a7257c63d3f4"
        rel = "scripts/cowork_verification.py"
        package_named, _unnamed = _top_level_index(
            _git_show_text(correction, rel))
        self.assertEqual(set(package_named) - set(self.base_named),
                         set(AUTHORIZED_ADDED_SYMBOLS),
                         "unauthorized top-level symbol(s) added/removed")
        self.assertFalse(set(self.base_named) - set(package_named),
                         "the claim-crash package must remove no symbol")

        wiring_before, _unnamed = _top_level_index(
            _git_show_text(wiring + "^", rel))
        wiring_after, _unnamed = _top_level_index(_git_show_text(wiring, rel))
        self.assertEqual(set(wiring_after) - set(wiring_before),
                         {"reconstruct_checkpoint_state_with_liveness"},
                         "the only later addition must belong to the one "
                         "named wiring commit")
        self.assertFalse(set(wiring_before) - set(wiring_after))

        added = set(self.cur_named) - set(self.base_named)
        self.assertEqual(added,
                         set(AUTHORIZED_ADDED_SYMBOLS)
                         | {"reconstruct_checkpoint_state_with_liveness"},
                         "unauthorized top-level symbol(s) added/removed")
        self.assertFalse(set(self.base_named) - set(self.cur_named),
                         "no top-level symbol may be removed at any point")

    def test_only_claim_checkpoint_was_changed(self):
        """Symbol-exact change control, cumulative and commit-pinned. The
        claim-crash package changed exactly `claim_checkpoint` and nothing
        else. The only other pre-existing symbol that differs in the live
        module is `reconstruct_all_checkpoints`, and it differs solely
        because of the one named claim-liveness wiring commit -- which is
        also the last commit to touch the production module at all, so the
        live file is byte-identical to the signed release."""
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        wiring = "f909122e1cc6d67098e65a443b57a7257c63d3f4"
        release = "3f5724ac24c19b0d8b9243b81d31d4cc0cc906f2"
        rel = "scripts/cowork_verification.py"
        package_named, _unnamed = _top_level_index(
            _git_show_text(correction, rel))
        package_changed = {name for name, text in self.base_named.items()
                           if package_named.get(name) != text}
        self.assertEqual(package_changed, set(AUTHORIZED_CHANGED_SYMBOLS),
                         "production symbol(s) changed outside the authorized "
                         "region: %s" % sorted(package_changed))

        wiring_before, _unnamed = _top_level_index(
            _git_show_text(wiring + "^", rel))
        wiring_after, _unnamed = _top_level_index(_git_show_text(wiring, rel))
        self.assertEqual({name for name, text in wiring_before.items()
                          if wiring_after.get(name) != text},
                         {"reconstruct_all_checkpoints"},
                         "the only later production edit must be the one the "
                         "named wiring commit owns")

        changed = {name for name, text in self.base_named.items()
                   if self.cur_named.get(name) != text}
        self.assertEqual(changed,
                         set(AUTHORIZED_CHANGED_SYMBOLS)
                         | {"reconstruct_all_checkpoints"},
                         "production symbol(s) changed outside the authorized "
                         "region: %s" % sorted(changed))
        self.assertEqual(self.current_source, _git_show_text(release, rel),
                         "the live production module must be byte-identical "
                         "to the signed release")

    def test_no_top_level_symbol_was_removed(self):
        self.assertFalse(set(self.base_named) - set(self.cur_named))

    def test_no_anonymous_top_level_statement_changed(self):
        self.assertEqual(self.base_unnamed, self.cur_unnamed,
                         "imports/try-blocks/module guards must be untouched")

    def test_reconstruct_checkpoint_state_is_byte_identical(self):
        base_text = self.base_named["reconstruct_checkpoint_state"]
        cur_text = self.cur_named["reconstruct_checkpoint_state"]
        self.assertEqual(cur_text, base_text)
        self.assertEqual(
            hashlib.sha256(cur_text.encode("utf-8")).hexdigest(),
            hashlib.sha256(base_text.encode("utf-8")).hexdigest())

    def test_publish_checkpoint_receipt_is_byte_identical(self):
        self.assertEqual(self.cur_named["publish_checkpoint_receipt"],
                         self.base_named["publish_checkpoint_receipt"])

    def test_claim_checkpoint_actually_changed(self):
        # Non-vacuous: the authorized region really did change, so the
        # confinement gates above are not passing on an untouched file.
        self.assertNotEqual(self.cur_named["claim_checkpoint"],
                            self.base_named["claim_checkpoint"])
        self.assertIn("lease_deadline_at", self.cur_named["claim_checkpoint"])
        self.assertIn("CHECKPOINT_CLAIM_LEASE_GRACE_S",
                      self.cur_named["claim_checkpoint"])

    def test_the_production_module_never_imports_cowork_activity(self):
        for node in ast.walk(ast.parse(self.current_source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("cowork_activity", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("cowork_activity", node.module or "")
        # No dynamic import either: the name never appears in any live
        # (non-docstring) string constant. Docstring PROSE may name it --
        # this module's own classifier documents why it stays uncoupled.
        for literal in _non_docstring_strings(self.current_source):
            self.assertNotIn("cowork_activity", literal)
            self.assertNotIn("ACTIVITY_CLASS", literal)
        self.assertNotIn("cowork_activity", sys.modules.get(
            "cowork_verification").__dict__)

    def test_the_abandoned_vocabulary_is_never_written_into_a_claim(self):
        # The constant still exists (removing it would itself be an
        # unauthorized edit), but nothing assigns it to a claim's `state`.
        for name in ("claim_checkpoint", "classify_checkpoint_claim_liveness",
                     "publish_checkpoint_receipt",
                     "reconstruct_checkpoint_state"):
            self.assertNotIn("CHECKPOINT_CLAIM_ABANDONED",
                             self.cur_named[name],
                             "%s must never reference the dead abandoned "
                             "vocabulary" % name)
        self.assertNotIn('"abandoned"',
                         self.cur_named["classify_checkpoint_claim_liveness"])

    def test_exactly_one_new_module_level_constant_was_added(self):
        added_assignments = {
            name for name in set(self.cur_named) - set(self.base_named)
            if not self.cur_named[name].lstrip().startswith(
                ("def ", "async def ", "class "))}
        self.assertEqual(added_assignments, {"CHECKPOINT_CLAIM_LEASE_GRACE_S"})
        self.assertNotIn("CHECKPOINT_CLAIM_DEFAULT_TIMEOUT_S",
                         self.cur_named,
                         "the correction replaced the finite-default "
                         "constant with the bounded lease grace; there must "
                         "not be two new module-level constants")

    def test_exactly_one_new_sibling_classifier_was_added(self):
        """Commit-pinned: the claim-crash package added exactly one new
        top-level `def`, the classifier. The only other new `def` in the
        live module is the reader the named claim-liveness wiring commit
        added, so the cumulative count is two and both are real callables."""
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        wiring = "f909122e1cc6d67098e65a443b57a7257c63d3f4"
        rel = "scripts/cowork_verification.py"
        package_named, _unnamed = _top_level_index(
            _git_show_text(correction, rel))
        added_defs = {
            name for name in set(package_named) - set(self.base_named)
            if package_named[name].lstrip().startswith("def ")}
        self.assertEqual(added_defs, {"classify_checkpoint_claim_liveness"})

        wiring_before, _unnamed = _top_level_index(
            _git_show_text(wiring + "^", rel))
        wiring_after, _unnamed = _top_level_index(_git_show_text(wiring, rel))
        self.assertEqual(
            {name for name in set(wiring_after) - set(wiring_before)
             if wiring_after[name].lstrip().startswith("def ")},
            {"reconstruct_checkpoint_state_with_liveness"})

        cumulative_defs = {
            name for name in set(self.cur_named) - set(self.base_named)
            if self.cur_named[name].lstrip().startswith("def ")}
        self.assertEqual(cumulative_defs,
                         {"classify_checkpoint_claim_liveness",
                          "reconstruct_checkpoint_state_with_liveness"})
        self.assertTrue(callable(
            verification.classify_checkpoint_claim_liveness))
        self.assertTrue(callable(
            verification.reconstruct_checkpoint_state_with_liveness))

    def test_the_changed_python_files_compile(self):
        import py_compile
        for rel in ("scripts/cowork_verification.py",
                    "scripts/test_m5_claim_crash_reclaim.py"):
            py_compile.compile(os.path.join(_REPO_ROOT, rel), doraise=True)

    def test_head_is_a_clean_scoped_successor_of_the_signed_release(self):
        """HEAD descends from the signed release along the exact signed
        first-parent chain; the whole authorized serial repair history
        stays inside the closed two-path M5 repair set, and the separately
        authorized skill release stays inside its own closed
        fourteen-path set.

        A committed test file CANNOT truthfully pin its own containing
        commit: that hash is only determined once the file's bytes are
        final, so a `HEAD == <literal>` / `HEAD^{tree} == <literal>` pin is
        unsatisfiable in the very commit that carries it. Nothing below
        pins this successor's own hash or tree. What it DOES pin is
        everything the user's authorized history has already made knowable
        and immutable:

          - the signed chain is exact, first parent by first parent, from
            the signed base through the fix, the bounded correction and the
            wiring package to the test-only restatement `3f5724ac...`,
            which still carries its own signed tree, and on through the
            separately authorized, separately reviewed serial repairs
            `c5bd555...` -> `1645ae2...` -> `698cda1...` -> `554ae1d...`,
            the last of which still carries its own signed tree too, and
            then through the separately authorized skill alignment
            `d994c2f...` to the empty release-boundary commit
            `a89b6037...`;
          - each known repair is held to the exact path it was authorized
            to write: the scope-gate restatement, the release-pin repair
            and the serial release-confinement repair wrote this suite's
            own file, the handoff-facts repair wrote
            `scripts/test_cowork.py`, and none of them wrote anything else.
            The admitted M5 repair set is those two paths and nothing else;
            any third path still fails closed;
          - the skill alignment is NOT an M5 repair and is not admitted by
            that authority. It is pinned to its own closed set of exactly
            fourteen LITERAL `skills/` paths -- enumerated here, never
            derived from whatever git reports today and never a
            `skills/**` prefix wildcard, so real tracked siblings under
            `skills/` that it did not write are still rejected. That is the
            fact the previous revision of this test got wrong: it confined
            EVERY successor of `698cda1...` to this suite alone, so the
            authorized skill release failed it closed on the exact history
            the user authorized. The boundary commit on top of it is
            proven EMPTY -- no changed path, and the alignment's own tree;
          - past `a89b6037...` the history is unknown, so it is held to the
            narrowest authorized rule rather than to a literal: every such
            commit must be an ordinary single-parent commit, signed by the
            same key, changing this suite's own file and nothing else --
            the release-pin suite is the only path the remaining correction
            authority covers, so even `scripts/test_cowork.py` and every
            authorized skill path are rejected there;
          - the index, the working tree and every untracked file may differ
            from HEAD only in this suite's own file, and the working tree
            may differ from the prior signed release only within the two
            closed sets -- so an unrelated committed, staged, dirty or
            untracked path fails this test closed;
          - every commit in the chain, HEAD included, carries an SSH
            signature whose embedded public key is byte-identical to the
            key that signed the release.

        SIGNATURE BOUNDARY, stated rather than papered over with a
        tautology: `git verify-commit` needs `gpg.ssh.allowedSignersFile`
        to be configured and present, and this package may not write git
        configuration, so CRYPTOGRAPHIC allowed-signers verification stays
        with the supervisor-owned release gate. Embedded signer-key
        equality is STRUCTURAL EVIDENCE ONLY -- the signature exists, is a
        real SSHSIG blob, and names the same signer as the signed release
        -- read straight out of the commit objects, so no git configuration
        is consulted or written to obtain it."""
        fix = "d2e484af75a1959eb21d0e45c1c8c8f34fc4745e"
        correction = "3c1d91c4a56271efb60c7d0c435f71ec8dc86c59"
        wiring = "f909122e1cc6d67098e65a443b57a7257c63d3f4"
        release = "3f5724ac24c19b0d8b9243b81d31d4cc0cc906f2"
        release_tree = "35fedfdfb9f4c5c3df4a5fd86471882342f01392"
        scope_gates = "c5bd555ba74db91b162a5d3d470862d29b8e85da"
        pin_repair = "1645ae22611caa8e333e4c3bdca2bdd62067cef6"
        handoff = "698cda13a62372c0aef683657b4a9a93837616d5"
        handoff_tree = "63aed89ed968925a2ddab704e3f74160ddb554ec"
        serial_repair = "554ae1d849c3c78726e08cb6fd639eb52d8140ac"
        serial_repair_tree = "877a836a4bbe31473971219b433974ef305d9796"
        skill_alignment = "d994c2f36618302207a201bdcf6f0dd67202af5d"
        skill_alignment_tree = "f2a7c86ce66dd55094e5d1d8e790a8df97c23304"
        boundary = "a89b6037cbbd4424dca7ffb2b138a8cc3eeea8da"
        this_suite = "scripts/test_m5_claim_crash_reclaim.py"
        handoff_suite = "scripts/test_cowork.py"
        # The closed set the authorized serial repair history may write,
        # the separate closed set the authorized skill release may write,
        # and the strictly narrower set the one remaining correction
        # authority may write.
        repair_paths = {this_suite, handoff_suite}
        skill_paths = {
            "skills/cowork-cli/SKILL.md",
            "skills/cowork-cli/references/live-supervision.md",
            "skills/cowork-debug/SKILL.md",
            "skills/cowork-orchestrate/SKILL.md",
            "skills/cowork-orchestrate/references/backend-gate-pointer.md",
            "skills/cowork-orchestrate/references/backend-gate.md",
            "skills/cowork-orchestrate/tests/test_select_backend.py",
            "skills/cowork-refactor-orchestrator/SKILL.md",
            "skills/cowork-refactor-orchestrator/agents/openai.yaml",
            "skills/cowork-refactor-orchestrator/references/"
            "artifact-contract-schema.md",
            "skills/cowork-refactor-orchestrator/references/"
            "bootstrap-backend.md",
            "skills/cowork-refactor-orchestrator/references/"
            "cowork-backend-gate.md",
            "skills/cowork-refactor-orchestrator/scripts/orchestrate.py",
            "skills/cowork-refactor-orchestrator/tests/test_orchestrate.py",
        }
        release_paths = repair_paths | skill_paths
        scoped = {this_suite}
        # A path no M5 repair package may write under any allowance;
        # the negative controls below are pointed at it.
        unrelated = "scripts/cowork.py"
        # Real tracked siblings under `skills/` the alignment never wrote.
        unauthorized_skills = {
            "skills/cowork-internal/SKILL.md",
            "skills/cowork-orchestrate/agents/openai.yaml",
            "skills/cowork-orchestrate/scripts/select_backend.py",
        }

        def git(*args):
            return subprocess.run(
                ["git"] + list(args), cwd=_REPO_ROOT, capture_output=True,
                text=True, check=True).stdout

        def rev(spec):
            return git("rev-parse", spec).strip()

        def paths(*args):
            return {p.strip()
                    for p in git("diff", "--name-only", *args).splitlines()
                    if p.strip()}

        def parents_of(spec):
            return git("rev-list", "--parents", "-n", "1", spec).split()[1:]

        def offenders(changed, allowed):
            """THE confinement predicate -- one implementation for every
            layer: whatever falls outside the closed set handed to it. It
            is applied identically to the committed successor range, each
            unknown successor commit, the index, the working tree and the
            untracked set, so every one of them fails closed on an
            unadmitted path the same way."""
            return set(changed) - set(allowed)

        def signing_key(spec):
            """The public key embedded in a commit's OWN SSH signature,
            parsed out of the raw commit object -- no git configuration is
            read or written to obtain it (see the boundary above)."""
            import base64
            import struct
            armor, grabbing = [], False
            for line in git("cat-file", "commit", spec).split("\n"):
                if line.startswith("gpgsig "):
                    grabbing = True
                    armor.append(line[len("gpgsig "):])
                elif grabbing and line.startswith(" "):
                    armor.append(line[1:])
                elif grabbing:
                    break
            self.assertTrue(armor, "%s carries no signature header" % spec)
            blob = base64.b64decode("".join(
                line for line in armor if not line.startswith("-----")))
            self.assertEqual(blob[:6], b"SSHSIG",
                             "%s is not carrying a real SSH signature"
                             % spec)
            key_len = struct.unpack(">I", blob[10:14])[0]
            return blob[14:14 + key_len]

        self.assertEqual(len(skill_paths), 14,
                         "the authorized skill-release set is exactly "
                         "fourteen literal paths")
        self.assertTrue(repair_paths.isdisjoint(skill_paths),
                        "the M5 repair authority and the skill-release set "
                        "must stay distinct closed sets")

        # 1. Pure history: the exact signed first-parent chain, the prior
        #    signed release's own tree, both authorized serial repairs and
        #    the authorized skill release. Nothing here can be invalidated
        #    by this successor's own bytes, because every one of these
        #    commits is already signed and immutable.
        for child, parent in ((fix, BASE_SHA), (correction, fix),
                              (wiring, correction), (release, wiring),
                              (scope_gates, release),
                              (pin_repair, scope_gates),
                              (handoff, pin_repair),
                              (serial_repair, handoff),
                              (skill_alignment, serial_repair),
                              (boundary, skill_alignment)):
            self.assertEqual(rev(child + "^"), parent,
                             "%s must sit directly on %s" % (child, parent))
            self.assertEqual(len(parents_of(child)), 1,
                             "%s must be an ordinary single-parent commit, "
                             "not a merge" % child)
        self.assertEqual(rev(release + "^{tree}"), release_tree,
                         "the prior signed release must still carry its "
                         "signed tree")
        self.assertEqual(rev(handoff + "^{tree}"), handoff_tree,
                         "the handoff-facts repair must still carry its own "
                         "signed tree")
        self.assertEqual(rev(serial_repair + "^{tree}"), serial_repair_tree,
                         "the last signed M5 repair must still carry its "
                         "own signed tree")
        self.assertEqual(rev(skill_alignment + "^{tree}"),
                         skill_alignment_tree,
                         "the skill alignment must still carry its own "
                         "signed tree")

        # 2. Each known repair wrote EXACTLY the one path it was
        #    authorized to write, and their union is exactly the closed M5
        #    repair path set -- measured from git, not restated from the
        #    literals above.
        self.assertEqual(paths(release, scope_gates), scoped,
                         "the scope-gate restatement must have written this "
                         "suite's own file alone")
        self.assertEqual(paths(scope_gates, pin_repair), scoped,
                         "the release-pin repair must have written this "
                         "suite's own file alone")
        self.assertEqual(paths(pin_repair, handoff), {handoff_suite},
                         "the handoff-facts repair must have written %s "
                         "alone" % handoff_suite)
        self.assertEqual(paths(handoff, serial_repair), scoped,
                         "the serial release-confinement repair must have "
                         "written this suite's own file alone")
        self.assertEqual(paths(release, scope_gates)
                         | paths(scope_gates, pin_repair)
                         | paths(pin_repair, handoff)
                         | paths(handoff, serial_repair),
                         repair_paths,
                         "the authorized serial repair history must write "
                         "exactly the closed M5 repair path set")

        # 2b. The separately authorized skill release: exactly its own
        #     fourteen literal paths, then an EMPTY boundary commit that
        #     changed nothing at all and carries the alignment's own tree.
        self.assertEqual(paths(serial_repair, skill_alignment), skill_paths,
                         "the skill alignment must have written exactly the "
                         "fourteen authorized skill paths")
        self.assertEqual(paths(skill_alignment, boundary), set(),
                         "the release-boundary commit must be empty")
        self.assertEqual(rev(boundary + "^{tree}"),
                         rev(skill_alignment + "^{tree}"),
                         "the empty release-boundary commit must carry the "
                         "skill alignment's own tree")
        self.assertEqual(paths(release, boundary), release_paths,
                         "the current signed release must change exactly "
                         "the closed M5 repair set plus the closed "
                         "skill-release set")

        # 2c. The skill-release set is a SET, not a `skills/**` prefix:
        #     real tracked siblings under `skills/` that the alignment did
        #     not write are rejected by the widest allowance in this test,
        #     and no authorized skill path is admitted as M5 repair
        #     authority or as an unknown-successor path.
        tracked_skills = {p.strip() for p
                          in git("ls-files", "--", "skills/").splitlines()
                          if p.strip()}
        for path in sorted(unauthorized_skills):
            self.assertIn(path, tracked_skills,
                          "%s must be a real tracked path for this control "
                          "to mean anything" % path)
            self.assertNotIn(path, skill_paths,
                             "%s is not part of the authorized skill set"
                             % path)
            self.assertEqual(offenders({path}, release_paths), {path},
                             "%s must fail closed -- the skill set is not a "
                             "prefix wildcard" % path)
        for path in sorted(skill_paths):
            self.assertEqual(offenders({path}, repair_paths), {path},
                             "%s is release evolution, never M5 repair "
                             "authority" % path)
            self.assertEqual(offenders({path}, scoped), {path},
                             "%s may never be written by an unknown "
                             "successor" % path)

        # 3. HEAD is the release boundary, or a first-parent descendant of
        #    it: no fork, no rewrite, no merge splicing in a second
        #    lineage.
        first_parents = [line.strip() for line
                         in git("rev-list", "--first-parent", "HEAD")
                         .splitlines() if line.strip()]
        for label, commit in (("prior signed release", release),
                              ("handoff-facts repair", handoff),
                              ("last signed M5 repair", serial_repair),
                              ("skill alignment", skill_alignment),
                              ("release boundary", boundary)):
            self.assertIn(commit, first_parents,
                          "the %s must be HEAD itself or a first-parent "
                          "ancestor of HEAD" % label)
        successors = first_parents[:first_parents.index(boundary)]

        # 4. Same signer across the whole chain, unknown successors
        #    included (structural evidence only -- see the boundary above).
        release_key = signing_key(release)
        for commit in [BASE_SHA, fix, correction, wiring, scope_gates,
                       pin_repair, handoff, serial_repair, skill_alignment,
                       boundary] + successors:
            self.assertEqual(
                signing_key(commit), release_key,
                "%s must be signed by the same key as the signed release"
                % commit)

        # 5. Every UNKNOWN commit on top of the release boundary is an
        #    ordinary single-parent commit confined to this suite's own
        #    file -- the only path the remaining correction authority
        #    covers, which is strictly narrower than either closed set.
        successor_paths = set()
        for commit in successors:
            parents = parents_of(commit)
            self.assertEqual(len(parents), 1,
                             "%s must be an ordinary single-parent commit, "
                             "not a merge" % commit)
            changed = paths(parents[0], commit)
            successor_paths |= changed
            stray = offenders(changed, scoped)
            self.assertFalse(
                stray,
                "commit %s changed path(s) outside this suite's own file: "
                "%s" % (commit, sorted(stray)))

        # 6. Cumulative confinement, the same predicate at every layer:
        #    the committed successor range against the two closed sets,
        #    the index/working tree and the untracked set against HEAD at
        #    this suite's own file, and the working tree against the prior
        #    signed release at the two closed sets.
        committed = paths(release, "HEAD")
        self.assertFalse(
            offenders(committed, release_paths),
            "commits on top of the prior signed release changed path(s) "
            "outside the closed M5 repair set and the closed skill-release "
            "set: %s" % sorted(offenders(committed, release_paths)))
        self.assertEqual(
            committed, release_paths,
            "the committed successor range must change exactly the closed "
            "M5 repair path set plus the closed skill-release path set")
        staged = paths("--cached")
        self.assertFalse(
            offenders(staged, scoped),
            "nothing but this suite's own file may ever be staged: %s"
            % sorted(offenders(staged, scoped)))
        head_delta = paths("HEAD")
        self.assertFalse(
            offenders(head_delta, scoped),
            "the index and working tree may differ from HEAD only in this "
            "suite's own file: %s" % sorted(offenders(head_delta, scoped)))
        dirty = paths(release)
        self.assertFalse(
            offenders(dirty, release_paths),
            "the working tree may differ from the prior signed release "
            "only within the closed M5 repair set and the closed "
            "skill-release set: %s"
            % sorted(offenders(dirty, release_paths)))
        # Untracked files are part of the working tree too: everything but
        # Python bytecode caches is held to the same one path.
        untracked = {p.strip() for p in git(
            "ls-files", "--others", "--exclude-standard").splitlines()
            if p.strip() and not (p.strip().endswith(".pyc")
                                  and "__pycache__/" in p)}
        self.assertFalse(
            offenders(untracked, scoped),
            "untracked path(s) outside this suite's own file that are not "
            "Python bytecode caches: %s" % sorted(offenders(untracked,
                                                            scoped)))

        # Non-vacuity: both known repairs and the whole authorized skill
        # release must really be present in the committed range, and this
        # successor must really EXIST -- as a scoped successor commit or
        # as a dirty file -- otherwise every confinement above would be
        # measuring an empty change.
        self.assertIn(this_suite, committed,
                      "the release-pin repair must be present in the "
                      "committed successor range")
        self.assertIn(handoff_suite, committed,
                      "the handoff-facts repair must be present in the "
                      "committed successor range")
        self.assertFalse(skill_paths - committed,
                         "every authorized skill path must be present in "
                         "the committed successor range: %s"
                         % sorted(skill_paths - committed))
        self.assertIn(this_suite, successor_paths | head_delta,
                      "this successor must be present either as a scoped "
                      "successor commit or in the working tree")

        # Negative control on REAL history, not a fabrication: the very
        # same predicate, pointed at a range that genuinely DID touch an
        # unrelated path (`scripts/cowork.py`, changed by the wiring
        # package under its own separate authority), must report it under
        # EVERY allowance. A predicate that passed here would be vacuous.
        real_range = paths(BASE_SHA, wiring)
        self.assertIn(unrelated, real_range,
                      "the real negative-control range must actually have "
                      "changed %s" % unrelated)
        self.assertIn(
            unrelated, offenders(real_range, release_paths),
            "the closed M5 repair set and the closed skill-release set "
            "must flag an unrelated path that a real range actually "
            "changed")
        self.assertIn(
            unrelated, offenders(real_range, repair_paths),
            "the closed M5 repair set must flag it too")
        self.assertIn(
            unrelated, offenders(real_range, scoped),
            "the release-pin-only rule must flag it too")

        # ... and every real set the predicate guards must fail closed on
        # one unrelated path added to it, at that layer's own allowance.
        for label, real, allowed in (("committed", committed, release_paths),
                                     ("staged", staged, scoped),
                                     ("head", head_delta, scoped),
                                     ("dirty", dirty, release_paths),
                                     ("untracked", untracked, scoped)):
            self.assertEqual(
                offenders(real | {unrelated}, allowed), {unrelated},
                "the %s confinement check must fail closed for an "
                "unrelated path, and flag nothing else" % label)

        # The unknown-successor rule really is the narrower one: the
        # handoff repair's path is admitted in the committed range and
        # REJECTED for any unknown successor, so layer 5 cannot be
        # silently widened into either closed set.
        self.assertFalse(offenders({handoff_suite}, repair_paths),
                         "%s belongs to the closed M5 repair set"
                         % handoff_suite)
        self.assertEqual(offenders({handoff_suite}, scoped), {handoff_suite},
                         "an unknown successor may write this suite's own "
                         "file and nothing else")


if __name__ == "__main__":
    unittest.main()
