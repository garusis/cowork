#!/usr/bin/env python3
"""Observer for the Jev observational pilot (off by default).

Ties the accepted capture and client libraries to the builder review flow
without ever touching it: every hook is observation only, never raises into the
caller, never reads a reviewer packet and never feeds anything back into review,
approval or delivery.

Activation is a coordinator-written, out-of-Git pilot directory named by the
``COWORK_JEV_PILOT_DIR`` environment variable. Unset, missing or invalid
activation leaves every hook inert (it returns ``None`` without any I/O).

Pilot directory layout (all written by the coordinator unless marked *)::

    cohort_activation.json   cohort_activation.v1 (caps use the protocol s10 keys)
    inclusion_list.json      [{session_id, ticket_ref, objective_text,
                               requirement_text, repository, repo_root,
                               base_ref?}] (digest bound in the activation)
    alias_log.jsonl          alias_log.v1 entries {at, kind, from, to, reason}
    captures/ acceptance/    * capture store and acceptance registry
    jev/                     * client FileStore plus cohort.lock
    observer_registry.jsonl  * append-only observer records
    SUSPENDED                suspension marker (suspend()/resume() or by hand)
    reports/                 * closed reports and dated supplements

Adjudication stays coordinator-executed: this module selects units, builds the
blind brief, reserves the per-unit envelope, takes the outcome and records
costs, but never starts an adjudicator.
"""

import contextlib
import datetime
import fcntl
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_jev_capture as cap  # noqa: E402
import cowork_jev_client as jc  # noqa: E402

PILOT_ENV = "COWORK_JEV_PILOT_DIR"
SUSPENDED_MARKER = "SUSPENDED"
ADJ_ENVELOPE = {"tool_calls": 30, "minutes": 20, "tokens": 400000}
CHECKPOINT_DAYS = 30
ADJ_UNITS_CAP = 40
DEFECT_CLASSES = ("missing_behavior", "contradicts_requirement",
                  "swallowed_error", "partial_state", "wrong_error_outcome")
ORIGINS = ("introduced", "preexisting", "unknown")
OUTCOMES = ("confirmed", "refuted", "unknown")
HARD_STOP_KINDS = ("data_scope_violation", "credential_exposure",
                   "budget_revocation")
BRIEF_VERSION = "jev-adj-brief-v1"
THRESHOLDS = {"sufficiency_gate": 0.70, "alert": 0.80, "uncertain_floor": 0.40}
NOT_QUERIED_INTERRUPTED = "not_queried_interrupted"
VENDOR_RECIPIENT = "typesafe"

SCHEMA_ACTIVATION = "cohort_activation.v1"
SCHEMA_STATE = "jev_candidate_state.v1"
SCHEMA_REVIEW_STARTED = "jev_review_started.v1"
SCHEMA_ORDINARY = "ordinary_review_record.v1"
SCHEMA_LINK = "jev_query_link.v1"
SCHEMA_RESULT = "jev_query_result.v1"
SCHEMA_ABANDONED = "jev_query_abandoned.v1"
SCHEMA_STAMP = "jev_close_stamp.v1"
SCHEMA_INTERVENTION = "jev_intervention.v1"
SCHEMA_HARD_STOP = "jev_hard_stop.v1"
SCHEMA_KEYED = "jev_ordinary_findings_keyed.v1"
SCHEMA_SELECTION = "jev_adjudication_selection.v1"
SCHEMA_ADJ_STARTED = "jev_adjudication_started.v1"
SCHEMA_ADJ_OUTCOME = "jev_adjudication_outcome.v1"
SCHEMA_ADJUDICATION = "jev_adjudication.v1"
SCHEMA_DEFECT = "defect_record.v1"
SCHEMA_ERROR = "jev_observer_error.v1"

_OVERRIDES = {}
_WORKERS = {}


# --------------------------------------------------------------------------- #
# Test seam and small helpers.                                                #
# --------------------------------------------------------------------------- #


def configure(**overrides):
    """Inject transport, credential_provider, clock or thread_starter."""
    _OVERRIDES.update(overrides)


def reset_overrides():
    _OVERRIDES.clear()
    _WORKERS.clear()


def _now():
    clock = _OVERRIDES.get("clock") or cap._utc_now
    return clock()


def _parse(value):
    return cap._parse_utc(value)


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pos_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _pos_num(value):
    return _num(value) and value > 0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


@contextlib.contextmanager
def _locked(path, blocking=True):
    """flock on a lock file; yields True when held, False when contended."""
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


# --------------------------------------------------------------------------- #
# Activation.                                                                 #
# --------------------------------------------------------------------------- #


def validate_activation(act):
    """None when the activation record is usable, else a short reason."""
    if not isinstance(act, dict) or act.get("schema") != SCHEMA_ACTIVATION:
        return "schema"
    for key in ("cohort_id", "salt"):
        if not isinstance(act.get(key), str) or not act[key]:
            return key
    if not _pos_int(act.get("cohort_number")):
        return "cohort_number"
    activated = act.get("activated_at")
    if not isinstance(activated, str) or not activated.endswith("Z") \
            or _parse(activated) is None:
        return "activated_at"
    if act.get("model") != jc.MODEL or \
            act.get("question_set") != jc.QUESTION_SET or \
            act.get("question_set_digest") != jc.QUESTION_SET_DIGEST:
        return "question_set"
    thresholds = act.get("thresholds")
    if not isinstance(thresholds, dict) or any(
            thresholds.get(k) != v for k, v in THRESHOLDS.items()):
        return "thresholds"
    caps = act.get("caps")
    if not isinstance(caps, dict):
        return "caps"
    jev = caps.get("jev")
    if not (isinstance(jev, dict)
            and _pos_int(jev.get("candidates_per_cohort"))
            and _pos_int(jev.get("max_units_queried"))
            and _pos_num(jev.get("proposed_cap_usd"))):
        return "caps.jev"
    adj = caps.get("adjudication")
    if not isinstance(adj, dict):
        return "caps.adjudication"
    cohort = adj.get("cohort_caps")
    if not (isinstance(cohort, dict)
            and all(_pos_num(cohort.get(k))
                    for k in ("tokens", "tool_calls", "wall_minutes"))
            and _pos_int(adj.get("units_cap"))
            and adj.get("per_unit_envelope") == ADJ_ENVELOPE):
        return "caps.adjudication"
    return _authorization_problem(act)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _authorization_problem(act):
    """Structural check of the recorded authorization (protocol s11/s12): no
    interactive approval and no inference of consent, only that every record
    a live call depends on is present and consistent."""
    auth = act.get("authorizations")
    if not isinstance(auth, dict):
        return "authorizations"
    for key in ("budget_ref", "credential_source_ref",
                "vendor_retention_status"):
        if not _text(auth.get(key)):
            return "authorizations.%s" % key
    scope = auth.get("data_scope")
    repos = scope.get("repositories") if isinstance(scope, dict) else None
    if not (isinstance(repos, list) and repos
            and all(_text(r) for r in repos)):
        return "authorizations.data_scope"
    recipients = auth.get("recipients")
    if not (isinstance(recipients, list) and recipients
            and all(_text(r) for r in recipients)):
        return "authorizations.recipients"
    adjudicator = act.get("adjudicator")
    if not (isinstance(adjudicator, dict)
            and _text(adjudicator.get("model_id"))
            and _text(adjudicator.get("provider"))
            and adjudicator.get("brief_version") == BRIEF_VERSION):
        return "adjudicator"
    lowered = [r.strip().lower() for r in recipients]
    if adjudicator["provider"].strip().lower() not in lowered or \
            VENDOR_RECIPIENT not in lowered:
        return "authorizations.recipients"
    docs = auth.get("docs_recheck")
    checked = _parse(docs.get("checked_at")) if isinstance(
        docs, dict) else None
    if not (checked is not None
            and checked <= _parse(act["activated_at"])
            and docs.get("price_usd_per_million_input")
            == str(jc.PRICE_USD_PER_M_INPUT)
            and docs.get("limits_recorded") is True):
        return "authorizations.docs_recheck"
    digest = act.get("inclusion_list_digest")
    if not (isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest)):
        return "inclusion_list_digest"
    return None


def inclusion_digest(inclusion):
    return cap._sha(cap.canonical_json(inclusion))


class Pilot:
    def __init__(self, root, activation, inclusion):
        self.root = root
        self.activation = activation
        self.inclusion = inclusion
        self.cohort_id = activation["cohort_id"]
        self.captures_dir = os.path.join(root, "captures")
        self.acceptance_dir = os.path.join(root, "acceptance")
        self.jev_dir = os.path.join(root, "jev")
        self.registry_path = os.path.join(root, "observer_registry.jsonl")
        self.registry_lock = os.path.join(root, "registry.lock")
        self.cohort_lock = os.path.join(root, "jev", "cohort.lock")
        self.adjudication_lock = os.path.join(root, "adjudication.lock")
        self.boundary_lock = os.path.join(root, "boundary.lock")
        self.suspended_path = os.path.join(root, SUSPENDED_MARKER)
        self.reports_dir = os.path.join(root, "reports")
        self.alias_path = os.path.join(root, "alias_log.jsonl")
        self.client_registry = os.path.join(root, "jev", "jev_registry.jsonl")

    @property
    def caps(self):
        return self.activation["caps"]

    @property
    def deadline(self):
        return _parse(self.activation["activated_at"]) + datetime.timedelta(
            days=CHECKPOINT_DAYS)


def load_pilot(root=None):
    """The active Pilot, or None. Never raises."""
    try:
        root = root or os.environ.get(PILOT_ENV)
        if not root or not os.path.isdir(root):
            return None
        activation = _read_json(os.path.join(root, "cohort_activation.json"))
        if validate_activation(activation) is not None:
            return None
        inclusion = _read_json(os.path.join(root, "inclusion_list.json"))
        if not isinstance(inclusion, (list, dict)):
            return None
        if activation["inclusion_list_digest"] != inclusion_digest(
                inclusion):
            return None
        return Pilot(root, activation, inclusion)
    except Exception:
        return None


def is_suspended(pilot):
    return os.path.exists(pilot.suspended_path)


def halt_reason(pilot):
    """'hard_stop' (revocation, never cleared by resume), 'suspended' or
    None. Reads only; takes no lock and never waits on remote work."""
    for rec in _records(pilot):
        if rec.get("schema") == SCHEMA_HARD_STOP:
            return "hard_stop"
    return "suspended" if is_suspended(pilot) else None


def guard_reason(pilot):
    """Why a NEW paid attempt or reservation must not start, else None."""
    live = load_pilot(pilot.root)
    if live is None:
        return "authorization_invalid"
    return halt_reason(live)


# --------------------------------------------------------------------------- #
# Registry.                                                                   #
# --------------------------------------------------------------------------- #


def _line(rec):
    return json.dumps(rec, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False) + "\n"


def _write_line(pilot, rec):
    with open(pilot.registry_path, "a", encoding="utf-8") as fh:
        fh.write(_line(rec))
        fh.flush()
        os.fsync(fh.fileno())


def _records(pilot):
    return _read_jsonl(pilot.registry_path)


def _append(pilot, rec):
    rec = dict(rec)
    rec.setdefault("at", _now())
    with _locked(pilot.registry_lock):
        _write_line(pilot, rec)
    return rec


def _append_unique(pilot, rec, same):
    """Append unless ``same(existing)`` is true for an existing record.
    Returns the written record, or None when one already exists."""
    rec = dict(rec)
    rec.setdefault("at", _now())
    with _locked(pilot.registry_lock):
        for existing in _records(pilot):
            if existing.get("schema") == rec["schema"] and same(existing):
                return None
        _write_line(pilot, rec)
    return rec


def _log_error(pilot, where, exc):
    try:
        if pilot is not None:
            _append(pilot, {"schema": SCHEMA_ERROR, "where": where,
                            "error": "%s: %s" % (type(exc).__name__,
                                                 str(exc)[:200])})
    except Exception:
        pass


def _of(schema, key, value):
    return lambda rec: rec.get(key) == value


# --------------------------------------------------------------------------- #
# Inclusion list, aliases, close stamps.                                      #
# --------------------------------------------------------------------------- #


def _entry_for(pilot, session_id):
    if isinstance(pilot.inclusion, list):
        for item in pilot.inclusion:
            if isinstance(item, dict) and item.get("session_id") == session_id:
                return item
    elif isinstance(pilot.inclusion, dict):
        ref = pilot.inclusion.get(session_id)
        if isinstance(ref, dict):
            return ref
        if isinstance(ref, str):
            return {"session_id": session_id, "ticket_ref": ref}
    return {}


def _aliases(pilot, kind):
    mapping = {}
    for item in _read_jsonl(pilot.alias_path):
        if item.get("kind") == kind and isinstance(item.get("from"), str) \
                and isinstance(item.get("to"), str):
            mapping[item["from"]] = item["to"]
    return mapping


def _canon(mapping, key):
    seen = set()
    while key in mapping and key not in seen:
        seen.add(key)
        key = mapping[key]
    return key


def _accepted_lines(pilot):
    return [ln for ln in cap._registry_lines(pilot.acceptance_dir)
            if ln.get("kind") == "accepted"
            and isinstance(ln.get("acceptance_seq"), int)]


def _stamp_close_locked(pilot, trigger, event_at, close_seq=None):
    """Immutable per-trigger close stamp; the caller holds the boundary lock.
    Without an explicit close_seq it is the highest acceptance_seq recorded at
    this serialized moment (sequence order, never a timestamp guess)."""
    if close_seq is None:
        seqs = [ln["acceptance_seq"] for ln in _accepted_lines(pilot)]
        close_seq = max(seqs) if seqs else 0
    return _append_unique(
        pilot, {"schema": SCHEMA_STAMP, "trigger": trigger,
                "close_seq": close_seq, "event_at": event_at},
        _of(SCHEMA_STAMP, "trigger", trigger))


def _stamp_close(pilot, trigger, event_at, close_seq=None):
    with _locked(pilot.boundary_lock):
        return _stamp_close_locked(pilot, trigger, event_at, close_seq)


def _stamp_cap_now(pilot):
    """Client callback just before the first refused reservation is recorded."""
    _stamp_close(pilot, "cap", _now())


def _stamps(pilot):
    return [r for r in _records(pilot) if r.get("schema") == SCHEMA_STAMP]


def _stamped_close_seq(pilot):
    stamps = [s for s in _stamps(pilot) if _parse(s.get("event_at"))]
    if not stamps:
        return None
    return min(stamps, key=lambda s: _parse(s["event_at"]))["close_seq"]


def stamp_cap_close(pilot):
    """Fallback for a closed record whose stamp the refusal callback did not
    write (older store or callback failure): trigger (c) with the highest
    acceptance_seq whose acceptance is not later than the closed record. The
    exact path is ``_stamp_cap_now``; a stamp that exists is never replaced."""
    for rec in _read_jsonl(pilot.client_registry):
        if rec.get("schema") == jc.SCHEMA_CLOSED and \
                rec.get("cohort_id") == pilot.cohort_id:
            event = _parse(rec.get("at"))
            with _locked(pilot.boundary_lock):
                seqs = [ln["acceptance_seq"] for ln in _accepted_lines(pilot)
                        if event is not None
                        and _parse(ln.get("at")) is not None
                        and _parse(ln["at"]) <= event]
                return _stamp_close_locked(pilot, "cap", rec["at"],
                                           max(seqs) if seqs else 0)
    return None


# --------------------------------------------------------------------------- #
# Hook A: promotion (capture), detached query worker.                         #
# --------------------------------------------------------------------------- #


def _session_seen(pilot, session_id):
    for rec in _records(pilot):
        if rec.get("schema") == SCHEMA_STATE and \
                rec.get("session_id") == session_id:
            return True
    for line in cap._registry_lines(pilot.acceptance_dir):
        if session_id in (line.get("session_ids") or []):
            return True
    return False


def _state(pilot, session_id, state, reason=None, **extra):
    rec = {"schema": SCHEMA_STATE, "session_id": session_id, "state": state,
           "exclusion_reason": reason}
    rec.update(extra)
    return _append(pilot, rec)


def hook_promoted(session_id, repo):
    """Hook A. Returns a token ``{candidate_id, capture_id, session_id}`` for
    the winning candidate of a first promotion, else None. Never raises."""
    pilot = None
    try:
        pilot = load_pilot()
        if pilot is None:
            return None
        return _hook_promoted(pilot, session_id, repo)
    except Exception as exc:
        _log_error(pilot, "hook_promoted", exc)
        return None


def _hook_promoted(pilot, session_id, repo):
    if _session_seen(pilot, session_id):
        return None
    halted = halt_reason(pilot)
    if halted:
        _state(pilot, session_id, "excluded",
               "observer_suspended" if halted == "suspended" else halted)
        return None
    if _parse(_now()) >= pilot.deadline:
        _state(pilot, session_id, "outside_cohort_window",
               "outside_cohort_window")
        return None
    included = cap.check_inclusion(pilot.inclusion, session_id)
    entry = _entry_for(pilot, session_id)
    scope = pilot.activation["authorizations"]["data_scope"]["repositories"]
    in_scope = (entry.get("repository") in scope
                and entry.get("repo_root") == os.path.realpath(repo))
    if not included["included"] or not in_scope:
        failed = {"ok": False, "exclusion_reason": "not_in_inclusion_list",
                  "ticket_ref": None, "session_ids": [session_id]}
        with _locked(pilot.boundary_lock):
            cap.record_acceptance(pilot.acceptance_dir, failed, clock=_now)
        _state(pilot, session_id, "excluded", "not_in_inclusion_list",
               problems=[] if not included["included"]
               else ["repository_not_authorized"])
        return None
    ref = included["ticket_ref"]
    ref = min(ref, _aliases(pilot, "ticket").get(ref, ref))
    entry = _entry_for(pilot, session_id)
    started = time.monotonic()
    result = cap.capture_candidate(
        repo, entry.get("objective_text"), entry.get("requirement_text"),
        ref, [session_id], pilot.captures_dir,
        base_ref=entry.get("base_ref"), salt=pilot.activation["salt"],
        cohort_id=pilot.cohort_id, clock=_now, worktree_root=repo)
    elapsed = int(round((time.monotonic() - started) * 1000))
    record = result.get("record") or {}
    cid = record.get("candidate_id") or cap.candidate_id_for(ref)
    base = {"ticket_ref": ref, "ticket_key": ref, "candidate_id": cid,
            "capture_id": result.get("capture_id"), "worktree_root": repo,
            "instrumentation_ms": elapsed}
    # The acceptance event, the winner/window decision, the close stamp and
    # the state record happen as ONE serialized boundary across processes
    # (never held across a remote call), so concurrent sessions near the
    # limit are ordered by acceptance_seq alone.
    with _locked(pilot.boundary_lock):
        token = _accept_locked(pilot, session_id, result, base)
    if token is not None and not halt_reason(pilot):
        _start_worker(pilot, cid, result["capture_id"])
    return token


def _accept_locked(pilot, session_id, result, base):
    line = cap.record_acceptance(pilot.acceptance_dir, result, clock=_now)
    if line is None:
        _state(pilot, session_id, "excluded", "capture_error",
               problems=["acceptance_registry_write_failed"], **base)
        return None
    if not result.get("ok") or line.get("kind") != "accepted":
        _state(pilot, session_id, "excluded",
               result.get("exclusion_reason") or "capture_error",
               problems=[(result.get("error") or {}).get("reason")], **base)
        return None
    verified = cap.verify_capture(pilot.captures_dir, result["capture_id"],
                                  pilot.acceptance_dir)
    if not verified["consistent"]:
        _state(pilot, session_id, "excluded", "capture_verification_failed",
               problems=verified["problems"], **base)
        return None
    window = {"activated_at": pilot.activation["activated_at"],
              "close_seq": _stamped_close_seq(pilot)}
    picked = cap.pick_candidate_winners(_accepted_lines(pilot), window)
    state = picked["states"].get(line["acceptance_seq"])
    if state != "winner":
        _state(pilot, session_id, state or "outside_cohort_window",
               state or "outside_cohort_window",
               acceptance_seq=line["acceptance_seq"],
               accepted_at=line["accepted_at"], **base)
        return None
    _state(pilot, session_id, "captured", None,
           acceptance_seq=line["acceptance_seq"],
           accepted_at=line["accepted_at"], **base)
    if len(picked["winners"]) >= pilot.caps["jev"]["candidates_per_cohort"]:
        _stamp_close_locked(pilot, "count", line["accepted_at"],
                            close_seq=line["acceptance_seq"])
    return {"candidate_id": base["candidate_id"],
            "capture_id": result["capture_id"], "session_id": session_id}


def _start_worker(pilot, candidate_id, capture_id):
    def work():
        run_query(pilot, candidate_id, capture_id)
    starter = _OVERRIDES.get("thread_starter")
    if starter is not None:
        starter(work)
        return None
    thread = threading.Thread(target=work, daemon=True,
                              name="jev-observer-" + candidate_id)
    _WORKERS[candidate_id] = thread
    thread.start()
    return thread


def join_workers(timeout=None):
    """Join every in-process worker (tests and orderly shutdown only)."""
    for thread in list(_WORKERS.values()):
        thread.join(timeout)


def _credential_provider(pilot):
    provider = _OVERRIDES.get("credential_provider")
    if provider is not None:
        return provider
    name = pilot.activation["authorizations"]["credential_source_ref"]
    return lambda: os.environ.get(name)


def run_query(pilot, candidate_id, capture_id):
    """Query worker body. Never raises."""
    try:
        _run_query(pilot, candidate_id, capture_id)
    except Exception as exc:
        _log_error(pilot, "run_query", exc)


def _withheld_unit_ids(units):
    ids = []
    for unit in units:
        omission = unit.get("omission")
        if omission == "data_withheld" or (
                not unit.get("state_text") and omission != "context_too_large"
                and unit.get("applicable") is True):
            ids.append(unit["unit_id"])
    return ids


def _run_query(pilot, candidate_id, capture_id):
    os.makedirs(pilot.jev_dir, exist_ok=True)
    with _locked(pilot.cohort_lock) as held:
        if not held or halt_reason(pilot):
            return
        for rec in _records(pilot):
            if rec.get("candidate_id") == candidate_id and rec.get(
                    "schema") in (SCHEMA_RESULT, SCHEMA_ABANDONED):
                return
        record = cap.load_capture(pilot.captures_dir, capture_id)
        if not record:
            return
        units = record.get("units") or []
        client = jc.JevClient(
            _OVERRIDES.get("transport") or jc.default_transport,
            jc.FileStore(pilot.jev_dir), _credential_provider(pilot), _now,
            cap_usd=pilot.caps["jev"]["proposed_cap_usd"],
            max_units_queried=pilot.caps["jev"]["max_units_queried"],
            guard=lambda: guard_reason(pilot),
            on_refusal=lambda: _stamp_cap_now(pilot))
        result = client.query_candidate(
            candidate_id, units, pilot.activation,
            withheld_unit_ids=_withheld_unit_ids(units))
        _append(pilot, {"schema": SCHEMA_LINK, "candidate_id": candidate_id,
                        "capture_id": capture_id,
                        "unit_ids": [u["unit_id"] for u in units]})
        _append(pilot, {"schema": SCHEMA_RESULT, "candidate_id": candidate_id,
                        "capture_id": capture_id,
                        "status": result.get("status"),
                        "reason": result.get("reason"),
                        "cohort_closed": bool(result.get("cohort_closed")),
                        "units": result.get("units") or []})
        stamp_cap_close(pilot)


# --------------------------------------------------------------------------- #
# Hooks A2 and B: review start and sealed review.                             #
# --------------------------------------------------------------------------- #


def _review_fingerprint(pilot, token, repo):
    entry = _entry_for(pilot, token["session_id"])
    started = time.monotonic()
    value = cap.compute_raw_fingerprint(
        repo, entry.get("objective_text"), entry.get("requirement_text"),
        [token["session_id"]], repo)
    return value, int(round((time.monotonic() - started) * 1000))


def hook_review_start(token, repo):
    """Hook A2: fingerprint of what the review is about to read. Never raises."""
    pilot = None
    try:
        pilot = load_pilot()
        if pilot is None or not token:
            return None
        value, elapsed = _review_fingerprint(pilot, token, repo)
        return _append_unique(
            pilot, {"schema": SCHEMA_REVIEW_STARTED,
                    "candidate_id": token["candidate_id"],
                    "reviewed_fingerprint": value,
                    "instrumentation_ms": elapsed},
            _of(SCHEMA_REVIEW_STARTED, "candidate_id", token["candidate_id"]))
    except Exception as exc:
        _log_error(pilot, "hook_review_start", exc)
        return None


def _verdict_findings(verdict):
    if not isinstance(verdict, dict):
        return []
    typed = verdict.get("corrective_findings")
    items = typed if isinstance(typed, list) else (
        [] if str(verdict.get("verdict") or "") == "approve" else
        verdict.get("findings"))
    texts = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            text = item.get("summary") or item.get("text") or ""
        else:
            text = item if isinstance(item, str) else ""
        if text.strip():
            texts.append(text.strip())
    return texts


def comparator_status(captured, *reviewed):
    """verifiable iff every reviewed fingerprint equals the captured one;
    unverifiable_changed if a computed value differs; else unknown."""
    computed = [v for v in reviewed if v]
    if captured and any(v != captured for v in computed):
        return "unverifiable_changed"
    if not captured or len(computed) != len(reviewed):
        return "unverifiable_unknown"
    return "verifiable"


def hook_review_sealed(token, repo, verdict):
    """Hook B: seal the ordinary review record. Never raises; the first seal
    for a candidate wins."""
    pilot = None
    try:
        pilot = load_pilot()
        if pilot is None or not token:
            return None
        cid = token["candidate_id"]
        end, elapsed = _review_fingerprint(pilot, token, repo)
        record = cap.load_capture(pilot.captures_dir, token["capture_id"])
        started = next((r for r in _records(pilot) if r.get("schema")
                        == SCHEMA_REVIEW_STARTED
                        and r.get("candidate_id") == cid), {})
        status = comparator_status(
            (record or {}).get("raw_content_fingerprint"),
            started.get("reviewed_fingerprint"), end)
        verified = cap.verify_capture(pilot.captures_dir,
                                      token["capture_id"],
                                      pilot.acceptance_dir)
        if not verified["consistent"]:
            _state(pilot, token["session_id"], "excluded",
                   "capture_verification_failed", candidate_id=cid,
                   capture_id=token["capture_id"],
                   problems=verified["problems"])
        verdict_text = str((verdict or {}).get("verdict") or "")
        return _append_unique(
            pilot, {"schema": SCHEMA_ORDINARY, "candidate_id": cid,
                    "sealed_at": _now(), "reviewed_raw_fingerprint": end,
                    "reviewed_fingerprint_at_start": started.get(
                        "reviewed_fingerprint"),
                    "comparator_status": status, "verdict": verdict_text,
                    "findings": [{"text": t, "primary_symbol": None,
                                  "defect_class": None}
                                 for t in _verdict_findings(verdict)],
                    "instrumentation_ms": elapsed
                    + int(started.get("instrumentation_ms") or 0)},
            _of(SCHEMA_ORDINARY, "candidate_id", cid))
    except Exception as exc:
        _log_error(pilot, "hook_review_sealed", exc)
        return None


# --------------------------------------------------------------------------- #
# Suspension and recovery.                                                    #
# --------------------------------------------------------------------------- #


def suspend(pilot):
    with open(pilot.suspended_path, "a"):
        pass
    return True


def resume(pilot):
    """Clear the suspension marker, then recover. Never re-sends a query and
    never clears a hard stop (revocation): that stays in force."""
    try:
        os.remove(pilot.suspended_path)
    except FileNotFoundError:
        pass
    result = recover(pilot)
    if isinstance(result, dict) and halt_reason(pilot) == "hard_stop":
        result["hard_stop"] = True
    return result


def recover(pilot):
    """Resolve interrupted work without any send. Idempotent; skipped while a
    live attempt holds the cohort lock."""
    try:
        os.makedirs(pilot.jev_dir, exist_ok=True)
        with _locked(pilot.cohort_lock, blocking=False) as held:
            if not held:
                return {"skipped": "live_attempt"}
            recovered = jc.recover(jc.FileStore(pilot.jev_dir), _now,
                                   cohort_id=pilot.cohort_id)
            abandoned = []
            for cand in build_view(pilot)["candidates"]:
                if cand["excluded"] or cand["query_result"] or \
                        cand["abandoned"]:
                    continue
                worker = _WORKERS.get(cand["candidate_id"])
                if worker is not None and worker.is_alive():
                    continue
                _append(pilot, {"schema": SCHEMA_ABANDONED,
                                "candidate_id": cand["candidate_id"],
                                "capture_id": cand["capture_id"],
                                "reason": "query_interrupted"})
                abandoned.append(cand["candidate_id"])
            stamp_cap_close(pilot)
            return {"recovered_attempts": len(recovered),
                    "abandoned": abandoned}
    except Exception as exc:
        _log_error(pilot, "recover", exc)
        return {"error": type(exc).__name__}


# --------------------------------------------------------------------------- #
# Derived view of the registry (shared by selection, defects and reports).    #
# --------------------------------------------------------------------------- #


def _stamp_time(rec):
    return rec.get("at") or rec.get("started_at")


def _unit_view(unit, status_rec, final):
    base = {"unit_id": unit["unit_id"], "kind": unit.get("kind"),
            "rank": unit.get("rank"), "applicable": unit.get("applicable")}
    if status_rec is None and final:
        status_rec = {"status": NOT_QUERIED_INTERRUPTED, "queried": False,
                      "abstain_reason": None, "failure_class": None,
                      "reason": "query_interrupted", "sealed_at": None}
    if status_rec is None:
        base.update(status=None, queried=False, pending=True,
                    abstain_reason=None, failure_class=None, sealed_at=None)
        return base
    base.update(status=status_rec.get("status"),
                queried=bool(status_rec.get("queried")), pending=False,
                abstain_reason=status_rec.get("abstain_reason"),
                failure_class=status_rec.get("failure_class"),
                max_p=status_rec.get("max_p"),
                sealed_at=status_rec.get("sealed_at"))
    return base


def _rank_key(unit):
    try:
        return int(unit.get("rank") or "", 16)
    except ValueError:
        return 1 << 70


def build_view(pilot, as_of=None):
    """Structured, deterministic view of everything recorded up to ``as_of``
    (a UTC string; None means everything)."""
    cutoff = _parse(as_of) if as_of else None

    def upto(rec):
        if cutoff is None:
            return True
        when = _parse(_stamp_time(rec))
        return when is not None and when <= cutoff

    obs = [r for r in _records(pilot) if upto(r)]
    client = [r for r in _read_jsonl(pilot.client_registry)
              if r.get("cohort_id") == pilot.cohort_id and upto(r)]
    first = {}
    for rec in obs:
        schema = rec.get("schema")
        if schema in (SCHEMA_ORDINARY, SCHEMA_RESULT, SCHEMA_ABANDONED,
                      SCHEMA_KEYED, SCHEMA_SELECTION, SCHEMA_REVIEW_STARTED):
            first.setdefault((schema, rec.get("candidate_id")), rec)
    sessions = [r for r in obs if r.get("schema") == SCHEMA_STATE]
    exclusions = {}
    for rec in sessions:
        if rec["state"] == "excluded" and rec.get("capture_id"):
            exclusions.setdefault(rec["capture_id"], rec)
    interventions = {}
    for rec in obs:
        if rec.get("schema") == SCHEMA_INTERVENTION:
            interventions.setdefault(rec["candidate_id"], []).append(rec)

    outcomes_by_unit = {}
    attempts = {}
    for rec in client:
        schema = rec.get("schema")
        if schema == jc.SCHEMA_STARTED:
            attempts.setdefault(rec["attempt_id"], {})["started"] = rec
        elif schema == jc.SCHEMA_OUTCOME:
            attempts.setdefault(rec["attempt_id"], {})["outcome"] = rec
            outcomes_by_unit.setdefault(rec["unit_id"], rec["unit_result"])

    candidates = []
    for rec in sessions:
        if rec["state"] != "captured":
            continue
        cid = rec["candidate_id"]
        result = first.get((SCHEMA_RESULT, cid))
        abandoned = first.get((SCHEMA_ABANDONED, cid))
        final = result is not None or abandoned is not None
        statuses = {}
        for unit in (result or {}).get("units", []):
            statuses[unit["unit_id"]] = unit
        record = cap.load_capture(pilot.captures_dir, rec["capture_id"]) or {}
        units = []
        for unit in record.get("units") or []:
            status = outcomes_by_unit.get(unit["unit_id"],
                                          statuses.get(unit["unit_id"]))
            units.append(_unit_view(unit, status, final))
        ordinary = first.get((SCHEMA_ORDINARY, cid))
        events = interventions.get(cid, [])
        intervened = False
        for ev in events:
            when = _parse(ev.get("event_at"))
            sealed = _parse(ordinary["sealed_at"]) if ordinary else None
            if ordinary is None or (when is not None and sealed is not None
                                    and when < sealed):
                intervened = True
        excl = exclusions.get(rec["capture_id"])
        keyed = first.get((SCHEMA_KEYED, cid))
        candidates.append({
            "candidate_id": cid, "session_id": rec["session_id"],
            "ticket_key": rec.get("ticket_key"),
            "capture_id": rec["capture_id"],
            "acceptance_seq": rec["acceptance_seq"],
            "accepted_at": rec["accepted_at"],
            "excluded": excl.get("exclusion_reason") if excl else None,
            "excluded_problems": excl.get("problems") if excl else None,
            "intervened": intervened,
            "intervention_reasons": [e.get("reason") for e in events],
            "ordinary": ordinary,
            "comparator": ordinary["comparator_status"] if ordinary else None,
            "sealed_at": ordinary["sealed_at"] if ordinary else None,
            "findings": (keyed or {}).get("findings"),
            "findings_unkeyed": len((ordinary or {}).get("findings") or [])
            if keyed is None else 0,
            "query_result": result, "abandoned": abandoned, "final": final,
            "units": units, "worktree_root": rec.get("worktree_root"),
            "instrumentation_ms": int(rec.get("instrumentation_ms") or 0),
            "review_instrumentation_ms": int(
                (ordinary or {}).get("instrumentation_ms") or 0),
            "capture_record": record})
    candidates.sort(key=lambda c: c["acceptance_seq"])

    adjudications = {}
    for rec in obs:
        schema = rec.get("schema")
        if schema == SCHEMA_ADJ_STARTED:
            adjudications.setdefault(rec["unit_id"], {}).setdefault(
                "started", rec)
        elif schema == SCHEMA_ADJ_OUTCOME:
            adjudications.setdefault(rec["unit_id"], {}).setdefault(
                "outcome", rec)
    return {
        "sessions": sessions, "candidates": candidates,
        "by_id": {c["candidate_id"]: c for c in candidates},
        "stamps": [r for r in obs if r.get("schema") == SCHEMA_STAMP],
        "hard_stops": [r for r in obs if r.get("schema") == SCHEMA_HARD_STOP],
        "attempts": attempts, "adjudications": adjudications,
        "selections": {cid: rec for (schema, cid), rec in first.items()
                       if schema == SCHEMA_SELECTION},
        "client_closed": [r for r in client
                          if r.get("schema") == jc.SCHEMA_CLOSED],
        "errors": [r for r in obs if r.get("schema") == SCHEMA_ERROR]}


# --------------------------------------------------------------------------- #
# Coordinator records.                                                        #
# --------------------------------------------------------------------------- #


def key_ordinary_findings(pilot, candidate_id, keyed):
    """Key the sealed ordinary findings with (primary_symbol, defect_class).
    First keying wins; requires a sealed ordinary record. Returns bool."""
    if not isinstance(keyed, list):
        return False
    findings = []
    for item in keyed:
        if not (isinstance(item, dict)
                and isinstance(item.get("primary_symbol"), str)
                and item["primary_symbol"].strip()
                and item.get("defect_class") in DEFECT_CLASSES):
            return False
        findings.append({"text": item.get("text"),
                         "primary_symbol": item["primary_symbol"].strip(),
                         "defect_class": item["defect_class"]})
    if not any(r.get("schema") == SCHEMA_ORDINARY
               and r.get("candidate_id") == candidate_id
               for r in _records(pilot)):
        return False
    return _append_unique(
        pilot, {"schema": SCHEMA_KEYED, "candidate_id": candidate_id,
                "findings": findings},
        _of(SCHEMA_KEYED, "candidate_id", candidate_id)) is not None


def record_intervention(pilot, candidate_id, reason, at=None):
    at = at or _now()
    return _append(pilot, {"schema": SCHEMA_INTERVENTION,
                           "candidate_id": candidate_id, "reason": reason,
                           "event_at": at})


def record_hard_stop(pilot, kind, at=None):
    if kind not in HARD_STOP_KINDS:
        return None
    at = at or _now()
    # Serialized with acceptance: close_seq is the highest acceptance_seq
    # recorded at this boundary (inclusive), never a timestamp guess. The same
    # sequence is stamped so later acceptances land outside the window.
    with _locked(pilot.boundary_lock):
        seqs = [ln["acceptance_seq"] for ln in _accepted_lines(pilot)]
        close_seq = max(seqs) if seqs else 0
        rec = _append(pilot, {"schema": SCHEMA_HARD_STOP, "kind": kind,
                              "event_at": at, "close_seq": close_seq})
        _stamp_close_locked(pilot, "hard_stop", at, close_seq)
    return rec


# --------------------------------------------------------------------------- #
# Adjudication: selection, brief, attempt rule, intake.                       #
# --------------------------------------------------------------------------- #


def _eligible_for_selection(cand, close_seq):
    return (not cand["excluded"] and not cand["intervened"]
            and cand["ordinary"] is not None and cand["final"]
            and (close_seq is None or cand["acceptance_seq"] <= close_seq))


def _select_locked(pilot, view):
    close_seq = _stamped_close_seq(pilot)
    units_cap = pilot.caps["adjudication"]["units_cap"]
    selected_total = sum(len(rec.get("selected") or [])
                         for rec in view["selections"].values())
    out = []
    for cand in view["candidates"]:
        cid = cand["candidate_id"]
        stored = view["selections"].get(cid)
        if stored is not None:
            out.append(stored)
            continue
        if not _eligible_for_selection(cand, close_seq):
            out.append({"candidate_id": cid, "final": cand["final"],
                        "sealed": cand["ordinary"] is not None,
                        "pending": True, "selected": []})
            continue
        picks, empty = [], []
        for stratum in ("alert", "no_alert"):
            pool = sorted((u for u in cand["units"]
                           if u["status"] == stratum), key=_rank_key)
            if pool:
                picks.append(pool[0])
            else:
                empty.append(stratum)
        picks.sort(key=_rank_key)
        selected, not_selected = [], []
        for unit in picks:
            if selected_total < units_cap:
                selected.append(unit["unit_id"])
                selected_total += 1
            else:
                not_selected.append(unit["unit_id"])
        rec = {"schema": SCHEMA_SELECTION, "candidate_id": cid,
               "selected": selected, "stratum_empty": empty,
               "not_selected": not_selected}
        written = _append_unique(pilot, rec,
                                 _of(SCHEMA_SELECTION, "candidate_id", cid))
        out.append(written or rec)
    return out


def select_adjudication(pilot):
    """Selection per protocol s5. A candidate is only selected once its
    ordinary review is sealed and its unit statuses are final; the first
    selection is persisted and never recomputed."""
    with _locked(pilot.adjudication_lock):
        return _select_locked(pilot, build_view(pilot))


def _find_unit(view, unit_id):
    for cand in view["candidates"]:
        for unit in cand["units"]:
            if unit["unit_id"] == unit_id:
                return cand, unit
    return None, None


def _consumed(view):
    totals = {"tokens": 0, "tool_calls": 0, "minutes": 0}
    for unit_id, adj in view["adjudications"].items():
        if "started" not in adj:
            continue
        usage = None
        out = adj.get("outcome")
        if out and out.get("charge_status") == "known":
            usage = out.get("usage")
        for key in totals:
            totals[key] += usage[key] if usage else ADJ_ENVELOPE[key]
    return totals


def begin_adjudication(pilot, unit_id):
    """Reserve the per-unit envelope and write the write-ahead started record.
    Idempotent per unit; refused before a sealed ordinary record, for a unit
    that was not selected, or when the reservation would exceed a cap."""
    blocked = guard_reason(pilot)
    if blocked:
        return {"state": "refused", "reason": blocked}
    with _locked(pilot.adjudication_lock):
        view = build_view(pilot)
        cand, unit = _find_unit(view, unit_id)
        if cand is None:
            return {"state": "refused", "reason": "unknown_unit"}
        if cand["ordinary"] is None:
            return {"state": "refused", "reason": "no_ordinary_record"}
        if cand["excluded"] or cand["intervened"]:
            return {"state": "refused", "reason": "candidate_not_eligible"}
        existing = view["adjudications"].get(unit_id, {}).get("started")
        if existing is not None:
            return {"state": "already_started", "started": existing}
        selection = next((s for s in _select_locked(pilot, view)
                          if s["candidate_id"] == cand["candidate_id"]), {})
        if unit_id not in (selection.get("selected") or []):
            return {"state": "refused", "reason": "not_selected"}
        caps = pilot.caps["adjudication"]["cohort_caps"]
        used = _consumed(view)
        limits = {"tokens": caps["tokens"], "tool_calls": caps["tool_calls"],
                  "minutes": caps["wall_minutes"]}
        if any(used[k] + ADJ_ENVELOPE[k] > limits[k] for k in limits):
            _stamp_close(pilot, "adjudication_cap", _now())
            return {"state": "refused", "reason": "cap_reached"}
        order = selection["selected"].index(unit_id) + 1
        started = _append(pilot, {
            "schema": SCHEMA_ADJ_STARTED, "unit_id": unit_id,
            "candidate_id": cand["candidate_id"],
            "reserved": dict(ADJ_ENVELOPE), "order_position": order})
        return {"state": "started", "started": started,
                "order_position": order}


def _valid_adjudication(record, unit_id):
    problems = []
    if not isinstance(record, dict):
        return ["not_an_object"]
    if record.get("schema") != SCHEMA_ADJUDICATION:
        problems.append("schema")
    if record.get("unit_id") != unit_id:
        problems.append("unit_id")
    adjudicator = record.get("adjudicator")
    if not (isinstance(adjudicator, dict) and adjudicator.get("model_id")
            and adjudicator.get("session_ref")):
        problems.append("adjudicator")
    outcome = record.get("outcome")
    if outcome not in OUTCOMES:
        problems.append("outcome")
    usage = record.get("usage")
    if not (isinstance(usage, dict) and all(
            _num(usage.get(k)) and 0 <= usage[k] <= ADJ_ENVELOPE[k]
            for k in ADJ_ENVELOPE)):
        problems.append("usage")
    evidence = record.get("evidence")

    def good_evidence():
        return (isinstance(evidence, list) and evidence and all(
            isinstance(e, dict) and e.get("capture_ref") and e.get("rel_path")
            and e.get("explanation") for e in evidence))

    if outcome in ("confirmed", "refuted") and not good_evidence():
        problems.append("evidence")
    if outcome == "confirmed":
        trigger = record.get("trigger")
        if not (isinstance(trigger, dict) and all(
                isinstance(trigger.get(k), str) and trigger[k].strip()
                for k in ("input", "expected", "actual"))):
            problems.append("trigger")
        if not (isinstance(record.get("primary_symbol"), str)
                and record["primary_symbol"].strip()):
            problems.append("primary_symbol")
        if record.get("defect_class") not in DEFECT_CLASSES:
            problems.append("defect_class")
        if record.get("origin") not in ORIGINS:
            problems.append("origin")
    return problems


def _adjudication_cost(pilot, unit_id, known, usage):
    return {"schema": jc.SCHEMA_COST, "cost_class": "adjudication",
            "unit_id": unit_id, "attempt_id": None,
            "reserved_tokens": ADJ_ENVELOPE["tokens"],
            "charge_status": "known" if known else "unknown",
            "input_tokens": usage["tokens"] if known else None,
            "output_tokens": None, "usd": None,
            "unknown_reason": None if known else "interrupted_unknown"}


def submit_adjudication(pilot, record):
    """Validate and record an adjudication outcome. The first outcome for a
    unit is immutable; an invalid record is rejected and leaves the run
    started (and so unknown on recovery)."""
    unit_id = record.get("unit_id") if isinstance(record, dict) else None
    problems = _valid_adjudication(record, unit_id)
    if problems:
        return {"accepted": False, "problems": problems}
    with _locked(pilot.adjudication_lock):
        view = build_view(pilot)
        adj = view["adjudications"].get(unit_id, {})
        if "started" not in adj:
            return {"accepted": False, "problems": ["not_started"]}
        if "outcome" in adj:
            return {"accepted": False, "problems": ["duplicate_outcome"]}
        cand, _unit = _find_unit(view, unit_id)
        _append(pilot, {"schema": SCHEMA_ADJ_OUTCOME, "unit_id": unit_id,
                        "candidate_id": cand["candidate_id"],
                        "outcome": record["outcome"], "adjudication": record,
                        "usage": record["usage"], "charge_status": "known",
                        "recovered": False,
                        "cost": _adjudication_cost(pilot, unit_id, True,
                                                   record["usage"])})
        return {"accepted": True, "problems": []}


def recover_adjudications(pilot):
    """started-without-outcome becomes unknown: usage null, reservation kept
    consumed. Never restarts a run. Coordinator-invoked."""
    recovered = []
    with _locked(pilot.adjudication_lock):
        view = build_view(pilot)
        for unit_id, adj in sorted(view["adjudications"].items()):
            if "started" in adj and "outcome" not in adj:
                _append(pilot, {
                    "schema": SCHEMA_ADJ_OUTCOME, "unit_id": unit_id,
                    "candidate_id": adj["started"]["candidate_id"],
                    "outcome": "unknown", "adjudication": None, "usage": None,
                    "charge_status": "unknown", "recovered": True,
                    "cost": _adjudication_cost(pilot, unit_id, False, None)})
                recovered.append(unit_id)
    return recovered


# --- blind brief ------------------------------------------------------------


def _scrub_strings(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "<redacted>")
        return value
    if isinstance(value, dict):
        return {k: _scrub_strings(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_strings(v, secrets) for v in value]
    return value


_STATUS_WORDS = re.compile(r"\b(alert|no[-_ ]alert)\b", re.IGNORECASE)


def _observer_authored(brief):
    capture_ref = brief["capture_ref"]
    return {
        "brief_version": brief["brief_version"],
        "order_position": brief["order_position"],
        "envelope": brief["envelope"],
        "kind": brief["unit"]["kind"],
        "family_questions": brief["family_questions"],
        "base_evidence": capture_ref["base_evidence"],
        "base_digest": capture_ref["base_digest"],
        "file_status": [[f.get("status"), f.get("redacted")]
                        for f in capture_ref["files"]],
        "withheld_files": len(capture_ref["withheld_files"]),
        "redaction_rules": [r.get("rule_id")
                            for r in capture_ref["redactions"]],
        "origin_note": capture_ref["origin_note"]}


def scan_brief(brief, forbidden_everywhere, forbidden_observer):
    """Problems found in a brief; empty means clean."""
    problems = []
    whole = json.dumps(brief, sort_keys=True, ensure_ascii=False)
    for label, values in forbidden_everywhere.items():
        if any(v and v in whole for v in values):
            problems.append(label)
    observer = json.dumps(_observer_authored(brief), sort_keys=True,
                          ensure_ascii=False)
    if _STATUS_WORDS.search(observer):
        problems.append("status_word")
    for label, values in forbidden_observer.items():
        for value in values:
            if value and re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])"
                                   % re.escape(value), observer):
                problems.append(label)
                break
    return problems


def build_brief(pilot, candidate_id, unit_id, order_position):
    """Blind adjudication brief from a verified, sanitized capture only.
    Returns ``{"brief": ...}`` or ``{"refused": reason}``."""
    view = build_view(pilot)
    cand = view["by_id"].get(candidate_id)
    if cand is None:
        return {"refused": "unknown_candidate"}
    verified = cap.verify_capture(pilot.captures_dir, cand["capture_id"],
                                  pilot.acceptance_dir)
    if not verified["consistent"]:
        _append_unique(
            pilot, {"schema": SCHEMA_STATE, "session_id": cand["session_id"],
                    "state": "excluded", "candidate_id": candidate_id,
                    "capture_id": cand["capture_id"],
                    "exclusion_reason": "capture_verification_failed",
                    "problems": verified["problems"]},
            lambda r: r.get("capture_id") == cand["capture_id"]
            and r.get("state") == "excluded")
        return {"refused": "capture_verification_failed",
                "problems": verified["problems"]}
    record = cap.load_capture(pilot.captures_dir, cand["capture_id"])
    unit = next((u for u in (record or {}).get("units") or []
                 if u["unit_id"] == unit_id), None)
    if unit is None or unit.get("kind") not in jc.QUESTION_IDS_BY_KIND:
        return {"refused": "unknown_unit"}
    cview = cap.capture_content_view(record)
    files = [{k: v for k, v in f.items() if k != "sha256"}
             for f in cview["files"]]
    base_entries = [{k: v for k, v in f.items() if k != "sha256"}
                    for f in cview["base_files"]]
    file_texts, withheld = {}, []
    for f in files:
        rel = f.get("rel_path")
        if rel is None:
            withheld.append(f.get("rel_label"))
            continue
        text = cap.read_capture_file_redacted(
            pilot.captures_dir, cand["capture_id"], rel)
        if text is None:
            withheld.append(rel)
        else:
            file_texts[rel] = text
    base_texts, base_missing = {}, False
    for f in base_entries:
        rel = f.get("rel_path")
        text = None if rel is None else cap.read_capture_base_file(
            pilot.captures_dir, cand["capture_id"], rel)
        if text is None:
            base_missing = True
        else:
            base_texts[rel] = text
    if cview.get("base_omitted"):
        base_evidence = "missing"
    elif base_missing:
        base_evidence = "partial"
    else:
        base_evidence = "present"
    origin_note = ("Base evidence is %s. When the base cannot show whether "
                   "the defect pre-exists, answer origin unknown."
                   % base_evidence)
    questions = [jc._QUESTION_BY_ID[q]["instructions"]
                 for q in jc.QUESTION_IDS_BY_KIND[unit["kind"]]
                 if q != "Q-SUF"]
    brief = {
        "brief_version": BRIEF_VERSION,
        "unit": {"kind": unit["kind"], "statement": unit["statement"]},
        "ticket": {"objective_text": cview["objective_text"],
                   "requirement_text": cview["requirement_text"]},
        "capture_ref": {
            "schema": cview["schema"], "files": files,
            "file_texts": file_texts, "withheld_files": withheld,
            "diff": cview["diff"], "redactions": cview["redactions"],
            "base_digest": cview["base_digest"],
            "base_files": base_entries, "base_texts": base_texts,
            "base_redactions": cview["base_redactions"],
            "base_omitted": cview["base_omitted"],
            "base_evidence": base_evidence, "origin_note": origin_note},
        "family_questions": questions,
        "order_position": order_position,
        "envelope": {"max_tool_calls": ADJ_ENVELOPE["tool_calls"],
                     "max_minutes": ADJ_ENVELOPE["minutes"],
                     "max_total_tokens": ADJ_ENVELOPE["tokens"]}}
    roots = [r for r in {cand.get("worktree_root"),
                         os.path.realpath(cand["worktree_root"])
                         if cand.get("worktree_root") else None} if r]
    sessions = [s for s in record.get("session_ids") or [] if s]
    brief = _scrub_strings(brief, sorted(set(roots + sessions), key=len,
                                         reverse=True))
    hashes = [record.get("raw_content_fingerprint"),
              record.get("base_raw_fingerprint")]
    hashes += [f.get("sha256") for f in record.get("files") or []]
    hashes += [f.get("sha256") for f in record.get("base_files") or []]
    ordinary = cand["ordinary"] or {}
    problems = scan_brief(
        brief,
        {"raw_fingerprint": [h for h in hashes if h],
         "session_id": sessions, "worktree_root": roots},
        {"rank": [u.get("rank") for u in record.get("units") or []],
         "ordinary_verdict": [ordinary.get("verdict")] + [
             f.get("text") for f in ordinary.get("findings") or []]})
    if problems:
        return {"refused": "brief_scan_failed", "problems": sorted(set(
            problems))}
    return {"brief": brief}


# --- defect records ---------------------------------------------------------


def build_defect_records(pilot, view=None):
    """defect_record.v1 for every confirmed adjudication. The same defect_key
    counts once and keeps unit_links to every adjudicated unit that surfaced
    it; alias-log merges are applied before anything is counted."""
    view = view or build_view(pilot)
    alias = _aliases(pilot, "defect")
    groups = {}
    for cand in view["candidates"]:
        if cand["excluded"]:
            continue
        for unit in cand["units"]:
            out = view["adjudications"].get(unit["unit_id"], {}).get("outcome")
            if not out or out.get("outcome") != "confirmed":
                continue
            adj = out["adjudication"]
            key = _canon(alias, "%s|%s|%s" % (
                cand["candidate_id"], adj["primary_symbol"],
                adj["defect_class"]))
            group = groups.setdefault(key, {"cand": cand, "units": [],
                                            "origins": set()})
            group["units"].append(unit)
            group["origins"].add(adj["origin"])
    records = []
    for key in sorted(groups):
        group = groups[key]
        cand = group["cand"]
        matched = any(
            _canon(alias, "%s|%s|%s" % (cand["candidate_id"],
                                        f["primary_symbol"],
                                        f["defect_class"])) == key
            for f in cand["findings"] or [])
        origin = next(iter(group["origins"])) \
            if len(group["origins"]) == 1 else "unknown"
        detected = any(u["status"] == "alert" for u in group["units"])
        records.append({
            "schema": SCHEMA_DEFECT, "defect_key": key,
            "candidate_id": cand["candidate_id"], "origin": origin,
            "unit_links": sorted(u["unit_id"] for u in group["units"]),
            "detected_by_jev": detected, "matched_ordinary": matched,
            "additional_signal": bool(
                not matched and cand["comparator"] == "verifiable"
                and not cand["intervened"])})
    return records
