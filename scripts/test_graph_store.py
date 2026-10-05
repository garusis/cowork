#!/usr/bin/env python3
"""Tests for cowork_graph_store: the durable governed-graph store.

Every fixture is neutral and built at run time: a temporary parent directory
holding a throwaway git repository, its linked worktrees, authority documents
and a private COWORK_SESSIONS_ROOT. Commits come from `git rev-parse HEAD` of
those repositories, ids are fresh UUIDs and graph clocks are injected `now`
strings. Owner-lease fixtures use the real clock, because lease deadlines are
wall-clock instants. Cross-process races use fork-context multiprocessing.

Run: python3 scripts/cowork_offline_tests.py test_graph_store
"""

import datetime
import fcntl
import hashlib
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

import cowork_capacity_scheduler  # noqa: E402
import cowork_execution_profiles as profiles  # noqa: E402
import cowork_graph  # noqa: E402
import cowork_graph_store as store  # noqa: E402
import cowork_owner  # noqa: E402
import cowork_state  # noqa: E402

NOW = "2030-01-01T00:00:00Z"
LATER = "2030-01-01T01:00:00Z"
STANDARD = "standard"
IGNORE_RULE = ".cowork/\n"
FABRICATED_PID_START = "2020-01-01T00:00:00Z"
RACE_TIMEOUT_S = 120
SUITE_LABEL = "graph store suite"
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


def _other_commit(commit):
    """A different well-formed commit id derived from a real one."""
    return ("1" if commit[0] == "0" else "0") + commit[1:]


def _dead_pid():
    """The pid of a real child that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


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


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


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


# --------------------------------------------------------------------------- #
# Shared fixture mixin.                                                       #
# --------------------------------------------------------------------------- #


class _StoreEnvMixin(object):
    """A temp parent with a main repository (ignoring `.cowork/`), worktree,
    authority and sessions-root factories, and graph-store assertions."""

    def setUp(self):
        super().setUp()
        self.parent = os.path.realpath(tempfile.mkdtemp(prefix="gs-"))
        self.addCleanup(shutil.rmtree, self.parent, True)
        self.addCleanup(self._restore_sessions_root,
                        os.environ.get("COWORK_SESSIONS_ROOT"))
        self._counter = itertools.count(1)
        self.use_sessions()
        self.main = self.make_repo("main")
        self.head = _git(["rev-parse", "HEAD"], self.main)

    def _restore_sessions_root(self, old):
        if old is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = old

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
        return path

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

    def vertex(self, root, preds=(), profile=STANDARD, work_id=None,
               authority=None, base_commit=None):
        path, digest = authority or self.authority()
        return {"work_id": work_id or _uuid(), "root": root,
                "base_commit": base_commit or self.head,
                "authority_path": path, "authority_digest": digest,
                "profile": profile, "predecessors": list(preds)}

    def revision(self, vertices, max_parallel=1, joins=None):
        doc = {"schema_version": 1, "max_parallel": max_parallel,
               "vertices": [dict(v) for v in vertices]}
        if joins is not None:
            doc["joins"] = joins
        return doc

    def admit_new(self, vertices, max_parallel=1, joins=None):
        return store.admit(self.revision(vertices, max_parallel, joins),
                           now=NOW)["graph_id"]

    def prime_lock(self, graph_id):
        """Create graph.json.lock with a no-op locked read, so a later locked
        refusal can be compared byte-for-byte including lock files."""
        cowork_state._locked_json_transaction(
            cowork_state.graph_state_path_for(graph_id), lambda existing: None)

    def seed(self, root=None):
        """A registered graph with one pending vertex and a primed lock."""
        root = root or self.worktree()
        vertex = self.vertex(root)
        graph_id = self.admit_new([vertex])
        self.prime_lock(graph_id)
        return types.SimpleNamespace(graph_id=graph_id, vertex=vertex,
                                     work_id=vertex["work_id"], root=root)

    def run_vertex(self, graph_id, work_id, root, profile=STANDARD):
        info = store.claim(graph_id, work_id, now=NOW)
        session = _uuid()
        store.bind_session(graph_id, work_id, info["lease_epoch"], session,
                           root, profile, now=NOW)
        return session, info["lease_epoch"]

    def running(self):
        """A graph whose single vertex is bound to a fresh session."""
        root = self.worktree()
        vertex = self.vertex(root)
        graph_id = self.admit_new([vertex])
        session, epoch = self.run_vertex(graph_id, vertex["work_id"], root)
        return types.SimpleNamespace(graph_id=graph_id,
                                     work_id=vertex["work_id"], root=root,
                                     session=session, epoch=epoch,
                                     vertex=vertex)

    def live_owner(self, session):
        claimant = cowork_owner.owner_identity(session, "run_flow",
                                               self.parent, None)
        lease = cowork_owner.acquire_owner_lease(session, claimant)
        return lease["owner_id"], lease["epoch"]

    def expired_owner(self, session, pid_start_source):
        """A lease past its deadline naming an exited pid on this host."""
        claimant = cowork_owner.owner_identity(session, "run_flow",
                                               self.parent, None)
        claimant.update(pid=_dead_pid(), pid_start_at=FABRICATED_PID_START,
                        pid_start_source=pid_start_source)
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=1))
        cowork_owner.acquire_owner_lease(session, claimant, now=past)

    def corrupt_owner(self, session):
        _write_bytes(os.path.join(cowork_state.owner_dir_for(session),
                                  "lease.json"), b"{not json")

    def pause_lease(self, session, automation_ref="auto-graph",
                    state="unclaimed"):
        lease_id = _uuid()
        lease = {
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
        }
        cowork_state.create_pause_lease(session, lease)
        if state == "claimed":
            cowork_state.claim_pause_lease(session, lease_id, "claimant-graph")
        elif state == "cancelled":
            cowork_state.cancel_pause_lease(session, lease_id)
        return lease_id

    def corrupt_pause_lease(self, session):
        _write_bytes(cowork_state.pause_lease_path_for(session, _uuid()),
                     b"{torn")

    def txn_artifacts(self, session, root, transaction_id=None,
                      request_session=None, request_repo=None,
                      result_digest=None, verdict="green",
                      binding="components_ran_once", disposition="accepted",
                      request=True, result=True, pointer=True):
        """Owned-verification artifacts for `session` at the cowork_state
        paths, fingerprinting `root` as it is now."""
        txn = transaction_id or "txn-" + uuid.uuid4().hex
        digest = store.live_fingerprint(root)
        self.assertIsNotNone(digest)
        if request:
            cowork_state.write_json_atomic(
                cowork_state.verification_request_path_for(session, txn),
                {"transaction_id": txn,
                 "session_uuid": request_session or session,
                 "repo": request_repo or root,
                 "snapshot": {"manifest_digest": digest},
                 "inventory": [{"label": SUITE_LABEL}]})
        if result:
            cowork_state.write_json_atomic(
                cowork_state.verification_result_path_for(session, txn),
                {"transaction_id": txn, "verdict": verdict,
                 "final_suite_binding": binding,
                 "snapshot": {"manifest_digest": result_digest or digest}})
        if pointer:
            cowork_state.write_current_receipt_pointer(
                session, {"transaction_id": txn})
        if disposition is not None:
            cowork_state.write_verification_disposition(
                session, {"transaction_id": txn, "disposition": disposition})
        return txn, digest

    # -- reads -------------------------------------------------------------- #

    def graphs(self):
        return cowork_state.graphs_root()

    def snap(self):
        return _snapshot(cowork_state.graphs_root())

    def graph_bytes(self, graph_id):
        return _read_bytes(cowork_state.graph_state_path_for(graph_id))

    def graph_state(self, graph_id):
        return json.loads(self.graph_bytes(graph_id).decode("utf-8"))

    def registry_ids(self):
        record = cowork_state._read_json_or_raise_if_corrupt(
            cowork_state.graph_registry_path())
        return [] if record is None else record["graph_ids"]

    def graph_dirs(self):
        if not os.path.isdir(self.graphs()):
            return []
        return sorted(n for n in os.listdir(self.graphs())
                      if store._is_canonical_uuid(n))

    # -- assertions --------------------------------------------------------- #

    def assertRefused(self, code, fn, *args, **kwargs):
        with self.assertRaises(cowork_graph.GraphRefusal) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(caught.exception.rc, cowork_graph.REASON_RC[code])
        return caught.exception

    def assertRefusedUnchanged(self, code, fn, *args, **kwargs):
        before = self.snap()
        refusal = self.assertRefused(code, fn, *args, **kwargs)
        self.assertEqual(self.snap(), before)
        return refusal

    def assertStoreConsistent(self):
        """Unreleased slots equal holding vertices within the cap in every
        registered graph, and every session is bound to at most one vertex,
        matching its session-index record."""
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
            for name in os.listdir(index_dir):
                record = store.vertex_binding_for_session(
                    name[:-len(".json")])
                self.assertIn(record["graph_id"], registered)


# --------------------------------------------------------------------------- #
# Admission.                                                                  #
# --------------------------------------------------------------------------- #


class StoreAdmissionTests(_StoreEnvMixin, unittest.TestCase):

    def test_admit_new_graph_writes_one_revision_and_registry(self):
        roots = [self.worktree(), self.worktree()]
        vertices = [self.vertex(r) for r in roots]
        result = store.admit(self.revision(vertices), now=NOW)
        graph_id = result["graph_id"]
        self.assertEqual(str(uuid.UUID(graph_id)), graph_id)
        self.assertEqual(uuid.UUID(graph_id).version, 4)
        self.assertEqual(result, {"graph_id": graph_id, "graph_revision": 1,
                                  "effective_cap": 1})
        state = self.graph_state(graph_id)
        self.assertEqual(len(state["revisions"]), 1)
        declared = {v["work_id"]: v for v in state["revisions"][0]["vertices"]}
        for vertex, root in zip(vertices, roots):
            st = os.stat(root)
            entry = declared[vertex["work_id"]]
            self.assertEqual(
                (entry["root_realpath"], entry["root_dev"], entry["root_ino"]),
                (root, st.st_dev, st.st_ino))
        self.assertEqual(
            json.loads(_read_bytes(cowork_state.graph_registry_path())),
            {"schema_version": 1, "graph_ids": [graph_id]})
        files = sorted(rel for rel, data in self.snap().items()
                       if isinstance(data, bytes))
        self.assertEqual(files, sorted([
            "registry.json", "registry.json.lock",
            os.path.join(graph_id, "graph.json")]))

    # Each case builds on a fresh store seeded with one admitted graph and
    # returns the refused attempt.

    def _case_revision_malformed(self, seeded):
        return lambda: store.admit({"schema_version": 1, "vertices": []},
                                   now=NOW)

    def _unused(self):
        return os.path.join(self.parent, self.name("unused-"))

    def _case_join_malformed(self, seeded):
        vertex = self.vertex(self._unused())
        doc = self.revision([vertex], joins=[{
            "join_id": _uuid(), "rule": "any_succeeded",
            "requires": [vertex["work_id"]]}])
        return lambda: store.admit(doc, now=NOW)

    def _case_cycle(self, seeded):
        a, b = _uuid(), _uuid()
        doc = self.revision([
            self.vertex(self._unused(), preds=[b], work_id=a),
            self.vertex(self._unused(), preds=[a], work_id=b)])
        return lambda: store.admit(doc, now=NOW)

    def _case_self_edge(self, seeded):
        a = _uuid()
        doc = self.revision([self.vertex(self._unused(), preds=[a],
                                         work_id=a)])
        return lambda: store.admit(doc, now=NOW)

    def _case_dangling_predecessor(self, seeded):
        doc = self.revision([self.vertex(self._unused(), preds=[_uuid()])])
        return lambda: store.admit(doc, now=NOW)

    def _case_duplicate_work_id(self, seeded):
        a = _uuid()
        doc = self.revision([self.vertex(self._unused(), work_id=a),
                             self.vertex(self._unused(), work_id=a)])
        return lambda: store.admit(doc, now=NOW)

    def _case_join_unknown_member(self, seeded):
        vertex = self.vertex(self._unused())
        doc = self.revision([vertex], joins=[{
            "join_id": _uuid(), "rule": "all_succeeded",
            "requires": [vertex["work_id"], _uuid()]}])
        return lambda: store.admit(doc, now=NOW)

    def _case_root_missing(self, seeded):
        doc = self.revision([self.vertex(self._unused())])
        return lambda: store.admit(doc, now=NOW)

    def _case_root_symlink(self, seeded):
        link = os.path.join(self.parent, self.name("link"))
        os.symlink(self.worktree(), link)
        doc = self.revision([self.vertex(link)])
        return lambda: store.admit(doc, now=NOW)

    def _case_root_not_worktree_toplevel(self, seeded):
        sub = os.path.join(self.worktree(), "sub")
        os.mkdir(sub)
        doc = self.revision([self.vertex(sub)])
        return lambda: store.admit(doc, now=NOW)

    def _case_base_commit_mismatch(self, seeded):
        doc = self.revision([self.vertex(
            self.worktree(), base_commit=_other_commit(self.head))])
        return lambda: store.admit(doc, now=NOW)

    def _case_anchor_dir_not_ignored(self, seeded):
        repo = self.make_repo(self.name("plain"), ignore=False)
        doc = self.revision([self.vertex(
            self.worktree(repo=repo),
            base_commit=_git(["rev-parse", "HEAD"], repo))])
        return lambda: store.admit(doc, now=NOW)

    def _case_candidate_collision(self, seeded):
        root = self.worktree()
        doc = self.revision([self.vertex(root), self.vertex(root)])
        return lambda: store.admit(doc, now=NOW)

    def _case_root_nested(self, seeded):
        outer = self.worktree()
        inner = self.worktree(inside=outer)
        doc = self.revision([self.vertex(outer), self.vertex(inner)])
        return lambda: store.admit(doc, now=NOW)

    def _case_root_overlaps_main_checkout(self, seeded):
        doc = self.revision([self.vertex(self.main)])
        return lambda: store.admit(doc, now=NOW)

    def _case_root_overlaps_sessions_root(self, seeded):
        root = self.worktree()
        self.use_sessions(os.path.join(root, ".cowork", "sessions"))
        self.seed()
        doc = self.revision([self.vertex(root)])
        return lambda: store.admit(doc, now=NOW)

    def _case_root_in_use(self, seeded):
        doc = self.revision([self.vertex(seeded.root)])
        return lambda: store.admit(doc, now=NOW)

    def _case_authority_missing(self, seeded):
        doc = self.revision([self.vertex(self.worktree(), authority=(
            os.path.join(self.parent, "absent-authority.json"),
            _hex64("absent")))])
        return lambda: store.admit(doc, now=NOW)

    def _case_authority_malformed(self, seeded):
        path, _digest = self.authority()
        doc = self.revision([self.vertex(self.worktree(),
                                         authority=(path, "not-hex"))])
        return lambda: store.admit(doc, now=NOW)

    def _case_authority_digest_mismatch(self, seeded):
        path, _digest = self.authority()
        doc = self.revision([self.vertex(
            self.worktree(), authority=(path, _hex64("other bytes")))])
        return lambda: store.admit(doc, now=NOW)

    def _case_authority_shared(self, seeded):
        shared = self.authority()
        doc = self.revision([self.vertex(self.worktree(), authority=shared),
                             self.vertex(self.worktree(), authority=shared)])
        return lambda: store.admit(doc, now=NOW)

    def _case_unknown_profile(self, seeded):
        doc = self.revision([self.vertex(self.worktree(),
                                         profile="no-such-profile")])
        return lambda: store.admit(doc, now=NOW)

    def _case_ceiling_invalid(self, seeded):
        doc = self.revision([self.vertex(self.worktree())], max_parallel=0)
        return lambda: store.admit(doc, now=NOW)

    def _case_ceiling_above_policy(self, seeded):
        doc = self.revision([self.vertex(self.worktree())], max_parallel=2)
        return lambda: store.admit(doc, now=NOW)

    def _case_revision_conflict(self, seeded):
        doc = self.revision([self.vertex(self.worktree(),
                                         work_id=seeded.work_id)])
        return lambda: store.admit(doc, graph_id=seeded.graph_id, now=NOW)

    def _case_graph_cancelled(self, seeded):
        store.cancel(seeded.graph_id, now=NOW)
        doc = self.revision([self.vertex(self.worktree())])
        return lambda: store.admit(doc, graph_id=seeded.graph_id, now=NOW)

    def _case_graph_unknown_unregistered(self, seeded):
        doc = self.revision([self.vertex(self.worktree())])
        return lambda: store.admit(doc, graph_id=_uuid(), now=NOW)

    def _case_graph_unknown_orphan(self, seeded):
        orphan = _uuid()
        self.assertTrue(cowork_state.write_json_atomic_durable(
            cowork_state.graph_state_path_for(orphan),
            cowork_graph.new_state(orphan)))
        doc = self.revision([self.vertex(self.worktree())])
        return lambda: store.admit(doc, graph_id=orphan, now=NOW)

    def _case_argument_error_graph_id(self, seeded):
        doc = self.revision([self.vertex(self.worktree())])
        noncanonical = "{%s}" % seeded.graph_id
        return lambda: store.admit(doc, graph_id=noncanonical, now=NOW)

    def _case_argument_error_now(self, seeded):
        doc = self.revision([self.vertex(self.worktree())])
        return lambda: store.admit(doc, now="not-a-time")

    def _case_graph_state_corrupt_not_json(self, seeded):
        _write_bytes(cowork_state.graph_state_path_for(seeded.graph_id),
                     b"{torn")
        doc = self.revision([self.vertex(self.worktree())])
        return lambda: store.admit(doc, now=NOW)

    def _case_graph_state_corrupt_not_graph_state(self, seeded):
        _write_bytes(cowork_state.graph_state_path_for(seeded.graph_id),
                     json.dumps({"graph_id": seeded.graph_id,
                                 "record": "Other"}).encode("utf-8"))
        doc = self.revision([self.vertex(self.worktree())])
        return lambda: store.admit(doc, now=NOW)

    def test_each_refusal_leaves_store_byte_identical(self):
        # root_alias is reachable on a real filesystem only through a case
        # alias (test_case_alias_refused_on_case_insensitive_fs) and
        # unsupported_concurrency_contract only through a policy edit, so
        # both are covered by the kernel's own tests.
        cases = [
            ("revision_malformed", self._case_revision_malformed),
            ("join_malformed", self._case_join_malformed),
            ("cycle", self._case_cycle),
            ("self_edge", self._case_self_edge),
            ("dangling_predecessor", self._case_dangling_predecessor),
            ("duplicate_work_id", self._case_duplicate_work_id),
            ("join_unknown_member", self._case_join_unknown_member),
            ("root_missing", self._case_root_missing),
            ("root_symlink", self._case_root_symlink),
            ("root_not_worktree_toplevel",
             self._case_root_not_worktree_toplevel),
            ("base_commit_mismatch", self._case_base_commit_mismatch),
            ("anchor_dir_not_ignored", self._case_anchor_dir_not_ignored),
            ("candidate_collision", self._case_candidate_collision),
            ("root_nested", self._case_root_nested),
            ("root_overlaps_main_checkout",
             self._case_root_overlaps_main_checkout),
            ("root_overlaps_sessions_root",
             self._case_root_overlaps_sessions_root),
            ("root_in_use", self._case_root_in_use),
            ("authority_missing", self._case_authority_missing),
            ("authority_malformed", self._case_authority_malformed),
            ("authority_digest_mismatch",
             self._case_authority_digest_mismatch),
            ("authority_shared", self._case_authority_shared),
            ("unknown_profile", self._case_unknown_profile),
            ("ceiling_invalid", self._case_ceiling_invalid),
            ("ceiling_above_policy", self._case_ceiling_above_policy),
            ("revision_conflict", self._case_revision_conflict),
            ("graph_cancelled", self._case_graph_cancelled),
            ("graph_unknown", self._case_graph_unknown_unregistered),
            ("graph_unknown", self._case_graph_unknown_orphan),
            ("argument_error", self._case_argument_error_graph_id),
            ("argument_error", self._case_argument_error_now),
            ("graph_state_corrupt", self._case_graph_state_corrupt_not_json),
            ("graph_state_corrupt",
             self._case_graph_state_corrupt_not_graph_state),
        ]
        for code, build in cases:
            with self.subTest(code=code, case=build.__name__):
                self.use_sessions()
                attempt = build(self.seed())
                self.assertRefusedUnchanged(code, attempt)

    def test_refused_new_graph_creates_no_directory(self):
        graphs = self.graphs()
        malformed = {"schema_version": 1, "vertices": []}
        self.assertRefused("revision_malformed", store.admit, malformed,
                           now=NOW)
        self.assertFalse(os.path.exists(graphs))
        self.assertRefused(
            "root_missing", store.admit,
            self.revision([self.vertex(self._unused())]), now=NOW)
        # Only the registry lock may appear; no graph state at all.
        self.assertEqual(os.listdir(graphs), ["registry.json.lock"])

        seeded = self.seed()
        registry = _read_bytes(cowork_state.graph_registry_path())
        for code, doc in (
                ("revision_malformed", malformed),
                ("root_missing",
                 self.revision([self.vertex(self._unused())])),
                ("root_in_use", self.revision([self.vertex(seeded.root)]))):
            with self.subTest(code=code):
                before = self.snap()
                self.assertRefused(code, store.admit, doc, now=NOW)
                self.assertEqual(self.graph_dirs(), [seeded.graph_id])
                self.assertEqual(
                    _read_bytes(cowork_state.graph_registry_path()), registry)
                self.assertEqual(self.snap(), before)

    def test_concurrent_admits_into_different_graphs_cannot_share_a_root(self):
        root = self.worktree()
        docs = [self.revision([self.vertex(root)]) for _ in range(2)]
        results = _race(lambda doc: store.admit(doc, now=NOW),
                        [(doc,) for doc in docs])
        self.assertEqual(sorted(r[0] for r in results), ["ok", "refused"],
                         results)
        self.assertEqual([r[1] for r in results if r[0] == "refused"],
                         ["root_in_use"])
        winner = [r[1]["graph_id"] for r in results if r[0] == "ok"][0]
        self.assertEqual(self.registry_ids(), [winner])
        self.assertEqual(self.graph_dirs(), [winner])

        existing = self.seed()
        contested = self.worktree()
        grow = self.revision([existing.vertex, self.vertex(contested)])
        fresh = self.revision([self.vertex(contested)])
        results = _race(
            lambda graph_id, doc: store.admit(doc, graph_id=graph_id, now=NOW),
            [(existing.graph_id, grow), (None, fresh)])
        self.assertEqual(sorted(r[0] for r in results), ["ok", "refused"],
                         results)
        self.assertEqual([r[1] for r in results if r[0] == "refused"],
                         ["root_in_use"])
        holders = []
        for graph_id in self.registry_ids():
            state = self.graph_state(graph_id)
            current = state["revisions"][state["revision"] - 1]
            if any(v["root"] == contested for v in current["vertices"]):
                holders.append(graph_id)
        self.assertEqual(len(holders), 1)

    def test_real_symlink_alias_refused(self):
        self.seed()
        link = os.path.join(self.parent, self.name("link"))
        os.symlink(self.worktree(), link)
        self.assertRefusedUnchanged("root_symlink", store.admit,
                                    self.revision([self.vertex(link)]),
                                    now=NOW)

    def test_real_nested_worktree_refused(self):
        self.seed()
        outer = self.worktree()
        inner = self.worktree(inside=outer)
        self.assertRefusedUnchanged(
            "root_nested", store.admit,
            self.revision([self.vertex(outer), self.vertex(inner)]), now=NOW)
        self.admit_new([self.vertex(outer)])
        self.assertRefusedUnchanged(
            "root_in_use", store.admit, self.revision([self.vertex(inner)]),
            now=NOW)

    def test_case_alias_refused_on_case_insensitive_fs(self):
        base = os.path.basename(self.parent)
        swapped_parent = os.path.join(os.path.dirname(self.parent),
                                      base.swapcase())
        try:
            same = os.path.samefile(swapped_parent, self.parent)
        except OSError:
            same = False
        if not same:
            self.skipTest("filesystem is case-sensitive")
        self.seed()
        root = self.worktree()
        alias = os.path.join(swapped_parent, os.path.basename(root))
        self.assertNotEqual(alias, root)
        canonical_probe = store.probe_root(root)
        alias_probe = store.probe_root(alias)
        self.assertEqual((alias_probe["dev"], alias_probe["ino"]),
                         (canonical_probe["dev"], canonical_probe["ino"]))

        before = self.snap()
        with self.assertRaises(cowork_graph.GraphRefusal) as caught:
            store.admit(self.revision([self.vertex(root), self.vertex(alias)]),
                        now=NOW)
        self.assertIn(caught.exception.code,
                      ("root_alias", "root_not_worktree_toplevel"))
        self.assertEqual(self.snap(), before)

        self.admit_new([self.vertex(root)])
        before = self.snap()
        with self.assertRaises(cowork_graph.GraphRefusal) as caught:
            store.admit(self.revision([self.vertex(alias)]), now=NOW)
        self.assertIn(caught.exception.code,
                      ("root_in_use", "root_not_worktree_toplevel"))
        self.assertEqual(self.snap(), before)

    def test_cross_graph_root_reuse_refused_until_terminal(self):
        root = self.worktree()
        first = self.vertex(root)
        holder = self.admit_new([first])
        self.prime_lock(holder)
        self.assertRefusedUnchanged(
            "root_in_use", store.admit, self.revision([self.vertex(root)]),
            now=NOW)
        other = self.seed()
        self.assertRefusedUnchanged(
            "root_in_use", store.admit,
            self.revision([other.vertex, self.vertex(root)]),
            graph_id=other.graph_id, now=NOW)
        outcomes = store.cancel(holder, first["work_id"], now=NOW)
        self.assertEqual([o["outcome"] for o in outcomes], ["cancelled"])
        result = store.admit(self.revision([self.vertex(root)]), now=NOW)
        self.assertEqual(result["graph_revision"], 1)
        self.assertIn(result["graph_id"], self.registry_ids())


# --------------------------------------------------------------------------- #
# Slots.                                                                      #
# --------------------------------------------------------------------------- #


class StoreClaimAtomicityTests(_StoreEnvMixin, unittest.TestCase):

    def _claim_race(self, cap):
        roots = [self.worktree() for _ in range(4)]
        graph_id = self.admit_new([self.vertex(r) for r in roots],
                                  max_parallel=cap)
        results = _race(lambda: store.claim(graph_id, None, now=NOW),
                        [()] * 4)
        return graph_id, results

    def _split(self, results):
        claimed = [r[1] for r in results if r[0] == "ok"]
        refused = sorted(r[1] for r in results if r[0] == "refused")
        self.assertEqual(len(claimed) + len(refused), len(results), results)
        return claimed, refused

    def test_concurrent_claim_processes_yield_exactly_cap_slots(self):
        graph_id, results = self._claim_race(1)
        claimed, refused = self._split(results)
        self.assertEqual(len(claimed), 1, results)
        self.assertEqual(refused, ["cap_reached"] * 3)
        held = cowork_graph.held_slots(self.graph_state(graph_id))
        self.assertEqual([(s["work_id"], s["lease_epoch"]) for s in held],
                         [(claimed[0]["work_id"], 1)])

    def test_concurrent_claims_under_patched_cap_two(self):
        with _patched_cap(2):
            graph_id, results = self._claim_race(2)
        claimed, refused = self._split(results)
        self.assertEqual(len(claimed), 2, results)
        self.assertEqual(len({c["work_id"] for c in claimed}), 2)
        self.assertEqual(refused, ["cap_reached"] * 2)
        held = cowork_graph.held_slots(self.graph_state(graph_id))
        self.assertEqual(sorted(s["work_id"] for s in held),
                         sorted(c["work_id"] for c in claimed))

    def test_ledger_consistent_after_contention(self):
        first, _ = self._claim_race(1)
        with _patched_cap(2):
            second, _ = self._claim_race(2)
        for graph_id in (first, second):
            with self.subTest(graph_id=graph_id):
                state = self.graph_state(graph_id)
                cowork_graph.check_invariants(state)
                self.assertEqual([s["seq"] for s in state["slots"]],
                                 list(range(1, len(state["slots"]) + 1)))
                held = sorted((s["work_id"], s["lease_epoch"])
                              for s in cowork_graph.held_slots(state))
                holding = sorted(
                    (w, r["lease_epoch"]) for w, r in state["vertices"].items()
                    if r["state"] in cowork_graph.HOLDING_STATES)
                self.assertEqual(held, holding)
                self.assertEqual(len(held),
                                 cowork_graph.effective_cap(state))
                for work_id, record in state["vertices"].items():
                    claims = [e for e in state["events"]
                              if e["op"] == "claim" and e["work_id"] == work_id]
                    if record["state"] == "claimed":
                        self.assertEqual(record["lease_epoch"], 1)
                        self.assertEqual(len(claims), 1)
                    else:
                        self.assertEqual(record["state"], "pending")
                        self.assertEqual(claims, [])


class SessionIndexTests(_StoreEnvMixin, unittest.TestCase):

    def test_one_vertex_per_session_across_two_graphs(self):
        first_root, second_root = self.worktree(), self.worktree()
        first, second = self.vertex(first_root), self.vertex(second_root)
        first_graph = self.admit_new([first])
        second_graph = self.admit_new([second])
        first_epoch = store.claim(first_graph, first["work_id"],
                                  now=NOW)["lease_epoch"]
        second_epoch = store.claim(second_graph, second["work_id"],
                                   now=NOW)["lease_epoch"]
        session = _uuid()
        store.bind_session(first_graph, first["work_id"], first_epoch,
                           session, first_root, STANDARD, now=NOW)
        index_path = cowork_state.graph_session_index_path_for(session)
        index_bytes = _read_bytes(index_path)
        second_bytes = self.graph_bytes(second_graph)

        self.assertRefused("session_already_bound", store.bind_session,
                           second_graph, second["work_id"], second_epoch,
                           session, second_root, STANDARD, now=NOW)
        self.assertRefused("session_already_bound",
                           store.check_bind_preconditions, second_graph,
                           second["work_id"], second_epoch, second_root,
                           STANDARD, session_uuid=session)
        self.assertIsNone(store.check_bind_preconditions(
            second_graph, second["work_id"], second_epoch, second_root,
            STANDARD))
        self.assertEqual(self.graph_bytes(second_graph), second_bytes)
        record = store.vertex_binding_for_session(session)
        self.assertEqual(
            (record["graph_id"], record["work_id"], record["lease_epoch"]),
            (first_graph, first["work_id"], first_epoch))

        first_bytes = self.graph_bytes(first_graph)
        again = store.bind_session(first_graph, first["work_id"],
                                   first_epoch, session, first_root,
                                   STANDARD, now=LATER)
        self.assertEqual(again, {"graph_id": first_graph,
                                 "work_id": first["work_id"],
                                 "lease_epoch": first_epoch,
                                 "session_uuid": session})
        self.assertEqual(self.graph_bytes(first_graph), first_bytes)
        self.assertEqual(_read_bytes(index_path), index_bytes)

    def test_unbound_session_lookup_creates_nothing(self):
        absent = self.use_sessions()
        session = _uuid()
        self.assertIsNone(store.vertex_binding_for_session(session))
        self.assertIsNone(store.fence(session, now=NOW))
        self.assertFalse(os.path.exists(absent))

        self.use_sessions()
        self.running()
        before = self.snap()
        self.assertIsNone(store.vertex_binding_for_session(session))
        self.assertIsNone(store.fence(session, now=NOW))
        self.assertEqual(self.snap(), before)
        self.assertFalse(os.path.exists(
            cowork_state.graph_session_index_path_for(session)))


# --------------------------------------------------------------------------- #
# Crash recovery.                                                             #
# --------------------------------------------------------------------------- #


class StoreCrashRecoveryTests(_StoreEnvMixin, unittest.TestCase):

    def test_crash_after_claim_write_before_launch(self):
        roots = [self.worktree(), self.worktree()]
        vertices = [self.vertex(r) for r in roots]
        graph_id = self.admit_new(vertices)
        work_id = vertices[0]["work_id"]
        self.assertEqual(store.claim(graph_id, work_id,
                                     now=NOW)["lease_epoch"], 1)
        self.assertRefused("cap_reached", store.claim, graph_id, now=NOW)
        self.assertRefusedUnchanged("claim_not_expired", store.reclaim,
                                    graph_id, work_id, now=NOW)
        self.assertEqual(store.reclaim(graph_id, work_id, now=LATER),
                         {"new_lease_epoch": 2, "holder_verdict": None,
                          "slot_released": True})
        state = self.graph_state(graph_id)
        self.assertEqual(state["vertices"][work_id]["state"], "pending")
        slots = [s for s in state["slots"] if s["work_id"] == work_id]
        self.assertEqual([s["release_reason"] for s in slots], ["reclaim"])
        self.assertEqual(cowork_graph.held_slots(state), [])
        self.assertRefusedUnchanged(
            "vertex_lease_superseded", store.bind_session, graph_id, work_id,
            1, _uuid(), roots[0], STANDARD, now=LATER)
        self.assertEqual(store.claim(graph_id, work_id,
                                     now=LATER)["lease_epoch"], 3)

    def test_crash_during_publish_before_graph_write_is_retryable(self):
        v = self.running()
        owner_id, owner_epoch = self.live_owner(v.session)
        self.txn_artifacts(v.session, v.root)
        before = self.graph_bytes(v.graph_id)
        with _failing_graph_writes():
            self.assertRefused("io_error", store.publish, v.graph_id,
                               v.work_id, v.session, owner_id, owner_epoch,
                               now=NOW)
        self.assertEqual(self.graph_bytes(v.graph_id), before)
        result = store.publish(v.graph_id, v.work_id, v.session, owner_id,
                               owner_epoch, now=NOW)
        self.assertEqual(result["outcome"], "published")
        state = self.graph_state(v.graph_id)
        self.assertEqual(state["vertices"][v.work_id]["state"], "succeeded")
        self.assertEqual([s["release_reason"] for s in state["slots"]],
                         ["terminal_receipt"])

    def test_crash_after_session_index_before_graph_write_completes_on_fence(
            self):
        root = self.worktree()
        vertex = self.vertex(root)
        graph_id = self.admit_new([vertex])
        work_id = vertex["work_id"]
        epoch = store.claim(graph_id, work_id, now=NOW)["lease_epoch"]
        session = _uuid()
        with _failing_graph_writes():
            self.assertRefused("io_error", store.bind_session, graph_id,
                               work_id, epoch, session, root, STANDARD,
                               now=NOW)
        self.assertTrue(os.path.exists(
            cowork_state.graph_session_index_path_for(session)))
        record = self.graph_state(graph_id)["vertices"][work_id]
        self.assertEqual((record["state"], record["session_uuid"]),
                         ("claimed", None))

        self.assertEqual(store.fence(session, now=NOW), {
            "graph_id": graph_id, "work_id": work_id, "lease_epoch": epoch,
            "session_uuid": session, "bind_completed": True})
        record = self.graph_state(graph_id)["vertices"][work_id]
        self.assertEqual((record["state"], record["session_uuid"]),
                         ("running", session))
        self.assertFalse(store.fence(session, now=NOW)["bind_completed"])
        self.assertRefusedUnchanged("vertex_held", store.bind_session,
                                    graph_id, work_id, epoch, _uuid(), root,
                                    STANDARD, now=NOW)

    def test_crash_after_publish_write_before_owner_release(self):
        v = self.running()
        owner_id, owner_epoch = self.live_owner(v.session)
        self.txn_artifacts(v.session, v.root)
        first = store.publish(v.graph_id, v.work_id, v.session, owner_id,
                              owner_epoch, now=NOW)
        self.assertEqual(first["outcome"], "published")
        self.assertEqual(cowork_owner.classify_owner_lease(v.session),
                         "live_owner")
        before = self.snap()
        again = store.publish(v.graph_id, v.work_id, v.session, owner_id,
                              owner_epoch, now=LATER)
        self.assertEqual(again["outcome"], "already_published")
        self.assertEqual(again["receipt_identity"], first["receipt_identity"])
        self.assertEqual(self.snap(), before)
        cowork_owner.release_owner_lease(v.session, owner_id, owner_epoch,
                                         "fixture_release")
        third = store.publish(v.graph_id, v.work_id, v.session, owner_id,
                              owner_epoch, now=LATER)
        self.assertEqual(third["outcome"], "already_published")
        self.assertEqual(third["receipt_identity"], first["receipt_identity"])
        self.assertEqual(self.snap(), before)

    def _crash_cancel_in_child(self, v):
        def child():
            with mock.patch.object(cowork_capacity_scheduler, "cancel",
                                   side_effect=lambda *a, **k: os._exit(9)):
                store.cancel(v.graph_id, v.work_id, now=NOW)
            os._exit(0)
        self.assertEqual(_run_child(child), 9)

    def test_crash_after_cancel_write_before_pause_lease_cancel_is_finished_by_retry(  # noqa: E501
            self):
        v = self.running()
        lease_id = self.pause_lease(v.session)
        self._crash_cancel_in_child(v)
        state = self.graph_state(v.graph_id)
        self.assertEqual(state["vertices"][v.work_id]["state"], "cancelled")
        self.assertEqual(cowork_graph.held_slots(state), [])
        self.assertEqual([s["release_reason"] for s in state["slots"]],
                         ["cancelled"])
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session),
                         [lease_id])

        before = self.graph_bytes(v.graph_id)
        outcomes = store.cancel(v.graph_id, v.work_id, now=NOW)
        self.assertEqual(outcomes, [{
            "work_id": v.work_id, "outcome": "already_cancelled",
            "pause_cleanup": [{"lease_id": lease_id, "result": "cancelled"}]}])
        self.assertEqual(self.graph_bytes(v.graph_id), before)
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session), [])

    def test_no_extra_slot_and_single_binding_after_every_crash_point(self):
        # A claim written whose child never launched.
        roots = [self.worktree(), self.worktree()]
        vertices = [self.vertex(r) for r in roots]
        claim_graph = self.admit_new(vertices)
        store.claim(claim_graph, vertices[0]["work_id"], now=NOW)
        self.assertStoreConsistent()

        # A session index written whose graph write was lost.
        root = self.worktree()
        vertex = self.vertex(root)
        bind_graph = self.admit_new([vertex])
        epoch = store.claim(bind_graph, vertex["work_id"],
                            now=NOW)["lease_epoch"]
        session = _uuid()
        with _failing_graph_writes():
            self.assertRefused("io_error", store.bind_session, bind_graph,
                               vertex["work_id"], epoch, session, root,
                               STANDARD, now=NOW)
        self.assertStoreConsistent()
        store.fence(session, now=NOW)
        self.assertStoreConsistent()

        # A publish whose graph write was lost, then retried.
        v = self.running()
        owner_id, owner_epoch = self.live_owner(v.session)
        self.txn_artifacts(v.session, v.root)
        with _failing_graph_writes():
            self.assertRefused("io_error", store.publish, v.graph_id,
                               v.work_id, v.session, owner_id, owner_epoch,
                               now=NOW)
        self.assertStoreConsistent()
        store.publish(v.graph_id, v.work_id, v.session, owner_id, owner_epoch,
                      now=NOW)
        self.assertStoreConsistent()

        # A cancel written whose PauseLease cleanup crashed.
        c = self.running()
        self.pause_lease(c.session)
        self._crash_cancel_in_child(c)
        self.assertStoreConsistent()

        # A NEW graph written whose registry write crashed: an orphan.
        doc = self.revision([self.vertex(self.worktree())])
        real = cowork_state.write_json_atomic_durable

        def crash_on_registry(path, data):
            if os.path.basename(path) == "registry.json":
                os._exit(9)
            return real(path, data)

        def admit_child():
            with mock.patch.object(cowork_state, "write_json_atomic_durable",
                                   side_effect=crash_on_registry):
                store.admit(doc, now=NOW)
            os._exit(0)

        registered = set(self.registry_ids())
        self.assertEqual(_run_child(admit_child), 9)
        self.assertEqual(set(self.registry_ids()), registered)
        orphans = [g for g in self.graph_dirs() if g not in registered]
        self.assertEqual(len(orphans), 1)
        orphan = orphans[0]
        self.assertTrue(os.path.exists(
            cowork_state.graph_state_path_for(orphan)))
        self.assertStoreConsistent()
        before = self.snap()
        self.assertRefused("graph_unknown", store.claim, orphan, now=NOW)
        self.assertRefused("graph_unknown", store.admit,
                           self.revision([self.vertex(self.worktree())]),
                           graph_id=orphan, now=NOW)
        self.assertRefused("graph_unknown", store.status, orphan)
        self.assertEqual(self.snap(), before)


# --------------------------------------------------------------------------- #
# Holder lifecycle.                                                           #
# --------------------------------------------------------------------------- #


class StoreReclaimTests(_StoreEnvMixin, unittest.TestCase):

    def test_reclaim_dead_owner_bumps_epoch_releases_once_and_audits(self):
        for label in ("stale_dead_owner", "unowned"):
            with self.subTest(holder=label):
                v = self.running()
                if label == "stale_dead_owner":
                    self.expired_owner(v.session, "ps_lstart")
                result = store.reclaim(v.graph_id, v.work_id, now=NOW)
                self.assertEqual(result, {"new_lease_epoch": v.epoch + 1,
                                          "holder_verdict": label,
                                          "slot_released": True})
                state = self.graph_state(v.graph_id)
                record = state["vertices"][v.work_id]
                self.assertEqual((record["state"], record["lease_epoch"],
                                  record["session_uuid"]),
                                 ("pending", v.epoch + 1, None))
                self.assertEqual([s["release_reason"] for s in state["slots"]],
                                 ["reclaim"])
                events = [e for e in state["events"] if e["op"] == "reclaim"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["detail"]["owner_verdict"], label)
                self.assertEqual(
                    store.status(v.graph_id)["statuses"][v.work_id], "ready")

    def test_reclaim_refused_while_owner_live(self):
        v = self.running()
        self.live_owner(v.session)
        self.assertRefusedUnchanged("vertex_live", store.reclaim, v.graph_id,
                                    v.work_id, now=NOW)

    def test_reclaim_refused_when_unproven(self):
        for label in ("stale_unproven", "corrupt_lease", "corrupt_pause"):
            with self.subTest(holder=label):
                v = self.running()
                if label == "stale_unproven":
                    self.expired_owner(v.session, "unavailable")
                elif label == "corrupt_lease":
                    self.corrupt_owner(v.session)
                else:
                    self.corrupt_pause_lease(v.session)
                self.assertRefusedUnchanged("holder_unproven", store.reclaim,
                                            v.graph_id, v.work_id, now=NOW)

    def test_reclaim_refused_while_pause_lease_live(self):
        v = self.running()
        self.pause_lease(v.session)
        self.assertRefusedUnchanged("vertex_paused", store.reclaim,
                                    v.graph_id, v.work_id, now=NOW)

    def test_reclaimed_session_cannot_publish_or_fence(self):
        v = self.running()
        store.reclaim(v.graph_id, v.work_id, now=NOW)
        successor, epoch = self.run_vertex(v.graph_id, v.work_id, v.root)
        self.assertEqual(epoch, v.epoch + 2)
        self.assertNotEqual(successor, v.session)
        self.txn_artifacts(v.session, v.root)
        index_path = cowork_state.graph_session_index_path_for(v.session)
        index_bytes = _read_bytes(index_path)
        self.assertRefusedUnchanged("vertex_lease_superseded", store.fence,
                                    v.session, now=NOW)
        self.assertRefusedUnchanged("receipt_cross_vertex", store.publish,
                                    v.graph_id, v.work_id, v.session,
                                    "owner-not-held", 1, now=NOW)
        self.assertRefusedUnchanged("session_already_bound",
                                    store.bind_session, v.graph_id, v.work_id,
                                    epoch, v.session, v.root, STANDARD,
                                    now=NOW)
        self.assertEqual(_read_bytes(index_path), index_bytes)


class StoreCancelPauseTests(_StoreEnvMixin, unittest.TestCase):

    def test_cancel_paused_vertex_cancels_live_pause_leases(self):
        v = self.running()
        unclaimed = self.pause_lease(v.session)
        claimed = self.pause_lease(v.session, state="claimed")
        finished = self.pause_lease(v.session, state="cancelled")
        outcomes = store.cancel(v.graph_id, v.work_id, now=NOW)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["work_id"], v.work_id)
        self.assertEqual(outcomes[0]["outcome"], "cancelled")
        self.assertEqual(
            sorted((e["lease_id"], e["result"])
                   for e in outcomes[0]["pause_cleanup"]),
            sorted([(unclaimed, "cancelled"), (claimed, "cancelled")]))
        state = self.graph_state(v.graph_id)
        record = state["vertices"][v.work_id]
        self.assertEqual((record["state"], record["lease_epoch"]),
                         ("cancelled", v.epoch + 1))
        self.assertEqual(cowork_graph.held_slots(state), [])
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session), [])
        self.assertEqual(cowork_state.read_pause_lease(
            v.session, finished)["consumption_state"], "cancelled")

    def test_second_cancel_is_idempotent_and_finishes_cleanup(self):
        v = self.running()
        first, second = sorted([self.pause_lease(v.session),
                                self.pause_lease(v.session)])
        real_cancel = cowork_capacity_scheduler.cancel

        def flaky(session_uuid, lease_id, automation_ref):
            if lease_id == first:
                raise OSError("injected cancel failure")
            return real_cancel(session_uuid, lease_id, automation_ref)

        with mock.patch.object(cowork_capacity_scheduler, "cancel",
                               side_effect=flaky):
            outcomes = store.cancel(v.graph_id, v.work_id, now=NOW)
        self.assertEqual(outcomes[0]["outcome"], "cancelled")
        self.assertEqual({e["lease_id"]: e["result"]
                          for e in outcomes[0]["pause_cleanup"]},
                         {first: "error", second: "cancelled"})
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session),
                         [first])

        before = self.graph_bytes(v.graph_id)
        again = store.cancel(v.graph_id, v.work_id, now=LATER)
        self.assertEqual(again, [{
            "work_id": v.work_id, "outcome": "already_cancelled",
            "pause_cleanup": [{"lease_id": first, "result": "cancelled"}]}])
        self.assertEqual(self.graph_bytes(v.graph_id), before)
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session), [])

    def test_cleanup_passes_stored_automation_ref_and_maps_not_found_or_not_cancellable_to_already_clean(  # noqa: E501
            self):
        v = self.running()
        lease_id = self.pause_lease(v.session,
                                    automation_ref="auto-graph-nondefault")
        with mock.patch.object(cowork_capacity_scheduler, "cancel",
                               wraps=cowork_capacity_scheduler.cancel) as spy:
            outcomes = store.cancel(v.graph_id, v.work_id, now=NOW)
        spy.assert_called_once_with(v.session, lease_id,
                                    "auto-graph-nondefault")
        self.assertEqual(outcomes[0]["pause_cleanup"],
                         [{"lease_id": lease_id, "result": "cancelled"}])

        session = _uuid()
        other = self.pause_lease(session)
        for reason, expected in (("not_found", "already_clean"),
                                 ("not_cancellable", "already_clean"),
                                 ("automation_ref_mismatch", "error")):
            with self.subTest(reason=reason):
                conflict = cowork_capacity_scheduler.SchedulerLeaseConflict(
                    other, reason)
                with mock.patch.object(cowork_capacity_scheduler, "cancel",
                                       side_effect=conflict):
                    cleanup = store._cleanup_pause_leases(session)
                self.assertEqual([(e["lease_id"], e["result"])
                                  for e in cleanup], [(other, expected)])
        self.assertEqual(cowork_state.live_pause_lease_ids(session), [other])

    def test_cancel_preserves_declaration_and_authority(self):
        v = self.running()
        revisions = self.graph_state(v.graph_id)["revisions"]
        authority = _read_bytes(v.vertex["authority_path"])
        tree = _snapshot(v.root)
        store.cancel(v.graph_id, v.work_id, now=NOW)
        self.assertEqual(self.graph_state(v.graph_id)["revisions"], revisions)
        self.assertEqual(_read_bytes(v.vertex["authority_path"]), authority)
        self.assertEqual(_snapshot(v.root), tree)

    def test_cancel_with_live_owner_records_request_only(self):
        v = self.running()
        self.live_owner(v.session)
        lease_id = self.pause_lease(v.session)
        outcomes = store.cancel(v.graph_id, v.work_id, now=NOW)
        self.assertEqual(outcomes, [{"work_id": v.work_id,
                                     "outcome": "cancel_requested",
                                     "pause_cleanup": []}])
        state = self.graph_state(v.graph_id)
        record = state["vertices"][v.work_id]
        self.assertEqual((record["state"], record["cancel_requested"]),
                         ("running", True))
        self.assertEqual(len(cowork_graph.held_slots(state)), 1)
        self.assertEqual(cowork_state.live_pause_lease_ids(v.session),
                         [lease_id])
        self.assertRefusedUnchanged("vertex_cancel_requested", store.fence,
                                    v.session, now=NOW)


# --------------------------------------------------------------------------- #
# Receipts and joins.                                                         #
# --------------------------------------------------------------------------- #


class StorePublishTests(_StoreEnvMixin, unittest.TestCase):

    def _owned_without_artifacts(self):
        """A running vertex whose session holds a live owner lease."""
        v = self.running()
        v.owner_id, v.owner_epoch = self.live_owner(v.session)
        return v

    def _owned(self, **artifacts):
        """`_owned_without_artifacts` plus transaction artifacts built with
        the given overrides."""
        v = self._owned_without_artifacts()
        self.txn_artifacts(v.session, v.root, **artifacts)
        return v

    def _publish(self, v, session=None, owner_id=None, owner_epoch=None):
        return lambda: store.publish(
            v.graph_id, v.work_id, session or v.session,
            owner_id or v.owner_id,
            v.owner_epoch if owner_epoch is None else owner_epoch, now=NOW)

    def test_publish_from_real_transaction_artifacts(self):
        v = self._owned()
        result = store.publish(v.graph_id, v.work_id, v.session, v.owner_id,
                               v.owner_epoch, now=NOW)
        self.assertEqual(result["outcome"], "published")
        self.assertIs(result["slot_released"], True)
        self.assertEqual(result["session_uuid"], v.session)
        state = self.graph_state(v.graph_id)
        record = state["vertices"][v.work_id]
        self.assertEqual(record["state"], "succeeded")
        receipt = record["receipt"]
        self.assertEqual(result["receipt_identity"],
                         list(cowork_graph.receipt_identity(receipt)))
        self.assertEqual(receipt["manifest_digest"],
                         store.live_fingerprint(v.root))
        self.assertEqual(receipt["required_checks"],
                         {c: True for c in profiles.REQUIRED_CHECKS})
        self.assertEqual(cowork_graph.held_slots(state), [])

    def _case_no_pointer(self):
        return self._publish(self._owned_without_artifacts())

    def _case_pointer_without_request(self):
        v = self._owned_without_artifacts()
        self.txn_artifacts(v.session, v.root, request=False)
        return self._publish(v)

    def _case_malformed_digest(self):
        return self._publish(self._owned(result_digest="not-a-digest"))

    def _case_request_session(self):
        return self._publish(self._owned(request_session=_uuid()))

    def _case_request_repo(self):
        return self._publish(self._owned(request_repo=self.parent))

    def _case_result_digest(self):
        return self._publish(self._owned(result_digest=_hex64("other")))

    def _case_verdict_red(self):
        return self._publish(self._owned(verdict="red"))

    def _case_disposition_rejected(self):
        return self._publish(self._owned(disposition="rejected"))

    def _case_collision(self):
        roots = [self.worktree(), self.worktree()]
        vertices = [self.vertex(r) for r in roots]
        graph_id = self.admit_new(vertices)
        first, _ = self.run_vertex(graph_id, vertices[0]["work_id"], roots[0])
        owner_id, owner_epoch = self.live_owner(first)
        txn, _digest = self.txn_artifacts(first, roots[0])
        store.publish(graph_id, vertices[0]["work_id"], first, owner_id,
                      owner_epoch, now=NOW)
        second, _ = self.run_vertex(graph_id, vertices[1]["work_id"],
                                    roots[1])
        owner_id, owner_epoch = self.live_owner(second)
        self.txn_artifacts(second, roots[1], transaction_id=txn)
        return lambda: store.publish(graph_id, vertices[1]["work_id"], second,
                                     owner_id, owner_epoch, now=NOW)

    def _case_missing_check(self):
        return self._publish(self._owned(binding="not_reached"))

    def _case_non_owner(self):
        v = self._owned()
        return self._publish(v, owner_epoch=v.owner_epoch + 1)

    def _case_cross_vertex(self):
        v = self._owned()
        unbound = _uuid()
        self.txn_artifacts(unbound, v.root)
        return self._publish(v, session=unbound)

    def _case_cancel_requested(self):
        v = self._owned()
        outcomes = store.cancel(v.graph_id, v.work_id, now=NOW)
        self.assertEqual(outcomes[0]["outcome"], "cancel_requested")
        return self._publish(v)

    def _case_terminal(self):
        v = self.running()
        store.fail(v.graph_id, v.work_id, "child_failed", now=NOW)
        v.owner_id, v.owner_epoch = self.live_owner(v.session)
        self.txn_artifacts(v.session, v.root)
        return self._publish(v)

    def test_publish_refused_for_each_store_reachable_receipt_rule(self):
        # receipt_stale_epoch, receipt_stale_revision and vertex_not_running
        # cannot be reached through the store: it builds the receipt from the
        # vertex's current epoch and claim revision, and an unbound claimed
        # vertex is refused as receipt_cross_vertex first.
        cases = [
            ("receipt_no_accepted_transaction", self._case_no_pointer),
            ("receipt_no_accepted_transaction",
             self._case_pointer_without_request),
            ("receipt_malformed", self._case_malformed_digest),
            ("receipt_wrong_candidate", self._case_request_session),
            ("receipt_wrong_candidate", self._case_request_repo),
            ("receipt_wrong_candidate", self._case_result_digest),
            ("receipt_wrong_candidate", self._case_verdict_red),
            ("receipt_wrong_candidate", self._case_disposition_rejected),
            ("receipt_candidate_collision", self._case_collision),
            ("receipt_missing_required_check", self._case_missing_check),
            ("receipt_non_owner", self._case_non_owner),
            ("receipt_cross_vertex", self._case_cross_vertex),
            ("vertex_cancel_requested", self._case_cancel_requested),
            ("vertex_terminal", self._case_terminal),
        ]
        for code, build in cases:
            with self.subTest(code=code, case=build.__name__):
                attempt = build()
                self.assertRefusedUnchanged(code, attempt)

    def test_candidate_edit_after_receipt_refused_as_changed(self):
        v = self._owned()
        with open(os.path.join(v.root, "README.txt"), "a") as fh:
            fh.write("edited after the transaction\n")
        self.assertRefusedUnchanged("receipt_candidate_changed",
                                    self._publish(v))

    def test_publish_refused_while_child_owner_live_elsewhere(self):
        v = self.running()
        self.live_owner(v.session)
        lease = cowork_owner.read_owner_lease(v.session)
        self.txn_artifacts(v.session, v.root)
        self.assertRefusedUnchanged("receipt_non_owner", store.publish,
                                    v.graph_id, v.work_id, v.session, _uuid(),
                                    1, now=NOW)
        self.assertEqual(cowork_owner.read_owner_lease(v.session), lease)

    def test_refusal_leaves_graph_bytes_identical(self):
        v = self._owned()
        readme = os.path.join(v.root, "README.txt")
        original = _read_bytes(readme)
        before = self.snap()

        self.assertRefused("receipt_non_owner",
                           self._publish(v, owner_epoch=v.owner_epoch + 1))
        self.assertEqual(self.snap(), before)

        with open(readme, "ab") as fh:
            fh.write(b"transient edit\n")
        self.assertRefused("receipt_candidate_changed", self._publish(v))
        self.assertEqual(self.snap(), before)
        _write_bytes(readme, original)

        with _failing_graph_writes():
            self.assertRefused("io_error", self._publish(v))
        self.assertEqual(self.snap(), before)

        self.assertEqual(self._publish(v)()["outcome"], "published")
        published = self.snap()
        self.assertEqual(self._publish(v)()["outcome"], "already_published")
        self.assertEqual(self.snap(), published)


class StoreJoinTests(_StoreEnvMixin, unittest.TestCase):

    def _published_pair(self):
        roots = [self.worktree(), self.worktree()]
        vertices = [self.vertex(r) for r in roots]
        join_id = _uuid()
        graph_id = self.admit_new(vertices, joins=[{
            "join_id": join_id, "rule": "all_succeeded",
            "requires": [v["work_id"] for v in vertices]}])
        for vertex, root in zip(vertices, roots):
            session, _ = self.run_vertex(graph_id, vertex["work_id"], root)
            owner_id, owner_epoch = self.live_owner(session)
            self.txn_artifacts(session, root)
            store.publish(graph_id, vertex["work_id"], session, owner_id,
                          owner_epoch, now=NOW)
        return graph_id, join_id, vertices, roots

    def test_join_after_all_succeeded(self):
        graph_id, join_id, vertices, _roots = self._published_pair()
        decision = store.join(graph_id, join_id, now=NOW)
        self.assertEqual(decision["outcome"], "joined")
        self.assertRegex(decision["decision_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual([m["work_id"] for m in decision["members"]],
                         sorted(v["work_id"] for v in vertices))
        before = self.graph_bytes(graph_id)
        self.assertEqual(store.join(graph_id, join_id, now=LATER), decision)
        self.assertEqual(self.graph_bytes(graph_id), before)

    def test_join_refused_when_root_changed_after_receipt(self):
        graph_id, join_id, _vertices, roots = self._published_pair()
        with open(os.path.join(roots[1], "README.txt"), "a") as fh:
            fh.write("edited after the receipt\n")
        self.assertRefusedUnchanged("receipt_candidate_changed", store.join,
                                    graph_id, join_id, now=NOW)

    def test_two_process_join_race_one_decision(self):
        graph_id, join_id, _vertices, _roots = self._published_pair()
        results = _race(lambda: store.join(graph_id, join_id, now=NOW),
                        [(), ()])
        self.assertEqual([r[0] for r in results], ["ok", "ok"], results)
        self.assertEqual(results[0][1]["decision_digest"],
                         results[1][1]["decision_digest"])
        state = self.graph_state(graph_id)
        self.assertEqual(list(state["joins"]), [join_id])
        self.assertEqual(list(state["joins"][join_id]),
                         [str(state["revision"])])
        self.assertEqual(len([e for e in state["events"]
                              if e["op"] == "join"]), 1)


# --------------------------------------------------------------------------- #
# Error mapping and fact gathering.                                           #
# --------------------------------------------------------------------------- #


class StoreErrorMappingTests(_StoreEnvMixin, unittest.TestCase):

    def test_unknown_graph_refused_without_creating_anything(self):
        self.seed()
        unknown = _uuid()
        noncanonical = "{%s}" % unknown
        session = _uuid()
        root = self.parent
        ops = [
            ("claim", lambda g: store.claim(g, now=NOW)),
            ("check_bind_preconditions",
             lambda g: store.check_bind_preconditions(g, _uuid(), 1, root,
                                                      STANDARD)),
            ("bind_session",
             lambda g: store.bind_session(g, _uuid(), 1, session, root,
                                          STANDARD, now=NOW)),
            ("publish",
             lambda g: store.publish(g, _uuid(), session, "owner", 1,
                                     now=NOW)),
            ("_publish_session", lambda g: store._publish_session(g, _uuid())),
            ("cancel", lambda g: store.cancel(g, now=NOW)),
            ("fail", lambda g: store.fail(g, _uuid(), "child_failed",
                                          now=NOW)),
            ("reclaim", lambda g: store.reclaim(g, _uuid(), now=NOW)),
            ("join", lambda g: store.join(g, _uuid(), now=NOW)),
            ("status", store.status),
        ]
        before = self.snap()
        for name, op in ops:
            with self.subTest(op=name):
                self.assertRefused("graph_unknown", op, unknown)
                self.assertRefused("argument_error", op, noncanonical)
                self.assertEqual(self.snap(), before)
                self.assertFalse(os.path.exists(
                    os.path.join(self.graphs(), unknown)))

    def test_lock_timeout_and_write_failure_map_to_rc1(self):
        seeded = self.seed()
        lock_path = cowork_state.graph_state_path_for(seeded.graph_id) + ".lock"
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()

        def hold_lock():
            with open(lock_path, "a+") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                ready.set()
                release.wait(RACE_TIMEOUT_S)

        holder = ctx.Process(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(ready.wait(RACE_TIMEOUT_S))
            with mock.patch.object(cowork_state, "_M3_LOCK_TIMEOUT_SECONDS",
                                   0.2):
                self.assertRefusedUnchanged("lock_timeout", store.claim,
                                            seeded.graph_id, now=NOW)
        finally:
            release.set()
            holder.join(RACE_TIMEOUT_S)
        with _failing_graph_writes():
            self.assertRefusedUnchanged("io_error", store.claim,
                                        seeded.graph_id, now=NOW)

    def test_corrupt_graph_state_maps_to_rc1(self):
        seeded = self.seed()
        _write_bytes(cowork_state.graph_state_path_for(seeded.graph_id),
                     b"{torn")
        self.assertRefusedUnchanged("graph_state_corrupt", store.claim,
                                    seeded.graph_id, now=NOW)
        self.assertRefusedUnchanged("graph_state_corrupt", store.status,
                                    seeded.graph_id)

        self.use_sessions()
        other = self.seed()
        _write_bytes(cowork_state.graph_registry_path(), b"[not a registry")
        self.assertRefusedUnchanged("graph_state_corrupt", store.claim,
                                    other.graph_id, now=NOW)
        self.assertRefusedUnchanged(
            "graph_state_corrupt", store.admit,
            self.revision([self.vertex(self.worktree())]), now=NOW)

    def test_listed_graph_without_file_is_corrupt(self):
        missing = _uuid()
        registry = cowork_state.graph_registry_path()
        self.assertTrue(cowork_state.write_json_atomic_durable(
            registry, {"schema_version": 1, "graph_ids": [missing]}))
        cowork_state._locked_json_transaction(registry, lambda existing: None)
        root = self.worktree()
        before = self.snap()
        self.assertRefused("graph_state_corrupt", store.claim, missing,
                           now=NOW)
        self.assertRefused("graph_state_corrupt", store.admit,
                           self.revision([self.vertex(root)]), now=NOW)
        self.assertEqual(self.snap(), before)
        self.assertFalse(os.path.exists(cowork_state.graph_dir_for(missing)))


class StoreFactGatheringTests(_StoreEnvMixin, unittest.TestCase):

    def test_probe_root_reports_worktree_identity(self):
        root = self.worktree()
        probe = store.probe_root(root)
        self.assertEqual(set(probe), set(cowork_graph._PROBE_KEYS))
        st = os.stat(root)
        self.assertEqual(
            (probe["declared"], probe["realpath"], probe["exists"],
             probe["is_dir"], probe["dev"], probe["ino"],
             probe["path_has_symlink"]),
            (root, root, True, True, st.st_dev, st.st_ino, False))
        self.assertEqual(probe["toplevel_realpath"], root)
        self.assertEqual(probe["head_commit"], _git(["rev-parse", "HEAD"],
                                                    root))
        self.assertEqual(probe["main_checkout_realpath"], self.main)
        main_st = os.stat(self.main)
        self.assertEqual(probe["main_checkout_dev_ino"],
                         [main_st.st_dev, main_st.st_ino])
        self.assertIs(probe["anchor_ignored"], True)
        parent_st = os.stat(self.parent)
        self.assertIn([parent_st.st_dev, parent_st.st_ino],
                      probe["ancestors"])

        main_probe = store.probe_root(self.main)
        self.assertEqual(main_probe["toplevel_realpath"], self.main)
        self.assertEqual(main_probe["main_checkout_realpath"], self.main)

        missing = store.probe_root(os.path.join(self.parent, "missing"))
        self.assertEqual(
            (missing["exists"], missing["is_dir"], missing["dev"],
             missing["toplevel_realpath"], missing["head_commit"],
             missing["main_checkout_realpath"], missing["anchor_ignored"]),
            (False, False, None, None, None, None, False))

        plain = self.make_repo(self.name("plain"), ignore=False)
        plain_probe = store.probe_root(plain)
        self.assertEqual(plain_probe["toplevel_realpath"], plain)
        self.assertIs(plain_probe["anchor_ignored"], False)

        link = os.path.join(self.parent, self.name("link"))
        os.symlink(root, link)
        linked = store.probe_root(link)
        self.assertIs(linked["path_has_symlink"], True)
        self.assertEqual(linked["realpath"], root)

    def test_gather_txn_facts_maps_artifacts(self):
        root = self.worktree()
        session = _uuid()
        txn, digest = self.txn_artifacts(session, root)
        st = os.stat(root)
        self.assertEqual(store.gather_txn_facts(session), {
            "transaction_id": txn, "request_session_uuid": session,
            "request_repo_dev_ino": [st.st_dev, st.st_ino],
            "request_manifest_digest": digest,
            "result_manifest_digest": digest, "verdict": "green",
            "final_suite_binding": "components_ran_once",
            "disposition": "accepted", "inventory_labels": [SUITE_LABEL]})

        self.assertIsNone(store.gather_txn_facts(_uuid()))
        unsafe = _uuid()
        cowork_state.write_current_receipt_pointer(
            unsafe, {"transaction_id": "../escape"})
        self.assertIsNone(store.gather_txn_facts(unsafe))
        no_request = _uuid()
        self.txn_artifacts(no_request, root, request=False)
        self.assertIsNone(store.gather_txn_facts(no_request))
        no_result = _uuid()
        self.txn_artifacts(no_result, root, result=False)
        self.assertIsNone(store.gather_txn_facts(no_result))

    def test_holder_facts_verdicts(self):
        before = _snapshot(self.sessions)
        self.assertEqual(store.holder_facts(None),
                         {"owner_verdict": "no_session",
                          "pause_lease_live": False})
        self.assertEqual(_snapshot(self.sessions), before)

        self.assertEqual(store.holder_facts(_uuid()),
                         {"owner_verdict": "unowned",
                          "pause_lease_live": False})
        live = _uuid()
        self.live_owner(live)
        self.assertEqual(store.holder_facts(live),
                         {"owner_verdict": "live_owner",
                          "pause_lease_live": False})
        paused = _uuid()
        self.pause_lease(paused)
        self.assertEqual(store.holder_facts(paused),
                         {"owner_verdict": "unowned",
                          "pause_lease_live": True})
        torn = _uuid()
        self.corrupt_pause_lease(torn)
        self.assertEqual(store.holder_facts(torn),
                         {"owner_verdict": "corrupt",
                          "pause_lease_live": True})

    def test_live_pause_lease_ids_filters_and_fails_closed(self):
        session = _uuid()
        self.assertEqual(cowork_state.live_pause_lease_ids(session), [])
        self.assertFalse(os.path.exists(cowork_state.capacity_dir_for(session)))

        unclaimed = self.pause_lease(session)
        claimed = self.pause_lease(session, state="claimed")
        self.pause_lease(session, state="cancelled")
        directory = os.path.dirname(
            cowork_state.pause_lease_path_for(session, unclaimed))
        self.assertTrue(os.path.exists(
            os.path.join(directory, unclaimed + ".json.lock")))
        _write_bytes(os.path.join(directory, unclaimed + ".json.tmp.1.2"),
                     b"{torn")
        self.assertEqual(cowork_state.live_pause_lease_ids(session),
                         sorted([unclaimed, claimed]))

        _write_bytes(os.path.join(directory, _uuid() + ".json"), b"{torn")
        with self.assertRaises(cowork_state.CorruptRecordError):
            cowork_state.live_pause_lease_ids(session)

    def test_create_exclusive_durable_identity_semantics(self):
        path = os.path.join(self.parent, "index", "record.json")
        record = {"schema_version": 1, "graph_id": _uuid(),
                  "work_id": _uuid(), "lease_epoch": 1, "bound_at": NOW}
        self.assertEqual(store._create_exclusive_durable(path, record),
                         "created")
        stored = _read_bytes(path)
        self.assertEqual(json.loads(stored.decode("utf-8")), record)
        self.assertEqual(store._create_exclusive_durable(
            path, dict(record, bound_at=LATER)), "identical")
        self.assertEqual(_read_bytes(path), stored)
        self.assertEqual(store._create_exclusive_durable(
            path, dict(record, lease_epoch=2)), "conflict")
        self.assertEqual(_read_bytes(path), stored)

        torn = os.path.join(self.parent, "index", "torn.json")
        _write_bytes(torn, b"{")
        self.assertEqual(store._create_exclusive_durable(torn, record),
                         "conflict")
        self.assertEqual(_read_bytes(torn), b"{")

    def test_graph_path_helpers(self):
        root = self.use_sessions()
        graphs = os.path.join(root, "graphs")
        graph_id, session = _uuid(), _uuid()
        self.assertEqual(cowork_state.graphs_root(), graphs)
        self.assertEqual(cowork_state.graph_dir_for(graph_id),
                         os.path.join(graphs, graph_id))
        self.assertEqual(cowork_state.graph_state_path_for(graph_id),
                         os.path.join(graphs, graph_id, "graph.json"))
        self.assertEqual(cowork_state.graph_registry_path(),
                         os.path.join(graphs, "registry.json"))
        self.assertEqual(cowork_state.graph_session_index_path_for(session),
                         os.path.join(graphs, "session-index",
                                      session + ".json"))
        for bad in ("../x", ""):
            with self.subTest(bad=bad):
                for helper in (cowork_state.graph_dir_for,
                               cowork_state.graph_state_path_for,
                               cowork_state.graph_session_index_path_for):
                    with self.assertRaises(ValueError):
                        helper(bad)
        self.assertFalse(os.path.exists(root))


if __name__ == "__main__":
    unittest.main()
