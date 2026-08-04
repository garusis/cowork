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
import glob
import hashlib
import json
import os
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


def children_path_for(session_uuid):
    """Append-only child-attempt/provider-correlation ledger."""
    return os.path.join(session_assets_dir(session_uuid), "children.jsonl")


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


def append_jsonl_atomic(path, record):
    """Append one JSON record as a line to `path`, creating it if needed.

    Uses `os.open` with `O_APPEND` so concurrent appenders (the worker writing
    its own attempt events) interleave whole lines rather than torn writes on
    POSIX, matching cowork_ledger's append-only convention. Returns True on
    success, False on any failure (tolerant: evidence write failures must
    degrade to 'poll found nothing yet', never crash the worker or parent)."""
    if not path:
        return False
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        return False
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


def save_pending_turn(path, role, text, prior=None):
    """Persist a failed direct turn for `role` so any future resume or switch replays it."""
    state = dict(prior or load(path) or {})
    pending = dict(state.get("pending_switches") or {})
    entry = dict(pending.get(role) or {})
    entry["pending_turn"] = text
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
