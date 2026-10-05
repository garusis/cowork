#!/usr/bin/env python3
"""Governed parallel work graph: the agent-only `cowork graph <op>` CLI
(issue #75, package P3).

A thin surface over `cowork_graph_store`. Ops: admit, status, claim, publish,
cancel, fail, reclaim, join. Every invocation ends stdout with exactly one
JSON result line:

    {cowork_graph_result: 1, rc, op, outcome: 'ok'|'refused'|'error',
     reason: <closed cowork_graph.REASON_RC code>|null, graph_id|null,
     work_id|null, ...op fields}

and returns that rc, which is always 0, 1, 2 or 3 (rc 5 is provider capacity
and is never used here). Diagnostics go to stderr. The envelope keys always
win over op fields: publish carries the store outcome as `publish_outcome`,
cancel carries its per-vertex list as `outcomes` and join carries the stored
JoinDecision as `decision`. A refusal adds `detail` (a string) when the
refusal has one.

publish takes no session or anchor argument: the child session is the one
graph.json binds to the vertex. It acquires that session's owner lease under
the 'graph_publish' entry point (a live, unproven or corrupt owner refuses
owner_conflict), takes over only an owner proved dead with --take-over (never
terminate_prior), publishes, and always releases the lease.

This module also owns the `--graph-vertex GRAPH_ID:WORK_ID:EPOCH` token
parser used by cowork.run_flow. It never imports cowork.

Python 3.9+, stdlib only.
"""

import argparse
import json
import os
import re
import sys
import traceback
import uuid

import cowork_graph
import cowork_graph_store
import cowork_owner

RESULT_VERSION = 1
OPS = ("admit", "status", "claim", "publish", "cancel", "fail", "reclaim",
       "join")
ENVELOPE_KEYS = frozenset({"cowork_graph_result", "rc", "op", "outcome",
                           "reason", "graph_id", "work_id"})
_EPOCH_RE = re.compile(r"(0|[1-9][0-9]*)")

GraphRefusal = cowork_graph.GraphRefusal


def _is_canonical_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def parse_graph_vertex(token):
    """(graph_id, work_id, epoch) for a canonical GRAPH_ID:WORK_ID:EPOCH
    token (lowercase UUIDs, a decimal epoch without sign or leading zeros),
    else None. Whether the vertex exists or the epoch is current is the
    store's decision, not this parser's."""
    if not isinstance(token, str):
        return None
    parts = token.split(":")
    if len(parts) != 3:
        return None
    graph_id, work_id, epoch = parts
    if not (_is_canonical_uuid(graph_id) and _is_canonical_uuid(work_id)):
        return None
    if not _EPOCH_RE.fullmatch(epoch):
        return None
    return graph_id, work_id, int(epoch)


# --------------------------------------------------------------------------- #
# Parser.                                                                     #
# --------------------------------------------------------------------------- #


class _ArgumentError(Exception):
    """An argparse usage error, raised instead of exiting so `main` can end
    with its one result line."""


class _Parser(argparse.ArgumentParser):

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        raise _ArgumentError(message)


def build_graph_parser():
    parser = _Parser(prog="cowork graph", allow_abbrev=False,
                     description="Agent-only governed parallel work graph "
                                 "operations. Ends with one JSON result line.")
    sub = parser.add_subparsers(dest="op", required=True, metavar="OP",
                                parser_class=_Parser)

    admit = sub.add_parser("admit", allow_abbrev=False,
                           help="admit a revision document")
    admit.add_argument("--revision-file", dest="revision_file", required=True,
                       metavar="PATH")
    target = admit.add_mutually_exclusive_group(required=True)
    target.add_argument("--new", action="store_true",
                        help="admit into a new graph")
    target.add_argument("--graph-id", dest="graph_id")

    status = sub.add_parser("status", allow_abbrev=False,
                            help="read-only graph status")
    status.add_argument("--graph-id", dest="graph_id", required=True)

    claim = sub.add_parser("claim", allow_abbrev=False,
                           help="claim one slot (first ready vertex, or "
                                "--work-id)")
    claim.add_argument("--graph-id", dest="graph_id", required=True)
    claim.add_argument("--work-id", dest="work_id")

    publish = sub.add_parser("publish", allow_abbrev=False,
                             help="publish the bound child session's receipt")
    publish.add_argument("--graph-id", dest="graph_id", required=True)
    publish.add_argument("--work-id", dest="work_id", required=True)
    publish.add_argument("--take-over", dest="take_over", action="store_true",
                         help="take over the child's owner lease when its "
                              "owner is proved dead")

    cancel = sub.add_parser("cancel", allow_abbrev=False,
                            help="cancel one vertex or the whole graph")
    cancel.add_argument("--graph-id", dest="graph_id", required=True)
    cancel.add_argument("--work-id", dest="work_id")

    fail = sub.add_parser("fail", allow_abbrev=False,
                          help="record a vertex as failed")
    fail.add_argument("--graph-id", dest="graph_id", required=True)
    fail.add_argument("--work-id", dest="work_id", required=True)
    fail.add_argument("--reason-code", dest="reason_code", required=True)

    reclaim = sub.add_parser("reclaim", allow_abbrev=False,
                             help="release an abandoned claim")
    reclaim.add_argument("--graph-id", dest="graph_id", required=True)
    reclaim.add_argument("--work-id", dest="work_id", required=True)

    join = sub.add_parser("join", allow_abbrev=False,
                          help="store (or return) a join decision")
    join.add_argument("--graph-id", dest="graph_id", required=True)
    join.add_argument("--join-id", dest="join_id", required=True)
    return parser


# --------------------------------------------------------------------------- #
# Result line.                                                                #
# --------------------------------------------------------------------------- #


def _line(op, rc, outcome, reason, graph_id, work_id, fields=None,
          detail=None):
    line = {"cowork_graph_result": RESULT_VERSION, "rc": rc, "op": op,
            "outcome": outcome, "reason": reason, "graph_id": graph_id,
            "work_id": work_id}
    for key, value in (fields or {}).items():
        if key not in ENVELOPE_KEYS:
            line[key] = value
    if detail is not None:
        line["detail"] = str(detail)
    return line


def _emit(write, line):
    write(json.dumps(line, sort_keys=True) + "\n")
    return line["rc"]


# --------------------------------------------------------------------------- #
# Ops.                                                                        #
# --------------------------------------------------------------------------- #


def _read_revision(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        raise GraphRefusal("argument_error", detail="revision_file")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GraphRefusal("revision_malformed", detail="not JSON")
    if not isinstance(doc, dict):
        raise GraphRefusal("revision_malformed", detail="not an object")
    return doc


def _acquire_child_lease(session, work_id, take_over):
    """The child session's owner lease for publication, or owner_conflict."""
    claimant = cowork_owner.owner_identity(session, "graph_publish",
                                           os.getcwd(), None)
    try:
        mode = (cowork_owner.select_takeover_mode(session)
                if take_over else None)
        if mode == "terminate_prior":
            raise GraphRefusal("owner_conflict", work_id,
                               "terminate_prior_refused")
        if mode == "proved_dead":
            return cowork_owner.take_over(session, claimant, "proved_dead")
        return cowork_owner.acquire_owner_lease(session, claimant)
    except cowork_owner.OwnerLeaseError as exc:
        raise GraphRefusal("owner_conflict", work_id,
                           cowork_owner.owner_refusal_reason(exc))


def _op_publish(graph_id, work_id, take_over):
    view = cowork_graph_store.status(graph_id)
    if work_id not in view["statuses"]:
        raise GraphRefusal("vertex_unknown", work_id)
    session = cowork_graph_store._publish_session(graph_id, work_id)
    if session is None:
        raise GraphRefusal("vertex_not_running", work_id)
    lease = _acquire_child_lease(session, work_id, take_over)
    try:
        result = cowork_graph_store.publish(graph_id, work_id, session,
                                            lease["owner_id"], lease["epoch"])
    finally:
        cowork_owner.release_owner_lease(session, lease["owner_id"],
                                         lease["epoch"], "normal_exit")
    return {"receipt_identity": result["receipt_identity"],
            "session_uuid": result["session_uuid"],
            "slot_released": result["slot_released"],
            "publish_outcome": result["outcome"]}


def _run_op(args):
    """(graph_id, work_id, fields) for one successful op."""
    op = args.op
    if op == "admit":
        doc = _read_revision(args.revision_file)
        result = cowork_graph_store.admit(
            doc, graph_id=None if args.new else args.graph_id)
        return result["graph_id"], None, result
    if op == "status":
        return args.graph_id, None, cowork_graph_store.status(args.graph_id)
    if op == "claim":
        info = cowork_graph_store.claim(args.graph_id, args.work_id)
        return info["graph_id"], info["work_id"], info
    if op == "publish":
        return args.graph_id, args.work_id, _op_publish(
            args.graph_id, args.work_id, args.take_over)
    if op == "cancel":
        return args.graph_id, args.work_id, {
            "outcomes": cowork_graph_store.cancel(args.graph_id,
                                                  args.work_id)}
    if op == "fail":
        return args.graph_id, args.work_id, cowork_graph_store.fail(
            args.graph_id, args.work_id, args.reason_code)
    if op == "reclaim":
        return args.graph_id, args.work_id, cowork_graph_store.reclaim(
            args.graph_id, args.work_id)
    if op == "join":
        return args.graph_id, None, {
            "decision": cowork_graph_store.join(args.graph_id, args.join_id)}
    raise ValueError("unknown graph op %r" % (op,))


def main(argv=None, output=None):
    """Run one `cowork graph` op; writes one result line and returns its rc.
    `output` is a write callable (default: stdout, resolved at call time)."""
    if output is not None:
        write = output
    else:
        def write(text):
            sys.stdout.write(text)
            sys.stdout.flush()
    argv = list(sys.argv[1:] if argv is None else argv)
    named_op = argv[0] if argv and argv[0] in OPS else None
    try:
        args = build_graph_parser().parse_args(argv)
    except _ArgumentError as exc:
        return _emit(write, _line(named_op, 2, "refused", "argument_error",
                                  None, None, detail=str(exc)))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        return _emit(write, _line(named_op, 2, "refused", "argument_error",
                                  None, None))
    graph_id = getattr(args, "graph_id", None)
    work_id = getattr(args, "work_id", None)
    try:
        graph_id, work_id, fields = _run_op(args)
        line = _line(args.op, 0, "ok", None, graph_id, work_id, fields)
    except GraphRefusal as exc:
        if work_id is None and isinstance(exc.work_id, str):
            work_id = exc.work_id
        line = _line(args.op, exc.rc, "refused", exc.code, graph_id, work_id,
                     detail=exc.detail)
    except Exception:  # noqa: BLE001 - one truthful terminal result line
        traceback.print_exc(file=sys.stderr)
        line = _line(args.op, 1, "error", "io_error", graph_id, work_id)
    return _emit(write, line)


if __name__ == "__main__":
    raise SystemExit(main())
