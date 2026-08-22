#!/usr/bin/env python3
"""WorkUnit record schema and dependency-graph schema/validators.

M2 Package A — pure contracts. This module is inert infrastructure: it
performs no file or network I/O, spawns nothing, and imports no runtime
module other than its sibling `cowork_control_plane` (also pure/inert), used
only to validate `lifecycle_state` against the closed PhaseState taxonomy.

Public API:
    validate_work_unit(record) -> dict (normalized copy) or raises ValueError

    validate_graph_node(node) -> dict (normalized copy) or raises ValueError
    graph_node_from_work_unit(work_unit) -> dict
    validate_revision(nodes) -> tuple[dict] or raises GraphValidationError
    new_graph() -> tuple (empty)
    append_revision(graph, nodes) -> tuple (new graph with one revision appended)
    GraphValidationError

Records and node dicts are ordinary JSON-native dictionaries, matching the
convention already used by the repository's other pure-schema modules.
schema_version is integer 1 throughout. Every validator returns a normalized
copy and never mutates its input.

Candidate identity rule (fail-closed): only a candidate-bound WorkUnit may be
`completed` — `validate_work_unit` rejects `lifecycle_state == "completed"`
when `candidate_manifest_digest` is null, matching
`cowork_control_plane.advance`, whose only edge into `completed` already
requires gate evidence naming a real digest. `candidate_index` is coupled to
`candidate_manifest_digest`: a non-null `candidate_index` is never valid when
`candidate_manifest_digest` is null (there is no manifest for the index to
select into), but a non-null digest with a null index remains valid — a
manifest may legally have no index.
"""

import re

import cowork_control_plane as control_plane

SCHEMA_VERSION = 1

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')

# Whether/how a work unit governs children it spawns. `inherit` — children
# get the same governance as this work unit; `isolated` — children are
# governed independently; `denied` — this work unit may not spawn governed
# children at all.
GOVERNED_CHILD_POLICIES = frozenset({"inherit", "isolated", "denied"})


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------

def _check_exact_keys(record, expected_keys, record_name):
    extra = set(record) - expected_keys
    missing = expected_keys - set(record)
    if missing:
        raise ValueError("%s missing keys: %s" % (record_name, sorted(missing)))
    if extra:
        raise ValueError("%s has extra keys: %s" % (record_name, sorted(extra)))


def _check_schema_version(record):
    v = record.get("schema_version")
    if isinstance(v, bool) or v != SCHEMA_VERSION:
        raise ValueError(
            "schema_version must be integer %d, got %r" % (SCHEMA_VERSION, v))


def _check_uuid(value, field):
    """Validate a UUID-shaped string and return its canonical lowercase form.

    Accepts any casing (`_UUID_RE` is case-insensitive) but always returns
    the lowercase representation, so every caller that stores the return
    value — never the raw input — sees one canonical identity string. This
    is what makes downstream identity comparisons (duplicate detection,
    dangling-predecessor/self-edge/cycle/fan-in checks) case-consistent: two
    UUIDs that differ only in casing normalize to the same string and are
    therefore correctly treated as the same identity, not two different ones.
    """
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ValueError("%s must be a UUID-shaped string, got %r" % (field, value))
    return value.lower()


def _check_nonempty_str(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a nonempty string, got %r" % (field, value))


def _check_str_or_null(value, field):
    if value is not None and not isinstance(value, str):
        raise ValueError("%s must be a string or null, got %r" % (field, value))


def _check_hex64_or_null(value, field):
    if value is None:
        return
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        raise ValueError(
            "%s must be null or 64 lowercase hex chars, got %r" % (field, value))


def _check_nonneg_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("%s must be a nonnegative integer, got %r" % (field, value))


def _check_nonneg_int_or_null(value, field):
    if value is None:
        return
    _check_nonneg_int(value, field)


def _check_governed_child_policy(value, field):
    if value is not None and value not in GOVERNED_CHILD_POLICIES:
        raise ValueError(
            "%s must be null or one of %s, got %r"
            % (field, sorted(GOVERNED_CHILD_POLICIES), value))


# ---------------------------------------------------------------------------
# WorkUnit
# ---------------------------------------------------------------------------

_WORK_UNIT_KEYS = frozenset({
    "schema_version", "record",
    "work_id", "session_id", "phase",
    "role", "seat", "round", "attempt",
    "controller", "provider", "requested_model", "effective_model", "effort",
    "candidate_manifest_digest", "candidate_index",
    "prompt_digest", "pending_turn_digest",
    "parent_work_id", "governed_child_policy",
    "graph_revision", "predecessor_work_ids", "fan_join_id",
    "lifecycle_state", "terminal_reason",
})


def validate_work_unit(record):
    """Return a normalized copy of a WorkUnit record, or raise ValueError.

    Never mutates input. Rejects missing or extra keys. `lifecycle_state`
    must be a member of `cowork_control_plane.PHASE_STATE_SET`.
    `terminal_reason` must be a nonempty string exactly when `lifecycle_state`
    is a member of `cowork_control_plane.TERMINAL_STATES`, and null
    otherwise — a WorkUnit can never claim a terminal reason for a state it
    has not terminated in, nor terminate silently.

    Candidate identity is fail-closed: `lifecycle_state == "completed"`
    requires a non-null `candidate_manifest_digest` — only a candidate-bound
    WorkUnit may complete, matching the only edge into `completed` in
    `cowork_control_plane.advance`. `candidate_index` must be null whenever
    `candidate_manifest_digest` is null; a non-null digest with a null index
    remains valid (an indexless manifest is legal).

    Every UUID-shaped field (`work_id`, `session_id`, `parent_work_id`, each
    `predecessor_work_ids` entry) is accepted in any casing but normalized to
    canonical lowercase in the returned copy (see _check_uuid) — the input's
    original casing is not preserved, so two records naming the same
    identity in different casing normalize to the same output identity.
    """
    if not isinstance(record, dict):
        raise ValueError("WorkUnit must be a dict, got %r" % type(record))
    _check_exact_keys(record, _WORK_UNIT_KEYS, "WorkUnit")
    _check_schema_version(record)
    if record["record"] != "WorkUnit":
        raise ValueError("record field must be 'WorkUnit', got %r" % record["record"])

    work_id = _check_uuid(record["work_id"], "work_id")
    session_id = _check_uuid(record["session_id"], "session_id")
    _check_str_or_null(record["phase"], "phase")

    _check_nonempty_str(record["role"], "role")
    _check_nonneg_int(record["seat"], "seat")
    _check_nonneg_int(record["round"], "round")
    _check_nonneg_int(record["attempt"], "attempt")

    _check_nonempty_str(record["controller"], "controller")
    _check_nonempty_str(record["provider"], "provider")
    _check_str_or_null(record["requested_model"], "requested_model")
    _check_str_or_null(record["effective_model"], "effective_model")
    _check_str_or_null(record["effort"], "effort")

    _check_hex64_or_null(record["candidate_manifest_digest"], "candidate_manifest_digest")
    _check_nonneg_int_or_null(record["candidate_index"], "candidate_index")
    if record["candidate_manifest_digest"] is None and record["candidate_index"] is not None:
        raise ValueError(
            "candidate_index must be null when candidate_manifest_digest is null, "
            "got candidate_index=%r" % (record["candidate_index"],))

    _check_hex64_or_null(record["prompt_digest"], "prompt_digest")
    _check_hex64_or_null(record["pending_turn_digest"], "pending_turn_digest")

    parent_work_id = record["parent_work_id"]
    if parent_work_id is not None:
        parent_work_id = _check_uuid(parent_work_id, "parent_work_id")
    _check_governed_child_policy(record["governed_child_policy"], "governed_child_policy")

    _check_nonneg_int_or_null(record["graph_revision"], "graph_revision")
    predecessor_ids = record["predecessor_work_ids"]
    if not isinstance(predecessor_ids, (list, tuple)):
        raise ValueError(
            "predecessor_work_ids must be a list or tuple, got %r" % type(predecessor_ids))
    normalized_predecessor_ids = tuple(
        _check_uuid(pid, "predecessor_work_ids entry") for pid in predecessor_ids)
    _check_str_or_null(record["fan_join_id"], "fan_join_id")

    state = record["lifecycle_state"]
    if state not in control_plane.PHASE_STATE_SET:
        raise ValueError(
            "lifecycle_state must be one of %s, got %r"
            % (sorted(control_plane.PHASE_STATE_SET), state))

    if state == "completed" and record["candidate_manifest_digest"] is None:
        raise ValueError(
            "lifecycle_state 'completed' requires a non-null "
            "candidate_manifest_digest; a candidate-free WorkUnit may never complete")

    terminal_reason = record["terminal_reason"]
    if state in control_plane.TERMINAL_STATES:
        if not isinstance(terminal_reason, str) or not terminal_reason:
            raise ValueError(
                "terminal_reason must be a nonempty string when lifecycle_state=%r"
                % state)
    else:
        if terminal_reason is not None:
            raise ValueError(
                "terminal_reason must be null when lifecycle_state=%r" % state)

    normalized = dict(record)
    normalized["work_id"] = work_id
    normalized["session_id"] = session_id
    normalized["parent_work_id"] = parent_work_id
    normalized["predecessor_work_ids"] = normalized_predecessor_ids
    return normalized


# ---------------------------------------------------------------------------
# Dependency graph: node schema
# ---------------------------------------------------------------------------

_GRAPH_NODE_KEYS = frozenset({
    "work_id", "candidate_manifest_digest", "candidate_index",
    "governed_child_policy", "predecessor_work_ids",
})


def validate_graph_node(node):
    """Return a normalized copy of a dependency-graph node, or raise ValueError.

    A graph node is the projection of a WorkUnit needed by the graph
    validators: its identity, the candidate/policy identity fan-in must
    agree on, and its declared predecessors. Candidate identity is the PAIR
    `(candidate_manifest_digest, candidate_index)`, matching
    `cowork_control_plane._gate_evidence_matches_candidate`'s definition — a
    graph node carries both fields, not the digest alone. Never mutates
    input.

    `candidate_index` is coupled to `candidate_manifest_digest` exactly as in
    `validate_work_unit`: a non-null `candidate_index` is never valid when
    `candidate_manifest_digest` is null; a non-null digest with a null index
    remains valid.

    `work_id` and every `predecessor_work_ids` entry are normalized to
    canonical lowercase (see _check_uuid) in the returned copy — this is
    what makes duplicate/dangling/self-edge/cycle/fan-in detection in
    validate_revision case-consistent: it always compares the normalized
    form, never the caller's original casing.
    """
    if not isinstance(node, dict):
        raise ValueError("graph node must be a dict, got %r" % type(node))
    _check_exact_keys(node, _GRAPH_NODE_KEYS, "graph node")
    work_id = _check_uuid(node["work_id"], "work_id")
    _check_hex64_or_null(node["candidate_manifest_digest"], "candidate_manifest_digest")
    _check_nonneg_int_or_null(node["candidate_index"], "candidate_index")
    if node["candidate_manifest_digest"] is None and node["candidate_index"] is not None:
        raise ValueError(
            "candidate_index must be null when candidate_manifest_digest is null, "
            "got candidate_index=%r" % (node["candidate_index"],))
    _check_governed_child_policy(node["governed_child_policy"], "governed_child_policy")
    preds = node["predecessor_work_ids"]
    if not isinstance(preds, (list, tuple)):
        raise ValueError(
            "predecessor_work_ids must be a list or tuple, got %r" % type(preds))
    normalized_preds = tuple(
        _check_uuid(pid, "predecessor_work_ids entry") for pid in preds)
    return {
        "work_id": work_id,
        "candidate_manifest_digest": node["candidate_manifest_digest"],
        "candidate_index": node["candidate_index"],
        "governed_child_policy": node["governed_child_policy"],
        "predecessor_work_ids": normalized_preds,
    }


def graph_node_from_work_unit(work_unit):
    """Project a validated WorkUnit dict down to its dependency-graph node."""
    return validate_graph_node({
        "work_id": work_unit["work_id"],
        "candidate_manifest_digest": work_unit["candidate_manifest_digest"],
        "candidate_index": work_unit["candidate_index"],
        "governed_child_policy": work_unit["governed_child_policy"],
        "predecessor_work_ids": work_unit["predecessor_work_ids"],
    })


# ---------------------------------------------------------------------------
# Dependency graph: revision validation
# ---------------------------------------------------------------------------

class GraphValidationError(ValueError):
    """Raised when a dependency-graph revision fails structural validation.

    `violations` is a tuple of dicts, each `{"code": ..., "work_id": ...,
    "detail": ...}`. All violations found are collected before raising —
    a caller can enumerate every problem in the revision, not just the
    first one struck.
    """

    def __init__(self, violations):
        self.violations = tuple(violations)
        codes = ", ".join(v["code"] for v in self.violations) or "unknown"
        super().__init__("dependency graph revision rejected: %s" % codes)


def _find_cycle_participants(by_id):
    """Return the set of work_ids on a predecessor cycle, via 3-color DFS.

    Edges run predecessor -> node (a node's predecessors must complete
    before it). Only edges between two ids present in `by_id` are walked;
    self-edges are skipped here since they are reported separately as
    `self_edge`. Iterative to avoid relying on recursion depth.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {wid: WHITE for wid in by_id}
    on_cycle = set()

    for start in by_id:
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack = [(start, iter(by_id[start]["predecessor_work_ids"]))]
        while stack:
            node, preds_iter = stack[-1]
            advanced = False
            for pred in preds_iter:
                if pred == node or pred not in by_id:
                    continue
                if color[pred] == WHITE:
                    color[pred] = GRAY
                    path.append(pred)
                    stack.append((pred, iter(by_id[pred]["predecessor_work_ids"])))
                    advanced = True
                    break
                if color[pred] == GRAY:
                    idx = path.index(pred)
                    on_cycle.update(path[idx:])
            if advanced:
                continue
            color[node] = BLACK
            stack.pop()
            path.pop()
    return on_cycle


def validate_revision(nodes):
    """Validate one dependency-graph revision in isolation.

    Returns a tuple of normalized graph-node dicts on success. Raises
    GraphValidationError, carrying every violation found, when the revision
    contains a duplicate work_id, a dangling predecessor, a self-edge, a
    predecessor cycle, cross-candidate fan-in (a node whose >=2 known
    predecessors disagree on the candidate identity PAIR
    `(candidate_manifest_digest, candidate_index)` — two predecessors naming
    the same digest but a different index are different candidates, matching
    `cowork_control_plane._gate_evidence_matches_candidate`'s definition), or
    cross-policy fan-in (disagreement on `governed_child_policy`).

    Validation is scoped entirely to `nodes` — no prior revision is
    consulted or mutated, matching the append-only invariant.
    """
    normalized = [validate_graph_node(n) for n in nodes]
    violations = []

    work_ids = [n["work_id"] for n in normalized]
    seen = set()
    duplicate_ids = set()
    for wid in work_ids:
        if wid in seen:
            duplicate_ids.add(wid)
        seen.add(wid)
    for wid in sorted(duplicate_ids):
        violations.append({
            "code": "duplicate_work_id", "work_id": wid,
            "detail": "work_id appears more than once in revision",
        })

    known_ids = set(work_ids)
    by_id = {}
    for n in normalized:
        by_id.setdefault(n["work_id"], n)

    dangling_seen = set()
    for n in normalized:
        wid = n["work_id"]
        for p in n["predecessor_work_ids"]:
            if p == wid:
                continue
            if p not in known_ids and (wid, p) not in dangling_seen:
                dangling_seen.add((wid, p))
                violations.append({
                    "code": "dangling_predecessor", "work_id": wid,
                    "detail": "predecessor not present in revision: %s" % p,
                })

    self_edge_seen = set()
    for n in normalized:
        wid = n["work_id"]
        if wid in n["predecessor_work_ids"] and wid not in self_edge_seen:
            self_edge_seen.add(wid)
            violations.append({
                "code": "self_edge", "work_id": wid,
                "detail": "work_id is its own predecessor",
            })

    for wid in sorted(_find_cycle_participants(by_id)):
        violations.append({
            "code": "cycle", "work_id": wid,
            "detail": "work_id participates in a predecessor cycle",
        })

    for n in normalized:
        wid = n["work_id"]
        known_preds = [
            by_id[p] for p in n["predecessor_work_ids"]
            if p in by_id and p != wid
        ]
        if len(known_preds) < 2:
            continue
        candidates = {
            (p["candidate_manifest_digest"], p["candidate_index"]) for p in known_preds}
        if len(candidates) > 1:
            violations.append({
                "code": "cross_candidate_fan_in", "work_id": wid,
                "detail": (
                    "fan-in predecessors disagree on "
                    "(candidate_manifest_digest, candidate_index)"),
            })
        policies = {p["governed_child_policy"] for p in known_preds}
        if len(policies) > 1:
            violations.append({
                "code": "cross_policy_fan_in", "work_id": wid,
                "detail": "fan-in predecessors disagree on governed_child_policy",
            })

    if violations:
        raise GraphValidationError(violations)
    return tuple(normalized)


def new_graph():
    """Return an empty, immutable dependency graph (no revisions yet)."""
    return ()


def append_revision(graph, nodes):
    """Validate `nodes` as a new revision and return a NEW graph with it appended.

    Never mutates `graph`; the return value is `graph` plus one appended
    revision dict `{"schema_version": 1, "record": "DependencyGraphRevision",
    "graph_revision": N, "nodes": (...)}` where N is `len(graph) + 1`. Each
    revision is validated independently and in full isolation from every
    other revision (see validate_revision) — persisting a new revision never
    reinterprets or mutates a prior one, satisfying the append-only
    invariant.
    """
    if not isinstance(graph, tuple):
        raise ValueError("graph must be a tuple of prior revisions, got %r" % type(graph))
    revision_number = len(graph) + 1
    normalized_nodes = validate_revision(nodes)
    new_revision = {
        "schema_version": SCHEMA_VERSION,
        "record": "DependencyGraphRevision",
        "graph_revision": revision_number,
        "nodes": normalized_nodes,
    }
    return graph + (new_revision,)
