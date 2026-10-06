#!/usr/bin/env python3
"""Cross-package acceptance tests for the governed parallel work graph.

These tests protect behavior expected of every future revision of the graph
stack, driven through the shipped surfaces: the `cowork graph` CLI
(cowork_graph_cli), the durable store (cowork_graph_store) and the pure kernel
(cowork_graph), plus cowork.run_flow with fake lead runners for the child
fences. They prove, on neutral inputs only:

* admission is fail-closed: every unsafe revision is refused with its closed
  reason code and CLI exit status, and leaves durable state byte-identical;
* ownership, the concurrency ceiling and the slot ledger hold under fan-out,
  crash and restart: held slots never exceed the effective cap, a vertex is
  bound once and a claim is recorded once per lease epoch;
* a join is a deterministic, receipt-backed, failure-aware decision that
  never merges anything.

Every fixture is built at run time: a temporary parent directory holding a
throwaway git repository, its linked worktrees, authority documents and a
private COWORK_SESSIONS_ROOT. Ids are fresh UUIDs (or derived from a tag where
a test pins them), graph clocks are injected `now` strings where the store
accepts one, and the shipped concurrency cap of 1 is raised only inside a
scoped patch of the profile definitions. Cross-process races use fork-context
multiprocessing.

Run: python3 scripts/cowork_offline_tests.py test_graph_acceptance
"""

import contextlib
import datetime
import hashlib
import io
import itertools
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_capacity as capacity_contracts  # noqa: E402
import cowork_capacity_scheduler  # noqa: E402
import cowork_execution_profiles as profiles  # noqa: E402
import cowork_graph  # noqa: E402
import cowork_graph_cli  # noqa: E402
import cowork_graph_store as store  # noqa: E402
import cowork_owner  # noqa: E402
import cowork_state  # noqa: E402
import cowork_verification  # noqa: E402

NOW = "2030-01-01T00:00:00Z"
LATER = "2030-01-01T01:00:00Z"
STANDARD = "standard"
IGNORE_RULE = ".cowork/\n"
FABRICATED_PID_START = "2020-01-01T00:00:00Z"
RACE_TIMEOUT_S = 120
SUITE_LABEL = "graph acceptance suite"
TURN_TEXT = "neutral paused turn"
PAUSE_ISSUED_AT = "2025-12-31T00:00:00Z"
PAUSE_NOT_BEFORE = "2025-12-31T01:00:00Z"
WAKE_NOW = "2026-01-02T00:00:00Z"
ENVELOPE = ("cowork_graph_result", "rc", "op", "outcome", "reason",
            "graph_id", "work_id")
DECISION_KEYS = {"schema_version", "record", "graph_id", "graph_revision",
                 "join_id", "rule", "outcome", "members", "decision_digest"}
FORBIDDEN_KEY_WORDS = ("merge", "source", "branch")
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


def _tagged_uuid(tag):
    """A canonical UUID4 derived from `tag`, so a test can pin an id."""
    raw = hashlib.sha256(tag.encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=raw, version=4))


def _hex64(tag):
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


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


def _tree(root):
    """{relative path: bytes} for every working file and {relative dir/:
    '<dir>'} for every working directory under `root` (version-control
    metadata excluded); {} when `root` does not exist."""
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        rel = os.path.relpath(dirpath, root)
        out[rel + "/"] = "<dir>"
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = fh.read()
    return out


def _durable(root):
    """{relative path: bytes} of the durable files under `root`. Lock files
    are inert coordination artifacts and directories carry no state, so
    neither is part of the durable record."""
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".lock"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = fh.read()
    return out


def _patched_cap(n):
    """Every shipped profile with a bounded concurrency cap of n."""
    definitions = {}
    for name, definition in profiles.PROFILE_DEFINITIONS.items():
        thawed = profiles._thaw(definition)
        thawed["concurrency"] = {"contract_version": 1, "mode": "bounded",
                                 "max_parallel_vertices": n,
                                 "fan_out_shapes": []}
        definitions[name] = thawed
    return mock.patch.object(profiles, "PROFILE_DEFINITIONS",
                             profiles._freeze(definitions))


def _failing_graph_writes():
    """Durable writes of any graph.json report failure; others are real."""
    real = cowork_state.write_json_atomic_durable

    def fake(path, data):
        if os.path.basename(path) == "graph.json":
            return False
        return real(path, data)

    return mock.patch.object(cowork_state, "write_json_atomic_durable",
                             side_effect=fake)


def _pinned_graph_id(graph_id):
    """The store's uuid4 draw returns `graph_id`. Only the store module's own
    `uuid` name is replaced; UUID stays real for its id validation."""
    namespace = types.SimpleNamespace(
        UUID=uuid.UUID, uuid4=lambda: uuid.UUID(graph_id))
    return mock.patch.object(store, "uuid", namespace)


def _race_child(barrier, queue, target, args):
    try:
        barrier.wait(RACE_TIMEOUT_S)
        queue.put(("ok", target(*args)))
    except cowork_graph.GraphRefusal as exc:
        queue.put(("refused", exc.code))
    except BaseException as exc:
        queue.put(("error", "%s: %s" % (type(exc).__name__, exc)))


def _race(target, arg_lists):
    """Run target(*args) for each args in forked processes released together
    by a Barrier. Returns [('ok', result) | ('refused', code) |
    ('error', text)] in completion order."""
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(len(arg_lists))
    queue = ctx.Queue()
    procs = [ctx.Process(target=_race_child,
                         args=(barrier, queue, target, tuple(args)))
             for args in arg_lists]
    for proc in procs:
        proc.start()
    try:
        results = [queue.get(timeout=RACE_TIMEOUT_S) for _ in procs]
    finally:
        for proc in procs:
            proc.join(RACE_TIMEOUT_S)
            if proc.is_alive():
                proc.terminate()
                proc.join(10)
    return results


def _run_child(fn):
    """Run fn in a forked child and return its exit code."""
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=fn)
    proc.start()
    proc.join(RACE_TIMEOUT_S)
    if proc.is_alive():
        proc.terminate()
        proc.join(10)
    return proc.exitcode


def _walk_keys(value):
    """Every dict key found anywhere inside a JSON-shaped value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            for inner in _walk_keys(item):
                yield inner
    elif isinstance(value, list):
        for item in value:
            for inner in _walk_keys(item):
                yield inner


def _bind_real_capacity_candidate(session_uuid, role, controller="claude"):
    """Compile a real dispatch manifest for (session_uuid, role) and bind it
    as the role's WorkUnit candidate, in the preflighting -> running ->
    candidate-bound order. Returns (work_id, binding)."""
    work_id = cowork._role_work_id(session_uuid, role, 0, 0)
    manifest, _ = cowork._compile_role_manifest(
        role=role, session_uuid=session_uuid, work_id=role,
        controller=controller, mode="implement", model=None, effort=None,
        sessions_dir=cowork_state.session_assets_dir(session_uuid))
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


class _AcceptanceEnvMixin(object):
    """A temp parent with a main repository (ignoring `.cowork/`), vertex
    roots as linked worktrees, a pinned sessions root, authority, owner-lease
    and transaction-artifact factories, a CLI driver that checks the
    one-result-line contract on every call, and ledger assertions."""

    def setUp(self):
        super().setUp()
        self.parent = os.path.realpath(tempfile.mkdtemp(prefix="ga-"))
        self.addCleanup(shutil.rmtree, self.parent, True)
        self.addCleanup(os.chdir, os.getcwd())
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        self._counter = itertools.count(1)
        self.use_sessions()
        self.main = self.make_repo("main")
        self.head = _git(["rev-parse", "HEAD"], self.main)
        self.scout_calls = []

    # -- factories ---------------------------------------------------------- #

    def name(self, prefix):
        return "%s%d" % (prefix, next(self._counter))

    def use_sessions(self, path=None):
        """Point COWORK_SESSIONS_ROOT at `path` (default: a fresh sibling of
        the repositories). The directory is not created here."""
        path = path or os.path.join(self.parent, self.name("sessions-"))
        os.environ["COWORK_SESSIONS_ROOT"] = path
        self.sessions = path
        return path

    def cap(self, n):
        """Raise the profile cap to `n` for the rest of the test."""
        patch = _patched_cap(n)
        patch.start()
        self.addCleanup(patch.stop)

    def make_repo(self, name, ignore=True):
        path = os.path.join(self.parent, name)
        os.mkdir(path)
        _git(["init", "-q"], path)
        if ignore:
            with open(os.path.join(path, ".gitignore"), "w") as fh:
                fh.write(IGNORE_RULE)
        with open(os.path.join(path, "README.txt"), "w") as fh:
            fh.write("neutral fixture\n")
        _git(["add", "-A"], path)
        _git(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"],
             path)
        return os.path.realpath(path)

    def worktree(self, inside=None, repo=None):
        path = os.path.join(inside or self.parent, self.name("wt"))
        _git(["worktree", "add", "--detach", path], repo or self.main)
        return os.path.realpath(path)

    def authority(self):
        tag = self.name("authority-")
        data = json.dumps({"authority": tag}).encode("utf-8")
        path = os.path.join(self.parent, "authority", tag + ".json")
        _write_bytes(path, data)
        return path, hashlib.sha256(data).hexdigest()

    def vertex(self, root=None, preds=(), profile=STANDARD, work_id=None,
               authority=None):
        path, digest = authority or self.authority()
        return {"work_id": work_id or _uuid(), "root": root or self.worktree(),
                "base_commit": self.head, "authority_path": path,
                "authority_digest": digest, "profile": profile,
                "predecessors": list(preds)}

    def join_decl(self, vertices):
        return {"join_id": _uuid(), "rule": "all_succeeded",
                "requires": [v["work_id"] for v in vertices]}

    def revision(self, vertices, max_parallel=1, joins=None):
        doc = {"schema_version": 1, "max_parallel": max_parallel,
               "vertices": [dict(v) for v in vertices]}
        if joins is not None:
            doc["joins"] = joins
        return doc

    def revision_file(self, doc):
        path = os.path.join(self.parent, "revisions",
                            self.name("rev-") + ".json")
        _write_bytes(path, json.dumps(doc).encode("utf-8"))
        return path

    def admit(self, vertices, max_parallel=1, joins=None):
        """Admit a new graph through the CLI; returns its id."""
        line = self.ok("admit", "--new", "--revision-file",
                       self.revision_file(self.revision(
                           vertices, max_parallel, joins)))
        return line["graph_id"]

    def start(self, graph_id, v, now=NOW):
        """Claim `v` and bind a fresh child session to it. Returns
        (session, lease_epoch)."""
        info = store.claim(graph_id, v["work_id"], now=now)
        session = _uuid()
        store.bind_session(graph_id, v["work_id"], info["lease_epoch"],
                           session, v["root"], v["profile"], now=now)
        return session, info["lease_epoch"]

    def running(self, max_parallel=1):
        """A one-vertex graph whose vertex is bound to a fresh session."""
        v = self.vertex()
        graph_id = self.admit([v], max_parallel)
        session, epoch = self.start(graph_id, v)
        return types.SimpleNamespace(
            graph_id=graph_id, work_id=v["work_id"], root=v["root"],
            session=session, epoch=epoch, vertex=v)

    def claimed(self, profile=STANDARD):
        """A one-vertex graph whose vertex is claimed and not yet bound."""
        v = self.vertex(profile=profile)
        graph_id = self.admit([v])
        info = store.claim(graph_id, v["work_id"], now=NOW)
        return types.SimpleNamespace(
            graph_id=graph_id, work_id=v["work_id"], root=v["root"],
            epoch=info["lease_epoch"], launch_argv=info["launch_argv"],
            profile=profile, vertex=v)

    def live_owner(self, session):
        claimant = cowork_owner.owner_identity(session, "run_flow",
                                               self.parent, None)
        lease = cowork_owner.acquire_owner_lease(session, claimant)
        return lease["owner_id"], lease["epoch"]

    def expired_owner(self, session, pid_start_source):
        """A lease past its deadline naming an exited pid on this host:
        'ps_lstart' classifies stale_dead_owner, 'unavailable'
        stale_unproven."""
        claimant = cowork_owner.owner_identity(session, "run_flow",
                                               self.parent, None)
        claimant.update(pid=_dead_pid(), pid_start_at=FABRICATED_PID_START,
                        pid_start_source=pid_start_source)
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=1))
        cowork_owner.acquire_owner_lease(session, claimant, now=past)

    def pause_lease(self, session, automation_ref="auto-graph"):
        lease_id = _uuid()
        cowork_state.create_pause_lease(session, {
            "schema_version": 1, "package_id": "pkg-graph",
            "lease_id": lease_id, "resume_mode": "scheduled",
            "not_before": "2030-01-01T00:10:00Z",
            "automation_ref": automation_ref,
            "consumption_state": "unclaimed", "failed_wake_attempts": 0,
            "issued_at": "2030-01-01T00:00:00Z", "role": "builder",
            "provider_session_id": "provider-" + lease_id,
            "controller_policy_digest": _hex64("policy"),
            "candidate_digest": _hex64("candidate"),
            "artifact_hashes": {"artifact.txt": _hex64("artifact")},
        })
        return lease_id

    def txn_artifacts(self, session, root, transaction_id=None):
        """Accepted green owned-verification artifacts for `session`
        fingerprinting `root` as it is now. Returns (transaction_id,
        manifest_digest)."""
        txn = transaction_id or "txn-" + uuid.uuid4().hex
        digest = store.live_fingerprint(root)
        self.assertIsNotNone(digest)
        cowork_state.write_json_atomic(
            cowork_state.verification_request_path_for(session, txn),
            {"transaction_id": txn, "session_uuid": session, "repo": root,
             "snapshot": {"manifest_digest": digest},
             "inventory": [{"label": SUITE_LABEL}]})
        cowork_state.write_json_atomic(
            cowork_state.verification_result_path_for(session, txn),
            {"transaction_id": txn, "verdict": "green",
             "final_suite_binding": "components_ran_once",
             "snapshot": {"manifest_digest": digest}})
        cowork_state.write_current_receipt_pointer(
            session, {"transaction_id": txn})
        cowork_state.write_verification_disposition(
            session, {"transaction_id": txn, "disposition": "accepted"})
        return txn, digest

    def finish(self, graph_id, v, session):
        """Accepted artifacts, then CLI publication, for a running vertex."""
        self.txn_artifacts(session, v["root"])
        line = self.ok("publish", "--graph-id", graph_id, "--work-id",
                       v["work_id"])
        self.assertEqual(line["publish_outcome"], "published")
        self.assertIs(line["slot_released"], True)
        return line

    def drain(self, graph_id, v):
        """Claim, bind and publish one vertex end to end."""
        session, _epoch = self.start(graph_id, v)
        self.finish(graph_id, v, session)
        return session

    def set_up_pause(self, root, session):
        """Durably pause `session`'s builder for provider capacity: a real
        candidate binding, a scheduled PauseLease, an acknowledged pending
        turn and the awaiting-capacity phase state."""
        os.chdir(root)
        spath = os.path.join(root, ".cowork", "session.json")
        cowork_state.ensure_session(spath, None, session)
        work_id, binding = _bind_real_capacity_candidate(session, "builder")
        lease_id = _uuid()
        automation_ref = "cowork.orchestration_resume/v%d" % (
            cowork_capacity_scheduler.SCHEDULER_DECISION_LAYER_VERSION)
        digest = binding["candidate_manifest_digest"]
        cowork_capacity_scheduler.start_new_episode(session, {
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
        cowork_state.write_pending_turn_before_pause(
            session, "builder", TURN_TEXT, lease_id=lease_id)
        cowork_state.acknowledge_pending_turn_before_pause(
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
        state = cowork_state.load(spath)
        state.setdefault("config", {})["builder"] = {
            "controller": "claude", "model": None, "effort": None,
            "mode": "implement", "yolo": True}
        state.setdefault("sessions", {})["builder"] = {
            "controller": "claude", "id": "prov-sess-1"}
        cowork_state.save(spath, state)
        return types.SimpleNamespace(root=root, session=session,
                                     lease_id=lease_id,
                                     automation_ref=automation_ref)

    # -- reads -------------------------------------------------------------- #

    def graphs(self):
        return cowork_state.graphs_root()

    def snap(self):
        return _durable(self.graphs())

    def graph_bytes(self, graph_id):
        return _read_bytes(cowork_state.graph_state_path_for(graph_id))

    def graph_state(self, graph_id):
        return json.loads(self.graph_bytes(graph_id).decode("utf-8"))

    def registry_ids(self):
        record = cowork_state._read_json_or_raise_if_corrupt(
            cowork_state.graph_registry_path())
        return [] if record is None else record["graph_ids"]

    def held(self, graph_id):
        return [(s["work_id"], s["lease_epoch"]) for s in
                cowork_graph.held_slots(self.graph_state(graph_id))]

    # -- the CLI driver ----------------------------------------------------- #

    def cli(self, *argv):
        """Run one op in-process; assert the one-result-line contract and
        return the parsed line."""
        out = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cowork_graph_cli.main(list(argv), output=out.append)
        return self.check_line("".join(out), rc)

    def check_line(self, text, rc):
        lines = text.splitlines()
        self.assertEqual(len(lines), 1, text)
        self.assertTrue(text.endswith("\n"))
        line = json.loads(lines[-1])
        self.assertIsInstance(line, dict)
        for key in ENVELOPE:
            self.assertIn(key, line)
        self.assertEqual(line["cowork_graph_result"], 1)
        self.assertEqual(line["rc"], rc)
        self.assertIn(line["outcome"], ("ok", "refused", "error"))
        if line["outcome"] == "ok":
            self.assertEqual((line["rc"], line["reason"]), (0, None))
        elif line["outcome"] == "refused":
            self.assertIn(line["reason"], cowork_graph.REASON_RC)
            self.assertEqual(line["rc"],
                             cowork_graph.REASON_RC[line["reason"]])
        return line

    def ok(self, *argv):
        line = self.cli(*argv)
        self.assertEqual(line["outcome"], "ok", line)
        return line

    def refused(self, code, *argv):
        """A CLI refusal: closed code, rc == REASON_RC[code], and the
        durable graph state unchanged."""
        before = self.snap()
        line = self.cli(*argv)
        self.assertEqual((line["outcome"], line["reason"]),
                         ("refused", code), line)
        self.assertEqual(line["rc"], cowork_graph.REASON_RC[code])
        self.assertEqual(self.snap(), before)
        return line

    def assertRefusedCode(self, code, fn, *args, **kwargs):
        """A store or kernel refusal with the closed code and its rc."""
        with self.assertRaises(cowork_graph.GraphRefusal) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(caught.exception.rc, cowork_graph.REASON_RC[code])
        return caught.exception

    def assertRefused(self, code, fn, *args, **kwargs):
        """A store or kernel refusal that leaves the durable graph state
        unchanged."""
        before = self.snap()
        refusal = self.assertRefusedCode(code, fn, *args, **kwargs)
        self.assertEqual(self.snap(), before)
        return refusal

    # -- ledger ------------------------------------------------------------- #

    def assertLedger(self):
        """Across every registered graph: the record satisfies the kernel
        invariants, held slots never exceed the effective cap and equal the
        holding vertices, a claim is recorded once per lease epoch, and every
        session is bound to exactly one vertex matching its session-index
        record."""
        registered = self.registry_ids()
        sessions = {}
        for graph_id in registered:
            state = self.graph_state(graph_id)
            cowork_graph.check_invariants(state)
            held = cowork_graph.held_slots(state)
            holding = [w for w, r in state["vertices"].items()
                       if r["state"] in cowork_graph.HOLDING_STATES]
            self.assertEqual(len(held), len(holding))
            self.assertLessEqual(len(held), cowork_graph.effective_cap(state))
            claims = [(e["work_id"], e["lease_epoch"])
                      for e in state["events"] if e["op"] == "claim"]
            self.assertEqual(len(claims), len(set(claims)))
            self.assertEqual(len(claims), len(state["slots"]))
            for work_id, record in state["vertices"].items():
                if record["session_uuid"] is not None:
                    sessions.setdefault(record["session_uuid"], []).append(
                        (graph_id, work_id))
        for session, places in sessions.items():
            self.assertEqual(len(places), 1, session)
            record = store.vertex_binding_for_session(session)
            self.assertIsNotNone(record, session)
            self.assertEqual((record["graph_id"], record["work_id"]),
                             places[0])
        index_dir = os.path.join(self.graphs(), "session-index")
        if os.path.isdir(index_dir):
            for name in sorted(os.listdir(index_dir)):
                if name.endswith(".lock"):
                    continue
                record = store.vertex_binding_for_session(
                    name[:-len(".json")])
                self.assertIn(record["graph_id"], registered)

    # -- run_flow drivers --------------------------------------------------- #

    def forbidden(self, role):
        def fake(*_a, **_k):
            raise AssertionError("%s must not be dispatched" % role)
        return fake

    def fake_scout(self, outcome="ended"):
        def run(config, context, selected, on_outcome=None, on_session=None,
                resume_id=None, **kw):
            self.scout_calls.append(resume_id)
            if on_session and resume_id is None:
                on_session("claude", "scout-" + _uuid())
            if on_outcome:
                on_outcome(outcome, None)
            return 0
        return run

    def run_flow(self, argv, scout=None):
        box = {}
        out = io.StringIO()
        rc = cowork.run_flow(
            cowork.build_parser().parse_args(argv), io_out=out,
            which=lambda c: "/bin/" + c,
            run_scout_fn=scout or self.forbidden("scout"),
            run_planner_fn=self.forbidden("planner"),
            run_builder_fn=self.forbidden("builder"),
            result_box=box)
        return rc, box

    def bound_run(self):
        """Claim a vertex and run its child from the root with claim's
        launch_argv; the fake scout ends the run (rc 1)."""
        v = self.claimed()
        os.chdir(v.root)
        rc, box = self.run_flow(v.launch_argv + ["--context", "neutral brief"],
                                scout=self.fake_scout())
        self.assertEqual(rc, 1)
        v.session = box["session_uuid"]
        v.spath = box["session_file"]
        v.box = box
        return v

    def assert_unowned(self, session):
        self.assertEqual(cowork_owner.classify_owner_lease(session),
                         "unowned")


# --------------------------------------------------------------------------- #
# Bounded fan-out under the enforced ceiling.                                 #
# --------------------------------------------------------------------------- #


class FanOutUnderCeilingAcceptance(_AcceptanceEnvMixin, unittest.TestCase):

    def test_claims_never_exceed_the_ceiling(self):
        self.cap(2)
        vertices = [self.vertex() for _ in range(4)]
        graph_id = self.admit(vertices, max_parallel=2)
        self.assertLedger()
        g = ("--graph-id", graph_id)
        first = self.ok("claim", *g, "--work-id", vertices[0]["work_id"])
        second = self.ok("claim", *g, "--work-id", vertices[1]["work_id"])
        self.assertEqual((first["lease_epoch"], second["lease_epoch"]),
                         (1, 1))
        self.assertEqual(len(self.held(graph_id)), 2)
        self.assertLedger()
        for v in vertices[2:]:
            refusal = self.refused("cap_reached", "claim", *g, "--work-id",
                                   v["work_id"])
            self.assertEqual(refusal["rc"], 2)
        self.refused("cap_reached", "claim", *g)
        self.assertEqual(sorted(w for w, _e in self.held(graph_id)),
                         sorted(v["work_id"] for v in vertices[:2]))
        self.assertLedger()
        state = self.graph_state(graph_id)
        self.assertEqual(
            [e["work_id"] for e in state["events"] if e["op"] == "claim"],
            [vertices[0]["work_id"], vertices[1]["work_id"]])

    def test_a_slot_is_released_before_the_next_claim(self):
        self.cap(2)
        vertices = [self.vertex() for _ in range(4)]
        graph_id = self.admit(vertices, max_parallel=2)
        g = ("--graph-id", graph_id)
        sessions = {}
        for v in vertices[:2]:
            sessions[v["work_id"]], _epoch = self.start(graph_id, v)
            self.assertLedger()
        waiting = sorted(v["work_id"] for v in vertices[2:])
        view = self.ok("status", *g)
        self.assertEqual(view["ready_order"], waiting)
        self.assertEqual(len(view["held_slots"]), 2)

        self.refused("cap_reached", "claim", *g, "--work-id", waiting[0])
        self.finish(graph_id, vertices[0], sessions[vertices[0]["work_id"]])
        self.assertLedger()
        self.assertEqual([w for w, _e in self.held(graph_id)],
                         [vertices[1]["work_id"]])
        third = self.ok("claim", *g)
        self.assertEqual(third["work_id"], waiting[0])
        self.assertEqual(len(self.held(graph_id)), 2)
        self.assertLedger()
        self.refused("cap_reached", "claim", *g, "--work-id", waiting[1])

        self.finish(graph_id, vertices[1], sessions[vertices[1]["work_id"]])
        fourth = self.ok("claim", *g, "--work-id", waiting[1])
        self.assertEqual(fourth["work_id"], waiting[1])
        self.assertLedger()
        # Each publication released its slot before the next claim landed.
        events = [(e["op"], e["work_id"])
                  for e in self.graph_state(graph_id)["events"]
                  if e["op"] in ("claim", "publish")]
        self.assertEqual(events, [
            ("claim", vertices[0]["work_id"]),
            ("claim", vertices[1]["work_id"]),
            ("publish", vertices[0]["work_id"]),
            ("claim", waiting[0]),
            ("publish", vertices[1]["work_id"]),
            ("claim", waiting[1])])


# --------------------------------------------------------------------------- #
# The shipped serial cap.                                                     #
# --------------------------------------------------------------------------- #


class ShippedSerialAcceptance(_AcceptanceEnvMixin, unittest.TestCase):

    def test_shipped_profiles_cap_one_and_serial_drain_joins(self):
        for name in sorted(profiles.PROFILE_DEFINITIONS):
            with self.subTest(profile=name):
                concurrency = profiles.resolved_vertex_policy(
                    name, "builder")["concurrency"]
                self.assertEqual(concurrency["mode"], "serial")
                self.assertEqual(concurrency["max_parallel_vertices"], 1)

        vertices = [self.vertex(), self.vertex()]
        joins = [self.join_decl(vertices)]
        above = self.revision_file(self.revision(vertices, max_parallel=2,
                                                 joins=joins))
        refusal = self.refused("ceiling_above_policy", "admit", "--new",
                               "--revision-file", above)
        self.assertEqual(refusal["rc"], 2)
        self.assertEqual(self.snap(), {})
        self.assertEqual(self.registry_ids(), [])

        line = self.ok("admit", "--new", "--revision-file",
                       self.revision_file(self.revision(
                           vertices, max_parallel=1, joins=joins)))
        graph_id = line["graph_id"]
        self.assertEqual(line["effective_cap"], 1)
        g = ("--graph-id", graph_id)
        self.assertLedger()

        self.ok("claim", *g, "--work-id", vertices[0]["work_id"])
        self.refused("cap_reached", "claim", *g, "--work-id",
                     vertices[1]["work_id"])
        self.refused("cap_reached", "claim", *g)
        self.assertEqual(len(self.held(graph_id)), 1)
        self.assertLedger()
        self.refused("early_join", "join", *g, "--join-id",
                     joins[0]["join_id"])

        first_session = _uuid()
        store.bind_session(graph_id, vertices[0]["work_id"], 1,
                           first_session, vertices[0]["root"], STANDARD,
                           now=NOW)
        self.finish(graph_id, vertices[0], first_session)
        self.assertEqual(self.held(graph_id), [])
        self.drain(graph_id, vertices[1])
        self.assertLedger()
        self.assertEqual(self.held(graph_id), [])

        joined = self.ok("join", *g, "--join-id", joins[0]["join_id"])
        self.assertEqual(joined["decision"]["outcome"], "joined")
        self.assertEqual(
            [m["work_id"] for m in joined["decision"]["members"]],
            sorted(v["work_id"] for v in vertices))


# --------------------------------------------------------------------------- #
# Arrival-order determinism of the join decision.                             #
# --------------------------------------------------------------------------- #


class PermutationDeterminismAcceptance(_AcceptanceEnvMixin,
                                       unittest.TestCase):

    def test_join_decision_is_independent_of_arrival_order(self):
        self.cap(3)
        vertices = [self.vertex() for _ in range(3)]
        join = self.join_decl(vertices)
        pinned_graph = _tagged_uuid("graph")
        revision = self.revision_file(self.revision(
            vertices, max_parallel=3, joins=[join]))
        sessions = {v["work_id"]: _tagged_uuid("session-" + v["work_id"])
                    for v in vertices}
        txns = {v["work_id"]: "txn-" + v["work_id"] for v in vertices}
        digests = {v["work_id"]: store.live_fingerprint(v["root"])
                   for v in vertices}

        stored = []
        orders = list(itertools.permutations(range(3)))
        self.assertEqual(len(orders), 6)
        for order in orders:
            with self.subTest(order=order):
                self.use_sessions()
                with _pinned_graph_id(pinned_graph):
                    line = self.ok("admit", "--new", "--revision-file",
                                   revision)
                graph_id = line["graph_id"]
                self.assertEqual(graph_id, pinned_graph)
                g = ("--graph-id", graph_id)
                ready = self.ok("status", *g)["ready_order"]
                arrivals = [vertices[i] for i in order]
                for v in arrivals:
                    self.ok("claim", *g, "--work-id", v["work_id"])
                for v in arrivals:
                    store.bind_session(graph_id, v["work_id"], 1,
                                       sessions[v["work_id"]], v["root"],
                                       STANDARD, now=NOW)
                for v in arrivals:
                    session = sessions[v["work_id"]]
                    txn, digest = self.txn_artifacts(
                        session, v["root"], transaction_id=txns[v["work_id"]])
                    self.assertEqual(digest, digests[v["work_id"]])
                    self.ok("publish", *g, "--work-id", v["work_id"])
                self.assertLedger()
                self.assertEqual(self.held(graph_id), [])

                decision = self.ok("join", *g, "--join-id",
                                   join["join_id"])["decision"]
                self.assertEqual(decision["outcome"], "joined")
                self.assertEqual(
                    set(decision), DECISION_KEYS)
                bytes_before = self.graph_bytes(graph_id)
                again = self.ok("join", *g, "--join-id",
                                join["join_id"])["decision"]
                self.assertEqual(again, decision)
                self.assertEqual(self.graph_bytes(graph_id), bytes_before)
                state = self.graph_state(graph_id)
                self.assertEqual(
                    cowork_graph.reduce_join(state, join["join_id"], digests),
                    decision)
                self.assertEqual(
                    state["joins"][join["join_id"]][str(state["revision"])],
                    decision)
                final = self.ok("status", *g)
                stored.append((cowork_graph.canonical_json(decision), ready,
                               final["statuses"], final["ready_order"]))

        self.assertEqual(len(stored), 6)
        self.assertEqual(len(set(item[0] for item in stored)), 1)
        self.assertEqual(len(set(json.dumps(item[1]) for item in stored)), 1)
        self.assertEqual(
            len(set(json.dumps(item[2], sort_keys=True) for item in stored)),
            1)
        self.assertEqual(stored[0][1], sorted(v["work_id"] for v in vertices))


# --------------------------------------------------------------------------- #
# A join is a decision, not a merge.                                          #
# --------------------------------------------------------------------------- #


class JoinNotMergeAcceptance(_AcceptanceEnvMixin, unittest.TestCase):

    def test_join_changes_no_source_head_manifest_or_file(self):
        vertices = [self.vertex(), self.vertex()]
        join = self.join_decl(vertices)
        graph_id = self.admit(vertices, joins=[join])
        for v in vertices:
            self.drain(graph_id, v)
        roots = [v["root"] for v in vertices]
        everything = roots + [self.main]
        fingerprints = {r: cowork_verification.manifest_fingerprint(
            cowork_verification.candidate_manifest(r)) for r in roots}
        heads = {r: _git(["rev-parse", "HEAD"], r) for r in everything}
        trees = {r: _tree(r) for r in everything}
        authorities = {v["work_id"]: _read_bytes(v["authority_path"])
                       for v in vertices}

        line = self.ok("join", "--graph-id", graph_id, "--join-id",
                       join["join_id"])
        decision = line["decision"]
        self.assertEqual(decision["outcome"], "joined")
        self.assertEqual(set(decision), DECISION_KEYS)

        for root in roots:
            self.assertEqual(cowork_verification.manifest_fingerprint(
                cowork_verification.candidate_manifest(root)),
                fingerprints[root])
        for root in everything:
            self.assertEqual(_git(["rev-parse", "HEAD"], root), heads[root])
            self.assertEqual(_tree(root), trees[root])
        self.assertEqual({v["work_id"]: _read_bytes(v["authority_path"])
                          for v in vertices}, authorities)
        for root in everything:
            self.assertEqual(_git(["status", "--porcelain"], root), "")
        members = {m["work_id"]: m["manifest_digest"]
                   for m in decision["members"]}
        self.assertEqual(members, {v["work_id"]: fingerprints[v["root"]]
                                   for v in vertices})
        keys = [str(k).lower() for k in _walk_keys(decision)]
        for word in FORBIDDEN_KEY_WORDS:
            self.assertEqual([k for k in keys if word in k], [], word)


# --------------------------------------------------------------------------- #
# Failure and cancellation are propagated to dependents and the join.         #
# --------------------------------------------------------------------------- #


class FailureCancellationAcceptance(_AcceptanceEnvMixin, unittest.TestCase):

    def _blocked_by(self, end_prerequisite):
        self.cap(2)
        a = self.vertex()
        b = self.vertex()
        c = self.vertex(preds=[a["work_id"]])
        join = self.join_decl([a, b, c])
        graph_id = self.admit([a, b, c], max_parallel=2, joins=[join])
        g = ("--graph-id", graph_id)
        self.start(graph_id, a)
        session_b, _epoch = self.start(graph_id, b)
        self.finish(graph_id, b, session_b)
        receipt_b = json.dumps(self.graph_state(graph_id)["vertices"][
            b["work_id"]]["receipt"], sort_keys=True)

        end_prerequisite(graph_id, a)
        self.assertLedger()
        state = self.graph_state(graph_id)
        self.assertIn(state["vertices"][a["work_id"]]["state"],
                      ("failed", "cancelled"))
        self.assertEqual(cowork_graph.held_slots(state), [])
        self.assertEqual(self.ok("status", *g)["statuses"], {
            a["work_id"]: state["vertices"][a["work_id"]]["state"],
            b["work_id"]: "succeeded", c["work_id"]: "blocked"})

        refusal = self.refused("vertex_blocked", "claim", *g, "--work-id",
                               c["work_id"])
        self.assertEqual(refusal["rc"], 2)
        self.refused("none_ready", "claim", *g)
        self.assertEqual(
            [e for e in self.graph_state(graph_id)["events"]
             if e["op"] == "claim" and e["work_id"] == c["work_id"]], [])

        decision = self.ok("join", *g, "--join-id",
                           join["join_id"])["decision"]
        self.assertEqual(decision["outcome"], "blocked")
        self.assertNotEqual(decision["outcome"], "joined")
        states = {m["work_id"]: m["state"] for m in decision["members"]}
        self.assertEqual(states[b["work_id"]], "succeeded")
        self.assertEqual(states[c["work_id"]], "blocked")
        self.assertIn(states[a["work_id"]], ("failed", "cancelled"))
        # The independent sibling's receipt is untouched.
        self.assertEqual(
            json.dumps(self.graph_state(graph_id)["vertices"][
                b["work_id"]]["receipt"], sort_keys=True), receipt_b)
        self.assertLedger()
        return graph_id

    def test_failed_prerequisite_blocks_dependent_and_join(self):
        def fail(graph_id, a):
            line = self.ok("fail", "--graph-id", graph_id, "--work-id",
                           a["work_id"], "--reason-code", "child_failed")
            self.assertIs(line["slot_released"], True)

        self._blocked_by(fail)

    def test_cancelled_prerequisite_blocks_dependent_and_join(self):
        def cancel(graph_id, a):
            line = self.ok("cancel", "--graph-id", graph_id, "--work-id",
                           a["work_id"])
            self.assertEqual([o["outcome"] for o in line["outcomes"]],
                             ["cancelled"])

        self._blocked_by(cancel)

    def test_graph_cancel_is_two_phase_and_releases_the_slot_once(self):
        published = self.vertex()
        holder = self.vertex()
        dependent = self.vertex(preds=[holder["work_id"]])
        graph_id = self.admit([published, holder, dependent])
        g = ("--graph-id", graph_id)
        self.drain(graph_id, published)
        receipt = json.dumps(self.graph_state(graph_id)["vertices"][
            published["work_id"]]["receipt"], sort_keys=True)
        session, epoch = self.start(graph_id, holder)
        owner_id, owner_epoch = self.live_owner(session)

        line = self.ok("cancel", *g)
        outcomes = {o["work_id"]: o["outcome"] for o in line["outcomes"]}
        self.assertEqual(outcomes, {holder["work_id"]: "cancel_requested",
                                    dependent["work_id"]: "cancelled"})
        state = self.graph_state(graph_id)
        self.assertIs(state["cancelled"], True)
        self.assertEqual(self.held(graph_id), [(holder["work_id"], epoch)])
        self.assertIs(state["vertices"][holder["work_id"]][
            "cancel_requested"], True)
        self.refused("graph_cancelled", "claim", *g)
        self.refused("graph_cancelled", "admit", "--graph-id", graph_id,
                     "--revision-file", self.revision_file(self.revision(
                         [published, holder, dependent])))
        self.assertLedger()

        cowork_owner.release_owner_lease(session, owner_id, owner_epoch,
                                         "normal_exit")
        line = self.ok("cancel", *g)
        outcomes = {o["work_id"]: o["outcome"] for o in line["outcomes"]}
        self.assertEqual(outcomes, {holder["work_id"]: "cancelled",
                                    dependent["work_id"]:
                                    "already_cancelled"})
        state = self.graph_state(graph_id)
        self.assertEqual(self.held(graph_id), [])
        released = [s for s in state["slots"]
                    if s["work_id"] == holder["work_id"]]
        self.assertEqual([s["release_reason"] for s in released],
                         ["cancelled"])
        self.assertEqual(state["vertices"][holder["work_id"]]["lease_epoch"],
                         epoch + 1)
        self.assertEqual(json.dumps(state["vertices"][published["work_id"]][
            "receipt"], sort_keys=True), receipt)
        self.assertLedger()

        settled = self.graph_bytes(graph_id)
        line = self.ok("cancel", *g)
        self.assertEqual({o["outcome"] for o in line["outcomes"]},
                         {"already_cancelled"})
        self.assertEqual(self.graph_bytes(graph_id), settled)
        self.refused("graph_cancelled", "claim", *g)


# --------------------------------------------------------------------------- #
# Negative controls: every unsafe input fails closed.                         #
# --------------------------------------------------------------------------- #


class NegativeControlMatrixAcceptance(_AcceptanceEnvMixin, unittest.TestCase):

    def admit_refused(self, code, doc):
        return self.refused(code, "admit", "--new", "--revision-file",
                            self.revision_file(doc))

    def seeded(self):
        v = self.vertex()
        graph_id = self.admit([v])
        return graph_id, v

    def test_shared_roots_are_refused(self):
        seeded_graph, seeded_vertex = self.seeded()
        root = self.worktree()
        self.admit_refused("candidate_collision", self.revision(
            [self.vertex(root), self.vertex(root)]))

        link = os.path.join(self.parent, self.name("link"))
        os.symlink(self.worktree(), link)
        self.admit_refused("root_symlink", self.revision([self.vertex(link)]))

        outer = self.worktree()
        inner = self.worktree(inside=outer)
        self.admit_refused("root_nested", self.revision(
            [self.vertex(outer), self.vertex(inner)]))

        self.admit_refused("root_in_use", self.revision(
            [self.vertex(seeded_vertex["root"])]))
        self.assertEqual(self.registry_ids(), [seeded_graph])
        self.assertEqual(
            sorted(n for n in os.listdir(self.graphs())
                   if store._is_canonical_uuid(n)), [seeded_graph])
        # The same refusal when a revision grows an existing graph onto a
        # root another graph holds.
        other_graph, other_vertex = self.seeded()
        self.refused("root_in_use", "admit", "--graph-id", seeded_graph,
                     "--revision-file", self.revision_file(self.revision(
                         [seeded_vertex, self.vertex(
                             other_vertex["root"])])))
        self.assertEqual(self.registry_ids(),
                         sorted([seeded_graph, other_graph]))
        self.assertLedger()

    def test_case_alias_root_is_refused(self):
        base = os.path.basename(self.parent)
        swapped_parent = os.path.join(os.path.dirname(self.parent),
                                      base.swapcase())
        try:
            same = os.path.samefile(swapped_parent, self.parent)
        except OSError:
            same = False
        if not same:
            self.skipTest("filesystem is case-sensitive")
        root = self.worktree()
        alias = os.path.join(swapped_parent, os.path.basename(root))
        self.assertNotEqual(alias, root)
        before = self.snap()
        line = self.cli("admit", "--new", "--revision-file",
                        self.revision_file(self.revision(
                            [self.vertex(root), self.vertex(alias)])))
        self.assertEqual(line["outcome"], "refused")
        self.assertIn(line["reason"], ("root_alias",
                                       "root_not_worktree_toplevel"))
        self.assertEqual(line["rc"], cowork_graph.REASON_RC[line["reason"]])
        self.assertEqual(self.snap(), before)

    def test_structural_errors_write_nothing(self):
        a, b = _uuid(), _uuid()
        cases = [
            ("cycle", self.revision([
                self.vertex(preds=[b], work_id=a),
                self.vertex(preds=[a], work_id=b)])),
            ("self_edge", self.revision([self.vertex(preds=[a],
                                                     work_id=a)])),
            ("dangling_predecessor", self.revision(
                [self.vertex(preds=[_uuid()])])),
        ]
        for code, doc in cases:
            with self.subTest(code=code):
                self.use_sessions()
                self.admit_refused(code, doc)
                self.assertEqual(self.snap(), {})
                self.assertEqual(self.registry_ids(), [])

    def test_candidate_and_authority_collisions_are_refused(self):
        shared = self.authority()
        self.admit_refused("authority_shared", self.revision(
            [self.vertex(authority=shared), self.vertex(authority=shared)]))
        self.assertEqual(self.snap(), {})

        roots = [self.worktree(), self.worktree()]
        vertices = [self.vertex(r) for r in roots]
        graph_id = self.admit(vertices)
        first, _epoch = self.start(graph_id, vertices[0])
        shared_txn, _digest = self.txn_artifacts(first, roots[0])
        self.ok("publish", "--graph-id", graph_id, "--work-id",
                vertices[0]["work_id"])
        second, _epoch = self.start(graph_id, vertices[1])
        self.txn_artifacts(second, roots[1], transaction_id=shared_txn)
        owner_id, owner_epoch = self.live_owner(second)
        self.assertRefused("receipt_candidate_collision", store.publish,
                           graph_id, vertices[1]["work_id"], second,
                           owner_id, owner_epoch, now=NOW)
        cowork_owner.release_owner_lease(second, owner_id, owner_epoch,
                                         "normal_exit")
        self.assertLedger()

    def test_stale_and_foreign_receipts_are_refused(self):
        # A reclaimed session cannot publish into the vertex it lost.
        v = self.running()
        self.assertEqual(store.reclaim(v.graph_id, v.work_id, now=NOW)[
            "holder_verdict"], "unowned")
        successor, epoch = self.start(v.graph_id, v.vertex)
        self.assertEqual(epoch, v.epoch + 2)
        self.txn_artifacts(v.session, v.root)
        self.assertRefused("receipt_cross_vertex", store.publish, v.graph_id,
                           v.work_id, v.session, "owner-not-held", 1,
                           now=NOW)

        # A candidate edited after its receipt facts were recorded.
        self.txn_artifacts(successor, v.root)
        with open(os.path.join(v.root, "README.txt"), "a") as fh:
            fh.write("edited after the transaction\n")
        refusal = self.refused("receipt_candidate_changed", "publish",
                               "--graph-id", v.graph_id, "--work-id",
                               v.work_id)
        self.assertEqual(refusal["rc"], 2)
        self.assertEqual(self.held(v.graph_id), [(v.work_id, epoch)])

        # The kernel's epoch and revision fences, which the store cannot
        # reach because it always builds a receipt from the current claim.
        state = self.graph_state(v.graph_id)
        txn = store.gather_txn_facts(successor)
        owner_ok = True
        receipt = cowork_graph.build_receipt(state, v.work_id, successor,
                                             "owner", 1, txn, NOW)
        fingerprint = store.live_fingerprint(v.root)
        before = json.dumps(state, sort_keys=True)
        stale_epoch = dict(receipt, lease_epoch=epoch - 1)
        with self.assertRaises(cowork_graph.GraphRefusal) as caught:
            cowork_graph.validate_receipt(state, stale_epoch, txn,
                                          fingerprint, owner_ok)
        self.assertEqual(caught.exception.code, "receipt_stale_epoch")
        self.assertEqual(caught.exception.rc, 2)
        self.assertEqual(json.dumps(state, sort_keys=True), before)

        self.assertLedger()

    def test_receipt_revision_fence(self):
        v = self.vertex()
        graph_id = self.admit([v])
        newcomer = self.vertex()
        self.ok("admit", "--graph-id", graph_id, "--revision-file",
                self.revision_file(self.revision([v, newcomer])))
        session, _epoch = self.start(graph_id, v)
        self.txn_artifacts(session, v["root"])
        state = self.graph_state(graph_id)
        self.assertEqual(state["revision"], 2)
        txn = store.gather_txn_facts(session)
        receipt = cowork_graph.build_receipt(state, v["work_id"], session,
                                             "owner", 1, txn, NOW)
        self.assertEqual(receipt["graph_revision"], 2)
        before = json.dumps(state, sort_keys=True)
        stale = dict(receipt, graph_revision=1)
        with self.assertRaises(cowork_graph.GraphRefusal) as caught:
            cowork_graph.validate_receipt(
                state, stale, txn, store.live_fingerprint(v["root"]), True)
        self.assertEqual(caught.exception.code, "receipt_stale_revision")
        self.assertEqual(caught.exception.rc, 2)
        self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_cross_vertex_receipt_is_refused(self):
        self.cap(2)
        a, b = self.vertex(), self.vertex()
        graph_id = self.admit([a, b], max_parallel=2)
        session_a, _ea = self.start(graph_id, a)
        session_b, _eb = self.start(graph_id, b)
        self.txn_artifacts(session_a, a["root"])
        self.txn_artifacts(session_b, b["root"])
        owner_id, owner_epoch = self.live_owner(session_b)
        self.assertRefused("receipt_cross_vertex", store.publish, graph_id,
                           a["work_id"], session_b, owner_id, owner_epoch,
                           now=NOW)
        self.assertEqual(len(self.held(graph_id)), 2)
        cowork_owner.release_owner_lease(session_b, owner_id, owner_epoch,
                                         "normal_exit")
        self.assertLedger()

    def test_non_owner_publication_is_refused(self):
        v = self.running()
        self.txn_artifacts(v.session, v.root)
        owner_id, owner_epoch = self.live_owner(v.session)
        lease = cowork_owner.read_owner_lease(v.session)
        self.assertRefused("receipt_non_owner", store.publish, v.graph_id,
                           v.work_id, v.session, owner_id, owner_epoch + 1,
                           now=NOW)
        self.assertRefused("receipt_non_owner", store.publish, v.graph_id,
                           v.work_id, v.session, _uuid(), owner_epoch,
                           now=NOW)
        # The child's own owner lease is live: the CLI cannot publish.
        line = self.refused("owner_conflict", "publish", "--graph-id",
                            v.graph_id, "--work-id", v.work_id)
        self.assertEqual(line["rc"], 3)
        self.assertEqual(cowork_owner.read_owner_lease(v.session), lease)
        self.assertEqual(self.held(v.graph_id), [(v.work_id, v.epoch)])
        self.assertIsNone(self.graph_state(v.graph_id)["vertices"][
            v.work_id]["receipt"])
        cowork_owner.release_owner_lease(v.session, owner_id, owner_epoch,
                                         "normal_exit")

    def test_duplicate_claims_and_bindings_are_refused(self):
        a, b = self.vertex(), self.vertex()
        graph_id = self.admit([a, b])
        g = ("--graph-id", graph_id)
        self.ok("claim", *g, "--work-id", a["work_id"])
        self.refused("vertex_held", "claim", *g, "--work-id", a["work_id"])
        self.assertEqual(self.held(graph_id), [(a["work_id"], 1)])

        # Two processes claiming the same vertex: exactly one wins.
        c = self.vertex()
        race_graph = self.admit([c])
        results = _race(lambda: store.claim(race_graph, c["work_id"],
                                            now=NOW), [()] * 2)
        self.assertEqual(sorted(r[0] for r in results), ["ok", "refused"],
                         results)
        self.assertEqual([r[1] for r in results if r[0] == "refused"],
                         ["vertex_held"])
        self.assertEqual(self.held(race_graph), [(c["work_id"], 1)])
        self.assertLedger()

    def test_one_session_cannot_be_bound_to_two_vertices(self):
        self.cap(2)
        a, b = self.vertex(), self.vertex()
        graph_id = self.admit([a, b], max_parallel=2)
        epoch_a = store.claim(graph_id, a["work_id"], now=NOW)[
            "lease_epoch"]
        epoch_b = store.claim(graph_id, b["work_id"], now=NOW)[
            "lease_epoch"]
        session = _uuid()
        store.bind_session(graph_id, a["work_id"], epoch_a, session,
                           a["root"], STANDARD, now=NOW)
        index_path = cowork_state.graph_session_index_path_for(session)
        index_bytes = _read_bytes(index_path)
        exc = self.assertRefused("session_already_bound", store.bind_session,
                                 graph_id, b["work_id"], epoch_b, session,
                                 b["root"], STANDARD, now=NOW)
        self.assertEqual(exc.rc, 3)
        self.assertEqual(_read_bytes(index_path), index_bytes)
        self.assertEqual(self.graph_state(graph_id)["vertices"][
            b["work_id"]]["state"], "claimed")
        self.assertLedger()

    def test_early_join_is_refused_until_every_receipt_is_in(self):
        a, b = self.vertex(), self.vertex()
        join = self.join_decl([a, b])
        graph_id = self.admit([a, b], joins=[join])
        g = ("--graph-id", graph_id)
        j = ("--join-id", join["join_id"])
        refusal = self.refused("early_join", "join", *g, *j)
        self.assertEqual(refusal["rc"], 2)
        self.ok("claim", *g, "--work-id", a["work_id"])
        self.refused("early_join", "join", *g, *j)
        session = _uuid()
        store.bind_session(graph_id, a["work_id"], 1, session, a["root"],
                           STANDARD, now=NOW)
        self.refused("early_join", "join", *g, *j)
        self.assertEqual(len(self.held(graph_id)), 1)
        self.finish(graph_id, a, session)
        self.refused("early_join", "join", *g, *j)
        self.drain(graph_id, b)
        self.assertEqual(self.held(graph_id), [])
        decision = self.ok("join", *g, *j)["decision"]
        self.assertEqual(decision["outcome"], "joined")
        self.assertLedger()


# --------------------------------------------------------------------------- #
# Crash and restart.                                                          #
# --------------------------------------------------------------------------- #


class CrashRestartAcceptance(_AcceptanceEnvMixin, unittest.TestCase):

    def test_claim_without_launch_is_reclaimed_after_its_deadline(self):
        a, b = self.vertex(), self.vertex()
        graph_id = self.admit([a, b])
        store.claim(graph_id, a["work_id"], now=NOW)
        self.assertLedger()
        self.assertRefused("cap_reached", store.claim, graph_id, now=NOW)
        refusal = self.assertRefused("claim_not_expired", store.reclaim,
                                     graph_id, a["work_id"], now=NOW)
        self.assertEqual(refusal.rc, 3)
        self.assertEqual(store.reclaim(graph_id, a["work_id"], now=LATER),
                         {"new_lease_epoch": 2, "holder_verdict": None,
                          "slot_released": True})
        state = self.graph_state(graph_id)
        self.assertEqual(state["vertices"][a["work_id"]]["state"], "pending")
        self.assertEqual([s["release_reason"] for s in state["slots"]],
                         ["reclaim"])
        self.assertEqual(self.held(graph_id), [])
        self.assertLedger()
        self.assertRefused("vertex_lease_superseded", store.bind_session,
                           graph_id, a["work_id"], 1, _uuid(), a["root"],
                           STANDARD, now=LATER)
        self.assertEqual(store.claim(graph_id, a["work_id"],
                                     now=LATER)["lease_epoch"], 3)
        self.assertEqual(len(self.held(graph_id)), 1)
        self.assertLedger()

    def test_bind_with_lost_graph_write_completes_on_fence(self):
        v = self.vertex()
        graph_id = self.admit([v])
        epoch = store.claim(graph_id, v["work_id"], now=NOW)["lease_epoch"]
        session = _uuid()
        with _failing_graph_writes():
            self.assertRefusedCode("io_error", store.bind_session, graph_id,
                                   v["work_id"], epoch, session, v["root"],
                                   STANDARD, now=NOW)
        self.assertTrue(os.path.exists(
            cowork_state.graph_session_index_path_for(session)))
        record = self.graph_state(graph_id)["vertices"][v["work_id"]]
        self.assertEqual((record["state"], record["session_uuid"]),
                         ("claimed", None))
        self.assertEqual(store.fence(session, now=NOW), {
            "graph_id": graph_id, "work_id": v["work_id"],
            "lease_epoch": epoch, "session_uuid": session,
            "bind_completed": True})
        record = self.graph_state(graph_id)["vertices"][v["work_id"]]
        self.assertEqual((record["state"], record["session_uuid"]),
                         ("running", session))
        self.assertFalse(store.fence(session, now=NOW)["bind_completed"])
        self.assertRefused("vertex_held", store.bind_session, graph_id,
                           v["work_id"], epoch, _uuid(), v["root"], STANDARD,
                           now=NOW)
        self.assertLedger()

    def test_publish_with_lost_graph_write_retries_exactly_once(self):
        v = self.running()
        self.txn_artifacts(v.session, v.root)
        before = self.graph_bytes(v.graph_id)
        h = ("--graph-id", v.graph_id, "--work-id", v.work_id)
        with _failing_graph_writes():
            refusal = self.refused("io_error", "publish", *h)
        self.assertEqual(refusal["rc"], 1)
        self.assertEqual(self.graph_bytes(v.graph_id), before)
        self.assertEqual(self.held(v.graph_id), [(v.work_id, v.epoch)])
        self.assert_unowned(v.session)
        self.assertLedger()

        first = self.ok("publish", *h)
        self.assertEqual(first["publish_outcome"], "published")
        settled = self.snap()
        again = self.ok("publish", *h)
        self.assertEqual(again["publish_outcome"], "already_published")
        self.assertEqual(again["receipt_identity"], first["receipt_identity"])
        self.assertEqual(self.snap(), settled)
        state = self.graph_state(v.graph_id)
        self.assertEqual([s["release_reason"] for s in state["slots"]],
                         ["terminal_receipt"])
        self.assertEqual(len([e for e in state["events"]
                              if e["op"] == "publish"]), 1)
        self.assertLedger()

    def test_abandoned_owner_lease_needs_proof_to_take_over(self):
        self.cap(3)
        vertices = [self.vertex() for _ in range(3)]
        graph_id = self.admit(vertices, max_parallel=3)
        sessions = [self.start(graph_id, v)[0] for v in vertices]
        for v, session in zip(vertices, sessions):
            self.txn_artifacts(session, v["root"])
        live, unproven, dead = vertices

        def h(v):
            return ("--graph-id", graph_id, "--work-id", v["work_id"])

        owner_id, owner_epoch = self.live_owner(sessions[0])
        self.refused("owner_conflict", "publish", *h(live), "--take-over")
        cowork_owner.release_owner_lease(sessions[0], owner_id, owner_epoch,
                                         "normal_exit")

        self.expired_owner(sessions[1], "unavailable")
        self.refused("owner_conflict", "publish", *h(unproven), "--take-over")
        self.refused("owner_conflict", "publish", *h(unproven))

        self.expired_owner(sessions[2], "ps_lstart")
        self.refused("owner_conflict", "publish", *h(dead))
        line = self.ok("publish", *h(dead), "--take-over")
        self.assertEqual(line["publish_outcome"], "published")
        self.assert_unowned(sessions[2])
        state = self.graph_state(graph_id)
        self.assertEqual([w for w, r in state["vertices"].items()
                          if r["state"] == "succeeded"],
                         [dead["work_id"]])
        self.assertEqual(len(self.held(graph_id)), 2)
        self.assertLedger()

    def test_cancel_with_crashed_pause_cleanup_converges(self):
        v = self.running()
        lease_id = self.pause_lease(v.session)

        def child():
            with mock.patch.object(cowork_capacity_scheduler, "cancel",
                                   side_effect=lambda *a, **k: os._exit(9)):
                store.cancel(v.graph_id, v.work_id, now=NOW)
            os._exit(0)

        self.assertEqual(_run_child(child), 9)
        state = self.graph_state(v.graph_id)
        self.assertEqual(state["vertices"][v.work_id]["state"], "cancelled")
        self.assertEqual(self.held(v.graph_id), [])
        self.assertEqual([s["release_reason"] for s in state["slots"]],
                         ["cancelled"])
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session),
                         [lease_id])
        self.assertLedger()

        before = self.graph_bytes(v.graph_id)
        line = self.ok("cancel", "--graph-id", v.graph_id, "--work-id",
                       v.work_id)
        self.assertEqual(line["outcomes"], [{
            "work_id": v.work_id, "outcome": "already_cancelled",
            "pause_cleanup": [{"lease_id": lease_id, "result": "cancelled"}]}])
        self.assertEqual(self.graph_bytes(v.graph_id), before)
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session), [])
        self.assertLedger()

    def test_plain_resume_continues_the_same_vertex(self):
        v = self.bound_run()
        self.assertEqual(len(self.scout_calls), 1)
        rc, box = self.run_flow(["--session-file", v.spath],
                                scout=self.fake_scout())
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.scout_calls), 2)
        self.assertEqual(box["session_uuid"], v.session)
        self.assertEqual(box["graph_vertex"], v.box["graph_vertex"])
        record = self.graph_state(v.graph_id)["vertices"][v.work_id]
        self.assertEqual((record["state"], record["session_uuid"],
                          record["lease_epoch"]),
                         ("running", v.session, v.epoch))
        self.assertEqual(self.held(v.graph_id), [(v.work_id, v.epoch)])
        self.assertEqual(store.vertex_binding_for_session(v.session)[
            "lease_epoch"], v.epoch)
        self.assert_unowned(v.session)
        self.assertLedger()

    def test_reclaimed_session_is_refused_without_dispatch(self):
        v = self.bound_run()
        self.assertEqual(store.reclaim(v.graph_id, v.work_id, now=NOW)[
            "holder_verdict"], "unowned")
        settled = self.snap()
        rc, box = self.run_flow(["--session-file", v.spath])
        self.assertEqual((rc, box["reason"]), (3, "vertex_lease_superseded"))
        self.assertEqual(rc, cowork_graph.REASON_RC[box["reason"]])
        self.assertEqual(len(self.scout_calls), 1)
        self.assertNotIn("graph_vertex", cowork.build_run_result(rc, box))
        self.assertEqual(self.snap(), settled)
        self.assert_unowned(v.session)
        self.assertLedger()


# --------------------------------------------------------------------------- #
# Pause, cancel and recovery keep bindings and slots.                         #
# --------------------------------------------------------------------------- #


class CapacityBindingAcceptance(_AcceptanceEnvMixin, unittest.TestCase):

    def trigger(self, pause, sent):
        out = []
        rc = cowork.run_resume_trigger([
            "--session-uuid", pause.session, "--role", "builder",
            "--lease-id", pause.lease_id, "--claimant-ref", "wake-1",
            "--automation-ref", pause.automation_ref, "--now", WAKE_NOW,
            "--cwd", pause.root], output=out.append,
            session_factory=lambda *a, **k: _FakeResumeSession(sent))
        return rc, out

    def test_pause_keeps_slot_and_binding_and_resume_continues(self):
        v = self.claimed()
        session = _uuid()
        store.bind_session(v.graph_id, v.work_id, v.epoch, session, v.root,
                           STANDARD, now=NOW)
        index_path = cowork_state.graph_session_index_path_for(session)
        authority = _read_bytes(v.vertex["authority_path"])
        before = (self.graph_bytes(v.graph_id), _read_bytes(index_path))
        revisions = self.graph_state(v.graph_id)["revisions"]

        pause = self.set_up_pause(v.root, session)
        self.assertTrue(cowork_state.live_pause_lease_ids(session))
        self.assertEqual((self.graph_bytes(v.graph_id),
                          _read_bytes(index_path)), before)
        self.assertEqual(self.held(v.graph_id), [(v.work_id, v.epoch)])
        self.assertEqual(self.ok("status", "--graph-id", v.graph_id)[
            "statuses"][v.work_id], "running")
        self.assertEqual(self.graph_state(v.graph_id)["revisions"],
                         revisions)
        self.assertEqual(_read_bytes(v.vertex["authority_path"]), authority)

        # An abandoned-looking claim cannot be reclaimed while paused.
        refusal = self.refused("vertex_paused", "reclaim", "--graph-id",
                               v.graph_id, "--work-id", v.work_id)
        self.assertEqual(refusal["rc"], 3)
        self.refused("vertex_paused", "fail", "--graph-id", v.graph_id,
                     "--work-id", v.work_id, "--reason-code", "child_failed")

        sent = []
        rc, _out = self.trigger(pause, sent)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
        self.assertEqual(sent, [TURN_TEXT])
        record = self.graph_state(v.graph_id)["vertices"][v.work_id]
        self.assertEqual((record["state"], record["session_uuid"],
                          record["lease_epoch"]),
                         ("running", session, v.epoch))
        self.assertEqual(self.held(v.graph_id), [(v.work_id, v.epoch)])
        self.assertEqual(store.vertex_binding_for_session(session)[
            "lease_epoch"], v.epoch)
        self.assert_unowned(session)
        self.assertLedger()

    def test_cancel_of_a_paused_vertex_cleans_the_lease_once(self):
        self.cap(2)
        done, paused = self.vertex(), self.vertex()
        graph_id = self.admit([done, paused], max_parallel=2)
        g = ("--graph-id", graph_id)
        self.drain(graph_id, done)
        receipt = json.dumps(self.graph_state(graph_id)["vertices"][
            done["work_id"]]["receipt"], sort_keys=True)
        session, epoch = self.start(graph_id, paused)
        lease_id = self.pause_lease(session,
                                    automation_ref="auto-graph-stored")
        revisions = self.graph_state(graph_id)["revisions"]
        authority = _read_bytes(paused["authority_path"])
        self.assertEqual(self.held(graph_id), [(paused["work_id"], epoch)])

        with mock.patch.object(cowork_capacity_scheduler, "cancel",
                               wraps=cowork_capacity_scheduler.cancel) as spy:
            line = self.ok("cancel", *g, "--work-id", paused["work_id"])
        spy.assert_called_once_with(session, lease_id, "auto-graph-stored")
        self.assertEqual(line["outcomes"], [{
            "work_id": paused["work_id"], "outcome": "cancelled",
            "pause_cleanup": [{"lease_id": lease_id, "result": "cancelled"}]}])
        state = self.graph_state(graph_id)
        self.assertEqual(self.held(graph_id), [])
        self.assertEqual([s["release_reason"] for s in state["slots"]
                          if s["work_id"] == paused["work_id"]],
                         ["cancelled"])
        self.assertEqual(state["revisions"], revisions)
        self.assertEqual(_read_bytes(paused["authority_path"]), authority)
        self.assertEqual(json.dumps(state["vertices"][done["work_id"]][
            "receipt"], sort_keys=True), receipt)
        self.assertEqual(cowork_state.live_pause_lease_ids(session), [])
        self.assertLedger()

        settled = self.graph_bytes(graph_id)
        again = self.ok("cancel", *g, "--work-id", paused["work_id"])
        self.assertEqual([o["outcome"] for o in again["outcomes"]],
                         ["already_cancelled"])
        self.assertEqual(again["outcomes"][0]["pause_cleanup"], [])
        self.assertEqual(self.graph_bytes(graph_id), settled)
        self.assertLedger()


if __name__ == "__main__":
    unittest.main()
