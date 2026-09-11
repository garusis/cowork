#!/usr/bin/env python3
"""Focused suite for issue #64 implementation package **P5** -- the eleven
negative controls, the evaluation-drain fixture, and the machine-checked
fixture matrix -- on the frozen base
`3e3bdba12abacdf1a5fc0c9301200adc5e3171c4`.

P5 adds **ZERO production delta**. This module and its sibling
`test_m55_owner_crash_reclaim.py` are the only two paths in the repository that
differ from that base; the sibling's `ScopeConfinementTests` proves it.

A refusal gate is only as good as the things it must NOT refuse, and the things
it must not let through anonymously. That is what lives here:

  - **N1.** A full three-phase `--no-session` flow, INCLUDING its evaluation
    drains, leases nothing, binds nothing, raises no `OwnerLeaseError` of any
    subclass, and is never `enforced` at any governed seam.

  - **N2.** The four read-only surfaces (`--check`, `--report`,
    `--evaluate-role`, `--session-owner`) each exit 0 under a LIVE foreign
    owner, acquire no lease (the lease file's bytes are identical across the
    query) and construct no controller. `--evaluate-role` additionally reaches
    no drain frame at all.

  - **N3.** The converse of N1: an owned run is `enforced` at EVERY
    `_advance_phase` entry and EVERY evaluation-region entry, with both counts
    asserted non-zero so a flow that never reached a seam cannot pass
    vacuously, and its lease released exactly once.

  - **N4.** A legacy session anchor with no lease acquires cleanly at epoch 1
    and produces NO migration artifact of any name.

  - **N5.** An owner parked at a human gate with zero sends in flight stays
    `live_owner` across more than three full deadlines of injected time, as
    seen by a genuinely SEPARATE OBSERVER PROCESS -- including one pretending
    to be on a foreign host, where the pid-alive shortcut is unavailable.

  - **N6.** The frozen persistence seam: `save_role_session`'s exact
    five-parameter signature, its characterization file's byte identity, and
    the durable output of the real `role_saver` closure both unenforced and
    enforced.

  - **N7.** A nested run restores all five outer context fields verbatim --
    including `provider_conflict` -- even when the INNER run recorded and
    drained a conflict of its own.

  - **N8.** The evaluator exemption is exactly one dispatch site, and a full
    owned run's measurement boundaries drain with zero owner exceptions.

  - **N9.** Rule E4's declared out-of-scope enqueue surface, BOUNDED rather
    than closed: every enqueue observed after a lease loss must be preceded by
    a governed call that RAISED.

  - **N10.** No owner refusal reaches the send gateway, and every exception the
    three binding functions raise is accounted for -- recorded and drained
    exactly once. Five arms: four ordinary flows, which with fake role runners
    start no turn and therefore reach the gateway structurally zero times, plus
    one that genuinely enters it with a PROVEN provider collision pending, so
    the per-entry check is a measurement rather than a statement about an empty
    set.

  - **N11.** The binding surface is TOTAL over its four failure modes on all
    three functions, reads None only for a genuinely absent record, and its
    teardown failure never displaces a run's real exit code.

  - **F14 (authored here).** The evaluation drain under a lost lease, its
    converse, and the deliberate mid-drain bound -- authored locally because no
    runtime witness for it exists anywhere in the tree and N10's accepted span
    is defined over it.

  - **`FixtureMatrixTests`.** The 19-row fixture matrix is a module-level
    mapping that a test RESOLVES: locally-owned rows must name a test method
    that exists in one of the two new modules, and mapped rows must name a
    (frozen file, class, test) triple that exists, resolved by `ast` parse.
    A prose-only matrix cannot enforce "no row marked covered without a
    resolvable test id"; this can.

**N10's mapped span, disclosed.** N10's accepted definition also spans F8's
provider-exclusivity arms. Those are already carried by
`scripts/test_m55_owner_exclusivity.py::TypedPropagationTests::
test_n10_nothing_anonymous_reaches_the_send_gateway`, a CLOSED package's frozen
suite. P5 does NOT re-execute them -- re-driving them would replay P3 -- and
does not silently drop them either: they are named in `FIXTURE_MATRIX` and
stated here.

Every fixture redirects `COWORK_SESSIONS_ROOT` into a fresh `tempfile.mkdtemp`,
injects `now=` rather than sleeping toward a deadline, drives the real
production functions with fake ROLE RUNNERS so no provider is ever contacted,
and registers every child it spawns. `COWORK_LIVE` is never set.

Run standalone:

    python3 -m unittest scripts.test_m55_owner_negative_controls -v
"""

import ast
import datetime
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_REPO_ROOT = os.path.dirname(_HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_eval as evaluation  # noqa: E402
import cowork_owner as owner  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

# The SIBLING module, bound as a MODULE OBJECT under a private name -- never
# `from ... import <TestCase>`. `unittest.TestLoader.loadTestsFromModule`
# collects any `TestCase` subclass that is an ATTRIBUTE of the module under
# test, so a `from`-import would silently re-run the sibling's whole suite
# inside this module, inflating this module's reported test count. A bound
# module object is not a `TestCase`, so it is never collected.
import test_m55_owner_crash_reclaim as _crash_reclaim  # noqa: E402

# The frozen base this package is bound to -- the same single literal the
# sibling pins, guarded there by `merge-base --is-ancestor`.
BASE_SHA = "3e3bdba12abacdf1a5fc0c9301200adc5e3171c4"

# P5's write authority, exactly. Cross-checked against the sibling's copy by
# `FixtureMatrixTests`.
ALLOWED_CHANGED_PATHS = frozenset({
    "scripts/test_m55_owner_negative_controls.py",
    "scripts/test_m55_owner_crash_reclaim.py",
})

# The persistence seam frozen by `test_cowork_state_m3.py`'s characterization
# suite: the EXACT parameter list, in order.
SAVE_ROLE_SESSION_PARAMETERS = ["path", "role", "controller", "session_id",
                                "prior"]
STATE_CHARACTERIZATION_FILE = "scripts/test_cowork_state_m3.py"

# The C5 failure set the three binding functions must be TOTAL over.
TRANSLATED_FAILURES = (
    ("TimeoutError", TimeoutError("p5 injected lock timeout")),
    ("OSError", OSError("p5 injected io failure")),
    ("CorruptRecordError",
     state_store.CorruptRecordError("p5 injected corrupt record")),
    ("ValueError", ValueError("p5 injected rejected identifier")),
)

# N5's injected clock: renew this many virtual seconds at a time, for long
# enough to cross more than three full deadlines
# (DEFAULT_HEARTBEAT_INTERVAL_S + DEFAULT_LEASE_GRACE_S = 150s).
N5_VIRTUAL_STEP_S = owner.DEFAULT_HEARTBEAT_INTERVAL_S
N5_DEADLINE_S = (owner.DEFAULT_HEARTBEAT_INTERVAL_S
                 + owner.DEFAULT_LEASE_GRACE_S)
N5_REQUIRED_VIRTUAL_S = 3 * N5_DEADLINE_S
N5_FIRES = (N5_REQUIRED_VIRTUAL_S // N5_VIRTUAL_STEP_S) + 1
N5_OBSERVE_EVERY = 4

# The full team P5's flow fixtures drive, so the run really crosses all three
# phases and all four measurement boundaries.
FULL_TEAM = ("scout", "planner", "builder")


# --------------------------------------------------------------------------- #
# The fixture matrix.                                                           #
#                                                                              #
# 18 assigned IDs plus F14. Every row is either LOCAL (owned by one of P5's two #
# new modules) or MAPPED (already carried by a named test in a frozen suite of  #
# a closed package, with `p5_adds` saying what P5 puts on top). Resolved by     #
# `FixtureMatrixTests`, never merely written down.                              #
# --------------------------------------------------------------------------- #

# `module key -> the two new modules`, used to resolve local rows.
_LOCAL_MODULES = {
    "crash_reclaim": _crash_reclaim,
    "negative_controls": sys.modules[__name__],
}

FIXTURE_MATRIX = {
    "F2": {
        "owner": "local",
        "tests": [("crash_reclaim", "CleanRestartTests",
                   "test_f2_a_real_child_clean_exit_releases_and_the_next_run"
                   "_takes_epoch_two")],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "LifecycleTests",
             "test_a_restart_after_a_clean_exit_acquires_the_next_epoch"),
            ("scripts/test_m55_owner_store.py", "AcquireGateTests",
             "test_clean_restart_acquires_epoch_two_without_takeover"),
        ],
        "p5_adds": "the REAL separate-process clean exit",
    },
    "F3": {
        "owner": "local",
        "tests": [
            ("crash_reclaim", "CrashedOwnerTests",
             "test_f3_a_real_sigkill_leaves_a_live_lease_until_the_deadline"),
            ("crash_reclaim", "CrashedOwnerTests",
             "test_f3_after_the_deadline_take_over_selects_proved_dead_and"
             "_bumps_the_epoch"),
        ],
        "mapped": [
            ("scripts/test_m55_owner_store.py", "AcquireGateTests",
             "test_stale_dead_owner_refuses_and_is_never_implicitly_reclaimed"),
            ("scripts/test_m55_owner_gate.py", "TakeoverWiringTests",
             "test_take_over_reclaims_a_provably_dead_owner_at_the_next_epoch"),
        ],
        "p5_adds": ("a REAL SIGKILL mid-turn, and `select_takeover_mode` "
                    "asserted to be exactly 'proved_dead' rather than "
                    "inferred from the takeover succeeding"),
    },
    "F4": {
        "owner": "local",
        "tests": [("crash_reclaim", "SigtermUnderTakeoverTests",
                   "test_f4_the_predecessor_marks_terminal_and_the_successor"
                   "_lease_is_byte_identical")],
        "mapped": [
            ("scripts/test_m55_owner_store.py", "TerminalMarkTests",
             "test_a_predecessors_mark_after_a_takeover_is_ignored_entirely"),
        ],
        "p5_adds": ("the full run-level consequence set under a real "
                    "concurrent takeover: sidecar + aborted PhaseState + "
                    "run.external_kill + exit 143 + successor byte identity"),
    },
    "F5": {
        "owner": "local",
        "tests": [("crash_reclaim", "PidReuseTests",
                   "test_f5_a_live_reused_pid_classifies_stale_dead_owner_and"
                   "_is_never_signalled")],
        "mapped": [
            ("scripts/test_m55_owner_store.py", "AcquireGateTests",
             "test_pid_reuse_classifies_dead_never_live"),
            ("scripts/test_m55_owner_store.py", "TakeoverTests",
             "test_terminate_prior_never_signals_a_mismatched_pid_start"),
        ],
        "p5_adds": ("a pid re-bound to a NEWLY SPAWNED, still-running process "
                    "rather than a synthetic record"),
    },
    "F11": {
        "owner": "local",
        "tests": [("crash_reclaim", "TakeoverPreservationTests",
                   "test_f11_ten_artifact_classes_are_sha256_identical_across"
                   "_a_real_takeover")],
        "mapped": [],
        "p5_adds": ("entirely new: the ten-artifact byte-identity sweep across "
                    "a real takeover"),
    },
    "F12": {
        "owner": "local",
        "tests": [("crash_reclaim", "TakeoverAbortTests",
                   "test_f12_a_prior_owner_that_survives_term_and_kill_aborts"
                   "_the_takeover")],
        "mapped": [
            ("scripts/test_m55_owner_store.py", "TakeoverTests",
             "test_terminate_prior_aborts_and_leaves_the_lease_byte_identical"),
        ],
        "p5_adds": ("a REAL live victim process surviving both escalation "
                    "steps"),
    },
    "F13": {
        "owner": "local",
        "tests": [("crash_reclaim", "SigtermTakeoverRaceTests",
                   "test_g8_fifty_randomized_interleavings_plus_both"
                   "_deterministic_orders")],
        "mapped": [],
        "p5_adds": "entirely new: gate G8 at its accepted depth",
    },
    "N1": {
        "owner": "local",
        "tests": [("negative_controls", "NoSessionTests",
                   "test_n1_a_full_flow_with_no_session_never_leases_and_never"
                   "_refuses")],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "NegativeControlTests",
             "test_no_session_never_leases_and_never_raises"),
        ],
        "p5_adds": ("a three-phase flow including its evaluation drains, plus "
                    "the zero-bind and enforced-never-True arms"),
    },
    "N2": {
        "owner": "local",
        "tests": [
            ("negative_controls", "ReadOnlySurfaceTests",
             "test_n2_the_four_read_only_surfaces_succeed_lease_free_under_a"
             "_live_owner"),
            ("negative_controls", "ReadOnlySurfaceTests",
             "test_n2_evaluate_role_reaches_no_drain_frame"),
        ],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "ResumeTriggerExitCodeTests",
             "test_the_read_only_preflight_refusals_stay_lease_free"),
        ],
        "p5_adds": "the --evaluate-role no-drain-frame arm",
    },
    "N3": {
        "owner": "local",
        "tests": [("negative_controls", "SingleOwnerTests",
                   "test_n3_every_governed_seam_and_every_evaluation"
                   "_transition_is_enforced")],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "NegativeControlTests",
             "test_an_owned_run_is_enforced_at_every_governed_seam"),
        ],
        "p5_adds": ("the non-vacuity assertions (every governed-seam counter "
                    "strictly > 0, driven by a lead role that really advances "
                    "its own phase) and the release-exactly-once arm"),
    },
    "N4": {
        "owner": "local",
        "tests": [("negative_controls", "LegacySessionTests",
                   "test_n4_a_legacy_session_acquires_at_epoch_one_with_no"
                   "_migration_artifact")],
        "mapped": [],
        "p5_adds": "entirely new",
    },
    "N5": {
        "owner": "local",
        "tests": [
            ("negative_controls", "ParkedOwnerTests",
             "test_n5_a_parked_owner_stays_live_across_three_deadlines_for_a"
             "_separate_observer_process"),
            ("negative_controls", "ParkedOwnerTests",
             "test_n5_a_foreign_host_observer_still_reads_live_owner"),
        ],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "LifecycleTests",
             "test_the_heartbeat_renews_and_a_straggler_cannot_resurrect"),
        ],
        "p5_adds": ("the separate-observer-process and simulated-foreign-host "
                    "arms over more than 3x(heartbeat+grace) of injected "
                    "clock"),
    },
    "N6": {
        "owner": "local",
        "tests": [
            ("negative_controls", "PersistenceSeamTests",
             "test_n6_save_role_session_signature_is_frozen"),
            ("negative_controls", "PersistenceSeamTests",
             "test_n6_the_characterization_file_is_byte_identical_to_the_base"),
            ("negative_controls", "PersistenceSeamTests",
             "test_n6_unenforced_durable_output_is_byte_identical"),
            ("negative_controls", "PersistenceSeamTests",
             "test_n6_enforced_adds_only_the_provider_binding_record"),
        ],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "ScopeConfinementTests",
             "test_save_role_session_is_frozen"),
            ("scripts/test_m55_owner_gate.py", "ScopeConfinementTests",
             "test_the_state_characterization_file_is_untouched"),
        ],
        "p5_adds": "the byte-identical durable-output arms (iii) and (iv)",
    },
    "N7": {
        "owner": "local",
        "tests": [("negative_controls", "NestedRunTests",
                   "test_n7_the_inner_run_restores_all_five_outer_context"
                   "_fields_including_a_drained_conflict")],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "LifecycleTests",
             "test_a_nested_run_restores_the_outer_context_verbatim"),
        ],
        "p5_adds": ("the `provider_conflict` field and the "
                    "inner-run-drained-a-conflict-of-its-own arm"),
    },
    "N8": {
        "owner": "local",
        "tests": [("negative_controls", "EvaluatorExemptionTests",
                   "test_n8_the_exemption_is_one_site_and_drains_without_owner"
                   "_exceptions")],
        "mapped": [
            ("scripts/test_m55_owner_gate.py", "NegativeControlTests",
             "test_the_evaluator_exemption_does_not_leak"),
            ("scripts/test_m55_owner_gate.py", "StaticGateTests",
             "test_the_dispatch_sites_and_the_single_evaluator_exemption"),
        ],
        "p5_adds": "the runtime multi-boundary drain arm alongside the AST count",
    },
    "N9": {
        "owner": "local",
        "tests": [("negative_controls", "EnqueueBoundTests",
                   "test_n9_every_post_loss_enqueue_is_preceded_by_a_raising"
                   "_governed_call")],
        "mapped": [],
        "p5_adds": "entirely new: the enqueue-bound witness for rule E4",
    },
    "N10": {
        "owner": "local + mapped",
        "tests": [("negative_controls", "SendGatewayTests",
                   "test_n10_no_owner_refusal_and_nothing_anonymous_reaches"
                   "_the_send_gateway")],
        "mapped": [
            ("scripts/test_m55_owner_exclusivity.py", "TypedPropagationTests",
             "test_n10_nothing_anonymous_reaches_the_send_gateway"),
        ],
        "p5_adds": ("the span over N1, N3, N11's injected failures and F14, "
                    "plus a fifth arm that actually ENTERS the send gateway "
                    "with a proven provider collision pending -- without it "
                    "property (1) would be asserted over an empty observation "
                    "set on every arm, because P5's fake role runners start no "
                    "turn and therefore never reach `_send`. On those four "
                    "flow arms the property is STRUCTURAL (zero entries, "
                    "asserted) and that is disclosed rather than presented as "
                    "measured. The F8(b)/(c)/(f)/(g) span is PRESERVED BY "
                    "REFERENCE to the mapped test and is deliberately NOT "
                    "re-executed here, because re-driving it would replay a "
                    "closed package"),
    },
    "N11": {
        "owner": "local",
        "tests": [
            ("negative_controls", "BindingSurfaceTests",
             "test_n11_the_four_failure_modes_map_to_provider_binding"
             "_unavailable_on_all_three_functions"),
            ("negative_controls", "BindingSurfaceTests",
             "test_n11_read_returns_none_only_for_an_absent_record"),
            ("negative_controls", "BindingSurfaceTests",
             "test_n11_release_swallows_and_traces_its_translated_exception"
             "_during_teardown"),
        ],
        "mapped": [
            ("scripts/test_m55_owner_store.py", "BindingSurfaceBehaviourTests",
             "test_every_injected_failure_mode_translates_on_every_reader"),
        ],
        "p5_adds": ("the totality sweep as 4 modes x 3 functions in one "
                    "control, plus the teardown-never-displaces-the-exit-code "
                    "arm"),
    },
    "F14": {
        "owner": "local",
        "tests": [
            ("negative_controls", "EvaluationDrainTests",
             "test_f14_a_pre_drain_loss_ends_at_rc_three_with_zero_evaluator"
             "_constructions"),
            ("negative_controls", "EvaluationDrainTests",
             "test_f14_a_valid_lease_drains_normally"),
            ("negative_controls", "EvaluationDrainTests",
             "test_f14_a_mid_drain_loss_is_observed_at_the_next_governed_seam"),
        ],
        "mapped": [],
        "p5_adds": ("entirely new; authored here so N10's cross-fixture span "
                    "is genuine rather than nominal"),
    },
}

ASSIGNED_FIXTURE_IDS = ("F2", "F3", "F4", "F5", "F11", "F12", "F13",
                        "N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "N9",
                        "N10", "N11")


# --------------------------------------------------------------------------- #
# Helpers and doubles, RE-DECLARED LOCALLY (see the sibling's note on why).      #
# --------------------------------------------------------------------------- #


def _git(args, check=False):
    return subprocess.run(
        ["git", "--no-optional-locks"] + list(args),
        cwd=_REPO_ROOT, capture_output=True, check=check)


def _git_show_bytes(rev, rel_path):
    return _git(["show", "%s:%s" % (rev, rel_path)], check=True).stdout


def _live_candidate_repo():
    """`_REPO_ROOT` when this suite runs in the LIVE git candidate, else None.
    Two conditions, for exactly the reason the sibling module records: an
    isolated snapshot HAS git (a real `git init` plus captured index bytes) but
    no history, so a toplevel test alone would not skip -- it would fall
    through to `git show <BASE>:...` against a repository holding no objects.
    """
    try:
        toplevel = _git(["rev-parse", "--show-toplevel"]).stdout.decode(
            "utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not toplevel:
        return None
    if os.path.realpath(toplevel) != os.path.realpath(_REPO_ROOT):
        return None
    try:
        found = _git(["cat-file", "-e", "%s^{commit}" % BASE_SHA])
    except (OSError, subprocess.SubprocessError):
        return None
    return _REPO_ROOT if found.returncode == 0 else None


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as fh:
        return _sha256(fh.read())


def _iso(moment):
    return moment.astimezone(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _lease_file(session_uuid):
    """Composed from `owner_dir_for`, never from `owner_lease_path_for` and
    never from the literal `"owner/lease.json"`: G3d's repo-wide sweep asserts
    that neither spelling ever reaches an `open(...)` anywhere in the
    repository, including in a new test file (test_m55_owner_store.py:260-269).
    """
    return os.path.join(state_store.owner_dir_for(session_uuid), "lease.json")


def _read_raw_lease_bytes(session_uuid):
    path = _lease_file(session_uuid)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def _child_env(root):
    env = dict(os.environ)
    env["COWORK_SESSIONS_ROOT"] = root
    env["COWORK_SCRIPTS"] = _HERE
    env["PYTHONPATH"] = _HERE + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("COWORK_LIVE", None)
    return env


class _Raises(object):
    """A double that fails the test if anything ever calls it. Used wherever
    the assertion is the ABSENCE of a dispatch, a lease or a drain."""

    def __init__(self, label):
        self.label = label

    def __call__(self, *args, **kwargs):
        raise AssertionError("%s was invoked" % self.label)


class _Counter(object):
    """A spy that counts calls and forwards to the real callable, so a fixture
    can assert "no further calls after the refusal" rather than "no calls at
    all" -- the second would be a claim about everything a healthy run
    legitimately did before reaching the seam."""

    def __init__(self, real=None):
        self.real = real
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.real is None:
            return None
        return self.real(*args, **kwargs)

    @property
    def count(self):
        return len(self.calls)


class _PopenLog(object):
    """A `subprocess.Popen` replacement that RECORDS every process creation by
    executable basename and then forwards to the real one.

    Recording rather than raising, where a whole `run_flow` is in scope: a
    healthy run legitimately shells out during its own measurement checkpoints
    long before the seam under test is reached, so a double that raised on call
    would fail a CORRECT candidate. The assertion fixtures make on this log is
    the absolute "no controller process anywhere", which is the claim that
    actually matters."""

    CONTROLLERS = ("claude", "codex", "opencode")

    def __init__(self):
        self.real = subprocess.Popen
        self.calls = []

    def __call__(self, command, *args, **kwargs):
        argv0 = command[0] if isinstance(command, (list, tuple)) else command
        self.calls.append(os.path.basename(str(argv0)))
        return self.real(command, *args, **kwargs)

    @property
    def controller_spawns(self):
        return [name for name in self.calls if name in self.CONTROLLERS]


class _InertSession(object):
    """The session object handed to the instrumented send gateway in N10's
    pending-conflict arm.

    It is INERT and it never needs to work: `cowork._send` is replaced by the
    recording spy for the whole of that arm, so this is only ever the spy's
    first argument. It exists so the call reads as the real call it stands for
    rather than as `_send(None, ...)`, and it carries no `send` method at all
    -- if the spy were ever removed, the arm would fail loudly instead of
    quietly starting a turn."""

    controller = "claude"


# N5's observer: a genuinely SEPARATE PROCESS that re-reads the durable record
# and classifies it at an injected instant handed to it on argv. A separate
# process is the point -- an in-process probe could be reading this process's
# own memory rather than what a second cowork would actually see.
_OBSERVER_CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["COWORK_SCRIPTS"])
import cowork_owner as owner

session_uuid, now_iso, foreign_host = sys.argv[1:4]
if foreign_host:
    # Simulate observing from ANOTHER MACHINE. The pid-alive OR-clause is then
    # unavailable by construction, so a `live_owner` verdict can only come from
    # the lease being inside its own deadline -- which `_classify_record`
    # decides BEFORE the host check (cowork_owner.py:824-826).
    owner.host_id = lambda _h=foreign_host: _h

verdict = owner.classify_owner_lease(session_uuid, now=now_iso)
record = owner.read_owner_lease(session_uuid)
sys.stdout.write(json.dumps({
    "verdict": verdict,
    "host_id": owner.host_id(),
    "heartbeat_at": (record or {}).get("heartbeat_at"),
    "lease_deadline_at": (record or {}).get("lease_deadline_at"),
}))
'''


# --------------------------------------------------------------------------- #
# Base case.                                                                    #
# --------------------------------------------------------------------------- #


class NegativeControlTestCase(unittest.TestCase):
    """Per-test isolation, a three-phase `run_flow` driver with fake role
    runners, and a child registry.

    The role runners are fakes so that a REAL `run_flow` -- the real owner
    acquisition, the real governed seams, the real measurement boundaries, the
    real release `finally` -- runs without a provider ever being contacted.
    Everything else in the frame is production code.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cowork-owner-p5n-root-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = tempfile.mkdtemp(prefix="cowork-owner-p5n-proj-")
        self.addCleanup(shutil.rmtree, self.project, True)
        patcher = mock.patch.dict(os.environ,
                                  {"COWORK_SESSIONS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spath = os.path.join(self.project, ".cowork", "session.json")
        # The module owner context is process-global by design; restore it
        # verbatim after every test so one fixture can never leak enforcement
        # into the next.
        prior_context = dict(cowork._OWNER_CONTEXT)
        self.addCleanup(cowork._restore_owner_context, prior_context)
        # `_BIND_LOG` is likewise process-global implementation state.
        prior_log = dict(owner._BIND_LOG)
        self.addCleanup(self._restore_bind_log, prior_log)
        self._children = []
        self.addCleanup(self._kill_every_child)
        # Any real process creation is VISIBLE for the whole case.
        self.popen_log = _PopenLog()
        popen_patcher = mock.patch.object(subprocess, "Popen", self.popen_log)
        popen_patcher.start()
        self.addCleanup(popen_patcher.stop)

    @staticmethod
    def _restore_bind_log(prior):
        owner._BIND_LOG.clear()
        owner._BIND_LOG.update(prior)

    def _kill_every_child(self):
        for proc in self._children:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:  # pragma: no cover - already reaped
                    pass
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass

    # -- fake role runners --------------------------------------------------- #

    @staticmethod
    def _approve(on_outcome):
        if on_outcome is not None:
            on_outcome("approved", None)

    def fake_scout(self, config, context, selected, io_in=None, io_out=None,
                   on_outcome=None, **kwargs):
        self._approve(on_outcome)
        return 0

    def fake_planner(self, config, context, selected, io_in=None, io_out=None,
                     on_outcome=None, **kwargs):
        self._approve(on_outcome)
        return 0

    def fake_builder(self, config, context, selected, io_in=None, io_out=None,
                     on_outcome=None, **kwargs):
        self._approve(on_outcome)
        return 0

    def advancing_scout(self, config, context, selected, io_in=None,
                        io_out=None, on_outcome=None, session_uuid=None,
                        **kwargs):
        """A fake lead role that still does the ONE thing a real one does at
        the governed WRITE seam: advance its own phase through
        `_advance_phase`.

        Without it the enforcement controls would be vacuous on that seam -- a
        fake role runner that advances nothing never reaches `_advance_phase`
        at all, and a claim about every entry of an empty set is not a
        measurement. The work id is the production one, recomputed from
        `_role_work_id` rather than invented."""
        work_id = cowork._role_work_id(session_uuid, "scout")
        cowork._advance_phase(session_uuid, work_id, "preflight_started",
                              source="p5_negative_controls")
        self._approve(on_outcome)
        return 0

    # -- run_flow driving ---------------------------------------------------- #

    def args(self, extra=(), team=("scout",)):
        argv = ["--team", ",".join(team)]
        for role in team:
            argv += ["--config", "%s=claude" % role]
        argv += ["--context", "goal", "--session-file", self.spath]
        return cowork.build_parser().parse_args(argv + list(extra))

    def run_flow(self, extra=(), team=("scout",), scout=None, planner=None,
                 builder=None):
        out = io.StringIO()
        rc = cowork.run_flow(
            self.args(extra, team=team), io_in=io.StringIO(""), io_out=out,
            which=lambda c: "/bin/" + c,
            run_scout_fn=scout if scout is not None else self.fake_scout,
            run_planner_fn=planner if planner is not None else
            self.fake_planner,
            run_builder_fn=builder if builder is not None else
            self.fake_builder)
        return rc, out.getvalue()

    def run_fresh(self, **kwargs):
        """One FRESH owned flow (`--new`), returning `(rc, session_uuid)`.

        Always a new session rather than a resume: the session is minted and
        persisted BEFORE the lease is acquired, so the uuid is readable even
        from a run that ended in a refusal."""
        extra = ["--new"] + list(kwargs.pop("extra", ()))
        rc, _out = self.run_flow(extra=extra, **kwargs)
        return rc, self.saved_session_uuid()

    def saved_session_uuid(self):
        return state_store.get_session_uuid(state_store.load(self.spath))

    # -- lease and binding seeding ------------------------------------------ #

    def claimant(self, session_uuid, entry_point="run_flow"):
        return owner.owner_identity(session_uuid, entry_point, self.project,
                                    self.spath)

    def seed_live_owner(self, session_uuid, now=None):
        """A genuinely LIVE lease: this process's own pid and start time, a
        fresh heartbeat, never released."""
        return owner.acquire_owner_lease(session_uuid,
                                         self.claimant(session_uuid), now=now)

    def seed_foreign_live_binding(self, controller, provider_session_id):
        """A DIFFERENT session, holding a live lease, holding the binding for
        this provider conversation. Returns its session uuid."""
        foreign = str(uuid.uuid4())
        record = self.seed_live_owner(foreign)
        owner.bind_provider_session(
            controller, provider_session_id,
            {"session_uuid": foreign, "owner_id": record["owner_id"]},
            "builder")
        return foreign

    def lose_the_lease(self, session_uuid, owner_id, epoch):
        """Take the session over from under the running flow, exactly as a
        second cowork process would: release this holder's record and acquire a
        fresh one under a NEW `owner_id`. Every later `assert_owner` then fails
        the fence, which is the loss every governed seam is supposed to
        observe."""
        owner.release_owner_lease(session_uuid, owner_id, epoch,
                                  "p5_injected_loss")
        return owner.acquire_owner_lease(session_uuid,
                                         self.claimant(session_uuid))

    # -- digests ------------------------------------------------------------- #

    def digest_root(self):
        """An exact per-path sha256 map of the sandboxed assets home, so a
        fixture can assert an ALLOWLIST of what may appear or change rather
        than a prefix match."""
        out = {}
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(dirpath, name)
                try:
                    out[os.path.relpath(full, self.root)] = _sha256_file(full)
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

    def owner_history(self, session_uuid):
        path = state_store.owner_history_path_for(session_uuid)
        if not os.path.exists(path):
            return []
        with open(path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- #
# N1 -- `--no-session` leases nothing, binds nothing and refuses nothing.        #
# --------------------------------------------------------------------------- #


class NoSessionTests(NegativeControlTestCase):
    """N1. `test_m55_owner_gate.py::NegativeControlTests::
    test_no_session_never_leases_and_never_raises` carries the scout-only
    version. What P5 adds is a THREE-PHASE flow that really reaches all four
    measurement boundaries and all their evaluation drains, plus the zero-bind
    arm and the "no owner exception of ANY subclass" arm."""

    # Every public entry point of the owner module. `--no-session` must reach
    # none of them in a way that refuses.
    ENTRY_POINTS = (
        "acquire_owner_lease", "take_over", "release_owner_lease",
        "renew_owner_lease", "assert_owner", "classify_owner_lease",
        "select_takeover_mode", "read_owner_lease", "owner_status_view",
        "bind_provider_session", "read_provider_binding",
        "release_provider_bindings", "mark_owner_terminal_unlocked",
    )

    def test_n1_a_full_flow_with_no_session_never_leases_and_never_refuses(self):
        refusals = []
        binds = _Counter(owner.bind_provider_session)
        contexts = {"advance": [], "evaluation": []}

        def recording(name, real):
            def wrapper(*args, **kwargs):
                try:
                    return real(*args, **kwargs)
                except owner.OwnerLeaseError as exc:
                    refusals.append((name, type(exc).__name__))
                    raise
            return wrapper

        real_advance = cowork._advance_phase
        real_eval = cowork.run_evaluation_transition

        def advance_spy(*args, **kwargs):
            contexts["advance"].append(dict(cowork._OWNER_CONTEXT))
            return real_advance(*args, **kwargs)

        def eval_spy(*args, **kwargs):
            contexts["evaluation"].append(dict(cowork._OWNER_CONTEXT))
            return real_eval(*args, **kwargs)

        patches = [
            mock.patch.object(cowork, "_advance_phase", advance_spy),
            mock.patch.object(cowork, "run_evaluation_transition", eval_spy),
            mock.patch.object(owner, "bind_provider_session", binds),
        ]
        for name in self.ENTRY_POINTS:
            if name == "bind_provider_session":
                continue
            patches.append(mock.patch.object(
                owner, name, recording(name, getattr(owner, name))))

        with _nested(patches):
            rc, _out = self.run_flow(extra=["--no-session"], team=FULL_TEAM,
                                     scout=self.advancing_scout)

        self.assertEqual(rc, 0)
        # Not vacuous: the run really crossed BOTH kinds of governed seam.
        self.assertTrue(contexts["evaluation"],
                        "no evaluation boundary was reached at all")
        self.assertTrue(contexts["advance"],
                        "no `_advance_phase` seam was reached at all")
        for where, seen in contexts.items():
            for ctx in seen:
                self.assertIs(ctx["enforced"], False, where)
        # No refusal of ANY subclass, anywhere.
        self.assertEqual(refusals, [])
        # Nothing was ever bound.
        self.assertEqual(binds.count, 0)
        # The ephemeral assets home is pre-existing `--no-session` behaviour;
        # what must not exist is an owner directory of any kind, or a global
        # binding index.
        for entry in os.listdir(self.root):
            self.assertFalse(
                os.path.exists(os.path.join(self.root, entry, "owner")),
                entry)
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "provider-bindings")))


# --------------------------------------------------------------------------- #
# N2 -- the read-only surfaces stay lease-free under a live owner.              #
# --------------------------------------------------------------------------- #


class ReadOnlySurfaceTests(NegativeControlTestCase):
    """N2. A read-only surface that refused -- or that quietly took a lease --
    would make a contested session undiagnosable from the very scripts that
    need it most."""

    SCORES = ["--output-quality", "5", "--intent-alignment", "5",
              "--evidence-quality", "5", "--self-sufficiency", "5",
              "--cost-worthiness", "5"]

    def _established_session_under_a_live_owner(self):
        rc, session_uuid = self.run_fresh()
        self.assertEqual(rc, 0)
        # A DIFFERENT process's live lease stands over the session for the
        # whole of every query below.
        self.seed_live_owner(session_uuid)
        self.assertEqual(owner.classify_owner_lease(session_uuid),
                         "live_owner")
        return session_uuid

    def test_n2_the_four_read_only_surfaces_succeed_lease_free_under_a_live_owner(self):
        session_uuid = self._established_session_under_a_live_owner()
        preflight_calls = _Counter(lambda: 0)

        surfaces = {
            "--check": ["--check"],
            "--report": ["--report", session_uuid],
            "--session-owner": ["--session-owner", session_uuid],
            "--evaluate-role": ["--evaluate-role", "orchestration",
                                "--eval-session", session_uuid,
                                "--phase", "session"] + self.SCORES,
        }
        for label, argv in surfaces.items():
            with self.subTest(surface=label):
                before = _read_raw_lease_bytes(session_uuid)
                self.assertIsNotNone(before)
                with _nested([
                        # `preflight.main` is stubbed so the fixture measures
                        # the DISPATCH being lease-free rather than this host's
                        # tool inventory; the surface is still reached, and the
                        # counter proves it.
                        mock.patch.object(cowork.preflight, "main",
                                          preflight_calls),
                        mock.patch.object(bridge, "ClaudeSession",
                                          _Raises("ClaudeSession")),
                        mock.patch.object(bridge, "CodexSession",
                                          _Raises("CodexSession")),
                        mock.patch.object(bridge, "OpencodeSession",
                                          _Raises("OpencodeSession")),
                        mock.patch.object(bridge, "_real_claude_spawn",
                                          _Raises("_real_claude_spawn")),
                        mock.patch.object(cowork, "run_flow",
                                          _Raises("run_flow")),
                ]):
                    rc = cowork.main(argv)
                self.assertEqual(rc, 0)
                # No lease was acquired, renewed, released or taken over.
                self.assertEqual(_read_raw_lease_bytes(session_uuid), before)
                # No CONTROLLER process was created -- anywhere in this
                # fixture. The scoping is named rather than silent: the
                # ownership gate's own liveness evidence comes from
                # `ps -o lstart=`, which is a real child process BY DESIGN, so
                # the claim is about controller executables, not about process
                # creation as such.
                self.assertEqual(self.popen_log.controller_spawns, [])
        self.assertEqual(preflight_calls.count, 1,
                         "--check never reached the preflight surface")

    def test_n2_evaluate_role_reaches_no_drain_frame(self):
        """`--evaluate-role` is a read-mostly side channel dispatched beside
        `--check`/`--report`, above `run_flow` entirely. It must therefore not
        reach ANY drain frame -- not the transition, not the drain itself."""
        session_uuid = self._established_session_under_a_live_owner()
        with _nested([
                mock.patch.object(cowork, "drain_evaluations",
                                  _Raises("drain_evaluations")),
                mock.patch.object(cowork, "run_evaluation_transition",
                                  _Raises("run_evaluation_transition")),
                mock.patch.object(cowork, "_isolated_evaluator_session",
                                  _Raises("_isolated_evaluator_session")),
        ]):
            rc = cowork.main(["--evaluate-role", "orchestration",
                              "--eval-session", session_uuid,
                              "--phase", "session"] + self.SCORES)
        self.assertEqual(rc, 0)


# --------------------------------------------------------------------------- #
# N3 -- an owned run is enforced at EVERY governed seam.                        #
# --------------------------------------------------------------------------- #


class SingleOwnerTests(NegativeControlTestCase):
    """N3, the converse of N1. `test_m55_owner_gate.py::NegativeControlTests::
    test_an_owned_run_is_enforced_at_every_governed_seam` carries the
    scout-only version. P5 adds the NON-VACUITY assertion -- both counters
    strictly greater than zero, so a flow that never reached a seam cannot pass
    -- and the release-exactly-once arm."""

    def test_n3_every_governed_seam_and_every_evaluation_transition_is_enforced(self):
        advance_ctx = []
        eval_ctx = []
        require_ctx = []
        real_advance = cowork._advance_phase
        real_eval = cowork.run_evaluation_transition
        refusals = []
        takeovers = _Counter(owner.take_over)
        releases = _Counter(owner.release_owner_lease)

        def advance_spy(*args, **kwargs):
            advance_ctx.append(dict(cowork._OWNER_CONTEXT))
            return real_advance(*args, **kwargs)

        def eval_spy(*args, **kwargs):
            eval_ctx.append(dict(cowork._OWNER_CONTEXT))
            return real_eval(*args, **kwargs)

        real_require = cowork._require_owner

        def require_spy(*args, **kwargs):
            require_ctx.append(dict(cowork._OWNER_CONTEXT))
            try:
                return real_require(*args, **kwargs)
            except owner.OwnerLeaseError as exc:
                refusals.append(type(exc).__name__)
                raise

        with _nested([
                mock.patch.object(cowork, "_advance_phase", advance_spy),
                mock.patch.object(cowork, "run_evaluation_transition",
                                  eval_spy),
                mock.patch.object(cowork, "_require_owner", require_spy),
                mock.patch.object(owner, "take_over", takeovers),
                mock.patch.object(owner, "release_owner_lease", releases),
        ]):
            rc, session_uuid = self.run_fresh(team=FULL_TEAM,
                                              scout=self.advancing_scout)

        self.assertEqual(rc, 0)
        # NON-VACUITY: all three counters must be strictly positive, or
        # "enforced everywhere" would be a statement about an empty set.
        self.assertGreater(len(eval_ctx), 0)
        self.assertGreater(len(advance_ctx), 0)
        self.assertGreater(len(require_ctx), 0)
        for ctx in advance_ctx + eval_ctx + require_ctx:
            self.assertIs(ctx["enforced"], True)
        sys.stderr.write(
            "\n[N3] enforced at %d `_advance_phase` entries, %d evaluation "
            "boundaries and %d `_require_owner` fences\n"
            % (len(advance_ctx), len(eval_ctx), len(require_ctx)))

        self.assertEqual(refusals, [])
        self.assertEqual(takeovers.count, 0)
        self.assertEqual(releases.count, 1)

        history = self.owner_history(session_uuid)
        self.assertEqual([e["event"] for e in history],
                         ["acquired", "released"])
        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["state"], "released")
        self.assertEqual(record["epoch"], 1)


# --------------------------------------------------------------------------- #
# N4 -- a legacy session acquires cleanly and migrates nothing.                  #
# --------------------------------------------------------------------------- #


class LegacySessionTests(NegativeControlTestCase):
    """N4, entirely new. A session that predates the lease has no `owner/`
    directory at all. Acquiring must add the lease's own artifacts and NOTHING
    else -- no migration record, no rewritten history, no touched legacy
    asset."""

    LEGACY_ASSETS = {
        "trace.jsonl": '{"event": "run.start", "schema_version": 1}\n',
        "identities.json": '{"roles": {}}\n',
        "scores.json": '{"entries": []}\n',
    }

    def test_n4_a_legacy_session_acquires_at_epoch_one_with_no_migration_artifact(self):
        session_uuid = str(uuid.uuid4())
        assets = state_store.session_assets_dir(session_uuid)
        os.makedirs(assets, exist_ok=True)
        for name, body in self.LEGACY_ASSETS.items():
            with open(os.path.join(assets, name), "w") as fh:
                fh.write(body)
        self.assertFalse(os.path.exists(
            state_store.owner_dir_for(session_uuid)))

        before = self.digest_root()
        record = owner.acquire_owner_lease(session_uuid,
                                           self.claimant(session_uuid))
        after = self.digest_root()

        self.assertEqual(record["epoch"], 1)
        self.assertEqual(record["state"], "live")

        owner_prefix = os.path.join(session_uuid, "owner") + os.sep
        added = set(after) - set(before)
        changed = {p for p in set(before) & set(after)
                   if before[p] != after[p]}

        # EVERY new path is the lease's own, and the two that matter are there.
        self.assertTrue(added, "the acquisition wrote nothing at all")
        for path in sorted(added):
            self.assertTrue(path.startswith(owner_prefix),
                            "a non-owner artifact appeared: %s" % path)
        self.assertIn(os.path.join(session_uuid, "owner", "lease.json"), added)
        self.assertIn(os.path.join(session_uuid, "owner", "history.jsonl"),
                      added)
        # And NOTHING pre-existing was rewritten -- no migration of any name.
        self.assertEqual(changed, set())


# --------------------------------------------------------------------------- #
# N5 -- a parked owner stays live, as a separate process sees it.                #
# --------------------------------------------------------------------------- #


class ParkedOwnerTests(NegativeControlTestCase):
    """N5. The grace is sized for one missed tick plus a slow fsync, NOT for a
    human gate -- a healthy owner parked at a gate keeps renewing because the
    heartbeat daemon fires independently of turns and of in-flight sends. This
    drives the REAL `_run_owner_heartbeat_loop` over more than three full
    deadlines of injected time and checks the durable record from a genuinely
    SEPARATE process at each probe."""

    def _park_and_probe(self, session_uuid, foreign_host=""):
        """Run the production heartbeat loop with an injected clock, probing
        from a separate observer process every `N5_OBSERVE_EVERY` fires.

        The loop runs in THIS thread: `fire` sets the stop event once it has
        driven enough virtual time, and `_run_owner_heartbeat_loop`'s own
        `stop_event.wait` then returns True on the next pass. No second thread
        is needed, so nothing here depends on thread scheduling.

        NOTHING IS ASSERTED INSIDE `fire`. The production loop wraps its tick
        in `except Exception` on purpose -- a heartbeat must never be able to
        fail a turn -- so an assertion raised in there would be SWALLOWED and
        the fixture would report a silent pass. Every observation is collected
        and asserted after the loop returns instead.
        """
        lease = self.seed_live_owner(session_uuid)
        start = owner._parse_instant(lease["heartbeat_at"])
        self.assertIsNotNone(start)
        virtual = {"at": start}
        fires = {"n": 0}
        renews = []
        probes = []
        errors = []
        stop = threading.Event()

        def fire():
            fires["n"] += 1
            try:
                virtual["at"] = virtual["at"] + datetime.timedelta(
                    seconds=N5_VIRTUAL_STEP_S)
                renews.append(owner.renew_owner_lease(
                    session_uuid, lease["owner_id"], lease["epoch"],
                    now=virtual["at"]))
                if fires["n"] % N5_OBSERVE_EVERY == 0:
                    at = _iso(virtual["at"])
                    observed = self._observe_raw(session_uuid, at,
                                                 foreign_host=foreign_host)
                    observed["at"] = at
                    probes.append(observed)
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                errors.append("%s: %s" % (type(exc).__name__, exc))
            finally:
                if fires["n"] >= N5_FIRES:
                    stop.set()

        cowork._run_owner_heartbeat_loop(stop, fire, 0.001)

        self.assertEqual(errors, [])
        self.assertEqual(fires["n"], N5_FIRES)
        for renewed in renews:
            self.assertIsNotNone(renewed, "a CAS renew wrote nothing")
        elapsed = (virtual["at"] - start).total_seconds()
        self.assertGreater(elapsed, N5_REQUIRED_VIRTUAL_S)
        return probes, elapsed

    def _observe_raw(self, session_uuid, at, foreign_host=""):
        """The separate observer, without any assertion of its own -- see
        `_park_and_probe` on why nothing may assert inside the tick."""
        proc = subprocess.Popen(
            [sys.executable, "-c", _OBSERVER_CHILD, session_uuid, at,
             foreign_host],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_child_env(self.root))
        self._children.append(proc)
        out, err = proc.communicate(timeout=120)
        if proc.returncode != 0:
            raise RuntimeError("observer exited %s: %s"
                               % (proc.returncode, err))
        return json.loads(out.strip())

    def test_n5_a_parked_owner_stays_live_across_three_deadlines_for_a_separate_observer_process(self):
        session_uuid = str(uuid.uuid4())
        # ZERO sends in flight: `_send` is a raise-on-call double for the whole
        # fixture, so nothing here can be mistaken for a turn keeping the lease
        # alive.
        with mock.patch.object(cowork, "_send", _Raises("_send")):
            probes, elapsed = self._park_and_probe(session_uuid)

        self.assertGreaterEqual(len(probes), 4, probes)
        for probe in probes:
            self.assertEqual(probe["verdict"], "live_owner", probe)
            deadline = owner._parse_instant(probe["lease_deadline_at"])
            at = owner._parse_instant(probe["at"])
            self.assertIsNotNone(deadline)
            # The deadline NEVER passes the injected now.
            self.assertGreater(deadline, at, probe)
        sys.stderr.write(
            "\n[N5] injected virtual seconds=%d (> 3 x %d deadline), "
            "separate-process probes=%d\n"
            % (int(elapsed), N5_DEADLINE_S, len(probes)))

    def test_n5_a_foreign_host_observer_still_reads_live_owner(self):
        """The same probe from a simulated FOREIGN host, where the pid-alive
        OR-clause is unavailable by construction. A renewed lease is still
        inside its own deadline, and `_classify_record` decides the deadline
        BEFORE the host check -- so the verdict is `live_owner` and no probe
        and no signal are needed."""
        session_uuid = str(uuid.uuid4())
        foreign = _sha256(b"p5-foreign-host")
        self.assertNotEqual(foreign, owner.host_id())
        with mock.patch.object(cowork, "_send", _Raises("_send")):
            probes, _elapsed = self._park_and_probe(session_uuid,
                                                    foreign_host=foreign)
        self.assertGreaterEqual(len(probes), 4, probes)
        for probe in probes:
            self.assertEqual(probe["host_id"], foreign, probe)
            self.assertEqual(probe["verdict"], "live_owner", probe)


# --------------------------------------------------------------------------- #
# N6 -- the frozen persistence seam.                                            #
# --------------------------------------------------------------------------- #


class PersistenceSeamTests(NegativeControlTestCase):
    """N6. `save_role_session` is the seam #64 must not have moved, and
    `test_cowork_state_m3.py` is what froze it. Arms (i) and (ii) restate that
    freeze on THIS candidate; arms (iii) and (iv) are what P5 adds -- the
    durable OUTPUT of the real `role_saver` closure, unenforced and enforced,
    asserted byte-for-byte."""

    SID_UNENFORCED = "p5-n6-unenforced"
    SID_ENFORCED = "p5-n6-enforced"

    def test_n6_save_role_session_signature_is_frozen(self):
        parameters = list(inspect.signature(
            state_store.save_role_session).parameters.keys())
        self.assertEqual(parameters, SAVE_ROLE_SESSION_PARAMETERS)

    def test_n6_the_characterization_file_is_byte_identical_to_the_base(self):
        if _live_candidate_repo() is None:
            self.skipTest(
                "not the live candidate repository; the characterization "
                "file's byte identity is measured by the scope preflight")
        self.assertEqual(
            _sha256_file(os.path.join(_REPO_ROOT,
                                      STATE_CHARACTERIZATION_FILE)),
            _sha256(_git_show_bytes(BASE_SHA, STATE_CHARACTERIZATION_FILE)))

    def _role_entry(self, role):
        state = state_store.load(self.spath) or {}
        return dict((state.get("sessions") or {}).get(role) or {})

    def _assert_matches_the_bare_seam(self, box, session_id):
        """The produced entry must equal what `save_role_session` ALONE
        produces from THE SAME PRIOR ENTRY.

        Comparing against a bare call on an empty anchor would be the wrong
        control, and the frozen contract says why: `save_role_session` is
        documented to MERGE into the role's existing entry so bookkeeping
        fields survive an id refresh, and a real run legitimately records one
        of those (`last_context_revision_seen`, written by the context-delivery
        seam) before the callback ever fires. An empty-anchor reference would
        therefore differ for a reason that has nothing to do with the owner
        machinery. The control that actually isolates the owner machinery is
        same prior in, same call, compare out."""
        reference_path = os.path.join(self.project, ".cowork",
                                      "reference.json")
        reference = state_store.save_role_session(
            reference_path, "scout", "claude", session_id,
            prior={"sessions": {"scout": dict(box["prior_entry"])}})
        self.assertEqual(
            json.dumps(box["produced_entry"], sort_keys=True).encode("utf-8"),
            json.dumps(reference["sessions"]["scout"],
                       sort_keys=True).encode("utf-8"))
        self.assertEqual(box["produced_entry"].get("id"), session_id)
        self.assertEqual(box["produced_entry"].get("controller"), "claude")
        # Whatever the prior entry carried survives the refresh. Trivially
        # true when this run's prior entry is empty, which is exactly why the
        # merge contract also gets its own witness below.
        for key, value in box["prior_entry"].items():
            if key in ("controller", "id"):
                continue
            self.assertEqual(box["produced_entry"].get(key), value, key)

        # THE MERGE CONTRACT, witnessed on a throwaway anchor and independent
        # of when this particular run happens to write its bookkeeping: a field
        # already on the role's entry SURVIVES an id refresh. That is what
        # `save_role_session` promises, and it is the property that makes
        # "same prior in, same call, compare out" the right control rather than
        # a comparison that could pass by both sides losing the same field.
        witness_path = os.path.join(self.project, ".cowork",
                                    "merge-witness.json")
        seeded = state_store.mark_context_seen(witness_path, "scout", 7)
        self.assertEqual(
            seeded["sessions"]["scout"]["last_context_revision_seen"], 7)
        refreshed = state_store.save_role_session(
            witness_path, "scout", "claude", session_id, prior=seeded)
        self.assertEqual(
            refreshed["sessions"]["scout"]["last_context_revision_seen"], 7)
        self.assertEqual(refreshed["sessions"]["scout"]["id"], session_id)

    def test_n6_unenforced_durable_output_is_byte_identical(self):
        """The real `role_saver(...)` closure, driven through the real
        `on_session` callback a role runner is handed, with `enforced` False --
        the `--no-session` shape of the seam. Its durable output must be
        byte-identical to the same persist with the owner machinery absent
        entirely."""
        box = {}

        def scout(config, context, selected, io_in=None, io_out=None,
                  on_outcome=None, **kwargs):
            box["prior_entry"] = self._role_entry("scout")
            prior = dict(cowork._OWNER_CONTEXT)
            cowork._OWNER_CONTEXT["enforced"] = False
            try:
                # With `enforced` False the closure must not reach the binding
                # surface AT ALL, so a raise-on-call double is the assertion.
                with mock.patch.object(owner, "bind_provider_session",
                                       _Raises("bind_provider_session")):
                    kwargs["on_session"]("claude", self.SID_UNENFORCED)
            finally:
                cowork._restore_owner_context(prior)
            box["produced_entry"] = self._role_entry("scout")
            self._approve(on_outcome)
            return 0

        rc, _session_uuid = self.run_fresh(scout=scout)
        self.assertEqual(rc, 0)
        self._assert_matches_the_bare_seam(box, self.SID_UNENFORCED)
        # And nothing was bound.
        self.assertFalse(os.path.exists(os.path.join(self.root,
                                                     "provider-bindings")))

    def test_n6_enforced_adds_only_the_provider_binding_record(self):
        """With `enforced` True and no conflicting binding, the anchor's
        `state['sessions'][role]` is byte-identical to the unenforced case and
        the ONLY durable artifact the owner machinery adds is the
        `provider-bindings/<sha256>.json` record -- asserted as an exact
        per-path digest-map difference, never a prefix match."""
        box = {}

        def scout(config, context, selected, io_in=None, io_out=None,
                  on_outcome=None, **kwargs):
            self.assertIs(cowork._OWNER_CONTEXT["enforced"], True)
            box["prior_entry"] = self._role_entry("scout")
            box["before"] = self.digest_root()
            kwargs["on_session"]("claude", self.SID_ENFORCED)
            box["after"] = self.digest_root()
            box["produced_entry"] = self._role_entry("scout")
            self._approve(on_outcome)
            return 0

        rc, session_uuid = self.run_fresh(scout=scout)
        self.assertEqual(rc, 0)

        binding_rel = os.path.relpath(
            state_store.provider_session_binding_path_for(
                "claude", self.SID_ENFORCED), self.root)
        added = set(box["after"]) - set(box["before"])
        # EXACTLY the binding record -- plus, at most, the flock sibling the
        # locked transaction that wrote it opens beside it. Named explicitly
        # rather than admitted by a prefix match: any OTHER new path would be
        # an extra durable artifact the enforced branch is not entitled to.
        self.assertIn(binding_rel, added)
        self.assertLessEqual(added - {binding_rel},
                             {binding_rel + ".lock"}, added)

        changed = {p for p in set(box["before"]) & set(box["after"])
                   if box["before"][p] != box["after"][p]}
        # The trace is append-only and observational; it is the ONLY thing the
        # seam rewrites inside the assets home.
        self.assertEqual(
            changed, {os.path.join(session_uuid, "trace.jsonl")}, changed)

        # The anchor entry itself is byte-identical to what the bare seam
        # produces from the same prior entry -- so the ONLY thing enforcement
        # added anywhere is the binding record asserted above.
        self._assert_matches_the_bare_seam(box, self.SID_ENFORCED)


# --------------------------------------------------------------------------- #
# N7 -- a nested run restores the outer context verbatim.                       #
# --------------------------------------------------------------------------- #


class NestedRunTests(NegativeControlTestCase):
    """N7. `test_m55_owner_gate.py::LifecycleTests::
    test_a_nested_run_restores_the_outer_context_verbatim` carries the clean
    nesting case. What P5 adds is the arm where the INNER run records AND
    drains a provider conflict of its own -- the case where the inner region
    genuinely mutates the shared box before restoring it."""

    def test_n7_the_inner_run_restores_all_five_outer_context_fields_including_a_drained_conflict(self):
        outer_uuid = str(uuid.uuid4())
        outer = self.seed_live_owner(outer_uuid)
        cowork._set_owner_context(outer_uuid, outer["owner_id"],
                                  outer["epoch"])
        outer_pending = owner.ProviderSessionConflict(
            "claude", "p5-outer-sid", str(uuid.uuid4()), "scout")
        cowork._OWNER_CONTEXT["provider_conflict"] = outer_pending
        cowork._OWNER_CONTEXT["matched"] = False
        expected = dict(cowork._OWNER_CONTEXT)

        # The INNER run is a genuinely different session in a different project
        # directory, and its scout hits a foreign live binding -- so the inner
        # region records a conflict of its own and drains it at the next
        # governed seam, ending at rc 3.
        inner_project = tempfile.mkdtemp(prefix="cowork-owner-p5n-inner-")
        self.addCleanup(shutil.rmtree, inner_project, True)
        self.spath = os.path.join(inner_project, ".cowork", "session.json")
        self.seed_foreign_live_binding("claude", "p5-n7-sid")

        drained = []
        real_require = cowork._require_owner

        def require_spy(*args, **kwargs):
            try:
                return real_require(*args, **kwargs)
            except owner.OwnerLeaseError as exc:
                drained.append(exc)
                raise

        def scout(config, context, selected, io_in=None, io_out=None,
                  on_outcome=None, **kwargs):
            kwargs["on_session"]("claude", "p5-n7-sid")
            return 0

        with mock.patch.object(cowork, "_require_owner", require_spy):
            inner_rc, _inner_uuid = self.run_fresh(scout=scout)

        # The inner run really did record and drain a conflict of its own.
        self.assertEqual(inner_rc, 3)
        self.assertEqual([type(e).__name__ for e in drained],
                         ["ProviderSessionConflict"])
        self.assertIsNot(drained[0], outer_pending)

        # And the OUTER context came back verbatim -- every field, including
        # `provider_conflict`, which is the same OBJECT it was before.
        self.assertEqual(cowork._current_owner_context(), expected)
        self.assertIs(cowork._OWNER_CONTEXT["provider_conflict"],
                      outer_pending)
        for field in ("session_uuid", "owner_id", "epoch", "enforced",
                      "matched", "provider_conflict"):
            self.assertEqual(cowork._OWNER_CONTEXT[field], expected[field],
                             field)


# --------------------------------------------------------------------------- #
# N8 -- the evaluator exemption is exactly one site and leaks nothing.          #
# --------------------------------------------------------------------------- #


class EvaluatorExemptionTests(NegativeControlTestCase):
    """N8. The evaluator dispatch site sits under two swallow-all handlers no
    package here may touch, so a refusal raised there would become an anonymous
    scoring failure. It is STRUCTURALLY exempt, and the compensating fence sits
    one frame above both swallows at the evaluation region's own boundary.

    P5 adds the RUNTIME arm -- a full owned run whose measurement boundaries
    all drain with zero owner exceptions -- alongside the AST count that keeps
    the exemption from widening."""

    def test_n8_the_exemption_is_one_site_and_drains_without_owner_exceptions(self):
        # (a) the exemption, at the gate itself.
        session_uuid = str(uuid.uuid4())
        record = self.seed_live_owner(session_uuid)
        prior = cowork._set_owner_context(session_uuid, record["owner_id"],
                                          record["epoch"])
        try:
            self.assertIsNone(cowork._owner_gate_fact(purpose="evaluator"))
            launch_fact = cowork._owner_gate_fact(purpose="launch")
            self.assertIsNotNone(launch_fact)
            self.assertIs(launch_fact["allowed"], True)
        finally:
            cowork._restore_owner_context(prior)

        # (b) the runtime arm: every measurement boundary drains, and not one
        # owner exception escapes any of them.
        boundaries = []
        escaped = []
        real_eval = cowork.run_evaluation_transition

        def eval_spy(*args, **kwargs):
            # The assertion below is that the `except` limb never runs on a
            # healthy owned run; it exists so that a leak is REPORTED by name
            # rather than surfacing as an opaque error.
            boundaries.append(kwargs.get("at"))
            try:
                return real_eval(*args, **kwargs)
            except owner.OwnerLeaseError as exc:
                escaped.append(type(exc).__name__)
                raise

        with _nested([
                mock.patch.object(cowork, "run_evaluation_transition",
                                  eval_spy),
                mock.patch.object(cowork, "_isolated_evaluator_session",
                                  _Raises("_isolated_evaluator_session")),
        ]):
            rc, _uuid = self.run_fresh(team=FULL_TEAM)
        self.assertEqual(rc, 0)
        self.assertEqual(escaped, [])
        # session.start, the two phase changes, and session.end.
        self.assertGreaterEqual(len(boundaries), 3, boundaries)

        # (c) the exemption cannot silently widen: exactly one of the dispatch
        # call sites carries `purpose="evaluator"`, and it is inside
        # `_isolated_evaluator_session`.
        tree = _cowork_tree()
        top = {n.name: n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        sites = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                 and _called_name(c) == "_decide_and_trace"]
        self.assertEqual(len(sites), 24)
        evaluator = []
        for call in sites:
            purposes = [a.value for a in call.args
                        if isinstance(a, ast.Constant)]
            purposes += [kw.value.value for kw in call.keywords
                         if kw.arg == "purpose"
                         and isinstance(kw.value, ast.Constant)]
            if "evaluator" in purposes:
                evaluator.append(call)
        self.assertEqual(len(evaluator), 1)
        target = top["_isolated_evaluator_session"]
        self.assertTrue(target.lineno <= evaluator[0].lineno
                        <= target.end_lineno)


# --------------------------------------------------------------------------- #
# N9 -- rule E4's enqueue surface, BOUNDED rather than closed.                   #
# --------------------------------------------------------------------------- #


class EnqueueBoundTests(NegativeControlTestCase):
    """N9, entirely new. Rule E4 declares the evaluator enqueue seam OUT OF
    SCOPE for this issue: closing it is a new authority decision, not a P5
    correction. What P5 can honestly do is BOUND it, and that is what this
    control is -- the property is stated, measured and REPORTED, including when
    the measured residual is empty.

    The bound: an enqueue that happens after the lease is lost must be
    PRECEDED, within the same run, by a governed call that RAISED. An enqueue
    reached with no raising governed call in front of it would mean the loss
    was never observed at all, which is the failure mode rule E4 leaves open
    and this control refuses to leave unmeasured."""

    def test_n9_every_post_loss_enqueue_is_preceded_by_a_raising_governed_call(self):
        log = []

        def governed(name, real):
            def wrapper(*args, **kwargs):
                try:
                    result = real(*args, **kwargs)
                except owner.OwnerLeaseError:
                    log.append(("governed_raised", name))
                    raise
                log.append(("governed_ok", name))
                return result
            return wrapper

        def enqueue_seam(name, real):
            def wrapper(*args, **kwargs):
                log.append(("enqueue", name))
                return real(*args, **kwargs)
            return wrapper

        def scout(config, context, selected, io_in=None, io_out=None,
                  on_outcome=None, **kwargs):
            ctx = cowork._current_owner_context()
            self.lose_the_lease(ctx["session_uuid"], ctx["owner_id"],
                                ctx["epoch"])
            log.append(("loss", None))
            return 0

        with _nested([
                mock.patch.object(cowork, "_require_owner",
                                  governed("_require_owner",
                                           cowork._require_owner)),
                mock.patch.object(cowork, "_advance_phase",
                                  governed("_advance_phase",
                                           cowork._advance_phase)),
                mock.patch.object(cowork, "_decide_and_trace",
                                  governed("_decide_and_trace",
                                           cowork._decide_and_trace)),
                mock.patch.object(cowork, "_make_enqueue_eval_fn",
                                  enqueue_seam("_make_enqueue_eval_fn",
                                               cowork._make_enqueue_eval_fn)),
                mock.patch.object(cowork, "_enqueue_reviewer_eval",
                                  enqueue_seam("_enqueue_reviewer_eval",
                                               cowork._enqueue_reviewer_eval)),
        ]):
            rc, _session_uuid = self.run_fresh(scout=scout)

        # The loss was real and the run ended on it.
        self.assertEqual(rc, 3)
        kinds = [entry[0] for entry in log]
        self.assertIn("loss", kinds)
        loss_at = kinds.index("loss")
        raised_after = [i for i, kind in enumerate(kinds)
                        if kind == "governed_raised" and i > loss_at]
        self.assertTrue(raised_after,
                        "the lease loss was never observed at a governed seam")

        # THE BOUND: every post-loss enqueue has a raising governed call in
        # front of it.
        enqueues_after = [i for i, kind in enumerate(kinds)
                          if kind == "enqueue" and i > loss_at]
        unbounded = [i for i in enqueues_after
                     if not any(j < i for j in raised_after)]
        self.assertEqual(unbounded, [], log)
        sys.stderr.write(
            "\n[N9] post-loss enqueues observed=%d, all bounded by a raising "
            "governed call; first observation of the loss at log index %d\n"
            % (len(enqueues_after), raised_after[0]))


# --------------------------------------------------------------------------- #
# N10 -- nothing anonymous reaches the send gateway.                            #
# --------------------------------------------------------------------------- #


class SendGatewayTests(NegativeControlTestCase):
    """N10. The send gateway has its own `except Exception`, so it is the one
    place a typed owner refusal could be flattened into an anonymous turn
    failure. Two properties, over every arm P5 drives itself.

    (1) `cowork._send` is wrapped with a spy that records `sys.exc_info()` at
        ENTRY. It must be `(None, None, None)` on every call -- the gateway is
        never entered while an owner refusal is propagating.

    (2) Every exception the three binding functions raise is TAGGED and
        ledgered, and each one must be ACCOUNTED FOR: recorded on
        `_OWNER_CONTEXT['provider_conflict']` and drained exactly once.

    WHY THERE IS A FIFTH ARM, AND WHAT THE FIRST FOUR CAN AND CANNOT SHOW.
    P5's flow fixtures replace the three role runners with fakes, so no turn is
    ever started and NO arm of an ordinary flow reaches `cowork._send` at all.
    On those four arms property (1) is therefore STRUCTURAL: the assertion that
    the gateway was entered zero times is real, but the per-entry
    `(None, None, None)` check has nothing to iterate, and a claim about every
    element of an empty set is not a measurement -- the same standard N3
    applies to its own counters and N9 applies to its own residual.

    `pending_conflict_send_frame` is the arm that makes it one. It drives the
    real `role_saver` callback into a PROVEN provider collision -- so a typed
    refusal is recorded and pending on the owner context -- and then enters the
    send gateway, which is exactly the shape production is defended against: an
    id observed mid-turn, a collision proven, and the next send about to run.
    Two things are measured there that the other four cannot show: the typed
    refusal never crossed the callback boundary (had it raised, the gateway's
    own `except Exception` would have flattened it into an anonymous turn
    failure), and the gateway entry that follows carries no exception at all.

    MAPPED SPAN, DISCLOSED: N10's accepted definition also spans F8's
    provider-exclusivity arms. Those are carried by
    `scripts/test_m55_owner_exclusivity.py::TypedPropagationTests::
    test_n10_nothing_anonymous_reaches_the_send_gateway` -- a CLOSED package's
    frozen suite -- and are deliberately NOT re-executed here.
    """

    # How many times each arm may enter the send gateway. The first four are
    # structurally zero (fake role runners start no turn); the fifth enters it
    # once, on purpose, with a proven refusal already pending.
    EXPECTED_GATEWAY_ENTRIES = {
        "no_session": 0,
        "owned": 0,
        "binding_failure": 0,
        "drain_loss": 0,
        "pending_conflict_send_frame": 1,
    }

    def test_n10_no_owner_refusal_and_nothing_anonymous_reaches_the_send_gateway(self):
        observed = 0
        for arm in ("no_session", "owned", "binding_failure", "drain_loss",
                    "pending_conflict_send_frame"):
            with self.subTest(arm=arm):
                observed += self._drive(arm)
        # NON-VACUITY: property (1)'s per-entry check really ran. Without this
        # the whole control would be a statement about an empty set.
        self.assertGreater(observed, 0,
                           "no arm ever entered the send gateway, so the "
                           "per-entry exception-state check never ran")
        sys.stderr.write(
            "\n[N10] send-gateway entries observed=%d (four flow arms reach it "
            "structurally zero times; the pending-conflict arm enters it once "
            "with a proven refusal pending). F8's span is carried by reference "
            "to the frozen exclusivity suite, not re-executed here.\n"
            % observed)

    # -- the arms ------------------------------------------------------------ #

    def _drive(self, arm):
        project = tempfile.mkdtemp(prefix="cowork-owner-p5n-n10-")
        self.addCleanup(shutil.rmtree, project, True)
        self.spath = os.path.join(project, ".cowork", "session.json")

        box = {}
        entries = []

        def send_spy(*args, **kwargs):
            # RECORDED AT ENTRY, and deliberately a passthrough that performs
            # no send: what is being measured is whether the gateway is ever
            # ENTERED while an owner refusal is propagating, and
            # `sys.exc_info()` at entry is exactly that question.
            entries.append(sys.exc_info())
            return None

        ledger = []
        recorded = []
        drained = []

        def tagged(name, real):
            def wrapper(*args, **kwargs):
                try:
                    return real(*args, **kwargs)
                except owner.OwnerLeaseError as exc:
                    ledger.append((name, exc))
                    raise
            return wrapper

        real_record = cowork._record_provider_conflict
        real_require = cowork._require_owner

        def record_spy(exc):
            recorded.append(exc)
            return real_record(exc)

        def require_spy(*args, **kwargs):
            try:
                return real_require(*args, **kwargs)
            except owner.OwnerLeaseError as exc:
                drained.append(exc)
                raise

        patches = [
            mock.patch.object(cowork, "_send", send_spy),
            mock.patch.object(cowork, "_record_provider_conflict", record_spy),
            mock.patch.object(cowork, "_require_owner", require_spy),
        ]
        for name in ("bind_provider_session", "read_provider_binding",
                     "release_provider_bindings"):
            patches.append(mock.patch.object(
                owner, name, tagged(name, getattr(owner, name))))

        with _nested(patches):
            rc = self._run_arm(arm, box)

        # PROPERTY 1. Every entry into the gateway happened with NO exception
        # in flight. This loop is the statement of the property; on the four
        # flow arms it has nothing to iterate (see the class docstring), which
        # is why the fifth arm exists and why the caller asserts a non-zero
        # total across the arms.
        for info in entries:
            self.assertEqual(info, (None, None, None), arm)
        # ...and each arm entered it exactly as many times as it is entitled
        # to, so a turn that started where none should have is a failure here
        # rather than a silent widening.
        self.assertEqual(len(entries), self.EXPECTED_GATEWAY_ENTRIES[arm], arm)

        # PROPERTY 2. Every tagged exception is accounted for.
        for name, exc in ledger:
            self.assertTrue(
                any(exc is other for other in recorded),
                "%s raised %r and nothing recorded it"
                % (name, type(exc).__name__))
        for exc in recorded:
            drains = [other for other in drained if other is exc]
            self.assertEqual(
                len(drains), 1,
                "a recorded refusal was drained %d times, not exactly once"
                % len(drains))

        expected_rc = {"no_session": 0, "owned": 0, "binding_failure": 3,
                       "drain_loss": 3, "pending_conflict_send_frame": 3}
        self.assertEqual(rc, expected_rc[arm], arm)

        if arm == "pending_conflict_send_frame":
            # The typed refusal NEVER crossed the callback boundary. Had it
            # raised out of `on_session`, it would have unwound into the send
            # gateway's own `except Exception` and been reported as an
            # anonymous turn failure instead of a named collision -- which is
            # the whole defect this control exists to catch.
            self.assertEqual(box["escaped"], [], box.get("escaped"))
            # And it really was a PROVEN collision left pending for the next
            # governed seam, not a quiet no-op.
            self.assertIsInstance(box["pending_after_callback"],
                                  owner.ProviderSessionConflict)

        return len(entries)

    def _run_arm(self, arm, box):
        if arm == "no_session":
            rc, _out = self.run_flow(extra=["--no-session"], team=FULL_TEAM)
            return rc
        if arm == "owned":
            rc, _uuid = self.run_fresh(team=FULL_TEAM)
            return rc
        if arm == "binding_failure":
            self.seed_foreign_live_binding("claude", "p5-n10-sid")

            def scout(config, context, selected, io_in=None, io_out=None,
                      on_outcome=None, **kwargs):
                kwargs["on_session"]("claude", "p5-n10-sid")
                return 0

            rc, _uuid = self.run_fresh(scout=scout)
            return rc
        if arm == "pending_conflict_send_frame":
            return self._pending_conflict_send_frame(box)
        # drain_loss: an established session whose lease is then lost BEFORE
        # the run's first drain, so the evaluation region refuses at its own
        # boundary rather than mid-drain.
        first_rc, _uuid = self.run_fresh()
        self.assertEqual(first_rc, 0)
        return _run_with_injected_loss(self)[0]

    def _pending_conflict_send_frame(self, box):
        """The arm that makes property (1) a measurement rather than a claim
        about an empty set.

        It reproduces the exact production shape the deferral exists for: a
        provider session id observed MID-TURN, proven to belong to a different
        live cowork session, recorded rather than raised, and then a send. The
        callback is invoked inside a `try` that stands where the send gateway's
        own `except Exception` stands, so an escaping typed refusal is caught
        and REPORTED here instead of silently becoming an anonymous turn
        failure."""
        self.seed_foreign_live_binding("claude", "p5-n10-frame-sid")

        def scout(config, context, selected, io_in=None, io_out=None,
                  on_outcome=None, **kwargs):
            escaped = []
            try:
                kwargs["on_session"]("claude", "p5-n10-frame-sid")
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad, and deliberately `Exception` rather than
                # `BaseException`: this stands exactly where the send gateway's
                # own swallow-all stands, and `OwnerLeaseError` derives from
                # `Exception`, so this catches precisely what the gateway would
                # have flattened. Recorded here and asserted EMPTY below.
                escaped.append(exc)
            box["escaped"] = escaped
            box["pending_after_callback"] = (
                cowork._OWNER_CONTEXT["provider_conflict"])
            # A PROVEN refusal is now pending. Enter the gateway with it
            # pending -- in production this is the next send of the same turn.
            cowork._send(_InertSession(), "p5 probe turn")
            return 0

        rc, _uuid = self.run_fresh(scout=scout)
        return rc


# --------------------------------------------------------------------------- #
# N11 -- the binding surface is TOTAL over its failure modes.                    #
# --------------------------------------------------------------------------- #


class BindingSurfaceTests(NegativeControlTestCase):
    """N11. `test_m55_owner_store.py::BindingSurfaceBehaviourTests::
    test_every_injected_failure_mode_translates_on_every_reader` carries this
    in depth. What P5 adds is the TOTALITY SWEEP as one control -- four failure
    modes on each of three functions, twelve cells -- plus the arm that a
    teardown failure never displaces a run's real exit code."""

    CONTROLLER = "claude"
    SID = "p5-n11-sid"

    def test_n11_the_four_failure_modes_map_to_provider_binding_unavailable_on_all_three_functions(self):
        owner_ref = {"session_uuid": str(uuid.uuid4()),
                     "owner_id": str(uuid.uuid4())}
        surfaces = (
            ("read_provider_binding", "_read_json_or_raise_if_corrupt",
             lambda: owner.read_provider_binding(self.CONTROLLER, self.SID)),
            ("bind_provider_session", "_locked_json_transaction",
             lambda: owner.bind_provider_session(
                 self.CONTROLLER, self.SID, owner_ref, "scout")),
            ("release_provider_bindings", "_locked_json_transaction",
             lambda: owner.release_provider_bindings(
                 owner_ref["session_uuid"], owner_ref["owner_id"])),
        )
        cells = 0
        for function_name, seam, call in surfaces:
            for mode_name, injected in TRANSLATED_FAILURES:
                with self.subTest(function=function_name, mode=mode_name):
                    with mock.patch.object(state_store, seam,
                                           side_effect=injected):
                        with self.assertRaises(
                                owner.ProviderBindingUnavailable) as caught:
                            call()
                    self.assertIs(caught.exception.__cause__, injected)
                    # Nothing raw escapes, and nothing is mistaken for a
                    # collision.
                    self.assertNotIsInstance(caught.exception,
                                             owner.ProviderSessionConflict)
                cells += 1
        self.assertEqual(cells, 12)

    def test_n11_read_returns_none_only_for_an_absent_record(self):
        self.assertIsNone(
            owner.read_provider_binding(self.CONTROLLER, "p5-absent-sid"))
        for mode_name, injected in TRANSLATED_FAILURES:
            with self.subTest(mode=mode_name):
                with mock.patch.object(state_store,
                                       "_read_json_or_raise_if_corrupt",
                                       side_effect=injected):
                    with self.assertRaises(owner.ProviderBindingUnavailable):
                        owner.read_provider_binding(self.CONTROLLER,
                                                    "p5-absent-sid")

    def test_n11_release_swallows_and_traces_its_translated_exception_during_teardown(self):
        """The release `finally` is the one place a teardown error could
        displace a run's real exit code. It must be traced and swallowed."""
        boom = owner.ProviderBindingUnavailable(
            None, None, detail="p5 injected teardown failure")

        def raising_release(*_args, **_kwargs):
            raise boom

        with mock.patch.object(owner, "release_provider_bindings",
                               raising_release):
            rc, session_uuid = self.run_fresh()

        self.assertEqual(rc, 0)
        failures = [e for e in self.trace_events(session_uuid)
                    if e.get("event") == "owner.binding_release_failed"]
        self.assertEqual(len(failures), 1, failures)
        self.assertEqual(failures[0].get("error_type"),
                         "ProviderBindingUnavailable")
        # The lease was still released cleanly, before the binding sweep.
        self.assertEqual(owner.read_owner_lease(session_uuid)["state"],
                         "released")


# --------------------------------------------------------------------------- #
# F14 -- the evaluation drain under a lost lease (authored here).               #
# --------------------------------------------------------------------------- #


def _seed_queue_entry(session_uuid, entry_id, phase="scouting"):
    """One self-contained, well-formed queue entry, written through the real
    `cowork_eval.enqueue`. It carries the evaluation SHAPE (so it is not a
    pre-lifecycle record) but no envelope, so nothing here needs a sealed
    evidence chain to be drained."""
    return evaluation.enqueue(
        state_store.evaluation_queue_path_for(session_uuid),
        {"entry_id": entry_id, "phase": phase, "round": 1,
         "policy_decision": {"selected": True,
                             "supersedes_earlier_candidates": False},
         "evaluatee": "scout", "criteria": ["p5"]})


def _run_with_injected_loss(case, extra=(), scout=None, team=("scout",)):
    """Run one owned flow whose lease is taken over BETWEEN the acquisition and
    the first governed seam.

    `_set_owner_context` is the exact seam for that: it is called immediately
    after `acquire_owner_lease`/`take_over` returns and before the heartbeat
    starts, the preflight runs, or any measurement boundary is reached. So the
    loss is genuinely PRE-DRAIN rather than approximately so."""
    real_set = cowork._set_owner_context

    def losing_set(session_uuid, owner_id, epoch):
        prior = real_set(session_uuid, owner_id, epoch)
        case.lose_the_lease(session_uuid, owner_id, epoch)
        return prior

    with mock.patch.object(cowork, "_set_owner_context", losing_set):
        rc, out = case.run_flow(extra=list(extra), team=team, scout=scout)
    return rc, out


class EvaluationDrainTests(NegativeControlTestCase):
    """F14, authored here (no runtime witness for it exists anywhere in the
    tree, and N10's accepted cross-fixture span is defined over it).

    The evaluation region is fenced ONCE per measurement boundary rather than
    per queue entry. That bound is deliberate and documented at
    cowork.py:14332-14334, so the third arm asserts the bound as written rather
    than pretending the fence is finer than it is."""

    def _established_session_with_a_queue(self, entry_ids):
        rc, session_uuid = self.run_fresh()
        self.assertEqual(rc, 0)
        for entry_id in entry_ids:
            self.assertTrue(_seed_queue_entry(session_uuid, entry_id))
        queue_path = state_store.evaluation_queue_path_for(session_uuid)
        self.assertTrue(os.path.exists(queue_path))
        return session_uuid, queue_path

    def test_f14_a_pre_drain_loss_ends_at_rc_three_with_zero_evaluator_constructions(self):
        session_uuid, queue_path = self._established_session_with_a_queue(
            ["p5-f14-a"])
        before = _sha256_file(queue_path)

        with mock.patch.object(cowork, "_isolated_evaluator_session",
                               _Raises("_isolated_evaluator_session")):
            rc, _out = _run_with_injected_loss(
                self, extra=["--evaluation-policy", "off"])

        self.assertEqual(rc, 3)
        # ZERO evaluator constructions, and the queue never moved: the fence at
        # the region's boundary refused before the drain could touch it.
        self.assertEqual(_sha256_file(queue_path), before)
        ends = [e for e in self.trace_events(session_uuid)
                if e.get("event") == "run.end"]
        self.assertEqual(ends[-1].get("rc"), 3)
        self.assertEqual(ends[-1].get("reason"), "session_owner_lost")

    def test_f14_a_valid_lease_drains_normally(self):
        """The converse arm, identical in every respect but the lease: the same
        boundary drains, the queue advances, and the run ends at rc 0."""
        session_uuid, queue_path = self._established_session_with_a_queue(
            ["p5-f14-b"])
        before = _sha256_file(queue_path)

        drains = _Counter(cowork.drain_evaluations)
        with _nested([
                mock.patch.object(cowork, "drain_evaluations", drains),
                mock.patch.object(cowork, "_isolated_evaluator_session",
                                  _Raises("_isolated_evaluator_session")),
        ]):
            rc, _out = self.run_flow(extra=["--evaluation-policy", "off"])

        self.assertEqual(rc, 0)
        self.assertGreater(drains.count, 0)
        # The queue ADVANCED: the entry now carries a durable, reasoned hold.
        self.assertNotEqual(_sha256_file(queue_path), before)
        fold = evaluation.read_entry_lifecycle(
            evaluation.read_queue(queue_path), "p5-f14-b")
        self.assertEqual(fold["state"], "held")
        self.assertEqual(fold["held_reason"], "policy_off")

    def test_f14_a_mid_drain_loss_is_observed_at_the_next_governed_seam(self):
        """A lease lost MID-drain is first observed at the NEXT governed seam,
        not per queue entry -- and the residual (what the drain went on to
        mutate after the loss) is BOUNDED by the set of entries that were
        eligible at drain ENTRY. That is the bound cowork.py:14332-14334
        declares; asserting anything finer would be asserting a fence that does
        not exist."""
        entry_ids = ["p5-f14-c1", "p5-f14-c2", "p5-f14-c3"]
        session_uuid, queue_path = self._established_session_with_a_queue(
            entry_ids)

        eligible_at_entry = []
        records_at_loss = {"n": None}
        scored = []
        real_drain = evaluation.drain

        def drain_spy(path, *args, **kwargs):
            eligible_at_entry.append(
                {e.get("entry_id")
                 for e in evaluation.pending_entries(path)})
            return real_drain(path, *args, **kwargs)

        def score_double(entry, verification, *args, **kwargs):
            scored.append(entry.get("entry_id"))
            if records_at_loss["n"] is None:
                records_at_loss["n"] = len(evaluation.read_queue(queue_path))
                ctx = cowork._current_owner_context()
                self.lose_the_lease(ctx["session_uuid"], ctx["owner_id"],
                                    ctx["epoch"])
            return {"ok": True, "error_class": None}

        with _nested([
                mock.patch.object(evaluation, "drain", drain_spy),
                mock.patch.object(cowork, "_score_queued_entry", score_double),
                mock.patch.object(cowork, "_isolated_evaluator_session",
                                  _Raises("_isolated_evaluator_session")),
        ]):
            rc, _out = self.run_flow()

        # The drain itself never raised -- the fence is per BOUNDARY, so the
        # loss was carried to the next governed seam, which ended the run.
        self.assertEqual(rc, 3)
        self.assertTrue(eligible_at_entry, "the drain was never entered")
        self.assertIsNotNone(records_at_loss["n"], "no entry was ever scored")
        self.assertGreater(len(scored), 1,
                           "the drain stopped at the losing entry, so there "
                           "is no residual to bound")

        # THE BOUND: everything mutated after the loss names an entry that was
        # already eligible when the drain was entered. Nothing new was pulled
        # in, and nothing outside that set was touched.
        records = evaluation.read_queue(queue_path)
        residual = {rec.get("entry_id")
                    for rec in records[records_at_loss["n"]:]
                    if rec.get("entry_id")}
        self.assertTrue(residual, "nothing was mutated after the loss at all")
        self.assertTrue(residual <= eligible_at_entry[0],
                        "residual %r escaped the drain-entry eligible set %r"
                        % (sorted(residual), sorted(eligible_at_entry[0])))
        sys.stderr.write(
            "\n[F14] mid-drain residual=%d entries, all inside the "
            "drain-entry eligible set of %d\n"
            % (len(residual), len(eligible_at_entry[0])))


# --------------------------------------------------------------------------- #
# The fixture matrix, MACHINE-CHECKED.                                          #
# --------------------------------------------------------------------------- #


class FixtureMatrixTests(unittest.TestCase):
    """Success criteria 2 and 3. A prose-only matrix in a build artifact cannot
    enforce "no row marked covered without a resolvable test id"; this can, and
    it fails loudly the moment a row names something that does not exist.

    The semantic judgement -- does the mapped test really carry that evidence?
    -- stays with the artifact and the independent reviewer. What is mechanised
    here is the part a human reading a table cannot check reliably: that every
    id RESOLVES."""

    def test_every_assigned_fixture_row_resolves_to_an_existing_test_id(self):
        self.assertEqual(
            sorted(FIXTURE_MATRIX), sorted(set(ASSIGNED_FIXTURE_IDS) | {"F14"}))
        self.assertEqual(len(FIXTURE_MATRIX), 19)

        for fixture_id in sorted(FIXTURE_MATRIX):
            row = FIXTURE_MATRIX[fixture_id]
            with self.subTest(fixture=fixture_id):
                self.assertIn(row["owner"], ("local", "local + mapped"))
                self.assertTrue(row["tests"],
                                "%s names no local test at all" % fixture_id)
                self.assertTrue((row.get("p5_adds") or "").strip(),
                                "%s declares no `p5_adds`" % fixture_id)
                for module_key, class_name, test_name in row["tests"]:
                    module = _LOCAL_MODULES[module_key]
                    klass = getattr(module, class_name, None)
                    self.assertIsNotNone(
                        klass, "%s: no class %s in %s"
                        % (fixture_id, class_name, module.__name__))
                    self.assertTrue(
                        callable(getattr(klass, test_name, None)),
                        "%s: no test %s on %s.%s"
                        % (fixture_id, test_name, module.__name__, class_name))
                for rel_path, class_name, test_name in row["mapped"]:
                    self._assert_frozen_test_exists(fixture_id, rel_path,
                                                    class_name, test_name)

    def test_the_scope_allowlist_agrees_with_the_sibling_module(self):
        self.assertEqual(ALLOWED_CHANGED_PATHS,
                         _crash_reclaim.ALLOWED_CHANGED_PATHS)
        self.assertEqual(BASE_SHA, _crash_reclaim.BASE_SHA)
        self.assertEqual(len(ALLOWED_CHANGED_PATHS), 2)
        for rel_path in sorted(ALLOWED_CHANGED_PATHS):
            self.assertTrue(
                os.path.isfile(os.path.join(_REPO_ROOT, rel_path)), rel_path)

    # -- resolution ---------------------------------------------------------- #

    def _assert_frozen_test_exists(self, fixture_id, rel_path, class_name,
                                   test_name):
        """Resolve a mapped row by AST PARSE of the frozen file -- never by
        importing it. Importing a frozen suite would bind its `TestCase`
        subclasses into this module's namespace, and the loader collects any
        `TestCase` that is a module attribute: P5's own reported test count
        would then silently include another package's suite."""
        full = os.path.join(_REPO_ROOT, rel_path)
        self.assertTrue(os.path.isfile(full),
                        "%s maps to a file that does not exist: %s"
                        % (fixture_id, rel_path))
        with open(full, "r") as fh:
            tree = ast.parse(fh.read(), filename=full)
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn(class_name, classes,
                      "%s maps to %s::%s, which does not exist"
                      % (fixture_id, rel_path, class_name))
        methods = {n.name for n in classes[class_name].body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn(test_name, methods,
                      "%s maps to %s::%s::%s, which does not exist"
                      % (fixture_id, rel_path, class_name, test_name))


# --------------------------------------------------------------------------- #
# Small shared utilities used above.                                            #
# --------------------------------------------------------------------------- #


class _nested(object):
    """Enter a list of context managers as one, in order, unwinding in reverse
    -- `contextlib.ExitStack` in the shape the surrounding fixtures already
    read as a `with` block."""

    def __init__(self, managers):
        self.managers = list(managers)
        self.entered = []

    def __enter__(self):
        for manager in self.managers:
            self.entered.append(manager.__enter__())
        return self.entered

    def __exit__(self, exc_type, exc, tb):
        suppressed = False
        for manager in reversed(self.managers):
            if manager.__exit__(exc_type, exc, tb):
                suppressed = True
        return suppressed


def _cowork_tree():
    with open(os.path.join(_HERE, "cowork.py"), "r") as fh:
        return ast.parse(fh.read(), filename="cowork.py")


def _called_name(node):
    """The bare callable name of a `Call`, whether written `f(...)` or
    `mod.f(...)`."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
