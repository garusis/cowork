#!/usr/bin/env python3
"""Tests for cowork_graph_cli: the agent-only `cowork graph <op>` surface.

Every fixture is neutral and built at run time: a temporary parent directory
holding a throwaway git repository, its linked worktrees, authority documents
and a private COWORK_SESSIONS_ROOT. Ids are fresh UUIDs and graph and owner
clocks are the real clock. The CLI is driven in-process through
cowork_graph_cli.main (with a capturing output callable) and cowork.main, and
once as a real `scripts/cowork.py graph ...` subprocess.

Run: python3 scripts/cowork_offline_tests.py test_graph_cli
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
import types
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_graph  # noqa: E402
import cowork_graph_cli  # noqa: E402
import cowork_graph_store as store  # noqa: E402
import cowork_owner  # noqa: E402
import cowork_state  # noqa: E402

STANDARD = "standard"
IGNORE_RULE = ".cowork/\n"
FABRICATED_PID_START = "2020-01-01T00:00:00Z"
SUITE_LABEL = "graph cli suite"
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Graph Fixture",
    "GIT_AUTHOR_EMAIL": "graph-fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Graph Fixture",
    "GIT_COMMITTER_EMAIL": "graph-fixture@example.invalid",
}
ENVELOPE = ("cowork_graph_result", "rc", "op", "outcome", "reason",
            "graph_id", "work_id")


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


def _lease_file(session):
    """The session's owner-lease record file, read here only as bytes."""
    return os.path.join(cowork_state.owner_dir_for(session), "lease.json")


# --------------------------------------------------------------------------- #
# Shared fixture mixin.                                                       #
# --------------------------------------------------------------------------- #


class _GraphEnvMixin(object):
    """A temp parent with a main repository (ignoring `.cowork/`), worktree,
    authority, owner-lease and transaction-artifact factories, and a CLI
    driver that checks the one-result-line contract on every call."""

    def setUp(self):
        super().setUp()
        self.parent = os.path.realpath(tempfile.mkdtemp(prefix="gc-"))
        self.addCleanup(shutil.rmtree, self.parent, True)
        self.addCleanup(self._restore_sessions_root,
                        os.environ.get("COWORK_SESSIONS_ROOT"))
        self._counter = itertools.count(1)
        self.sessions = os.path.join(self.parent, "sessions")
        os.environ["COWORK_SESSIONS_ROOT"] = self.sessions
        self.main_repo = self.make_repo("main")
        self.head = _git(["rev-parse", "HEAD"], self.main_repo)

    def _restore_sessions_root(self, old):
        if old is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = old

    # -- factories ---------------------------------------------------------- #

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
        return path

    def worktree(self):
        path = os.path.join(self.parent, self.name("wt"))
        _git(["worktree", "add", "--detach", path], self.main_repo)
        return os.path.realpath(path)

    def authority(self):
        tag = self.name("authority-")
        data = json.dumps({"authority": tag}).encode("utf-8")
        path = os.path.join(self.parent, "authority", tag + ".json")
        _write_bytes(path, data)
        return path, hashlib.sha256(data).hexdigest()

    def vertex(self, root=None, preds=(), profile=STANDARD):
        path, digest = self.authority()
        return {"work_id": _uuid(), "root": root or self.worktree(),
                "base_commit": self.head, "authority_path": path,
                "authority_digest": digest, "profile": profile,
                "predecessors": list(preds)}

    def revision(self, vertices, joins=None):
        doc = {"schema_version": 1, "max_parallel": 1,
               "vertices": [dict(v) for v in vertices]}
        if joins is not None:
            doc["joins"] = joins
        return doc

    def revision_file(self, doc):
        path = os.path.join(self.parent, "revisions", self.name("rev-")
                            + ".json")
        _write_bytes(path, json.dumps(doc).encode("utf-8"))
        return path

    def admit(self, vertices, joins=None):
        return store.admit(self.revision(vertices, joins))["graph_id"]

    def running(self):
        """A graph whose single vertex is bound to a fresh session that has
        never held an owner lease."""
        vertex = self.vertex()
        graph_id = self.admit([vertex])
        info = store.claim(graph_id, vertex["work_id"])
        session = _uuid()
        store.bind_session(graph_id, vertex["work_id"], info["lease_epoch"],
                           session, vertex["root"], STANDARD)
        return types.SimpleNamespace(
            graph_id=graph_id, work_id=vertex["work_id"],
            root=vertex["root"], session=session,
            epoch=info["lease_epoch"])

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

    def corrupt_owner(self, session):
        _write_bytes(_lease_file(session), b"{not json")

    def pause_lease(self, session):
        lease_id = _uuid()
        cowork_state.create_pause_lease(session, {
            "schema_version": 1, "package_id": "pkg-graph-cli",
            "lease_id": lease_id, "resume_mode": "scheduled",
            "not_before": "2030-01-01T00:10:00Z",
            "automation_ref": "auto-graph-cli",
            "consumption_state": "unclaimed", "failed_wake_attempts": 0,
            "issued_at": "2030-01-01T00:00:00Z", "role": "builder",
            "provider_session_id": "provider-" + lease_id,
            "controller_policy_digest": _hex64("policy"),
            "candidate_digest": _hex64("candidate"),
            "artifact_hashes": {"artifact.txt": _hex64("artifact")},
        })
        return lease_id

    def txn_artifacts(self, session, root):
        """Accepted green owned-verification artifacts for `session`
        fingerprinting `root` as it is now."""
        txn = "txn-" + uuid.uuid4().hex
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
        return txn

    # -- reads -------------------------------------------------------------- #

    def graph_bytes(self, graph_id):
        return _read_bytes(cowork_state.graph_state_path_for(graph_id))

    def graph_state(self, graph_id):
        return json.loads(self.graph_bytes(graph_id).decode("utf-8"))

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
        if "detail" in line:
            self.assertIsInstance(line["detail"], str)
        return line

    def ok(self, *argv):
        line = self.cli(*argv)
        self.assertEqual(line["outcome"], "ok", line)
        return line

    def refused(self, code, *argv):
        line = self.cli(*argv)
        self.assertEqual((line["outcome"], line["reason"]),
                         ("refused", code), line)
        return line


# --------------------------------------------------------------------------- #
# The CLI contract.                                                           #
# --------------------------------------------------------------------------- #


class GraphCliContractTests(_GraphEnvMixin, unittest.TestCase):

    def test_every_op_emits_exactly_one_last_json_line(self):
        # admit
        a, b = self.vertex(), self.vertex()
        join_id = _uuid()
        line = self.ok("admit", "--new", "--revision-file",
                       self.revision_file(self.revision(
                           [a, b], joins=[{"join_id": join_id,
                                           "rule": "all_succeeded",
                                           "requires": [a["work_id"]]}])))
        graph_id = line["graph_id"]
        self.assertEqual(line["op"], "admit")
        self.refused("revision_malformed", "admit", "--new",
                     "--revision-file", self.revision_file({"x": 1}))
        # status
        self.ok("status", "--graph-id", graph_id)
        self.refused("graph_unknown", "status", "--graph-id", _uuid())
        # claim
        self.ok("claim", "--graph-id", graph_id, "--work-id", a["work_id"])
        self.refused("cap_reached", "claim", "--graph-id", graph_id)
        # fail
        self.refused("vertex_not_claimed", "fail", "--graph-id", graph_id,
                     "--work-id", b["work_id"], "--reason-code", "broken")
        self.ok("fail", "--graph-id", graph_id, "--work-id", a["work_id"],
                "--reason-code", "broken")
        # join
        self.refused("join_unknown", "join", "--graph-id", graph_id,
                     "--join-id", _uuid())
        self.ok("join", "--graph-id", graph_id, "--join-id", join_id)
        # reclaim (a running vertex whose session never held a lease)
        v = self.running()
        self.refused("vertex_not_claimed", "reclaim", "--graph-id",
                     graph_id, "--work-id", b["work_id"])
        self.ok("reclaim", "--graph-id", v.graph_id, "--work-id", v.work_id)
        # cancel
        self.refused("vertex_unknown", "cancel", "--graph-id", graph_id,
                     "--work-id", _uuid())
        self.ok("cancel", "--graph-id", graph_id, "--work-id", b["work_id"])
        # publish
        self.refused("vertex_not_running", "publish", "--graph-id",
                     v.graph_id, "--work-id", v.work_id)
        p = self.running()
        self.txn_artifacts(p.session, p.root)
        self.ok("publish", "--graph-id", p.graph_id, "--work-id", p.work_id)
        # Through cowork.main as well: stdout holds only the graph line.
        for argv, outcome in ((["graph", "status", "--graph-id", graph_id],
                               "ok"),
                              (["graph", "status", "--graph-id", _uuid()],
                               "refused")):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = cowork.main(argv)
            line = self.check_line(out.getvalue(), rc)
            self.assertEqual((line["op"], line["outcome"]),
                             ("status", outcome))

    def test_every_refusal_code_reachable_from_cli_has_its_rc(self):
        # (i) real state.
        self.refused("argument_error", "status", "--graph-id", "not-a-uuid")
        self.refused("revision_malformed", "admit", "--new",
                     "--revision-file", self.revision_file({"x": 1}))
        self.refused("graph_unknown", "status", "--graph-id", _uuid())

        a = self.vertex()
        b = self.vertex(preds=[a["work_id"]])
        join_id = _uuid()
        graph_id = self.admit([a, b], joins=[
            {"join_id": join_id, "rule": "all_succeeded",
             "requires": [b["work_id"]]}])
        g = ("--graph-id", graph_id)
        unknown = _uuid()
        self.refused("vertex_unknown", "claim", *g, "--work-id", unknown)
        self.refused("vertex_unknown", "fail", *g, "--work-id", unknown,
                     "--reason-code", "broken")
        self.refused("vertex_unknown", "reclaim", *g, "--work-id", unknown)
        line = self.refused("vertex_unknown", "publish", *g, "--work-id",
                            unknown)
        self.assertEqual(line["work_id"], unknown)
        self.refused("vertex_not_ready", "claim", *g, "--work-id",
                     b["work_id"])
        self.refused("vertex_not_claimed", "fail", *g, "--work-id",
                     a["work_id"], "--reason-code", "broken")
        self.refused("vertex_not_claimed", "reclaim", *g, "--work-id",
                     a["work_id"])
        self.refused("vertex_not_running", "publish", *g, "--work-id",
                     a["work_id"])
        self.refused("early_join", "join", *g, "--join-id", join_id)
        self.refused("join_unknown", "join", *g, "--join-id", _uuid())
        self.ok("claim", *g, "--work-id", a["work_id"])
        self.refused("vertex_held", "claim", *g, "--work-id", a["work_id"])
        self.refused("cap_reached", "claim", *g)
        self.refused("claim_not_expired", "reclaim", *g, "--work-id",
                     a["work_id"])
        self.ok("fail", *g, "--work-id", a["work_id"], "--reason-code",
                "broken")
        self.refused("vertex_terminal", "claim", *g, "--work-id",
                     a["work_id"])
        self.refused("vertex_terminal", "fail", *g, "--work-id",
                     a["work_id"], "--reason-code", "broken")
        self.refused("vertex_blocked", "claim", *g, "--work-id",
                     b["work_id"])
        self.refused("none_ready", "claim", *g)
        self.ok("cancel", *g)
        self.refused("graph_cancelled", "claim", *g)

        v = self.running()
        h = ("--graph-id", v.graph_id, "--work-id", v.work_id)
        self.refused("receipt_no_accepted_transaction", "publish", *h)
        owner_id, owner_epoch = self.live_owner(v.session)
        self.refused("vertex_live", "reclaim", *h)
        self.refused("vertex_live", "fail", *h, "--reason-code", "broken")
        self.refused("owner_conflict", "publish", *h)
        cowork_owner.release_owner_lease(v.session, owner_id, owner_epoch,
                                         "normal_exit")
        self.pause_lease(v.session)
        self.refused("vertex_paused", "reclaim", *h)

        u = self.running()
        self.expired_owner(u.session, "unavailable")
        self.refused("holder_unproven", "reclaim", "--graph-id", u.graph_id,
                     "--work-id", u.work_id)

        torn = self.running()
        _write_bytes(cowork_state.graph_state_path_for(torn.graph_id),
                     b"{torn")
        self.refused("graph_state_corrupt", "status", "--graph-id",
                     torn.graph_id)

        # (ii) every closed code a store op can raise maps to its rc.
        graph_id = _uuid()
        for code in cowork_graph.REASON_CODES:
            with self.subTest(code=code), mock.patch.object(
                    store, "status",
                    side_effect=cowork_graph.GraphRefusal(code)):
                line = self.refused(code, "status", "--graph-id", graph_id)
                self.assertEqual(line["rc"], cowork_graph.REASON_RC[code])
                self.assertEqual(line["graph_id"], graph_id)

    def test_argument_errors_emit_one_line_rc_2(self):
        graph_id, work_id = _uuid(), _uuid()
        revision = self.revision_file(self.revision([self.vertex()]))
        cases = [
            ([], None),
            (["bogus"], None),
            (["--graph-id", graph_id], None),
            (["status"], "status"),
            (["admit", "--revision-file", revision, "--new", "--graph-id",
              graph_id], "admit"),
            (["admit", "--revision-file", revision], "admit"),
            (["fail", "--graph-id", graph_id, "--work-id", work_id], "fail"),
            (["claim", "--graph-id", graph_id, "--work", work_id], "claim"),
            (["publish", "--graph-id", graph_id, "--work-id", work_id,
              "--session-file", os.path.join(self.parent, "s.json")],
             "publish"),
            (["admit", "--new", "--revision-file",
              os.path.join(self.parent, "missing.json")], "admit"),
        ]
        for argv, op in cases:
            with self.subTest(argv=argv):
                line = self.refused("argument_error", *argv)
                self.assertEqual(line["rc"], 2)
                self.assertEqual(line["op"], op)
        not_json = os.path.join(self.parent, "not-json.json")
        _write_bytes(not_json, b"{not json")
        not_object = self.revision_file([1, 2])
        for path in (not_json, not_object):
            with self.subTest(path=path):
                self.assertEqual(self.refused(
                    "revision_malformed", "admit", "--new",
                    "--revision-file", path)["rc"], 2)
        for argv in (["--help"], ["status", "--help"]):
            with self.subTest(argv=argv):
                out = []
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc = cowork_graph_cli.main(argv, output=out.append)
                self.assertEqual(rc, 0)
                self.assertEqual(out, [])
                self.assertNotIn("cowork_graph_result", stdout.getvalue())

    def test_unexpected_exception_is_io_error_rc_1(self):
        graph_id = _uuid()
        out = []
        err = io.StringIO()
        with mock.patch.object(store, "status",
                               side_effect=RuntimeError("fixture failure")), \
                contextlib.redirect_stderr(err):
            rc = cowork_graph_cli.main(["status", "--graph-id", graph_id],
                                       output=out.append)
        line = self.check_line("".join(out), rc)
        self.assertEqual((line["outcome"], line["reason"], line["rc"]),
                         ("error", "io_error", 1))
        self.assertEqual(line["graph_id"], graph_id)
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("Traceback", "".join(out))

    def test_result_line_field_rule(self):
        # status: the view's graph_id is the envelope graph_id; the view
        # fields ride alongside.
        a, b = self.vertex(), self.vertex()
        join_id = _uuid()
        graph_id = self.admit([a, b], joins=[
            {"join_id": join_id, "rule": "all_succeeded",
             "requires": [a["work_id"]]}])
        line = self.ok("status", "--graph-id", graph_id)
        self.assertEqual(line["graph_id"], graph_id)
        self.assertIsNone(line["work_id"])
        self.assertEqual(line["revision"], 1)
        self.assertEqual(set(line["statuses"]),
                         {a["work_id"], b["work_id"]})
        # Refusal detail is a string even when the refusal carries a dict.
        self.ok("claim", "--graph-id", graph_id, "--work-id", a["work_id"])
        line = self.refused("cap_reached", "claim", "--graph-id", graph_id,
                            "--work-id", b["work_id"])
        self.assertIsInstance(line["detail"], str)
        self.assertEqual(line["work_id"], b["work_id"])
        # join: the JoinDecision rides as `decision`; the envelope outcome
        # stays 'ok'. A repeat returns the stored decision unchanged.
        self.ok("fail", "--graph-id", graph_id, "--work-id", a["work_id"],
                "--reason-code", "broken")
        first = self.ok("join", "--graph-id", graph_id, "--join-id", join_id)
        self.assertEqual(first["outcome"], "ok")
        self.assertEqual(first["decision"]["outcome"], "blocked")
        self.assertEqual(first["decision"]["record"], "JoinDecision")
        again = self.ok("join", "--graph-id", graph_id, "--join-id", join_id)
        self.assertEqual(again["decision"], first["decision"])
        # cancel: per-vertex outcomes, sorted by work_id.
        c, d = self.vertex(), self.vertex()
        other = self.admit([c, d])
        line = self.ok("cancel", "--graph-id", other)
        self.assertEqual([o["work_id"] for o in line["outcomes"]],
                         sorted([c["work_id"], d["work_id"]]))
        self.assertEqual({o["outcome"] for o in line["outcomes"]},
                         {"cancelled"})
        # publish: the store outcome rides as publish_outcome.
        v = self.running()
        self.txn_artifacts(v.session, v.root)
        h = ("--graph-id", v.graph_id, "--work-id", v.work_id)
        first = self.ok("publish", *h)
        self.assertEqual(first["publish_outcome"], "published")
        self.assertIs(first["slot_released"], True)
        self.assertEqual(first["session_uuid"], v.session)
        again = self.ok("publish", *h)
        self.assertEqual(again["publish_outcome"], "already_published")
        self.assertEqual(again["receipt_identity"],
                         first["receipt_identity"])

    def test_main_dispatches_graph_before_argparse(self):
        graph_id = self.admit([self.vertex()])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cowork, "build_parser",
                               side_effect=AssertionError("flat argparse")), \
                mock.patch.object(cowork, "run_flow",
                                  side_effect=AssertionError("run_flow")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cowork.main(["graph", "status", "--graph-id", graph_id])
        self.assertEqual(rc, 0)
        line = self.check_line(out.getvalue(), rc)
        self.assertEqual((line["op"], line["graph_id"]), ("status", graph_id))
        self.assertNotIn("cowork_result", line)

    def test_process_exit_status_equals_rc(self):
        graph_id = self.admit([self.vertex()])
        env = dict(os.environ)
        env["COWORK_SESSIONS_ROOT"] = self.sessions
        script = os.path.join(_HERE, "cowork.py")
        for target, expected in ((graph_id, 0), (_uuid(), 2)):
            with self.subTest(expected=expected):
                proc = subprocess.run(
                    [sys.executable, script, "graph", "status",
                     "--graph-id", target],
                    cwd=self.parent, env=env, capture_output=True,
                    text=True, timeout=120)
                line = json.loads(proc.stdout.splitlines()[-1])
                self.assertEqual(proc.returncode, line["rc"])
                self.assertEqual(line["rc"], expected)

    def test_claim_returns_launch_argv_and_cwd(self):
        a = self.vertex()
        b = self.vertex(profile="assurance")
        graph_id = self.admit([a])
        other = self.admit([b])
        for gid, work_args, vertex in (
                (graph_id, (), a),
                (other, ("--work-id", b["work_id"]), b)):
            with self.subTest(work_args=work_args):
                line = self.ok("claim", "--graph-id", gid, *work_args)
                epoch = line["lease_epoch"]
                self.assertEqual(epoch, 1)
                self.assertEqual((line["graph_id"], line["work_id"]),
                                 (gid, vertex["work_id"]))
                self.assertEqual(line["root"], vertex["root"])
                self.assertEqual(line["cwd"], vertex["root"])
                self.assertEqual(line["profile"], vertex["profile"])
                token = "%s:%s:%d" % (gid, vertex["work_id"], epoch)
                self.assertEqual(line["launch_argv"],
                                 ["--new", "--profile", vertex["profile"],
                                  "--graph-vertex", token])
                self.assertEqual(
                    cowork_graph_cli.parse_graph_vertex(
                        line["launch_argv"][4]),
                    (gid, vertex["work_id"], epoch))

    def test_graph_vertex_token_parser_is_canonical_only(self):
        graph_id, work_id = _uuid(), _uuid()
        self.assertEqual(cowork_graph_cli.parse_graph_vertex(
            "%s:%s:7" % (graph_id, work_id)), (graph_id, work_id, 7))
        for token in ("%s:%s" % (graph_id, work_id),
                      "%s:%s:1:2" % (graph_id, work_id),
                      "%s:%s:1" % (graph_id.upper(), work_id),
                      "%s:%s:1" % (graph_id, "not-a-uuid"),
                      "%s:%s:+1" % (graph_id, work_id),
                      "%s:%s:01" % (graph_id, work_id),
                      "%s:%s:-1" % (graph_id, work_id),
                      "%s:%s:1\n" % (graph_id, work_id),
                      "%s:%s:" % (graph_id, work_id),
                      "", None, 5):
            with self.subTest(token=token):
                self.assertIsNone(cowork_graph_cli.parse_graph_vertex(token))


# --------------------------------------------------------------------------- #
# Publication ownership.                                                      #
# --------------------------------------------------------------------------- #


class GraphCliPublishOwnerTests(_GraphEnvMixin, unittest.TestCase):

    def publish(self, v, *extra):
        return self.cli("publish", "--graph-id", v.graph_id, "--work-id",
                        v.work_id, *extra)

    @contextlib.contextmanager
    def recorded_acquisitions(self):
        real = cowork_owner.acquire_owner_lease
        calls = []

        def record(session_uuid, claimant, now=None):
            lease = real(session_uuid, claimant, now=now)
            calls.append((session_uuid, dict(claimant), dict(lease)))
            return lease

        with mock.patch.object(cowork_owner, "acquire_owner_lease",
                               side_effect=record):
            yield calls

    def test_publish_acquires_child_lease_with_graph_publish_entry_point(
            self):
        v = self.running()
        self.txn_artifacts(v.session, v.root)
        with self.recorded_acquisitions() as calls:
            line = self.publish(v)
        self.assertEqual((line["outcome"], line["publish_outcome"]),
                         ("ok", "published"))
        self.assertEqual(len(calls), 1)
        session, claimant, lease = calls[0]
        self.assertEqual(session, v.session)
        self.assertEqual(claimant["entry_point"], "graph_publish")
        self.assertIsNone(claimant["session_file"])
        receipt = self.graph_state(v.graph_id)["vertices"][v.work_id][
            "receipt"]
        self.assertEqual((receipt["owner_id"], receipt["owner_epoch"]),
                         (lease["owner_id"], lease["epoch"]))
        self.assertEqual(receipt["session_uuid"], v.session)

    def test_publish_refused_while_child_live(self):
        for kind in ("live", "stale_unproven", "corrupt"):
            with self.subTest(kind=kind):
                v = self.running()
                self.txn_artifacts(v.session, v.root)
                if kind == "live":
                    self.live_owner(v.session)
                elif kind == "stale_unproven":
                    self.expired_owner(v.session, "unavailable")
                else:
                    self.corrupt_owner(v.session)
                lease_path = _lease_file(v.session)
                lease_before = _read_bytes(lease_path)
                before = self.graph_bytes(v.graph_id)
                line = self.publish(v)
                self.assertEqual((line["reason"], line["rc"]),
                                 ("owner_conflict", 3))
                self.assertEqual(self.graph_bytes(v.graph_id), before)
                self.assertEqual(_read_bytes(lease_path), lease_before)
                if kind == "live":
                    self.assertEqual(
                        cowork_owner.classify_owner_lease(v.session),
                        "live_owner")

    def test_publish_resolves_session_from_graph_binding_not_from_caller(
            self):
        v = self.running()
        first = v.session
        self.expired_owner(first, "ps_lstart")
        line = self.ok("reclaim", "--graph-id", v.graph_id, "--work-id",
                       v.work_id)
        self.assertEqual(line["holder_verdict"], "stale_dead_owner")
        info = store.claim(v.graph_id, v.work_id)
        second = _uuid()
        store.bind_session(v.graph_id, v.work_id, info["lease_epoch"],
                           second, v.root, STANDARD)
        self.txn_artifacts(first, v.root)
        self.txn_artifacts(second, v.root)
        first_lease = _read_bytes(_lease_file(first))
        with self.recorded_acquisitions() as calls:
            line = self.publish(v)
        self.assertEqual(line["publish_outcome"], "published")
        self.assertEqual(line["session_uuid"], second)
        receipt = self.graph_state(v.graph_id)["vertices"][v.work_id][
            "receipt"]
        self.assertEqual(receipt["session_uuid"], second)
        self.assertEqual(receipt["lease_epoch"], info["lease_epoch"])
        self.assertEqual([c[0] for c in calls], [second])
        self.assertEqual(_read_bytes(_lease_file(first)), first_lease)
        # No caller anchor is accepted at all.
        self.refused("argument_error", "publish", "--graph-id", v.graph_id,
                     "--work-id", v.work_id, "--session-file",
                     os.path.join(v.root, ".cowork", "session.json"))
        self.refused("argument_error", "publish", "--graph-id", v.graph_id,
                     "--work-id", v.work_id, "--session-uuid", first)

    def test_take_over_only_when_proved_dead(self):
        dead = self.running()
        self.txn_artifacts(dead.session, dead.root)
        self.expired_owner(dead.session, "ps_lstart")
        before = self.graph_bytes(dead.graph_id)
        line = self.publish(dead)
        self.assertEqual((line["reason"], line["rc"]), ("owner_conflict", 3))
        self.assertEqual(self.graph_bytes(dead.graph_id), before)
        line = self.publish(dead, "--take-over")
        self.assertEqual((line["outcome"], line["publish_outcome"]),
                         ("ok", "published"))
        self.assertEqual(cowork_owner.classify_owner_lease(dead.session),
                         "unowned")

        live = self.running()
        self.txn_artifacts(live.session, live.root)
        self.live_owner(live.session)
        before = self.graph_bytes(live.graph_id)
        with mock.patch.object(os, "kill") as kill:
            line = self.publish(live, "--take-over")
        kill.assert_not_called()
        self.assertEqual((line["reason"], line["rc"]), ("owner_conflict", 3))
        self.assertEqual(line["detail"], "terminate_prior_refused")
        self.assertEqual(self.graph_bytes(live.graph_id), before)
        self.assertEqual(cowork_owner.classify_owner_lease(live.session),
                         "live_owner")

        unproven = self.running()
        self.txn_artifacts(unproven.session, unproven.root)
        self.expired_owner(unproven.session, "unavailable")
        before = self.graph_bytes(unproven.graph_id)
        with mock.patch.object(os, "kill") as kill:
            line = self.publish(unproven, "--take-over")
        kill.assert_not_called()
        self.assertEqual((line["reason"], line["rc"]), ("owner_conflict", 3))
        self.assertEqual(self.graph_bytes(unproven.graph_id), before)

    def test_lease_released_after_publish_and_after_refusal(self):
        ok = self.running()
        self.txn_artifacts(ok.session, ok.root)
        self.assertEqual(self.publish(ok)["outcome"], "ok")
        self.assertEqual(cowork_owner.classify_owner_lease(ok.session),
                         "unowned")
        self.assertEqual(cowork_owner.read_owner_lease(ok.session)["state"],
                         "released")

        no_txn = self.running()
        line = self.publish(no_txn)
        self.assertEqual((line["reason"], line["rc"]),
                         ("receipt_no_accepted_transaction", 2))
        self.assertEqual(cowork_owner.classify_owner_lease(no_txn.session),
                         "unowned")
        record = cowork_owner.read_owner_lease(no_txn.session)
        self.assertEqual((record["state"], record["entry_point"]),
                         ("released", "graph_publish"))

        gone = self.running()
        self.ok("reclaim", "--graph-id", gone.graph_id, "--work-id",
                gone.work_id)
        line = self.publish(gone)
        self.assertEqual((line["reason"], line["rc"]),
                         ("vertex_not_running", 2))
        self.assertEqual(cowork_owner.classify_owner_lease(gone.session),
                         "unowned")
        self.assertIsNone(cowork_owner.read_owner_lease(gone.session))


if __name__ == "__main__":
    unittest.main()
