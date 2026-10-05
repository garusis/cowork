#!/usr/bin/env python3
"""Governed parallel work graph: pure contracts (issue #75, package P1).

This module owns the graph kernel a later store and CLI consume: the closed
reason-code table, revision admission, the slot ledger, the holder lifecycle
(cancel / fail / reclaim), vertex receipts and the deterministic join
decision. It never persists, launches or merges anything.

Pure and stdlib-only: no filesystem, process, socket or clock access. Every
time value is an injected ISO-8601 UTC `now` string and every external fact
(root probes, authority bytes, holder liveness, transaction facts, live
manifest fingerprints) is an injected value. The only sibling imports are
`cowork_workunit` (structural graph validation) and
`cowork_execution_profiles` (per-vertex policy, read at call time). Owner,
final-suite and disposition vocabularies are mirrored as literals; unknown
values fail closed.

Every transition deep-copies its input, runs `check_invariants` on the copy
and on the result, and returns a new state; the caller's object is never
touched. Every refusal raises `GraphRefusal` whose `code` is a member of the
closed `REASON_RC` table.

Public API:
    new_state(graph_id) -> state
    admit(state, revision_doc, probes, sessions_root_probe,
          foreign_active_roots, authority_bytes, now, policy_fn=None) -> state
    claim(state, work_id, now) -> (state, claim_info)
    bind(state, work_id, lease_epoch, session_uuid, cwd_dev_ino, profile,
         session_index_conflict, now=None) -> state
    assert_holder(state, work_id, lease_epoch, session_uuid)
        -> 'ok' | 'bind_incomplete'
    cancel(state, work_id, holder_facts_by_work_id, now) -> (state, outcomes)
    fail(state, work_id, reason_code, holder_facts, now) -> state
    reclaim(state, work_id, holder_facts, now) -> state
    required_check_evidence(policy, txn_facts) -> {check: bool}
    build_receipt(state, work_id, session_uuid, owner_id, owner_epoch,
                  txn_facts, now, policy_fn=None) -> VertexReceipt
    validate_receipt(state, receipt, txn_facts, live_fingerprint, owner_ok,
                     policy_fn=None) -> 'valid' | 'already_published'
    publish(state, receipt, txn_facts, live_fingerprint, owner_ok, now,
            policy_fn=None) -> state
    reduce_join(state, join_id, live_fingerprints) -> JoinDecision
    join(state, join_id, live_fingerprints, now) -> (state, JoinDecision)
    derive_statuses / ready_order / held_slots / effective_cap /
    check_invariants / status_view
    normalize_revision_document / structural_check / check_roots /
    check_authorities / policy_cap
    canonical_json / digest / receipt_identity

Interface shapes (ids are lowercase UUID strings; a (dev, ino) pair is a
2-element JSON list of ints):
    revision_document: {schema_version: 1, max_parallel, claim_ttl_s?
        (int in [60, 86400], default 900), vertices: [{work_id, root,
        base_commit, authority_path, authority_digest, profile,
        predecessors}], joins?: [{join_id, rule: 'all_succeeded',
        requires}]}
    probes: {work_id: RootProbe} for every active vertex; RootProbe =
        {declared, realpath, exists, is_dir, dev, ino, path_has_symlink,
        ancestors: [[dev, ino], ...], toplevel_realpath|null,
        head_commit|null, main_checkout_realpath|null,
        main_checkout_dev_ino|null, anchor_ignored}
    sessions_root_probe: {realpath, dev, ino, ancestors}
    foreign_active_roots: [{graph_id, work_id, realpath, dev, ino,
        ancestors}] for non-terminal vertices of other graphs
    authority_bytes: {work_id: bytes|None} for every active vertex
    holder_facts: {owner_verdict: unowned|live_owner|stale_dead_owner|
        stale_unproven|corrupt|no_session, pause_lease_live: bool}
    txn_facts: {transaction_id, request_session_uuid,
        request_repo_dev_ino|null, request_manifest_digest,
        result_manifest_digest, verdict, final_suite_binding,
        disposition|null, inventory_labels: [str]} or None
    policy_fn: callable(profile, role) -> resolved_vertex_policy-shaped
        dict; None means cowork_execution_profiles.resolved_vertex_policy
    claim_info: {graph_id, work_id, lease_epoch, root, profile,
        authority_digest, claim_deadline, cwd, launch_argv}
    VertexReceipt: {schema_version: 1, record: 'VertexReceipt', graph_id,
        graph_revision, work_id, lease_epoch, session_uuid, owner_id,
        owner_epoch, transaction_id, manifest_digest, verdict,
        final_suite_binding, disposition, required_checks, published_at}
    JoinDecision: {schema_version: 1, record: 'JoinDecision', graph_id,
        graph_revision, join_id, rule, outcome, members, decision_digest}
    status_view: {graph_id, revision, cancelled, effective_cap,
        held_slots, ready_order, statuses, joins}

A join is a decision record only: no code path here reads or writes a root,
and no record carries a merge or source field.

Python 3.9+.
"""

import copy
import datetime
import hashlib
import json
import re

import cowork_execution_profiles
import cowork_workunit

SCHEMA_VERSION = 1
POLICY_ROLE = "builder"
SUPPORTED_CONCURRENCY_CONTRACT = 1

VERTEX_STATES = ("pending", "claimed", "running", "succeeded", "failed",
                 "cancelled")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")
HOLDING_STATES = ("claimed", "running")
DERIVED_STATUSES = ("waiting", "ready", "blocked", "claimed", "running",
                    "succeeded", "failed", "cancelled")
JOIN_RULES = ("all_succeeded",)
JOIN_OUTCOMES = ("joined", "blocked")

DEFAULT_CLAIM_TTL_S = 900
MIN_CLAIM_TTL_S = 60
MAX_CLAIM_TTL_S = 86400

ACCEPTED_FINAL_SUITE_BINDINGS = ("ran_once", "components_ran_once")
REUSED_FINAL_SUITE_BINDING = "reused_dependency_bound"
REUSE_MODE_DEPENDENCY_DIGEST = "dependency_digest"
VERDICT_GREEN = "green"
DISPOSITION_ACCEPTED = "accepted"

# Mirrors cowork_owner.OWNER_VERDICTS; 'no_session' is the graph-only value
# for a vertex whose child session cannot be found.
OWNER_VERDICTS = frozenset({"unowned", "live_owner", "stale_dead_owner",
                            "stale_unproven", "corrupt"})
NO_SESSION = "no_session"
NOT_LIVE_VERDICTS = ("stale_dead_owner", "unowned")
LIVE_OWNER = "live_owner"

RELEASE_REASONS = ("terminal_receipt", "failed", "cancelled", "reclaim")
EVENT_OPS = ("admit", "claim", "bind", "publish", "cancel", "cancel_request",
             "fail", "reclaim", "join")
CANCEL_OUTCOMES = ("cancelled", "cancel_requested", "already_cancelled")

HOLDER_OK = "ok"
HOLDER_BIND_INCOMPLETE = "bind_incomplete"
RECEIPT_VALID = "valid"
RECEIPT_ALREADY_PUBLISHED = "already_published"

RC1_CODES = (
    "graph_state_corrupt", "graph_state_inconsistent", "lock_timeout",
    "io_error",
)
RC2_CODES = (
    "argument_error", "revision_malformed", "cycle", "self_edge",
    "dangling_predecessor", "duplicate_work_id", "root_missing",
    "root_symlink", "root_not_worktree_toplevel", "base_commit_mismatch",
    "anchor_dir_not_ignored", "candidate_collision", "root_alias",
    "root_nested", "root_overlaps_main_checkout",
    "root_overlaps_sessions_root", "root_in_use", "authority_missing",
    "authority_malformed", "authority_digest_mismatch", "authority_shared",
    "unknown_profile", "ceiling_invalid", "ceiling_above_policy",
    "unsupported_concurrency_contract", "join_malformed",
    "join_unknown_member", "revision_conflict", "graph_cancelled",
    "graph_unknown", "vertex_unknown", "vertex_not_ready", "vertex_blocked",
    "none_ready", "cap_reached", "vertex_terminal", "vertex_not_claimed",
    "vertex_not_running", "bind_root_mismatch", "bind_profile_mismatch",
    "graph_vertex_malformed", "graph_vertex_requires_profile",
    "graph_vertex_requires_new_session", "graph_vertex_flag_conflict",
    "receipt_malformed", "receipt_cross_vertex", "receipt_stale_epoch",
    "receipt_stale_revision", "receipt_no_accepted_transaction",
    "receipt_wrong_candidate", "receipt_candidate_collision",
    "receipt_missing_required_check", "receipt_candidate_changed",
    "join_unknown", "early_join",
)
RC3_CODES = (
    "vertex_held", "session_already_bound", "vertex_lease_superseded",
    "vertex_cancel_requested", "vertex_live", "vertex_paused",
    "holder_unproven", "claim_not_expired", "receipt_non_owner",
    "owner_conflict",
)
REASON_RC = dict([(c, 1) for c in RC1_CODES] + [(c, 2) for c in RC2_CODES]
                 + [(c, 3) for c in RC3_CODES])
REASON_CODES = tuple(sorted(REASON_RC))

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_NOW_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_REASON_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_VERTEX_DECL_KEYS = frozenset({"work_id", "root", "base_commit",
                               "authority_path", "authority_digest",
                               "profile", "predecessors"})
_ROOT_IDENTITY_KEYS = ("root_realpath", "root_dev", "root_ino")
_JOIN_KEYS = frozenset({"join_id", "rule", "requires"})
_DOC_REQUIRED_KEYS = frozenset({"schema_version", "max_parallel",
                                "vertices"})
_DOC_OPTIONAL_KEYS = frozenset({"claim_ttl_s", "joins"})
_PROBE_KEYS = frozenset({"declared", "realpath", "exists", "is_dir", "dev",
                         "ino", "path_has_symlink", "ancestors",
                         "toplevel_realpath", "head_commit",
                         "main_checkout_realpath", "main_checkout_dev_ino",
                         "anchor_ignored"})
_RECEIPT_KEYS = frozenset({"schema_version", "record", "graph_id",
                           "graph_revision", "work_id", "lease_epoch",
                           "session_uuid", "owner_id", "owner_epoch",
                           "transaction_id", "manifest_digest", "verdict",
                           "final_suite_binding", "disposition",
                           "required_checks", "published_at"})
_TXN_KEYS = frozenset({"transaction_id", "request_session_uuid",
                       "request_repo_dev_ino", "request_manifest_digest",
                       "result_manifest_digest", "verdict",
                       "final_suite_binding", "disposition",
                       "inventory_labels"})
_STATE_KEYS = frozenset({"schema_version", "record", "graph_id", "cancelled",
                         "revision", "revisions", "vertices", "slots",
                         "joins", "events"})
_VERTEX_RECORD_KEYS = frozenset({"state", "first_admitted_revision",
                                 "lease_epoch", "session_uuid",
                                 "claim_revision", "claimed_at",
                                 "claim_deadline", "cancel_requested",
                                 "terminal_reason", "receipt"})
_SLOT_KEYS = frozenset({"seq", "work_id", "lease_epoch", "acquired_at",
                        "released_at", "release_reason"})


class GraphRefusal(Exception):
    """A closed refusal. `code` is always a member of REASON_RC."""

    def __init__(self, code, work_id=None, detail=None):
        if code not in REASON_RC:
            raise ValueError("unknown graph reason code %r" % (code,))
        self.code = code
        self.work_id = work_id
        self.detail = detail
        super().__init__(self._text())

    @property
    def rc(self):
        return REASON_RC[self.code]

    def _text(self):
        parts = [self.code]
        if self.work_id is not None:
            parts.append(str(self.work_id))
        if self.detail is not None:
            parts.append(str(self.detail))
        return ": ".join(parts)

    def __str__(self):
        return self._text()


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def digest(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def receipt_identity(receipt):
    return (receipt["graph_id"], receipt["graph_revision"],
            receipt["work_id"], receipt["lease_epoch"],
            receipt["session_uuid"], receipt["transaction_id"],
            receipt["manifest_digest"])


# --------------------------------------------------------------------------- #
# Private helpers.                                                            #
# --------------------------------------------------------------------------- #


def _parse_now(now):
    if not isinstance(now, str) or not _NOW_RE.match(now):
        raise GraphRefusal("argument_error", detail="now must be ISO-8601 Z")
    try:
        parsed = datetime.datetime.strptime(now[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise GraphRefusal("argument_error", detail="now is not a real time")
    fraction = now[20:-1] if now[19] == "." else ""
    if fraction:
        parsed = parsed.replace(microsecond=int(fraction.ljust(6, "0")))
    return parsed


def _format_time(moment):
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += ".%06d" % moment.microsecond
    return text + "Z"


def _canon_uuid(value):
    """The lowercase form of a UUID-shaped string, or None."""
    try:
        return cowork_workunit._check_uuid(value, "id")
    except ValueError:
        return None


def _is_uuid(value):
    return isinstance(value, str) and _canon_uuid(value) == value


def _is_int(value, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return minimum is None or value >= minimum


def _hex(value, n):
    pattern = _HEX40_RE if n == 40 else _HEX64_RE
    return isinstance(value, str) and bool(pattern.match(value))


def _pair(value):
    """(int, int) for a 2-element list/tuple of ints, else None."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(_is_int(v) for v in value):
        return None
    return (value[0], value[1])


def _pairs(value):
    """A set of (int, int) for a list of pairs, else None."""
    if not isinstance(value, list):
        return None
    out = set()
    for item in value:
        pair = _pair(item)
        if pair is None:
            return None
        out.add(pair)
    return out


def _abs_path(value):
    return (isinstance(value, str) and value.startswith("/")
            and "\x00" not in value)


def _current_revision(state):
    if state["revision"] == 0:
        return None
    return state["revisions"][state["revision"] - 1]


def _declaration(state, work_id, revision):
    entry = state["revisions"][revision - 1]
    for vertex in entry["vertices"]:
        if vertex["work_id"] == work_id:
            return vertex
    raise GraphRefusal("graph_state_inconsistent", work_id,
                       "vertex missing from revision %d" % revision)


def _current_ids(state):
    current = _current_revision(state)
    if current is None:
        return []
    return [v["work_id"] for v in current["vertices"]]


def _bare_declaration(vertex):
    return {k: vertex[k] for k in _VERTEX_DECL_KEYS}


def _release_slot(state, work_id, lease_epoch, reason, now):
    """Release the one unreleased slot of (work_id, lease_epoch)."""
    for slot in state["slots"]:
        if (slot["work_id"] == work_id and slot["lease_epoch"] == lease_epoch
                and slot["released_at"] is None):
            slot["released_at"] = now
            slot["release_reason"] = reason
            return
    raise GraphRefusal("graph_state_inconsistent", work_id,
                       "no unreleased slot for lease epoch %r"
                       % (lease_epoch,))


def _event(state, op, work_id, lease_epoch, detail, now):
    state["events"].append({"seq": len(state["events"]) + 1, "at": now,
                            "op": op, "work_id": work_id,
                            "lease_epoch": lease_epoch, "detail": detail})


def _enter(state):
    copied = copy.deepcopy(state)
    check_invariants(copied)
    return copied


def _exit(state):
    check_invariants(state)
    return state


def _vertex(state, work_id):
    vertex = state["vertices"].get(work_id) if isinstance(
        work_id, str) else None
    if vertex is None:
        raise GraphRefusal("vertex_unknown", work_id)
    return vertex


def _holder(facts):
    """(verdict, paused) for well-formed holder facts, else (None, None)."""
    if not isinstance(facts, dict):
        return None, None
    verdict = facts.get("owner_verdict")
    paused = facts.get("pause_lease_live")
    if verdict not in OWNER_VERDICTS and verdict != NO_SESSION:
        return None, None
    if not isinstance(paused, bool):
        return None, None
    return verdict, paused


def _refuse_unless_not_live(work_id, facts):
    """The holder rule shared by fail and reclaim of a running vertex."""
    verdict, paused = _holder(facts)
    if verdict == LIVE_OWNER:
        raise GraphRefusal("vertex_live", work_id)
    if verdict not in NOT_LIVE_VERDICTS:
        raise GraphRefusal("holder_unproven", work_id,
                           "owner_verdict %r" % (verdict,))
    if paused:
        raise GraphRefusal("vertex_paused", work_id)
    return verdict


def _policy(policy_fn, profile, work_id=None):
    fn = (policy_fn if policy_fn is not None
          else cowork_execution_profiles.resolved_vertex_policy)
    try:
        return fn(profile, POLICY_ROLE)
    except cowork_execution_profiles.UnknownProfile:
        raise GraphRefusal("unknown_profile", work_id, profile)


# --------------------------------------------------------------------------- #
# Revision normalization and admission checks.                                #
# --------------------------------------------------------------------------- #


def _normalize_vertex(raw):
    if not isinstance(raw, dict) or set(raw) != _VERTEX_DECL_KEYS:
        raise GraphRefusal("revision_malformed", detail="vertex keys")
    try:
        node = cowork_workunit.validate_graph_node({
            "work_id": raw["work_id"], "candidate_manifest_digest": None,
            "candidate_index": None, "governed_child_policy": None,
            "predecessor_work_ids": raw["predecessors"]})
    except ValueError as exc:
        raise GraphRefusal("revision_malformed", detail=str(exc))
    work_id = node["work_id"]
    if not isinstance(raw["predecessors"], list):
        raise GraphRefusal("revision_malformed", work_id, "predecessors")
    preds = list(node["predecessor_work_ids"])
    if len(set(preds)) != len(preds):
        raise GraphRefusal("revision_malformed", work_id,
                           "duplicate predecessors")
    if not _abs_path(raw["root"]):
        raise GraphRefusal("revision_malformed", work_id, "root")
    if not _hex(raw["base_commit"], 40):
        raise GraphRefusal("revision_malformed", work_id, "base_commit")
    if not _abs_path(raw["authority_path"]):
        raise GraphRefusal("revision_malformed", work_id, "authority_path")
    if not isinstance(raw["authority_digest"], str):
        raise GraphRefusal("revision_malformed", work_id, "authority_digest")
    if not isinstance(raw["profile"], str) or not raw["profile"]:
        raise GraphRefusal("revision_malformed", work_id, "profile")
    return {"work_id": work_id, "root": raw["root"],
            "base_commit": raw["base_commit"],
            "authority_path": raw["authority_path"],
            "authority_digest": raw["authority_digest"],
            "profile": raw["profile"], "predecessors": sorted(preds)}


def _normalize_join(raw):
    if not isinstance(raw, dict) or set(raw) != _JOIN_KEYS:
        raise GraphRefusal("join_malformed", detail="join keys")
    join_id = _canon_uuid(raw["join_id"])
    if join_id is None:
        raise GraphRefusal("join_malformed", detail="join_id")
    if raw["rule"] not in JOIN_RULES:
        raise GraphRefusal("join_malformed", join_id, "rule")
    requires = raw["requires"]
    if not isinstance(requires, list) or not requires:
        raise GraphRefusal("join_malformed", join_id, "requires")
    members = [_canon_uuid(m) for m in requires]
    if None in members or len(set(members)) != len(members):
        raise GraphRefusal("join_malformed", join_id, "requires")
    return {"join_id": join_id, "rule": raw["rule"],
            "requires": sorted(members)}


def normalize_revision_document(doc):
    """A normalized revision document, or GraphRefusal revision_malformed /
    join_malformed. `max_parallel` is only required to be present here; its
    value is checked by admission (ceiling_invalid)."""
    if not isinstance(doc, dict):
        raise GraphRefusal("revision_malformed", detail="not an object")
    keys = set(doc)
    if not _DOC_REQUIRED_KEYS <= keys or keys - (
            _DOC_REQUIRED_KEYS | _DOC_OPTIONAL_KEYS):
        raise GraphRefusal("revision_malformed", detail="document keys")
    if not _is_int(doc["schema_version"]) or (
            doc["schema_version"] != SCHEMA_VERSION):
        raise GraphRefusal("revision_malformed", detail="schema_version")
    ttl = doc.get("claim_ttl_s", DEFAULT_CLAIM_TTL_S)
    if not _is_int(ttl) or not MIN_CLAIM_TTL_S <= ttl <= MAX_CLAIM_TTL_S:
        raise GraphRefusal("revision_malformed", detail="claim_ttl_s")
    raw_vertices = doc["vertices"]
    if not isinstance(raw_vertices, list) or not raw_vertices:
        raise GraphRefusal("revision_malformed", detail="vertices")
    vertices = [_normalize_vertex(v) for v in raw_vertices]
    vertices.sort(key=lambda v: v["work_id"])
    raw_joins = doc.get("joins", [])
    if not isinstance(raw_joins, list):
        raise GraphRefusal("join_malformed", detail="joins")
    joins = [_normalize_join(j) for j in raw_joins]
    join_ids = [j["join_id"] for j in joins]
    if len(set(join_ids)) != len(join_ids):
        raise GraphRefusal("join_malformed", detail="duplicate join_id")
    joins.sort(key=lambda j: j["join_id"])
    return {"schema_version": SCHEMA_VERSION,
            "max_parallel": doc["max_parallel"], "claim_ttl_s": ttl,
            "vertices": vertices, "joins": joins}


def structural_check(vertices, joins=()):
    """Delegate acyclicity / duplicate / dangling / self-edge checks to
    cowork_workunit.validate_revision over a LIST projection (duplicates
    stay visible), then refuse joins naming an unknown member."""
    projection = [{"work_id": v["work_id"],
                   "candidate_manifest_digest": None,
                   "candidate_index": None,
                   "governed_child_policy": None,
                   "predecessor_work_ids": list(v["predecessors"])}
                  for v in vertices]
    try:
        cowork_workunit.validate_revision(projection)
    except cowork_workunit.GraphValidationError as exc:
        violations = sorted(
            exc.violations,
            key=lambda v: (str(v.get("code")), str(v.get("work_id"))))
        first = violations[0] if violations else {}
        code = first.get("code")
        if code not in REASON_RC:
            code = "revision_malformed"
        raise GraphRefusal(code, first.get("work_id"), [
            {"code": v.get("code"), "work_id": v.get("work_id")}
            for v in violations])
    except ValueError as exc:
        raise GraphRefusal("revision_malformed", detail=str(exc))
    known = {v["work_id"] for v in vertices}
    for join in sorted(joins, key=lambda j: j["join_id"]):
        for member in sorted(join["requires"]):
            if member not in known:
                raise GraphRefusal("join_unknown_member", member,
                                   join["join_id"])


def _probe_identity(probe):
    """(realpath, (dev, ino), ancestors) for a well-formed probe, else
    None."""
    if not isinstance(probe, dict) or not _PROBE_KEYS <= set(probe):
        return None
    if probe["exists"] is not True or probe["is_dir"] is not True:
        return None
    if not _is_int(probe["dev"]) or not _is_int(probe["ino"]):
        return None
    if not isinstance(probe["realpath"], str):
        return None
    ancestors = _pairs(probe["ancestors"])
    if ancestors is None:
        return None
    return probe["realpath"], (probe["dev"], probe["ino"]), ancestors


def _identity_record(value):
    """(realpath, (dev, ino), ancestors) for a sessions-root / foreign-root
    record, else None."""
    if not isinstance(value, dict):
        return None
    if not isinstance(value.get("realpath"), str):
        return None
    if not _is_int(value.get("dev")) or not _is_int(value.get("ino")):
        return None
    ancestors = _pairs(value.get("ancestors"))
    if ancestors is None:
        return None
    return value["realpath"], (value["dev"], value["ino"]), ancestors


def _overlaps(a, b):
    """Equal or nested identities (either direction)."""
    return (a[0] == b[0] or a[1] == b[1] or a[1] in b[2] or b[1] in a[2])


def check_roots(vertices, probes, sessions_root_probe, foreign_active_roots):
    """Root identity checks over the given (active) vertices. Returns
    {work_id: (realpath, dev, ino)}."""
    sessions = _identity_record(sessions_root_probe)
    if sessions is None:
        raise GraphRefusal("argument_error", detail="sessions_root_probe")
    if not isinstance(foreign_active_roots, list):
        raise GraphRefusal("argument_error", detail="foreign_active_roots")
    foreign = []
    for entry in foreign_active_roots:
        identity = _identity_record(entry)
        if identity is None:
            raise GraphRefusal("argument_error",
                               detail="foreign_active_roots entry")
        foreign.append((entry.get("graph_id"), entry.get("work_id"),
                        identity))
    probes = probes if isinstance(probes, dict) else {}
    identities = {}
    for vertex in sorted(vertices, key=lambda v: v["work_id"]):
        work_id = vertex["work_id"]
        probe = probes.get(work_id)
        identity = _probe_identity(probe)
        if identity is None or probe["declared"] != vertex["root"]:
            raise GraphRefusal("root_missing", work_id)
        realpath, dev_ino, ancestors = identity
        if probe["path_has_symlink"] is not False or (
                realpath != vertex["root"]):
            raise GraphRefusal("root_symlink", work_id)
        if probe["toplevel_realpath"] is None or (
                probe["toplevel_realpath"] != realpath):
            raise GraphRefusal("root_not_worktree_toplevel", work_id)
        if probe["head_commit"] is None or (
                probe["head_commit"] != vertex["base_commit"]):
            raise GraphRefusal("base_commit_mismatch", work_id)
        if probe["anchor_ignored"] is not True:
            raise GraphRefusal("anchor_dir_not_ignored", work_id)
        main = probe["main_checkout_realpath"]
        main_pair = _pair(probe["main_checkout_dev_ino"])
        if (not isinstance(main, str) or main_pair is None
                or main == realpath
                or main.startswith(realpath.rstrip("/") + "/")
                or main_pair == dev_ino):
            raise GraphRefusal("root_overlaps_main_checkout", work_id)
        if (sessions[1] == dev_ino or sessions[1] in ancestors
                or dev_ino in sessions[2] or sessions[0] == realpath):
            raise GraphRefusal("root_overlaps_sessions_root", work_id)
        identities[work_id] = identity
    ordered = sorted(identities)
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            a, b = identities[left], identities[right]
            if a[0] == b[0]:
                raise GraphRefusal("candidate_collision", right, left)
            if a[1] == b[1]:
                raise GraphRefusal("root_alias", right, left)
            if a[1] in b[2] or b[1] in a[2]:
                raise GraphRefusal("root_nested", right, left)
    for work_id in ordered:
        for graph_id, other_work_id, identity in foreign:
            if _overlaps(identities[work_id], identity):
                raise GraphRefusal("root_in_use", work_id, {
                    "graph_id": graph_id, "work_id": other_work_id})
    return {w: (identities[w][0], identities[w][1][0], identities[w][1][1])
            for w in ordered}


def check_authorities(vertices, authority_bytes):
    """Authority presence, format, digest and distinctness over the given
    (active) vertices."""
    supplied = authority_bytes if isinstance(authority_bytes, dict) else {}
    ordered = sorted(vertices, key=lambda v: v["work_id"])
    for vertex in ordered:
        work_id = vertex["work_id"]
        blob = supplied.get(work_id)
        if not isinstance(blob, bytes):
            raise GraphRefusal("authority_missing", work_id)
        declared = vertex["authority_digest"]
        if not _hex(declared, 64):
            raise GraphRefusal("authority_malformed", work_id)
        if hashlib.sha256(blob).hexdigest() != declared:
            raise GraphRefusal("authority_digest_mismatch", work_id)
    seen = {}
    for vertex in ordered:
        declared = vertex["authority_digest"]
        if declared in seen:
            raise GraphRefusal("authority_shared", vertex["work_id"],
                               seen[declared])
        seen[declared] = vertex["work_id"]


def policy_cap(vertices, policy_fn=None):
    """min over the declared profiles of the policy's max_parallel_vertices,
    read through resolved_vertex_policy(profile, 'builder') (or policy_fn)."""
    by_profile = {}
    for vertex in sorted(vertices, key=lambda v: v["work_id"]):
        by_profile.setdefault(vertex["profile"], vertex["work_id"])
    caps = []
    for profile in sorted(by_profile):
        work_id = by_profile[profile]
        policy = _policy(policy_fn, profile, work_id)
        concurrency = policy.get("concurrency") if isinstance(
            policy, dict) else None
        if not isinstance(concurrency, dict):
            raise GraphRefusal("unsupported_concurrency_contract", work_id,
                               profile)
        version = concurrency.get("contract_version")
        cap = concurrency.get("max_parallel_vertices")
        if not _is_int(version) or (
                version != SUPPORTED_CONCURRENCY_CONTRACT) or (
                not _is_int(cap, 1)):
            raise GraphRefusal("unsupported_concurrency_contract", work_id,
                               profile)
        caps.append(cap)
    if not caps:
        raise GraphRefusal("revision_malformed", detail="no vertices")
    return min(caps)


# --------------------------------------------------------------------------- #
# State, admission and supersession.                                          #
# --------------------------------------------------------------------------- #


def new_state(graph_id):
    if not _is_uuid(graph_id):
        raise GraphRefusal("argument_error", detail="graph_id")
    return {"schema_version": SCHEMA_VERSION, "record": "GraphState",
            "graph_id": graph_id, "cancelled": False, "revision": 0,
            "revisions": [], "vertices": {}, "slots": [], "joins": {},
            "events": []}


def _check_supersession(state, normalized):
    proposed = {v["work_id"]: v for v in normalized["vertices"]}
    for work_id in sorted(state["vertices"]):
        record = state["vertices"][work_id]
        if record["state"] == "pending":
            continue
        current = _current_revision(state)
        stored = _bare_declaration(_declaration(
            state, work_id, current["graph_revision"]))
        if proposed.get(work_id) != stored:
            raise GraphRefusal("revision_conflict", work_id,
                               "non-pending vertex must be repeated "
                               "identically")
    for entry in state["revisions"]:
        for vertex in entry["vertices"]:
            work_id = vertex["work_id"]
            if work_id in proposed and (
                    proposed[work_id] != _bare_declaration(vertex)):
                raise GraphRefusal("revision_conflict", work_id,
                                   "work id reused with a new declaration")
    proposed_joins = {j["join_id"]: j for j in normalized["joins"]}
    for join_id in sorted(state["joins"]):
        decisions = state["joins"][join_id]
        if not decisions or join_id not in proposed_joins:
            continue
        decided_at = max(int(r) for r in decisions)
        earlier = [j for j in state["revisions"][decided_at - 1]["joins"]
                   if j["join_id"] == join_id]
        if not earlier or earlier[0] != proposed_joins[join_id]:
            raise GraphRefusal("revision_conflict", detail={
                "join_id": join_id, "rule": "decided join changed"})


def _carried_identity(state, work_id):
    for entry in reversed(state["revisions"]):
        for vertex in entry["vertices"]:
            if vertex["work_id"] == work_id and all(
                    k in vertex for k in _ROOT_IDENTITY_KEYS):
                return {k: vertex[k] for k in _ROOT_IDENTITY_KEYS}
    raise GraphRefusal("graph_state_inconsistent", work_id,
                       "terminal vertex without a recorded root identity")


def admit(state, revision_doc, probes, sessions_root_probe,
          foreign_active_roots, authority_bytes, now, policy_fn=None):
    """Admit one revision, fail closed. Check order: graph_cancelled,
    normalize, structural, roots and authorities (active vertices only),
    policy cap, ceiling, supersession."""
    s = _enter(state)
    _parse_now(now)
    if s["cancelled"]:
        raise GraphRefusal("graph_cancelled")
    normalized = normalize_revision_document(revision_doc)
    structural_check(normalized["vertices"], normalized["joins"])
    active = [v for v in normalized["vertices"]
              if s["vertices"].get(v["work_id"], {}).get(
                  "state", "pending") not in TERMINAL_STATES]
    roots = check_roots(active, probes, sessions_root_probe,
                        foreign_active_roots)
    check_authorities(active, authority_bytes)
    cap = policy_cap(normalized["vertices"], policy_fn)
    ceiling = normalized["max_parallel"]
    if not _is_int(ceiling, 1):
        raise GraphRefusal("ceiling_invalid", detail=ceiling)
    if ceiling > cap:
        raise GraphRefusal("ceiling_above_policy",
                           detail={"max_parallel": ceiling,
                                   "policy_cap": cap})
    _check_supersession(s, normalized)

    graph_revision = s["revision"] + 1
    revision_vertices = []
    for vertex in normalized["vertices"]:
        entry = dict(vertex)
        entry["predecessors"] = list(vertex["predecessors"])
        if vertex["work_id"] in roots:
            realpath, dev, ino = roots[vertex["work_id"]]
            entry.update(root_realpath=realpath, root_dev=dev, root_ino=ino)
        else:
            entry.update(_carried_identity(s, vertex["work_id"]))
        revision_vertices.append(entry)
    s["revisions"].append({
        "graph_revision": graph_revision, "max_parallel": ceiling,
        "effective_cap": ceiling, "policy_cap": cap,
        "claim_ttl_s": normalized["claim_ttl_s"],
        "vertices": revision_vertices,
        "joins": copy.deepcopy(normalized["joins"]), "admitted_at": now,
        "revision_digest": digest(normalized)})
    s["revision"] = graph_revision
    added = []
    for vertex in normalized["vertices"]:
        if vertex["work_id"] in s["vertices"]:
            continue
        added.append(vertex["work_id"])
        s["vertices"][vertex["work_id"]] = {
            "state": "pending", "first_admitted_revision": graph_revision,
            "lease_epoch": 0, "session_uuid": None, "claim_revision": None,
            "claimed_at": None, "claim_deadline": None,
            "cancel_requested": False, "terminal_reason": None,
            "receipt": None}
    _event(s, "admit", None, None,
           {"graph_revision": graph_revision, "added": sorted(added)}, now)
    return _exit(s)


# --------------------------------------------------------------------------- #
# Derived statuses, ordering and invariants.                                  #
# --------------------------------------------------------------------------- #


def derive_statuses(state):
    """{work_id: derived status} for the current revision's vertices."""
    current = _current_revision(state)
    if current is None:
        return {}
    preds = {v["work_id"]: list(v["predecessors"])
             for v in current["vertices"]}
    statuses = {}
    remaining = sorted(preds)
    while remaining:
        progressed = False
        for work_id in list(remaining):
            if any(p in remaining for p in preds[work_id]):
                continue
            remaining.remove(work_id)
            progressed = True
            vstate = state["vertices"][work_id]["state"]
            if vstate != "pending":
                statuses[work_id] = vstate
                continue
            pred_states = [statuses.get(p) for p in preds[work_id]]
            if any(p in ("failed", "cancelled", "blocked")
                   for p in pred_states):
                statuses[work_id] = "blocked"
            elif all(p == "succeeded" for p in pred_states) and (
                    not state["cancelled"]):
                statuses[work_id] = "ready"
            else:
                statuses[work_id] = "waiting"
        if not progressed:
            for work_id in remaining:
                vstate = state["vertices"][work_id]["state"]
                statuses[work_id] = (vstate if vstate != "pending"
                                     else "blocked")
            break
    return {w: statuses[w] for w in sorted(statuses)}


def ready_order(state):
    statuses = derive_statuses(state)
    ready = [w for w, status in statuses.items() if status == "ready"]
    return sorted(ready, key=lambda w: (
        state["vertices"][w]["first_admitted_revision"], w))


def held_slots(state):
    return sorted((s for s in state["slots"] if s["released_at"] is None),
                  key=lambda s: s["seq"])


def effective_cap(state):
    current = _current_revision(state)
    return 0 if current is None else current["effective_cap"]


def _check_shape(state):
    if not isinstance(state, dict) or set(state) != _STATE_KEYS:
        raise ValueError("state keys")
    if state["schema_version"] != SCHEMA_VERSION or (
            state["record"] != "GraphState"):
        raise ValueError("state header")
    if not _is_uuid(state["graph_id"]):
        raise ValueError("graph_id")
    if not isinstance(state["cancelled"], bool):
        raise TypeError("cancelled")
    if not _is_int(state["revision"], 0) or (
            state["revision"] != len(state["revisions"])):
        raise ValueError("revision counter")
    for index, entry in enumerate(state["revisions"]):
        if entry["graph_revision"] != index + 1:
            raise ValueError("revision numbering")
        if not _is_int(entry["effective_cap"], 1):
            raise ValueError("effective_cap")
        for vertex in entry["vertices"]:
            if not _is_uuid(vertex["work_id"]):
                raise ValueError("revision vertex id")
            if not _VERTEX_DECL_KEYS | set(_ROOT_IDENTITY_KEYS) <= set(
                    vertex):
                raise ValueError("revision vertex keys")
            if not isinstance(vertex["predecessors"], list):
                raise TypeError("predecessors")
        for join in entry["joins"]:
            if set(join) != _JOIN_KEYS or not isinstance(
                    join["requires"], list):
                raise ValueError("revision join")
    if not isinstance(state["vertices"], dict):
        raise TypeError("vertices")
    for work_id, record in state["vertices"].items():
        if not _is_uuid(work_id) or set(record) != _VERTEX_RECORD_KEYS:
            raise ValueError("vertex record")
        if record["state"] not in VERTEX_STATES:
            raise ValueError("vertex state")
        if not _is_int(record["lease_epoch"], 0):
            raise ValueError("lease_epoch")
        if not isinstance(record["cancel_requested"], bool):
            raise TypeError("cancel_requested")
    if not isinstance(state["slots"], list):
        raise TypeError("slots")
    for slot in state["slots"]:
        if set(slot) != _SLOT_KEYS:
            raise ValueError("slot keys")
    if not isinstance(state["joins"], dict):
        raise TypeError("joins")
    for decisions in state["joins"].values():
        for key, decision in decisions.items():
            if not _is_int(int(key), 1) or not _hex(
                    decision["decision_digest"], 64):
                raise ValueError("join decision")
    if not isinstance(state["events"], list):
        raise TypeError("events")


def _inconsistent(rule, work_id=None):
    return GraphRefusal("graph_state_inconsistent", work_id, rule)


def check_invariants(state):
    """Raise graph_state_corrupt for a malformed record and
    graph_state_inconsistent for a semantic violation; return None."""
    try:
        _check_shape(state)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise GraphRefusal("graph_state_corrupt")
    vertices = state["vertices"]
    current_ids = set(_current_ids(state))
    for work_id in current_ids:
        if work_id not in vertices:
            raise _inconsistent("current revision vertex without record",
                                work_id)
    slots = state["slots"]
    seen = set()
    for index, slot in enumerate(slots):
        if slot["seq"] != index + 1:
            raise _inconsistent("slot seq not 1..n")
        key = (slot["work_id"], slot["lease_epoch"])
        if key in seen:
            raise _inconsistent("duplicate slot acquisition",
                                slot["work_id"])
        seen.add(key)
        if (slot["released_at"] is None) != (slot["release_reason"] is None):
            raise _inconsistent("half-released slot", slot["work_id"])
        if slot["release_reason"] is not None and (
                slot["release_reason"] not in RELEASE_REASONS):
            raise _inconsistent("unknown release reason", slot["work_id"])
        if slot["work_id"] not in vertices:
            raise _inconsistent("slot for unknown vertex", slot["work_id"])
    held = held_slots(state)
    held_keys = sorted((s["work_id"], s["lease_epoch"]) for s in held)
    holding = sorted((w, r["lease_epoch"]) for w, r in vertices.items()
                     if r["state"] in HOLDING_STATES)
    if held_keys != holding:
        raise _inconsistent("unreleased slots differ from held vertices")
    for slot in held:
        record = vertices[slot["work_id"]]
        claim_revision = record["claim_revision"]
        if not _is_int(claim_revision, 1) or (
                claim_revision > state["revision"]):
            raise _inconsistent("held vertex without claim revision",
                                slot["work_id"])
        cap = state["revisions"][claim_revision - 1]["effective_cap"]
        earlier = sum(1 for t in held if t["seq"] <= slot["seq"])
        if earlier > cap:
            raise _inconsistent("slot acquired over its revision cap",
                                slot["work_id"])
    sessions = {}
    for work_id in sorted(vertices):
        record = vertices[work_id]
        vstate = record["state"]
        if vstate == "running" and record["session_uuid"] is None:
            raise _inconsistent("running vertex without session", work_id)
        if vstate == "claimed" and record["session_uuid"] is not None:
            raise _inconsistent("claimed vertex with session", work_id)
        if vstate in ("claimed", "running", "succeeded") and (
                not _is_int(record["claim_revision"], 1)
                or record["claim_revision"] > state["revision"]):
            raise _inconsistent("claim revision missing", work_id)
        if vstate in HOLDING_STATES and work_id not in current_ids:
            raise _inconsistent("held vertex outside current revision",
                                work_id)
        if (vstate == "succeeded") != (record["receipt"] is not None):
            raise _inconsistent("receipt iff succeeded", work_id)
        session = record["session_uuid"]
        if session is not None:
            if session in sessions:
                raise _inconsistent("session bound to two vertices",
                                    work_id)
            sessions[session] = work_id
    return None


# --------------------------------------------------------------------------- #
# Slot ledger: claim, bind, fence.                                            #
# --------------------------------------------------------------------------- #


def claim(state, work_id, now):
    """Acquire one slot for `work_id` (or the first ready vertex when None).
    Returns (new_state, claim_info)."""
    s = _enter(state)
    moment = _parse_now(now)
    if s["cancelled"]:
        raise GraphRefusal("graph_cancelled")
    statuses = derive_statuses(s)
    held = len(held_slots(s))
    cap = effective_cap(s)
    if work_id is not None:
        if work_id not in statuses:
            raise GraphRefusal("vertex_unknown", work_id)
        vstate = s["vertices"][work_id]["state"]
        if vstate in TERMINAL_STATES:
            raise GraphRefusal("vertex_terminal", work_id)
        if vstate in HOLDING_STATES:
            raise GraphRefusal("vertex_held", work_id)
        if statuses[work_id] == "blocked":
            raise GraphRefusal("vertex_blocked", work_id)
        if statuses[work_id] != "ready":
            raise GraphRefusal("vertex_not_ready", work_id)
        if held >= cap:
            raise GraphRefusal("cap_reached", work_id,
                               {"held": held, "effective_cap": cap})
        chosen = work_id
    else:
        if held >= cap:
            raise GraphRefusal("cap_reached",
                               detail={"held": held, "effective_cap": cap})
        order = ready_order(s)
        if not order:
            raise GraphRefusal("none_ready")
        chosen = order[0]
    current = _current_revision(s)
    record = s["vertices"][chosen]
    record["lease_epoch"] += 1
    record["state"] = "claimed"
    record["claim_revision"] = current["graph_revision"]
    record["claimed_at"] = now
    record["claim_deadline"] = _format_time(
        moment + datetime.timedelta(seconds=current["claim_ttl_s"]))
    epoch = record["lease_epoch"]
    s["slots"].append({"seq": len(s["slots"]) + 1, "work_id": chosen,
                       "lease_epoch": epoch, "acquired_at": now,
                       "released_at": None, "release_reason": None})
    _event(s, "claim", chosen, epoch,
           {"graph_revision": current["graph_revision"]}, now)
    decl = _declaration(s, chosen, current["graph_revision"])
    info = {"graph_id": s["graph_id"], "work_id": chosen,
            "lease_epoch": epoch, "root": decl["root"],
            "profile": decl["profile"],
            "authority_digest": decl["authority_digest"],
            "claim_deadline": record["claim_deadline"], "cwd": decl["root"],
            "launch_argv": ["--new", "--profile", decl["profile"],
                            "--graph-vertex", "%s:%s:%d" % (
                                s["graph_id"], chosen, epoch)]}
    return _exit(s), info


def bind(state, work_id, lease_epoch, session_uuid, cwd_dev_ino, profile,
         session_index_conflict, now=None):
    """Bind a child session to a claimed vertex (state running). Binding the
    same session with the same epoch again is idempotent. `now`, when given,
    stamps the 'bind' event."""
    s = _enter(state)
    if now is not None:
        _parse_now(now)
    if not _is_uuid(session_uuid):
        raise GraphRefusal("argument_error", work_id, "session_uuid")
    if not _is_int(lease_epoch):
        raise GraphRefusal("argument_error", work_id, "lease_epoch")
    cwd = _pair(cwd_dev_ino)
    if cwd is None:
        raise GraphRefusal("argument_error", work_id, "cwd_dev_ino")
    record = _vertex(s, work_id)
    if session_index_conflict:
        raise GraphRefusal("session_already_bound", work_id, session_uuid)
    for other_id, other in sorted(s["vertices"].items()):
        if other_id != work_id and other["session_uuid"] == session_uuid:
            raise GraphRefusal("session_already_bound", work_id, other_id)
    if lease_epoch != record["lease_epoch"]:
        raise GraphRefusal("vertex_lease_superseded", work_id)
    if record["state"] == "running" and (
            record["session_uuid"] != session_uuid):
        raise GraphRefusal("vertex_held", work_id)
    if record["state"] not in HOLDING_STATES:
        raise GraphRefusal("vertex_not_claimed", work_id)
    decl = _declaration(s, work_id, record["claim_revision"])
    if cwd != (decl["root_dev"], decl["root_ino"]):
        raise GraphRefusal("bind_root_mismatch", work_id)
    if profile != decl["profile"]:
        raise GraphRefusal("bind_profile_mismatch", work_id)
    if record["state"] == "running":
        return _exit(s)
    record["state"] = "running"
    record["session_uuid"] = session_uuid
    _event(s, "bind", work_id, lease_epoch,
           {"session_uuid": session_uuid}, now)
    return _exit(s)


def assert_holder(state, work_id, lease_epoch, session_uuid):
    """The fence a vertex child passes before governed work: 'ok' for the
    bound running holder, 'bind_incomplete' for a claimed vertex with the
    caller's epoch and no session yet."""
    s = _enter(state)
    record = _vertex(s, work_id)
    if s["cancelled"] or record["cancel_requested"]:
        raise GraphRefusal("vertex_cancel_requested", work_id)
    if lease_epoch != record["lease_epoch"]:
        raise GraphRefusal("vertex_lease_superseded", work_id, "epoch")
    if record["state"] == "claimed" and record["session_uuid"] is None:
        return HOLDER_BIND_INCOMPLETE
    if record["state"] != "running" or (
            record["session_uuid"] != session_uuid):
        raise GraphRefusal("vertex_lease_superseded", work_id, "holder")
    return HOLDER_OK


# --------------------------------------------------------------------------- #
# Holder lifecycle: cancel, fail, reclaim.                                    #
# --------------------------------------------------------------------------- #


def _confirm_cancel(s, work_id, record, verdict, now):
    epoch = record["lease_epoch"]
    if record["state"] in HOLDING_STATES:
        _release_slot(s, work_id, epoch, "cancelled", now)
        record["lease_epoch"] = epoch + 1
    record["state"] = "cancelled"
    record["terminal_reason"] = "cancelled"
    _event(s, "cancel", work_id, epoch, {"owner_verdict": verdict}, now)
    return "cancelled"


def _cancel_vertex(s, work_id, facts, now):
    record = s["vertices"][work_id]
    vstate = record["state"]
    if vstate == "cancelled":
        return "already_cancelled"
    if vstate in ("pending", "claimed"):
        return _confirm_cancel(s, work_id, record, None, now)
    verdict, _paused = _holder(facts)
    if verdict in NOT_LIVE_VERDICTS:
        return _confirm_cancel(s, work_id, record, verdict, now)
    if not record["cancel_requested"]:
        record["cancel_requested"] = True
        _event(s, "cancel_request", work_id, record["lease_epoch"],
               {"owner_verdict": verdict}, now)
    return "cancel_requested"


def cancel(state, work_id, holder_facts_by_work_id, now):
    """Cancel one vertex, or the whole graph when work_id is None. A running
    vertex is confirmed cancelled only when its holder is provably not live;
    otherwise a durable cancel request is recorded and no slot is released.
    Returns (new_state, [{work_id, outcome}] sorted by work_id)."""
    s = _enter(state)
    _parse_now(now)
    facts = (holder_facts_by_work_id
             if isinstance(holder_facts_by_work_id, dict) else {})
    current_ids = set(_current_ids(s))
    if work_id is not None:
        if work_id not in current_ids:
            raise GraphRefusal("vertex_unknown", work_id)
        if s["vertices"][work_id]["state"] in ("succeeded", "failed"):
            raise GraphRefusal("vertex_terminal", work_id)
        targets = [work_id]
    else:
        if not s["cancelled"]:
            s["cancelled"] = True
            _event(s, "cancel", None, None, {"scope": "graph"}, now)
        targets = sorted(w for w in current_ids
                         if s["vertices"][w]["state"]
                         not in ("succeeded", "failed"))
    outcomes = [{"work_id": w,
                 "outcome": _cancel_vertex(s, w, facts.get(w), now)}
                for w in sorted(targets)]
    return _exit(s), outcomes


def fail(state, work_id, reason_code, holder_facts, now):
    """Record a claimed/running vertex as failed. A running vertex needs a
    provably non-live, unpaused holder."""
    s = _enter(state)
    _parse_now(now)
    if not isinstance(reason_code, str) or not _REASON_TOKEN_RE.match(
            reason_code):
        raise GraphRefusal("argument_error", work_id, "reason_code")
    record = _vertex(s, work_id)
    if record["state"] in TERMINAL_STATES:
        raise GraphRefusal("vertex_terminal", work_id)
    if record["state"] == "pending":
        raise GraphRefusal("vertex_not_claimed", work_id)
    verdict = None
    if record["state"] == "running":
        verdict = _refuse_unless_not_live(work_id, holder_facts)
    epoch = record["lease_epoch"]
    _release_slot(s, work_id, epoch, "failed", now)
    record["state"] = "failed"
    record["terminal_reason"] = reason_code
    record["lease_epoch"] = epoch + 1
    _event(s, "fail", work_id, epoch,
           {"reason_code": reason_code, "owner_verdict": verdict}, now)
    return _exit(s)


def reclaim(state, work_id, holder_facts, now):
    """Release an abandoned claim: a claimed vertex past its claim deadline,
    or a running vertex whose holder is provably dead/unowned and not
    paused. The vertex returns to pending (cancelled when a cancel was
    requested) under a bumped lease epoch."""
    s = _enter(state)
    moment = _parse_now(now)
    record = _vertex(s, work_id)
    if record["state"] in TERMINAL_STATES:
        raise GraphRefusal("vertex_terminal", work_id)
    if record["state"] == "pending":
        raise GraphRefusal("vertex_not_claimed", work_id)
    verdict = None
    if record["state"] == "claimed":
        if moment < _parse_now(record["claim_deadline"]):
            raise GraphRefusal("claim_not_expired", work_id,
                               record["claim_deadline"])
    else:
        verdict = _refuse_unless_not_live(work_id, holder_facts)
    epoch = record["lease_epoch"]
    _release_slot(s, work_id, epoch, "reclaim", now)
    record["lease_epoch"] = epoch + 1
    record["session_uuid"] = None
    record["claim_revision"] = None
    record["claimed_at"] = None
    record["claim_deadline"] = None
    if record["cancel_requested"]:
        record["state"] = "cancelled"
        record["terminal_reason"] = "cancelled"
    else:
        record["state"] = "pending"
    _event(s, "reclaim", work_id, epoch,
           {"owner_verdict": verdict, "landed": record["state"]}, now)
    return _exit(s)


# --------------------------------------------------------------------------- #
# Receipts.                                                                   #
# --------------------------------------------------------------------------- #


def required_check_evidence(policy, txn_facts):
    """{check: bool} for every policy required check; unknown names and
    missing facts are False."""
    checks = policy.get("required_checks") if isinstance(policy, dict) else []
    txn = txn_facts if isinstance(txn_facts, dict) else {}
    reuse_mode = policy.get("reuse_mode") if isinstance(policy, dict) else None
    green = txn.get("verdict") == VERDICT_GREEN
    binding = txn.get("final_suite_binding")
    labels = txn.get("inventory_labels")
    evidence = {}
    for check in checks or ():
        if check == "owned_verification_final_suite":
            ok = green and (binding in ACCEPTED_FINAL_SUITE_BINDINGS or (
                binding == REUSED_FINAL_SUITE_BINDING
                and reuse_mode == REUSE_MODE_DEPENDENCY_DIGEST))
        elif check == "paired_reviewer_approval":
            ok = txn.get("disposition") == DISPOSITION_ACCEPTED
        elif check == "user_declared_checks":
            ok = green and isinstance(labels, list) and bool(labels) and all(
                isinstance(label, str) for label in labels)
        else:
            ok = False
        evidence[check] = bool(ok)
    return evidence


def build_receipt(state, work_id, session_uuid, owner_id, owner_epoch,
                  txn_facts, now, policy_fn=None):
    """Assemble a VertexReceipt for the vertex's current claim from the
    injected transaction facts. Validates nothing; publish does."""
    s = _enter(state)
    _parse_now(now)
    record = _vertex(s, work_id)
    if not isinstance(txn_facts, dict):
        raise GraphRefusal("receipt_no_accepted_transaction", work_id)
    claim_revision = record["claim_revision"]
    if claim_revision is None:
        raise GraphRefusal("vertex_not_claimed", work_id)
    decl = _declaration(s, work_id, claim_revision)
    policy = _policy(policy_fn, decl["profile"], work_id)
    return {"schema_version": SCHEMA_VERSION, "record": "VertexReceipt",
            "graph_id": s["graph_id"], "graph_revision": claim_revision,
            "work_id": work_id, "lease_epoch": record["lease_epoch"],
            "session_uuid": session_uuid, "owner_id": owner_id,
            "owner_epoch": owner_epoch,
            "transaction_id": txn_facts.get("transaction_id"),
            "manifest_digest": txn_facts.get("result_manifest_digest"),
            "verdict": txn_facts.get("verdict"),
            "final_suite_binding": txn_facts.get("final_suite_binding"),
            "disposition": txn_facts.get("disposition"),
            "required_checks": required_check_evidence(policy, txn_facts),
            "published_at": now}


def _receipt_well_formed(receipt):
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        return False
    if not _is_int(receipt["schema_version"]) or (
            receipt["schema_version"] != SCHEMA_VERSION):
        return False
    if receipt["record"] != "VertexReceipt":
        return False
    for key in ("graph_id", "work_id", "session_uuid"):
        if not _is_uuid(receipt[key]):
            return False
    if not _is_int(receipt["graph_revision"], 1) or not _is_int(
            receipt["lease_epoch"], 1) or not _is_int(
            receipt["owner_epoch"], 0):
        return False
    for key in ("owner_id", "transaction_id", "verdict",
                "final_suite_binding"):
        if not isinstance(receipt[key], str) or not receipt[key]:
            return False
    if receipt["disposition"] is not None and not isinstance(
            receipt["disposition"], str):
        return False
    if not _hex(receipt["manifest_digest"], 64):
        return False
    checks = receipt["required_checks"]
    if not isinstance(checks, dict) or not all(
            isinstance(k, str) and isinstance(v, bool)
            for k, v in checks.items()):
        return False
    try:
        _parse_now(receipt["published_at"])
    except GraphRefusal:
        return False
    return True


def _txn_well_formed(txn):
    if not isinstance(txn, dict) or set(txn) != _TXN_KEYS:
        return False
    for key in ("transaction_id", "request_session_uuid",
                "request_manifest_digest", "result_manifest_digest",
                "verdict", "final_suite_binding"):
        if not isinstance(txn[key], str) or not txn[key]:
            return False
    if txn["request_repo_dev_ino"] is not None and _pair(
            txn["request_repo_dev_ino"]) is None:
        return False
    if txn["disposition"] is not None and not isinstance(
            txn["disposition"], str):
        return False
    return isinstance(txn["inventory_labels"], list)


def validate_receipt(state, receipt, txn_facts, live_fingerprint, owner_ok,
                     policy_fn=None):
    """'valid' | 'already_published', or GraphRefusal. Order: shape, then an
    identical stored receipt identity, then the receipt rules in their
    contract order, then the vertex state."""
    s = _enter(state)
    if not _receipt_well_formed(receipt):
        raise GraphRefusal("receipt_malformed")
    work_id = receipt["work_id"]
    record = s["vertices"].get(work_id)
    if record is not None and record["receipt"] is not None and (
            receipt_identity(record["receipt"])
            == receipt_identity(receipt)):
        return RECEIPT_ALREADY_PUBLISHED
    if receipt["graph_id"] != s["graph_id"] or record is None or (
            receipt["session_uuid"] != record["session_uuid"]):
        raise GraphRefusal("receipt_cross_vertex", work_id)
    if receipt["lease_epoch"] != record["lease_epoch"]:
        raise GraphRefusal("receipt_stale_epoch", work_id)
    if receipt["graph_revision"] != record["claim_revision"]:
        raise GraphRefusal("receipt_stale_revision", work_id)
    if owner_ok is not True:
        raise GraphRefusal("receipt_non_owner", work_id)
    if txn_facts is None or not _txn_well_formed(txn_facts):
        raise GraphRefusal("receipt_no_accepted_transaction", work_id)
    decl = _declaration(s, work_id, record["claim_revision"])
    root_pair = (decl["root_dev"], decl["root_ino"])
    wrong = []
    if txn_facts["transaction_id"] != receipt["transaction_id"]:
        wrong.append("transaction_id")
    for key in ("verdict", "final_suite_binding", "disposition"):
        if txn_facts[key] != receipt[key]:
            wrong.append(key)
    if txn_facts["request_session_uuid"] != receipt["session_uuid"]:
        wrong.append("request_session_uuid")
    if _pair(txn_facts["request_repo_dev_ino"]) != root_pair:
        wrong.append("request_repo_dev_ino")
    if txn_facts["result_manifest_digest"] != (
            txn_facts["request_manifest_digest"]) or (
            txn_facts["result_manifest_digest"]
            != receipt["manifest_digest"]):
        wrong.append("manifest_digest")
    if txn_facts["verdict"] != VERDICT_GREEN:
        wrong.append("not_green")
    if txn_facts["disposition"] != DISPOSITION_ACCEPTED:
        wrong.append("not_accepted")
    if wrong:
        raise GraphRefusal("receipt_wrong_candidate", work_id, wrong)
    for other_id in sorted(s["vertices"]):
        other = s["vertices"][other_id]["receipt"]
        if other_id == work_id or other is None:
            continue
        if other["transaction_id"] == receipt["transaction_id"] or (
                other["session_uuid"] == receipt["session_uuid"]):
            raise GraphRefusal("receipt_candidate_collision", work_id,
                               other_id)
    policy = _policy(policy_fn, decl["profile"], work_id)
    required = policy.get("required_checks") if isinstance(
        policy, dict) else None
    if not isinstance(required, (list, tuple)) or not required:
        raise GraphRefusal("receipt_missing_required_check", work_id,
                           "policy names no required checks")
    evidence = required_check_evidence(policy, txn_facts)
    missing = sorted(c for c, ok in evidence.items() if not ok)
    if missing or receipt["required_checks"] != {
            c: True for c in required}:
        raise GraphRefusal("receipt_missing_required_check", work_id,
                           missing or "required_checks differ")
    if live_fingerprint is None or (
            live_fingerprint != receipt["manifest_digest"]):
        raise GraphRefusal("receipt_candidate_changed", work_id)
    if s["cancelled"] or record["cancel_requested"]:
        raise GraphRefusal("vertex_cancel_requested", work_id)
    if record["state"] in TERMINAL_STATES:
        raise GraphRefusal("vertex_terminal", work_id)
    if record["state"] != "running":
        raise GraphRefusal("vertex_not_running", work_id)
    return RECEIPT_VALID


def publish(state, receipt, txn_facts, live_fingerprint, owner_ok, now,
            policy_fn=None):
    """Accept a fully validated receipt: the vertex succeeds, the receipt is
    stored and its slot is released exactly once. An identical receipt
    identity already stored returns an equal copy of the state."""
    s = _enter(state)
    _parse_now(now)
    outcome = validate_receipt(s, receipt, txn_facts, live_fingerprint,
                               owner_ok, policy_fn)
    if outcome == RECEIPT_ALREADY_PUBLISHED:
        return _exit(s)
    work_id = receipt["work_id"]
    record = s["vertices"][work_id]
    _release_slot(s, work_id, record["lease_epoch"], "terminal_receipt", now)
    record["state"] = "succeeded"
    record["receipt"] = copy.deepcopy(receipt)
    _event(s, "publish", work_id, record["lease_epoch"],
           {"transaction_id": receipt["transaction_id"],
            "manifest_digest": receipt["manifest_digest"]}, now)
    return _exit(s)


# --------------------------------------------------------------------------- #
# Join reducer.                                                               #
# --------------------------------------------------------------------------- #


def _join_declaration(state, join_id):
    canonical = _canon_uuid(join_id)
    current = _current_revision(state)
    if canonical is not None and current is not None:
        for join in current["joins"]:
            if join["join_id"] == canonical:
                return join
    raise GraphRefusal("join_unknown", detail=join_id)


def reduce_join(state, join_id, live_fingerprints):
    """The deterministic decision for one declared join in the current
    revision. Pure: no timestamp, owner id, merge or source field."""
    s = _enter(state)
    join = _join_declaration(s, join_id)
    fingerprints = (live_fingerprints
                    if isinstance(live_fingerprints, dict) else {})
    statuses = derive_statuses(s)
    held = {slot["work_id"] for slot in held_slots(s)}
    members = sorted(join["requires"])
    for work_id in members:
        if statuses.get(work_id) in ("waiting", "ready", "claimed",
                                     "running") or work_id in held:
            raise GraphRefusal("early_join", work_id, join["join_id"])
    blocked = any(statuses.get(w) in ("failed", "cancelled", "blocked")
                  for w in members)
    material = []
    for work_id in members:
        record = s["vertices"][work_id]
        receipt = record["receipt"]
        if not blocked:
            if receipt is None or (
                    receipt["work_id"], receipt["lease_epoch"],
                    receipt["session_uuid"], receipt["graph_revision"]) != (
                    work_id, record["lease_epoch"], record["session_uuid"],
                    record["claim_revision"]):
                raise GraphRefusal("graph_state_inconsistent", work_id,
                                   "receipt does not bind the vertex")
            if fingerprints.get(work_id) != receipt["manifest_digest"]:
                raise GraphRefusal("receipt_candidate_changed", work_id)
        material.append({
            "work_id": work_id, "state": statuses.get(work_id),
            "lease_epoch": record["lease_epoch"],
            "session_uuid": record["session_uuid"],
            "transaction_id": (None if receipt is None
                               else receipt["transaction_id"]),
            "manifest_digest": (None if receipt is None
                                else receipt["manifest_digest"])})
    body = {"graph_id": s["graph_id"], "graph_revision": s["revision"],
            "join_id": join["join_id"], "rule": join["rule"],
            "outcome": "blocked" if blocked else "joined",
            "members": material}
    decision = {"schema_version": SCHEMA_VERSION, "record": "JoinDecision"}
    decision.update(body)
    decision["decision_digest"] = digest(body)
    return decision


def join(state, join_id, live_fingerprints, now):
    """Store the join decision once per (join_id, current revision); a
    repeated join returns the stored decision unchanged."""
    s = _enter(state)
    _parse_now(now)
    declaration = _join_declaration(s, join_id)
    canonical = declaration["join_id"]
    key = str(s["revision"])
    stored = s["joins"].get(canonical, {}).get(key)
    if stored is not None:
        return _exit(s), copy.deepcopy(stored)
    decision = reduce_join(s, canonical, live_fingerprints)
    s["joins"].setdefault(canonical, {})[key] = copy.deepcopy(decision)
    _event(s, "join", None, None,
           {"join_id": canonical, "graph_revision": s["revision"],
            "outcome": decision["outcome"],
            "decision_digest": decision["decision_digest"]}, now)
    return _exit(s), decision


# --------------------------------------------------------------------------- #
# Read model.                                                                 #
# --------------------------------------------------------------------------- #


def status_view(state):
    s = _enter(state)
    return {"graph_id": s["graph_id"], "revision": s["revision"],
            "cancelled": s["cancelled"], "effective_cap": effective_cap(s),
            "held_slots": [{"seq": slot["seq"], "work_id": slot["work_id"],
                            "lease_epoch": slot["lease_epoch"],
                            "acquired_at": slot["acquired_at"]}
                           for slot in held_slots(s)],
            "ready_order": ready_order(s), "statuses": derive_statuses(s),
            "joins": copy.deepcopy(s["joins"])}
