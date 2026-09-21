#!/usr/bin/env python3
"""Controller/provider session EXCLUSIVITY. The global binding store and the
ownership gate/lifecycle are proven in `test_m55_owner_store.py` and
`test_m55_owner_gate.py`; what has to be proven here is the wiring between
them -- REACHABILITY, ORDERING and COST:

  - **Three ordered enforcement seams, each refusing before anything is paid
    for.** Enforcement point 1 is `_owner_gate_fact` (pre-dispatch), point 2 is
    `run_resume_trigger` step 1 (pre-claim and pre-construction), point 3 is
    `run_flow`'s `role_saver().on_sess` (bind-before-persist, the durable
    backstop for an id only observed mid-turn). Each is driven through the REAL
    production function with controller constructors and process creation
    replaced by doubles that RAISE on call.

  - **Exclusivity follows LIVENESS, not the record.** The same session re-binds
    idempotently, including after a takeover changed `owner_id`; a binding whose
    owning session is no longer live is adoptable; and a dying predecessor
    deletes nothing a successor inherited. Driven through the real
    `cowork_owner` functions with an injected clock.

  - **Nothing typed is lost and no paid session is stranded.** A proven
    collision persists NOTHING; an unverifiable index still persists, because a
    live provider conversation left outside the durable record that names it is
    the exact defect #64 exists to remove. Every `ProviderBindingUnavailable`
    sub-arm keeps its real `__cause__`, and nothing anonymous reaches the send
    gateway.

  - **The exit contract is exact.** Every resume-trigger outcome name maps to
    one declared integer, no integer is used twice, and the two exclusivity
    outcomes (11 and 12) are the names the refusal paths actually emit.

Declared behaviour, asserted as such:

  1. A pre-dispatch exclusivity refusal raises the `ProviderSessionConflict` the gate
     already built, so the run ends under `run.end` reason
     `provider_session_bound` -- the SAME code `dispatch.decision` records --
     and the block shown names the session that actually
     holds the provider conversation rather than the one they are sitting in.

  2. `ProviderBindingUnavailable` raised inside `_owner_gate_fact` PROPAGATES
     rather than becoming a refusing fact (no such code exists in the
     dispatch refusal vocabulary), so `dispatch.contract` / `dispatch.decision` are not
     emitted on that one path -- the raise precedes `decide()`.

Every fixture redirects `COWORK_SESSIONS_ROOT` into a fresh `tempfile.mkdtemp()`
(so nothing here touches the real home dir and nothing is written inside the
worktree), drives the REAL production functions rather than fakes of them, and
spawns no provider and no network client.
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
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_owner as owner  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

# The complete resume-trigger exit contract: outcome name -> process exit
# code, as external callers consume it.
RESUME_TRIGGER_EXIT_CONTRACT = {
    "success": 0,
    "internal_error": 1,
    "invalid_arguments": 2,
    "not_due": 3,
    "conflict": 4,
    "attempts_exhausted": 5,
    "binding_mismatch": 6,
    "invalidated": 7,
    "no_pending_turn": 8,
    "send_failed": 9,
    "owner_conflict": 10,
    "provider_session_bound": 11,
    "provider_binding_unavailable": 12,
}
EXCLUSIVITY_EXIT_CODES = ("provider_session_bound",
                          "provider_binding_unavailable")

OWNER_SUBCLASSES = (
    owner.OwnerLeaseConflict, owner.OwnerLeaseCorrupt, owner.OwnerLeaseLost,
    owner.ProviderSessionConflict, owner.ProviderBindingUnavailable,
)

_ALLOW = {"allowed": True, "refusal_code": None, "refusal_message": None,
          "source": None}


# --------------------------------------------------------------------------- #
# Shared helpers.                                                              #
# --------------------------------------------------------------------------- #


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _cowork_source():
    with open(os.path.join(_HERE, "cowork.py"), "r") as fh:
        return fh.read()


def _cowork_tree():
    return ast.parse(_cowork_source(), filename="cowork.py")


def _named_top_level(tree):
    """Every top-level symbol resolvable BY NAME: functions, classes and simple
    assignments, as `name -> AST node`.

    Comparisons over these go through `ast.dump`, so they are semantic rather
    than a byte digest that a legitimate re-indentation would break on a
    CORRECT candidate."""
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node
    return out


def _closures_of(parent):
    """Every `FunctionDef` whose nearest enclosing `FunctionDef` is `parent`,
    resolved BY NAME through the module AST rather than by line -- so a `try`
    wrapper introduced between the parent and the closure does not hide it,
    and a duplicate name is detected rather than silently mis-pinned."""
    found = {}

    def walk(node, nearest):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if nearest is parent:
                    found.setdefault(child.name, []).append(child)
                walk(child, child)
            else:
                walk(child, nearest)

    walk(parent, parent)
    return found


def _find_functions(tree, name):
    """Every `FunctionDef` of that name ANYWHERE in the tree, however deeply
    nested. `on_sess` is a closure of a closure, so no by-parent lookup
    reaches it."""
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name]


def _called_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _call_names(node):
    return [n for n in (_called_name(c) for c in ast.walk(node)
                        if isinstance(c, ast.Call))
            if n is not None]


def _handler_names(handler):
    """Every dotted/bare exception name a single `except` clause catches."""
    if handler.type is None:
        return [None]
    parts = (handler.type.elts if isinstance(handler.type, ast.Tuple)
             else [handler.type])
    return [ast.unparse(p) for p in parts]


def _swallows(handler):
    """A handler SWALLOWS when it is broad AND does not re-raise. The
    qualifier is the plan's own: `except BaseException: ... raise` records a
    reason and lets the exception continue, and treating that as a swallow
    would fail the very shape section 3.5 mandates."""
    names = _handler_names(handler)
    broad = any(n is None or n in ("Exception", "BaseException")
                for n in names)
    if not broad:
        return False
    return not any(isinstance(stmt, ast.Raise) and stmt.exc is None
                   for stmt in handler.body)


def _broad_try_spans(tree):
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if any(_swallows(h) for h in node.handlers):
            for stmt in node.body:
                spans.append((stmt.lineno, stmt.end_lineno))
    return spans


def _dead_pid():
    """A pid that has genuinely exited -- a real crashed owner's pid, obtained
    by letting a real child run and reaping it, never a guessed number."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _lease_file(session_uuid):
    """The lease file, spelled WITHOUT `owner_lease_path_for` and without the
    literal `"owner/lease.json"`, on purpose: G3d's repo-wide sweep asserts
    neither spelling ever reaches an `open(...)` anywhere in the repository,
    including this file."""
    return os.path.join(state_store.owner_dir_for(session_uuid), "lease.json")


def _write_raw_lease(session_uuid, payload):
    """Fabricate a durable lease record directly, bypassing the store, so a
    fixture can put the classifier in front of a state a healthy run could only
    reach by crashing."""
    os.makedirs(state_store.owner_dir_for(session_uuid), exist_ok=True)
    with open(_lease_file(session_uuid), "w") as fh:
        json.dump(payload, fh)


def _selector_argv(argv):
    """The agent-only contract accepts exactly one session selector: a new
    session at an explicit fresh path is `--session-file` alone, and an
    ephemeral run names no session file."""
    argv = list(argv)
    if "--no-session" in argv and "--session-file" in argv:
        i = argv.index("--session-file")
        del argv[i:i + 2]
    if "--session-file" in argv:
        argv = [a for a in argv if a != "--new"]
    return argv


class _Raises(object):
    """A double that fails the test if anything ever calls it. Used wherever
    the assertion is the ABSENCE of a paid dispatch, a claim or a write."""

    def __init__(self, label):
        self.label = label

    def __call__(self, *args, **kwargs):
        raise AssertionError("%s was invoked on a refused run" % self.label)


class _Counter(object):
    """A spy that counts calls and forwards to the real callable, so a fixture
    can assert "no FURTHER calls after the refusal" rather than "no calls at
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


class _Trace(object):
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class _nested(object):
    """Enter a list of context managers as one, in order, unwinding in reverse
    -- `contextlib.ExitStack` in the shape the surrounding fixtures already
    read as a `with` block, and with no import beyond what this module already
    uses."""

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


class _PopenLog(object):
    """A `subprocess.Popen` replacement that RECORDS every process creation by
    executable basename and then forwards to the real one.

    Recording rather than raising, where a whole `run_flow` is in scope: a
    healthy run legitimately shells out during its own measurement checkpoints
    long before the seam under test is reached, so a double that raised on
    call would fail a CORRECT candidate. The assertion the fixtures make on
    this log is the delta across the seam plus an absolute "no controller
    process anywhere", which is the claim that actually matters."""

    def __init__(self):
        self.real = subprocess.Popen
        self.calls = []

    def __call__(self, command, *args, **kwargs):
        argv0 = command[0] if isinstance(command, (list, tuple)) else command
        self.calls.append(os.path.basename(str(argv0)))
        return self.real(command, *args, **kwargs)


def _guarded_popen():
    """A `subprocess.Popen` replacement that allows ONLY `ps`, and the scoping
    is named rather than silent: the ownership gate's own liveness evidence
    comes from `ps -o lstart=` (`cowork_owner._probe_pid_start`), which is a
    real child process BY DESIGN -- it is the PID-reuse defence, not a
    dispatch. Anything else reaching `Popen` on a refused run fails."""
    real_popen = subprocess.Popen

    def guarded(command, *args, **kwargs):
        argv0 = command[0] if isinstance(command, (list, tuple)) else command
        if os.path.basename(str(argv0)) != "ps":
            raise AssertionError(
                "a process was created on a refused run: %r" % (command,))
        return real_popen(command, *args, **kwargs)

    return guarded


# --------------------------------------------------------------------------- #
# Base case: a sandboxed session anchor plus a sandboxed assets home.           #
# --------------------------------------------------------------------------- #


class ExclusivityTestCase(unittest.TestCase):
    """Every test gets its own `COWORK_SESSIONS_ROOT` and its own project
    directory, so no fixture can touch the real home dir, write inside the
    worktree, or observe another test's lease or binding."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cowork-owner-p3-root-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = tempfile.mkdtemp(prefix="cowork-owner-p3-proj-")
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

    @staticmethod
    def _restore_bind_log(prior):
        owner._BIND_LOG.clear()
        owner._BIND_LOG.update(prior)

    # -- run_flow driving -------------------------------------------------- #

    def args(self, extra=()):
        return cowork.build_parser().parse_args(_selector_argv(
            ["--team", "scout,scout-reviewer", "--config", "scout=claude",
             "--context", "goal", "--session-file", self.spath] + list(extra)))

    def run_flow(self, extra=(), scout=None, **kwargs):
        out = io.StringIO()
        rc = cowork.run_flow(
            self.args(extra), io_out=out,
            which=lambda c: "/bin/" + c,
            run_scout_fn=scout if scout is not None else self._ok_scout,
            **kwargs)
        return rc, out.getvalue()

    def _ok_scout(self, config, context, selected, io_out=None,
                  resume_id=None, on_session=None, intel_path=None,
                  review_path=None, on_outcome=None, **kwargs):
        """The fake scout lead+review seam: it reports the explicit approval
        the real seam reports on success, because `_final_rc` keeps rc 0 only
        for an approved last outcome. Each call is recorded so a happy-path
        run can prove the fake lead actually ran."""
        self.scout_calls = getattr(self, "scout_calls", 0) + 1
        on_outcome("approved", None)
        return 0

    def establish_session(self):
        """Run one complete owned flow, then return its `session_uuid`. The
        lease it leaves behind is `released`, so a later fixture starts from a
        genuine post-clean-exit state rather than a fabricated one."""
        before = getattr(self, "scout_calls", 0)
        rc, _out = self.run_flow(["--new"])
        self.assertEqual(rc, 0)
        self.assertGreater(self.scout_calls, before,
                           "the fake scout lead never ran")
        return self.saved_session_uuid()

    def saved_session_uuid(self):
        return state_store.get_session_uuid(state_store.load(self.spath))

    def run_fresh(self, scout, extra=(), **kwargs):
        """One FRESH owned flow (`--new`), returning `(rc, session_uuid)`.

        Always a new session rather than a resume: the session is minted and
        persisted before the lease is acquired, so the uuid is readable even
        from a run that ended in a refusal, and the role runner is guaranteed
        to be reached without depending on saved-phase resume semantics."""
        rc, _out = self.run_flow(["--new"] + list(extra), scout=scout,
                                 **kwargs)
        return rc, self.saved_session_uuid()

    # -- lease and binding seeding ----------------------------------------- #

    def claimant(self, session_uuid, entry_point="run_flow"):
        return owner.owner_identity(session_uuid, entry_point, self.project,
                                    self.spath)

    def seed_live_owner(self, session_uuid):
        """A genuinely LIVE lease: this process's own pid and start time, a
        fresh heartbeat, never released. `classify_owner_lease` returns
        `live_owner` for it, which is what a different session must be refused
        against."""
        return owner.acquire_owner_lease(session_uuid,
                                         self.claimant(session_uuid))

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

    def seed_dead_foreign_binding(self, controller, provider_session_id):
        """A different session that once held the binding and is no longer
        live -- the self-healing orphan case, which must NOT block."""
        foreign = str(uuid.uuid4())
        record = self.seed_live_owner(foreign)
        owner.bind_provider_session(
            controller, provider_session_id,
            {"session_uuid": foreign, "owner_id": record["owner_id"]},
            "builder")
        owner.release_owner_lease(foreign, record["owner_id"],
                                  record["epoch"], "normal_exit")
        return foreign

    def own_context(self, session_uuid=None):
        """Publish a context naming a lease this process genuinely holds."""
        session_uuid = session_uuid or str(uuid.uuid4())
        record = self.seed_live_owner(session_uuid)
        cowork._set_owner_context(session_uuid, record["owner_id"],
                                  record["epoch"])
        return session_uuid, record

    # -- trace reading ------------------------------------------------------ #

    def trace_events(self, session_uuid):
        with open(trace_store.trace_path_for(session_uuid), "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def run_end(self, session_uuid):
        ends = [e for e in self.trace_events(session_uuid)
                if e.get("event") == "run.end"]
        self.assertTrue(ends, "no run.end event was traced")
        return ends[-1]


# --------------------------------------------------------------------------- #
# F8(b) -- enforcement point 1: the pre-dispatch seam.                          #
# --------------------------------------------------------------------------- #


class PreDispatchSeamTests(ExclusivityTestCase):
    """`_owner_gate_fact`'s binding limb: the refusal that lands before
    `dispatch.decide()` can allow, and therefore before any controller session
    object exists."""

    def test_a_foreign_live_binding_produces_a_refusing_fact(self):
        session_uuid, _record = self.own_context()
        foreign = self.seed_foreign_live_binding("claude", "prov-sid-1")
        fact = cowork._owner_gate_fact("claude", "prov-sid-1", "launch")
        self.assertIsNotNone(fact)
        self.assertIs(fact["allowed"], False)
        self.assertEqual(fact["refusal_code"], "provider_session_bound")
        self.assertEqual(fact["source"], "owner_lease")
        self.assertIn(foreign, fact["refusal_message"])
        self.assertIn("prov-sid-1", fact["refusal_message"])
        self.assertNotEqual(foreign, session_uuid)

    def test_matched_stays_true_on_a_provider_refusal(self):
        """Decision pd-d. A foreign PROVIDER binding is not a loss of THIS
        session's ownership, so `matched` must stay True -- otherwise the
        terminal sidecar would misreport why the run ended."""
        self.own_context()
        self.seed_foreign_live_binding("claude", "prov-sid-2")
        fact = cowork._owner_gate_fact("claude", "prov-sid-2", "launch")
        self.assertIs(fact["allowed"], False)
        self.assertIs(cowork._OWNER_CONTEXT["matched"], True)

    def test_a_later_allow_drains_the_pending_conflict(self):
        """The carrier the raise site reads: it must hold the conflict for the
        refusal being decided RIGHT NOW and nothing else.

        Three properties nothing else in the tree measures. The role reaches
        the conflict through the new trailing keyword (or the operator block
        would say "for role None"); the slot is OVERWRITE-on-each-evaluation
        rather than first-refusal-wins; and a later allow CLEARS it, so a
        stale conflict can never be reported for a refusal it did not prove.
        The slot is read by key and never popped, which also pins the box's
        declared key set that `_restore_owner_context` copies wholesale."""
        self.own_context()
        foreign = self.seed_foreign_live_binding("claude", "prov-sid-11")
        fact = cowork._owner_gate_fact("claude", "prov-sid-11", "launch",
                                       "scout")
        self.assertIs(fact["allowed"], False)
        self.assertEqual(fact["refusal_code"], "provider_session_bound")
        pending = cowork._OWNER_CONTEXT["pending_dispatch_conflict"]
        self.assertIsInstance(pending, owner.ProviderSessionConflict)
        self.assertEqual(pending.owner_session_uuid, foreign)
        self.assertEqual(pending.role, "scout")
        self.assertEqual(cowork._owner_gate_fact("claude", "never-bound-11",
                                                 "launch", "scout"),
                         dict(_ALLOW))
        self.assertIsNone(cowork._OWNER_CONTEXT["pending_dispatch_conflict"])

    def test_a_same_session_binding_allows(self):
        session_uuid, record = self.own_context()
        owner.bind_provider_session(
            "claude", "prov-sid-3",
            {"session_uuid": session_uuid, "owner_id": record["owner_id"]},
            "scout")
        self.assertEqual(cowork._owner_gate_fact("claude", "prov-sid-3",
                                                 "launch"),
                         dict(_ALLOW))

    def test_a_binding_whose_owner_is_not_live_allows(self):
        """F8(e) at the dispatch seam: an abandoned binding self-heals rather
        than blocking forever."""
        self.own_context()
        self.seed_dead_foreign_binding("claude", "prov-sid-4")
        self.assertEqual(cowork._owner_gate_fact("claude", "prov-sid-4",
                                                 "launch"),
                         dict(_ALLOW))

    def test_an_absent_binding_allows(self):
        self.own_context()
        self.assertEqual(cowork._owner_gate_fact("claude", "never-bound",
                                                 "launch"),
                         dict(_ALLOW))

    def test_the_evaluator_purpose_returns_before_the_limb(self):
        """G11b / rule E2 is not weakened: the exemption returns None ABOVE
        the binding limb, proven by a reader that raises if it is reached."""
        self.own_context()
        self.seed_foreign_live_binding("claude", "prov-sid-5")
        with mock.patch.object(owner, "read_provider_binding",
                               _Raises("read_provider_binding")):
            self.assertIsNone(
                cowork._owner_gate_fact("claude", "prov-sid-5", "evaluator"))

    def test_no_resume_id_means_no_binding_lookup(self):
        """There is nothing to be exclusive about when no provider
        conversation is being resumed, so the index is not consulted at all."""
        self.own_context()
        with mock.patch.object(owner, "read_provider_binding",
                               _Raises("read_provider_binding")):
            self.assertEqual(cowork._owner_gate_fact("claude", None, "launch"),
                             dict(_ALLOW))
            self.assertEqual(cowork._owner_gate_fact(None, "prov-sid-6",
                                                     "launch"),
                             dict(_ALLOW))

    def test_an_unenforced_context_contributes_no_fact(self):
        """N1 parity: with no lease held the limb is unreachable, so
        `--no-session` behaviour is unchanged by the exclusivity limb."""
        self.assertIs(cowork._OWNER_CONTEXT["enforced"], False)
        with mock.patch.object(owner, "read_provider_binding",
                               _Raises("read_provider_binding")):
            self.assertIsNone(
                cowork._owner_gate_fact("claude", "prov-sid-7", "launch"))

    def test_the_dispatch_seam_raises_after_evidencing_the_refusal(self):
        """The refusal is FULLY EVIDENCED before the raise, and the raise --
        not a returned decision -- is what stops each call site from falling
        through to its own `preflight_rejected` branch.

        Declared asymmetry 1, RESOLVED: the class raised is the
        `ProviderSessionConflict` the gate already built, so the exception's
        typed reason is `provider_session_bound` -- the same code the decision
        carries -- and the object naming the FOREIGN session, carrying the real
        role, is what reaches catch point 1's renderer."""
        self.own_context()
        foreign = self.seed_foreign_live_binding("claude", "prov-sid-8")
        trace = _Trace()
        with self.assertRaises(owner.ProviderSessionConflict) as caught:
            cowork._decide_and_trace(trace, "scout", "claude", "launch",
                                     "cowork.py:test",
                                     resume_session_id="prov-sid-8")
        self.assertEqual([n for n, _ in trace.events],
                         ["dispatch.contract", "dispatch.decision"])
        decision = trace.events[1][1]
        self.assertEqual(decision["outcome"], "refuse")
        self.assertEqual(decision["refusal_code"], "provider_session_bound")
        self.assertEqual(decision["source"], "owner_lease")
        # The run-ending reason now matches the decision's own refusal code.
        # `owner_refusal_reason`, never `exception.reason`: `reason` is a
        # property of `OwnerLeaseLost` alone.
        self.assertEqual(owner.owner_refusal_reason(caught.exception),
                         "provider_session_bound")
        # And the raised object is the FOREIGN-naming conflict carrying the
        # real role -- which is what makes the operator's block correct.
        self.assertEqual(caught.exception.owner_session_uuid, foreign)
        self.assertEqual(caught.exception.role, "scout")

    def test_an_unreadable_index_propagates_without_a_dispatch_decision(self):
        """DECLARED ASYMMETRY 2. `provider_binding_unavailable` is absent from
        the dispatch refusal vocabulary, so the condition cannot be expressed
        as a refusing fact. It
        propagates instead -- fail-closed and typed -- at the declared cost of
        the two dispatch events not being emitted on this one path."""
        self.own_context()
        trace = _Trace()
        boom = owner.ProviderBindingUnavailable("claude", "prov-sid-9",
                                                detail="index unreadable")
        with mock.patch.object(owner, "read_provider_binding",
                               side_effect=boom):
            with self.assertRaises(owner.ProviderBindingUnavailable):
                cowork._owner_gate_fact("claude", "prov-sid-9", "launch")
            with self.assertRaises(owner.ProviderBindingUnavailable):
                cowork._decide_and_trace(trace, "scout", "claude", "launch",
                                         "cowork.py:test",
                                         resume_session_id="prov-sid-9")
        self.assertEqual(trace.events, [])
        self.assertEqual(owner.owner_refusal_reason(boom),
                         "provider_binding_unavailable")

    def test_a_refused_run_constructs_nothing_and_spawns_nothing(self):
        """G1b, extended to the provider limb. A REAL `run_flow` under a valid
        own lease reaches the seam with a resume id, and every path a paid
        dispatch must cross is a double that RAISES on call.

        Claims, phase advances and process creations are asserted as "none
        caused BY the refusal" rather than "none at all in the whole run": a
        healthy run legitimately advances its own phases and runs its own
        measurement checkpoints before ever reaching this seam, and pinning the
        DELTA across the seam is the honest form of the claim. The absolute
        assertion is kept where it is unambiguous -- no controller process is
        created at any point, and every controller-construction seam is a
        double that raises on call."""
        foreign = self.seed_foreign_live_binding("claude", "prov-sid-10")
        advance = _Counter(cowork._advance_phase)
        spawns = _PopenLog()
        seen = {}

        def scout(config, context, selected, **kwargs):
            seen["fact"] = cowork._owner_gate_fact("claude", "prov-sid-10",
                                                   "launch")
            seen["advance_before"] = advance.count
            seen["spawn_before"] = len(spawns.calls)
            cowork._decide_and_trace(None, "scout", "claude", "launch",
                                     "cowork.py:test",
                                     resume_session_id="prov-sid-10")
            raise AssertionError("the dispatch seam did not refuse")

        with mock.patch.object(cowork, "_advance_phase", advance), \
                mock.patch.object(cowork.capacity_scheduler, "claim",
                                  _Raises("capacity_scheduler.claim")), \
                mock.patch.object(bridge, "_real_claude_spawn",
                                  _Raises("_real_claude_spawn")), \
                mock.patch.object(bridge, "ClaudeSession",
                                  _Raises("ClaudeSession")), \
                mock.patch.object(bridge, "CodexSession",
                                  _Raises("CodexSession")), \
                mock.patch.object(bridge, "OpencodeSession",
                                  _Raises("OpencodeSession")), \
                mock.patch.object(cowork, "_isolated_evaluator_session",
                                  _Raises("_isolated_evaluator_session")), \
                mock.patch.object(cowork, "_construct_resume_session",
                                  _Raises("_construct_resume_session")), \
                mock.patch.object(subprocess, "Popen", spawns):
            # `run_flow` rather than `run_fresh`: the operator's refusal text
            # is what this fixture has to read, and `run_fresh` discards
            # stdout. Its return contract stays untouched -- three other
            # fixtures outside this package's authority depend on it -- so the
            # uuid is taken separately, exactly as `run_fresh` itself takes it.
            rc, out = self.run_flow(["--new"], scout=scout)
            session_uuid = self.saved_session_uuid()

        self.assertEqual(rc, 3)
        self.assertEqual(seen["fact"]["refusal_code"],
                         "provider_session_bound")
        # Zero phase advances and zero claims caused BY the refusal.
        self.assertEqual(advance.count, seen["advance_before"])
        # Zero process creations after the seam, except the ownership gate's
        # own `ps` liveness probe -- which is the PID-reuse defence, not a
        # dispatch (`cowork_owner._probe_pid_start`).
        self.assertEqual(set(spawns.calls[seen["spawn_before"]:]) - {"ps"},
                         set())
        # And no controller process was created anywhere in the run.
        self.assertEqual(
            {"claude", "codex", "opencode"} & set(spawns.calls), set())
        # Declared asymmetry 1 RESOLVED, end to end through a real run: the
        # run ends under the same code its own dispatch decision recorded.
        end = self.run_end(session_uuid)
        self.assertEqual(end.get("rc"), 3)
        self.assertEqual(end.get("reason"), "provider_session_bound")
        # The operator surface itself. Assertions are scoped to the refusal
        # BLOCK rather than to raw stdout: `run_flow` returns the WHOLE run's
        # output, so "this run's own uuid appears nowhere" specified over
        # stdout would fail for reasons having nothing to do with the refusal.
        # The ANCHOR is what makes the narrower scope sound -- the text catch
        # point 1 actually wrote is byte-identical to the renderer's output for
        # this same conflict.
        conflict = owner.ProviderSessionConflict("claude", "prov-sid-10",
                                                 foreign, "scout")
        block = owner.refusal_message(conflict)
        self.assertIn(block, out)
        self.assertNotEqual(foreign, session_uuid)
        self.assertIn(foreign, block)
        self.assertNotIn(session_uuid, block)
        self.assertIn("reason    provider_session_bound", block)
        self.assertIn("for role scout", block)
        self.assertNotIn("role None", block)


# --------------------------------------------------------------------------- #
# F8(c) -- enforcement point 2: the headless resume trigger, pre-construction.  #
# --------------------------------------------------------------------------- #


class PreConstructionSeamTests(ExclusivityTestCase):
    """`run_resume_trigger` step 1. Placed after every pre-existing preflight
    check and BEFORE the ownership acquire, so a refusal here has claimed
    nothing, advanced no phase, constructed no controller session and sent
    nothing."""

    LEASE = {"lease_id": "lease-1", "role": "scout"}

    def state_with(self, provider_session_id):
        return {"config": {"scout": {"controller": "claude"}},
                "sessions": {"scout": {"controller": "claude",
                                       "id": provider_session_id}}}

    def harness(self, state, pending=True, work_id="work-1",
                candidate={"candidate": "c"}, mismatch=None, extra=()):
        """Every read-only preflight dependency stubbed to PASS, so the only
        thing under test is the new exclusivity check and its placement."""
        pending_record = ({"acknowledged": True, "lease_id": "lease-1"}
                          if pending else None)
        return [
            mock.patch.object(cowork.capacity_scheduler, "wake_decision",
                              lambda *_a, **_k: "due"),
            mock.patch.object(state_store, "read_pause_lease",
                              lambda *_a, **_k: {"lease_id": "lease-1"}),
            mock.patch.object(state_store, "pause_lease_from_stored_record",
                              lambda record: record),
            mock.patch.object(cowork.capacity_contracts,
                              "validate_pause_lease",
                              lambda record: dict(self.LEASE)),
            mock.patch.object(cowork, "_current_role_work_id_for_session",
                              lambda *_a, **_k: work_id),
            mock.patch.object(cowork, "_capacity_candidate_binding",
                              lambda *_a, **_k: candidate),
            mock.patch.object(cowork, "_find_session_state",
                              lambda *_a, **_k: state),
            mock.patch.object(cowork, "_resume_wake_failure_kind",
                              lambda *_a, **_k: mismatch),
            mock.patch.object(state_store, "read_pending_turn_before_pause",
                              lambda *_a, **_k: pending_record),
        ] + list(extra)

    def trigger(self, session_uuid, patches, argv_extra=()):
        lines = []
        with _nested(patches):
            rc = cowork.run_resume_trigger(
                ["--session-uuid", session_uuid, "--lease-id", "lease-1",
                 "--claimant-ref", "test", "--automation-ref", "test",
                 "--cwd", self.project] + list(argv_extra),
                output=lines.append)
        return rc, lines

    def test_a_foreign_live_binding_refuses_with_exit_eleven(self):
        session_uuid = str(uuid.uuid4())
        foreign = self.seed_foreign_live_binding("claude", "prov-sid-a")
        wake = _Counter()
        patches = self.harness(self.state_with("prov-sid-a"), extra=[
            mock.patch.object(cowork, "_account_failed_wake_attempt", wake),
            mock.patch.object(owner, "acquire_owner_lease",
                              _Raises("acquire_owner_lease")),
            mock.patch.object(cowork.capacity_scheduler, "claim",
                              _Raises("capacity_scheduler.claim")),
            mock.patch.object(cowork, "_advance_phase",
                              _Raises("_advance_phase")),
            mock.patch.object(cowork, "_construct_resume_session",
                              _Raises("_construct_resume_session")),
            mock.patch.object(cowork, "_send", _Raises("_send")),
            mock.patch.object(subprocess, "Popen", _guarded_popen()),
        ])
        rc, lines = self.trigger(session_uuid, patches)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_PROVIDER_SESSION_BOUND)
        payload = json.loads(lines[-1])
        self.assertEqual(payload["outcome"], "provider_session_bound")
        self.assertEqual(payload["owner_session_uuid"], foreign)
        self.assertEqual(payload["controller"], "claude")
        self.assertEqual(payload["provider_session_id"], "prov-sid-a")
        self.assertEqual(payload["lease_id"], "lease-1")
        # Decision pd-b: an exclusivity refusal is not a failed WAKE ATTEMPT.
        # Nothing was attempted, so nothing may be charged.
        self.assertEqual(wake.count, 0)
        # And no lease was ever acquired for this session.
        self.assertIsNone(owner.read_owner_lease(session_uuid))

    def test_an_unreadable_index_refuses_with_exit_twelve(self):
        session_uuid = str(uuid.uuid4())
        wake = _Counter()
        boom = owner.ProviderBindingUnavailable("claude", "prov-sid-b",
                                                detail="index unreadable")
        patches = self.harness(self.state_with("prov-sid-b"), extra=[
            mock.patch.object(cowork, "_account_failed_wake_attempt", wake),
            mock.patch.object(owner, "read_provider_binding",
                              side_effect=boom),
            mock.patch.object(owner, "acquire_owner_lease",
                              _Raises("acquire_owner_lease")),
            mock.patch.object(cowork.capacity_scheduler, "claim",
                              _Raises("capacity_scheduler.claim")),
        ])
        rc, lines = self.trigger(session_uuid, patches)
        self.assertEqual(
            rc, cowork.RESUME_TRIGGER_EXIT_PROVIDER_BINDING_UNAVAILABLE)
        payload = json.loads(lines[-1])
        self.assertEqual(payload["outcome"], "provider_binding_unavailable")
        self.assertIn("index unreadable", payload["detail"])
        self.assertEqual(wake.count, 0)
        self.assertIsNone(owner.read_owner_lease(session_uuid))

    def _assert_falls_through(self, session_uuid, provider_session_id):
        """The check must be INVISIBLE on every non-colliding path: control
        reaches the pre-existing acquire block and then step 2's claim, which
        is stubbed to refuse with a pre-existing code."""
        patches = self.harness(self.state_with(provider_session_id), extra=[
            mock.patch.object(
                cowork.capacity_scheduler, "claim",
                mock.Mock(side_effect=cowork.capacity_scheduler
                          .SchedulerLeaseConflict("lease-1",
                                                  "already_claimed"))),
        ])
        rc, lines = self.trigger(session_uuid, patches)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_CONFLICT)
        self.assertEqual(json.loads(lines[-1])["outcome"], "conflict")

    def test_a_same_session_binding_falls_through(self):
        session_uuid = str(uuid.uuid4())
        record = self.seed_live_owner(session_uuid)
        owner.bind_provider_session(
            "claude", "prov-sid-c",
            {"session_uuid": session_uuid, "owner_id": record["owner_id"]},
            "scout")
        owner.release_owner_lease(session_uuid, record["owner_id"],
                                  record["epoch"], "normal_exit")
        self._assert_falls_through(session_uuid, "prov-sid-c")

    def test_a_non_live_foreign_binding_falls_through(self):
        session_uuid = str(uuid.uuid4())
        self.seed_dead_foreign_binding("claude", "prov-sid-d")
        self._assert_falls_through(session_uuid, "prov-sid-d")

    def test_an_absent_binding_falls_through(self):
        self._assert_falls_through(str(uuid.uuid4()), "prov-sid-e")

    def test_a_session_with_no_provider_id_falls_through(self):
        """`provider_session_id` is None for a role that has never reported
        one -- the check is guarded on it, so nothing is consulted."""
        session_uuid = str(uuid.uuid4())
        patches = self.harness(
            {"config": {"scout": {"controller": "claude"}}}, extra=[
                mock.patch.object(owner, "read_provider_binding",
                                  _Raises("read_provider_binding")),
                mock.patch.object(
                    cowork.capacity_scheduler, "claim",
                    mock.Mock(side_effect=cowork.capacity_scheduler
                              .SchedulerLeaseConflict("lease-1",
                                                      "already_claimed"))),
            ])
        rc, _lines = self.trigger(session_uuid, patches)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_CONFLICT)

    def test_the_pre_existing_preflight_refusals_keep_their_precedence(self):
        """Placement, asserted positively: every refusal that came before the
        new check still wins, and still returns its own pre-existing code, even
        with a colliding binding on file."""
        self.seed_foreign_live_binding("claude", "prov-sid-f")
        cases = (
            ("role_mismatch",
             dict(extra_argv=["--role", "planner"]),
             cowork.RESUME_TRIGGER_EXIT_BINDING_MISMATCH),
            ("candidate_mismatch",
             dict(work_id=None),
             cowork.RESUME_TRIGGER_EXIT_BINDING_MISMATCH),
            ("no_pending_turn",
             dict(pending=False),
             cowork.RESUME_TRIGGER_EXIT_NO_PENDING_TURN),
        )
        for reason, kwargs, expected in cases:
            with self.subTest(reason):
                session_uuid = str(uuid.uuid4())
                extra_argv = kwargs.pop("extra_argv", ())
                patches = self.harness(
                    self.state_with("prov-sid-f"),
                    extra=[mock.patch.object(
                        cowork, "_account_failed_wake_attempt", _Counter())],
                    **kwargs)
                rc, lines = self.trigger(session_uuid, patches, extra_argv)
                self.assertEqual(rc, expected)
                payload = json.loads(lines[-1])
                self.assertEqual(payload["outcome"], "conflict")
                self.assertEqual(payload["reason"], reason)


# --------------------------------------------------------------------------- #
# F8(f) -- enforcement point 3: the durable backstop, bind before persist.      #
# --------------------------------------------------------------------------- #


class DurableBackstopSeamTests(ExclusivityTestCase):
    """`run_flow`'s `role_saver().on_sess`.

    The callback under test is the REAL production closure: `run_flow` passes
    `role_saver("scout")` in as `on_session` and `role_saver(SCOUT_REVIEWER)`
    in as `on_reviewer_session`, so an injected role runner receives both and
    can invoke them exactly as the bridge's own `on_session_id` seam does.
    That is what makes these arms cover all three call chains -- the bridge's
    mid-turn notification, the reviewer's persistent session and a lead role's
    fresh mint -- without faking the seam."""

    def _conflicting_run(self, role_kwarg, sid):
        """Run one owned flow whose role runner reports `sid` for a provider
        conversation a live foreign session already owns."""
        foreign = self.seed_foreign_live_binding("claude", sid)
        saves = _Counter(state_store.save_role_session)
        seen = {}

        def scout(config, context, selected, **kwargs):
            seen["enforced"] = cowork._OWNER_CONTEXT["enforced"]
            kwargs[role_kwarg]("claude", sid)
            seen["pending"] = cowork._OWNER_CONTEXT["provider_conflict"]
            return 0

        with mock.patch.object(state_store, "save_role_session", saves):
            rc, session_uuid = self.run_fresh(scout)
        return session_uuid, foreign, saves, seen, rc

    def test_arm_a_a_conflict_persists_nothing_on_the_lead_role_chain(self):
        """The chain the bridge's `on_session_id` drives: an id observed
        mid-turn is bound BEFORE it is persisted, and a proven collision
        persists nothing at all."""
        session_uuid, foreign, saves, seen, rc = self._conflicting_run(
            "on_session", "prov-sid-A")
        self.assertIs(seen["enforced"], True)
        self.assertEqual(saves.count, 0)
        state = state_store.load(self.spath)
        self.assertIsNone(
            state_store.get_role_session(state, "scout", "claude"))
        events = [e for e in self.trace_events(session_uuid)
                  if e.get("event") == "owner.provider_session_conflict"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("owner_session_uuid"), foreign)
        self.assertEqual(events[0].get("session_id"), "prov-sid-A")
        self.assertEqual(events[0].get("role"), "scout")
        # No `role.session_saved` may follow a refused bind.
        self.assertEqual(
            [e for e in self.trace_events(session_uuid)
             if e.get("event") == "role.session_saved"
             and e.get("session_id") == "prov-sid-A"], [])
        self.assertEqual(rc, 3)

    def test_arm_c1_the_reviewer_chain_behaves_identically(self):
        session_uuid, _foreign, saves, _seen, rc = self._conflicting_run(
            "on_reviewer_session", "prov-sid-C1")
        self.assertEqual(saves.count, 0)
        state = state_store.load(self.spath)
        self.assertIsNone(state_store.get_role_session(
            state, cowork.SCOUT_REVIEWER, "claude"))
        events = [e for e in self.trace_events(session_uuid)
                  if e.get("event") == "owner.provider_session_conflict"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("role"), cowork.SCOUT_REVIEWER)
        self.assertEqual(rc, 3)

    def test_arm_c2_a_freshly_minted_id_is_refused_too(self):
        """The fresh-mint chain: no id was ever saved for this role, so the
        seam is the FIRST place the collision could be seen -- and it still
        writes no id.

        The claim is deliberately "no ID was persisted", not "no entry
        exists". A role's `sessions[role]` entry legitimately carries
        BOOKKEEPING written by other parts of the run (notably
        `last_context_revision_seen`), which is exactly why
        `save_role_session`'s own docstring says it MERGES into that entry
        rather than replacing it. Asserting the entry is absent would pin an
        unrelated fact and fail a correct candidate."""
        session_uuid, _foreign, saves, _seen, rc = self._conflicting_run(
            "on_session", "prov-sid-C2")
        state = state_store.load(self.spath)
        entry = (state.get("sessions") or {}).get("scout") or {}
        self.assertNotIn("id", entry)
        self.assertNotIn("controller", entry)
        self.assertIsNone(
            state_store.get_role_session(state, "scout", "claude"))
        self.assertEqual(saves.count, 0)
        self.assertEqual(rc, 3)
        self.assertEqual(self.run_end(session_uuid).get("reason"),
                         "provider_session_bound")

    def test_the_conflict_surfaces_at_the_next_governed_seam(self):
        """The declared deferral window: the refusal is
        recorded during the callback and RE-RAISED at the next governed seam,
        inside `run_flow`'s own frame, rather than crossing the callback
        boundary into the send gateway's `except Exception`."""
        session_uuid, _foreign, _saves, seen, rc = self._conflicting_run(
            "on_session", "prov-sid-D")
        # Recorded during the callback, still pending when it returned ...
        self.assertIsInstance(seen["pending"], owner.ProviderSessionConflict)
        # ... and the run ends on it, typed.
        self.assertEqual(rc, 3)
        end = self.run_end(session_uuid)
        self.assertEqual(end.get("rc"), 3)
        self.assertEqual(end.get("reason"), "provider_session_bound")

    def test_the_window_cannot_span_a_whole_run(self):
        """`run_flow`'s `session.end` checkpoint is unconditional and fences
        first, so the deferral is bounded by the run rather than open-ended.
        Asserted structurally: the fenced evaluation boundary the checkpoint
        goes through calls `_require_owner` as its first statement."""
        closures = _closures_of(
            _named_top_level(_cowork_tree())["run_flow"])
        nodes = closures["evaluation_transition"]
        self.assertEqual(len(nodes), 1)
        body = list(nodes[0].body)
        if (isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]
        self.assertEqual(ast.unparse(body[0]), "_require_owner(session_uuid)")
        checkpoint = closures["measurement_checkpoint"][0]
        self.assertIn("evaluation_transition", _call_names(checkpoint))

    def test_a_pending_conflict_is_drained_exactly_once(self):
        session_uuid, record = self.own_context()
        pending = owner.ProviderSessionConflict("claude", "prov-sid-E",
                                                str(uuid.uuid4()), "scout")
        cowork._record_provider_conflict(pending)
        with self.assertRaises(owner.ProviderSessionConflict) as caught:
            cowork._require_owner(session_uuid)
        self.assertIs(caught.exception, pending)
        self.assertIsNone(cowork._OWNER_CONTEXT["provider_conflict"])
        # Drained: the second call falls through to the ordinary lease check,
        # which passes because this process genuinely holds the lease.
        self.assertIsNone(cowork._require_owner(session_uuid))
        self.assertEqual(record["epoch"], cowork._OWNER_CONTEXT["epoch"])

    def test_the_first_refusal_wins(self):
        """A later `ProviderBindingUnavailable` must never displace an earlier
        `ProviderSessionConflict`: proof outranks an unverifiable index."""
        self.own_context()
        first = owner.ProviderSessionConflict("claude", "sid", "other",
                                              "scout")
        second = owner.ProviderBindingUnavailable("claude", "sid",
                                                  detail="later")
        cowork._record_provider_conflict(first)
        cowork._record_provider_conflict(second)
        self.assertIs(cowork._OWNER_CONTEXT["provider_conflict"], first)

    def test_recording_is_a_no_op_without_a_lease(self):
        self.assertIs(cowork._OWNER_CONTEXT["enforced"], False)
        cowork._record_provider_conflict(
            owner.ProviderSessionConflict("claude", "sid", "other", "scout"))
        self.assertIsNone(cowork._OWNER_CONTEXT["provider_conflict"])

    def test_no_session_never_binds_and_persists_exactly_as_before(self):
        """N1. Under `--no-session` no lease is held, so the seam is inert:
        `bind_provider_session` is never called and the pre-existing
        in-run-only persistence path is unchanged."""
        seen = {}

        def scout(config, context, selected, **kwargs):
            seen["enforced"] = cowork._OWNER_CONTEXT["enforced"]
            kwargs["on_session"]("claude", "prov-sid-F")
            kwargs["on_outcome"]("approved", None)
            return 0

        with mock.patch.object(owner, "bind_provider_session",
                               _Raises("bind_provider_session")):
            rc, _out = self.run_flow(["--no-session"], scout=scout)
        self.assertEqual(rc, 0)
        self.assertIn("enforced", seen, "the fake scout lead never ran")
        self.assertIs(seen["enforced"], False)


# --------------------------------------------------------------------------- #
# F8(a) / F8(d) / F8(e) -- exclusivity follows liveness, not the record.        #
# --------------------------------------------------------------------------- #


class LivenessScopingTests(ExclusivityTestCase):
    """Driven through the REAL `cowork_owner` binding functions with an
    injected clock and a real temporary `COWORK_SESSIONS_ROOT`. No fixture here
    relies on real `ps` timing (risk R2): every deadline is crossed by passing
    `now=`, never by sleeping."""

    def setUp(self):
        super().setUp()
        self.session_uuid = str(uuid.uuid4())
        self.record = self.seed_live_owner(self.session_uuid)
        self.owner_ref = {"session_uuid": self.session_uuid,
                          "owner_id": self.record["owner_id"]}

    def _past_deadline(self):
        deadline = datetime.datetime.fromisoformat(
            self.record["lease_deadline_at"].replace("Z", "+00:00"))
        return deadline + datetime.timedelta(seconds=1)

    def _take_over(self):
        """A real takeover of this session by a successor, reached the only
        way the store permits: the incumbent is provably dead and the clock is
        past its deadline."""
        fabricated = dict(self.record)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        return owner.take_over(self.session_uuid,
                               self.claimant(self.session_uuid),
                               "proved_dead", now=self._past_deadline())

    def test_f8a_a_binding_blocks_only_a_live_foreign_session(self):
        owner.bind_provider_session("claude", "sid-live", self.owner_ref,
                                    "builder")
        other = str(uuid.uuid4())
        other_record = self.seed_live_owner(other)
        other_ref = {"session_uuid": other,
                     "owner_id": other_record["owner_id"]}
        with self.assertRaises(owner.ProviderSessionConflict) as caught:
            owner.bind_provider_session("claude", "sid-live", other_ref,
                                        "scout")
        self.assertEqual(caught.exception.owner_session_uuid,
                         self.session_uuid)
        # Nothing was overwritten by the refusal.
        self.assertEqual(
            owner.read_provider_binding("claude",
                                        "sid-live")["owner_session_uuid"],
            self.session_uuid)

    def test_f8a_the_same_session_rebinds_across_a_takeover(self):
        """Exclusivity is per SESSION, not per owner: a successor keeps its
        predecessor's provider conversations without any hand-off step."""
        owner.bind_provider_session("claude", "sid-rebind", self.owner_ref,
                                    "builder")
        successor = self._take_over()
        self.assertNotEqual(successor["owner_id"], self.record["owner_id"])
        rebound = owner.bind_provider_session(
            "claude", "sid-rebind",
            {"session_uuid": self.session_uuid,
             "owner_id": successor["owner_id"]}, "builder")
        self.assertEqual(rebound["bound_by_owner_id"], successor["owner_id"])
        self.assertEqual(rebound["owner_session_uuid"], self.session_uuid)

    def test_f8d_a_predecessor_cannot_delete_a_successors_bindings(self):
        owner.bind_provider_session("claude", "sid-inherited", self.owner_ref,
                                    "builder")
        path = state_store.provider_session_binding_path_for(
            "claude", "sid-inherited")
        self._take_over()
        with open(path, "rb") as fh:
            before = _sha256(fh.read())
        self.assertEqual(
            owner.release_provider_bindings(self.session_uuid,
                                            self.record["owner_id"]), 0)
        with open(path, "rb") as fh:
            self.assertEqual(_sha256(fh.read()), before)

    def test_f8e_an_orphan_binding_self_heals_with_no_sweep_step(self):
        """The same key blocks a foreign session while the owning lease is
        live, and stops blocking once it is not -- with nothing anywhere in the
        path that sweeps, expires or repairs the record."""
        owner.bind_provider_session("claude", "sid-orphan", self.owner_ref,
                                    "builder")
        other = str(uuid.uuid4())
        other_record = self.seed_live_owner(other)
        other_ref = {"session_uuid": other,
                     "owner_id": other_record["owner_id"]}
        with self.assertRaises(owner.ProviderSessionConflict):
            owner.bind_provider_session("claude", "sid-orphan", other_ref,
                                        "scout")
        owner.release_owner_lease(self.session_uuid, self.record["owner_id"],
                                  self.record["epoch"], "normal_exit")
        adopted = owner.bind_provider_session("claude", "sid-orphan",
                                              other_ref, "scout")
        self.assertEqual(adopted["owner_session_uuid"], other)
        # The record itself was never removed in between -- adoption is an
        # overwrite decided by liveness, not a cleanup pass.
        self.assertEqual(adopted["role"], "scout")

    def test_release_is_bounded_to_the_keys_this_process_bound(self):
        """Plan section 3.6, "bounded": release iterates `_BIND_LOG`, never the
        whole global binding directory. Proven by planting a record this
        process did not bind and showing it survives."""
        owner.bind_provider_session("claude", "sid-mine", self.owner_ref,
                                    "builder")
        planted = state_store.provider_session_binding_path_for(
            "claude", "sid-planted")
        os.makedirs(os.path.dirname(planted), exist_ok=True)
        with open(planted, "w") as fh:
            json.dump({"schema_version": owner.OWNER_LEASE_SCHEMA_VERSION,
                       "record": "ProviderSessionBinding",
                       "controller": "claude",
                       "provider_session_id": "sid-planted",
                       "owner_session_uuid": self.session_uuid,
                       "role": "builder",
                       "bound_by_owner_id": self.record["owner_id"]}, fh)
        self.assertEqual(
            owner.release_provider_bindings(self.session_uuid,
                                            self.record["owner_id"]), 1)
        self.assertIsNone(owner.read_provider_binding("claude", "sid-mine"))
        self.assertIsNotNone(
            owner.read_provider_binding("claude", "sid-planted"))

    def test_the_gate_seam_agrees_with_the_store_on_liveness(self):
        """The inline composition in `cowork.py` (read + classify) and the
        store's own in-transaction rule must reach the SAME verdict, or the two
        seams could disagree about the same record."""
        owner.bind_provider_session("claude", "sid-agree", self.owner_ref,
                                    "builder")
        other, _record = self.own_context()
        fact = cowork._owner_gate_fact("claude", "sid-agree", "launch")
        self.assertIs(fact["allowed"], False)
        owner.release_owner_lease(self.session_uuid, self.record["owner_id"],
                                  self.record["epoch"], "normal_exit")
        self.assertEqual(cowork._owner_gate_fact("claude", "sid-agree",
                                                 "launch"),
                         dict(_ALLOW))
        self.assertNotEqual(other, self.session_uuid)


# --------------------------------------------------------------------------- #
# F8(g) + N10 -- typed propagation, and no paid session left unpersisted.       #
# --------------------------------------------------------------------------- #


class TypedPropagationTests(ExclusivityTestCase):
    """Five failure arms. Each proves the same two things: the underlying cause
    survives as `__cause__`, and the paid provider session id is STILL
    persisted -- because an unverifiable index proves nothing, and stranding a
    live conversation outside the durable record that names it is the defect
    this issue exists to remove."""

    def _unavailable_run(self, sid, injection=None):
        """One owned flow whose role runner reports `sid` while the binding
        index is failing for `injection`'s reason. The injection is scoped to
        the callback itself, so the real translation inside
        `bind_provider_session` is what produces the exception."""
        seen = {}

        def scout(config, context, selected, **kwargs):
            if injection is None:
                kwargs["on_session"]("claude", sid)
            else:
                with mock.patch.object(state_store,
                                       "_locked_json_transaction",
                                       side_effect=injection):
                    kwargs["on_session"]("claude", sid)
            seen["pending"] = cowork._OWNER_CONTEXT["provider_conflict"]
            return 0

        rc, session_uuid = self.run_fresh(scout)
        return session_uuid, seen, rc

    def _assert_persisted(self, session_uuid, sid):
        state = state_store.load(self.spath)
        self.assertEqual(state["sessions"]["scout"]["id"], sid)
        self.assertEqual(state_store.get_role_session(state, "scout",
                                                      "claude"), sid)
        # And the durable resolver -- the thing a later resume depends on --
        # really does find it.
        with mock.patch.object(os, "getcwd", lambda: self.project):
            self.assertEqual(
                cowork._durable_provider_session_id(session_uuid, "scout",
                                                    "claude"), sid)

    def _assert_typed_end(self, session_uuid, rc, seen, cause_type):
        self.assertIsInstance(seen["pending"],
                              owner.ProviderBindingUnavailable)
        self.assertIsInstance(seen["pending"].__cause__, cause_type)
        self.assertEqual(rc, 3)
        end = self.run_end(session_uuid)
        self.assertEqual(end.get("rc"), 3)
        self.assertEqual(end.get("reason"), "provider_binding_unavailable")

    def test_sub_arm_i_a_lock_timeout(self):
        session_uuid, seen, rc = self._unavailable_run(
            "prov-sid-i", TimeoutError("lock"))
        self._assert_typed_end(session_uuid, rc, seen, TimeoutError)
        self._assert_persisted(session_uuid, "prov-sid-i")

    def test_sub_arm_ii_an_io_failure(self):
        session_uuid, seen, rc = self._unavailable_run(
            "prov-sid-ii", OSError("io"))
        self._assert_typed_end(session_uuid, rc, seen, OSError)
        self._assert_persisted(session_uuid, "prov-sid-ii")

    def test_sub_arm_iii_a_corrupt_record(self):
        session_uuid, seen, rc = self._unavailable_run(
            "prov-sid-iii", state_store.CorruptRecordError("damaged"))
        self._assert_typed_end(session_uuid, rc, seen,
                               state_store.CorruptRecordError)
        self._assert_persisted(session_uuid, "prov-sid-iii")

    def test_sub_arm_iv_an_unsafe_identifier(self):
        """No injection at all: a provider session id outside
        `_SAFE_IDENTIFIER_RE` is rejected by the real path helper, and the
        translation is the store's own."""
        session_uuid, seen, rc = self._unavailable_run("bad/id")
        self._assert_typed_end(session_uuid, rc, seen, ValueError)
        self._assert_persisted(session_uuid, "bad/id")

    def test_sub_arm_v_the_resume_trigger_returns_twelve(self):
        session_uuid = str(uuid.uuid4())
        boom = owner.ProviderBindingUnavailable("claude", "prov-sid-v",
                                                detail="unreadable")
        lines = []
        managers = [
            mock.patch.object(cowork.capacity_scheduler, "wake_decision",
                              lambda *_a, **_k: "due"),
            mock.patch.object(state_store, "read_pause_lease",
                              lambda *_a, **_k: {"lease_id": "lease-1"}),
            mock.patch.object(state_store, "pause_lease_from_stored_record",
                              lambda record: record),
            mock.patch.object(cowork.capacity_contracts,
                              "validate_pause_lease",
                              lambda record: {"lease_id": "lease-1",
                                              "role": "scout"}),
            mock.patch.object(cowork, "_current_role_work_id_for_session",
                              lambda *_a, **_k: "work-1"),
            mock.patch.object(cowork, "_capacity_candidate_binding",
                              lambda *_a, **_k: {"candidate": "c"}),
            mock.patch.object(
                cowork, "_find_session_state",
                lambda *_a, **_k: {
                    "config": {"scout": {"controller": "claude"}},
                    "sessions": {"scout": {"controller": "claude",
                                           "id": "prov-sid-v"}}}),
            mock.patch.object(cowork, "_resume_wake_failure_kind",
                              lambda *_a, **_k: None),
            mock.patch.object(state_store, "read_pending_turn_before_pause",
                              lambda *_a, **_k: {"acknowledged": True,
                                                 "lease_id": "lease-1"}),
            mock.patch.object(owner, "read_provider_binding",
                              side_effect=boom),
        ]
        with _nested(managers):
            rc = cowork.run_resume_trigger(
                ["--session-uuid", session_uuid, "--lease-id", "lease-1",
                 "--claimant-ref", "test", "--automation-ref", "test",
                 "--cwd", self.project],
                output=lines.append)
        self.assertEqual(
            rc, cowork.RESUME_TRIGGER_EXIT_PROVIDER_BINDING_UNAVAILABLE)
        self.assertEqual(json.loads(lines[-1])["outcome"],
                         "provider_binding_unavailable")

    def test_the_failure_is_traced_with_its_real_cause(self):
        session_uuid, _seen, _rc = self._unavailable_run(
            "prov-sid-traced", TimeoutError("lock"))
        events = [e for e in self.trace_events(session_uuid)
                  if e.get("event") == "owner.provider_binding_unavailable"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("error_type"),
                         "ProviderBindingUnavailable")
        self.assertEqual(events[0].get("cause_type"), "TimeoutError")
        self.assertEqual(events[0].get("session_id"), "prov-sid-traced")

    def test_n10_nothing_anonymous_reaches_the_send_gateway(self):
        """N10. The binding surface is the one place a typed refusal could be
        flattened into an anonymous turn failure. An instrumented `_send`
        records nothing at all on either binding path -- conflict or
        unavailable -- because the refusal never crosses the callback
        boundary."""
        sends = _Counter()
        self.seed_foreign_live_binding("claude", "prov-sid-n10")

        def scout(config, context, selected, **kwargs):
            kwargs["on_session"]("claude", "prov-sid-n10")
            with mock.patch.object(state_store, "_locked_json_transaction",
                                   side_effect=TimeoutError("lock")):
                kwargs["on_session"]("claude", "prov-sid-n10b")
            return 0

        with mock.patch.object(cowork, "_send", sends):
            rc, session_uuid = self.run_fresh(scout)
        self.assertEqual(sends.count, 0)
        self.assertEqual(rc, 3)
        # First refusal wins: the conflict, not the later unavailable.
        self.assertEqual(self.run_end(session_uuid).get("reason"),
                         "provider_session_bound")


# --------------------------------------------------------------------------- #
# G13a-f / G14 (git-free half) -- the standing static gates.                    #
# --------------------------------------------------------------------------- #


class FrozenSeamTests(unittest.TestCase):
    """Structural gates over the live `cowork.py`, all git-free so they run
    identically in the hermetic snapshot and in the live tree."""

    @classmethod
    def setUpClass(cls):
        cls.tree = _cowork_tree()
        cls.top = _named_top_level(cls.tree)

    def _on_sess(self):
        nodes = _find_functions(self.tree, "on_sess")
        self.assertEqual(len(nodes), 1, "expected exactly one `on_sess`")
        return nodes[0]

    def test_g13b_the_handler_list_is_exactly_the_two_declared_types(self):
        """The whole obligation of enforcement point 3, as a structure: two
        handlers, in this order, the first returning (persist nothing) and the
        second NOT (fall through and persist)."""
        node = self._on_sess()
        tries = [n for n in ast.walk(node) if isinstance(n, ast.Try)]
        self.assertEqual(len(tries), 1)
        handlers = tries[0].handlers
        self.assertEqual([_handler_names(h) for h in handlers],
                         [["cowork_owner.ProviderSessionConflict"],
                          ["cowork_owner.ProviderBindingUnavailable"]])
        # A conflict persists NOTHING: its handler returns.
        self.assertTrue(any(isinstance(s, ast.Return)
                            for s in ast.walk(handlers[0])))
        # An unavailable index still persists: no early exit of any kind.
        for stmt in ast.walk(handlers[1]):
            self.assertNotIsInstance(stmt, ast.Return)
            self.assertNotIsInstance(stmt, ast.Continue)
            self.assertNotIsInstance(stmt, ast.Break)
        # The bind really does precede the persist -- compared by SOURCE
        # POSITION, never by `ast.walk` order, which is breadth-first and says
        # nothing about which statement runs first.
        def line_of(name):
            found = [c.lineno for c in ast.walk(node)
                     if isinstance(c, ast.Call) and _called_name(c) == name]
            self.assertEqual(len(found), 1, name)
            return found[0]

        self.assertLess(line_of("bind_provider_session"),
                        line_of("save_role_session"))

    def test_g13f_no_broad_handler_anywhere_on_the_binding_path(self):
        node = self._on_sess()
        for handler in [h for n in ast.walk(node) if isinstance(n, ast.Try)
                        for h in n.handlers]:
            self.assertFalse(_swallows(handler), ast.unparse(handler))
        spans = _broad_try_spans(self.tree)
        binding_calls = ("bind_provider_session", "read_provider_binding",
                         "_record_provider_conflict")
        offenders = []
        for call in ast.walk(self.tree):
            if not isinstance(call, ast.Call):
                continue
            if _called_name(call) not in binding_calls:
                continue
            for start, end in spans:
                if start <= call.lineno <= end:
                    offenders.append((_called_name(call), call.lineno))
        self.assertEqual(offenders, [])

    def test_the_binding_call_sites_are_exactly_the_three_seams(self):
        """A fourth call site added later fails here rather than going
        unreviewed."""
        counts = {}
        for call in ast.walk(self.tree):
            if isinstance(call, ast.Call):
                name = _called_name(call)
                if name in ("read_provider_binding", "bind_provider_session",
                            "classify_owner_lease"):
                    counts[name] = counts.get(name, 0) + 1
        self.assertEqual(counts, {"read_provider_binding": 2,
                                  "bind_provider_session": 1,
                                  "classify_owner_lease": 2})

    def test_g13d_the_reason_mapping_is_total_over_the_hierarchy(self):
        for klass in OWNER_SUBCLASSES:
            with self.subTest(klass.__name__):
                self.assertIn(klass, owner.OWNER_REFUSAL_REASONS)
        self.assertEqual(
            owner.OWNER_REFUSAL_REASONS[owner.ProviderSessionConflict],
            "provider_session_bound")
        self.assertEqual(
            owner.OWNER_REFUSAL_REASONS[owner.ProviderBindingUnavailable],
            "provider_binding_unavailable")

    def test_g14_save_role_session_keeps_its_exact_parameters(self):
        self.assertEqual(
            list(inspect.signature(
                state_store.save_role_session).parameters.keys()),
            ["path", "role", "controller", "session_id", "prior"])

    def test_record_provider_conflict_never_raises_and_never_overwrites(self):
        """The deferral helper must be inert: it cannot raise (it is called
        from inside a callback that fires during a live send), and it cannot
        displace an already-pending refusal (first refusal wins -- that is the
        one with proof)."""
        node = self.top["_record_provider_conflict"]
        self.assertEqual(
            [n for n in ast.walk(node) if isinstance(n, ast.Raise)], [])
        self.assertIsNotNone(ast.get_docstring(node))
        writes = [n for n in ast.walk(node) if isinstance(n, ast.Assign)
                  and "provider_conflict" in ast.unparse(n)]
        self.assertEqual(len(writes), 1)
        guards = [n for n in ast.walk(node) if isinstance(n, ast.If)
                  and "provider_conflict" in ast.unparse(n.test)
                  and "None" in ast.unparse(n.test)]
        self.assertEqual(len(guards), 1)
        self.assertIn(writes[0], list(ast.walk(guards[0])))

    def test_the_gate_limb_runs_after_matched_is_set(self):
        """Decision pd-d, asserted structurally as well as behaviourally: the
        binding lookup cannot precede `matched = True`."""
        node = self.top["_owner_gate_fact"]
        matched = [n for n in ast.walk(node) if isinstance(n, ast.Assign)
                   and ast.unparse(n).startswith("_OWNER_CONTEXT['matched']")
                   and ast.unparse(n).endswith("True")]
        self.assertTrue(matched)
        lookup = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                  and _called_name(c) == "read_provider_binding"]
        self.assertEqual(len(lookup), 1)
        self.assertLess(max(n.lineno for n in matched), lookup[0].lineno)

    def test_the_resume_trigger_check_precedes_the_acquire(self):
        """Decision pd-b, structurally: the exclusivity check sits before
        `acquire_owner_lease` and does NOT route through
        `_preflight_conflict`, which would charge a failed wake attempt."""
        node = self.top["run_resume_trigger"]
        read = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                and _called_name(c) == "read_provider_binding"]
        acquire = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                   and _called_name(c) == "acquire_owner_lease"]
        self.assertEqual(len(read), 1)
        self.assertEqual(len(acquire), 1)
        self.assertLess(read[0].lineno, acquire[0].lineno)
        # The statement that returns exit 11/12 must not call the charging
        # helper anywhere between the read and the acquire.
        between = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                   and _called_name(c) == "_account_failed_wake_attempt"
                   and read[0].lineno < c.lineno < acquire[0].lineno]
        self.assertEqual(between, [])


# --------------------------------------------------------------------------- #
# G9 -- the resume-trigger exit contract.                                       #
# --------------------------------------------------------------------------- #


class ResumeTriggerExitContractTests(unittest.TestCase):

    def test_the_exit_contract_is_exactly_the_declared_table(self):
        self.assertEqual(dict(cowork.RESUME_TRIGGER_EXIT_CODES),
                         RESUME_TRIGGER_EXIT_CONTRACT)

    def test_the_exclusivity_constants_match_the_contract(self):
        self.assertEqual(cowork.RESUME_TRIGGER_EXIT_PROVIDER_SESSION_BOUND, 11)
        self.assertEqual(
            cowork.RESUME_TRIGGER_EXIT_PROVIDER_BINDING_UNAVAILABLE, 12)

    def test_no_integer_is_used_twice(self):
        codes = cowork.RESUME_TRIGGER_EXIT_CODES
        self.assertEqual(len(set(codes.values())), len(codes))

    def test_the_emitted_outcome_names_are_keys_of_the_contract(self):
        """Every `outcome` string the two exclusivity paths write is exactly
        the contract key that maps to the code they return, so an external
        consumer can look the integer up by name."""
        source = _cowork_source()
        for name in EXCLUSIVITY_EXIT_CODES:
            with self.subTest(name):
                self.assertIn('"outcome": "%s"' % name, source)
                self.assertEqual(cowork.RESUME_TRIGGER_EXIT_CODES[name],
                                 RESUME_TRIGGER_EXIT_CONTRACT[name])


if __name__ == "__main__":
    unittest.main()
