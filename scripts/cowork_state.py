#!/usr/bin/env python3
"""cowork session store.

Persists a cowork session in a project-local `.cowork/session.json` so the team
+ per-role config is not re-asked on the next run in the same directory, and so
the scout's claude/codex session can be resumed if a run is killed.

Schema (version 1):

    {
      "version": 1,
      "team": ["scout", "advisor", ...],
      "config": {"scout": {"controller": "claude", "yolo": true, "mode": "plan"}, ...},
      "context": {                 # current shared session context (versioned)
        "text": "...",
        "hash": "<sha256>",
        "revision": 3,
        "source": "--context"
      },
      "sessions": {
        "scout": {"controller": "claude", "id": "<uuid>",   # claude session_id
                  "last_context_revision_seen": 3}
        # or:    {"controller": "codex",  "id": "<thread_id>", ...}
      }
    }

The context invariant: explicit context (`--context`/prompted goal) is persisted
as the CURRENT session context, with a monotonically increasing revision. Any
role invoked afterward must receive that current context unless it has already
acknowledged that revision (`last_context_revision_seen`); a resumed CLI session
that has not seen the latest revision gets it as an explicit wake block instead
of being discarded.

Python 3.9+, stdlib only. Does not import co_plan_file.py.
"""

import glob
import hashlib
import json
import os
import time

VERSION = 1
DIR_NAME = ".cowork"
FILE_NAME = "session.json"


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


def read_eval(path):
    """Return the normalized list of evaluation dicts from a scratch file, or
    [] when the file is missing, unreadable, or malformed (mirrors
    `read_review`'s tolerance — an eval turn that wrote nothing usable is
    skipped, never an error).

    Normalization: each criterion needs a non-empty `name` and an int-coercible
    `score` (clamped to 1-5); `feedback` and `enhancement_suggestions` are
    stringified. Entries with no parseable criteria are dropped."""
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
            try:
                score = int(crit.get("score"))
            except (TypeError, ValueError):
                continue
            criteria.append({
                "name": name,
                "score": max(1, min(5, score)),
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
            data = {"session": session_uuid, "evaluations": []}
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
