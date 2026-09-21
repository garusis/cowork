#!/usr/bin/env python3
"""Durable stranded-claim classification.

A checkpoint claim left behind by a claimant that CRASHED is byte-for-byte
indistinguishable, on disk, from a claim whose executor is still running --
`reconstruct_checkpoint_state` reports `"claimed"` for both -- so the
classifier must distinguish a stranded checkpoint (`process_crash`/
`hung_descendant`) from ongoing work or no-evidence silence by other durable
evidence.

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
  - the dead `CHECKPOINT_CLAIM_ABANDONED` vocabulary is never written into a
    claim, and `cowork_activity.py` is never imported by the production
    module.

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

# The criterion-5 classes a stranded claim must land in, and the three it may
# never land in. Cross-checked against the REAL closed taxonomy in
# `cowork_activity.ACTIVITY_CLASSES` -- which the production module itself
# never imports (proven in `ClaimModuleBoundaryTests`).
CRASH_CLASSES = ("process_crash", "hung_descendant")
FORBIDDEN_FOR_A_STRAND = ("productive_model_work", "provider_wait",
                          "no_evidence_silence")

GRACE_S = verification.CHECKPOINT_CLAIM_LEASE_GRACE_S


def _read_local_bytes(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), "rb") as fh:
        return fh.read()


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
        """Stranded-claim classification, end to end and non
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


# =========================================================================== #
# Claim module boundary invariants.                                           #
# =========================================================================== #

class ClaimModuleBoundaryTests(unittest.TestCase):
    """Product invariants of the claim module: it stays uncoupled from the
    activity vocabulary and never writes the dead abandoned state."""

    def setUp(self):
        self.current_source = _read_local_bytes(
            "scripts/cowork_verification.py").decode("utf-8")
        self.cur_named, _unnamed = _top_level_index(self.current_source)

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


if __name__ == "__main__":
    unittest.main()
