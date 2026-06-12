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

import hashlib
import json
import os

VERSION = 1
DIR_NAME = ".cowork"
FILE_NAME = "session.json"


def session_dir(cwd=None):
    return os.path.join(cwd or os.getcwd(), DIR_NAME)


def session_path(cwd=None):
    return os.path.join(session_dir(cwd), FILE_NAME)


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


def review_path_for(intel_dir, session_uuid):
    """Path of the scout-reviewer's verdict file for a session (sibling of the
    scout intel file)."""
    return os.path.join(intel_dir, "scout-review.%s.json" % session_uuid)


def planner_plan_json_path_for(intel_dir, session_uuid):
    """Path of the planner's JSON plan deliverable (machine source of truth and
    the planner's status channel)."""
    return os.path.join(intel_dir, "planner.plan.%s.json" % session_uuid)


def planner_plan_md_path_for(intel_dir, session_uuid):
    """Path of the planner's human-first markdown plan (the user's review
    surface at the plan gate)."""
    return os.path.join(intel_dir, "planner.plan.%s.md" % session_uuid)


def planner_review_path_for(intel_dir, session_uuid):
    """Path of the planning-advisor's verdict file for a session (sibling of
    the planner plan files)."""
    return os.path.join(intel_dir, "planner-review.%s.json" % session_uuid)


# --------------------------------------------------------------------------- #
# Peer evaluations.                                                            #
#                                                                              #
# After each review round both sides of the active pairing privately score     #
# each other. Each evaluator writes a per-turn scratch file in .cowork (its    #
# ONLY eval write target); the orchestrator reads it back, stamps metadata,    #
# and appends the entries to a per-session aggregate scores.json under         #
# ~/.cowork/sessions/<uuid>/ (orchestrator-written only — evaluators are       #
# never given that path). Purely observational: a missing or malformed         #
# scratch is skipped, never an error.                                          #
# --------------------------------------------------------------------------- #


def eval_scratch_path_for(intel_dir, role, session_uuid):
    """Path of `role`'s private evaluation scratch file for a session
    (overwritten each eval turn; sibling of the other session assets)."""
    return os.path.join(intel_dir, "eval.%s.%s.json" % (role, session_uuid))


def scores_path_for(session_uuid):
    """Path of the per-session aggregate scores file. The root is overridable
    via COWORK_SESSIONS_ROOT so tests never write to the real home dir."""
    root = (os.environ.get("COWORK_SESSIONS_ROOT")
            or os.path.expanduser(os.path.join("~", ".cowork", "sessions")))
    return os.path.join(root, session_uuid, "scores.json")


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


def has_eval_entry(scores_path, evaluator, evaluatee, context,
                   planning_epoch=None):
    """True when the aggregate already holds a matching evaluation.

    The resume-safe "did this already happen" check: the once-per-phase
    ->scout consumed-intel eval must not be re-emitted when a run is resumed
    or restarted within the planning phase, and the in-memory closure flag
    does not survive that. `planning_epoch` scopes the match to one planning
    phase: a hand-back round trip bumps the epoch (even when the re-approved
    intel is byte-identical), so the scout is evaluated again for the new
    phase. With `planning_epoch=None` the match is epoch-agnostic (the safe,
    more-deduping fallback when no epoch is wired). Tolerant by design: a
    missing or malformed aggregate reads as "not yet"."""
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
                     or entry.get("planning_epoch") == planning_epoch)):
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

PHASES = ("scouting", "planning")


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
