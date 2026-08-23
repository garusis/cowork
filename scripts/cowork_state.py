#!/usr/bin/env python3
"""cowork session store.

Persists a cowork session in a project-local `.cowork/session.json` so the team
+ per-role config is not re-asked on the next run in the same directory, and so
the scout's claude/codex session can be resumed if a run is killed.

Schema (version 1):

    {
      "version": 1,
      "team": ["scout", "advisor", ...],
      "config": {"scout": {"controller": "claude", "model": null,
                           "effort": null, "yolo": true, "mode": "plan"}, ...},
      # controller: claude | codex | opencode. model/effort: null = the
      # controller CLI's own default; opencode models are "provider/model".
      "context": {                 # current shared session context (versioned)
        "text": "...",
        "hash": "<sha256>",
        "revision": 3,
        "source": "--context"
      },
      "sessions": {
        "scout": {"controller": "claude", "id": "<uuid>",   # claude session_id
                  "last_context_revision_seen": 3}
        # or:    {"controller": "codex",    "id": "<thread_id>", ...}
        # or:    {"controller": "opencode", "id": "<ses_...>", ...}
      },
      # OPTIONAL session controller policy. ABSENT = unrestricted, which is how
      # every session saved before this field existed loads and runs. Removing a
      # restriction DELETES the key, so a lifted session is byte-equivalent in
      # shape to a pre-feature session.
      "controller_policy": {"allowed": ["claude", "codex"],
                            "updated": 1750000000.0,
                            "source": "cli"}
    }

A PRESENT-BUT-INVALID `controller_policy` is a HARD ERROR, never an implicit
"unrestricted": reading a damaged restriction as "no restriction" would start
exactly the provider the policy existed to forbid. `read_controller_policy`
therefore returns a TAGGED result and there is deliberately no reader that
collapses `invalid` to None; the run stops until an explicit
`--allow-controllers` replaces the policy wholesale.

The context invariant: explicit context (`--context`/prompted goal) is persisted
as the CURRENT session context, with a monotonically increasing revision. Any
role invoked afterward must receive that current context unless it has already
acknowledged that revision (`last_context_revision_seen`); a resumed CLI session
that has not seen the latest revision gets it as an explicit wake block instead
of being discarded.

Python 3.9+, stdlib only.
"""

import datetime
import fcntl
import glob
import hashlib
import json
import math
import os
import re
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_policy as policy  # noqa: E402

VERSION = 1
DIR_NAME = ".cowork"
FILE_NAME = "session.json"

# Evaluation policy (D2): how much of a run gets scored. `all_rounds` is the
# default; the others are the levers for turning measurement overhead down.
# Whatever the value, the overhead is reported separately so the choice is
# informed rather than blind.
EVALUATION_POLICIES = ("all_rounds", "final_round", "sampled", "off")
DEFAULT_EVALUATION_POLICY = "all_rounds"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def session_dir(cwd=None):
    return os.path.join(cwd or os.getcwd(), DIR_NAME)


def session_path(cwd=None):
    return os.path.join(session_dir(cwd), FILE_NAME)


def new_session_path(cwd, session_uuid):
    """Path of a per-session state file `.cowork/session.<uuid>.json`. Each
    cowork session in a directory gets its own file so many sessions coexist;
    the legacy single `session.json` (see `session_path`) is still discovered
    in place."""
    return os.path.join(session_dir(cwd), "session.%s.json" % session_uuid)


def _uuid_from_filename(path):
    """Extract the uuid encoded in a `session.<uuid>.json` name, or None for the
    legacy `session.json` (which carries no uuid in its name)."""
    base = os.path.basename(path)
    if base == FILE_NAME:
        return None
    if base.startswith("session.") and base.endswith(".json"):
        mid = base[len("session."):-len(".json")]
        return mid or None
    return None


def discover_session_files(cwd=None):
    """Return the sorted list of per-directory session files: every
    `.cowork/session.*.json` plus the legacy `.cowork/session.json` if present.
    A directory glob — no registry/index file to keep in sync."""
    d = session_dir(cwd)
    found = set(glob.glob(os.path.join(d, "session.*.json")))
    legacy = os.path.join(d, FILE_NAME)
    if os.path.exists(legacy):
        found.add(legacy)
    return sorted(found)


def derive_summary(state, max_len=72):
    """Short human label derived lazily from the stored goal: the first
    non-empty line of the context text, internal whitespace collapsed and
    truncated with an ellipsis. None when the session has no context text
    (the caller falls back to `fallback_label`)."""
    text = get_context(state)
    if not text:
        return None
    for raw in str(text).splitlines():
        line = " ".join(raw.split())
        if line:
            if len(line) > max_len:
                return line[:max_len - 1].rstrip() + "…"
            return line
    return None


def fallback_label(session_uuid, created_or_mtime=None):
    """Deterministic label for a session with no derivable summary: a short id
    plus, when a timestamp is given, a formatted local time. Used in the picker
    so an empty-goal or pre-context session is still identifiable."""
    short = (session_uuid or "????????")[:8]
    label = "session %s" % short
    if created_or_mtime:
        label += " · " + time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(created_or_mtime))
    return label


def list_sessions(cwd=None):
    """Return the directory's sessions, newest-first, as a list of dicts
    `{id, path, summary, phase, created, last_active}`.

    Each discovered file is loaded (unreadable/incompatible files are skipped,
    never raised). `id` is the persisted `session_uuid`, falling back to the
    uuid parsed from a `session.<uuid>.json` name; a legacy `session.json` with
    neither is skipped. `summary` is `derive_summary` (None when no context).
    `last_active` is the file mtime (every atomic save refreshes it); `created`
    is the persisted mint-time epoch (None for legacy files). Ordered
    newest-first by `last_active or created`, tie-broken by `created`."""
    out = []
    for path in discover_session_files(cwd):
        state = load(path)
        if state is None:
            continue
        sid = get_session_uuid(state) or _uuid_from_filename(path)
        if not sid:
            continue
        try:
            last_active = os.path.getmtime(path)
        except OSError:
            last_active = None
        created = state.get("created")
        out.append({
            "id": sid,
            "path": path,
            "summary": derive_summary(state),
            "phase": get_phase(state),
            "created": created,
            "last_active": last_active,
        })
    out.sort(
        key=lambda s: (s["last_active"] or s["created"] or 0,
                       s["created"] or 0),
        reverse=True)
    return out


def load(path):
    """Return the stored state dict, or None if absent/unreadable/incompatible."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != VERSION:
        return None
    return state


def save(path, state):
    """Write state atomically, creating the .cowork dir if needed."""
    state = dict(state)
    state["version"] = VERSION
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def get_session_uuid(state):
    return (state or {}).get("session_uuid")


def read_status(intel_path):
    """Return the scout intel `status` (needs_input/ready_for_review), or None if
    the file is missing, unreadable, or not yet written. Tolerant by design so a
    missing/partial file never forces the cowork loop to end."""
    if not intel_path:
        return None
    try:
        with open(intel_path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        return data.get("status")
    return None


def fingerprint_status(intel_path):
    """Return a fingerprint of the status file: `{exists, status, sha256, size,
    mtime_ns}`.

    `sha256`/`size` are computed over the RAW file bytes (NOT the parsed JSON),
    so any byte-level change — even a malformed-but-different write — registers
    as progress; only a genuinely missing or byte-identical file reads as a
    no-op. `status` reuses `read_status` (None on missing/unparseable).

    Tolerant by design (mirrors `read_status`): a missing or unreadable file
    yields `{exists: False, status: None, sha256: None, size: None,
    mtime_ns: None}` and never raises. stdlib only."""
    result = {"exists": False, "status": None, "sha256": None,
              "size": None, "mtime_ns": None}
    if not intel_path:
        return result
    try:
        with open(intel_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return result
    result["exists"] = True
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    result["size"] = len(raw)
    try:
        result["mtime_ns"] = os.stat(intel_path).st_mtime_ns
    except OSError:
        result["mtime_ns"] = None
    result["status"] = read_status(intel_path)
    return result


def invalidate_ready_status(intel_path, from_status="ready_for_review"):
    """Downgrade a stale `from_status` status (default `ready_for_review`) to
    `needs_input`.

    Returns True only when the file was changed. Tolerant by design: missing,
    unreadable, malformed, or non-matching files are left alone and return
    False."""
    if not intel_path:
        return False
    try:
        with open(intel_path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("status") != from_status:
        return False
    data["status"] = "needs_input"
    try:
        dirname = os.path.dirname(intel_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp = intel_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, intel_path)
    except OSError:
        return False
    return True


VALID_VERDICTS = ("approve", "revise", "needs_user")


def read_review(review_path):
    """Return the scout-reviewer verdict dict, or None if the file is missing,
    unreadable, or not yet written. Tolerant by design (mirrors read_status) so a
    missing/partial review never crashes the cowork loop.

    A file that is present but lacks a valid `verdict` is reported as a
    `{"verdict": "revise", ...}` so the caller never silently approves on a
    malformed review — the safe non-approving default."""
    if not review_path:
        return None
    try:
        with open(review_path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("verdict") not in VALID_VERDICTS:
        # Present but malformed: degrade to a safe, non-approving verdict so the
        # plan never reaches the user on an unparseable review.
        return _safe_revise(
            "Reviewer wrote an unparseable or missing verdict; treating as "
            "revise (safe default).", data.get("user_question"))
    if data.get("verdict") == "needs_user" and not str(
            data.get("user_question") or "").strip():
        # needs_user with no question can't be relayed faithfully -> safe revise.
        return _safe_revise(
            "Reviewer returned needs_user without a user_question; treating as "
            "revise (safe default).", None)
    return data


def _safe_revise(reason, user_question):
    return {
        "verdict": "revise",
        "findings": [reason],
        "user_question": user_question,
        "malformed": True,
    }


def read_handoff(path):
    """Return the hand-back payload string when the status file signals
    `handoff_back` with a non-empty `handoff` payload, else None. Tolerant by
    design (mirrors read_status): a missing, unreadable, or malformed file —
    or a `handoff_back` without a payload — yields None so the caller degrades
    to the normal needs-input gate instead of triggering a hand-back."""
    if not path:
        return None
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") != "handoff_back":
        return None
    payload = str(data.get("handoff") or "").strip()
    return payload or None


def scout_intel_md_path_for(intel_dir, session_uuid):
    """Path of the scout's human-first markdown intel (the user's review surface
    at the scout gate, sibling of the scout intel JSON). The JSON stays the
    machine source of truth + status channel; this MD is the readable rendering
    and is folded into the reviewer hash-gate composite. The per-session folder
    carries the uuid, so the filename does not; `session_uuid` is accepted for
    call-site stability but unused."""
    return os.path.join(intel_dir, "scout.intel.md")


def review_path_for(intel_dir, session_uuid):
    """Path of the scout-reviewer's verdict file for a session (sibling of the
    scout intel file). The per-session folder carries the uuid, so the filename
    does not; `session_uuid` is accepted for call-site stability but unused."""
    return os.path.join(intel_dir, "scout-review.json")


def planner_plan_json_path_for(intel_dir, session_uuid):
    """Path of the planner's JSON plan deliverable (machine source of truth and
    the planner's status channel). The per-session folder carries the uuid, so
    the filename does not; `session_uuid` is accepted for call-site stability
    but unused."""
    return os.path.join(intel_dir, "planner.plan.json")


def planner_plan_md_path_for(intel_dir, session_uuid):
    """Path of the planner's human-first markdown plan (the user's review
    surface at the plan gate). The per-session folder carries the uuid, so the
    filename does not; `session_uuid` is accepted for call-site stability but
    unused."""
    return os.path.join(intel_dir, "planner.plan.md")


def planner_review_path_for(intel_dir, session_uuid):
    """Path of the planning-advisor's verdict file for a session (sibling of
    the planner plan files). The per-session folder carries the uuid, so the
    filename does not; `session_uuid` is accepted for call-site stability but
    unused."""
    return os.path.join(intel_dir, "planner-review.json")


def build_status_path_for(intel_dir, session_uuid):
    """Path of the builder's status JSON for a session (the builder's status
    channel and verification log; sibling of the plan files). The per-session
    folder carries the uuid, so the filename does not; `session_uuid` is
    accepted for call-site stability but unused."""
    return os.path.join(intel_dir, "builder.status.json")


def build_review_path_for(intel_dir, session_uuid):
    """Path of the build-reviewer's verdict file for a session (sibling of the
    builder status file). The per-session folder carries the uuid, so the
    filename does not; `session_uuid` is accepted for call-site stability but
    unused."""
    return os.path.join(intel_dir, "builder-review.json")


def build_summary_path_for(intel_dir, session_uuid):
    """Path of the builder's human-first markdown summary (the user's review
    surface at the build gate, sibling of the builder status file). It is the
    builder's post-build report — emitted at the self-audit when the builder
    marks ready_for_review — NOT a hash-gate baseline (the builder stays out of
    the reviewer hash-gate). The per-session folder carries the uuid, so the
    filename does not; `session_uuid` is accepted for call-site stability but
    unused."""
    return os.path.join(intel_dir, "builder.summary.md")


def worktree_status_path_for(intel_dir, session_uuid):
    """Path of the worktree role's status artifact for a session (sibling of the
    other session assets). The pre-scouting worktree role writes its result here
    and cowork validates it deterministically before redirecting (D13). The
    per-session folder carries the uuid, so the filename does not; `session_uuid`
    is accepted for call-site stability but unused."""
    return os.path.join(intel_dir, "worktree.status.json")


# --------------------------------------------------------------------------- #
# Created worktree (per `--worktree`).                                          #
#                                                                              #
# When a session is launched with `--worktree`, the worktree role creates a    #
# git worktree + branch and the path is recorded here so a resume reuses the   #
# existing worktree (D6) instead of re-creating it.                            #
# --------------------------------------------------------------------------- #


def set_worktree(path, worktree_path, branch, prior=None):
    """Persist the created worktree path + branch on the session record,
    preserving the rest of the state. Returns the updated state."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    state["worktree"] = {"path": worktree_path, "branch": branch}
    save(path, state)
    return state


def get_worktree(state):
    """Return the recorded worktree `{path, branch}` dict, or None. Tolerant of
    legacy session files written before worktrees existed."""
    wt = (state or {}).get("worktree")
    if isinstance(wt, dict) and wt.get("path"):
        return {"path": wt.get("path"), "branch": wt.get("branch")}
    return None


# --------------------------------------------------------------------------- #
# Peer evaluations.                                                            #
#                                                                              #
# After each review round both sides of the active pairing privately score     #
# each other. Each evaluator writes a per-turn scratch file under the           #
# session-assets home (its ONLY eval write target); the orchestrator reads it   #
# back, stamps metadata, and appends the entries to a per-session aggregate     #
# scores.json under ~/.cowork/sessions/<uuid>/ (orchestrator-written only —     #
# evaluators are never given that path). Purely observational: a missing or     #
# malformed scratch is skipped, never an error.                                 #
# --------------------------------------------------------------------------- #


def eval_scratch_path_for(intel_dir, role, session_uuid):
    """Path of `role`'s private evaluation scratch file for a session
    (overwritten each eval turn; sibling of the other session assets). The
    per-session folder carries the uuid, so the filename does not; `role` stays
    in the name to keep the two evaluators' scratch files distinct, while
    `session_uuid` is accepted for call-site stability but unused."""
    return os.path.join(intel_dir, "eval.%s.json" % role)


def session_assets_dir(session_uuid):
    """Directory holding a session's produced assets (intel, reviews, plans,
    build status, eval scratch) — the home for every per-session artifact,
    alongside the aggregate scores.json and the trace already kept here. The
    root is overridable via COWORK_SESSIONS_ROOT so tests never write to the
    real home dir. (`session.json` is the one exception: it stays project-local
    as the per-directory anchor — see `session_path`.)"""
    root = (os.environ.get("COWORK_SESSIONS_ROOT")
            or os.path.expanduser(os.path.join("~", ".cowork", "sessions")))
    return os.path.join(root, session_uuid)


def scores_path_for(session_uuid):
    """Path of the per-session aggregate scores file. The root is overridable
    via COWORK_SESSIONS_ROOT so tests never write to the real home dir."""
    return os.path.join(session_assets_dir(session_uuid), "scores.json")


def identities_path_for(session_uuid):
    """Path of the per-session role-identity registry: which tool (claude or
    codex), live model, and provider session id each role actually ran with.
    Written by the orchestrator on every turn; read at eval-aggregation time so
    score entries can stamp the EVALUATEE's tool+model, not just the
    evaluator's."""
    return os.path.join(session_assets_dir(session_uuid), "identities.json")


def orchestrator_evaluations_path_for(session_uuid):
    """Path of the per-session ORCHESTRATOR-owned evaluations file: targeted,
    structured scores an external orchestrator/driver records against an
    individual role contribution (scout, planner, builder, a paired reviewer,
    or `orchestration` itself). Semantically SEPARATE from the peer
    `scores.json` — it is written by the driver, never by a role, and never read
    by any phase gate. The root is overridable via COWORK_SESSIONS_ROOT so tests
    never write to the real home dir (mirrors `scores_path_for`)."""
    return os.path.join(session_assets_dir(session_uuid),
                        "orchestrator-evaluations.json")


def children_path_for(session_uuid):
    """Append-only child-attempt/provider-correlation ledger."""
    return os.path.join(session_assets_dir(session_uuid), "children.jsonl")


def dispatch_links_path_for(session_uuid):
    """Append-only exactly-once AttemptLink ledger (pending_replay and gate_repair).
    Keyed by idempotency_key so duplicate observations are idempotent."""
    return os.path.join(session_assets_dir(session_uuid), "dispatch_links.jsonl")


def actions_path_for(session_uuid):
    """Append-only sanitized action-policy decisions."""
    return os.path.join(session_assets_dir(session_uuid), "actions.jsonl")


def capability_pins_path_for(session_uuid):
    return os.path.join(session_assets_dir(session_uuid),
                        "capability-pins.json")


def read_capability_allowlist(session_uuid):
    """Load and validate schema-pinned read-only capabilities, fail closed."""
    try:
        with open(capability_pins_path_for(session_uuid), "r") as fh:
            raw = json.load(fh)
        entries = raw.get("capabilities") if isinstance(raw, dict) else raw
        from cowork_action_policy import load_capability_allowlist
        return load_capability_allowlist(entries), None
    except (OSError, ValueError, TypeError) as exc:
        return {}, type(exc).__name__


def guard_dir_for(session_uuid):
    return os.path.join(session_assets_dir(session_uuid), "guard")


def guard_context_path_for(session_uuid, role):
    return os.path.join(guard_dir_for(session_uuid), "%s.context.json" % role)


def guard_settings_path_for(session_uuid, role):
    return os.path.join(guard_dir_for(session_uuid), "%s.settings.json" % role)


def guard_socket_path_for(session_uuid, role, nonce):
    """Short supervisor-owned AF_UNIX path, independent of session depth."""
    seed = "\0".join((
        os.path.realpath(session_assets_dir(session_uuid)),
        str(role), str(nonce))).encode()
    digest = hashlib.sha256(seed).hexdigest()[:32]
    # macOS sockaddr_un paths are limited to roughly 104 bytes. /tmp keeps the
    # transport bounded even when COWORK_SESSIONS_ROOT is deeply nested.
    return os.path.join("/tmp", "cowork-guard-%s.sock" % digest)


def controller_state_dir_for(session_uuid, role):
    """Stable writable controller-private state root for one role."""
    return os.path.join(session_assets_dir(session_uuid), "controller-state",
                        role)


def upsert_role_identity(path, role, identity, work_id=None):
    """Merge one role's identity dict into the registry at `path`.

    Only non-None values are written, and known values are never overwritten
    by None (a later turn that could not observe the model must not erase an
    earlier observation). A no-change merge skips the write. Tolerant: any
    OSError/ValueError yields False — identity is observational and must never
    break a turn.

    The latest-wins map is kept as-is for existing readers, and an IMMUTABLE
    OBSERVATION is additionally appended under `observations[]` (CV-002). The
    map alone cannot say what a given turn ran with: a role that switches model
    mid-session would have the later model read back onto its earlier turns. A
    turn's identity is therefore read from its own observation, keyed by
    `work_id`, and observations are never rewritten."""
    if not (path and role and isinstance(identity, dict)):
        return False
    fresh = {k: v for k, v in identity.items() if v is not None}
    if not fresh:
        return False
    try:
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        current = data.get(role) if isinstance(data.get(role), dict) else {}
        merged = dict(current)
        merged.update(fresh)
        observations = data.get("observations")
        if not isinstance(observations, list):
            observations = []
        observation = dict(fresh)
        observation["role"] = role
        observation["observed_at"] = _utc_now()
        if work_id:
            observation["work_id"] = work_id
        already = bool(work_id) and any(
            isinstance(o, dict) and o.get("work_id") == work_id
            and o.get("role") == role for o in observations)
        if merged == current and (already or not work_id):
            return True
        if not already:
            observations.append(observation)
            data["observations"] = observations
        data[role] = merged
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except (OSError, ValueError):
        return False
    return True


def read_role_identities(path):
    """The registry as a dict, or {} when missing/malformed (tolerant)."""
    if not path:
        return {}
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# The contributions an orchestrator evaluation may target, the orchestration
# phase scopes, and the five score dimensions — the SINGLE source of truth,
# re-exported by cowork.py so the CLI, the schema validation here, and the tests
# never drift apart.
ORCHESTRATOR_EVAL_ROLES = ("scout", "scout-reviewer", "planner",
                           "planning-advisor", "builder", "build-reviewer",
                           "orchestration")
ORCHESTRATOR_EVAL_PHASES = ("scouting", "planning", "building", "session")
ORCHESTRATOR_EVAL_SCORE_FIELDS = ("output_quality", "intent_alignment",
                                  "evidence_quality", "self_sufficiency",
                                  "cost_worthiness")

_SHA256_HEX_CHARS = set("0123456789abcdef")


def is_sha256_hex(value):
    """Whether `value` is a well-formed lowercase-or-uppercase 64-char SHA-256
    hex digest. A digest that is not exactly this is not a real fingerprint."""
    return (isinstance(value, str) and len(value) == 64
            and all(char in _SHA256_HEX_CHARS for char in value.lower()))


def _valid_orchestrator_score(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= 5)


def valid_orchestrator_evaluation_entry(entry):
    """Whether one stored entry conforms to the orchestrator-evaluation schema.

    Fail-closed audit history: an entry that is not an object with a supported
    role/target, all five integer 1-5 scores, the required target/session/
    timestamp fields, and well-typed optional identity/digest/evidence fields is
    REJECTED. A JSON array that parses fine but carries a string, an arbitrary
    dict, or an out-of-range score is corrupt history, not scores to average.

    The stored-fingerprint schema is enforced strictly: `artifact_digest_state`
    is REQUIRED and limited to `observed` or `unavailable`; an `observed` entry
    MUST carry a valid 64-hex `artifact_digest` and a non-negative
    `artifact_size`, while an `unavailable` entry must NOT carry a digest, size,
    or fingerprint status. Duration and cost evidence must be finite and
    non-negative — NaN/inf or a negative figure is not honest evidence."""
    if not isinstance(entry, dict):
        return False
    role = entry.get("role")
    if role not in ORCHESTRATOR_EVAL_ROLES:
        return False
    if not (isinstance(entry.get("session_uuid"), str)
            and entry.get("session_uuid")):
        return False
    if not (isinstance(entry.get("timestamp"), str) and entry.get("timestamp")):
        return False
    for field in ORCHESTRATOR_EVAL_SCORE_FIELDS:
        if not _valid_orchestrator_score(entry.get(field)):
            return False
    if role == "orchestration":
        # Orchestration is targeted by phase, never a work_id.
        if entry.get("phase") not in ORCHESTRATOR_EVAL_PHASES:
            return False
        if "work_id" in entry:
            return False
    else:
        if not (isinstance(entry.get("work_id"), str) and entry.get("work_id")):
            return False
        # A team role MAY carry a free phase annotation; it must be a string.
        if entry.get("phase") is not None \
                and not isinstance(entry.get("phase"), str):
            return False
    # Optional identity/annotation strings.
    for field in ("tool", "model", "session_id", "effort", "notes",
                  "artifact_fingerprint_status"):
        if field in entry and not isinstance(entry[field], str):
            return False
    # artifact_digest_state is REQUIRED and limited to the two closed states.
    # It is the field that declares whether a fingerprint was actually captured,
    # so it must be present and one of {observed, unavailable}; an entry that
    # merely omits it, or invents a third state, is not a valid record.
    digest_state = entry.get("artifact_digest_state")
    if digest_state not in ("observed", "unavailable"):
        return False
    if digest_state == "observed":
        # 'observed' with no evidence is a contradiction: it MUST carry a real
        # 64-hex digest and a non-negative integer size.
        if not is_sha256_hex(entry.get("artifact_digest")):
            return False
        size = entry.get("artifact_size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
    else:
        # 'unavailable' must not smuggle digest evidence: a state that says
        # "no fingerprint" cannot also carry a digest, size, or fingerprint
        # status.
        for field in ("artifact_digest", "artifact_size",
                      "artifact_fingerprint_status"):
            if field in entry:
                return False
    if "round" in entry:
        rnd = entry["round"]
        if not isinstance(rnd, int) or isinstance(rnd, bool):
            return False
    if "duration_s" in entry:
        duration = entry["duration_s"]
        if not isinstance(duration, (int, float)) \
                or isinstance(duration, bool) \
                or not math.isfinite(duration) or duration < 0:
            return False
    if "usage" in entry and not isinstance(entry["usage"], dict):
        return False
    if "cost_usd" in entry:
        # Cost must be a real, non-negative number: NaN/inf and negatives are
        # never honest evidence and must not enter the audit history.
        cost = entry["cost_usd"]
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) \
                or not math.isfinite(cost) or cost < 0:
            return False
    return True


def read_orchestrator_evaluations(path):
    """The orchestrator-evaluations file as a validated JSON ARRAY of entries.

    Distinguishes MISSING from MALFORMED, which is load-bearing: a missing file
    is a new file with no history, while an unreadable/corrupt file means
    history may exist but cannot be trusted. Silently treating either as `[]`
    would destroy or skew an audit trail without warning.

    - File genuinely absent (`FileNotFoundError`) -> `[]` (a fresh history).
    - Any OTHER OSError (permission/read error) -> raise ValueError: the file
      may exist and must NOT be reinitialized to an empty history.
    - Not valid JSON, not a JSON array, OR any entry that fails the schema
      (`valid_orchestrator_evaluation_entry`) -> raise ValueError, so the caller
      preserves the bytes and surfaces the failure rather than silently skipping
      bad entries or skewing history counts.
    """
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError(
            "unreadable orchestrator evaluations file at %s: %s" % (path, exc))
    except ValueError:
        raise ValueError(
            "malformed orchestrator evaluations file at %s" % path)
    if not isinstance(data, list):
        raise ValueError(
            "malformed orchestrator evaluations file at %s "
            "(not a JSON array)" % path)
    for entry in data:
        if not valid_orchestrator_evaluation_entry(entry):
            raise ValueError(
                "malformed orchestrator evaluations file at %s "
                "(entry fails the evaluation schema)" % path)
    return data


def append_orchestrator_evaluation(path, entry):
    """Append one targeted evaluation entry to the orchestrator-evaluations
    array at `path`, atomically.

    Mirrors `write_verification_disposition` above — the directly-analogous
    orchestrator-written append-only JSON record — using `write_json_atomic`
    with NO lock file (the project's deliberate lock-free pattern for this kind
    of orchestrator sidecar).

    - New entry fails the stored schema -> return
      `{'ok': False, 'error': 'invalid_entry'}` and write NOTHING. The new entry
      is validated up front (not only the existing history), so a malformed
      digest state, a negative/non-finite duration or cost, or any other schema
      violation can never be appended to the audit trail.
    - Missing file -> start from `[]` and write a one-element array.
    - Malformed / unreadable / wrong-schema EXISTING content -> return
      `{'ok': False, 'error': 'malformed'}` and write NOTHING, so the existing
      bytes are preserved for an operator to inspect rather than overwritten.
    - Success -> `{'ok': True}`.
    - Any write failure -> `{'ok': False, 'error': 'write_failed'}`.
    """
    if not valid_orchestrator_evaluation_entry(entry):
        return {"ok": False, "error": "invalid_entry"}
    try:
        existing = read_orchestrator_evaluations(path)
    except ValueError:
        return {"ok": False, "error": "malformed"}
    new_list = list(existing) + [entry]
    if write_json_atomic(path, new_list):
        return {"ok": True}
    return {"ok": False, "error": "write_failed"}


def resolve_work_id_identity(identities_data, role, work_id):
    """FALLBACK identity resolver from `identities.json` observations[].

    The AUTHORITATIVE source is the per-turn `identity` object on the matching
    controller.turn.start/end trace event (see cowork._resolve_identity_from_
    trace): the real identities.json observation carries role/tool/model/
    session_id/observed_at but NO work_id, so it cannot correlate to a specific
    turn. This helper stays as a defensive fallback for the rare observation
    that DOES carry a work_id (never overwriting the trace-derived identity),
    and reads the IMMUTABLE observations rather than the latest-wins map so a
    correlatable observation is still historically correct.

    Returns `{'tool', 'model', 'session_id'}` with None values stripped, or
    `{}` when no observation carries a matching work_id. Tolerant: never
    raises."""
    if not isinstance(identities_data, dict) or not role or not work_id:
        return {}
    observations = identities_data.get("observations")
    if not isinstance(observations, list):
        return {}
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        if obs.get("role") == role and obs.get("work_id") == work_id:
            resolved = {
                "tool": obs.get("tool"),
                "model": obs.get("model"),
                "session_id": obs.get("session_id"),
            }
            return {k: v for k, v in resolved.items() if v is not None}
    return {}


def evaluation_queue_path_for(session_uuid):
    """Path of the durable evaluation queue (P12). Scoring is sealed and
    enqueued the instant a verdict is valid and drained at phase end, so no
    round ever waits for it; the queue lives on disk so a session killed
    mid-phase leaves its rounds queued rather than lost."""
    return os.path.join(session_assets_dir(session_uuid),
                        "evaluation_queue.jsonl")


def next_phase_round(session_uuid, phase, seat):
    """A MONOTONIC round identity for one (phase, seat), persisted.

    `review_rounds` restarts at 0 in every fresh `_role_loop`, so after a resume
    the new round reused the pre-resume round's `(phase, round)` key. Everything
    that joins on that key — the evaluation queue, ledger findings, marginal-cost
    round grouping, the ordering check, final-round supersession — then merged
    two genuinely different rounds, and the evidence chain could report the
    current round and its prior as the same one.

    The in-loop counter still drives the conversation; THIS is the durable
    identity the joins use. Tolerant: if it cannot be persisted the caller falls
    back to the in-loop counter, which is no worse than before.
    """
    if not session_uuid:
        return None
    path = os.path.join(session_assets_dir(session_uuid), "round_epochs.json")
    key = "%s|%s" % (phase or "phase", seat or "role")
    try:
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        nxt = int(data.get(key) or 0) + 1
        data[key] = nxt
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return nxt
    except (OSError, ValueError, TypeError):
        return None


def current_phase_round(session_uuid, phase, seat, default=None):
    """The durable round identity WITHOUT minting a new one.

    `next_phase_round` increments; a caller that merely needs to label something
    with the round already in progress must not advance the counter, or two
    records from one round would carry different identities.
    """
    if not session_uuid:
        return default
    path = os.path.join(session_assets_dir(session_uuid), "round_epochs.json")
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        value = (data or {}).get("%s|%s" % (phase or "phase", seat or "role"))
        return int(value) if value else default
    except (OSError, ValueError, TypeError):
        return default


def evidence_chain_path_for(session_uuid):
    """Where the prior round's frozen evidence pointers are persisted.

    The chain used to live in an in-memory closure, so a resume lost it and the
    round after a restart sealed no prior evidence at all — silently reverting
    to the behaviour P6 exists to prevent. On disk it survives the process.
    """
    return os.path.join(session_assets_dir(session_uuid),
                        "evidence_chain.json")


def read_evidence_chain(session_uuid, seat):
    """The prior round's frozen pointers for one seat, or {}."""
    data = None
    try:
        with open(evidence_chain_path_for(session_uuid), "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    entry = data.get(seat)
    return entry if isinstance(entry, dict) else {}


def write_evidence_chain(session_uuid, seat, entry):
    """Record this round's frozen pointers as the next round's prior. Tolerant:
    a write failure loses the chain for one round rather than the run."""
    path = evidence_chain_path_for(session_uuid)
    try:
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        data[seat] = entry
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        return False
    return True


def ledger_path_for(session_uuid):
    """Path of the append-only ledger of everything cowork assigns an id to:
    findings, decisions, human amendments, escaped defects and verification
    attempts. Written only by cowork_ledger (P3)."""
    return os.path.join(session_assets_dir(session_uuid), "ledger.jsonl")


def measurement_path_for(session_uuid):
    """Path of the authoritative measurement record (D3). The text report is a
    rendering of this file, never a recomputation of the raw sources."""
    return os.path.join(session_assets_dir(session_uuid), "measurement.json")


def build_manifest_path_for(session_uuid):
    """Path of the per-file build baseline manifest (`build_baseline.json`).

    Distinct from the prose `build_baseline.txt`, which records the HEAD sha and
    dirty flag for a human. Build and review metrics are computed against THIS
    manifest, because a session that starts from a dirty tree has no commit
    describing what the build actually started from."""
    return os.path.join(session_assets_dir(session_uuid),
                        "build_baseline.json")


# Identifier validation for manifest path helpers. Rejects anything that could
# traverse directories or produce ambiguous filenames. UUIDs, short slugs, and
# dotted work IDs all pass; slashes, dots-only, and NUL bytes are refused.
_SAFE_IDENTIFIER_RE = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9_\-\.]{0,254}$')


def _assert_safe_identifier(value, label):
    """Raise ValueError for identifiers that could escape the intended directory."""
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a nonempty string" % label)
    if not _SAFE_IDENTIFIER_RE.match(value):
        raise ValueError(
            "%s %r is unsafe (must match [A-Za-z0-9][A-Za-z0-9_\\-.]{0,254})"
            % (label, value))
    # Extra guard: reject anything with os.sep or os.altsep embedded even if
    # the regex somehow did not catch it (e.g. on a platform with exotic seps).
    if os.sep in value or (os.altsep and os.altsep in value):
        raise ValueError("%s %r contains a path separator" % (label, value))


def manifest_dir_for(session_uuid):
    """Directory holding dispatch manifests for one session.

    Rejects unsafe session_uuid values (path traversal, empty, etc.).
    Root is overridable via COWORK_SESSIONS_ROOT, same as all session assets."""
    _assert_safe_identifier(session_uuid, "session_uuid")
    return os.path.join(session_assets_dir(session_uuid), "manifests")


def manifest_path_for(session_uuid, work_id):
    """Path of the per-work-item dispatch manifest JSON file.

    Rejects unsafe session_uuid or work_id values."""
    _assert_safe_identifier(session_uuid, "session_uuid")
    _assert_safe_identifier(work_id, "work_id")
    return os.path.join(manifest_dir_for(session_uuid),
                        "manifest.%s.json" % work_id)


def invalidate_manifest_for(session_uuid, work_id):
    """Delete the persisted manifest so the next compile starts fresh.

    Tolerant: any OSError (file missing, bad path) is swallowed silently.
    """
    try:
        path = manifest_path_for(session_uuid, work_id)
        if os.path.exists(path):
            os.unlink(path)
    except (OSError, ValueError):
        pass


def current_manifest_status(session_uuid, work_id):
    """Return status.phase of the current manifest, or None.

    None means the manifest is absent, unreadable, or schema-invalid.
    """
    try:
        import cowork_dispatch_manifest as _dm
        path = manifest_path_for(session_uuid, work_id)
        manifest = _dm.load_manifest(path)
        if manifest is None:
            return None
        return (manifest.get("status") or {}).get("phase")
    except Exception:
        return None


def get_evaluation_policy(state):
    """The saved evaluation policy, defaulting to `all_rounds`. Unlike the
    controller policy an unreadable value is NOT a hard error: this setting
    governs measurement, and measurement must never break a run — an
    unrecognised value falls back to the default."""
    value = (state or {}).get("evaluation_policy")
    return value if value in EVALUATION_POLICIES else DEFAULT_EVALUATION_POLICY


def save_evaluation_policy(path, value, prior=None):
    """Persist the evaluation policy. Returns the new state. Raises ValueError
    on an unknown value, so a typo at the CLI is refused rather than silently
    turning scoring off."""
    if value not in EVALUATION_POLICIES:
        raise ValueError(
            "unknown evaluation policy %r (expected one of: %s)"
            % (value, ", ".join(EVALUATION_POLICIES)))
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    state["evaluation_policy"] = value
    save(path, state)
    return state


def build_manifest(root, paths):
    """Hash each path under `root` into a per-file manifest entry.

    Returns `{"root", "generated_at", "files": [{path, sha256, bytes}, ...]}`.
    A path that cannot be read is recorded with `sha256: None` and
    `state: "unreadable"` rather than dropped — a file the build cannot see is
    a fact about the baseline, not an absence."""
    files = []
    for rel in sorted(set(paths or ())):
        full = os.path.join(root, rel) if root else rel
        entry = {"path": rel}
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            entry["bytes"] = len(raw)
            entry["state"] = "ok"
        except OSError:
            entry["sha256"] = None
            entry["bytes"] = None
            entry["state"] = "unreadable"
        files.append(entry)
    return {"root": root, "generated_at": _utc_now(), "files": files}


def write_build_manifest(path, manifest):
    """Persist a build manifest. Tolerant: a write failure yields False and
    leaves the run alone — the manifest feeds measurement, not the build."""
    if not (path and isinstance(manifest, dict)):
        return False
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        return False
    return True


def read_build_manifest(path):
    """The manifest as a dict, or None when missing/malformed."""
    if not path:
        return None
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def manifest_digest(manifest):
    """A single stable digest over a manifest's file entries, used as the
    `source_manifest` stamp on a verification attempt (P17). Order-independent:
    computed over the sorted (path, sha256) pairs, so the digest identifies the
    tree state rather than the order it happened to be hashed in."""
    if not isinstance(manifest, dict):
        return None
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    pairs = sorted(
        "%s:%s" % (f.get("path"), f.get("sha256"))
        for f in files if isinstance(f, dict))
    return hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Owned verification transactions (cowork_verification).                      #
#                                                                              #
# Every artifact below lives under the same per-session assets directory as   #
# the rest of this file's paths (`session_assets_dir`), in a `verification/`  #
# subtree keyed by transaction id. The layout exists so the parent, the       #
# spawned worker, and a later `report`/`measure` reader can all find the same #
# files by deterministic path alone — no index file to keep in sync, mirroring#
# `discover_session_files`'s directory-glob approach above.                   #
# --------------------------------------------------------------------------- #


def verification_root_for(session_uuid):
    """Root directory for all owned-verification-transaction artifacts of one
    session: requests, snapshots, locks, and results. A directory, not a file,
    so a transaction's several sidecar files (request/attempts/result) sit
    together under one deterministic parent."""
    return os.path.join(session_assets_dir(session_uuid), "verification")


def verification_transaction_dir(session_uuid, transaction_id):
    """Directory holding one transaction's request, attempt-event stream,
    active-process-group state, and terminal result. `transaction_id` is
    caller-minted (e.g. a uuid4 hex) and is the sole key — two transactions
    never share a directory even if their content later turns out equal."""
    return os.path.join(verification_root_for(session_uuid),
                        "transactions", transaction_id)


def verification_request_path_for(session_uuid, transaction_id):
    """Path of the versioned JSON request cowork_verification.run_transaction
    writes before spawning the worker: transaction id, request key, snapshot
    identity, normalized configuration/inventory, and timeout/retry policy.
    The worker is launched with this file's path as its sole positional
    argument (see cowork_verification.py's `--worker` entry point)."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "request.json")


def verification_worker_identity_path_for(session_uuid, transaction_id):
    """Path of the worker's self-reported `{source_hash, protocol_version}`,
    written before it runs any command so the parent can require equality with
    the snapshot manifest entry for the worker's own file before trusting any
    evidence that follows."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "worker_identity.json")


def verification_worker_startup_log_path_for(session_uuid, transaction_id):
    """Path of the worker process's raw stdout+stderr, captured from the
    moment it is spawned. Normally near-empty (the worker deliberately
    writes nothing but its identity report and attempt events elsewhere);
    its only real job is to hold whatever a worker that crashes before
    reporting identity (an ImportError traceback, for instance) printed on
    its way out, so a startup failure produces a captured, structured
    reason instead of the parent silently waiting out the full startup
    allowance with no diagnostic evidence at all."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "worker_startup.log")


def verification_attempt_events_path_for(session_uuid, transaction_id):
    """Path of the append-only JSON-lines stream of per-command start/terminal
    events the worker writes atomically and the parent tails/polls for bounded
    evidence retry. One line per event; never rewritten, matching the
    ledger's append-only convention."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "attempts.jsonl")


def verification_active_pgid_path_for(session_uuid, transaction_id):
    """Path of the atomically published `{pgid, label, started_at}` for the
    command currently running, so a parent that must clean up a hung/crashed
    worker knows which process group to TERM/KILL without guessing. Absent or
    stale (no live pgid) once no command is active."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "active_pgid.json")


def verification_permit_path_for(session_uuid, transaction_id):
    """Path of the parent-issued per-entry PERMIT: `{index, ledger_attempt_id,
    issued_at}` for the ONE entry the worker is currently authorized to
    start. The worker must not begin entry N until this file names entry
    N's exact (index, ledger_attempt_id); the parent writes it ONLY after
    that entry's 'running' ledger revision has already durably succeeded,
    and only issues the NEXT permit after entry N's terminal/unresolved
    ledger revision has ALSO durably succeeded — closing the run-ahead gap
    where the worker, running independently of the parent's own bookkeeping
    pace, could start (or finish) a later entry before the parent had even
    discovered an earlier one's ledger failure."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "permit.json")


def verification_result_path_for(session_uuid, transaction_id):
    """Path of the transaction's terminal, immutable `TransactionResult`
    (green/red/unverified, per-command attempts, mutation report, final-suite
    fact, evidence state). Written exactly once, atomically, at the end of
    `run_transaction`."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "result.json")


def verification_snapshot_root_for(session_uuid):
    """Root of the content-addressed snapshot object store shared by every
    transaction in a session: `objects/<sha256[:2]>/<sha256>` blobs plus one
    manifest/index pair per transaction under `snapshots/<transaction_id>/`.
    Sharing the object store across transactions is a pure size optimization
    (identical file content across two nearby transactions is stored once);
    each transaction's manifest still pins its own exact set of entries."""
    return os.path.join(verification_root_for(session_uuid), "snapshot")


def verification_snapshot_objects_dir(session_uuid):
    """Directory of content-addressed blobs (`objects/<sha256[:2]>/<sha256>`)
    copied out of the candidate tree. Sharded two-level by digest prefix so no
    single directory accumulates every object the session ever snapshots."""
    return os.path.join(verification_snapshot_root_for(session_uuid),
                        "objects")


def verification_snapshot_object_path(session_uuid, sha256):
    """Path of one content-addressed object blob for `sha256`."""
    return os.path.join(verification_snapshot_objects_dir(session_uuid),
                        sha256[:2], sha256)


def verification_snapshot_manifest_path_for(session_uuid, transaction_id):
    """Path of one transaction's captured snapshot manifest: `path ->
    {type, sha256, mode, symlink_target}` for every tracked/untracked-non-
    ignored entry, plus the raw git-index digest recorded alongside it. The
    authoritative identity a worker's isolated checkout is materialized from
    and every mutation check is compared against."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "snapshot_manifest.json")


def verification_snapshot_index_path_for(session_uuid, transaction_id):
    """Path of the RAW git-index bytes captured for one transaction (not a
    digest — the literal `.git/index` contents), so a worker's isolated
    checkout can be materialized with `GIT_INDEX_FILE` pointed at an exact
    copy without ever touching the live candidate's index."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "index.raw")


def verification_snapshot_checkout_dir(session_uuid, transaction_id):
    """Root of the materialized BOOTSTRAP checkout for one transaction: a
    real directory tree (not content-addressed) built from the snapshot
    manifest/objects, that the worker process is spawned from. NOT where
    isolated_snapshot commands run — each command gets its own fresh
    per-command checkout (`verification_command_checkout_dir`) so a command
    can never mutate the bootstrap worker code that later commands (and the
    worker's own re-exec, if any) depend on.

    Excluded from the normal argv/cwd command-input path (no command's
    cwd/argv/env is ever pointed here, and static argv validation rejects
    any LITERAL escape attempt before launch) — this is NOT an access-
    control boundary: a command's own inline logic could still discover or
    construct this path at runtime, which static argv inspection cannot
    see, so "inaccessible" would overclaim. NOT filesystem-enforced
    read-only either, since a later cleanup pass must still be able to
    remove it. Kept separate per transaction so a command in one
    transaction can never observe another's checkout."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "checkout")


def verification_command_checks_root_for(session_uuid, transaction_id):
    """Parent directory of every per-command `isolated_snapshot` checkout for
    one transaction. Not materialized itself — only used as the stable
    boundary `validate_argv_safety` resolves escape checks against, since the
    individual per-command subdirectories are created and destroyed one at a
    time as the worker runs each command, not up front."""
    return os.path.join(
        verification_transaction_dir(session_uuid, transaction_id),
        "checks")


def verification_command_checkout_dir(session_uuid, transaction_id, index):
    """Root of ONE command's fresh, writable, per-command checkout — a real
    directory tree freshly materialized from the frozen snapshot
    manifest/objects (never hard-linked, never reused from a previous
    command), given functional local Git/index semantics of its own, used
    for exactly one command, and removed immediately after that command's
    terminal event is recorded. `index` is the command's position in the
    deduplicated inventory (an int), not its label — avoids any label
    sanitization while keeping directory names short, stable, and
    collision-free within one transaction."""
    return os.path.join(
        verification_command_checks_root_for(session_uuid, transaction_id),
        "%04d" % index)


def verification_lock_path_for(session_uuid, request_key):
    """Path of the kernel-level (`fcntl.flock`) single-flight lock file for one
    normalized request key (hash of snapshot digest, index digest, normalized
    configuration, and normalized inventory-including-execution-mode). Lives
    at the session's verification root, not inside any one transaction's
    directory, because the lock's purpose is to be found by a SECOND,
    equivalent request before that request knows the first transaction's id."""
    return os.path.join(verification_root_for(session_uuid),
                        "locks", "%s.lock" % request_key)


# --------------------------------------------------------------------------- #
# Current-receipt pointer + review dispositions (ORCH-050 / CV-050 / UX-021). #
#                                                                              #
# The pointer binds a builder promotion to the OWNED transaction receipt the   #
# gate used, so the reviewer surface and the human gate render one derived     #
# overlay from owned state rather than from agent prose. The dispositions      #
# sidecar is the read-through cache of the `verification.disposition` trace    #
# events (the trace stays authoritative); both are orchestrator-written, never #
# agent-writable.                                                              #
# --------------------------------------------------------------------------- #


def current_receipt_pointer_path_for(session_uuid):
    """Path of the current-receipt pointer: the binding between the builder's
    latest verified promotion and the owned transaction receipt the gate used
    (transaction id, receipt path, manifest/index digests, verdict, final-suite
    identity, command count, review round, disposition, contradiction flag).
    One file per session — it always names the CURRENT bound candidate."""
    return os.path.join(verification_root_for(session_uuid),
                        "current_receipt.json")


def write_current_receipt_pointer(session_uuid, pointer):
    """Persist the current-receipt pointer atomically. Tolerant: a write
    failure returns False and the surfaces simply render no overlay, matching
    the legacy no-transaction path."""
    if not session_uuid or not isinstance(pointer, dict):
        return False
    return write_json_atomic(current_receipt_pointer_path_for(session_uuid),
                             pointer)


def read_current_receipt_pointer(session_uuid):
    """The current-receipt pointer as a dict, or None when absent/malformed
    (fail-closed: no pointer, no overlay)."""
    if not session_uuid:
        return None
    return read_json_tolerant(current_receipt_pointer_path_for(session_uuid))


def verification_dispositions_path_for(session_uuid):
    """Path of the review-disposition sidecar (D-0001): a small append-friendly
    JSON document holding one entry per `verification.disposition` trace event,
    keyed by transaction id on read. The trace is the single source of truth;
    this file is a reconciled read-through cache so a resumed or `--report`-only
    pass can render dispositions without replaying the whole trace."""
    return os.path.join(verification_root_for(session_uuid),
                        "dispositions.json")


def write_verification_disposition(session_uuid, entry):
    """Append one disposition entry (`{transaction_id, disposition,
    review_round, reviewed_manifest_digest}`) to the sidecar, atomically.
    `recorded_at` is stamped here when absent. Tolerant: returns False on any
    failure rather than breaking the run."""
    if not session_uuid or not isinstance(entry, dict) \
            or not entry.get("transaction_id"):
        return False
    path = verification_dispositions_path_for(session_uuid)
    data = read_json_tolerant(path)
    if not isinstance(data, dict):
        data = {}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    record = dict(entry)
    record.setdefault("recorded_at", _utc_now())
    entries.append(record)
    data["entries"] = entries
    return write_json_atomic(path, data)


def read_verification_dispositions(session_uuid):
    """The LATEST disposition entry per transaction id, as
    `{transaction_id: entry}`. Append order decides — a later entry for the
    same transaction id supersedes the earlier one. Tolerant: `{}` when the
    sidecar is missing or malformed."""
    if not session_uuid:
        return {}
    data = read_json_tolerant(verification_dispositions_path_for(session_uuid))
    entries = (data or {}).get("entries")
    out = {}
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("transaction_id"):
                out[entry["transaction_id"]] = entry
    return out


def write_json_atomic(path, data):
    """Write `data` as JSON atomically (write-to-temp-then-`os.replace`),
    creating parent directories as needed. The single shared atomic-write
    primitive for verification-transaction artifacts; mirrors `save`'s and
    `write_build_manifest`'s temp-then-rename pattern above so there is one
    write style in this module, not a second bespoke one. Returns True on
    success, False on any OSError/ValueError/TypeError (tolerant, matching
    `write_build_manifest`)."""
    if not path:
        return False
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp = path + ".tmp.%d.%d" % (os.getpid(), int(time.time() * 1e6))
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        return False
    return True


def read_json_tolerant(path):
    """Read one JSON object from `path`, or None if missing/unreadable/not a
    JSON object. Tolerant by design (mirrors `read_build_manifest`/`load`
    above): a transaction/result/lock reader must never raise on a file being
    written concurrently by another process."""
    if not path:
        return None
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_all_fd(fd):
    """Read from the CURRENT fd offset to EOF. A freshly `os.open`ed fd's
    read offset starts at 0 regardless of `O_APPEND` -- `O_APPEND` only
    affects where WRITES land (always the current end-of-file, chosen
    atomically at write time), never where reads start -- so this always
    returns the file's full byte content, and reading it first never
    displaces a subsequent `O_APPEND` write."""
    chunks = []
    while True:
        chunk = os.read(fd, 262144)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _torn_tail_length(raw):
    """Bytes to drop from the END of `raw` (this file's exact on-disk
    content) to remove a torn trailing record left by an interrupted
    append -- the repair half of B-CRASH-ATOMICITY-1. Returns 0 when `raw`
    is empty or already ends in one complete, well-formed record.

    Every record this module ever writes is produced by `append_jsonl_
    atomic` as `json.dumps(record, sort_keys=True) + "\\n"` -- `json.dumps`
    never emits a literal, unescaped newline byte inside its output (a
    newline INSIDE a JSON string round-trips as the two-character escape
    `\\n`, never a raw 0x0A byte), so every successfully written record
    contains EXACTLY ONE literal `\\n` byte, and it is always the record's
    own final byte. Two things follow, and together they are sufficient to
    detect torn-ness without re-validating the whole file on every append
    (which would turn an O(1)-per-append primitive into O(file size)):

    1. If `raw` does NOT end in `\\n`, the bytes after the last complete
       record's own trailing `\\n` (or the entire buffer, if there is no
       complete record at all yet) are an INCOMPLETE final write -- a
       `_write_all_fd` (below) that raised, or crashed, partway through
       its own line's bytes, before reaching that line's own `\\n`. This is
       the only way a JSONL file this module produces can fail to end in
       `\\n` at all.
    2. If `raw` DOES end in `\\n`, the write that produced it either
       completed (durable, well-formed, valid JSON) or did not start at
       all (prior content unchanged) -- there is no third case, because a
       write that reached exactly this record's own single trailing `\\n`
       byte necessarily wrote every byte before it too (a single `\\n` is
       always a record's LAST byte, never embedded mid-record, so reaching
       it at all means the whole record was written). This case is
       additionally hardened against non-crash corruption (a manually
       edited file, a bug in a caller) by verifying the final line is
       still valid, well-formed JSON naming an object; a final line that is
       present, newline-terminated, but NOT parseable is treated the same
       as a torn tail -- it can never be a genuinely reconstructible
       record, so the same repair applies to it.

    A CRASH DURING THE REPAIR TRUNCATION ITSELF (before the next append
    that would apply it) leaves the SAME torn tail for the append after
    that to find and repair identically -- this function is a pure,
    stateless, idempotent classification of `raw`'s bytes, not something
    that depends on how many prior crashes already tried and failed to
    repair it."""
    if not raw:
        return 0
    if not raw.endswith(b"\n"):
        last_newline = raw.rfind(b"\n")
        return len(raw) - (last_newline + 1)
    body = raw[:-1]
    prev_newline = body.rfind(b"\n")
    last_line = body[prev_newline + 1:]
    if not last_line:
        return 0
    try:
        obj = json.loads(last_line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return len(raw) - (prev_newline + 1)
    if not isinstance(obj, dict):
        return len(raw) - (prev_newline + 1)
    return 0


def _write_all_fd(fd, data):
    """Write EVERY byte of `data`, looping over `os.write`'s POSIX-legal
    short writes rather than trusting a single call to consume the whole
    buffer (the completeness half of B-CRASH-ATOMICITY-1's write side --
    see `append_jsonl_atomic`'s docstring for why a bare, unchecked
    `os.write` call is not by itself a completeness guarantee). Raises
    OSError immediately on a zero-or-negative return -- the one `os.write`
    outcome that can never mean forward progress -- so the caller's own
    except/rollback path runs instead of looping forever on a broken fd."""
    view = memoryview(data)
    total = len(view)
    written = 0
    while written < total:
        n = os.write(fd, view[written:])
        if n <= 0:
            raise OSError(
                "os.write returned %r with %d of %d bytes remaining"
                % (n, total - written, total))
        written += n


_MAX_REPAIR_OBSERVE_RETRIES = 64


def _read_from_start(fd):
    """`os.lseek` to 0, then read to EOF -- the one primitive every
    freshness recheck below is built from, so "re-observe the file" always
    means the exact same thing (a full fresh read from byte 0), never a
    read from wherever the fd's offset happens to be left."""
    os.lseek(fd, 0, os.SEEK_SET)
    return _read_all_fd(fd)


def _repair_torn_tail_now(fd):
    """Read `fd`'s CURRENT bytes and, if THAT fresh read shows a
    torn/unparseable trailing fragment, remove it via `ftruncate` -- but
    only after a SECOND fresh read, taken immediately before the
    truncate, comes back BYTE-FOR-BYTE IDENTICAL to the first. If it does
    not (a reentrant signal-handler call durably appended its own record,
    or repaired one away, in the gap between this call's two reads --
    B-CRASH-ROLLBACK-1 / M1), this call NEVER truncates against the
    now-stale first read; it re-observes the file from scratch and
    re-decides, bounded by `_MAX_REPAIR_OBSERVE_RETRIES` so a
    pathological, constantly changing file (never expected outside a
    misuse of the reentrant-only path) fails loudly instead of spinning
    forever.

    The recheck compares the FULL BYTES, not merely `len(...)`/`st_size`:
    a prior version of this function compared sizes only, which a
    reentrant record whose length happens to equal the removed torn
    fragment's length would pass despite being entirely different bytes
    (M1) -- content equality cannot be fooled by a coincidental length
    match, since it requires the two reads to have observed the exact
    same on-disk bytes with nothing added, removed, or changed between
    them.

    This is what makes the repair NON-TRUNCATING with respect to any
    record durably committed by someone else: `_torn_tail_length` only
    ever classifies a COMPLETE, valid, newline-terminated record as
    healthy (0 bytes to drop), and this function never truncates unless
    the exact bytes it is about to act on were JUST reconfirmed
    unchanged -- so a complete record another writer already durably
    committed, whenever it lands, is never a truncation target, only ever
    a genuinely incomplete/unparseable fragment (this call's own
    abandoned partial write, or a leftover from an actually unrelated
    prior crash) is."""
    for _ in range(_MAX_REPAIR_OBSERVE_RETRIES):
        raw = _read_from_start(fd)
        healthy = len(raw) - _torn_tail_length(raw)
        if healthy == len(raw):
            return healthy
        if _read_from_start(fd) != raw:
            continue
        os.ftruncate(fd, healthy)
        return healthy
    raise OSError(
        "_repair_torn_tail_now exceeded %d attempts -- the file kept "
        "changing underneath this repair, which the documented "
        "single-signal reentrant path cannot do repeatedly"
        % _MAX_REPAIR_OBSERVE_RETRIES)


def _rollback_failed_append(fd, line):
    """Best-effort undo of exactly THIS call's own failed attempt to
    append `line`, from a FRESH read taken right now, RECONFIRMED
    byte-for-byte via a second fresh read immediately before the
    `ftruncate` actually runs -- never a size or content snapshot reused
    across a gap (the exact staleness B-CRASH-ROLLBACK-1 / B1 closes: a
    prior version of this rollback decided WHAT to remove from one read,
    then acted on that decision with no reconfirmation at all, so a
    reentrant signal-handler call that durably committed its own record
    in that gap -- including one whose length happened to coincide with
    what was being removed -- was erased instead of this call's own
    bytes alone). On any mismatch between the two reads, this call NEVER
    truncates against the stale first read; it re-observes and
    re-decides from scratch, bounded by `_MAX_REPAIR_OBSERVE_RETRIES`.

    Two cases, both decided from THIS fresh read alone, and both
    reconfirmed before acting:
      - the current tail is EXACTLY this call's own complete `line`
        (the ordinary case: the write fully landed but a later step, e.g.
        `fsync`, failed) -- that whole record is removed, because a
        `False` return must mean this attempt's own record is not the
        one left visible;
      - otherwise, only a torn/unparseable trailing fragment is removed
        (`_torn_tail_length`, unrelated to whichever writer produced it)
        -- covering a genuine short write, and B-CRASH-REENTRANT-2's
        interleaved case where a reentrant writer's own complete record
        now sits ahead of this call's own now-incomplete remainder.

    A complete, valid record belonging to a DIFFERENT writer matches
    neither case -- it is not this call's own `line`, and it is not
    torn -- so it is never touched. If the bounded retries are exhausted
    (only reachable via a pathologically, continuously changing file,
    never the single-signal case this exists for), this best-effort
    cleanup simply gives up without truncating anything -- never a
    guess against stale data."""
    for _ in range(_MAX_REPAIR_OBSERVE_RETRIES):
        try:
            raw = _read_from_start(fd)
        except OSError:
            return
        if raw.endswith(line):
            target = len(raw) - len(line)
        else:
            torn = _torn_tail_length(raw)
            if not torn:
                return
            target = len(raw) - torn
        try:
            fresh = _read_from_start(fd)
        except OSError:
            return
        if fresh != raw:
            continue
        try:
            os.ftruncate(fd, target)
            os.fsync(fd)
        except OSError:
            pass
        return


def _fsync_parent_dir(path):
    """Fsync `path`'s parent directory (M3): a newly created file's own
    directory ENTRY is durable state belonging to the PARENT directory's
    inode, not the file's -- fsyncing the file's own fd never touches it.
    Raises `OSError` on failure (never swallows it) so a caller treats a
    failed directory fsync exactly like a failed data fsync -- rolled
    back and reported False -- rather than silently returning True for a
    file whose directory entry was never actually confirmed durable."""
    dirname = os.path.dirname(path) or "."
    dirfd = os.open(dirname, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def append_jsonl_atomic(path, record):
    """Append one JSON record as a line to `path`, creating it if needed --
    made TRUTHFULLY crash-reconstructible by B-CRASH-ATOMICITY-1.

    A PRIOR VERSION OF THIS DOCSTRING CLAIMED "POSIX guarantees this is
    atomic for writes at or under PIPE_BUF, so a crash mid-append yields
    either the exact prior file or the exact prior file plus one whole new
    line, never a torn/interpolated record" -- THIS WAS FALSE, on two
    independent counts, and is corrected here rather than merely deleted so
    the exact prior claim and why it was wrong stays discoverable:

    1. The `PIPE_BUF`-atomicity guarantee is a POSIX property of PIPES and
       FIFOs specifically (`write(2)`: "writes of PIPE_BUF bytes or fewer
       ... shall not be interleaved with data from other processes") -- it
       says nothing whatsoever about REGULAR files, which is what this
       function opens. For a regular file, `O_APPEND` guarantees only that
       each `write()` call's target offset is chosen atomically as the
       CURRENT end-of-file (so two writers' bytes are never interleaved
       WITHIN a single successful call) -- it makes no crash-durability or
       whole-call-completeness promise at all: `write(2)` is explicitly
       permitted to return having written FEWER bytes than requested (a
       short write) for perfectly ordinary reasons -- an interrupting
       signal, a filesystem/quota limit, a transient resource shortage --
       and the pre-fix code IGNORED `os.write`'s return value entirely,
       silently treating any non-raising short write as a fully durable
       success.
    2. Nothing in the pre-fix code ever called `fsync`. A `write()` that
       does return the full byte count only means the bytes reached the
       page cache, not stable storage -- an OS crash or power loss before
       the kernel's own background flush can lose the write ENTIRELY, or
       (since the flush itself is not required to be all-or-nothing across
       the write's byte range once corruption/an early crash is in play)
       leave a torn prefix on disk, regardless of how small the write was.

    THIS FUNCTION NOW GUARANTEES: it returns True ONLY when the complete,
    exact `record` bytes (JSON + trailing newline) are verified fully
    written (`_write_all_fd` loops over every `os.write` short-write) AND
    `fsync`ed to stable storage -- never on a partial write, and never
    before the durability call. Any failure at any point (open, the repair
    truncate below, the write loop, or the fsync) is caught, the fd is
    best-effort rolled back (`_rollback_failed_append`) to undo only THIS
    call's own attempt, and this function returns False -- it never
    reports success for a record that is not truthfully, durably,
    completely on disk.

    REPAIR-BEFORE-APPEND (the other half of B-CRASH-ATOMICITY-1, closing
    the "next writer" side of it): a genuine crash that lands strictly
    inside a PRIOR call's own write -- unrecoverable in-process by
    definition, since no code runs after a real kill/power-loss -- can
    still leave a torn tail durably on disk from before this call even
    started. Appending blindly on top of that (as the pre-fix code did)
    would concatenate this call's bytes directly onto the torn prefix with
    NO newline between them, corrupting BOTH the old torn fragment AND
    this call's own otherwise-valid new record into one jointly
    unparseable line -- silently losing a legitimately new, fully-written
    record to a WHOLLY UNRELATED PRIOR crash. So before writing anything,
    this function repairs any torn tail via `_repair_torn_tail_now`, so
    `O_APPEND`'s "always the current end-of-file" lands this call's write
    at the REPAIRED end-of-file, never past a fragment left by an earlier,
    unrelated interruption. Net effect: after ANY sequence of crashes and
    retries, the file on disk is always exactly the prior valid history,
    or the prior valid history plus one whole new record -- never a torn
    tail, never two records concatenated into one unparseable line.

    B-CRASH-ROLLBACK-1 / B-CRASH-REENTRANT-2 (this correction): the
    guarantees above were previously proven only against a crash that
    happens strictly BEFORE or strictly AFTER one whole call to this
    function -- never a REAL signal landing INSIDE this call's own
    critical section (the documented reentrant signal-handler path this
    module's SELF-DEADLOCK banner sanctions can run `append_jsonl_atomic`
    again, on a separate fd for the SAME path, while this call is paused
    partway through its own read, write, or fsync). Two distinct hazards
    follow from treating this call as if it were internally atomic, and
    both are closed here, not merely made less likely:

    1. ROLLBACK using a STALE size. The pre-correction rollback truncated
       to a `repaired_size` computed once, before the write attempt, then
       reused verbatim in the except-block `ftruncate` after the write
       (and fsync) had run -- an arbitrarily long window. If a reentrant
       signal-handler call durably committed its OWN complete record
       (its own fresh open/read/repair/write/fsync/close on a different
       fd) anywhere in that window, this call's rollback would truncate
       the file back to the PRE-window size, erasing that already-durable
       record -- a record this call never itself observed being written,
       and had no right to remove. `_repair_torn_tail_now` and
       `_rollback_failed_append` (below) never reuse a size OR a content
       snapshot computed earlier in the same call: every truncation
       decision is made from a read taken at that exact moment, then
       immediately RECONFIRMED byte-for-byte via a second fresh read
       taken right before the `ftruncate` actually executes -- a
       size-only recheck was tried and rejected, because a reentrant
       record whose length happens to coincide with what is being
       removed would pass a size check while still being entirely
       different bytes (M1) -- so neither function ever shortens the
       file beneath a complete record it did not itself just observe,
       and any mismatch between the two reads discards the decision and
       retries against the fresh state instead of acting on stale data.

    2. A signal landing mid-write letting this call report FALSE SUCCESS.
       `_write_all_fd` loops over short writes; if a signal fires BETWEEN
       two of its own `os.write` calls and the handler's reentrant append
       durably lands its own record in that exact gap, this call's
       remaining bytes (the back half of its own `line`) then land, via
       `O_APPEND`, AFTER the handler's record -- stranding this call's own
       bytes as a fragment that no longer means anything on its own, even
       though `_write_all_fd` itself never raised (it did, eventually,
       write every byte it was asked to -- just not contiguously). Never
       calling `fsync`/returning True on that basis would be the ORIGINAL
       false-success hazard reborn one level deeper. So after `fsync`,
       this function re-reads the file fresh and requires this call's own
       exact `line` bytes to appear INTACT somewhere in it (`in`, not
       `endswith` -- a reentrant record legitimately landing AFTER this
       call's own untouched, contiguous write, e.g. between this call's
       `fsync` and that check, is not corruption and must not be reported
       as failure); if they do not, this call's own write is corrupted or
       incomplete and it is treated exactly like any other failure --
       rolled back and reported False, never True.

    THIS FUNCTION MUST NEVER RAISE (M2): every step below that can fail
    lives inside a `try` matched to `except (OSError, ValueError,
    TypeError): return False`, INCLUDING `os.close` -- a prior version
    left `os.close` in a bare `finally` with no guard of its own, so an
    `OSError` surfacing from `close` (a deferred write-back error is a
    real POSIX possibility) would escape this function entirely,
    breaking the documented "never crash the worker or parent" contract
    for every one of this module's bare, unguarded call sites. `close`
    is now wrapped in its own `try/except OSError: pass` so it can never
    turn a completed, correctly-classified True/False outcome into an
    uncaught exception.

    PARENT-DIRECTORY DURABILITY FOR A NEWLY CREATED FILE (M3): `fsync`ing
    the fd makes this record's own BYTES durable, but a brand new file's
    DIRECTORY ENTRY is a separate piece of durable state, tracked by the
    parent directory's own inode, not this file's -- POSIX does not
    guarantee that creating a file (`O_CREAT`) makes its directory entry
    durable merely because the file's own data was later fsynced. Without
    also fsyncing the parent directory, a crash after this function
    returns True for a FIRST record (e.g. `mint_work_unit` creating a
    brand new `work_units/<uuid>.jsonl`) can still lose the directory
    entry entirely, so recovery finds no file at all -- a WorkUnit that
    silently never existed, despite this function's own truthful-success
    claim. So whenever this call is the one that created `path` (it did
    not exist immediately before this call's own `os.open`), this
    function fsyncs the parent directory too, as part of what "durable,
    complete success" means, BEFORE returning True -- and if that
    directory fsync itself fails, this is treated exactly like any other
    durability failure: rolled back and reported False, never a True that
    silently omits it.

    Returns True on verified, durable, complete success; False on any
    failure (tolerant: evidence write failures must degrade to 'poll found
    nothing yet', never crash the worker or parent -- unchanged from
    before this correction)."""
    if not path:
        return False
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        created = not os.path.exists(path)
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    except (OSError, ValueError, TypeError):
        return False
    try:
        try:
            _repair_torn_tail_now(fd)
            _write_all_fd(fd, line)
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            if line not in _read_all_fd(fd):
                raise OSError(
                    "append_jsonl_atomic: this call's own record did not "
                    "land intact -- a reentrant signal-handler write "
                    "interleaved with this call's own partial write")
            if created:
                _fsync_parent_dir(path)
        except (OSError, ValueError, TypeError):
            _rollback_failed_append(fd, line)
            return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def read_jsonl_tolerant(path):
    """Read every JSON-object line from `path`, oldest first. Tolerant: a
    missing file yields [], and an unparseable/non-dict line is skipped rather
    than raised — mirrors cowork_ledger.read_ledger's tolerance so a reader can
    tail a file the worker is still appending to."""
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return out
    return out


# Scores that are honest about not being numbers. They are real values, not
# missing ones: `not_applicable` means the criterion cannot apply to this round
# (round-1 responsiveness has no prior feedback to respond to), and
# `insufficient_evidence` means the evaluator could not judge from what it was
# given. Neither is ranked and neither is averaged.
NON_NUMERIC_SCORES = ("not_applicable", "insufficient_evidence")


def normalize_score(value):
    """Normalize one criterion score: an int clamped to 1-5, one of the honest
    non-numeric values, or `insufficient_evidence` when it parses as neither."""
    if isinstance(value, str) and value.strip() in NON_NUMERIC_SCORES:
        return value.strip()
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return "insufficient_evidence"


def is_numeric_score(value):
    """True only for a score that may be averaged or ranked."""
    return isinstance(value, int) and not isinstance(value, bool)


def read_eval(path):
    """Return the normalized list of evaluation dicts from a scratch file, or
    [] when the file is missing, unreadable, or malformed (mirrors
    `read_review`'s tolerance — an eval turn that wrote nothing usable is
    skipped, never an error).

    Normalization: each criterion needs a non-empty `name` and a score that is
    either int-coercible (clamped to 1-5) or one of the honest non-numeric
    values `not_applicable` / `insufficient_evidence`; `feedback` and
    `enhancement_suggestions` are stringified. Entries with no parseable
    criteria are dropped.

    A criterion whose score parses as neither is recorded as
    `insufficient_evidence` rather than DROPPED (CV-019). Dropping it silently
    shrank the denominator, so an evaluator that could not judge a criterion
    made the remaining scores look like the whole picture; the criterion now
    survives as unscoreable and is excluded from averages explicitly."""
    if not path:
        return []
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("evaluations")
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        criteria = []
        for crit in entry.get("criteria") or []:
            if not isinstance(crit, dict):
                continue
            name = str(crit.get("name") or "").strip()
            if not name:
                continue
            criteria.append({
                "name": name,
                "score": normalize_score(crit.get("score")),
                "feedback": str(crit.get("feedback") or ""),
            })
        if not criteria:
            continue
        out.append({
            "evaluatee": str(entry.get("evaluatee") or ""),
            "criteria": criteria,
            "enhancement_suggestions": str(
                entry.get("enhancement_suggestions") or ""),
        })
    return out


def get_scouting_epoch(state):
    """Return the persisted scouting-phase epoch (0 when scouting was never
    re-entered, and for legacy sessions saved before this epoch existed). The
    initial scouting pass runs at epoch 0; a planner -> scout hand-back bumps
    it, so the scout reviewer hash-gate baseline is invalidated by a re-entry."""
    try:
        return int((state or {}).get("scouting_epoch") or 0)
    except (TypeError, ValueError):
        return 0


def bump_scouting_epoch(path, prior=None):
    """Increment and persist the scouting-phase epoch. Called on every
    planning -> scouting transition (a user-confirmed planner -> scout
    hand-back), so a hand-back round trip yields a NEW epoch even when the
    re-investigated intel is byte-identical — invalidating any stale scout
    hash-gate baseline from the prior scouting pass (mirrors
    `bump_planning_epoch`)."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    state["scouting_epoch"] = get_scouting_epoch(state) + 1
    save(path, state)
    return state


def get_planning_epoch(state):
    """Return the persisted planning-phase epoch (0 when planning was never
    entered, and for legacy sessions saved before epochs existed)."""
    try:
        return int((state or {}).get("planning_epoch") or 0)
    except (TypeError, ValueError):
        return 0


def bump_planning_epoch(path, prior=None):
    """Increment and persist the planning-phase epoch. Called on every
    scouting -> planning transition (each intel approval that starts a
    planning phase), so a hand-back round trip yields a NEW epoch even when
    the re-approved intel is byte-identical."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    state["planning_epoch"] = get_planning_epoch(state) + 1
    save(path, state)
    return state


def get_building_epoch(state):
    """Return the persisted building-phase epoch (0 when building was never
    entered, and for legacy sessions saved before epochs existed)."""
    try:
        return int((state or {}).get("building_epoch") or 0)
    except (TypeError, ValueError):
        return 0


def bump_building_epoch(path, prior=None):
    """Increment and persist the building-phase epoch. Called on every
    plan-approved -> building transition, so a builder -> planner hand-back
    round trip yields a NEW epoch even when the re-approved plan is
    byte-identical."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    state["building_epoch"] = get_building_epoch(state) + 1
    save(path, state)
    return state


def has_eval_entry(scores_path, evaluator, evaluatee, context,
                   planning_epoch=None, building_epoch=None):
    """True when the aggregate already holds a matching evaluation.

    The resume-safe "did this already happen" check: the once-per-phase
    consumed-upstream eval (->scout in the planning phase, ->planner in the
    building phase) must not be re-emitted when a run is resumed or restarted
    within the same phase, and the in-memory closure flag does not survive
    that. `planning_epoch`/`building_epoch` scope the match to one phase: a
    hand-back round trip bumps the relevant epoch (even when the re-approved
    upstream artifact is byte-identical), so the upstream role is evaluated
    again for the new phase. With both epochs None the match is epoch-agnostic
    (the safe, more-deduping fallback when no epoch is wired). The two epoch
    params are mutually exclusive in practice (the planning phase passes one,
    the building phase the other). Tolerant by design: a missing or malformed
    aggregate reads as "not yet"."""
    if not scores_path:
        return False
    try:
        with open(scores_path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    evaluations = data.get("evaluations")
    if not isinstance(evaluations, list):
        return False
    for entry in evaluations:
        if (isinstance(entry, dict)
                and entry.get("evaluator") == evaluator
                and entry.get("evaluatee") == evaluatee
                and entry.get("context") == context
                and (planning_epoch is None
                     or entry.get("planning_epoch") == planning_epoch)
                and (building_epoch is None
                     or entry.get("building_epoch") == building_epoch)):
            return True
    return False


def append_score_entries(scores_path, session_uuid, entries):
    """Append stamped evaluation entries to the per-session aggregate file.

    Read-modify-write of the whole scores.json (the orchestrator is the only
    writer). A malformed existing file is reset to a fresh shape. Returns True
    on success, False otherwise — all OSErrors are swallowed because a home-dir
    failure must never crash a run."""
    if not scores_path or not entries:
        return False
    try:
        dirname = os.path.dirname(scores_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        try:
            with open(scores_path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if (not isinstance(data, dict)
                or not isinstance(data.get("evaluations"), list)):
            # schema 2: entries may carry evaluator/evaluatee tool+model,
            # eval_turn_id, usage, duration_ms, specs_in_turn,
            # reviewed_verdict traceability stamps.
            data = {"session": session_uuid, "schema": 2, "evaluations": []}
        data["evaluations"].extend(entries)
        tmp = scores_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, scores_path)
    except OSError:
        return False
    return True


def ensure_session(path, prior, new_uuid):
    """Guarantee the session has a cowork session UUID (distinct from any
    claude/codex session id) and that it is persisted. Returns the state.

    `new_uuid` is used only when none exists yet, so callers control id
    generation (real runs pass a fresh uuid4; tests can pass a fixed value)."""
    state = dict(prior or {})
    if not state.get("session_uuid"):
        state["session_uuid"] = new_uuid
        state.setdefault("created", time.time())
        state.setdefault("team", [])
        state.setdefault("config", {})
        state.setdefault("sessions", {})
        save(path, state)
    return state


def has_config(state):
    return bool(state and state.get("team") and state.get("config"))


def save_config(path, team, config, prior=None):
    """Persist team + config, preserving any existing saved sessions."""
    state = dict(prior or {})
    state["team"] = list(team)
    state["config"] = {r: dict(c) for r, c in config.items()}
    state.setdefault("sessions", {})
    save(path, state)
    return state


def get_role_session(state, role, controller):
    """Return the saved session id for a role if it matches the controller."""
    if not state:
        return None
    sess = (state.get("sessions") or {}).get(role)
    if sess and sess.get("controller") == controller and sess.get("id"):
        return sess["id"]
    return None


def save_role_session(path, role, controller, session_id, prior=None):
    """Persist (or update) the resumable session id for a role. Merges into the
    role's existing entry so bookkeeping fields (e.g.
    `last_context_revision_seen`) survive an id refresh."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    sessions = dict(state.get("sessions") or {})
    entry = dict(sessions.get(role) or {})
    entry.update({"controller": controller, "id": session_id})
    sessions[role] = entry
    state["sessions"] = sessions
    save(path, state)
    return state


def save_pending_turn(path, role, text, prior=None, source=None):
    """Persist a failed direct turn for `role` so any future resume or switch replays it.

    When `source` is provided (a pending_source/v1 dict truthfully referring to an
    existing source — trace event, provider session, or delivery fingerprint), it is
    stored as an additive `pending_source` sibling alongside `pending_turn`. Callers
    that omit `source` get the same positional-only behavior as before; no extra
    fields are added and existing test doubles remain compatible.
    """
    state = dict(prior or load(path) or {})
    pending = dict(state.get("pending_switches") or {})
    entry = dict(pending.get(role) or {})
    entry["pending_turn"] = text
    if source is not None:
        entry["pending_source"] = source
    else:
        entry.pop("pending_source", None)
    pending[role] = entry
    state["pending_switches"] = pending
    save(path, state)
    return state


def read_pending_switch(state, role):
    """Return the pending fresh-provider handoff metadata for `role`, if any."""
    entry = ((state or {}).get("pending_switches") or {}).get(role)
    return dict(entry) if isinstance(entry, dict) else None


def clear_pending_switch(path, role, prior=None):
    """Remove a role's pending switch marker, preserving the rest of state."""
    state = dict(prior or load(path) or {})
    pending = dict(state.get("pending_switches") or {})
    pending.pop(role, None)
    if pending:
        state["pending_switches"] = pending
    else:
        state.pop("pending_switches", None)
    save(path, state)
    return state


def switch_role_controller(path, role, target_controller, prior=None,
                           reason=None, source=None, created=None,
                           pending_turn=None):
    """Persist a controller switch for one role.

    The visible cowork session continues, but the provider-specific hidden
    session cannot migrate. We therefore update the saved role config, clear the
    active provider id for that role, preserve non-id bookkeeping fields on the
    role session entry, and record a small pending handoff marker that the next
    fresh launch can consume.

    Signature and observable effect are unchanged: this is `_apply_role_switch`
    (the pure in-memory step, shared with the multi-role transition below) plus
    exactly one save. It touches ONLY the role's own entries — never any
    session-level field such as `controller_policy`.
    """
    state = _apply_role_switch(
        dict(prior or load(path) or {}), role, target_controller,
        reason=reason, source=source, created=created,
        pending_turn=pending_turn)
    save(path, state)
    return state


def _apply_role_switch(state, role, target_controller, reason=None,
                       source=None, created=None, pending_turn=None):
    """The in-memory half of a controller switch: return the updated state with
    `role` moved to `target_controller`. Performs NO save, so the multi-role
    transition can apply several of these and persist once."""
    state = dict(state or {})
    state.setdefault("team", state.get("team") or [])
    config = dict(state.get("config") or {})
    role_cfg = dict(config.get(role) or {})
    from_controller = role_cfg.get("controller")
    role_cfg["controller"] = target_controller
    # Model ids and effort levels are controller-specific (e.g. opencode wants
    # provider/model, claude wants an alias) — a switched role falls back to
    # the new controller's own defaults instead of carrying a foreign id over.
    role_cfg["model"] = None
    role_cfg["effort"] = None
    config[role] = role_cfg
    state["config"] = config

    sessions = dict(state.get("sessions") or {})
    entry = dict(sessions.get(role) or {})
    entry["controller"] = target_controller
    entry.pop("id", None)
    sessions[role] = entry
    state["sessions"] = sessions

    pending = dict(state.get("pending_switches") or {})
    prev_entry = pending.get(role) or {}
    switch_entry = {
        "from_controller": from_controller,
        "to_controller": target_controller,
        "reason": reason,
        "source": source,
        "created": created if created is not None else time.time(),
    }
    pt = pending_turn if pending_turn is not None else prev_entry.get("pending_turn")
    if pt is not None:
        switch_entry["pending_turn"] = pt
    # Carry the pending_source/v1 sibling through the switch so text and source
    # travel together and can be paired at replay time.
    ps = prev_entry.get("pending_source")
    if ps is not None:
        switch_entry["pending_source"] = ps
    pending[role] = switch_entry
    state["pending_switches"] = pending
    return state


# --------------------------------------------------------------------------- #
# Session controller policy.                                                   #
#                                                                              #
# The allowed-controller set for this session. ABSENT = unrestricted (every     #
# pre-feature session); PRESENT-BUT-INVALID = a hard error, never an implicit   #
# "unrestricted". See the module docstring and cowork_policy.                   #
# --------------------------------------------------------------------------- #

POLICY_KEY = "controller_policy"


class InvalidControllerPolicy(ValueError):
    """A saved `controller_policy` that cannot be read. Carries the raw value so
    the caller can report what it found without guessing what it meant."""

    def __init__(self, raw, reason=None):
        self.raw = raw
        self.reason = reason or "controller_policy is not a readable allowed set"
        super().__init__(self.reason)


def read_controller_policy(state):
    """Return a TAGGED read of the saved policy:

        ("unrestricted", None)              — the key is absent
        ("allowed", ("claude", "codex"))    — a valid restricted set
        ("invalid", <raw value>)            — present but unreadable

    A policy is INVALID when the key is present and any of: the value is not a
    dict; `allowed` is missing, is not a list, or is empty; any element is not a
    known controller name.

    There is deliberately NO reader that collapses `invalid` to `unrestricted` —
    that downgrade is exactly the fail-open behaviour this feature exists to
    prevent."""
    if not isinstance(state, dict) or POLICY_KEY not in state:
        return ("unrestricted", None)
    raw = state.get(POLICY_KEY)
    if not isinstance(raw, dict):
        return ("invalid", raw)
    allowed = raw.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        return ("invalid", raw)
    for item in allowed:
        if not isinstance(item, str) or item not in policy.CONTROLLERS:
            return ("invalid", raw)
    return ("allowed", policy.normalize(allowed))


def get_allowed_controllers(state):
    """The allowed tuple, or None when unrestricted. Raises
    InvalidControllerPolicy on an unreadable policy — so a caller that ignores
    the tagged form still cannot fail open."""
    kind, value = read_controller_policy(state)
    if kind == "invalid":
        raise InvalidControllerPolicy(value)
    return value if kind == "allowed" else None


def apply_controller_transition(path, mappings, allowed=None, set_policy=False,
                                prior=None, source=None, reason=None,
                                created=None, pending_turns=None):
    """Apply an allowed-set change and any number of role remappings as ONE
    state transition: every mutation happens in memory and is persisted by
    EXACTLY ONE `save`.

    `set_policy` DEFAULTS TO FALSE, so the failure mode of forgetting it is
    "preserve the saved policy", never "delete it":

      - set_policy=False -> the `controller_policy` key is not read, written or
        removed; its serialized value is byte-identical across the transition.
      - set_policy=True, allowed=None -> the key is REMOVED (the restriction is
        lifted, leaving the file byte-equivalent in shape to a pre-feature one).
      - set_policy=True, allowed=<iterable> -> the key is written with the
        normalized set. This form is also the repair path for an INVALID saved
        policy: it replaces the value wholesale without ever reading it.

    Raises ValueError BEFORE any mutation when a mapping names a role that is
    not in the saved config. Returns the new state."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})

    pairs = [(role, target) for role, target in (mappings or [])]
    saved_config = state.get("config") or {}
    for role, _target in pairs:
        if role not in saved_config:
            raise ValueError(
                "cannot switch %r: role is not in the saved session config."
                % role)
    if set_policy and allowed is not None:
        # Normalize (and reject an empty/unknown set) before touching anything.
        allowed = policy.normalize(allowed)

    stamp = created if created is not None else time.time()
    for role, target in pairs:
        state = _apply_role_switch(
            state, role, target, reason=reason, source=source, created=stamp,
            pending_turn=(pending_turns or {}).get(role))
    if set_policy:
        if allowed is None:
            state.pop(POLICY_KEY, None)
        else:
            state[POLICY_KEY] = {
                "allowed": list(allowed),
                "updated": stamp,
                "source": source or "cli",
            }
    save(path, state)
    return state


# --------------------------------------------------------------------------- #
# Phase tracking.                                                               #
#                                                                              #
# The cowork flow is a loop of phases (scouting -> planning, with a            #
# user-confirmed hand-back planning -> scouting). The current phase is         #
# persisted so a killed run resumes into the last active phase. Plan approval  #
# ends the CLI with the phase left at `planning`; a rerun resumes the planner  #
# conversation the same way a rerun resumes the scout today.                   #
# --------------------------------------------------------------------------- #

PHASES = ("scouting", "planning", "building")


def get_phase(state):
    """Return the persisted phase. Absent or unknown values default to
    `scouting` for back-compat with session files written before phases."""
    phase = (state or {}).get("phase")
    return phase if phase in PHASES else "scouting"


def save_phase(path, phase, prior=None):
    """Persist the current phase, preserving the rest of the session state."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    state["phase"] = phase
    save(path, state)
    return state


# --------------------------------------------------------------------------- #
# Shared session context (versioned).                                          #
#                                                                              #
# Explicit context is a session-wide event, not a one-off prompt to the        #
# user-facing role: it is persisted with a revision, and every role tracks the #
# last revision it acknowledged so a resumed CLI session can be woken with the #
# current context instead of silently operating on stale assumptions.         #
# --------------------------------------------------------------------------- #


def save_context(path, text, prior=None, source="--context"):
    """Persist `text` as the CURRENT session context. Bumps the revision only
    when the text actually changed; re-providing identical context is a no-op."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    if get_context(state) == text:
        return state
    state["context"] = {
        "text": text,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "revision": get_context_revision(state) + 1,
        "source": source,
    }
    save(path, state)
    return state


def get_context(state):
    """Return the current session context text, or None. Tolerates the legacy
    plain-string form."""
    ctx = (state or {}).get("context")
    if isinstance(ctx, dict):
        return ctx.get("text")
    return ctx


def get_context_revision(state):
    """Return the current context revision (0 when no context exists). A legacy
    plain-string context counts as revision 1."""
    ctx = (state or {}).get("context")
    if isinstance(ctx, dict):
        try:
            return int(ctx.get("revision") or 0)
        except (TypeError, ValueError):
            return 0
    return 1 if ctx else 0


def get_seen_revision(state, role):
    """Return the last context revision this role acknowledged (0 if never)."""
    sess = ((state or {}).get("sessions") or {}).get(role) or {}
    try:
        return int(sess.get("last_context_revision_seen") or 0)
    except (TypeError, ValueError):
        return 0


def mark_context_seen(path, role, revision, prior=None):
    """Record that `role` has received (acknowledged) context `revision`."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    sessions = dict(state.get("sessions") or {})
    entry = dict(sessions.get(role) or {})
    entry["last_context_revision_seen"] = revision
    sessions[role] = entry
    state["sessions"] = sessions
    save(path, state)
    return state


def role_context_gap(state, role):
    """Return the current context text when `role` has not yet acknowledged the
    current revision, else None."""
    if get_context_revision(state) > get_seen_revision(state, role):
        return get_context(state)
    return None


# --------------------------------------------------------------------------- #
# Reviewer hash-gate baseline.                                                 #
#                                                                              #
# When a user-facing lead (scout / planner) re-marks `ready_for_review` but    #
# the artifact set the paired reviewer sees is byte-identical to what that     #
# reviewer LAST APPROVED — in the same phase epoch and the same acknowledged   #
# context revision — the reviewer turn is skipped and the prior approval is    #
# reused (never a silent bypass: a visible marker is emitted). The baseline    #
# that authorizes a skip is persisted in the active session state file, keyed  #
# under sessions[reviewer_role]['last_approved_baseline'], so a skip survives  #
# a cowork resume. Only an explicit reviewer `approve` seeds it.               #
# --------------------------------------------------------------------------- #

# Stable sentinel mixed into the composite for a MISSING member file, so a set
# with one file absent never collides with a set where both are present-but-empty.
_MISSING_MEMBER = b"\x00cowork-missing-artifact\x00"


def composite_artifact_hash(paths):
    """Return a sha256 hex digest over the member files' RAW bytes, concatenated
    in the given fixed order (e.g. [intel.json, intel.md] or [plan.json,
    plan.md]).

    Reuses the `fingerprint_status` raw-byte approach (NOT parsed JSON), so any
    byte change to any member — even a malformed-but-different write — changes
    the composite. A missing member contributes a stable sentinel (so "one file
    missing" never hashes the same as "both present"); a per-member length
    prefix keeps the concatenation unambiguous. stdlib only."""
    h = hashlib.sha256()
    for path in paths or []:
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            raw = _MISSING_MEMBER
        h.update(b"%d:" % len(raw))
        h.update(raw)
    return h.hexdigest()


def record_review_baseline(path, reviewer_role, epoch, context_revision,
                           composite_hash, prior=None):
    """Persist the last-approved hash-gate baseline for `reviewer_role` under
    sessions[reviewer_role]['last_approved_baseline'] = {epoch, context_revision,
    hash}, and return the updated state.

    Takes `prior` WITHOUT reloading from disk — exactly like `mark_context_seen`
    — so the caller MUST thread its in-memory state (e.g. run_flow's
    holder['state']) as `prior` and assign the returned state back; a baseline
    written only to disk would be clobbered by the next lead-ack / phase-save
    that threads the older in-memory state."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    sessions = dict(state.get("sessions") or {})
    entry = dict(sessions.get(reviewer_role) or {})
    entry["last_approved_baseline"] = {
        "epoch": epoch,
        "context_revision": context_revision,
        "hash": composite_hash,
    }
    sessions[reviewer_role] = entry
    state["sessions"] = sessions
    save(path, state)
    return state


def get_review_baseline(state, reviewer_role):
    """Return `reviewer_role`'s persisted last-approved baseline dict
    {epoch, context_revision, hash}, or None when none is stored. Tolerant by
    design: a missing/legacy/malformed entry reads as None (no skip)."""
    sess = ((state or {}).get("sessions") or {}).get(reviewer_role) or {}
    baseline = sess.get("last_approved_baseline")
    if not isinstance(baseline, dict):
        return None
    if "hash" not in baseline:
        return None
    return baseline


def review_skip_eligible(state, reviewer_role, current_epoch,
                         current_context_revision, current_composite_hash):
    """Whether the paired reviewer turn may be SKIPPED, reusing its last
    approval.

    True only when ALL hold:
      - a baseline exists for `reviewer_role`;
      - baseline.hash == current_composite_hash (the artifact set is
        byte-identical to what the reviewer last approved);
      - baseline.epoch == current_epoch (no phase re-entry since — a hand-back
        bumps the epoch and clears the skip);
      - baseline.context_revision == the reviewer's acknowledged revision
        (`get_seen_revision`) — the approval authority is what the reviewer
        actually acked, not merely what is current;
      - that acknowledged revision == current_context_revision (no newer,
        unacknowledged context — a skip must never implicitly absorb new
        context).

    Any mismatch (or any missing baseline) returns False -> a full review runs.
    Tolerant by design."""
    baseline = get_review_baseline(state, reviewer_role)
    if not baseline:
        return False
    acked = get_seen_revision(state, reviewer_role)
    return (baseline.get("hash") == current_composite_hash
            and baseline.get("epoch") == current_epoch
            and baseline.get("context_revision") == acked
            and acked == current_context_revision)


# =========================================================================== #
# M2 Package B: crash-safe WorkUnit / dependency-graph / PhaseState history,  #
# and atomic controller-policy/config transitions.                            #
#                                                                              #
# Every artifact below lives under the same per-session assets directory as   #
# the rest of this file (`session_assets_dir`), reusing the two existing      #
# atomic-write primitives (`write_json_atomic`, `append_jsonl_atomic`) and    #
# the repo's existing `fcntl.flock` lock+read-modify-append pattern           #
# (mirrors `cowork_ledger._with_ledger_lock`). None of this touches the       #
# legacy `.cowork/session*.json` schema or `load`/`save` above: a version-1   #
# session anchor written before M2 existed carries none of these fields and   #
# is completely unaffected by their presence or absence.                     #
#                                                                              #
# SIGNAL-SAFETY LIMITATION (explicit, not overclaimed): nothing in this       #
# module is async-signal-safe. Every append here allocates, formats JSON,     #
# and opens/flocks a lock file before the single append `os.write` — none of  #
# that is safe to run from inside a raw C-level signal handler. Python only   #
# ever runs `signal.signal`-registered handlers from safe bytecode-boundary   #
# re-entry (never truly asynchronously with respect to the interpreter), so a #
# handler installed via lower-level means (`sigaction` from C, a handler that #
# must remain reentrant against its own signal) MUST NOT call in here — and   #
# any handler that does can still be interrupted between the lock acquire and #
# release by a SECOND, different signal delivered before the first handler    #
# returns, which is a general Python limitation this module cannot paper      #
# over.                                                                       #
#                                                                              #
# SELF-DEADLOCK, NOT MERE ASYNC-SIGNAL-SAFETY: the LOCKED append entry points #
# below (`mint_work_unit`, `append_work_unit_transition`,                     #
# `append_graph_revision`, `append_phase_state_entry`) each open a NEW fd on  #
# `<path>.lock` and take `fcntl.flock(LOCK_EX)`. flock locks attach to the    #
# OPEN FILE DESCRIPTION, not the process: a second `open()` + `flock()` in    #
# THIS SAME PROCESS (even the same thread, via signal re-entry) for the same  #
# path is a distinct lock owner and blocks — it does NOT correctly recognize  #
# "I already hold this." A `signal.signal`-registered handler that calls one  #
# of the locked entry points while the interrupted frame already holds that   #
# SAME path's lock therefore self-deadlocks forever, not merely waits: the    #
# outer frame can never resume to release the lock, because control is stuck  #
# inside the handler's own `flock()` call. This is exactly the hazard         #
# `cowork_ledger._append_locked` documents and exists to avoid for its own    #
# lock. `append_phase_state_entry_unlocked` (below) is the analogous safe,    #
# explicit, reentrant entry point here: it performs the same validation and   #
# write but never calls `flock`, so Package E's external-kill signal handler  #
# — which may run while this process already holds the PhaseState lock for   #
# the very same (session_id, work_id) — MUST call it, never the locked        #
# `append_phase_state_entry`, to record a terminal PhaseState.                #
# `append_work_unit_transition_unlocked` (below, narrowly scoped) is the      #
# identical reentrant twin for the WorkUnit lock, for the SAME handler        #
# mirroring that terminal PhaseState onto the WorkUnit's own                  #
# `lifecycle_state` (MJ-4) — never the locked `append_work_unit_transition`   #
# for that call.                                                              #
#                                                                              #
# TERMINAL-DOMINANT RECONSTRUCTION (B-14-R1 correction, replaces the earlier  #
# thread-count precondition): there is NO precondition on this process's      #
# thread count anywhere below, for either the locked or the unlocked          #
# PhaseState entry point — a real host that keeps long-lived threads alive    #
# for the entire span an external kill can land in (a guard thread, a UI      #
# spinner, a verification watchdog) can always durably record every          #
# PhaseState, including the terminal one, with no OSError refusal. A prior    #
# version of this module tried to close the handler/interrupted-append final  #
# window by proving `threading.active_count() == 1` and blocking signals via  #
# `signal.pthread_sigmask`; that precondition is sufficient but essentially   #
# never true in the intended host, so it converted a rare unsound ordering    #
# into total, deterministic loss of terminal currency — exactly what this     #
# module must never do. The window is closed differently now, without any    #
# signal masking or thread-count check: (1) `_phase_state_builder` (below)    #
# refuses to build ANY further record once a terminal state exists ANYWHERE   #
# in the history it is handed, not merely as the last entry, so the           #
# always-active stale-build retry in `_jsonl_append_unlocked` already catches #
# a handler-written terminal record for every interruption up to and          #
# including the freshness re-read; and (2) `current_phase_state` (below)      #
# treats the FIRST terminal record found in history as durable, dominant      #
# truth regardless of its position, so even the one residual sliver — a       #
# signal landing strictly between that re-read and the actual                 #
# `append_jsonl_atomic` call, letting a stale record physically land AFTER a  #
# handler-written terminal one (including a SECOND, later-written terminal    #
# record from a genuine terminal-vs-terminal race, e.g. a normal `completed`  #
# attempt interrupted by an external-kill `aborted` — B-14-R2-A) — can never  #
# be reconstructed as "current".                                             #
#                                                                              #
# TRUTHFUL TO RAW-HISTORY READERS TOO (B-14-R2-B correction: the prior        #
# version of this reconstruction ONLY corrected what `current_phase_state`    #
# returned, leaving `read_phase_state_history`/`read_m2_state` exposing the   #
# stray post-terminal record with a colliding `transition_index` and no way   #
# to tell it apart from a real transition — truthful for one accessor,        #
# ambiguous for every other reader of the same data): `read_phase_state_       #
# history` itself now runs every non-empty result through                    #
# `_reconstruct_phase_state_history`, which overwrites `transition_index` to  #
# always equal true file position (never a stale/colliding embedded value)   #
# and adds a `superseded` field naming any record positioned after the FIRST  #
# terminal one as exactly that — a durable, truthfully-labeled race artifact  #
# — for every consumer, not merely `current_phase_state` (which is now just  #
# "the last record with `superseded=False`" in this same reconciled list).   #
# `read_m2_state`'s `phase_state_history`/`phase_state` fields inherit this   #
# automatically, since both delegate to these same two functions.            #
#                                                                              #
# LAZY DEPENDENCY (not module-level): `cowork_workunit`/`cowork_control_plane`#
# (Package A) are imported LOCALLY, inside the functions below that actually  #
# need them — never at this module's top level. `cowork_state.py` is         #
# sometimes captured/deployed as an isolated snapshot without its M2         #
# siblings; a module-level `import cowork_workunit` (which itself imports    #
# `cowork_control_plane`) would make importing THIS FILE — and therefore     #
# every pre-M2 export (`load`, `save`, `get_phase`, ...) — fail whenever      #
# those siblings are absent, even for a caller that never touches M2 at all. #
# With the import deferred, `import cowork_state` and every non-M2 export    #
# keep working exactly as before M2 existed; only actually CALLING one of    #
# the WorkUnit/graph/PhaseState functions below raises a plain `ImportError` #
# naming the missing module, and only then.                                  #
# =========================================================================== #


def _import_workunit():
    """Lazily import `cowork_workunit` (Package A). See the module-level
    LAZY DEPENDENCY note above this function for why this is a local, not
    top-level, import."""
    import cowork_workunit
    return cowork_workunit


def _import_control_plane():
    """Lazily import `cowork_control_plane` (Package A). See the
    module-level LAZY DEPENDENCY note above `_import_workunit` for why this
    is a local, not top-level, import."""
    import cowork_control_plane
    return cowork_control_plane


def _lower_safe_identifier(value, label):
    """Lowercase + validate one path-segment identifier. UUID-shaped fields
    (work_id, session_id) are case-insensitive per `cowork_workunit`'s own
    normalization, so every path built from one must use the same lowercase
    form regardless of the caller's original casing, or two callers naming
    the same identity in different casing would silently address two
    different files."""
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a nonempty string" % label)
    lowered = value.lower()
    _assert_safe_identifier(lowered, label)
    return lowered


_MAX_UNLOCKED_APPEND_ATTEMPTS = 64


def _jsonl_append_unlocked(path, build_record):
    """The raw read-build-append sequence — NO locking of its own. This is
    the REENTRANT entry point: a caller that may run while this same process
    already holds `<path>.lock` (e.g. a `signal.signal`-registered handler
    interrupting an in-flight locked append for the SAME path — see the
    module-level SELF-DEADLOCK banner above this section) must call this
    directly instead of `_locked_jsonl_append`, exactly like
    `cowork_ledger._append_locked` exists for its own already-locked
    callers.

    `build_record(existing_records)` must return the record to append (it
    sees the exact prior history, so it can veto with a raised exception
    before anything is written — a raised exception propagates immediately,
    is NEVER retried, and leaves the file completely untouched).

    STALE-BUILD DETECTION (closes the interleaving hazard the module-level
    SELF-DEADLOCK banner's own sanctioned scenario opens; the ONLY
    mechanism this function uses to close it — no signal masking, no
    thread-count precondition, unconditionally active for every caller): a
    signal handler calling this function can run to completion BETWEEN the
    moment this call reads `existing` and the moment it durably writes its
    own record — Python only ever re-enters a `signal.signal` handler at a
    bytecode boundary of the interrupted frame, so the file this
    interrupted frame is about to append to may already carry the
    handler's own new record(s) by the time it resumes. Blindly appending a
    record `build_record` derived from the now-stale `existing` would (a)
    durably land AFTER a handler-written TERMINAL record, and (b) reuse a
    `transition_index` the handler's record already claimed. This retry
    prevents both FOR EVERY INTERRUPTION IT CAN SEE: immediately before
    writing, this function re-reads the path and compares it to the
    `existing` snapshot `build_record` was actually called with; on any
    difference, it discards the just-built record and calls `build_record`
    again against the FRESH read — so a caller like `_phase_state_builder`
    that refuses to build anything once ANY record in the history it is
    handed is terminal (see its docstring; it scans the WHOLE history, not
    merely the last entry) reliably sees the fresh terminal record on this
    retry and raises instead of building a colliding one, and a caller
    whose build is still legal against the fresh state gets a correctly
    renumbered, non-colliding `transition_index` AT WRITE TIME. This is a
    write-time prevention, not the whole story: it cannot see a handler
    that lands strictly AFTER this retry's own freshness re-read (see the
    RESIDUAL FINAL WINDOW paragraph immediately below), which is closed
    differently — at READ TIME, not here. Bounded by
    `_MAX_UNLOCKED_APPEND_ATTEMPTS` so a pathologically fast concurrent
    writer (a genuine misuse of this reentrant-only entry point, never the
    single-signal case it exists for) fails loudly rather than spinning
    forever.

    RESIDUAL FINAL WINDOW, MADE HARMLESS AT READ TIME RATHER THAN CLOSED AT
    WRITE TIME: the freshness re-read above only detects a handler that
    already landed BEFORE it runs. A signal delivered strictly AFTER that
    re-read returns matching data but BEFORE the following
    `append_jsonl_atomic` call could still let this frame durably append its
    now-stale `record` — terminal or not, including a SECOND, later-written
    terminal record in a genuine terminal-vs-terminal race (e.g. a
    `completed` write racing an external-kill `aborted`; see B-14-R2-A in
    the module-level TERMINAL-DOMINANT RECONSTRUCTION banner above this
    section) — right after a handler-written terminal one, WITH A REUSED
    build-time `transition_index`. This one signal, not two, is not blocked
    (a prior version of this module attempted that via a thread-local
    `signal.pthread_sigmask` block gated on `threading.active_count() == 1`,
    which is essentially never true in the intended long-lived-thread host
    and so converted a rare unsound ordering into a deterministic refusal
    of every PhaseState write; see the TERMINAL-DOMINANT RECONSTRUCTION
    banner for why that trade was wrong and what replaces it). Instead this
    write-time collision is corrected AT READ TIME, uniformly for every
    consumer: `read_phase_state_history` runs the raw file through
    `_reconstruct_phase_state_history`, which overwrites `transition_index`
    to the record's true file position (never the possibly-duplicate
    embedded value) and marks every record positioned after the FIRST
    durably-written terminal one `superseded=True` — including a stray
    non-terminal record from this sliver, AND including a second,
    later-written terminal record from a genuine terminal-vs-terminal race.
    `current_phase_state` — "the last record with `superseded=False`" —
    then always reports that first terminal record as durable, dominant
    current truth regardless of what physically lands after it: a stray
    post-terminal record from this exact sliver is durably RETAINED and
    truthfully labeled, never discarded, and the first terminal record's
    CURRENCY is never lost or overtaken, even though its position in the
    file is not what prevents the collision. `mint_work_unit`/
    `append_work_unit_transition`/`append_graph_revision` have no
    terminal-record or `superseded` concept at all, so this residual sliver
    never applies to them in the first place.

    The actual write is `append_jsonl_atomic` — see that function's own
    docstring for the full B-CRASH-ATOMICITY-1 correction (a prior version
    of THIS docstring repeated the same false PIPE_BUF-atomicity claim that
    function's docstring documents and corrects). In one sentence:
    `append_jsonl_atomic` verifies every byte is written and `fsync`ed
    before ever reporting success, repairs a torn tail left by an earlier,
    unrelated crash before appending, and rolls back its own partial write
    on any failure — so a crash mid-append (this call's own, or a prior
    one this call's repair step subsumes) always yields either the exact
    prior valid file or the exact prior valid file plus one whole new
    line, never a torn or interpolated record. That per-call guarantee is
    what every ordering claim in this docstring builds on: it says which
    RECORD each call durably contributes, not merely that some possibly-
    incomplete bytes landed somewhere in the file.

    Calling this concurrently for the SAME path from two truly independent
    threads/processes (not the single-threaded signal-reentry case above) is
    NOT serialized by locking — that safety comes only from
    `_locked_jsonl_append`'s `flock`; the stale-build retry above merely
    keeps such a race from corrupting ordering, it does not replace the
    lock's mutual exclusion for a genuinely concurrent writer.
    """
    existing = read_jsonl_tolerant(path)
    for _ in range(_MAX_UNLOCKED_APPEND_ATTEMPTS):
        record = build_record(existing)
        current = read_jsonl_tolerant(path)
        if current != existing:
            existing = current
            continue
        if not append_jsonl_atomic(path, record):
            raise OSError("append failed for %s" % path)
        return record
    raise OSError(
        "_jsonl_append_unlocked exceeded %d attempts for %s -- the file "
        "kept changing underneath this unlocked appender; a genuinely "
        "concurrent writer must use the locked entry point instead"
        % (_MAX_UNLOCKED_APPEND_ATTEMPTS, path))


def _locked_jsonl_append(path, build_record):
    """Append exactly one record to an append-only jsonl file, serialized
    against every other appender to the SAME path via a per-path
    `fcntl.flock` lock file (mirrors `cowork_ledger._with_ledger_lock`).

    Acquires the lock, then delegates the actual read-build-write sequence
    to `_jsonl_append_unlocked` — see that function's docstring for what
    `build_record` may do and what a raised exception guarantees.

    MUST NOT be called reentrantly (from a signal handler or otherwise)
    while this same process already holds `<path>.lock` via a different fd
    — flock locks attach to the open file description, not the process, so
    a second `open()` + `flock()` here blocks forever, not merely waits (see
    the module-level SELF-DEADLOCK banner above this section). A reentrant
    caller must use `_jsonl_append_unlocked` directly instead.
    """
    lock_path = path + ".lock"
    dirname = os.path.dirname(lock_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return _jsonl_append_unlocked(path, build_record)
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_fh.close()


# --------------------------------------------------------------------------- #
# WorkUnit store: mint-before-preflight persistence + append-only lifecycle   #
# history per work_id + current-state accessor.                              #
# --------------------------------------------------------------------------- #


def work_unit_history_path_for(session_id, work_id):
    """Path of one work_id's append-only lifecycle history within its
    session. Rejects unsafe session_id/work_id values."""
    session_id = _lower_safe_identifier(session_id, "session_id")
    work_id = _lower_safe_identifier(work_id, "work_id")
    return os.path.join(session_assets_dir(session_id), "work_units",
                        "%s.jsonl" % work_id)


def mint_work_unit(record):
    """Durably persist a NEWLY MINTED WorkUnit's first lifecycle record — the
    mint-before-preflight boundary (P1 of the WorkUnit lifecycle): this MUST
    be called, and durably return, before preflight for `record['work_id']`
    begins, so a crash between mint and preflight recovers to a real
    `pending` WorkUnit on disk rather than one that silently never existed.

    `record` is validated via `cowork_workunit.validate_work_unit` (Package
    A) BEFORE anything is written; an invalid record raises ValueError and
    writes nothing. The validated, normalized copy — canonical lowercase
    UUIDs, not the caller's raw casing — is what is durably stored.

    Minting is a ONE-TIME event per work_id: a second mint attempt for the
    same work_id raises ValueError and writes nothing (every subsequent
    lifecycle change is `append_work_unit_transition`, never a re-mint).
    This check-then-write happens under this work_id's own append lock, so
    two racing mint attempts for the same work_id can never both succeed.

    Returns the durably stored record (a dict, JSON-native — tuples
    normalized by `validate_work_unit` serialize as JSON arrays and read
    back as lists, matching every other jsonl history in this module).
    """
    validated = _import_workunit().validate_work_unit(record)
    path = work_unit_history_path_for(validated["session_id"], validated["work_id"])

    def build(existing):
        if existing:
            raise ValueError(
                "work_id %r already minted (%d existing history record(s))"
                % (validated["work_id"], len(existing)))
        entry = dict(validated)
        entry["transition_index"] = 0
        entry["recorded_at"] = _utc_now()
        return entry

    return _locked_jsonl_append(path, build)


def append_work_unit_transition(record):
    """Durably append one lifecycle-transition record to an ALREADY-MINTED
    work_id's append-only history.

    `record` is validated via `cowork_workunit.validate_work_unit` (Package
    A) BEFORE anything is written. Raises ValueError — writing nothing — when
    the work_id has no minted history yet (`mint_work_unit` must run first;
    a transition can never be the first entry). Serialized against every
    other appender for the same work_id via the same per-path lock
    `mint_work_unit` uses, so a mint and a transition (or two transitions)
    racing for the same work_id are always ordered, never interleaved.

    Returns the durably stored record.
    """
    validated = _import_workunit().validate_work_unit(record)
    path = work_unit_history_path_for(validated["session_id"], validated["work_id"])

    def build(existing):
        if not existing:
            raise ValueError(
                "work_id %r has no minted history; call mint_work_unit first"
                % validated["work_id"])
        entry = dict(validated)
        entry["transition_index"] = len(existing)
        entry["recorded_at"] = _utc_now()
        return entry

    return _locked_jsonl_append(path, build)


def append_work_unit_transition_unlocked(record):
    """Reentrant twin of `append_work_unit_transition`, for a caller that
    may run while this SAME process already holds this (session_id,
    work_id)'s WorkUnit lock -- narrowly, M2 Package E's external-kill
    signal handler mirroring a just-written terminal PhaseState onto the
    SAME WorkUnit join key while the main flow's own locked
    `append_work_unit_transition`/`mint_work_unit` call for the same
    work_id may still be in its locked critical section. See the
    module-level SELF-DEADLOCK banner above this section --the same hazard
    `append_phase_state_entry_unlocked` exists for applies identically to
    this file's WorkUnit lock: a second `open()` + `flock()` in this same
    process for the same `<path>.lock` is a distinct lock owner and blocks
    forever, not merely waits.

    Identical validation and identical durable-record shape to the locked
    twin -- the only difference is `_jsonl_append_unlocked` in place of
    `_locked_jsonl_append`, exactly mirroring
    `append_phase_state_entry_unlocked`'s relationship to
    `append_phase_state_entry` (including that function's freshness-checked
    stale-build retry, so a record built here against a since-superseded
    `existing` read is rebuilt against the current file rather than
    corrupting append order). Raises ValueError -- writing nothing -- when
    work_id has no minted history yet, exactly like the locked twin.

    Offers no additional concurrency safety beyond reentrancy: a genuinely
    concurrent (different-process, or different-thread NOT caused by signal
    re-entry) caller must still go through the locked
    `append_work_unit_transition`.
    """
    validated = _import_workunit().validate_work_unit(record)
    path = work_unit_history_path_for(validated["session_id"], validated["work_id"])

    def build(existing):
        if not existing:
            raise ValueError(
                "work_id %r has no minted history; call mint_work_unit first"
                % validated["work_id"])
        entry = dict(validated)
        entry["transition_index"] = len(existing)
        entry["recorded_at"] = _utc_now()
        return entry

    return _jsonl_append_unlocked(path, build)


def read_work_unit_history(session_id, work_id):
    """Every persisted lifecycle record for work_id, oldest first. Tolerant:
    `[]` when the work_id was never minted (including every legacy, pre-M2
    session, which has no `work_units/` directory at all)."""
    path = work_unit_history_path_for(session_id, work_id)
    return read_jsonl_tolerant(path)


def current_work_unit_state(session_id, work_id):
    """The latest persisted lifecycle record for work_id (the current-state
    accessor), or `None` when the work_id was never minted."""
    history = read_work_unit_history(session_id, work_id)
    return history[-1] if history else None


def work_unit_from_history_record(record):
    """Project one persisted WorkUnit history record (as returned by
    `read_work_unit_history`/`current_work_unit_state`/`mint_work_unit`/
    `append_work_unit_transition`) back down to the exact canonical WorkUnit
    shape `cowork_workunit.validate_work_unit` accepts.

    Every persisted history record carries two fields the canonical
    WorkUnit schema does not: `transition_index` (this record's position in
    the append-only history) and `recorded_at` (its persistence timestamp).
    Those are storage metadata, not part of the WorkUnit itself, so
    `validate_work_unit` — which enforces an EXACT key set via
    `_check_exact_keys` — raises `ValueError: WorkUnit has extra keys: [...]`
    on a persisted record verbatim. This projection drops exactly those two
    keys and nothing else, so
    `validate_work_unit(work_unit_from_history_record(current_work_unit_state(
    session_id, work_id)))` round-trips a read-back record through the same
    schema validation a freshly-minted one already passed — reconstruction
    reproduces exactly what a clean run would have produced.

    A pure dict projection: never mutates `record`, performs no I/O, and
    does not itself call `validate_work_unit` (no Package A dependency of
    its own) — a caller that wants revalidation calls it explicitly on the
    returned dict. Returns `None` when `record` is `None`, so a `None`
    history record (nothing minted yet) projects to `None`, not an error.
    """
    if record is None:
        return None
    projected = dict(record)
    projected.pop("transition_index", None)
    projected.pop("recorded_at", None)
    return projected


# --------------------------------------------------------------------------- #
# Dependency-graph store: versioned, append-only, immutable revisions +       #
# current-revision accessor. Validated with Package A (cowork_workunit).      #
# --------------------------------------------------------------------------- #


def graph_revisions_path_for(session_id):
    """Path of one session's append-only dependency-graph revision log."""
    session_id = _lower_safe_identifier(session_id, "session_id")
    return os.path.join(session_assets_dir(session_id), "graph",
                        "revisions.jsonl")


def append_graph_revision(session_id, nodes):
    """Validate `nodes` (Package A: `cowork_workunit.validate_revision`) as
    the session's NEXT dependency-graph revision and durably append it.

    IMMUTABLE + APPEND-ONLY: revisions already on disk are read only to
    COUNT them (to number the new one) and are never rewritten; this call
    only ever adds one new line. Validation happens BEFORE the write — a
    revision that fails `validate_revision` (duplicate work_id, dangling
    predecessor, self-edge, cycle, cross-candidate/cross-policy fan-in)
    raises `cowork_workunit.GraphValidationError` and writes nothing, so a
    rejected revision never lands on disk even partially. Each revision is
    validated in isolation from every other revision, exactly like
    `cowork_workunit.append_revision` — persisting a new one never
    reinterprets or mutates a prior one.

    Two racing appenders for the same session are serialized by this
    session's own append lock, so they can never both mint the same
    `graph_revision` number.

    Returns the durably stored revision dict `{schema_version, record,
    graph_revision, nodes, recorded_at}`.
    """
    path = graph_revisions_path_for(session_id)

    def build(existing):
        wu = _import_workunit()
        normalized_nodes = wu.validate_revision(nodes)
        return {
            "schema_version": wu.SCHEMA_VERSION,
            "record": "DependencyGraphRevision",
            "graph_revision": len(existing) + 1,
            "nodes": [dict(n) for n in normalized_nodes],
            "recorded_at": _utc_now(),
        }

    return _locked_jsonl_append(path, build)


def read_graph_revisions(session_id):
    """Every persisted dependency-graph revision, oldest first, as a tuple
    (matching `cowork_workunit`'s in-memory graph representation). Tolerant:
    `()` when no revision has ever been appended (including every legacy,
    pre-M2 session)."""
    return tuple(read_jsonl_tolerant(graph_revisions_path_for(session_id)))


def current_graph_revision(session_id):
    """The latest persisted dependency-graph revision dict (the
    current-revision accessor), or `None` when the session has none yet."""
    revisions = read_graph_revisions(session_id)
    return revisions[-1] if revisions else None


# --------------------------------------------------------------------------- #
# Durable, taxonomy-typed PhaseState history per work_id/session. ADDITIVE to #
# the legacy session-level `get_phase`/`save_phase` above, which track an     #
# entirely different concept (the scouting/planning/building SESSION phase,  #
# not one work item's closed `cowork_control_plane.PHASE_STATE_SET` state).  #
# Suitable as the durable target for Package E's external-kill and           #
# controller-switch TERMINAL records — see the module-level signal-safety    #
# limitation banner above this section.                                     #
# --------------------------------------------------------------------------- #


def phase_state_history_path_for(session_id, work_id):
    """Path of one work_id's append-only PhaseState history within its
    session."""
    session_id = _lower_safe_identifier(session_id, "session_id")
    work_id = _lower_safe_identifier(work_id, "work_id")
    return os.path.join(session_assets_dir(session_id), "phase_state",
                        "%s.jsonl" % work_id)


def _validate_phase_state_args(cp, session_id, work_id, state, reason_code,
                               evidence):
    """Shared, fail-closed argument validation for
    `append_phase_state_entry`/`append_phase_state_entry_unlocked`. Raises
    ValueError (writing nothing) for anything that must never be persisted."""
    if state not in cp.PHASE_STATE_SET:
        raise ValueError(
            "state must be one of %s, got %r"
            % (sorted(cp.PHASE_STATE_SET), state))
    if state in cp.TERMINAL_STATES:
        if not isinstance(reason_code, str) or not reason_code:
            raise ValueError(
                "reason_code must be a nonempty string for terminal state %r"
                % state)
    if state == "completed":
        # Fail closed, and CANDIDATE-BOUND TO THIS WORK_ID SPECIFICALLY (not
        # merely to *some* well-formed candidate): `completed` may never be
        # recorded without gate evidence naming the exact candidate THIS
        # work_id's own minted WorkUnit record carries. A minted WorkUnit is
        # therefore REQUIRED here -- there is no other place this store can
        # get a genuine candidate identity to bind against, and a work_id
        # with no minted WorkUnit has no legitimate candidate to be bound
        # to. Deriving `expected_candidate` from the durable WorkUnit record
        # and passing it into `cowork_control_plane.advance`'s OWN
        # `("awaiting_gate", "gate_validated")` rule (the sole edge into
        # `completed`) means evidence naming a DIFFERENT, otherwise
        # well-formed candidate (e.g. copied from another work_id) is
        # refused with reason_code "gate_evidence_candidate_mismatch",
        # exactly like a genuine caller advancing that specific WorkUnit
        # would be refused.
        work_unit = work_unit_from_history_record(
            current_work_unit_state(session_id, work_id))
        if work_unit is None:
            raise ValueError(
                "state 'completed' requires an already-minted WorkUnit for "
                "session_id=%r work_id=%r to bind gate evidence to -- call "
                "mint_work_unit first" % (session_id, work_id))
        expected_candidate = {
            "candidate_manifest_digest": work_unit.get("candidate_manifest_digest"),
            "candidate_index": work_unit.get("candidate_index"),
        }
        gated_state, why = cp.advance(
            "awaiting_gate", "gate_validated", evidence=evidence,
            expected_candidate=expected_candidate)
        if gated_state != "completed":
            raise ValueError(
                "state 'completed' requires candidate-bound, valid "
                "gate-validation evidence naming THIS work_id's own "
                "candidate %r; cowork_control_plane.advance refused with "
                "reason_code %r" % (expected_candidate, why))


def _first_terminal_record(cp, history):
    """The first record in `history` (oldest-first) whose `state` is a
    `cowork_control_plane.TERMINAL_STATES` member, or `None` if none exists.
    Scanning the WHOLE history — not merely the last entry — is what makes
    B-14's residual race harmless: a stale, non-terminal record that a
    signal landing in the final write-time sliver let land physically AFTER
    a handler-written terminal one is still positioned strictly LATER than
    that terminal record, so the terminal record this returns is always the
    genuine, first-durably-written one, never the race artifact."""
    for record in history:
        if record.get("state") in cp.TERMINAL_STATES:
            return record
    return None


def _mint_append_id():
    """A fresh, independently-minted, collision-resistant identity token for
    ONE build/append attempt (B-SHAPE-R2).

    128 bits from `secrets` (a CSPRNG, not merely `random`) — the intended
    property here is collision-resistance, not unpredictability, but the
    stdlib's collision-resistant source is the same one either way, and this
    value is minted fresh on EVERY call to `build()` below, including a
    discarded/retried build (see `_jsonl_append_unlocked`'s stale-build
    retry): only the token attached to the record that actually lands
    durably ever matters, so a retried build simply mints and discards
    another one, never reuses the token from a build that never wrote."""
    return secrets.token_hex(16)


def _phase_state_builder(cp, session_id, work_id, state, reason_code, event,
                         evidence, source):
    """Build the record to append, and — closing B-11 and the B-14-R1
    residual — refuse to build ANYTHING once ANY record already in
    `existing` is terminal, wherever it sits in the history, not merely as
    the last entry. `cowork_control_plane.TERMINAL_STATES` documents
    terminal states as having "no legal outbound M2 transition"; enforcing
    that against the FRESH read `_jsonl_append_unlocked`'s stale-build retry
    always hands this closure immediately before the actual write (see that
    function's docstring) is what REFUSES an interrupted, stale-derived
    append for every interruption up to and including that freshness
    re-read: the interrupted frame's retry sees the fresh terminal record
    and raises instead of building a competing one. The one interruption
    this alone cannot see — a signal landing strictly AFTER that re-read but
    BEFORE the following write — is instead made harmless, at READ TIME,
    by `current_phase_state` treating the first durably-written terminal
    record as dominant CURRENT truth regardless of what physically lands
    after it, terminal or not (including a second, later-written terminal
    record from a genuine terminal-vs-terminal race — B-14-R2-A); the
    stray record itself is never discarded, only truthfully marked
    `superseded=True` by `read_phase_state_history`'s reconciliation (see
    the module-level TERMINAL-DOMINANT RECONSTRUCTION banner).

    Every built record also carries a fresh `append_id` (B-SHAPE-R2, see
    `_mint_append_id`) — the field `_reconciled_phase_state_entry` below
    uses to find THIS call's own durable record on readback, deliberately
    never `recorded_at` (see that function's docstring for why a
    timestamp, even a real one, cannot be trusted as sole identity)."""
    def build(existing):
        prior_terminal = _first_terminal_record(cp, existing)
        if prior_terminal is not None:
            raise ValueError(
                "cannot append PhaseState state=%r for session_id=%r "
                "work_id=%r: state %r (transition_index=%r) is already "
                "terminal and has no legal outbound transition -- this is "
                "exactly what prevents an interrupted, stale-derived append "
                "from overtaking an external-kill's terminal record"
                % (state, session_id, work_id, prior_terminal.get("state"),
                   prior_terminal.get("transition_index")))
        return {
            "session_id": session_id,
            "work_id": work_id,
            "state": state,
            "reason_code": reason_code,
            "event": event,
            "evidence": evidence,
            "source": source,
            "transition_index": len(existing),
            "recorded_at": _utc_now(),
            "append_id": _mint_append_id(),
        }
    return build


def append_phase_state_entry(session_id, work_id, state, reason_code,
                             event=None, evidence=None, source=None):
    """Durably append one taxonomy-typed PhaseState record for
    (session_id, work_id).

    `state` must be a member of `cowork_control_plane.PHASE_STATE_SET`;
    anything else raises ValueError before anything is written, so a caller
    (e.g. Package E's SIGTERM/controller-switch handling) can never persist
    a state string outside the closed taxonomy. `reason_code` must be a
    nonempty string whenever `state` is a `cowork_control_plane.
    TERMINAL_STATES` member — a terminal record can never claim to be
    reasonless — and is otherwise free-form (may be None). `state ==
    'completed'` additionally fails closed unless `session_id`/`work_id`
    already have a minted WorkUnit and `evidence` is well-shaped, passing
    gate-validation evidence naming THAT SAME WorkUnit's own candidate
    (digest + index) under `cowork_control_plane.advance`'s own
    `("awaiting_gate", "gate_validated")` rule with `expected_candidate`
    bound to it (see `_validate_phase_state_args`) — evidence naming a
    different, even otherwise well-formed, candidate is refused, and so is a
    missing or candidate-free WorkUnit. Nothing is written on any of these
    rejections.

    Once ANY record for (session_id, work_id) is observed as terminal by
    `_phase_state_builder`'s freshness-checked read, every further append
    THIS FUNCTION attempts against that read is refused (see
    `_phase_state_builder`, which scans the WHOLE history, not merely the
    last entry): terminal states have no legal outbound transition. This
    closes the race for every interruption up to and including
    `_jsonl_append_unlocked`'s freshness re-read. It does NOT, by itself,
    make a terminal record un-appendable-after: in the one residual
    write-time sliver documented on `_jsonl_append_unlocked` (a signal
    handler racing between that function's re-read and its own write), a
    separate physical record — terminal or not, including a genuine
    terminal-vs-terminal race, e.g. a `completed` write racing an
    external-kill `aborted` — CAN still durably land after a handler-written
    terminal one. What IS guaranteed, unconditionally, is CURRENCY, not
    write-refusal: `read_phase_state_history`'s reconciliation
    (`_reconstruct_phase_state_history`) marks every record positioned after
    the first durably-written terminal one as `superseded=True` (present on
    every record this function's own return value and every reader agree
    on), and `current_phase_state` — "the last record with
    `superseded=False`" — always reports that first terminal record as
    current regardless of what, if anything, durably lands after it. A
    handler-written terminal record is therefore un-overtakeable AS CURRENT
    STATE; it is never claimed to be un-appendable-after (see the
    module-level TERMINAL-DOMINANT RECONSTRUCTION banner above the M2
    Package B section for the full mechanism).

    Append-only via the same lock+single-`os.write(O_APPEND)` pattern as the
    rest of this module (see `_locked_jsonl_append`); two concurrent
    appenders for the same work_id are serialized, never interleaved.

    MUST NOT be called from a signal handler that may run while this same
    process already holds this (session_id, work_id)'s PhaseState lock — see
    the module-level SELF-DEADLOCK banner above the M2 Package B section and
    `append_phase_state_entry_unlocked` below, the safe entry point for that
    case (e.g. Package E's external-kill handler).

    NO THREAD-COUNT PRECONDITION: unlike a prior version of this module,
    this call never inspects `threading.active_count()` and never raises
    `OSError` because another thread is alive — every PhaseState write,
    terminal or not, succeeds in the intended multi-threaded host exactly as
    it would in a single-threaded one. Durability of the terminal record
    under the exact handler/interrupted-append interleavings this module
    guards against instead comes from `_phase_state_builder`'s whole-history
    terminal scan plus `current_phase_state`'s terminal-dominant
    reconstruction — see the module-level TERMINAL-DOMINANT RECONSTRUCTION
    banner above the M2 Package B section for the full mechanism and why the
    old thread-count precondition was withdrawn.

    Returns the CANONICAL, READ-TIME-RECONCILED record for this entry —
    identical in shape to what `read_phase_state_history`/
    `current_phase_state` return for the same durable entry, including the
    true-position `transition_index` and the `superseded` flag (B-14-R2-
    SHAPE-OPEN, closed via `_reconciled_phase_state_entry`) — never the raw
    build-time dict alone, so a caller can never receive an identity shape
    that silently disagrees with the same record after readback.
    """
    cp = _import_control_plane()
    _validate_phase_state_args(cp, session_id, work_id, state, reason_code,
                               evidence)
    path = phase_state_history_path_for(session_id, work_id)
    build = _phase_state_builder(
        cp, session_id, work_id, state, reason_code, event, evidence, source)
    written = _locked_jsonl_append(path, build)
    return _reconciled_phase_state_entry(cp, session_id, work_id, written)


def append_phase_state_entry_unlocked(session_id, work_id, state,
                                      reason_code, event=None, evidence=None,
                                      source=None):
    """Reentrant twin of `append_phase_state_entry`, for a caller that may
    run while this SAME process already holds this (session_id, work_id)'s
    PhaseState lock — most notably Package E's external-kill signal handler
    recording a terminal PhaseState while the main flow's own
    `append_phase_state_entry` call for the same work_id is still in its
    locked critical section. See the module-level SELF-DEADLOCK banner above
    the M2 Package B section for why `append_phase_state_entry` itself is
    unsafe to call in that situation (mirrors
    `cowork_ledger._append_locked`'s reason for existing).

    Identical validation and identical CANONICAL return shape — including
    the same candidate-bound `completed` gate, the same freshness-checked
    terminal refusal, and the same read-time-reconciled `transition_index`/
    `superseded` on the returned record — as `append_phase_state_entry`; see
    that function's docstring, especially the CURRENCY-vs-write-refusal
    distinction it documents (this is precisely the entry point that can
    durably write the SECOND record — terminal or not — of a residual-sliver
    race, e.g. Package E's external-kill `aborted` racing a main-flow
    `completed`; see `PhaseStateFinalWindowSignalTest` in the test suite).
    The only difference from the locked twin is that this never calls
    `fcntl.flock`, so it never contends with a lock this process may
    already be holding via a different fd; instead, `_jsonl_append_unlocked`
    re-checks freshness immediately before writing (see its docstring) so a
    stale build here is retried against the current file rather than
    corrupting order. It offers no additional concurrency safety beyond
    that: a genuinely concurrent (different-process, or different-thread NOT
    caused by signal re-entry) caller must still go through the locked
    `append_phase_state_entry`.

    NO THREAD-COUNT PRECONDITION (see `append_phase_state_entry`'s docstring
    for the full mechanism): this call never inspects
    `threading.active_count()` and never refuses because a foreign thread is
    alive — Package E's external-kill handler can always durably record a
    terminal PhaseState here, in the intended multi-threaded host exactly as
    in a single-threaded one.
    """
    cp = _import_control_plane()
    _validate_phase_state_args(cp, session_id, work_id, state, reason_code,
                               evidence)
    path = phase_state_history_path_for(session_id, work_id)
    build = _phase_state_builder(
        cp, session_id, work_id, state, reason_code, event, evidence, source)
    written = _jsonl_append_unlocked(path, build)
    return _reconciled_phase_state_entry(cp, session_id, work_id, written)


def _reconstruct_phase_state_history(cp, raw_history):
    """Turn the RAW, exactly-as-durably-written record list into the
    truthful, non-ambiguous one every consumer — `read_phase_state_history`
    and, through it, `read_m2_state` — actually sees (B-14-R2-B: identity
    and index semantics must be truthful to raw-history/read_m2_state
    consumers themselves, not merely papered over inside
    `current_phase_state`).

    Two corrections, applied uniformly to every record (a no-op copy for
    the overwhelmingly common non-race case):

    1. `transition_index` is OVERWRITTEN to equal the record's true
       position `i` in this oldest-first list, never trusted from
       whatever was embedded at build time. The one residual B-14 write-time
       sliver (a signal landing strictly between `_jsonl_append_unlocked`'s
       freshness re-read and its write) can durably land a stale record
       carrying the SAME embedded index as an earlier terminal one — every
       consumer of this corrected list instead sees a strictly increasing,
       collision-free `0..len-1` sequence that always matches true file
       position, closing "duplicate/ambiguous transition identity" outright
       rather than merely hiding it from one accessor.
    2. `superseded` (new field, always present, bool) is `True` for every
       record positioned AFTER the FIRST terminal record this history
       contains (see `cowork_control_plane.TERMINAL_STATES`), `False`
       otherwise — including for the terminal record itself, and for a
       SECOND terminal-vs-terminal race loser (B-14-R2-A: two terminal
       writes racing in the same residual sliver both durably land, but
       only the genuinely first-written one is ever `superseded=False`).
       A record with `superseded=True` is a durable, truthfully-labeled
       race artifact — never silently indistinguishable from a legitimate
       transition to a caller reading this list directly, never merely
       absent from `current_phase_state`.

    Never mutates the underlying file or the raw dicts read from it —
    returns NEW dicts; the append-only bytes on disk are exactly what was
    durably written, unchanged."""
    first_terminal_pos = None
    for i, record in enumerate(raw_history):
        if record.get("state") in cp.TERMINAL_STATES:
            first_terminal_pos = i
            break
    reconstructed = []
    for i, record in enumerate(raw_history):
        entry = dict(record)
        entry["transition_index"] = i
        entry["superseded"] = (
            first_terminal_pos is not None and i > first_terminal_pos)
        reconstructed.append(entry)
    return reconstructed


def _reconciled_phase_state_entry(cp, session_id, work_id, written):
    """Turn the just-appended raw `written` record (as returned by
    `_locked_jsonl_append`/`_jsonl_append_unlocked`, carrying only its
    build-time `transition_index` and no `superseded` key at all) into the
    SAME canonical shape `read_phase_state_history`/`current_phase_state`
    return for that identical durable entry (closes B-14-R2-SHAPE-OPEN): a
    caller of `append_phase_state_entry`/`_unlocked` must not receive an
    identity shape that silently disagrees with the same durable record
    after readback.

    Re-reads and reconciles the full history via
    `_reconstruct_phase_state_history` (the exact function
    `read_phase_state_history` itself uses) and returns the reconciled
    entry whose `append_id` equals `written`'s (B-SHAPE-R2 correction).

    IDENTITY IS `append_id` ALONE, DELIBERATELY NOT `recorded_at` AND NOT
    any caller-supplied field. `append_id` is a fresh 128-bit token minted
    by `_mint_append_id()` exactly once per successful build attempt (see
    `_phase_state_builder`'s `build()` closure) — never derived from, or
    influenced by, `evidence`/`event`/`source`/`reason_code`/wall-clock
    time, so it carries none of THREE separate failure modes a caller or
    the host clock can trigger:

    1. EVIDENCE SHAPE (B-SHAPE-R1, still closed by this same field):
       matching on every field `_phase_state_builder` set, evidence
       included, broke through plain Python `==` whenever `evidence`
       contained a tuple (serializes as a JSON array, reads back as a
       `list`, `(1, 2) == [1, 2]` is `False`), a non-string dict key
       (coerced to its string form on write, `{1: "a"} == {"1": "a"}` is
       `False` on readback), or `NaN` (never `==` itself regardless of
       serialization). None of the three is a defect in the write path
       itself, yet any of them made a genuinely successful, durable write
       raise `RuntimeError` here purely because of what the caller
       happened to put in `evidence`.
    2. DUPLICATE `recorded_at` (B-SHAPE-R2): a prior version of this
       function matched on `recorded_at` ALONE, taking the LAST reconciled
       entry carrying that value as "close enough" on the theory that two
       builds mint identical microsecond-precision timestamps only
       vanishingly rarely. That is a timestamp-UNIQUENESS assumption, not a
       proof, and it is false in exactly the case this correction targets:
       two DIFFERENT durable records — e.g. this call's own write racing a
       reentrant signal-handler write via `append_phase_state_entry_
       unlocked` (see the module-level SELF-DEADLOCK / TERMINAL-DOMINANT
       RECONSTRUCTION banners) — can land with `_utc_now()` returning the
       identical string for both, whether from coincident real-clock
       precision or a host whose clock genuinely does not advance between
       two appends. Taking "the last one with this recorded_at" then risks
       returning a DIFFERENT record than the one this call actually wrote —
       a false `transition_index`/`superseded` value silently attributed to
       the wrong entry — purely because that other record happened to be
       appended (by anyone, anywhere) after this call's own write but
       before this reconciliation's re-read.
    3. BACKWARD-MOVING WALL CLOCK: nothing about `_utc_now()` guarantees
       monotonicity — an NTP step, a container clock reset, or a suspended/
       resumed host can all make a LATER append's `recorded_at` compare
       EARLIER than a prior one's. `recorded_at`-based matching (LAST
       occurrence, or any timestamp-comparison-based selection) has no
       principled way to stay correct once the values it is matching on no
       longer even reflect append order; `transition_index`/`superseded`
       already never depended on `recorded_at` (see
       `_reconstruct_phase_state_history`: true file position and the
       first-terminal scan, in append order, are the only inputs) — this
       correction extends that same clock-independence to identity itself.

    `append_id` is immune to all three: it is always a plain string this
    module mints itself from a CSPRNG, never a container, never NaN, never
    caller-influenced, and never derived from wall-clock time at all — two
    independently-minted 128-bit tokens colliding is not a plausible event
    in the way a shared/backward timestamp is, so "the reconciled entry
    whose `append_id` matches" is unambiguous by construction rather than
    "probably fine" like the pre-B-SHAPE-R2 approach. Matching still
    prefers the END of the reconciled list purely as a minor optimization
    (the just-written record is usually near the end, so this typically
    finds it in the fewest iterations), NOT because search direction
    changes which entry CAN match — with a collision-resistant token, AT
    MOST one reconciled entry ever matches at all, so direction is
    otherwise irrelevant to correctness.
    Raises `RuntimeError` — never silently returns an unreconciled or
    wrong-identity shape — only when no reconciled record carries this
    call's own `append_id` at all, a case that indicates a genuine anomaly
    in the write/read path itself, never a consequence of `evidence`'s (or
    `event`'s/`source`'s/`reason_code`'s) shape, a duplicate `recorded_at`,
    or a backward clock step."""
    raw = read_jsonl_tolerant(phase_state_history_path_for(session_id, work_id))
    reconciled = _reconstruct_phase_state_history(cp, raw)
    append_id = written.get("append_id")
    for entry in reversed(reconciled):
        if entry.get("append_id") == append_id:
            return entry
    raise RuntimeError(
        "appended PhaseState record for session_id=%r work_id=%r "
        "(append_id=%r) not found in its own history on immediate "
        "readback -- this should be unreachable" % (
            session_id, work_id, append_id))


def read_phase_state_history(session_id, work_id):
    """Every persisted PhaseState record for (session_id, work_id), oldest
    first. Tolerant: `[]` when none has ever been recorded (including every
    legacy, pre-M2 session) — this empty case needs no Package A import at
    all.

    TRUTHFULLY RECONSTRUCTED, not a raw pass-through (B-14-R2-B): a
    non-empty result is run through `_reconstruct_phase_state_history`,
    which overwrites `transition_index` with each record's true position
    (never a possibly-stale/colliding embedded value) and adds a
    `superseded` flag naming any record positioned after the first terminal
    one as exactly that — a durable but non-authoritative race artifact —
    rather than leaving a direct reader of this list unable to tell a
    genuine transition from one. This is a no-op renumbering for the
    overwhelmingly common non-race history (every record already
    `superseded=False`, indices already contiguous). `current_phase_state`
    (below) is built on top of this same reconciled list, so both accessors
    always agree.

    Requires Package A (`cowork_control_plane.TERMINAL_STATES`) only when
    there is at least one record to reconcile — a genuinely empty history
    (including every legacy, pre-M2 session, and any session that never
    recorded a PhaseState) never imports it, exactly like every other
    tolerant reader in this module. `read_m2_state` catches the `ImportError`
    this raises for a NON-empty history when Package A is genuinely absent
    (e.g. an isolated diagnostic snapshot reading files a full deployment
    wrote) and degrades to its tolerant absent shape rather than propagate
    it — see that function's docstring."""
    path = phase_state_history_path_for(session_id, work_id)
    raw = read_jsonl_tolerant(path)
    if not raw:
        return []
    cp = _import_control_plane()
    return _reconstruct_phase_state_history(cp, raw)


def current_phase_state(session_id, work_id):
    """The durable, dominant current PhaseState record for
    (session_id, work_id), or `None` when none has ever been recorded.

    Simply the last NON-`superseded` record in `read_phase_state_history`'s
    reconciled list — see `_reconstruct_phase_state_history` for exactly
    what `superseded` means and why it, not `history[-1]`, is the correct
    boundary. Concretely: the first durably-written terminal record if the
    history contains one (un-overtakeable by any later record, terminal or
    not — B-14-R2-A's terminal-vs-terminal race included), otherwise the
    last (most recent) record, exactly as before. This function does not
    import Package A itself; it inherits whatever
    `read_phase_state_history` already reconciled."""
    current = None
    for record in read_phase_state_history(session_id, work_id):
        if record["superseded"]:
            break
        current = record
    return current


# --------------------------------------------------------------------------- #
# Atomic controller policy/config transition record.                          #
#                                                                              #
# A proposed controller policy/config change resolves in ONE atomic          #
# operation to 'committed' or 'rejected' — never left ambiguous, and a        #
# rejected transition leaves the durable file (and therefore every reader's   #
# "active" view of it) byte-identical to its pre-attempt state, because the   #
# write only ever happens on the committed path.                             #
# --------------------------------------------------------------------------- #


def controller_transition_path_for(session_id):
    """Path of the durable committed controller policy/config state for
    `session_id`."""
    session_id = _lower_safe_identifier(session_id, "session_id")
    return os.path.join(session_assets_dir(session_id),
                        "controller_transition.json")


def controller_transition_log_path_for(session_id):
    """Path of the append-only audit log of every transition ATTEMPT
    (committed or rejected) for `session_id`."""
    session_id = _lower_safe_identifier(session_id, "session_id")
    return os.path.join(session_assets_dir(session_id),
                        "controller_transition_log.jsonl")


def read_controller_transition(session_id):
    """The durable controller policy/config state for `session_id`, or the
    zero-revision default `{"policy": None, "config": None, "revision": 0}`
    when nothing has ever committed. Tolerant: a missing or malformed file
    reads as the same default rather than raising — a legacy or pre-M2
    session synthesizes this as absent, matching `read_m2_state` below."""
    data = read_json_tolerant(controller_transition_path_for(session_id))
    if not isinstance(data, dict) or "revision" not in data:
        return {"policy": None, "config": None, "revision": 0}
    return data


def propose_controller_transition(session_id, expected_revision, policy=None,
                                  config=None, validate=None, reason=None,
                                  source=None):
    """Resolve one proposed controller policy/config change ATOMICALLY to
    'committed' or 'rejected'.

    Optimistic concurrency (compare-and-swap on `revision`): the caller
    passes `expected_revision`, the revision it last observed via
    `read_controller_transition`. The whole read-check-write sequence runs
    under this session's ONE controller-transition lock (mirrors
    `cowork_ledger._with_ledger_lock`), so two racing proposers can never
    both commit against the same prior revision — the second sees the
    first's new revision and is rejected with reason 'stale_revision',
    writing nothing. A racing caller therefore cannot silently "win": only
    the returned dict says whether ITS proposal became durable, and ignoring
    it does not make an unwritten proposal active.

    `validate(current, policy, config)` is an OPTIONAL caller-supplied guard
    invoked with the lock held and BEFORE any write; raising `ValueError`
    from it rejects the transition (reason `'invalid: <message>'`) and, like
    every other rejection path here, writes nothing — the durable bytes stay
    byte-identical to the pre-attempt state.

    Returns `{'outcome': 'committed', 'reason': ..., 'state': <new durable
    state>}` or `{'outcome': 'rejected', 'reason': ..., 'state': <unchanged
    current state>}`. Every attempt, committed or rejected, is additionally
    appended to the append-only transition log for audit (best-effort: a log
    append failure does not change the returned outcome, since the durable
    policy/config file is already the source of truth by that point).
    """
    if not session_id:
        return {"outcome": "rejected", "reason": "no_session", "state": None}
    lock_path = controller_transition_path_for(session_id) + ".lock"
    dirname = os.path.dirname(lock_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            current = read_controller_transition(session_id)
            stamp = _utc_now()
            outcome, why, state = None, None, current
            if current.get("revision", 0) != expected_revision:
                outcome, why = "rejected", "stale_revision"
            elif validate is not None:
                try:
                    validate(current, policy, config)
                except ValueError as exc:
                    outcome, why = "rejected", "invalid: %s" % exc
            if outcome is None:
                new_state = {
                    "policy": policy if policy is not None else current.get("policy"),
                    "config": config if config is not None else current.get("config"),
                    "revision": current.get("revision", 0) + 1,
                    "updated": stamp,
                }
                path = controller_transition_path_for(session_id)
                if write_json_atomic(path, new_state):
                    outcome, why, state = "committed", reason, new_state
                else:
                    outcome, why, state = "rejected", "write_failed", current
            log_entry = {
                "outcome": outcome,
                "reason": why,
                "expected_revision": expected_revision,
                "current_revision": current.get("revision", 0),
                "source": source,
                "recorded_at": stamp,
            }
            append_jsonl_atomic(
                controller_transition_log_path_for(session_id), log_entry)
            return {"outcome": outcome, "reason": why, "state": state}
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_fh.close()


def read_controller_transition_log(session_id):
    """Every recorded transition attempt (committed or rejected) for
    `session_id`, oldest first. Tolerant: `[]` when none has ever been
    attempted."""
    return read_jsonl_tolerant(controller_transition_log_path_for(session_id))


# --------------------------------------------------------------------------- #
# Legacy read/migration shim.                                                 #
#                                                                              #
# A version-1 `.cowork/session*.json` anchor written before M2 existed        #
# carries none of the fields above; `load`/`save` above are UNCHANGED by this #
# package (no new required key was added to the version-1 schema), so such an #
# anchor keeps loading with prior externally observable behavior. This shim   #
# is the single tolerant entry point for reading the M2-owned view alongside  #
# it: every M2 field synthesizes as explicitly ABSENT/UNKNOWN for a session   #
# that never wrote any M2 state, rather than raising or fabricating state     #
# nothing ever wrote.                                                        #
# --------------------------------------------------------------------------- #


def read_m2_state(state, work_id=None):
    """Tolerant migration shim: given a loaded legacy-or-current session
    `state` dict (from `load()`), return the M2-visible view.

    Never raises — including for a TRUTHY but malformed or unsafe
    `session_uuid` (a non-string, or a string that fails the path-safety
    check every path-building helper below applies). `session_uuid` is read
    via `get_session_uuid` (the same accessor every other reader in this
    module uses); a session with none at all — the oldest possible legacy
    anchor — synthesizes every M2 field as absent without touching disk. A
    session WITH a `session_uuid` but no M2 artifacts on disk (any session
    saved before M2, or one that simply never minted a WorkUnit / appended a
    graph revision / recorded a PhaseState / committed a controller
    transition) synthesizes the same absent shape, because every read this
    shim delegates to (`read_work_unit_history`, `read_graph_revisions`,
    `read_phase_state_history`, `read_controller_transition`) is
    independently tolerant of a missing file. A TRUTHY `session_uuid` (or
    `work_id`) that is non-str or fails `_assert_safe_identifier` — e.g. a
    hand-corrupted legacy anchor carrying `{'session_uuid': 12345}` or
    `{'session_uuid': '../../etc'}` — raises `ValueError` deep inside every
    one of those delegated readers' path-building helpers; this shim catches
    that here and synthesizes the SAME absent shape rather than letting it
    propagate, so a corrupt legacy anchor can never crash a caller of this
    tolerant shim.

    Also never raises when Package A (`cowork_control_plane`/
    `cowork_workunit`) is genuinely absent from this deployment (B-03-R1):
    a real `session_uuid`/`work_id` pair naming a session that DOES have
    durable WorkUnit or PhaseState history on disk needs Package A's
    taxonomy (`TERMINAL_STATES`, `validate_work_unit`, ...) to interpret
    it — `current_work_unit_state`/`current_phase_state`/
    `read_phase_state_history` raise a plain `ImportError` in that specific
    case, exactly like every other M2 function that genuinely needs the
    missing sibling and only when actually called (see the module-level
    LAZY DEPENDENCY note above the M2 Package B section). This shim catches
    that `ImportError` here too and synthesizes the SAME absent shape,
    since an isolated snapshot with no Package A siblings has no way to
    interpret M2 semantics at all, regardless of whether disk data exists —
    it must degrade gracefully, never crash a caller of this tolerant shim.
    """
    result = {
        "session_uuid": None,
        "work_unit_state": None,
        "work_unit_history": [],
        "graph_revision": None,
        "graph_revisions": (),
        "phase_state": None,
        "phase_state_history": [],
        "controller_transition": None,
    }
    if not isinstance(state, dict):
        return result
    session_uuid = get_session_uuid(state)
    result["session_uuid"] = session_uuid
    if not session_uuid:
        return result
    absent = dict(result)
    try:
        transition = read_controller_transition(session_uuid)
        if transition.get("revision", 0):
            result["controller_transition"] = transition
        result["graph_revisions"] = read_graph_revisions(session_uuid)
        result["graph_revision"] = current_graph_revision(session_uuid)
        if work_id:
            result["work_unit_history"] = read_work_unit_history(session_uuid, work_id)
            result["work_unit_state"] = current_work_unit_state(session_uuid, work_id)
            result["phase_state_history"] = read_phase_state_history(
                session_uuid, work_id)
            result["phase_state"] = current_phase_state(session_uuid, work_id)
    except (ValueError, ImportError):
        return absent
    return result
