#!/usr/bin/env python3
"""The ownership gate, the typed refusal, and the crash-safe owner lifecycle
wired into `cowork.py`/`cowork_dispatch.py`. The owner store itself is proven
in `test_owner_store.py`; what has to be proven here is ORDERING and
REACHABILITY, not storage:

  - **G1 (refusal precedes paid dispatch).** The reducer evaluates the
    ownership fact FIRST, ahead of the policy guard, so an unowned process is
    refused before any other fact can allow. A real `run_flow` against a live
    foreign owner returns rc 3 with `session_factory`, `bridge._real_claude_
    spawn`, the three controller session classes, the evaluator session and
    all four role runners replaced by doubles that RAISE on call -- zero
    controller constructions and zero dispatched agent work. The assertion is
    on those dispatch seams, NOT on process creation in general: `run_flow`'s
    own preconditions legitimately run children (the owner gate's `ps`
    liveness probe, the git work-tree prerequisite's `git rev-parse`), and
    neither is a dispatch. At the dispatch
    seam an `owner_lease`-sourced refusal RAISES, after both trace events are
    emitted, so no call site can fall through to its local refusal branch and
    write `preflight_rejected`; asserted with `resume_session_id` SET, which
    is what defeats the three deliberately non-short-circuiting
    `refuse and not resume_id` conditions. And a standing AST anti-swallow
    sweep proves no fenced seam sits under a broad handler.

  - **G2 (refusal precedes shared writes).** sha256 of every governed durable
    artifact under the session assets home and of the project-local anchor,
    captured before a refused run and re-captured after: identical, on an
    explicit allowlist (`owner/history.jsonl` and `trace.jsonl` only), never a
    prefix match.

  - **G3 (crash-safe lifecycle).** An AST gate over `run_flow`: the
    acquisition is followed by a `try` whose `finally` stops and joins the
    heartbeat, releases the lease, releases the provider bindings and restores
    the owner context IN THAT ORDER, with no `return` between; the SIGTERM
    handler's terminal mark is APPENDED after the three pre-existing effects
    and before the `SystemExit`, reads `_current_owner_context()` and nothing
    else, takes no lock and appends to no JSONL; `_owner_handle_box` occurs
    nowhere; and `main`'s only owner handling is the DECLARED-BASE backstop. Complemented by a
    runtime exactly-once release assertion over every exit path.

  - **G4 (`--take-over`).** Mode selection comes from the same in-lock verdict
    the acquire gate uses: a provably dead owner is taken over at `epoch + 1`,
    and an owner whose death is UNPROVABLE refuses -- `--take-over` never
    falls back from one mode to the other.

  - **G9 / G10 / G11 / G13d / G14 and the negative controls.** The additive
    exit code 10; the heartbeat's placement and its harmless straggler; the
    evaluator exemption and its compensating fence; the declared exception
    hierarchy and the TOTAL typed-reason mapping; the `cowork_eval.drain`
    never-raises declaration; and N1/N3/N7/N8 -- `--no-session` never leases and never raises, an owned
    run is enforced at every governed seam, a nested run restores the outer
    context exactly, and the evaluator exemption does not leak.

Every fixture redirects `COWORK_SESSIONS_ROOT` into a fresh `tempfile.mkdtemp()`
(so nothing here touches the real home dir and nothing is written inside the
worktree), drives the REAL production functions rather than fakes of them, and
spawns no provider and no network client.
"""

import ast
import datetime
import hashlib
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
import cowork_dispatch as dispatch  # noqa: E402
import cowork_owner as owner  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

OWNER_SUBCLASSES = (
    owner.OwnerLeaseConflict, owner.OwnerLeaseCorrupt, owner.OwnerLeaseLost,
    owner.ProviderSessionConflict, owner.ProviderBindingUnavailable,
)

_ALLOW = {"allowed": True, "refusal_code": None, "refusal_message": None,
          "source": None}


# --------------------------------------------------------------------------- #
# Shared helpers.                                                              #
# --------------------------------------------------------------------------- #


def _read_local_bytes(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), "rb") as fh:
        return fh.read()


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _cowork_source():
    with open(os.path.join(_HERE, "cowork.py"), "r") as fh:
        return fh.read()


def _cowork_tree():
    return ast.parse(_cowork_source(), filename="cowork.py")


def _top_level(tree):
    return {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))}


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


def _called_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _calls_in(node):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)]


def _call_names(node):
    return [n for n in (_called_name(c) for c in _calls_in(node))
            if n is not None]


def _handler_names(handler):
    """Every dotted/bare exception name a single `except` clause catches."""
    if handler.type is None:
        return [None]
    parts = (handler.type.elts if isinstance(handler.type, ast.Tuple)
             else [handler.type])
    return [ast.unparse(p) for p in parts]


def _dead_pid():
    """A pid that has genuinely exited -- a real crashed owner's pid, obtained
    by letting a real child run and reaping it, never a guessed number."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _contract(purpose="launch", resume_session_id=None):
    return cowork._make_dispatch_contract(
        "scout", "claude", purpose, "test-site",
        resume_session_id=resume_session_id)


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
    the assertion is the ABSENCE of a paid dispatch."""

    def __init__(self, label):
        self.label = label

    def __call__(self, *args, **kwargs):
        raise AssertionError("%s was invoked on a refused run" % self.label)


# --------------------------------------------------------------------------- #
# Base case: a sandboxed session anchor plus a sandboxed assets home.           #
# --------------------------------------------------------------------------- #


class OwnerGateTestCase(unittest.TestCase):
    """Every test gets its own `COWORK_SESSIONS_ROOT` and its own project
    directory, so no fixture can touch the real home dir, write inside the
    worktree, or observe another test's lease."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cowork-owner-p2-root-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = tempfile.mkdtemp(prefix="cowork-owner-p2-proj-")
        self.addCleanup(shutil.rmtree, self.project, True)
        patcher = mock.patch.dict(os.environ,
                                  {"COWORK_SESSIONS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spath = os.path.join(self.project, ".cowork", "session.json")
        # The module owner context is process-global by design; restore it
        # verbatim after every test so one fixture can never leak enforcement
        # into the next.
        prior = dict(cowork._OWNER_CONTEXT)
        self.addCleanup(cowork._restore_owner_context, prior)

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
        test can prove the fake lead actually ran."""
        self.scout_calls = getattr(self, "scout_calls", 0) + 1
        on_outcome("approved", None)
        return 0

    def assert_scout_ran(self, before):
        self.assertGreater(getattr(self, "scout_calls", 0), before,
                           "the fake scout lead never ran")

    def establish_session(self, scout=None):
        """Run one complete owned flow, then return its `session_uuid`. The
        lease it leaves behind is `released`, so a later fixture starts from a
        genuine post-clean-exit state rather than a fabricated one."""
        before = getattr(self, "scout_calls", 0)
        rc, _ = self.run_flow(["--new"], scout=scout)
        self.assertEqual(rc, 0)
        if scout is None:
            self.assert_scout_ran(before)
        state = state_store.load(self.spath)
        return state_store.get_session_uuid(state)

    # -- lease seeding ------------------------------------------------------ #

    def claimant(self, session_uuid, pid=None, pid_start_at=None):
        record = owner.owner_identity(session_uuid, "run_flow", self.project,
                                      self.spath)
        if pid is not None:
            record["pid"] = pid
            record["pid_start_at"] = pid_start_at
            record["pid_start_source"] = ("ps_lstart" if pid_start_at
                                          else "unavailable")
        return record

    def seed_live_owner(self, session_uuid):
        """A genuinely LIVE lease: this process's own pid and start time, a
        fresh heartbeat, never released. `classify_owner_lease` returns
        `live_owner` for it, which is what a second process must be refused
        against."""
        return owner.acquire_owner_lease(session_uuid,
                                         self.claimant(session_uuid))

    def seed_dead_owner(self, session_uuid, age_seconds=7200):
        """A crashed owner: a pid that has genuinely exited, and a heartbeat
        old enough that the real clock is past the deadline. `ps` reports the
        pid absent, so death is PROVED and the verdict is
        `stale_dead_owner`."""
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=age_seconds))
        return owner.acquire_owner_lease(
            session_uuid,
            self.claimant(session_uuid, pid=_dead_pid(),
                          pid_start_at="2020-01-01T00:00:00Z"),
            now=stale)

    def seed_unprovable_owner(self, session_uuid, age_seconds=7200):
        """A lease past its deadline whose owner's death cannot be PROVED --
        no readable start time. The fail-safe direction says refuse, and
        `--take-over` must refuse too, because neither mode is safe."""
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=age_seconds))
        return owner.acquire_owner_lease(
            session_uuid,
            self.claimant(session_uuid, pid=_dead_pid(), pid_start_at=None),
            now=stale)

    # -- durable-artifact digests ------------------------------------------ #

    def digest_tree(self):
        """sha256 of every file under the sandboxed assets home and of the
        project-local anchor directory -- an exact per-path map, so G2 can
        assert an ALLOWLIST of what may change rather than a prefix match."""
        out = {}
        for base in (self.root, os.path.join(self.project, ".cowork")):
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    full = os.path.join(dirpath, name)
                    with open(full, "rb") as fh:
                        out[os.path.relpath(full, base)] = _sha256(fh.read())
        return out


# --------------------------------------------------------------------------- #
# G1a -- the reducer evaluates the ownership fact FIRST.                        #
# --------------------------------------------------------------------------- #


class ReducerOrderingTests(unittest.TestCase):
    """G1a. `dispatch.decide`'s ordering is the whole point of the ownership
    fact: a process that does not own the session must be refused before any
    other fact can allow a paid dispatch."""

    REFUSE_OWNER = {"allowed": False, "refusal_code": "session_not_owned",
                    "refusal_message": "lease lost", "source": "owner_lease"}
    REFUSE_POLICY = {"allowed": False,
                     "refusal_code": "controller_not_allowed",
                     "refusal_message": "blocked", "source": "policy_guard"}

    def test_refusing_owner_fact_wins_over_allowing_facts(self):
        decision = dispatch.decide(
            _contract(), owner_result=self.REFUSE_OWNER,
            policy_result=dict(_ALLOW), preflight_result=dict(_ALLOW),
            probe_result=dict(_ALLOW))
        self.assertEqual(decision["outcome"], "refuse")
        self.assertEqual(decision["refusal_code"], "session_not_owned")
        self.assertEqual(decision["source"], "owner_lease")
        self.assertIs(decision["spawned"], False)

    def test_owner_fact_is_evaluated_ahead_of_policy(self):
        """Both facts refuse; the ownership one must be the one reported."""
        decision = dispatch.decide(
            _contract(), owner_result=self.REFUSE_OWNER,
            policy_result=self.REFUSE_POLICY)
        self.assertEqual(decision["refusal_code"], "session_not_owned")
        self.assertEqual(decision["source"], "owner_lease")

    def test_policy_still_wins_when_the_owner_fact_allows(self):
        decision = dispatch.decide(
            _contract(), owner_result=dict(_ALLOW),
            policy_result=self.REFUSE_POLICY)
        self.assertEqual(decision["refusal_code"], "controller_not_allowed")
        self.assertEqual(decision["source"], "policy_guard")

    def test_owner_refusal_codes_and_source_are_registered(self):
        self.assertLessEqual({"session_not_owned", "provider_session_bound"},
                             set(dispatch._REFUSAL_CODES))
        self.assertIn("owner_lease", dispatch._REFUSAL_SOURCES)

    def test_a_none_owner_fact_changes_nothing(self):
        """G1e's no-op parity, at the reducer: the pre-#64 call shape and the
        explicit `owner_result=None` shape produce the same outcome."""
        for kwargs in ({}, {"owner_result": None}):
            decision = dispatch.decide(_contract(), **kwargs)
            self.assertEqual(decision["outcome"], "allow")
            self.assertIsNone(decision["source"])

    def test_provider_session_bound_is_a_valid_refusal_code(self):
        fact = {"allowed": False, "refusal_code": "provider_session_bound",
                "refusal_message": "bound elsewhere", "source": "owner_lease"}
        decision = dispatch.decide(_contract(), owner_result=fact)
        self.assertEqual(decision["refusal_code"], "provider_session_bound")


# --------------------------------------------------------------------------- #
# G1d / G11b -- the dispatch seam raises rather than returning.                 #
# --------------------------------------------------------------------------- #


class DispatchSeamTests(OwnerGateTestCase):

    class _Trace(object):
        def __init__(self):
            self.events = []

        def event(self, name, **fields):
            self.events.append((name, fields))

    def _lose_the_lease(self):
        """Publish a context naming a lease this process does NOT hold, which
        is exactly the durable state a taken-over owner is left in."""
        session_uuid = str(uuid.uuid4())
        cowork._set_owner_context(session_uuid, str(uuid.uuid4()), 1)
        return session_uuid

    def test_owner_gate_fact_refuses_when_the_lease_is_foreign(self):
        self._lose_the_lease()
        fact = cowork._owner_gate_fact("claude", None, "launch")
        self.assertIsNotNone(fact)
        self.assertIs(fact["allowed"], False)
        self.assertEqual(fact["refusal_code"], "session_not_owned")
        self.assertEqual(fact["source"], "owner_lease")
        self.assertIs(cowork._OWNER_CONTEXT["matched"], False)

    def test_decide_and_trace_raises_after_emitting_both_events(self):
        """G1d. The refusal is FULLY EVIDENCED before the raise, and the raise
        -- not a returned decision -- is what stops each call site from
        writing `preflight_rejected` next. Asserted with `resume_session_id`
        SET, the condition the three `refuse and not resume_id` branches make
        non-short-circuiting."""
        self._lose_the_lease()
        trace = self._Trace()
        with self.assertRaises(owner.OwnerLeaseLost):
            cowork._decide_and_trace(trace, "scout", "claude", "launch",
                                     "cowork.py:test",
                                     resume_session_id="resume-abc")
        names = [n for n, _ in trace.events]
        self.assertEqual(names, ["dispatch.contract", "dispatch.decision"])
        decision = trace.events[1][1]
        self.assertEqual(decision["outcome"], "refuse")
        self.assertEqual(decision["refusal_code"], "session_not_owned")
        self.assertEqual(decision["source"], "owner_lease")

    def test_the_evaluator_purpose_is_structurally_exempt(self):
        """G11b / rule E2. The one site whose refusal would be swallowed twice
        over never produces one -- and the exemption is keyed on `purpose`
        alone, so it cannot widen by accident."""
        self._lose_the_lease()
        self.assertIsNone(cowork._owner_gate_fact(purpose="evaluator"))
        trace = self._Trace()
        decision = cowork._decide_and_trace(trace, "scout", "claude",
                                            "evaluator", "cowork.py:test")
        self.assertEqual(decision["outcome"], "allow")

    def test_an_unenforced_context_contributes_no_fact_at_all(self):
        """G1e / N1. With no lease held, `decide()` must receive
        `owner_result is None` -- not an allowing fact, which would still be a
        behaviour change."""
        seen = {}
        real = dispatch.decide

        def spy(contract, **kwargs):
            seen.update(kwargs)
            return real(contract, **kwargs)

        self.assertIs(cowork._OWNER_CONTEXT["enforced"], False)
        with mock.patch.object(dispatch, "decide", spy):
            cowork._decide_and_trace(None, "scout", "claude", "launch",
                                     "cowork.py:test")
        self.assertIn("owner_result", seen)
        self.assertIsNone(seen["owner_result"])

    def test_a_valid_lease_produces_an_allowing_fact(self):
        session_uuid = str(uuid.uuid4())
        lease = self.seed_live_owner(session_uuid)
        cowork._set_owner_context(session_uuid, lease["owner_id"],
                                  lease["epoch"])
        fact = cowork._owner_gate_fact("claude", None, "launch")
        self.assertEqual(fact, dict(_ALLOW))
        self.assertIs(cowork._OWNER_CONTEXT["matched"], True)


# --------------------------------------------------------------------------- #
# G1b / G2 -- a refused run: zero paid dispatch, zero shared mutation.          #
# --------------------------------------------------------------------------- #


class RefusalOrderingTests(OwnerGateTestCase):

    def test_a_second_live_process_is_refused_with_rc_3(self):
        session_uuid = self.establish_session()
        self.seed_live_owner(session_uuid)
        rc, out = self.run_flow(scout=_Raises("run_scout"))
        self.assertEqual(rc, 3)
        self.assertIn("already owned by another live cowork process", out)
        self.assertIn("reason   live_owner", out)

    def test_a_refused_live_owner_run_constructs_and_dispatches_no_agent_work(
            self):
        """G1b. A run refused against a live owner must construct and dispatch
        NO controller and NO agent work. That -- not "no child process" -- is
        the product boundary, and the doubles below are what measure it: every
        one RAISES on call, so reaching any of them fails the test by name.

        The seams are enumerated rather than inferred, and they are the
        complete set a paid dispatch has to cross:

          * provider dispatch  -- `bridge._real_claude_spawn`
          * controllers        -- `bridge.ClaudeSession`, `CodexSession`,
                                  `OpencodeSession`
          * evaluator          -- `cowork._isolated_evaluator_session`
          * roles              -- the scout, planner, builder and worktree
                                  runners `run_flow` dispatches through

        This test deliberately does NOT assert on process creation in general.
        An earlier revision patched `subprocess.Popen` and failed on any argv0
        but `ps`, which coupled the ownership invariant to an executable
        allowlist it never meant to own: the ownership gate's own liveness
        probe (`cowork_owner._probe_pid_start`) is a real child by design, and
        so are `run_flow`'s environment preconditions -- the git work tree
        prerequisite asks `git rev-parse` before the lease is acquired, because
        a launch outside a work tree has no write boundary to confine a role
        to. Neither is a dispatch. Refusing them proved nothing about ownership
        and made an unrelated precondition look like a violation of it, so the
        allowlist is gone and the dispatch seams themselves are the assertion.
        """
        session_uuid = self.establish_session()
        self.seed_live_owner(session_uuid)
        with mock.patch.object(bridge, "_real_claude_spawn",
                               _Raises("_real_claude_spawn")), \
                mock.patch.object(bridge, "ClaudeSession",
                                  _Raises("ClaudeSession")), \
                mock.patch.object(bridge, "CodexSession",
                                  _Raises("CodexSession")), \
                mock.patch.object(bridge, "OpencodeSession",
                                  _Raises("OpencodeSession")), \
                mock.patch.object(cowork, "_isolated_evaluator_session",
                                  _Raises("_isolated_evaluator_session")):
            rc, _out = self.run_flow(
                scout=_Raises("run_scout"),
                run_planner_fn=_Raises("run_planner"),
                run_builder_fn=_Raises("run_builder"),
                run_worktree_fn=_Raises("run_worktree"))
        self.assertEqual(rc, 3)

    def test_the_refusal_reaches_no_preflight_and_no_phase_entry(self):
        """The ordering invariant, asserted positively: acquisition sits ABOVE
        `preflight.preflight` and above the `session.start` measurement
        checkpoint, so a refused run reaches neither."""
        session_uuid = self.establish_session()
        self.seed_live_owner(session_uuid)
        with mock.patch.object(cowork.preflight, "preflight",
                               _Raises("preflight.preflight")), \
                mock.patch.object(cowork, "run_evaluation_transition",
                                  _Raises("run_evaluation_transition")):
            rc, _out = self.run_flow(scout=_Raises("run_scout"))
        self.assertEqual(rc, 3)

    def test_the_refusal_mutates_no_governed_durable_artifact(self):
        """G2. An exact allowlist, never a prefix match: only the append-only
        owner audit and the trace's own refusal event may differ."""
        session_uuid = self.establish_session()
        self.seed_live_owner(session_uuid)
        before = self.digest_tree()
        rc, _out = self.run_flow(scout=_Raises("run_scout"))
        self.assertEqual(rc, 3)
        after = self.digest_tree()
        allowed = {
            os.path.join(session_uuid, "owner", "history.jsonl"),
            os.path.join(session_uuid, "trace.jsonl"),
        }
        changed = {p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)}
        self.assertEqual(changed - allowed, set())

    def test_the_refusal_is_traced_as_a_typed_run_end(self):
        session_uuid = self.establish_session()
        self.seed_live_owner(session_uuid)
        rc, _out = self.run_flow(scout=_Raises("run_scout"))
        self.assertEqual(rc, 3)
        with open(trace_store.trace_path_for(session_uuid), "r") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        ends = [e for e in events if e.get("event") == "run.end"]
        self.assertEqual(ends[-1].get("reason"), "session_owner_conflict")
        self.assertEqual(ends[-1].get("rc"), 3)

    def test_a_crashed_owner_is_never_implicitly_reclaimed(self):
        """Recovery is explicit or not at all: a provably dead owner still
        refuses a plain run, and says so with the machine-readable verdict."""
        session_uuid = self.establish_session()
        self.seed_dead_owner(session_uuid)
        rc, out = self.run_flow(scout=_Raises("run_scout"))
        self.assertEqual(rc, 3)
        self.assertIn("reason   stale_dead_owner", out)


# --------------------------------------------------------------------------- #
# G4 -- `--take-over` mode selection, wired.                                    #
# --------------------------------------------------------------------------- #


class TakeoverWiringTests(OwnerGateTestCase):

    def test_take_over_reclaims_a_provably_dead_owner_at_the_next_epoch(self):
        session_uuid = self.establish_session()
        prior = self.seed_dead_owner(session_uuid)
        before = self.scout_calls
        rc, _out = self.run_flow(["--take-over"])
        self.assertEqual(rc, 0)
        self.assert_scout_ran(before)
        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["epoch"], prior["epoch"] + 1)
        self.assertEqual(record["state"], "released")
        self.assertEqual(record["predecessor"]["owner_id"], prior["owner_id"])

    def test_take_over_refuses_when_death_is_not_provable(self):
        """F10. `stale_unproven` selects NO mode, and `--take-over` never
        falls back from one mode to the other -- so the run is refused and
        the lease is left exactly as it was."""
        session_uuid = self.establish_session()
        prior = self.seed_unprovable_owner(session_uuid)
        before = _sha256(json.dumps(
            owner.read_owner_lease(session_uuid), sort_keys=True).encode())
        rc, out = self.run_flow(["--take-over"], scout=_Raises("run_scout"))
        self.assertEqual(rc, 3)
        self.assertIn("reason   stale_unproven", out)
        after = owner.read_owner_lease(session_uuid)
        self.assertEqual(after["owner_id"], prior["owner_id"])
        self.assertEqual(
            _sha256(json.dumps(after, sort_keys=True).encode()), before)

    def test_take_over_on_an_unowned_session_is_an_ordinary_acquire(self):
        session_uuid = self.establish_session()
        before = self.scout_calls
        rc, _out = self.run_flow(["--take-over"])
        self.assertEqual(rc, 0)
        self.assert_scout_ran(before)
        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["epoch"], 2)
        self.assertIsNone(record["predecessor"])

    def test_the_flag_exists_and_defaults_off(self):
        self.assertIs(self.args().take_over, False)
        self.assertIs(self.args(["--take-over"]).take_over, True)


# --------------------------------------------------------------------------- #
# Lifecycle: acquire, renew, release exactly once, restore.                     #
# --------------------------------------------------------------------------- #


class LifecycleTests(OwnerGateTestCase):

    def test_a_clean_run_acquires_at_epoch_one_and_releases_once(self):
        session_uuid = self.establish_session()
        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["epoch"], 1)
        self.assertEqual(record["state"], "released")
        self.assertEqual(record["terminal_reason"], "normal_exit")
        history = self._history(session_uuid)
        self.assertEqual([e["event"] for e in history],
                         ["acquired", "released"])

    def test_a_restart_after_a_clean_exit_acquires_the_next_epoch(self):
        """F2: no takeover path is taken, because a released lease is
        `unowned`."""
        session_uuid = self.establish_session()
        before = self.scout_calls
        rc, _out = self.run_flow()
        self.assertEqual(rc, 0)
        self.assert_scout_ran(before)
        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["epoch"], 2)
        self.assertIsNone(record["predecessor"])

    def test_every_exit_path_releases_exactly_once(self):
        """The `finally` covers the ordinary return, the refusal, a
        KeyboardInterrupt, an EOFError and an arbitrary crash -- and each
        records its OWN reason, so the audit says what happened."""
        cases = [
            (KeyboardInterrupt, "interrupted"),
            (EOFError, "input_closed"),
            (RuntimeError, "crash"),
        ]
        for exc_type, expected in cases:
            with self.subTest(exc=exc_type.__name__):
                project = tempfile.mkdtemp(prefix="cowork-owner-p2-exit-")
                self.addCleanup(shutil.rmtree, project, True)
                self.spath = os.path.join(project, ".cowork", "session.json")

                def boom(*_a, **_k):
                    raise exc_type("stop here")

                with self.assertRaises(exc_type):
                    self.run_flow(["--new"], scout=boom)
                state = state_store.load(self.spath)
                session_uuid = state_store.get_session_uuid(state)
                record = owner.read_owner_lease(session_uuid)
                self.assertEqual(record["state"], "released")
                self.assertEqual(record["terminal_reason"], expected)
                self.assertEqual(
                    [e["event"] for e in self._history(session_uuid)],
                    ["acquired", "released"])

    def test_the_owner_context_is_restored_on_every_exit_path(self):
        before = dict(cowork._OWNER_CONTEXT)
        self.establish_session()
        self.assertEqual(dict(cowork._OWNER_CONTEXT), before)

        session_uuid = state_store.get_session_uuid(
            state_store.load(self.spath))
        self.seed_live_owner(session_uuid)
        self.run_flow(scout=_Raises("run_scout"))
        self.assertEqual(dict(cowork._OWNER_CONTEXT), before)

    def test_a_nested_run_restores_the_outer_context_verbatim(self):
        """N7. The prior context is SAVED and restored, never reset to a
        constant, so an inner run on a second session leaves the outer one
        exactly as it found it -- including `provider_conflict`."""
        outer_uuid = str(uuid.uuid4())
        lease = self.seed_live_owner(outer_uuid)
        cowork._set_owner_context(outer_uuid, lease["owner_id"],
                                  lease["epoch"])
        pending = owner.ProviderSessionConflict("claude", "sid", "other",
                                                "scout")
        cowork._OWNER_CONTEXT["provider_conflict"] = pending
        expected = dict(cowork._OWNER_CONTEXT)

        inner_project = tempfile.mkdtemp(prefix="cowork-owner-p2-inner-")
        self.addCleanup(shutil.rmtree, inner_project, True)
        self.spath = os.path.join(inner_project, ".cowork", "session.json")
        rc, _out = self.run_flow(["--new"])
        self.assertEqual(rc, 0)
        self.assert_scout_ran(0)
        self.assertEqual(cowork._current_owner_context(), expected)
        self.assertIs(cowork._OWNER_CONTEXT["provider_conflict"], pending)

    def test_the_heartbeat_renews_and_a_straggler_cannot_resurrect(self):
        """G10. The loop fires on schedule with no send in flight, and a renew
        landing after the release is a compare-and-swap that writes nothing --
        which is what makes the bounded join safe."""
        session_uuid = str(uuid.uuid4())
        lease = self.seed_live_owner(session_uuid)
        fired = threading.Event()
        stop = threading.Event()

        def fire():
            owner.renew_owner_lease(session_uuid, lease["owner_id"],
                                    lease["epoch"])
            fired.set()

        thread = threading.Thread(
            target=cowork._run_owner_heartbeat_loop,
            args=(stop, fire, 0.01), daemon=True)
        thread.start()
        self.assertTrue(fired.wait(5))
        first = owner.read_owner_lease(session_uuid)["heartbeat_at"]
        fired.clear()
        self.assertTrue(fired.wait(5))
        stop.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(owner.read_owner_lease(session_uuid)["heartbeat_at"],
                            first)

        owner.release_owner_lease(session_uuid, lease["owner_id"],
                                  lease["epoch"], "normal_exit")
        released = owner.read_owner_lease(session_uuid)
        self.assertIsNone(owner.renew_owner_lease(
            session_uuid, lease["owner_id"], lease["epoch"]))
        self.assertEqual(owner.read_owner_lease(session_uuid), released)

    def test_a_heartbeat_failure_never_escapes_the_loop(self):
        stop = threading.Event()
        seen = []

        def fire():
            seen.append(1)
            raise OSError("disk went away")

        thread = threading.Thread(
            target=cowork._run_owner_heartbeat_loop,
            args=(stop, fire, 0.01), daemon=True)
        thread.start()
        for _ in range(500):
            if len(seen) >= 2:
                break
            threading.Event().wait(0.01)
        stop.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(seen), 2)

    def _history(self, session_uuid):
        path = state_store.owner_history_path_for(session_uuid)
        if not os.path.exists(path):
            return []
        with open(path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- #
# N1 / N3 / N8 -- negative controls.                                            #
# --------------------------------------------------------------------------- #


class NegativeControlTests(OwnerGateTestCase):

    def test_no_session_never_leases_and_never_raises(self):
        """N1. `--no-session` mints a real ephemeral uuid and passes it
        onward exactly like a persisted one, but acquires nothing -- so
        `enforced` is never True and no owner exception of ANY subclass is
        raised."""
        seen = []
        real = cowork._require_owner

        def spy(session_uuid=None, advisory=False):
            seen.append(dict(cowork._OWNER_CONTEXT))
            return real(session_uuid, advisory)

        with mock.patch.object(cowork, "_require_owner", spy), \
                mock.patch.object(owner, "acquire_owner_lease",
                                  _Raises("acquire_owner_lease")), \
                mock.patch.object(owner, "take_over", _Raises("take_over")):
            rc, _out = self.run_flow(["--no-session"])
        self.assertEqual(rc, 0)
        self.assert_scout_ran(0)
        self.assertTrue(seen, "no governed seam was exercised at all")
        self.assertTrue(all(ctx["enforced"] is False for ctx in seen))
        # The ephemeral assets home is pre-existing `--no-session` behaviour;
        # what must not exist is an owner directory of any kind.
        for entry in os.listdir(self.root):
            self.assertFalse(
                os.path.exists(os.path.join(self.root, entry, "owner")))
        self.assertFalse(os.path.exists(os.path.join(self.root,
                                                     "provider-bindings")))

    def test_an_owned_run_is_enforced_at_every_governed_seam(self):
        """N3. The converse of N1: `enforced` is True at every
        `_advance_phase` entry and at every `evaluation_transition` entry, so
        N1's no-op rule cannot silently disable enforcement in a real run."""
        advance_ctx = []
        real_advance = cowork._advance_phase

        def advance_spy(*args, **kwargs):
            advance_ctx.append(dict(cowork._OWNER_CONTEXT))
            return real_advance(*args, **kwargs)

        eval_ctx = []
        real_eval = cowork.run_evaluation_transition

        def eval_spy(*args, **kwargs):
            eval_ctx.append(dict(cowork._OWNER_CONTEXT))
            return real_eval(*args, **kwargs)

        with mock.patch.object(cowork, "_advance_phase", advance_spy), \
                mock.patch.object(cowork, "run_evaluation_transition",
                                  eval_spy):
            rc, _out = self.run_flow(["--new"])
        self.assertEqual(rc, 0)
        self.assert_scout_ran(0)
        self.assertTrue(eval_ctx, "no evaluation boundary was reached")
        for ctx in advance_ctx + eval_ctx:
            self.assertIs(ctx["enforced"], True)

    def test_the_evaluator_exemption_does_not_leak(self):
        """N8. Exempt on `purpose="evaluator"`, fenced on everything else,
        with the SAME foreign lease in place."""
        session_uuid = str(uuid.uuid4())
        cowork._set_owner_context(session_uuid, str(uuid.uuid4()), 1)
        self.assertIsNone(cowork._owner_gate_fact(purpose="evaluator"))
        fact = cowork._owner_gate_fact(purpose="launch")
        self.assertIsNotNone(fact)
        self.assertIs(fact["allowed"], False)

    def test_require_owner_ignores_a_different_session(self):
        session_uuid = str(uuid.uuid4())
        cowork._set_owner_context(session_uuid, str(uuid.uuid4()), 1)
        self.assertIsNone(cowork._require_owner(str(uuid.uuid4())))
        with self.assertRaises(owner.OwnerLeaseLost):
            cowork._require_owner(session_uuid)

    def test_the_advisory_guard_never_raises_and_never_drains(self):
        """Plan section 3.9 rule 1. The signal path computes the verdict,
        records it for the sidecar, and leaves a pending conflict UNDRAINED so
        a later governed seam still reports it."""
        session_uuid = str(uuid.uuid4())
        cowork._set_owner_context(session_uuid, str(uuid.uuid4()), 1)
        self.assertIsNone(cowork._require_owner(session_uuid, advisory=True))
        self.assertIs(cowork._OWNER_CONTEXT["matched"], False)

        pending = owner.ProviderSessionConflict("claude", "sid", "other",
                                                "scout")
        cowork._OWNER_CONTEXT["provider_conflict"] = pending
        self.assertIsNone(cowork._require_owner(session_uuid, advisory=True))
        self.assertIs(cowork._OWNER_CONTEXT["provider_conflict"], pending)

    def test_a_recorded_conflict_is_re_raised_and_drained_exactly_once(self):
        """The C2 drain limb, unit-tested here by seeding the key directly --
        its producer is exercised elsewhere."""
        session_uuid = str(uuid.uuid4())
        lease = self.seed_live_owner(session_uuid)
        cowork._set_owner_context(session_uuid, lease["owner_id"],
                                  lease["epoch"])
        pending = owner.ProviderSessionConflict("claude", "sid", "other",
                                                "scout")
        cowork._OWNER_CONTEXT["provider_conflict"] = pending
        with self.assertRaises(owner.ProviderSessionConflict) as caught:
            cowork._require_owner(session_uuid)
        self.assertIs(caught.exception, pending)
        self.assertIsNone(cowork._OWNER_CONTEXT["provider_conflict"])
        self.assertIsNone(cowork._require_owner(session_uuid))


# --------------------------------------------------------------------------- #
# G11a / G13d -- the declared hierarchy and the TOTAL typed-reason mapping.     #
# --------------------------------------------------------------------------- #


class ExceptionContractTests(OwnerGateTestCase):

    def test_the_declared_bases(self):
        """G11a. Asserted POSITIVELY on `__bases__`, not by `issubclass`:
        every `Exception` is trivially a `BaseException` subclass, and the
        claim being pinned is the DECLARED base."""
        self.assertEqual(owner.OwnerLeaseError.__bases__, (Exception,))
        for klass in OWNER_SUBCLASSES:
            self.assertIn(owner.OwnerLeaseError, klass.__bases__,
                          klass.__name__)

    def test_the_subclass_set_is_closed(self):
        tree = ast.parse(_read_local_bytes("scripts/cowork_owner.py"))
        declared = {n.name for n in tree.body if isinstance(n, ast.ClassDef)
                    and any(ast.unparse(b) == "OwnerLeaseError"
                            for b in n.bases)}
        self.assertEqual(declared,
                         {k.__name__ for k in OWNER_SUBCLASSES})

    def test_a_plain_except_exception_catches_them(self):
        """The property that makes rules E2/E3 and contracts C1/C5 NECESSARY
        rather than merely tidy."""
        for klass in OWNER_SUBCLASSES:
            with self.subTest(klass.__name__):
                caught = False
                try:
                    raise self._instance(klass)
                except Exception:  # noqa: BLE001 - that is the assertion
                    caught = True
                self.assertTrue(caught)

    def test_the_reason_mapping_is_total_over_the_hierarchy(self):
        """G13d. The mapping's key set equals the closed subclass set exactly,
        so neither can drift from the other and no subclass can reach an
        operator with a generic or missing reason."""
        self.assertEqual(set(owner.OWNER_REFUSAL_REASONS),
                         set(OWNER_SUBCLASSES))
        self.assertEqual(
            sorted(owner.OWNER_REFUSAL_REASONS.values()),
            sorted(["session_owner_lost", "session_owner_conflict",
                    "session_owner_corrupt", "provider_session_bound",
                    "provider_binding_unavailable"]))
        for klass, reason in owner.OWNER_REFUSAL_REASONS.items():
            self.assertEqual(owner.owner_refusal_reason(self._instance(klass)),
                             reason)

    def test_every_subclass_renders_an_operator_message(self):
        for klass in OWNER_SUBCLASSES:
            with self.subTest(klass.__name__):
                text = owner.refusal_message(self._instance(klass),
                                             session_uuid="sess-1")
                self.assertTrue(text.startswith("cowork: "))
                self.assertIn(
                    owner.owner_refusal_reason(self._instance(klass))
                    if klass in (owner.ProviderSessionConflict,
                                 owner.ProviderBindingUnavailable)
                    else "reason", text)

    def test_each_subclass_raised_in_the_region_yields_rc_3_and_its_reason(
            self):
        """G13d's behavioural half, driven through the REAL `run_flow`: each
        of the five subclasses raised from inside the guarded region is caught
        by the declared-base handler and reported with its own typed reason,
        never a traceback and never an ordinary rc."""
        for klass in OWNER_SUBCLASSES:
            with self.subTest(klass.__name__):
                project = tempfile.mkdtemp(prefix="cowork-owner-p2-typed-")
                self.addCleanup(shutil.rmtree, project, True)
                self.spath = os.path.join(project, ".cowork", "session.json")
                instance = self._instance(klass)

                def boom(*_a, **_k):
                    raise instance

                rc, out = self.run_flow(["--new"], scout=boom)
                self.assertEqual(rc, 3)
                self.assertTrue(out.strip().startswith("cowork: "))
                session_uuid = state_store.get_session_uuid(
                    state_store.load(self.spath))
                with open(trace_store.trace_path_for(session_uuid), "r") as fh:
                    events = [json.loads(x) for x in fh if x.strip()]
                ends = [e for e in events if e.get("event") == "run.end"]
                self.assertEqual(ends[-1]["reason"],
                                 owner.OWNER_REFUSAL_REASONS[klass])
                self.assertEqual(ends[-1]["rc"], 3)
                self.assertEqual(
                    owner.read_owner_lease(session_uuid)["terminal_reason"],
                    "owner_refusal")

    @staticmethod
    def _instance(klass):
        if klass is owner.OwnerLeaseConflict:
            return klass("sess-1", "live_owner")
        if klass is owner.OwnerLeaseCorrupt:
            return klass("sess-1", "bad bytes")
        if klass is owner.OwnerLeaseLost:
            return klass("sess-1", "owner-1", 1)
        if klass is owner.ProviderSessionConflict:
            return klass("claude", "sid-1", "other-session", "scout")
        return klass("claude", "sid-1", "scout", "index unreadable")


# --------------------------------------------------------------------------- #
# G9 / F6 -- the resume-trigger owner-conflict exit code.                       #
# --------------------------------------------------------------------------- #


class ResumeTriggerExitCodeTests(OwnerGateTestCase):

    def test_owner_conflict_holds_exit_code_ten_exclusively(self):
        """G9. `owner_conflict` maps to 10, nothing else maps to 10, and no
        integer in the mapping is used twice."""
        self.assertEqual(cowork.RESUME_TRIGGER_EXIT_OWNER_CONFLICT, 10)
        codes = cowork.RESUME_TRIGGER_EXIT_CODES
        self.assertEqual(codes["owner_conflict"], 10)
        self.assertEqual([k for k, v in codes.items() if v == 10],
                         ["owner_conflict"])
        self.assertEqual(len(set(codes.values())), len(codes))

    def test_a_second_resume_trigger_is_refused_with_exit_ten(self):
        """F6. A duplicate resume-trigger against a session another process
        already owns: exit 10, outcome `owner_conflict`, and it is refused
        BEFORE step 2 -- so zero claims, zero phase advances, zero
        constructions and zero sends, each asserted by a double that raises
        on call."""
        session_uuid = str(uuid.uuid4())
        self.seed_live_owner(session_uuid)
        lines = []
        lease = {"lease_id": "lease-1", "role": "scout"}
        with mock.patch.object(cowork.capacity_scheduler, "wake_decision",
                               lambda *_a, **_k: "due"), \
                mock.patch.object(state_store, "read_pause_lease",
                                  lambda *_a, **_k: {"lease_id": "lease-1"}), \
                mock.patch.object(state_store, "pause_lease_from_stored_record",
                                  lambda record: record), \
                mock.patch.object(cowork.capacity_contracts,
                                  "validate_pause_lease",
                                  lambda record: dict(lease)), \
                mock.patch.object(cowork, "_current_role_work_id_for_session",
                                  lambda *_a, **_k: "work-1"), \
                mock.patch.object(cowork, "_capacity_candidate_binding",
                                  lambda *_a, **_k: {"candidate": "c"}), \
                mock.patch.object(cowork, "_find_session_state",
                                  lambda *_a, **_k: {"config": {}}), \
                mock.patch.object(cowork, "_resume_wake_failure_kind",
                                  lambda *_a, **_k: None), \
                mock.patch.object(state_store, "read_pending_turn_before_pause",
                                  lambda *_a, **_k: {"acknowledged": True,
                                                     "lease_id": "lease-1"}), \
                mock.patch.object(cowork.capacity_scheduler, "claim",
                                  _Raises("capacity_scheduler.claim")), \
                mock.patch.object(cowork, "_advance_phase",
                                  _Raises("_advance_phase")), \
                mock.patch.object(cowork, "_construct_resume_session",
                                  _Raises("_construct_resume_session")), \
                mock.patch.object(cowork, "_send", _Raises("_send")):
            rc = cowork.run_resume_trigger(
                ["--session-uuid", session_uuid, "--lease-id", "lease-1",
                 "--claimant-ref", "test", "--automation-ref", "test",
                 "--cwd", self.project],
                output=lines.append)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_OWNER_CONFLICT)
        payload = json.loads(lines[-1])
        self.assertEqual(payload["outcome"], "owner_conflict")
        self.assertEqual(payload["reason"], "session_owner_conflict")

    def test_the_first_resume_trigger_acquires_and_releases_its_lease(self):
        """The converse of F6: the one that PROCEEDS holds a real lease for
        the owned region and releases it on the way out, so a later
        resume-trigger is not blocked by its predecessor."""
        session_uuid = str(uuid.uuid4())
        lines = []
        lease = {"lease_id": "lease-1", "role": "scout"}
        observed = {}

        def claim_spy(*_a, **_k):
            observed["ctx"] = cowork._current_owner_context()
            raise cowork.capacity_scheduler.SchedulerLeaseConflict(
                "lease-1", "already_claimed")

        with mock.patch.object(cowork.capacity_scheduler, "wake_decision",
                               lambda *_a, **_k: "due"), \
                mock.patch.object(state_store, "read_pause_lease",
                                  lambda *_a, **_k: {"lease_id": "lease-1"}), \
                mock.patch.object(state_store, "pause_lease_from_stored_record",
                                  lambda record: record), \
                mock.patch.object(cowork.capacity_contracts,
                                  "validate_pause_lease",
                                  lambda record: dict(lease)), \
                mock.patch.object(cowork, "_current_role_work_id_for_session",
                                  lambda *_a, **_k: "work-1"), \
                mock.patch.object(cowork, "_capacity_candidate_binding",
                                  lambda *_a, **_k: {"candidate": "c"}), \
                mock.patch.object(cowork, "_find_session_state",
                                  lambda *_a, **_k: {"config": {}}), \
                mock.patch.object(cowork, "_resume_wake_failure_kind",
                                  lambda *_a, **_k: None), \
                mock.patch.object(state_store, "read_pending_turn_before_pause",
                                  lambda *_a, **_k: {"acknowledged": True,
                                                     "lease_id": "lease-1"}), \
                mock.patch.object(cowork.capacity_scheduler, "claim",
                                  claim_spy):
            rc = cowork.run_resume_trigger(
                ["--session-uuid", session_uuid, "--lease-id", "lease-1",
                 "--claimant-ref", "test", "--automation-ref", "test",
                 "--cwd", self.project],
                output=lines.append)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_CONFLICT)
        # The claim ran INSIDE the owned region ...
        self.assertIs(observed["ctx"]["enforced"], True)
        self.assertEqual(observed["ctx"]["session_uuid"], session_uuid)
        # ... and the lease was released on the way out, exactly once.
        record = owner.read_owner_lease(session_uuid)
        self.assertEqual(record["state"], "released")
        self.assertEqual(record["entry_point"], "resume_trigger")
        self.assertIs(cowork._OWNER_CONTEXT["enforced"], False)

    def test_a_lease_lost_mid_trigger_is_reported_not_raised(self):
        """A takeover WHILE this trigger is working reaches a governed seam
        and raises. The published contract is "exactly one JSON line, one of
        `RESUME_TRIGGER_EXIT_CODES`, never `sys.exit`", so the refusal is
        caught at the same declared base `run_flow` uses and reported as
        `owner_conflict` -- never as a traceback."""
        session_uuid = str(uuid.uuid4())
        lines = []
        lease = {"lease_id": "lease-1", "role": "scout"}

        def steal_then_claim(*_a, **_k):
            # A successor takes the lease over mid-run; the next governed
            # seam this trigger reaches must observe it.
            ctx = cowork._current_owner_context()
            owner.release_owner_lease(session_uuid, ctx["owner_id"],
                                      ctx["epoch"], "stolen")
            cowork._advance_phase(session_uuid, "work-1", "gate_validated")
            raise AssertionError("the governed seam did not fence")

        with mock.patch.object(cowork.capacity_scheduler, "wake_decision",
                               lambda *_a, **_k: "due"), \
                mock.patch.object(state_store, "read_pause_lease",
                                  lambda *_a, **_k: {"lease_id": "lease-1"}), \
                mock.patch.object(state_store, "pause_lease_from_stored_record",
                                  lambda record: record), \
                mock.patch.object(cowork.capacity_contracts,
                                  "validate_pause_lease",
                                  lambda record: dict(lease)), \
                mock.patch.object(cowork, "_current_role_work_id_for_session",
                                  lambda *_a, **_k: "work-1"), \
                mock.patch.object(cowork, "_capacity_candidate_binding",
                                  lambda *_a, **_k: {"candidate": "c"}), \
                mock.patch.object(cowork, "_find_session_state",
                                  lambda *_a, **_k: {"config": {}}), \
                mock.patch.object(cowork, "_resume_wake_failure_kind",
                                  lambda *_a, **_k: None), \
                mock.patch.object(state_store, "read_pending_turn_before_pause",
                                  lambda *_a, **_k: {"acknowledged": True,
                                                     "lease_id": "lease-1"}), \
                mock.patch.object(cowork.capacity_scheduler, "claim",
                                  steal_then_claim):
            rc = cowork.run_resume_trigger(
                ["--session-uuid", session_uuid, "--lease-id", "lease-1",
                 "--claimant-ref", "test", "--automation-ref", "test",
                 "--cwd", self.project],
                output=lines.append)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_OWNER_CONFLICT)
        payload = json.loads(lines[-1])
        self.assertEqual(payload["outcome"], "owner_conflict")
        self.assertEqual(payload["reason"], "session_owner_lost")
        self.assertIs(cowork._OWNER_CONTEXT["enforced"], False)

    def test_the_read_only_preflight_refusals_stay_lease_free(self):
        """The acquisition sits before step 2's CLAIM, which is where the
        first state-mutating step is. Steps 0 and 1 are read-only preflight,
        so a refusal there acquires nothing at all -- a stuck session can
        still be probed."""
        session_uuid = str(uuid.uuid4())
        lines = []
        with mock.patch.object(cowork.capacity_scheduler, "wake_decision",
                               lambda *_a, **_k: "due"), \
                mock.patch.object(state_store, "read_pause_lease",
                                  lambda *_a, **_k: None):
            rc = cowork.run_resume_trigger(
                ["--session-uuid", session_uuid, "--lease-id", "lease-1",
                 "--claimant-ref", "test", "--automation-ref", "test",
                 "--cwd", self.project],
                output=lines.append)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_CONFLICT)
        self.assertIsNone(owner.read_owner_lease(session_uuid))


# --------------------------------------------------------------------------- #
# G1f / G3 / G10 / G11c / G11d -- the standing static gates.                    #
# --------------------------------------------------------------------------- #


class StaticGateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = _cowork_tree()
        cls.top = _top_level(cls.tree)
        cls.run_flow = cls.top["run_flow"]
        cls.closures = _closures_of(cls.run_flow)

    # -- G3a -------------------------------------------------------------- #

    def _acquisition_and_wrapper(self):
        """The acquisition `Try` (the one whose body calls
        `acquire_owner_lease`/`take_over`) and the wrapper `Try` that follows
        it, both located in `run_flow`'s own top-level statement list."""
        body = self.run_flow.body
        acq_index = None
        for index, stmt in enumerate(body):
            if any(name in ("acquire_owner_lease", "take_over")
                   for name in _call_names(stmt)):
                acq_index = index
                break
        self.assertIsNotNone(acq_index, "no acquisition found in run_flow")
        wrapper = None
        wrap_index = None
        for index in range(acq_index + 1, len(body)):
            if isinstance(body[index], ast.Try) and body[index].finalbody:
                wrapper = body[index]
                wrap_index = index
                break
        self.assertIsNotNone(wrapper, "no try/finally wrapper after acquire")
        return body, acq_index, wrap_index, wrapper

    def test_the_acquisition_is_followed_by_the_release_wrapper(self):
        body, acq, wrap, wrapper = self._acquisition_and_wrapper()
        # Nothing between the acquisition construct and the wrapper may
        # return: once the lease is held, every exit must go through the
        # `finally`.
        for stmt in body[acq + 1:wrap]:
            for node in ast.walk(stmt):
                self.assertNotIsInstance(node, ast.Return)
        # And the wrapper must be the LAST statement of run_flow, so the
        # whole post-acquisition body is inside it.
        self.assertEqual(wrap, len(body) - 1)

    def test_the_finally_tears_down_in_the_specified_order(self):
        _body, _acq, _wrap, wrapper = self._acquisition_and_wrapper()
        names = []
        for stmt in wrapper.finalbody:
            names.extend(_call_names(stmt))
        wanted = ["set", "join", "release_owner_lease",
                  "release_provider_bindings", "_restore_owner_context"]
        positions = [names.index(n) for n in wanted]
        self.assertEqual(positions, sorted(positions), names)

    def test_the_first_handler_catches_the_declared_base(self):
        """G13d / contract C3, catch point 1."""
        _body, _acq, _wrap, wrapper = self._acquisition_and_wrapper()
        # The declared base, alone and first: no earlier or wider clause can
        # see an owner refusal before its typed handler does.
        self.assertEqual(_handler_names(wrapper.handlers[0]),
                         ["cowork_owner.OwnerLeaseError"])
        later = wrapper.handlers[1:]
        names = [n for h in later for n in _handler_names(h)]
        # No later clause re-catches owner errors by subclass name (a stale
        # tuple is the SW64S-M02 omission), and no broad clause swallows.
        self.assertFalse([n for n in names
                          if n is not None and n.startswith("cowork_owner.")],
                         names)
        self.assertFalse([ast.unparse(h.type) if h.type else None
                          for h in later if self._swallows(h)])
        # A tampered decision answer is its own typed stop, never left to the
        # crash limb; it is a ValueError, so it cannot shadow the base.
        self.assertIn("state_store.DecisionAnswerTampered", names)
        # Everything else still ends in the re-raising crash limb.
        self.assertEqual(_handler_names(wrapper.handlers[-1]),
                         ["BaseException"])

    # -- G10 -------------------------------------------------------------- #

    def test_the_heartbeat_starts_before_the_wrapper_and_stops_inside_it(self):
        body, acq, wrap, wrapper = self._acquisition_and_wrapper()
        pre = []
        for stmt in body[:wrap]:
            pre.extend(_call_names(stmt))
        self.assertIn("_run_owner_heartbeat_loop",
                      [ast.unparse(n) for stmt in body[:wrap]
                       for n in ast.walk(stmt) if isinstance(n, ast.Name)])
        self.assertIn("start", pre)
        final = []
        for stmt in wrapper.finalbody:
            final.extend(_call_names(stmt))
        self.assertIn("set", final)
        self.assertIn("join", final)

    # -- G3b -------------------------------------------------------------- #

    def test_the_external_kill_handler_marks_terminal_last_and_safely(self):
        handler = self.closures["_handle_external_kill"][0]
        self.assertEqual(len(self.closures["_handle_external_kill"]), 1)
        body = handler.body
        # The three pre-existing effects, then the mark, then the SystemExit.
        def index_of(pred):
            for i, stmt in enumerate(body):
                if pred(stmt):
                    return i
            return -1

        advance = index_of(lambda s: "_advance_phase" in _call_names(s))
        traced = index_of(lambda s: "event" in _call_names(s))
        marked = index_of(
            lambda s: "mark_owner_terminal_unlocked" in _call_names(s))
        exiting = index_of(lambda s: isinstance(s, ast.Raise))
        self.assertGreater(marked, advance)
        self.assertGreater(marked, traced)
        self.assertGreater(exiting, marked)
        # It is wrapped, so a #64 failure cannot cost the SystemExit.
        mark_stmt = body[marked]
        tries = [n for n in ast.walk(mark_stmt) if isinstance(n, ast.Try)]
        self.assertTrue(tries)
        # It reads the module owner context -- and no other module state.
        self.assertIn("_current_owner_context", _call_names(handler))
        reads = {n.id for n in ast.walk(handler) if isinstance(n, ast.Name)}
        self.assertNotIn("_OWNER_CONTEXT", reads)
        self.assertNotIn("_owner_handle_box", reads)
        self.assertLess(
            min(i for i, s in enumerate(body)
                if "_current_owner_context" in _call_names(s)), marked + 1)
        # And it takes no lock, mutates no lease and appends to no JSONL.
        forbidden = {"_locked_json_transaction", "acquire_owner_lease",
                     "release_owner_lease", "renew_owner_lease", "take_over",
                     "release_provider_bindings", "bind_provider_session",
                     "append_jsonl_atomic"}
        self.assertEqual(set(_call_names(handler)) & forbidden, set())

    def test_owner_handle_box_occurs_nowhere(self):
        self.assertNotIn("_owner_handle_box", _cowork_source())

    # -- G3c -------------------------------------------------------------- #

    def test_main_has_only_the_declared_base_backstop(self):
        main = self.top["main"]
        forbidden = {"acquire_owner_lease", "release_owner_lease",
                     "renew_owner_lease", "take_over",
                     "mark_owner_terminal_unlocked", "bind_provider_session",
                     "release_provider_bindings", "_set_owner_context",
                     "_restore_owner_context"}
        self.assertEqual(set(_call_names(main)) & forbidden, set())
        handlers = [h for node in ast.walk(main) if isinstance(node, ast.Try)
                    for h in node.handlers]
        owner_handlers = [h for h in handlers
                          if any("OwnerLease" in n
                                 for n in _handler_names(h) if n)]
        self.assertEqual(len(owner_handlers), 1)
        self.assertEqual(_handler_names(owner_handlers[0]),
                         ["cowork_owner.OwnerLeaseError"])
        self.assertEqual(
            [ast.unparse(s) for s in owner_handlers[0].body
             if not isinstance(s, ast.Expr)],
            ["return 3"])

    # -- G11c / G11d ------------------------------------------------------- #

    def test_the_dispatch_sites_and_the_single_evaluator_exemption(self):
        """G11c. Exactly 22 call sites, exactly one carrying
        `purpose="evaluator"`, and that one inside
        `_isolated_evaluator_session`. The exemption cannot silently widen."""
        sites = [c for c in ast.walk(self.tree) if isinstance(c, ast.Call)
                 and _called_name(c) == "_decide_and_trace"]
        self.assertEqual(len(sites), 22)
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
        target = self.top["_isolated_evaluator_session"]
        self.assertTrue(target.lineno <= evaluator[0].lineno
                        <= target.end_lineno)

    def test_the_evaluation_region_is_fenced_at_its_boundary(self):
        """G11d, with the docstring qualifier the plan requires: the closure's
        current first body statement IS its docstring, and in the AST a
        docstring is a statement, so the unqualified assertion would fail a
        correct candidate."""
        nodes = self.closures["evaluation_transition"]
        self.assertEqual(len(nodes), 1)
        body = list(nodes[0].body)
        if (isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        self.assertEqual(ast.unparse(body[0]), "_require_owner(session_uuid)")

    def test_the_governed_write_seams_are_fenced_first(self):
        for name, expected in (
                ("_ensure_work_unit", "_require_owner(session_uuid)"),
                ("_bind_candidate", "_require_owner(session_uuid)"),
                ("_advance_phase",
                 "_require_owner(session_uuid, advisory=unlocked)")):
            with self.subTest(name):
                body = list(self.top[name].body)
                if (isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)):
                    body = body[1:]
                self.assertEqual(ast.unparse(body[0]), expected)

    # -- G1f --------------------------------------------------------------- #

    @staticmethod
    def _swallows(handler):
        """A handler SWALLOWS when it is broad AND does not re-raise.

        The qualifier is the plan's own, not a convenience: rule E5 counts
        `except KeyboardInterrupt: raise` as a non-swallow explicitly, and
        section 3.5's mandated release wrapper ends its `except BaseException`
        limb with a bare `raise` for exactly that reason -- it records the
        release reason and lets the exception continue. Treating a re-raising
        handler as a swallow would fail the very shape this package is
        required to produce."""
        names = _handler_names(handler)
        broad = any(n is None or n in ("Exception", "BaseException")
                    for n in names)
        if not broad:
            return False
        return not any(isinstance(stmt, ast.Raise) and stmt.exc is None
                       for stmt in handler.body)

    def _broad_try_spans(self, tree):
        spans = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if any(self._swallows(h) for h in node.handlers):
                for stmt in node.body:
                    spans.append((stmt.lineno, stmt.end_lineno))
        return spans

    def test_no_fenced_seam_sits_under_a_broad_handler(self):
        """G1f part 1, widened to EVERY exception in the declared hierarchy by
        covering every fenced seam rather than only the dispatch one."""
        spans = self._broad_try_spans(self.tree)
        fenced = {"_decide_and_trace", "_advance_phase", "_ensure_work_unit",
                  "_bind_candidate", "evaluation_transition"}
        offenders = []
        for call in ast.walk(self.tree):
            if not isinstance(call, ast.Call):
                continue
            if _called_name(call) not in fenced:
                continue
            for start, end in spans:
                if start <= call.lineno <= end:
                    offenders.append((_called_name(call), call.lineno))
        self.assertEqual(offenders, [])

    def test_the_role_loop_try_still_has_zero_handlers(self):
        """G1f part 2. The `try` wrapping `run_flow`'s main loop has a
        `finally` and NO handlers, so nothing raised inside it is swallowed --
        which is why an owner refusal from a role runner reaches catch point
        1."""
        candidates = [n for n in ast.walk(self.run_flow)
                      if isinstance(n, ast.Try) and n.finalbody
                      and any("signal" in ast.unparse(s)
                              for s in n.finalbody)]
        self.assertTrue(candidates)
        for node in candidates:
            self.assertEqual(node.handlers, [])

    def test_the_six_callback_seams_are_enumerated_and_unstranded(self):
        """G1f part 3, as a SET assertion rather than a line list: a seventh
        seam added later fails the count instead of going silently
        unchecked."""
        bridge_tree = ast.parse(
            _read_local_bytes("scripts/cowork_bridge.py"),
            filename="cowork_bridge.py")
        bridge_seams = [c for c in ast.walk(bridge_tree)
                        if isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "on_session_id"]
        self.assertEqual(len(bridge_seams), 2)
        for start, end in self._broad_try_spans(bridge_tree):
            for call in bridge_seams:
                self.assertFalse(start <= call.lineno <= end)

        direct = [c for c in ast.walk(self.tree) if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Name)
                  and c.func.id == "on_session"]
        lambdas = [n for n in ast.walk(self.tree)
                   if isinstance(n, ast.Lambda)
                   and any(isinstance(c, ast.Call)
                           and isinstance(c.func, ast.Name)
                           and c.func.id == "on_session"
                           for c in ast.walk(n))]
        direct = [c for c in direct
                  if not any(c in list(ast.walk(lam)) for lam in lambdas)]
        self.assertEqual(len(direct), 4)
        owners = {"run_reviewer_once", "run_scout", "run_planner",
                  "run_builder"}
        for call in direct:
            hosts = {name for name in owners
                     if self.top[name].lineno <= call.lineno
                     <= self.top[name].end_lineno}
            self.assertEqual(len(hosts), 1, ast.unparse(call))
        spans = self._broad_try_spans(self.tree)
        for call in direct:
            for start, end in spans:
                self.assertFalse(start <= call.lineno <= end)


# --------------------------------------------------------------------------- #
# G11f -- the evaluator drain declaration.                                     #
# --------------------------------------------------------------------------- #


class EvalDrainDeclarationTests(unittest.TestCase):

    def test_eval_drain_declares_it_never_raises(self):
        """Rule E4: the owner gate relies on `cowork_eval.drain` never
        raising, and the module declares that contract."""
        import cowork_eval
        self.assertIn("never raises", cowork_eval.drain.__doc__)


if __name__ == "__main__":
    unittest.main()
