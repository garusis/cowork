#!/usr/bin/env python3
"""The single, topology-driven, file-only transport for every cross-role handoff.

Every cross-role edge in cowork's orchestration (scout/reviewer, scout->planner,
planner/advisor, planner->builder, builder/reviewer, both hand-backs, every
controller-switch case, peer evaluation, and context revision/resume) is served
by ONE renderer here. A handoff carries only:

- absolute authoritative FILE PATHS (each with byte size + sha256), which the
  receiving CLI reads from disk (the CLIs have filesystem access, so the files
  on disk are the source of truth), and
- short, CONTENT-FREE orchestration facts (closed-schema enums, counts, hashes,
  path/byte metadata, and normalized reason codes).

It NEVER embeds bodies, findings, questions, hand-back payloads, controller
switch-recovery text, evaluation verdict bodies, deterministic diffs, or the
shared session-context text. Only the original user->scout prompt inlines user
text; every CROSS-ROLE delivery of the shared context — including the
scout->planner and planner->builder seeds — carries it by PATH (both seed edges
require a `context` artifact slot).

## Single choke point

`render_handoff(edge_id, *, artifacts, facts, ctx)` is the ONLY function anywhere
that emits a cross-role prompt block. Every cross-role prompt builder in
cowork.py delegates to it and returns its output; none does its own body
templating. All prose lives in the per-edge `render` callables in the `EDGES`
registry, so a newly added role/edge cannot invent a divergent embedding path.

## Fail closed on STRUCTURE, tolerant on CONTENT

Structural violations RAISE: an unknown/unregistered edge id, a required path
that is not absolute, or a fact value outside the closed-schema/normalized
whitelist. This is distinct from CONTENT tolerance: an absolute-but-missing or
unreadable file degrades to a "(missing on disk)" descriptor and never raises,
preserving the existing degrade-never-raise behavior.

Pure stdlib.
"""

import datetime
import hashlib
import os
import uuid


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


# --------------------------------------------------------------------------- #
# Path-first descriptor primitives (absorbed from the retired diff-packet).     #
# --------------------------------------------------------------------------- #

FULL_REREAD_INSTRUCTION = (
    "Read the FULL current files from disk at the paths above. They are the "
    "authoritative current source of truth for your review."
)

DEFAULT_FULL_REREAD_HEADER = (
    "Authoritative artifacts (current files on disk — the source of truth):")

# The controller-switch prompt marker. Exposed so a CONSUMER (e.g. the role loop
# suppressing the echo of controller-only recovery context) can detect a switch
# handoff without re-hardcoding the literal — the transport owns the string.
SWITCH_HANDOFF_MARKER = "[controller switch handoff]"


class UnknownEdgeError(ValueError):
    """Raised when render_handoff is given an unregistered edge id."""


class RelativePathError(ValueError):
    """Raised when a required artifact path is not absolute (fail-closed)."""


class ContentFreeError(ValueError):
    """Raised when a fact value is outside the content-free whitelist."""


class MissingSourceError(ValueError):
    """Raised when an edge is rendered without its required artifact source(s),
    or with an artifact whose source is not declared for that edge."""


class ContextError(ValueError):
    """Raised when the `ctx` composition dict carries a key the edge does not
    declare, or a value that fails the key's closed schema (e.g. a body smuggled
    through an unvalidated ctx field)."""


# Registry-owned labels for each artifact SOURCE SLOT. The descriptor line a
# cross-role prompt shows uses THESE labels, not any caller-supplied `label`, so
# a caller cannot smuggle a body through the label field.
SLOT_LABELS = {
    "context": "shared session context (same the active roles were given)",
    "intel_json": "scout intel JSON (machine source of truth)",
    "intel_md": "scout intel markdown (the user's review surface)",
    "plan_json": "plan JSON (machine source of truth)",
    "plan_md": "plan markdown (the user's review surface)",
    "build_status": "builder status JSON (status + verification log)",
    "build_summary": "builder markdown summary (the user's review surface)",
    "build_baseline": "build-baseline metadata (per-root start commit + dirty)",
    "verification_receipt": "owned verification receipt (orchestrator-run "
                            "transaction result)",
    "review": "reviewer verdict + findings (JSON)",
    "payload": "hand-back note",
    "artifacts": "session artifact",
    "recovery": "switch recovery note (free-form, orchestration only)",
    "pending_turn": "failed pending turn (process it after orienting)",
    "verdict": "reviewer verdict + findings (JSON)",
    "reviewed": "the artifact you reviewed",
    "upstream": "consumed upstream artifact",
}


_RENDER_TOKEN = object()


class HandoffBlock(str):
    """A rendered cross-role prompt block that ALSO carries how its artifacts
    were delivered and the content-free descriptor records, so both the prompt
    and the trace/report accounting derive from ONE object (no re-inference).

    Transparent as a plain `str` (formatting/concatenation produce a normal
    str), so every existing string consumer keeps working unchanged.

    - ``edge_id`` names the registry edge it was rendered from.
    - ``edge_ids`` tuple of all edge identities carried by this block or composition.
    - ``delivery`` is always "path" (bodies are never embedded).
    - ``embedded`` maps each artifact path to the BYTES it contributes to the
      prompt (its descriptor line only — never the body).
    - ``descriptors`` is the ordered list of content-free per-file accounting
      records (``{path, bytes, sha256, delivery, embedded_bytes}``) the trace
      and report read directly."""

    def __new__(cls, text, *, _token=None, edge_id=None, edge_ids=None,
                delivery="path", embedded=None, descriptors=None):
        if _token is not _RENDER_TOKEN:
            raise TypeError("HandoffBlock must be produced by render_handoff")
        obj = super().__new__(cls, text)
        obj.edge_id = edge_id
        if edge_ids:
            obj.edge_ids = tuple(edge_ids)
        elif edge_id:
            obj.edge_ids = (edge_id,)
        else:
            obj.edge_ids = ()
        obj.delivery = delivery
        obj.embedded = dict(embedded or {})
        obj.descriptors = list(descriptors or [])
        return obj


_DELIVERY_TOKEN = object()


class _BoundaryText(str):
    def __new__(cls, text, *, _token=None, kind=None):
        if _token is not _DELIVERY_TOKEN:
            raise TypeError("boundary text must be created by cowork_handoff")
        obj = super().__new__(cls, text)
        obj.kind = kind
        return obj


def _initial_user_text(text):
    return _BoundaryText(text, _token=_DELIVERY_TOKEN, kind="initial_user")


def _static_role_text(text):
    return _BoundaryText(text, _token=_DELIVERY_TOKEN, kind="static_role")


def _user_lead_reply(text):
    return _BoundaryText(text, _token=_DELIVERY_TOKEN, kind="user_lead_reply")


# Cross-role composition uses one transport-owned separator value.  Keeping the
# separator typed at its source means orchestration code never has to mint a
# generic "trusted string" merely to join two renderer-produced blocks.
STATIC_SEPARATOR = _static_role_text("\n\n")


class DeliveryEnvelope(str):
    """Opaque gateway value produced only by the transport constructors.

    Cross-role envelopes retain the exact edge identities and descriptors from
    their originating ``HandoffBlock`` objects. Direct envelopes are limited to
    an explicit closed set for the initial user turn, static role instructions,
    and subsequent user-facing lead turns.
    """

    def __new__(cls, text, *, _token=None, delivery_class=None, edge_ids=None,
                descriptors=None, direct_kind=None):
        if _token is not _DELIVERY_TOKEN:
            raise TypeError("DeliveryEnvelope must be created by cowork_handoff")
        obj = super().__new__(cls, text)
        obj.delivery_class = delivery_class
        obj.edge_ids = tuple(edge_ids or ())
        obj.descriptors = list(descriptors or [])
        obj.direct_kind = direct_kind
        return obj


def compose_handoff_blocks(*parts):
    """Safely compose multiple trusted HandoffBlocks and static role fragments.

    Every part MUST be either a HandoffBlock or a _BoundaryText with kind='static_role'.
    Untyped plain strings (or un-persisted smuggled content) are rejected.
    """
    valid_blocks = []
    text_parts = []
    edge_ids = []
    descriptors = []
    seen_paths = set()

    for p in parts:
        if p is None or p == "":
            continue
        if isinstance(p, HandoffBlock):
            valid_blocks.append(p)
            text_parts.append(str(p))
            eids = getattr(p, "edge_ids", None) or ((p.edge_id,) if p.edge_id else ())
            for eid in eids:
                if eid and eid not in edge_ids:
                    edge_ids.append(eid)
            for d in getattr(p, "descriptors", []) or []:
                if isinstance(d, dict) and d.get("path") and d["path"] not in seen_paths:
                    descriptors.append(d)
                    seen_paths.add(d["path"])
        elif isinstance(p, _BoundaryText) and p.kind == "static_role":
            text_parts.append(str(p))
        else:
            raise TypeError("cannot compose handoff blocks with untyped string %r" % (p,))

    if not valid_blocks:
        raise TypeError("compose_handoff_blocks requires at least one HandoffBlock")

    primary_edge = edge_ids[0] if edge_ids else valid_blocks[0].edge_id
    full_text = "".join(text_parts)
    return HandoffBlock(
        full_text, _token=_RENDER_TOKEN, edge_id=primary_edge,
        edge_ids=edge_ids, delivery="path",
        embedded={}, descriptors=descriptors)


def cross_role_delivery(*parts):
    """Compose delivered bytes from exact trusted parts, never a free text arg.

    At least one part must be a render_handoff-produced HandoffBlock. Remaining
    parts may only be typed static-role instructions from the closed boundary
    constructor. The final text is assembled here from those exact values.
    """
    blocks = [p for p in parts if isinstance(p, HandoffBlock)]
    if not blocks:
        raise TypeError("cross-role delivery requires HandoffBlock provenance")
    if not all(isinstance(p, HandoffBlock)
               or (isinstance(p, _BoundaryText)
                   and p.kind == "static_role")
               for p in parts):
        raise TypeError("cross-role delivery accepts only handoff/static parts")
    edge_ids = []
    descriptors = []
    seen_paths = set()
    for block in blocks:
        b_eids = getattr(block, "edge_ids", None) or ((block.edge_id,) if block.edge_id else ())
        for eid in b_eids:
            if eid and eid not in edge_ids:
                if eid not in EDGES:
                    raise UnknownEdgeError("handoff block has no registered edge identity: %r" % (eid,))
                edge_ids.append(eid)
        for d in getattr(block, "descriptors", []) or []:
            if isinstance(d, dict) and d.get("path") and d["path"] not in seen_paths:
                descriptors.append(d)
                seen_paths.add(d["path"])
    return DeliveryEnvelope(
        "".join(str(p) for p in parts), _token=_DELIVERY_TOKEN,
        delivery_class="cross_role",
        edge_ids=edge_ids, descriptors=descriptors)


def direct_delivery(value):
    """Envelope a typed value from an explicit non-handoff boundary."""
    if not isinstance(value, _BoundaryText):
        raise TypeError("direct delivery requires typed boundary provenance")
    return DeliveryEnvelope(
        str(value), _token=_DELIVERY_TOKEN, delivery_class="direct",
        direct_kind=value.kind)


def _read_raw(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _descriptor_entries(artifacts):
    """Read each artifact once and build its content-free descriptor entry
    (label, path, bytes, sha256, present). Tolerant: a missing/unreadable file
    degrades to a present=False entry, never raises."""
    entries = []
    for art in artifacts:
        path = art["path"]
        raw = _read_raw(path)
        present = raw is not None
        raw_bytes = raw if raw is not None else b""
        entries.append({
            "label": art.get("label") or os.path.basename(path),
            "path": path,
            "kind": art.get("kind"),
            "bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "present": present,
            # A missing file still gets a digest here, because the prompt line
            # renders one — but it is the digest of NOTHING, and without this
            # flag it is indistinguishable from a real empty file's. That
            # ambiguity is how a missing artifact came to be treated as content
            # (CV-008). Consumers check this flag; the sealed envelope below
            # refuses to record the digest at all.
            "empty_or_missing": (not present) or len(raw_bytes) == 0,
        })
    return entries


def _descriptor_line(e):
    present = "" if e["present"] else " (missing on disk)"
    return ("  - %s: %s  [%d bytes, sha256 %s]%s"
            % (e["label"], e["path"], e["bytes"], e["sha256"][:12], present))


def _descriptor_lines(entries):
    return "\n".join(_descriptor_line(e) for e in entries)


def _descriptor_records(entries):
    """The content-free per-file accounting the trace/report consume: one
    ``{path, bytes, sha256, delivery, embedded_bytes}`` per entry. `delivery`
    is always "path" and `embedded_bytes` is 0 (path-only delivery embeds zero body content)."""
    records = []
    for e in entries:
        records.append({
            "path": e["path"],
            "bytes": e["bytes"],
            "sha256": e["sha256"],
            "delivery": "path",
            "embedded_bytes": 0,
        })
    return records


def _embedded_map(entries):
    return {e["path"]: len(_descriptor_line(e).encode("utf-8")) for e in entries}


# `render_handoff` is the ONLY renderer. There is deliberately no public
# second builder (the old `build_full_reread_packet`) that could emit a
# path-first block WITHOUT the registry / absolute-path / source / fact / ctx
# checks — every cross-role prompt must go through the registry choke point.


def build_diff_recipe(repos):
    """The build-reviewer's live-delta capture recipe — CONTENT-FREE static
    instructions parameterized only by the selected repo roots (paths +
    has_head booleans). It is built HERE (owned by the transport) from validated
    repo metadata, so no caller can inject arbitrary text through it. `repos` is
    a list of ``{"path": <abs>, "has_head": bool}`` (empty -> single-cwd form)."""
    if not repos:
        return (
            "The unit of review is the builder's FULL working-tree delta against "
            "this plan. The delta is NOT embedded here — capture the COMPLETE "
            "delta yourself. Plain `git diff` is insufficient: it omits STAGED "
            "changes and UNTRACKED new files (and the builder creates files). Run:"
            "\n  - `git status --porcelain` — every staged, unstaged, and "
            "untracked path at a glance;"
            "\n  - `git diff HEAD` (or `git diff --stat HEAD` first, then targeted "
            "`git diff HEAD -- <path>`) — all tracked staged+unstaged changes "
            "since the last commit;"
            "\n  - read each untracked/new file directly — it will NOT appear in "
            "`git diff`."
            "\nReview the full delta critically against the plan and context "
            "above.")
    blocks = []
    for r in repos:
        path = r.get("path", ".")
        if r.get("has_head"):
            blocks.append(
                "  Repo %s (has a baseline commit):"
                "\n    - `git -C %s status --porcelain` — staged, unstaged, and "
                "untracked paths;"
                "\n    - `git -C %s diff HEAD` (or `git -C %s diff --stat HEAD` "
                "first, then targeted `git -C %s diff HEAD -- <path>`) — all "
                "tracked staged+unstaged changes since the last commit;"
                "\n    - read each untracked/new file under %s directly — it "
                "will NOT appear in `git diff`."
                % (path, path, path, path, path, path))
        else:
            blocks.append(
                "  Repo %s (NO baseline commit — unborn repo or non-git "
                "fallback; do NOT run `git diff HEAD`, it fails):"
                "\n    - `git -C %s status --porcelain` — every path at a glance;"
                "\n    - `git -C %s diff --cached` and `git -C %s diff` — staged "
                "and unstaged changes;"
                "\n    - read untracked/new files under %s directly."
                % (path, path, path, path, path))
    return (
        "The unit of review is the builder's FULL working-tree delta against "
        "this plan, taken as the UNION of the deltas of EACH of these selected "
        "repo roots. The delta is NOT embedded here — capture the COMPLETE delta "
        "yourself, per root. Plain `git diff` is insufficient: it omits STAGED "
        "changes and UNTRACKED new files (and the builder creates files). "
        "Capture the delta of EACH of these repos:"
        "\n%s"
        "\nReview the union of per-root deltas critically against the plan and "
        "context above. An empty delta in a repo the plan touches is a finding; "
        "ignore repos the plan does not list." % "\n".join(blocks))


def _valid_repos(value):
    """A `repos` ctx value: a list of ``{"path": <abs str>, "has_head": bool}``.
    Bounds the diff-recipe input so no free-form text rides through it."""
    if not isinstance(value, (list, tuple)):
        return False
    for r in value:
        if not isinstance(r, dict):
            return False
        path = r.get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            return False
        if not isinstance(r.get("has_head"), bool):
            return False
    return True


# Per-ctx-key closed schema (blocks body-smuggling through unvalidated ctx):
#  - context_update_prefix: must itself be a HandoffBlock (built via
#    render_handoff, so path-only);
#  - repos: a validated list of {abs path, bool has_head} (drives the recipe).
_CTX_SCHEMAS = {
    "context_update_prefix": lambda v: isinstance(v, HandoffBlock),
    "repos": _valid_repos,
}


def _assert_ctx(edge_id, ctx, allowed):
    for key, value in (ctx or {}).items():
        if key not in allowed:
            raise ContextError(
                "edge %r: ctx key %r is not declared for this edge (declared: "
                "%s)" % (edge_id, key, sorted(allowed)))
        schema = _CTX_SCHEMAS.get(key)
        if schema is not None and not schema(value):
            raise ContextError(
                "edge %r: ctx key %r has a value outside its closed schema — a "
                "cross-role body must not ride through ctx" % (edge_id, key))


# --------------------------------------------------------------------------- #
# Content-free fact boundary. Facts are not merely single tokens — each fact key #
# is validated against a CLOSED per-key schema (a fixed enum set, or a           #
# normalized reason/source code), so an unknown one-token value like             #
# team=["not-a-real-role"] is rejected, not silently accepted.                   #
# --------------------------------------------------------------------------- #

# Canonical role/pair registry.  Orchestration, fact validation, and topology
# validation all derive from this single declaration. `non_handoff` is the
# explicit classification for a role that never participates in a cross-role
# edge (currently only the pre-phase worktree helper).
ROLE_REGISTRY = {
    "scout": {"order": 0, "reviewer": "scout-reviewer"},
    "scout-reviewer": {"order": 1, "reviews": "scout"},
    "planner": {"order": 2, "reviewer": "planning-advisor"},
    "planning-advisor": {"order": 3, "reviews": "planner"},
    "builder": {"order": 4, "reviewer": "build-reviewer"},
    "build-reviewer": {"order": 5, "reviews": "builder"},
    "worktree": {"order": 6, "non_handoff": True, "selectable": False},
}
ROLES = frozenset(ROLE_REGISTRY)
CONTROLLERS = frozenset({"claude", "codex", "opencode", "unknown"})
PHASES = frozenset({"scouting", "planning", "building"})
ARTIFACT_NOUNS = frozenset({"intel", "plan", "build"})

# Back-compat: the union of all closed enums (kept for external callers/tests).
KNOWN_ENUMS = ROLES | CONTROLLERS | PHASES | ARTIFACT_NOUNS | frozenset({
    "needs_input", "ready_for_review", "handoff_back", "approve", "revise",
    "needs_user", "seed", "resume", "handback", "switch", "eval",
    "review_ctx", "context_update",
})

# A single normalized token: no whitespace, bounded length. Covers reason/source
# codes, hashes, and path/byte metadata. Free-form authored text (whitespace /
# newlines, or over-long) is rejected by this, which is the whole point.
_MAX_TOKEN = 256


def is_content_free_token(value):
    """True when `value` is a single content-free token — a normalized reason
    code, a hash, or path/byte metadata — safe to ride a prompt inline. Free-form
    authored text (whitespace/newlines, or over-long) is NOT."""
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    if not value or len(value) > _MAX_TOKEN:
        return False
    return not any(c.isspace() for c in value)


def _in(enum_set):
    return lambda v: isinstance(v, str) and v in enum_set


def _list_in(enum_set):
    return lambda v: isinstance(v, (list, tuple)) and all(
        isinstance(x, str) and x in enum_set for x in v)


# Per-fact CLOSED schema: a validator that accepts only the fact's legal values.
# A key without an entry falls back to the normalized-token check (reason codes).
_FACT_SCHEMAS = {
    "team": _list_in(ROLES),
    "role": _in(ROLES),
    "phase": _in(PHASES),
    "from_controller": _in(CONTROLLERS),
    "to_controller": _in(CONTROLLERS),
    "artifact_noun": _in(ARTIFACT_NOUNS),
    "reason_code": is_content_free_token,
    "source_code": is_content_free_token,
    # ORCH-050 owned-verification overlay facts (derived tokens only — a hex
    # id, digests, enum verdicts/bindings/dispositions, an integer count, a
    # boolean flag; never a byte of agent prose).
    "txn_id": is_content_free_token,
    "manifest_digest": is_content_free_token,
    "index_digest": is_content_free_token,
    "verdict": _in({"green", "red", "unverified"}),
    "final_suite_label": is_content_free_token,
    "final_suite_binding": _in({"ran_once", "not_reached", "legacy_unknown"}),
    "command_count": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "disposition": _in({"pending_review", "accepted", "superseded_by_finding",
                        "rejected"}),
    "contradiction": lambda v: isinstance(v, bool),
}


def _assert_content_free(edge_id, facts, allowed):
    for key, value in (facts or {}).items():
        if key not in allowed:
            raise ContentFreeError(
                "edge %r: fact %r is not declared for this edge" % (edge_id, key))
        schema = _FACT_SCHEMAS.get(key, is_content_free_token)
        if not schema(value):
            raise ContentFreeError(
                "edge %r: fact %r=%r is outside its closed schema — only the "
                "declared enum values (or a normalized code) are allowed inline; "
                "authored text must be written to a file and referenced by path"
                % (edge_id, key, value))


# --------------------------------------------------------------------------- #
# Persistence: materialize cross-role state as authoritative files so an edge   #
# can carry a PATH instead of a body. Tolerant — a failure returns None and the #
# descriptor degrades to "(missing on disk)"; it never raises.                  #
# --------------------------------------------------------------------------- #

def _safe(name):
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (name or ""))


def _write_file(assets_dir, filename, text):
    if not assets_dir:
        return None
    path = os.path.join(assets_dir, filename)
    try:
        os.makedirs(assets_dir, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text if text is not None else "")
        return path
    except OSError:
        return None


def persist_context_file(assets_dir, revision, text):
    """Materialize the shared session context to a revision-keyed file so
    cross-role RE-delivery references its PATH (a stale file is never served —
    the revision is in the name). Returns the absolute path, or None."""
    try:
        rev = int(revision or 0)
    except (TypeError, ValueError):
        rev = 0
    return _write_file(assets_dir, "context.rev%d.md" % rev, text or "")


def persist_pending_turn_file(assets_dir, role, text):
    """Materialize a failed pending turn (a whole prompt body) to a file for the
    controller-switch edge. Returns the absolute path, or None."""
    return _write_file(assets_dir, "switch.pending_turn.%s.txt" % _safe(role),
                       text or "")


def persist_switch_recovery_file(assets_dir, role, text):
    """Materialize free-form switch recovery text (a reason/source/diagnostic
    that is NOT a normalized code) to a file. Returns the absolute path, or
    None."""
    return _write_file(assets_dir, "switch.recovery.%s.txt" % _safe(role),
                       text or "")


def persist_build_baseline_file(assets_dir, note):
    """Materialize the build-baseline / repo metadata note to a file for the
    build-reviewer edge. Returns the absolute path, or None."""
    return _write_file(assets_dir, "build_baseline.txt", note or "")


# --------------------------------------------------------------------------- #
# Declarative topology / edge registry — the SINGLE place cross-role edges are  #
# wired. All cross-role prompt prose lives in the per-edge `render` callables.  #
# --------------------------------------------------------------------------- #

def _read_from_disk_block(descriptor_lines):
    return "%s\n\n%s" % (descriptor_lines, FULL_REREAD_INSTRUCTION)


# ---- route 1: scout <-> scout-reviewer situational context ---------------- #

def _render_review_ctx(descriptor_lines, facts, ctx):
    team = ", ".join(facts.get("team") or []) or "(unspecified)"
    return (
        "The files below are the current authoritative files on disk — the "
        "SAME shared initial context the reviewed role was given, plus the "
        "artifact(s) to review. Read them from disk.\n%s\n\n"
        "Team on this session: %s\n\n"
        "Review the current artifact(s) critically against the shared context "
        "above.\n\n%s"
        % (descriptor_lines, team, FULL_REREAD_INSTRUCTION))


def _render_review_resume(descriptor_lines, facts, ctx):
    body = (
        "The reviewed role has updated its artifact(s) since your last review. "
        "Re-review the current authoritative files below against the current "
        "task context, and write your verdict to the review file again.\n%s\n\n"
        "%s" % (descriptor_lines, FULL_REREAD_INSTRUCTION))
    prefix = ctx.get("context_update_prefix")
    return (prefix + "\n\n" + body) if prefix else body


# ---- routes 2/5/8: reviewer -> lead hand-back (findings by path) ---------- #

def _render_handback_revise(descriptor_lines, facts, ctx):
    noun = facts.get("artifact_noun") or "artifact"
    return (
        "[reviewer handoff] A reviewer checked your %s and it is not ready to "
        "hand off yet. Read the reviewer's findings from the review file on "
        "disk:\n%s\n"
        "Address them, update your %s, and set status back to "
        "ready_for_review when done. Do not mention the reviewer to the user."
        % (noun, descriptor_lines, noun))


def _render_handback_needs_user(descriptor_lines, facts, ctx):
    noun = facts.get("artifact_noun") or "artifact"
    return (
        "[reviewer handoff] Before this can go to the user for approval, a "
        "blocking product question is unresolved. Read the reviewer's "
        "user_question (and its findings) from the review file on disk:\n%s\n"
        "Put that question to the user in your own next reply. You MAY rephrase "
        "it into your own voice, but you must NOT change its meaning or omit any "
        "part of its context. Then set status back to needs_input. Do not "
        "mention the reviewer to the user." % descriptor_lines)


# ---- routes 3/6: active-lead seed (context MAY be inline — decision 2) ----- #

def _render_planner_seed(descriptor_lines, facts, ctx):
    # scout->planner is a CROSS-ROLE handoff: the shared context is a file the
    # planner reads from disk (referenced among the descriptors), NEVER inline.
    return (
        "The scout phase is complete and the user APPROVED the scout intel. "
        "Digest it and drive the planning conversation. The approved intel AND "
        "the current shared context are the files on disk below.\n\n%s"
        % _read_from_disk_block(descriptor_lines))


def _render_builder_seed(descriptor_lines, facts, ctx):
    # planner->builder is a CROSS-ROLE handoff: the shared context rides by path.
    return (
        "The planning phase is complete and the user APPROVED the plan. "
        "Execute it: make the code changes, verify them, and drive the build "
        "conversation. The approved plan AND the current shared context are the "
        "files on disk below.\n\n%s"
        % _read_from_disk_block(descriptor_lines))


def _render_intel_updated(descriptor_lines, facts, ctx):
    return (
        "The scout intel changed since you started planning: your hand-back was "
        "executed, the scout re-investigated, and the user approved the updated "
        "intel. Digest it and continue planning. Keep prior plan content only "
        "where it remains compatible.\n\n%s"
        % _read_from_disk_block(descriptor_lines))


def _render_plan_updated(descriptor_lines, facts, ctx):
    return (
        "The plan changed since you started building: your hand-back was "
        "executed, the planner re-planned, and the user approved the UPDATED "
        "plan. Digest the changes and continue building. Keep prior work only "
        "where it remains compatible.\n\n%s"
        % _read_from_disk_block(descriptor_lines))


# ---- route 4: planner -> planning-advisor (plan AND approved intel) -------- #

def _render_advisor_ctx(descriptor_lines, facts, ctx):
    team = ", ".join(facts.get("team") or []) or "(unspecified)"
    return (
        "The files below are the current authoritative files on disk — the "
        "SAME shared session context the planner was given, the planner's "
        "current plan to review, AND the approved scout intel the plan must "
        "cover. Read them from disk.\n%s\n\n"
        "Team on this session: %s\n\n"
        "Review the planner's current plan critically against the shared "
        "context and verify its criteria-coverage against the approved intel "
        "above.\n\n%s" % (descriptor_lines, team, FULL_REREAD_INSTRUCTION))


# ---- route 7: builder -> build-reviewer (artifacts by path; live diff) ----- #

def _render_owned_verification_block(facts):
    """The ORCH-050 owned-facts overlay block, rendered from the edge's
    closed-schema fact tokens (derived from Cowork-owned records — never from
    agent prose). Empty string when no owned receipt binds (legacy sessions
    render exactly as before). A set `contradiction` flag renders the marked
    CONTRADICTION line."""
    if not facts.get("txn_id"):
        return ""
    lines = [
        "Owned verification receipt (orchestrator-derived — the authoritative",
        "verification fact base for this review; the builder's own verification",
        "prose is secondary to it):",
        "  transaction=%s  verdict=%s  final_suite=%s (%s)"
        % (facts.get("txn_id"), facts.get("verdict"),
           facts.get("final_suite_label"), facts.get("final_suite_binding")),
        "  manifest=%s  index=%s  commands=%s"
        % (str(facts.get("manifest_digest"))[:12],
           str(facts.get("index_digest"))[:12], facts.get("command_count")),
        "  disposition=%s" % facts.get("disposition"),
        "  The receipt file itself (result.json) reaches you by absolute path",
        "  among the artifacts above (the verification_receipt slot).",
    ]
    if facts.get("contradiction"):
        lines.append(
            "  CONTRADICTION: the builder's own verification prose is missing")
        lines.append(
            "  or disagrees with this receipt — trust the receipt, not the"
            " prose.")
    return "\n".join(lines)


def _render_build_reviewer_ctx(descriptor_lines, facts, ctx):
    team = ", ".join(facts.get("team") or []) or "(unspecified)"
    parts = [
        "The files below are the current authoritative files on disk — the "
        "SAME shared session context the builder was given, both approved plan "
        "files, the builder's status JSON, the builder's markdown summary "
        "(when present), and the build-baseline metadata. Read them from disk."
        "\n%s" % descriptor_lines,
        "Team on this session: %s" % team,
    ]
    owned = _render_owned_verification_block(facts)
    if owned:
        parts.append(owned)
    parts.extend([FULL_REREAD_INSTRUCTION, build_diff_recipe(ctx.get("repos"))])
    return "\n\n".join(parts)


def _render_build_reviewer_resume(descriptor_lines, facts, ctx):
    parts = [
        "The builder has updated its work since your last review. Re-review "
        "the current full working-tree delta against the plan and the "
        "builder's current status. The current authoritative files are on "
        "disk:\n%s" % descriptor_lines,
    ]
    owned = _render_owned_verification_block(facts)
    if owned:
        parts.append(owned)
    parts.extend([FULL_REREAD_INSTRUCTION, build_diff_recipe(ctx.get("repos"))])
    body = "\n\n".join(parts)
    prefix = ctx.get("context_update_prefix")
    return (prefix + "\n\n" + body) if prefix else body


# ---- routes 9/10: hand-back wake (payload by path) ------------------------ #

def _render_scout_handback_wake(descriptor_lines, facts, ctx):
    return (
        "The planner handed the work back to you mid-planning (the user "
        "confirmed the hand-back). Re-run your full cycle: investigate, clarify "
        "with the user, update your intel file, and set status "
        "ready_for_review when done. Read the planner's hand-back note from the "
        "file on disk:\n%s" % descriptor_lines)


def _render_planner_handback_wake(descriptor_lines, facts, ctx):
    return (
        "The builder handed the work back to you mid-build (the user confirmed "
        "the hand-back). Re-plan as needed: update your plan files, clarify "
        "with the user, and set status ready_for_review when done. Read the "
        "builder's hand-back note from the file on disk:\n%s" % descriptor_lines)


# ---- route 11: controller switch (paths + content-free facts) -------------- #

def _render_switch(descriptor_lines, facts, ctx):
    phase = facts.get("phase") or "unknown"
    role = facts.get("role") or "unknown"
    from_controller = facts.get("from_controller") or "unknown"
    to_controller = facts.get("to_controller") or "unknown"
    lines = [
        SWITCH_HANDOFF_MARKER,
        "You are continuing an existing cowork %s phase as %s." % (phase, role),
        "Controller switched: %s -> %s." % (from_controller, to_controller),
        "This is a fresh %s provider conversation. Hidden chat history from %s "
        "is not available; cowork-visible session state, artifacts, shared "
        "context, and the working tree continue."
        % (to_controller, from_controller),
    ]
    reason = facts.get("reason_code")
    if reason:
        lines.append("Switch reason: %s." % reason)
    source = facts.get("source_code")
    if source:
        lines.append("Switch source: %s." % source)
    if descriptor_lines:
        lines.extend([
            "",
            "The shared context, the current artifacts, any free-form switch "
            "recovery note, and the failed pending turn (if any) are the "
            "authoritative files on disk below — read them from disk to orient "
            "yourself, then process the failed pending turn:",
            descriptor_lines,
        ])
    return "\n".join(lines).strip()


def _render_pending_resume(descriptor_lines, facts, ctx):
    return (
        "[pending turn resume]\n"
        "You are resuming a session after a failed turn. Read the files below "
        "from disk to orient yourself, then process the failed pending turn:\n"
        "%s" % descriptor_lines)


# ---- route 13: context revision wake (context by path) --------------------- #

def _render_context_update(descriptor_lines, facts, ctx):
    return (
        "New user context was provided for this resumed cowork session.\n\n"
        "Treat this as the current task context. Keep prior session knowledge "
        "only where it remains compatible. Read the current context from the "
        "file on disk:\n%s" % descriptor_lines)


# ---- route 12: peer evaluation evidence (verdict + consumed upstream) ------ #

def _render_eval_verdict(descriptor_lines, facts, ctx):
    return (
        "The reviewer's verdict + findings for this round (and the artifact you "
        "reviewed, when named) are the current files on disk — read them before "
        "scoring:\n%s\n\n%s" % (descriptor_lines, FULL_REREAD_INSTRUCTION))


def _render_eval_upstream(descriptor_lines, facts, ctx):
    # Static header (no caller-supplied label) — the descriptor line already
    # names the file; nothing free-form rides here.
    return (
        "The upstream artifact(s) this phase consumed (current files on disk — "
        "the authoritative source of truth; read them before scoring):\n%s\n\n%s"
        % (descriptor_lines, FULL_REREAD_INSTRUCTION))


# Each edge declares the artifact SOURCE SLOTS it may carry and, of those, which
# are REQUIRED. render_handoff fails closed (SC1/SC2) when: an artifact is
# untagged, an artifact's source is not declared for the edge, or a required slot
# has no artifact. Multi-file inputs are modelled as DISTINCT slots (plan_json +
# plan_md, intel_json + intel_md, build_status + build_baseline) so per-file
# cardinality is enforced — shipping only one half of a pair is rejected. Every
# real route's artifacts are tagged with their granular `source` in cowork.py.
EDGES = {
    # route 1 (intel_md optional: the scout-reviewer receives both, but a
    # JSON-only intel call is still path-first and legal)
    "scout->scout-reviewer:review_ctx": {
        "from_role": "scout", "to_role": "scout-reviewer", "kind": "review_ctx",
        "sources": ["context", "intel_json", "intel_md"],
        "required": ["context", "intel_json"],
        "facts": ("team",), "render": _render_review_ctx,
    },
    "scout->scout-reviewer:review_resume": {
        "from_role": "scout", "to_role": "scout-reviewer", "kind": "resume",
        "sources": ["intel_json", "intel_md"], "required": ["intel_json"],
        "facts": ("team",), "ctx_keys": ("context_update_prefix",),
        "render": _render_review_resume,
    },
    # routes 2/5/8 (reviewer -> lead, generic over the three pairs)
    "reviewer->lead:handback_revise": {
        "from_role": "reviewer", "to_role": "lead", "kind": "handback",
        "sources": ["review"], "required": ["review"],
        "facts": ("artifact_noun",), "render": _render_handback_revise,
    },
    "reviewer->lead:handback_needs_user": {
        "from_role": "reviewer", "to_role": "lead", "kind": "handback",
        "sources": ["review"], "required": ["review"],
        "facts": ("artifact_noun",), "render": _render_handback_needs_user,
    },
    # route 3 (cross-role seed: context rides by PATH, not inline)
    "scout->planner:seed": {
        "from_role": "scout", "to_role": "planner", "kind": "seed",
        "sources": ["context", "intel_json", "intel_md"],
        "required": ["context", "intel_json"],
        "facts": (), "render": _render_planner_seed,
    },
    "scout->planner:intel_updated": {
        "from_role": "scout", "to_role": "planner", "kind": "resume",
        "sources": ["intel_json", "intel_md"], "required": ["intel_json"],
        "facts": (), "render": _render_intel_updated,
    },
    # route 4 (multi-source: plan JSON+md AND BOTH approved scout intel files —
    # decision 6 / SC2 name scout.intel.json + scout.intel.md explicitly, so both
    # intel slots are required)
    "planner->planning-advisor:review_ctx": {
        "from_role": "planner", "to_role": "planning-advisor",
        "kind": "review_ctx",
        "sources": ["context", "plan_json", "plan_md", "intel_json",
                    "intel_md"],
        "required": ["context", "plan_json", "plan_md", "intel_json",
                     "intel_md"],
        "facts": ("team",), "render": _render_advisor_ctx,
    },
    "planner->planning-advisor:review_resume": {
        "from_role": "planner", "to_role": "planning-advisor", "kind": "resume",
        "sources": ["plan_json", "plan_md"], "required": ["plan_json", "plan_md"],
        "facts": ("team",), "ctx_keys": ("context_update_prefix",),
        "render": _render_review_resume,
    },
    # route 6 (cross-role seed: context rides by PATH, not inline)
    "planner->builder:seed": {
        "from_role": "planner", "to_role": "builder", "kind": "seed",
        "sources": ["context", "plan_json", "plan_md"],
        "required": ["context", "plan_json", "plan_md"],
        "facts": (), "render": _render_builder_seed,
    },
    "planner->builder:plan_updated": {
        "from_role": "planner", "to_role": "builder", "kind": "resume",
        "sources": ["plan_json", "plan_md"], "required": ["plan_json", "plan_md"],
        "facts": (), "render": _render_plan_updated,
    },
    # route 7 (build_summary + verification_receipt optional;
    # build_status + build_baseline required). The ORCH-050 overlay facts are
    # declared on BOTH edges; they ride only when an owned receipt binds.
    "builder->build-reviewer:review_ctx": {
        "from_role": "builder", "to_role": "build-reviewer",
        "kind": "review_ctx",
        "sources": ["context", "plan_json", "plan_md", "build_status",
                    "build_summary", "build_baseline",
                    "verification_receipt"],
        "required": ["context", "plan_json", "plan_md", "build_status",
                     "build_baseline"],
        "facts": ("team", "txn_id", "manifest_digest", "index_digest",
                  "verdict", "final_suite_label", "final_suite_binding",
                  "command_count", "disposition", "contradiction"),
        "ctx_keys": ("repos",),
        "render": _render_build_reviewer_ctx,
    },
    "builder->build-reviewer:review_resume": {
        "from_role": "builder", "to_role": "build-reviewer", "kind": "resume",
        "sources": ["plan_json", "plan_md", "build_status", "build_summary",
                    "build_baseline", "verification_receipt"],
        "required": ["plan_json", "plan_md", "build_status", "build_baseline"],
        "facts": ("team", "txn_id", "manifest_digest", "index_digest",
                  "verdict", "final_suite_label", "final_suite_binding",
                  "command_count", "disposition", "contradiction"),
        "ctx_keys": ("repos", "context_update_prefix"),
        "render": _render_build_reviewer_resume,
    },
    # route 9
    "planner->scout:handback_wake": {
        "from_role": "planner", "to_role": "scout", "kind": "handback",
        "sources": ["payload"], "required": ["payload"], "facts": (),
        "render": _render_scout_handback_wake,
    },
    # route 10
    "builder->planner:handback_wake": {
        "from_role": "builder", "to_role": "planner", "kind": "handback",
        "sources": ["payload"], "required": ["payload"], "facts": (),
        "render": _render_planner_handback_wake,
    },
    # route 11 (all artifact slots are conditional -> none strictly required)
    "controller->switch": {
        "from_role": "controller", "to_role": "role", "kind": "switch",
        "sources": ["context", "artifacts", "recovery", "pending_turn"],
        "required": [],
        "facts": ("phase", "role", "from_controller", "to_controller",
                  "reason_code", "source_code"),
        "render": _render_switch,
    },
    "lead->pending_resume": {
        "from_role": "role", "to_role": "role", "kind": "resume",
        "sources": ["context", "artifacts", "recovery", "pending_turn"],
        "required": [],
        "facts": ("phase", "role", "reason_code", "source_code"),
        "render": _render_pending_resume,
    },
    # route 13
    "context->update": {
        "from_role": "orchestrator", "to_role": "role", "kind": "context_update",
        "sources": ["context"], "required": ["context"], "facts": (),
        "render": _render_context_update,
    },
    # route 12 (peer evaluation evidence — verdict + consumed upstream)
    "eval->reviewer_verdict": {
        "from_role": "orchestrator", "to_role": "evaluator", "kind": "eval",
        # `upstream` carries the PRIOR round's frozen verdict (P6). Without it
        # in the allowed set the sealed chain could be assembled and then not
        # delivered, so "responsiveness to feedback" would have nothing to be
        # responsive to and would score not_applicable forever.
        "sources": ["verdict", "reviewed", "upstream"], "required": ["verdict"],
        "facts": (), "render": _render_eval_verdict,
    },
    "eval->upstream": {
        "from_role": "orchestrator", "to_role": "evaluator", "kind": "eval",
        "sources": ["upstream"], "required": ["upstream"], "facts": (),
        "render": _render_eval_upstream,
    },
}


def edges_between(from_role, to_role):
    """Every registered edge id from `from_role` to `to_role` (either order),
    used to tie an external role/pair registry to the topology: a role or
    reviewer pair added without its required edges resolves to an empty set."""
    return {eid for eid, e in EDGES.items()
            if {e["from_role"], e["to_role"]} == {from_role, to_role}}


def review_ctx_edge(lead_role, reviewer_role):
    """The canonical review-context edge id for a (lead, reviewer) pair, or None
    when the pair has no such edge registered. A new reviewer pair wired into an
    external registry WITHOUT adding this edge yields None (caught by tests)."""
    eid = "%s->%s:review_ctx" % (lead_role, reviewer_role)
    return eid if eid in EDGES else None


def reviewer_pairs(registry=None):
    """Reviewer->lead pairs derived from the canonical role declaration."""
    registry = registry or ROLE_REGISTRY
    return {role: spec["reviews"] for role, spec in registry.items()
            if spec.get("reviews")}


def selectable_roles(registry=None):
    registry = registry or ROLE_REGISTRY
    return [
        role for role, _spec in sorted(
            registry.items(), key=lambda item: item[1].get("order", 999))
        if _spec.get("selectable", True) and not _spec.get("non_handoff")
    ]


def validate_role_topology(registry=None, edges=None):
    """Fail closed when a role/reviewer pair is not wired into the topology.

    Every handoff-capable role must occur in a registered edge. Every declared
    reviewer pair must have its canonical review-context edge and at least one
    edge between the pair. A role may avoid those requirements only through the
    explicit ``non_handoff`` classification.
    """
    registry = registry or ROLE_REGISTRY
    edges = edges or EDGES
    edge_roles = {
        role for spec in edges.values()
        for role in (spec["from_role"], spec["to_role"])
    }
    for role, spec in registry.items():
        if spec.get("non_handoff"):
            continue
        if role not in edge_roles:
            raise ValueError("role %r has no topology edge" % role)
        reviewer = spec.get("reviewer")
        if reviewer:
            if reviewer not in registry:
                raise ValueError("role %r names unknown reviewer %r"
                                 % (role, reviewer))
            eid = "%s->%s:review_ctx" % (role, reviewer)
            if eid not in edges:
                raise ValueError("reviewer pair %s<->%s lacks %s"
                                 % (role, reviewer, eid))
            if not any({e["from_role"], e["to_role"]} == {role, reviewer}
                       for e in edges.values()):
                raise ValueError("reviewer pair %s<->%s has no topology edges"
                                 % (role, reviewer))
        lead = spec.get("reviews")
        if lead and registry.get(lead, {}).get("reviewer") != role:
            raise ValueError("reviewer %r is not paired back from %r"
                             % (role, lead))
    return True


def render_handoff(edge_id, *, artifacts=None, facts=None, ctx=None):
    """THE CHOKE POINT. Render one cross-role prompt block for `edge_id`.

    - `artifacts`: ordered list of ``{"label", "path", "kind"}`` — the
      authoritative files this edge carries. Every path MUST be absolute
      (fail-closed; a relative path raises RelativePathError). An absolute-but-
      missing file degrades to a "(missing on disk)" descriptor (content
      tolerance).
      Every artifact may carry a `source` (one of the edge's declared sources);
      an undeclared source, or a REQUIRED source with no artifact, raises
      MissingSourceError (structural fail-closed — a route cannot silently omit
      context/plan/intel/review/baseline/pending-turn/verdict paths).
    - `facts`: content-free orchestration facts. Every key must be declared for
      the edge and every value must pass that key's CLOSED schema (a fixed enum
      set, or a normalized code) — an unknown enum value raises ContentFreeError.
    - `ctx`: edge-specific composition, restricted to the edge's declared
      `ctx_keys` and validated per key (a context-update prefix must be a
      HandoffBlock; repos must be validated {abs path, bool} metadata). An
      undeclared or ill-typed ctx key raises ContextError. Never a body.

    Artifact `label`s shown in the prompt are taken from the registry's
    SLOT_LABELS (by source), NOT from any caller-supplied label, so a body
    cannot ride through the label field.

    Returns a `HandoffBlock` (a `str`) from which BOTH the prompt and the
    trace/report accounting descriptors derive. An unregistered edge raises
    UnknownEdgeError."""
    edge = EDGES.get(edge_id)
    if edge is None:
        raise UnknownEdgeError("no such cross-role edge: %r" % (edge_id,))
    facts = facts or {}
    _assert_content_free(edge_id, facts, edge["facts"])
    _assert_ctx(edge_id, ctx, set(edge.get("ctx_keys") or ()))
    artifacts = list(artifacts or [])
    declared = set(edge.get("sources") or ())
    present_sources = set()
    normalized = []
    for art in artifacts:
        path = art.get("path")
        if not path or not os.path.isabs(path):
            raise RelativePathError(
                "edge %r: required artifact path must be absolute, got %r"
                % (edge_id, path))
        # Source is MANDATORY: an untagged artifact cannot satisfy a required
        # slot and could smuggle an unaccounted file, so it fails closed.
        source = art.get("source")
        if source is None:
            raise MissingSourceError(
                "edge %r: every artifact must declare a 'source' (one of %s); "
                "an untagged artifact is rejected" % (edge_id, sorted(declared)))
        if source not in declared:
            raise MissingSourceError(
                "edge %r: artifact source %r is not declared for this edge "
                "(declared: %s)" % (edge_id, source, sorted(declared)))
        present_sources.add(source)
        # Registry-owned label: ignore any caller `label` (a smuggle vector) and
        # use the fixed per-slot label. The path/kind/source are structural.
        normalized.append({"label": SLOT_LABELS.get(source, source),
                           "path": path, "kind": art.get("kind"),
                           "source": source})
    # Fail closed on STRUCTURE: every REQUIRED source SLOT must be filled. Each
    # multi-file input is modelled as its own slot (plan_json + plan_md,
    # intel_json + intel_md, build_status + build_baseline, ...), so a route that
    # ships only one half of a plan/intel pair — or omits a status/baseline path
    # — is rejected, enforcing per-file cardinality, not mere set-presence.
    for req in edge.get("required") or ():
        if req not in present_sources:
            raise MissingSourceError(
                "edge %r: required artifact slot %r has no artifact "
                "(supplied: %s)" % (edge_id, req, sorted(present_sources)))
    entries = _descriptor_entries(normalized)
    descriptor_lines = _descriptor_lines(entries)
    prompt = edge["render"](descriptor_lines, facts, ctx or {})
    descriptors = _descriptor_records(entries)
    edge_ids = [edge_id]

    if ctx and isinstance(ctx.get("context_update_prefix"), HandoffBlock):
        prefix = ctx["context_update_prefix"]
        prefix_eids = getattr(prefix, "edge_ids", None) or ((prefix.edge_id,) if prefix.edge_id else ())
        for eid in prefix_eids:
            if eid and eid not in edge_ids:
                edge_ids.append(eid)
        seen_paths = {d["path"] for d in descriptors}
        for d in getattr(prefix, "descriptors", []) or []:
            if isinstance(d, dict) and d.get("path") and d["path"] not in seen_paths:
                descriptors.append(d)
                seen_paths.add(d["path"])

    return HandoffBlock(prompt, _token=_RENDER_TOKEN, edge_id=edge_id,
                        edge_ids=edge_ids, delivery="path",
                        embedded=_embedded_map(entries),
                        descriptors=descriptors)


# --------------------------------------------------------------------------- #
# Sealed evidence envelopes (P4/P6 — the CV-008 fix).                         #
#                                                                             #
# CV-008's root cause is an ORDERING one, not a hashing one: `render_handoff`  #
# hashes each artifact when the prompt BLOCK is built, and the evaluation      #
# block was built before the reviewer had written its verdict. A file that did #
# not exist yet therefore contributed the empty-file digest as if it were      #
# content, and a file about to be rewritten contributed the PREVIOUS round's.  #
# Either way a score was bound to evidence the evaluator never saw.            #
#                                                                             #
# Sealing after the artifact is written AND validated removes the defect       #
# structurally rather than patching a call order that could drift back.        #
# --------------------------------------------------------------------------- #


class EvidenceEnvelope:
    """An immutable statement of what evidence existed, and with what content,
    at the moment a claim about it was made.

    Sealing is not the same as reading: an artifact that is missing or that
    fails validation is recorded as `present=False` / `validated=False` and is
    given NO digest. The empty-file digest is never substituted, because that is
    exactly the substitution that made a missing file look like content.
    """

    def __init__(self, envelope_id, sealed_at, artifacts, context=None):
        self.envelope_id = envelope_id
        self.sealed_at = sealed_at
        self.artifacts = artifacts
        self.context = context or {}

    def as_dict(self):
        return {
            "envelope_id": self.envelope_id,
            "sealed_at": self.sealed_at,
            "artifacts": self.artifacts,
            "context": self.context,
        }

    @property
    def complete(self):
        """True only when every sealed artifact was present AND validated."""
        return all(a.get("present") and a.get("validated")
                   for a in self.artifacts) if self.artifacts else False

    def digests(self):
        return {a["path"]: a.get("sha256") for a in self.artifacts}


def seal_envelope(artifacts, validate=None, context=None, envelope_id=None,
                  sealed_at=None):
    """Seal the evidence for one claim, AFTER the caller has written it.

    `artifacts` is an iterable of `{path, label?, role?}` dicts. `validate` is
    an optional `validate(path, raw) -> bool` the caller supplies to say whether
    the file it just wrote is actually usable (a verdict file that parses, say)
    — an artifact that exists but is unusable is not evidence, and is sealed as
    such rather than silently counted.

    Never raises: an unreadable artifact seals as absent.
    """
    sealed = []
    for art in artifacts or []:
        if isinstance(art, str):
            art = {"path": art}
        if not isinstance(art, dict) or not art.get("path"):
            continue
        path = art["path"]
        raw = _read_raw(path)
        present = raw is not None
        validated = False
        if present:
            if validate is None:
                validated = True
            else:
                try:
                    validated = bool(validate(path, raw))
                except Exception:  # noqa: BLE001 - a validator never breaks a seal
                    validated = False
        sealed.append({
            "path": path,
            "label": art.get("label") or os.path.basename(path),
            "role": art.get("role"),
            "present": present,
            "validated": validated,
            # NO digest for an artifact that is absent or invalid. `None` says
            # "there was nothing to hash"; the empty-file digest would say
            # "there was content, and it was empty" — a different, false claim.
            "sha256": (hashlib.sha256(raw).hexdigest()
                       if present and validated else None),
            "bytes": len(raw) if present else None,
        })
    return EvidenceEnvelope(
        envelope_id=envelope_id or str(uuid.uuid4()),
        sealed_at=sealed_at or _utc_now(),
        artifacts=sealed, context=context)


def verify_envelope(envelope):
    """Re-read a sealed envelope's artifacts and compare against the seal.

    Returns `{"state": "ok"|"changed"|"unknown", "changed": [<path>, ...]}`.

    Called at aggregation time, which is what makes deferred scoring safe: an
    artifact that changed between sealing and scoring marks its score
    `unverifiable` rather than being re-hashed to whatever it says now. Re-
    hashing would make every score verifiable by construction and prove
    nothing.
    """
    if isinstance(envelope, dict):
        envelope = EvidenceEnvelope(
            envelope.get("envelope_id"), envelope.get("sealed_at"),
            envelope.get("artifacts") or [], envelope.get("context"))
    if not isinstance(envelope, EvidenceEnvelope) or not envelope.artifacts:
        return {"state": "unknown", "changed": [],
                "detail": "no sealed artifacts to verify"}
    changed = []
    for art in envelope.artifacts:
        raw = _read_raw(art.get("path"))
        now = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if now != art.get("sha256"):
            changed.append(art.get("path"))
    return {"state": "changed" if changed else "ok", "changed": changed}
