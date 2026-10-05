#!/usr/bin/env python3
"""Governed parallel work graph: durable store (issue #75, package P2).

The I/O layer over the pure kernel in `cowork_graph`. Every operation gathers
the external facts the kernel takes as injected values (root probes, the
sessions-root probe, other graphs' active roots, authority bytes, holder
facts, owned-verification transaction facts, live manifest fingerprints),
then runs exactly ONE `cowork_graph` transition inside one
`cowork_state._locked_json_transaction` on
`<sessions_root>/graphs/<graph_id>/graph.json`. The store adds no state
machine, no reason code and no exit code of its own.

Layout (all under `cowork_state.graphs_root()`):
    registry.json              {schema_version: 1, graph_ids: [sorted ids]}
    <graph_id>/graph.json      the single current GraphState record
    session-index/<uuid>.json  {schema_version: 1, graph_id, work_id,
                                lease_epoch, bound_at}, created O_EXCL and
                                never rewritten or deleted

Lock order: registry.json.lock -> graph.json.lock -> (owner-lease and
PauseLease locks, taken briefly inside reads). Every admit holds the registry
lock across foreign-root collection, the pure admit and the graph write. A
NEW graph takes no graph lock: its directory and graph.json are written only
after the pure admit accepted it. No path takes the registry lock while it
holds a graph lock.

Existence gate: a graph exists only when its id is listed in the registry AND
its graph.json is present. The check runs before any graph lock is taken, so
an unknown id creates nothing on disk (graph_unknown). An orphan directory
left by a crash between the graph.json write and the registry write is
therefore inert. A listed graph without its file is graph_state_corrupt.

Write-only-if-changed: a transition whose result equals the state read under
the lock writes nothing, so idempotent replays leave graph.json
byte-identical, and a refusal raises before any write.

Error mapping: `cowork_graph.GraphRefusal` passes through unchanged;
TimeoutError -> lock_timeout, cowork_state.CorruptRecordError ->
graph_state_corrupt, any other OSError -> io_error (all rc 1).

Out of scope here: the `cowork graph` CLI, the --graph-vertex flag, the
run_flow / resume-trigger fences, owner-lease acquisition for publication
and the 'graph_publish' entry point. `publish` only fences with
`cowork_owner.assert_owner` inside the graph lock.

Python 3.9+, stdlib only.
"""

import datetime
import functools
import json
import os
import re
import stat
import subprocess
import uuid

import cowork_capacity_scheduler
import cowork_graph
import cowork_owner
import cowork_state
import cowork_verification

GIT_TIMEOUT_S = 30
INDEX_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1

_INDEX_KEYS = frozenset({"schema_version", "graph_id", "work_id",
                         "lease_epoch", "bound_at"})
_REGISTRY_KEYS = frozenset({"schema_version", "graph_ids"})
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_LEASE_ALREADY_CLEAN = ("not_found", "not_cancellable")

GraphRefusal = cowork_graph.GraphRefusal
CorruptRecordError = cowork_state.CorruptRecordError


# --------------------------------------------------------------------------- #
# Plumbing: clock, error mapping, ids, registry and graph reads.              #
# --------------------------------------------------------------------------- #


def _now(now):
    """The injected `now`, or the UTC clock in the kernel's ISO-8601 form."""
    if now is not None:
        return now
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _mapped(fn, *args, **kwargs):
    """Run `fn`, mapping store failures onto the closed rc-1 codes.
    TimeoutError is an OSError subclass, so it is caught first."""
    try:
        return fn(*args, **kwargs)
    except GraphRefusal:
        raise
    except TimeoutError as exc:
        raise GraphRefusal("lock_timeout", detail=str(exc)) from exc
    except CorruptRecordError as exc:
        raise GraphRefusal("graph_state_corrupt", detail=str(exc)) from exc
    except OSError as exc:
        raise GraphRefusal("io_error", detail=str(exc)) from exc


def _store_op(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return _mapped(fn, *args, **kwargs)
    return wrapper


def _is_canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _check_graph_id(graph_id):
    if not _is_canonical_uuid(graph_id):
        raise GraphRefusal("argument_error", detail="graph_id")


def _check_session(session_uuid):
    try:
        cowork_state._assert_safe_identifier(session_uuid, "session_uuid")
    except ValueError:
        raise GraphRefusal("argument_error", detail="session_uuid")


def _empty_registry():
    return {"schema_version": REGISTRY_SCHEMA_VERSION, "graph_ids": []}


def _valid_registry(data):
    """The registry record, or the empty one when absent. Any other shape is
    corrupt."""
    if data is None:
        return _empty_registry()
    ids = data.get("graph_ids")
    version = data.get("schema_version")
    if (set(data) != _REGISTRY_KEYS or isinstance(version, bool)
            or version != REGISTRY_SCHEMA_VERSION
            or not isinstance(ids, list)
            or not all(_is_canonical_uuid(g) for g in ids)
            or ids != sorted(set(ids))):
        raise CorruptRecordError("%s: not a valid graph registry"
                                 % cowork_state.graph_registry_path())
    return data


def _read_registry():
    return _valid_registry(cowork_state._read_json_or_raise_if_corrupt(
        cowork_state.graph_registry_path()))


def _require_registered(graph_id, registry=None):
    """The existence gate: a canonical id, listed in the registry, with its
    graph.json present. Never creates anything."""
    _check_graph_id(graph_id)
    if registry is None:
        registry = _read_registry()
    if graph_id not in registry["graph_ids"]:
        raise GraphRefusal("graph_unknown", detail=graph_id)
    if not os.path.exists(cowork_state.graph_state_path_for(graph_id)):
        raise CorruptRecordError("graph %s is registered but has no "
                                 "graph.json" % graph_id)


def _read_graph(graph_id):
    """Strict lockless read of a registered graph's record. Graph writes are
    atomic replaces, so this never sees a torn file."""
    data = cowork_state._read_json_or_raise_if_corrupt(
        cowork_state.graph_state_path_for(graph_id))
    if data is None or data.get("graph_id") != graph_id:
        raise CorruptRecordError("graph %s: graph.json is missing or names "
                                 "another graph" % graph_id)
    cowork_graph.check_invariants(data)
    return data


def _graph_txn(graph_id, step):
    """One locked read-transition-write on graph.json. Callers run
    `_require_registered(graph_id)` first. `step(state)` returns the new
    state; an equal state writes nothing."""
    def mutate(existing):
        if existing is None or existing.get("graph_id") != graph_id:
            raise CorruptRecordError("graph %s: graph.json is missing or "
                                     "names another graph" % graph_id)
        cowork_graph.check_invariants(existing)
        new = step(existing)
        return None if new == existing else new
    return cowork_state._locked_json_transaction(
        cowork_state.graph_state_path_for(graph_id), mutate)


def _declaration(state, work_id, revision):
    for vertex in state["revisions"][revision - 1]["vertices"]:
        if vertex["work_id"] == work_id:
            return vertex
    raise GraphRefusal("graph_state_inconsistent", work_id,
                       "vertex missing from revision %d" % revision)


def _latest_declaration(state, work_id):
    for entry in reversed(state["revisions"]):
        for vertex in entry["vertices"]:
            if vertex["work_id"] == work_id:
                return vertex
    return None


def _vertex_root(state, work_id):
    """The declared root of the vertex's claim revision, or None for an
    unknown or unclaimed vertex."""
    record = (state["vertices"].get(work_id)
              if isinstance(work_id, str) else None)
    if record is None or record["claim_revision"] is None:
        return None
    return _declaration(state, work_id, record["claim_revision"])["root"]


# --------------------------------------------------------------------------- #
# Fact gathering.                                                             #
# --------------------------------------------------------------------------- #


def _dev_ino(path):
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return [st.st_dev, st.st_ino]


def _ancestors(realpath):
    """[[dev, ino], ...] of every parent of `realpath` up to and including
    '/'. Parents that cannot be stat'ed are skipped."""
    out = []
    if not isinstance(realpath, str):
        return out
    current = realpath
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            break
        pair = _dev_ino(parent)
        if pair is not None:
            out.append(pair)
        current = parent
    return out


def _git(args, cwd):
    """Stripped stdout of a bounded git call, or None on any failure."""
    try:
        proc = subprocess.run(["git"] + list(args), cwd=cwd,
                              capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_rc(args, cwd):
    """The exit status of a bounded git call, or None when it did not run."""
    try:
        proc = subprocess.run(["git"] + list(args), cwd=cwd,
                              capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return proc.returncode


def _path_has_symlink(path):
    """True when any cumulative component of the absolute `path` is a
    symlink; stops at the first missing component."""
    prefix = ""
    for part in [p for p in path.split("/") if p]:
        prefix += "/" + part
        try:
            mode = os.lstat(prefix).st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            return True
    return False


def probe_root(path):
    """A RootProbe for one declared vertex root. Every field that cannot be
    established is None/False, which the kernel's root checks refuse."""
    probe = {key: None for key in cowork_graph._PROBE_KEYS}
    probe.update(declared=path, exists=False, is_dir=False,
                 path_has_symlink=False, ancestors=[], anchor_ignored=False)
    if not isinstance(path, str) or not path.startswith("/") or (
            "\x00" in path):
        return probe
    realpath = os.path.realpath(path)
    probe["realpath"] = realpath
    probe["path_has_symlink"] = _path_has_symlink(path)
    try:
        st = os.stat(realpath)
    except OSError:
        st = None
    if st is not None:
        probe.update(exists=True, is_dir=stat.S_ISDIR(st.st_mode),
                     dev=st.st_dev, ino=st.st_ino)
    probe["ancestors"] = _ancestors(realpath)
    if not probe["is_dir"]:
        return probe
    toplevel = _git(["rev-parse", "--show-toplevel"], realpath)
    if toplevel:
        probe["toplevel_realpath"] = os.path.realpath(toplevel)
    head = _git(["rev-parse", "HEAD"], realpath)
    if head and _HEX40_RE.match(head):
        probe["head_commit"] = head
    common = _git(["rev-parse", "--git-common-dir"], realpath)
    if common:
        resolved = os.path.realpath(os.path.join(realpath, common))
        if os.path.basename(resolved) == ".git":
            main = os.path.dirname(resolved)
            probe["main_checkout_realpath"] = main
            probe["main_checkout_dev_ino"] = _dev_ino(main)
    probe["anchor_ignored"] = _git_rc(
        ["check-ignore", "-q", ".cowork/"], realpath) == 0
    return probe


def probe_sessions_root():
    """{realpath, dev, ino, ancestors} of the sessions root. Read-only."""
    realpath = os.path.realpath(cowork_state.sessions_root())
    pair = _dev_ino(realpath)
    return {"realpath": realpath,
            "dev": pair[0] if pair else None,
            "ino": pair[1] if pair else None,
            "ancestors": _ancestors(realpath)}


def _index_identity(record):
    return (record.get("schema_version"), record.get("graph_id"),
            record.get("work_id"), record.get("lease_epoch"))


def _fsync_dir(dirname):
    fd = os.open(dirname, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_exclusive_durable(path, record):
    """Create `path` exclusively (O_CREAT|O_EXCL) and fsync the file and its
    parent directory. Returns 'created', 'identical' (an existing record
    with the same identity tuple; bound_at is ignored) or 'conflict' (a
    different or unreadable existing record). Never rewrites or deletes."""
    dirname = os.path.dirname(path)
    os.makedirs(dirname, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            with open(path, "r") as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            return "conflict"
        if isinstance(existing, dict) and (
                _index_identity(existing) == _index_identity(record)):
            return "identical"
        return "conflict"
    with os.fdopen(fd, "w") as fh:
        json.dump(record, fh, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_dir(dirname)
    return "created"


def _foreign_active_roots(graph_ids):
    """Root identities of every non-terminal vertex of the listed graphs,
    read strictly: a missing, unreadable or invalid listed graph refuses
    (fail closed) instead of hiding its roots."""
    out = []
    for gid in sorted(graph_ids):
        data = _read_graph(gid)
        for work_id in sorted(data["vertices"]):
            if data["vertices"][work_id]["state"] in (
                    cowork_graph.TERMINAL_STATES):
                continue
            decl = _latest_declaration(data, work_id)
            if decl is None:
                raise CorruptRecordError("graph %s: vertex %s has no "
                                         "declaration" % (gid, work_id))
            realpath = decl["root_realpath"]
            ancestors = _ancestors(realpath)
            out.append({"graph_id": gid, "work_id": work_id,
                        "realpath": realpath, "dev": decl["root_dev"],
                        "ino": decl["root_ino"], "ancestors": ancestors})
            current = _dev_ino(realpath)
            if current is not None and current != [decl["root_dev"],
                                                   decl["root_ino"]]:
                out.append({"graph_id": gid, "work_id": work_id,
                            "realpath": realpath, "dev": current[0],
                            "ino": current[1], "ancestors": ancestors})
    return out


def _read_authority(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (OSError, TypeError, ValueError):
        return None


def holder_facts(session_uuid):
    """{owner_verdict, pause_lease_live} for a vertex holder. Liveness comes
    only from `cowork_owner.classify_owner_lease`; an unreadable PauseLease
    means corrupt and paused (fail closed)."""
    if session_uuid is None:
        return {"owner_verdict": cowork_graph.NO_SESSION,
                "pause_lease_live": False}
    try:
        live = cowork_state.live_pause_lease_ids(session_uuid)
        verdict = cowork_owner.classify_owner_lease(session_uuid, probe=True)
    except (CorruptRecordError, OSError, ValueError):
        return {"owner_verdict": "corrupt", "pause_lease_live": True}
    return {"owner_verdict": verdict, "pause_lease_live": bool(live)}


def _nested(doc, *keys):
    for key in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def gather_txn_facts(session_uuid):
    """txn_facts for the child's current owned-verification transaction, or
    None when there is no pointer, no safe transaction id, or no request /
    result artifact (the kernel refuses receipt_no_accepted_transaction)."""
    pointer = cowork_state.read_current_receipt_pointer(session_uuid)
    if not isinstance(pointer, dict):
        return None
    txn = pointer.get("transaction_id")
    try:
        cowork_state._assert_safe_identifier(txn, "transaction_id")
    except ValueError:
        return None
    request = cowork_state.read_json_tolerant(
        cowork_state.verification_request_path_for(session_uuid, txn))
    result = cowork_state.read_json_tolerant(
        cowork_state.verification_result_path_for(session_uuid, txn))
    if request is None or result is None:
        return None
    entry = cowork_state.read_verification_dispositions(session_uuid).get(txn)
    inventory = request.get("inventory")
    return {
        "transaction_id": txn,
        "request_session_uuid": request.get("session_uuid"),
        "request_repo_dev_ino": _dev_ino(request.get("repo")),
        "request_manifest_digest": _nested(request, "snapshot",
                                           "manifest_digest"),
        "result_manifest_digest": _nested(result, "snapshot",
                                          "manifest_digest"),
        "verdict": result.get("verdict"),
        "final_suite_binding": result.get("final_suite_binding"),
        "disposition": (entry.get("disposition")
                        if isinstance(entry, dict) else None),
        "inventory_labels": [e.get("label") for e in (
            inventory if isinstance(inventory, list) else [])
            if isinstance(e, dict)],
    }


def live_fingerprint(root):
    """The live candidate manifest fingerprint of `root`, or None when it
    cannot be enumerated (the kernel refuses receipt_candidate_changed)."""
    if not isinstance(root, str):
        return None
    try:
        return cowork_verification.manifest_fingerprint(
            cowork_verification.candidate_manifest(root))
    except (cowork_verification.SnapshotRaceError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Admission.                                                                  #
# --------------------------------------------------------------------------- #


@_store_op
def admit(revision_doc, graph_id=None, now=None):
    """Admit a revision into a NEW graph (graph_id None) or an existing one.
    Returns {graph_id, graph_revision, effective_cap}."""
    now = _now(now)
    normalized = cowork_graph.normalize_revision_document(revision_doc)
    if graph_id is not None:
        _check_graph_id(graph_id)
    probes = {v["work_id"]: probe_root(v["root"])
              for v in normalized["vertices"]}
    authority = {v["work_id"]: _read_authority(v["authority_path"])
                 for v in normalized["vertices"]}
    os.makedirs(cowork_state.sessions_root(), exist_ok=True)
    sessions_probe = probe_sessions_root()
    box = {}

    def mutate(existing):
        registry = _valid_registry(existing)
        ids = list(registry["graph_ids"])
        if graph_id is not None:
            _require_registered(graph_id, registry)
            foreign = _foreign_active_roots([g for g in ids if g != graph_id])
            box["state"] = _graph_txn(graph_id, lambda st: cowork_graph.admit(
                st, revision_doc, probes, sessions_probe, foreign, authority,
                now))
            box["graph_id"] = graph_id
            return None
        new_id = str(uuid.uuid4())
        foreign = _foreign_active_roots(ids)
        state = cowork_graph.admit(
            cowork_graph.new_state(new_id), revision_doc, probes,
            sessions_probe, foreign, authority, now)
        if not cowork_state.write_json_atomic_durable(
                cowork_state.graph_state_path_for(new_id), state):
            raise OSError("write failed for graph %s" % new_id)
        box["state"] = state
        box["graph_id"] = new_id
        return {"schema_version": REGISTRY_SCHEMA_VERSION,
                "graph_ids": sorted(ids + [new_id])}

    cowork_state._locked_json_transaction(cowork_state.graph_registry_path(),
                                          mutate)
    state = box["state"]
    return {"graph_id": box["graph_id"], "graph_revision": state["revision"],
            "effective_cap": cowork_graph.effective_cap(state)}


# --------------------------------------------------------------------------- #
# Slots and binding.                                                          #
# --------------------------------------------------------------------------- #


@_store_op
def claim(graph_id, work_id=None, now=None):
    """Acquire one slot; returns the kernel's claim_info."""
    now = _now(now)
    _require_registered(graph_id)
    box = {}

    def step(st):
        new, box["info"] = cowork_graph.claim(st, work_id, now)
        return new

    _graph_txn(graph_id, step)
    return box["info"]


def _index_conflict(session_uuid, graph_id, work_id, lease_epoch):
    """True when the session already has an index record naming a different
    (graph, vertex, epoch), or an unreadable one."""
    try:
        existing = cowork_state._read_json_or_raise_if_corrupt(
            cowork_state.graph_session_index_path_for(session_uuid))
    except CorruptRecordError:
        return True
    if existing is None:
        return False
    return _index_identity(existing) != (INDEX_SCHEMA_VERSION, graph_id,
                                         work_id, lease_epoch)


@_store_op
def check_bind_preconditions(graph_id, work_id, lease_epoch, cwd, profile,
                             session_uuid=None):
    """Read-only: raise the refusal `bind_session` would raise, else None.
    Without a session uuid a fresh placeholder stands in for it."""
    if session_uuid is not None:
        _check_session(session_uuid)
    _require_registered(graph_id)
    state = _read_graph(graph_id)
    conflict = False
    if session_uuid is None:
        session_uuid = str(uuid.uuid4())
    else:
        conflict = _index_conflict(session_uuid, graph_id, work_id,
                                   lease_epoch)
    cowork_graph.bind(state, work_id, lease_epoch, session_uuid,
                      _dev_ino(cwd), profile, conflict)
    return None


@_store_op
def bind_session(graph_id, work_id, lease_epoch, session_uuid, cwd, profile,
                 now=None):
    """Bind a child session to a claimed vertex. The session-index record is
    created inside the graph lock before the graph record is written."""
    now = _now(now)
    _check_session(session_uuid)
    _require_registered(graph_id)
    cwd_pair = _dev_ino(cwd)
    index_path = cowork_state.graph_session_index_path_for(session_uuid)

    def step(st):
        conflict = _index_conflict(session_uuid, graph_id, work_id,
                                   lease_epoch)
        new = cowork_graph.bind(st, work_id, lease_epoch, session_uuid,
                                cwd_pair, profile, conflict, now)
        outcome = _create_exclusive_durable(index_path, {
            "schema_version": INDEX_SCHEMA_VERSION, "graph_id": graph_id,
            "work_id": work_id, "lease_epoch": lease_epoch,
            "bound_at": now})
        if outcome == "conflict":
            raise GraphRefusal("session_already_bound", work_id, session_uuid)
        return new

    _graph_txn(graph_id, step)
    return {"graph_id": graph_id, "work_id": work_id,
            "lease_epoch": lease_epoch, "session_uuid": session_uuid}


@_store_op
def vertex_binding_for_session(session_uuid):
    """The session's index record, or None when it is unbound. Read-only;
    never creates a directory."""
    _check_session(session_uuid)
    path = cowork_state.graph_session_index_path_for(session_uuid)
    record = cowork_state._read_json_or_raise_if_corrupt(path)
    if record is None:
        return None
    epoch = record.get("lease_epoch")
    if (set(record) != _INDEX_KEYS
            or record["schema_version"] != INDEX_SCHEMA_VERSION
            or not _is_canonical_uuid(record["graph_id"])
            or not isinstance(record["work_id"], str)
            or isinstance(epoch, bool) or not isinstance(epoch, int)):
        raise CorruptRecordError("%s: not a valid session-index record"
                                 % path)
    return record


@_store_op
def fence(session_uuid, now=None):
    """None for an unbound session; otherwise the holder fence. A bind that
    crashed after its index record completes here in the same transaction.
    Returns {graph_id, work_id, lease_epoch, session_uuid, bind_completed}."""
    binding = vertex_binding_for_session(session_uuid)
    if binding is None:
        return None
    now = _now(now)
    graph_id = binding["graph_id"]
    work_id = binding["work_id"]
    epoch = binding["lease_epoch"]
    _require_registered(graph_id)
    box = {"bind_completed": False}

    def step(st):
        verdict = cowork_graph.assert_holder(st, work_id, epoch, session_uuid)
        if verdict == cowork_graph.HOLDER_OK:
            return st
        decl = _declaration(st, work_id,
                            st["vertices"][work_id]["claim_revision"])
        new = cowork_graph.bind(st, work_id, epoch, session_uuid,
                                [decl["root_dev"], decl["root_ino"]],
                                decl["profile"], False, now)
        box["bind_completed"] = True
        return new

    _graph_txn(graph_id, step)
    return {"graph_id": graph_id, "work_id": work_id, "lease_epoch": epoch,
            "session_uuid": session_uuid,
            "bind_completed": box["bind_completed"]}


# --------------------------------------------------------------------------- #
# Receipts.                                                                   #
# --------------------------------------------------------------------------- #


@_store_op
def publish(graph_id, work_id, session_uuid, owner_id, owner_epoch, now=None):
    """Publish the vertex receipt from the child's accepted transaction.
    Facts and the live fingerprint are gathered outside the lock; the owner
    fence and the kernel publish run inside it. Acquires no lease."""
    now = _now(now)
    _check_session(session_uuid)
    _require_registered(graph_id)
    txn = gather_txn_facts(session_uuid)
    fingerprint = live_fingerprint(_vertex_root(_read_graph(graph_id),
                                                work_id))
    box = {}

    def step(st):
        try:
            cowork_owner.assert_owner(session_uuid, owner_id, owner_epoch)
            owner_ok = True
        except cowork_owner.OwnerLeaseError:
            owner_ok = False
        receipt = cowork_graph.build_receipt(st, work_id, session_uuid,
                                             owner_id, owner_epoch, txn, now)
        new = cowork_graph.publish(st, receipt, txn, fingerprint, owner_ok,
                                   now)
        box["outcome"] = ("already_published" if new == st
                          else "published")
        box["receipt"] = new["vertices"][work_id]["receipt"]
        return new

    _graph_txn(graph_id, step)
    return {"outcome": box["outcome"],
            "receipt_identity": list(cowork_graph.receipt_identity(
                box["receipt"])),
            "session_uuid": session_uuid, "slot_released": True}


@_store_op
def _publish_session(graph_id, work_id):
    """The vertex's bound session uuid (lockless strict read), or None."""
    _require_registered(graph_id)
    record = (_read_graph(graph_id)["vertices"].get(work_id)
              if isinstance(work_id, str) else None)
    return None if record is None else record["session_uuid"]


# --------------------------------------------------------------------------- #
# Holder lifecycle.                                                           #
# --------------------------------------------------------------------------- #


def _error_detail(exc):
    return "%s: %s" % (type(exc).__name__, exc)


def _cleanup_pause_leases(session_uuid):
    """Cancel every live PauseLease of the session with its own stored
    automation_ref. Leases a concurrent or earlier cancel already finished
    are 'already_clean'; any other failure is reported, never raised."""
    try:
        lease_ids = cowork_state.live_pause_lease_ids(session_uuid)
    except Exception as exc:
        return [{"lease_id": None, "result": "error",
                 "detail": _error_detail(exc)}]
    out = []
    for lease_id in lease_ids:
        try:
            stored = cowork_state.read_pause_lease(session_uuid, lease_id)
            if stored is None:
                out.append({"lease_id": lease_id, "result": "error",
                            "detail": "PauseLease is unreadable or invalid"})
                continue
            cowork_capacity_scheduler.cancel(session_uuid, lease_id,
                                             stored["automation_ref"])
            out.append({"lease_id": lease_id, "result": "cancelled"})
        except cowork_capacity_scheduler.SchedulerLeaseConflict as exc:
            if exc.reason in _LEASE_ALREADY_CLEAN:
                out.append({"lease_id": lease_id, "result": "already_clean"})
            else:
                out.append({"lease_id": lease_id, "result": "error",
                            "detail": _error_detail(exc)})
        except Exception as exc:
            out.append({"lease_id": lease_id, "result": "error",
                        "detail": _error_detail(exc)})
    return out


def _running_session(state, work_id):
    record = (state["vertices"].get(work_id)
              if isinstance(work_id, str) else None)
    if record is None or record["state"] != "running":
        return None
    return record["session_uuid"]


@_store_op
def cancel(graph_id, work_id=None, now=None):
    """Cancel one vertex or the whole graph. Holder facts are gathered inside
    the lock; PauseLease cleanup runs after the write, outside the lock, for
    every cancelled or already-cancelled vertex that has a session.
    Returns [{work_id, outcome, pause_cleanup}] sorted by work_id."""
    now = _now(now)
    _require_registered(graph_id)
    box = {}

    def step(st):
        facts = {w: holder_facts(r["session_uuid"])
                 for w, r in st["vertices"].items()
                 if r["state"] == "running"
                 and (work_id is None or w == work_id)}
        new, box["outcomes"] = cowork_graph.cancel(st, work_id, facts, now)
        box["state"] = new
        return new

    _graph_txn(graph_id, step)
    results = []
    for outcome in sorted(box["outcomes"], key=lambda o: o["work_id"]):
        entry = dict(outcome)
        session = box["state"]["vertices"][outcome["work_id"]]["session_uuid"]
        if outcome["outcome"] in ("cancelled", "already_cancelled") and (
                session is not None):
            entry["pause_cleanup"] = _cleanup_pause_leases(session)
        else:
            entry["pause_cleanup"] = []
        results.append(entry)
    return results


@_store_op
def fail(graph_id, work_id, reason_code, now=None):
    """Record a claimed/running vertex as failed (non-live holder only)."""
    now = _now(now)
    _require_registered(graph_id)

    def step(st):
        facts = holder_facts(_running_session(st, work_id))
        return cowork_graph.fail(st, work_id, reason_code, facts, now)

    _graph_txn(graph_id, step)
    return {"state": "failed", "slot_released": True}


@_store_op
def reclaim(graph_id, work_id, now=None):
    """Release an abandoned claim. Returns {new_lease_epoch, holder_verdict
    (None for a claimed vertex), slot_released}."""
    now = _now(now)
    _require_registered(graph_id)
    box = {}

    def step(st):
        session = _running_session(st, work_id)
        facts = holder_facts(session)
        new = cowork_graph.reclaim(st, work_id, facts, now)
        box["verdict"] = facts["owner_verdict"] if session else None
        box["epoch"] = new["vertices"][work_id]["lease_epoch"]
        return new

    _graph_txn(graph_id, step)
    return {"new_lease_epoch": box["epoch"], "holder_verdict": box["verdict"],
            "slot_released": True}


# --------------------------------------------------------------------------- #
# Join and status.                                                            #
# --------------------------------------------------------------------------- #


def _canonical_or_none(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


@_store_op
def join(graph_id, join_id, now=None):
    """Store (or return) the join decision. Live fingerprints of the
    published members are computed outside the lock and re-validated by
    the kernel inside it."""
    now = _now(now)
    _require_registered(graph_id)
    snapshot = _read_graph(graph_id)
    canonical = _canonical_or_none(join_id)
    fingerprints = {}
    if snapshot["revision"]:
        current = snapshot["revisions"][snapshot["revision"] - 1]
        for declared in current["joins"]:
            if declared["join_id"] != canonical:
                continue
            for member in declared["requires"]:
                record = snapshot["vertices"].get(member)
                if record is not None and record["receipt"] is not None:
                    fingerprints[member] = live_fingerprint(
                        _vertex_root(snapshot, member))
    box = {}

    def step(st):
        new, box["decision"] = cowork_graph.join(st, join_id, fingerprints,
                                                 now)
        return new

    _graph_txn(graph_id, step)
    return box["decision"]


@_store_op
def status(graph_id):
    """The kernel's read-only status view. Takes no lock."""
    _require_registered(graph_id)
    return cowork_graph.status_view(_read_graph(graph_id))
