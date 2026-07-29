#!/usr/bin/env python3
"""D2: evaluation isolation, the scoring policy, and the durable queue.

Three problems are solved together here, because they are the same problem.

1. SCORING CONTAMINATED THE WORK. Evaluation used to run as an extra turn on the
   role's OWN session, so the thing being measured and the thing measuring it
   shared a context: an agent's later work was influenced by having just scored
   its reviewer. Evaluation now runs in an isolated session that has never
   touched the work and can only read the files it was handed.

2. SCORING SAT IN THE CRITICAL PATH. A round waited for its own evaluation
   before the fix could be handed back. Evaluation is now SEALED AND ENQUEUED
   the instant a verdict is valid, and DRAINED at phase end — the correction
   handoff never waits, so measurement is passive in wall-clock terms and not
   merely in ordering.

3. SCORES WERE BOUND TO EVIDENCE THAT DID NOT EXIST YET (CV-008). Sealing
   happens after the verdict is written and validated, and the seal is
   re-verified at drain time. Evidence that changed while the entry sat in the
   queue marks the score `unverifiable` rather than being re-hashed to whatever
   it says now — re-hashing would make every score verifiable by construction.

THE COST OF DEFERRAL is bounded by making the queue durable: it is written
before the round continues, an entry is marked drained only after its scores are
aggregated, and anything a killed process left pending is drained at the next
start of that session. A session killed mid-phase leaves rounds QUEUED, not
lost, and pending entries are reported as pending rather than silently dropped.

Every failure in this module degrades the MEASUREMENT and never the run.

Python 3.9+, stdlib only.
"""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_handoff as handoff  # noqa: E402
import cowork_state as state_store  # noqa: E402

POLICIES = state_store.EVALUATION_POLICIES
DEFAULT_POLICY = state_store.DEFAULT_EVALUATION_POLICY

# The deterministic `sampled` rule (P7). Eligible rounds 1, 3, 5, ... are
# selected. Deterministic rather than random so a run is reproducible and so a
# four-round fixture yields exactly 2 selected and 2 skipped — both strictly
# between zero and all, which is what makes "sampled" testable at all.
SAMPLED_RULE = "every_other_eligible_round_from_first"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def decide(policy, round_index, is_final_round=False):
    """Whether this round gets scored, and WHY.

    Returns `{"selected", "rule", "reason", "policy", "round"}`. The reason is
    recorded per round so the record can show what a lower policy actually
    skipped — a policy whose savings are invisible is not an informed choice.
    """
    policy = policy if policy in POLICIES else DEFAULT_POLICY
    out = {"policy": policy, "round": round_index, "rule": policy,
           "selected": False, "reason": ""}
    if policy == "off":
        out["reason"] = "evaluation disabled by policy"
    elif policy == "all_rounds":
        out["selected"] = True
        out["reason"] = "every round is scored under all_rounds"
    elif policy == "final_round":
        # A round cannot know it is the last one WHILE it is happening — only
        # the phase end knows that. Enqueuing only when `is_final_round` is
        # already true therefore selected nothing at all in a live run, because
        # nothing upstream can supply that flag mid-phase.
        #
        # So every round is enqueued as a CANDIDATE and each new candidate
        # supersedes the previous one; at drain time only the surviving
        # candidate per phase is scored. The selection is still `final_round`,
        # it is just resolved when finality is actually knowable.
        out["selected"] = True
        out["candidate"] = True
        out["supersedes_earlier_candidates"] = True
        out["reason"] = ("enqueued as the phase's final-round candidate; "
                         "superseded by any later round in the same phase")
    elif policy == "sampled":
        out["rule"] = SAMPLED_RULE
        try:
            index = int(round_index)
        except (TypeError, ValueError):
            index = 1
        out["selected"] = (index % 2) == 1
        out["reason"] = ("odd eligible round selected by %s" % SAMPLED_RULE
                         if out["selected"]
                         else "even eligible round skipped by %s"
                              % SAMPLED_RULE)
    return out


# --------------------------------------------------------------------------- #
# Sealing and enqueueing.                                                     #
# --------------------------------------------------------------------------- #


def seal_round(artifacts, validate=None, context=None):
    """Seal one round's evidence chain AFTER its verdict is written.

    The chain deliberately includes the PRIOR round's verdict and artifact
    revision (P6). Without them, criteria like "responsiveness to feedback" have
    nothing to be responsive TO, so every round would score `not_applicable` and
    the comparison the whole project exists for would be gutted. With them,
    round 1 is correctly `not_applicable` (there is no prior feedback) while
    later rounds are genuinely observable.

    Path-first, never embedded: the envelope carries paths and digests, and the
    isolated evaluator reads the files itself.
    """
    return handoff.seal_envelope(artifacts, validate=validate, context=context)


def enqueue(queue_path, entry):
    """Append one self-contained entry to the durable queue.

    Self-contained is the requirement: a later process — possibly after a crash,
    with none of this one's memory — must be able to drain it. The entry
    therefore carries the sealed envelope, the evaluator/evaluatee pair, the
    criteria, the phase and round, the policy decision that selected it, and the
    identity snapshot.
    """
    if not queue_path or not isinstance(entry, dict):
        return False
    record = dict(entry)
    record.setdefault("queued_at", _utc_now())
    record.setdefault("state", "pending")
    try:
        line = json.dumps(record, sort_keys=True)
    except (TypeError, ValueError):
        return False
    try:
        dirname = os.path.dirname(queue_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(queue_path, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True


def read_queue(queue_path):
    """Every queue record, oldest first. Tolerant of a partially-written tail."""
    if not queue_path or not os.path.exists(queue_path):
        return []
    out = []
    try:
        with open(queue_path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return out
    return out


def _partition(entries, phase_closed=None):
    """Split pending entries into (score now, retire, hold until phase end).

    A `final_round` candidate may only be resolved once its phase is CLOSED.
    Resolving it at any drain looked correct while every drain happened at phase
    end — but a drain also runs at the next session start, so a process that
    died after round 1 would find round 1 as the only candidate, score it as
    "final", and then score round 2 as well when the phase really ended. Two
    rounds scored, one of them not final, and the policy quietly became
    `all_rounds` for that session.

    `phase_closed(phase) -> bool` says whether a phase is over. When it is not
    known (a recovery drain at session start), candidates are HELD pending
    rather than resolved — deferring costs a delay; resolving early costs
    correctness.
    """
    scoreable, retire, held = [], [], []
    candidates = []
    for entry in entries or []:
        decision = entry.get("policy_decision") or {}
        if decision.get("supersedes_earlier_candidates"):
            candidates.append(entry)
        else:
            scoreable.append(entry)
    best = {}
    for entry in candidates:
        phase = entry.get("phase")
        if phase_closed is not None and not phase_closed(phase):
            # Phase still open: this candidate may yet be superseded.
            held.append(entry)
            continue
        if phase_closed is None:
            # No finality signal at all — the safe reading is "not yet".
            held.append(entry)
            continue
        key = (phase, entry.get("evaluator_seat"))
        try:
            round_index = int(entry.get("round") or 0)
        except (TypeError, ValueError):
            round_index = 0
        current = best.get(key)
        if current is None:
            best[key] = (round_index, entry)
        elif round_index >= current[0]:
            retire.append(current[1])
            best[key] = (round_index, entry)
        else:
            retire.append(entry)
    scoreable.extend(entry for _, entry in best.values())
    return scoreable, retire, held


def resolve_candidates(entries, phase_closed=None):
    """The entries a drain would score, given a phase-finality signal."""
    return _partition(entries, phase_closed)[0]
def superseded_candidates(entries, phase_closed=None):
    """The candidates a drain retires — reported, not hidden."""
    return _partition(entries, phase_closed)[1]


def held_candidates(entries, phase_closed=None):
    """Candidates deliberately left pending because their phase is still open
    (or its finality is unknown). Reported as pending, never as scored."""
    return _partition(entries, phase_closed)[2]


def pending_entries(queue_path):
    """Entries still awaiting scoring.

    The queue is append-only, so a drain is recorded by appending a `drained`
    marker naming the entry rather than by rewriting it. An entry is pending
    when no such marker exists — which is exactly the state a killed process
    leaves behind, and exactly what the next start picks up.
    """
    records = read_queue(queue_path)
    drained = {rec.get("entry_id") for rec in records
               if rec.get("state") in ("drained", "failed_permanent")}
    seen = {}
    for rec in records:
        entry_id = rec.get("entry_id")
        if not entry_id or rec.get("state") != "pending":
            continue
        if entry_id in drained or entry_id in seen:
            continue
        seen[entry_id] = rec
    return list(seen.values())


def mark_drained(queue_path, entry_id, state="drained", detail=None):
    """Record that an entry was drained (or permanently failed).

    An entry that fails to drain is NOT marked: it stays pending and is reported
    as pending, so a transient failure means "scored later", not "silently
    dropped". Only a permanent failure gets a marker, and it says so.
    """
    if not (queue_path and entry_id):
        return False
    marker = {"entry_id": entry_id, "state": state,
              "marked_at": _utc_now()}
    if detail:
        marker["detail"] = detail
    try:
        with open(queue_path, "a") as fh:
            fh.write(json.dumps(marker, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


def drain(queue_path, score_fn, verify_fn=None, phase_closed=None):
    """Drain every pending entry through `score_fn`.

    `score_fn(entry, verification) -> bool` runs the isolated evaluator and
    aggregates its scores; it returns True when the entry is done with.
    `verify_fn(envelope) -> dict` re-checks the seal and defaults to
    `cowork_handoff.verify_envelope`.

    THE SEAL IS RE-VERIFIED BEFORE SCORING COUNTS. An entry whose evidence
    changed while it waited is passed to `score_fn` with
    `verification['state'] == 'changed'`, which marks the score `unverifiable`
    and excludes it from aggregates. The longer the deferral, the more
    load-bearing this check is — which is why it is here rather than assumed.

    Returns a summary; never raises.
    """
    verify_fn = verify_fn or handoff.verify_envelope
    summary = {"drained": 0, "failed": 0, "unverifiable": 0, "pending": 0,
               "superseded": 0, "held": 0, "state": "ok"}
    try:
        pending = pending_entries(queue_path)
        entries, dropped, held = _partition(pending, phase_closed)
    except Exception:  # noqa: BLE001
        summary["state"] = "failed"
        return summary
    summary["held"] = len(held)
    for entry in dropped:
        # A superseded final-round candidate is retired explicitly, so it is
        # neither scored nor left pending forever. Only ever reached once the
        # phase is CLOSED, so it can no longer be superseded by a later round.
        mark_drained(queue_path, entry.get("entry_id"), state="drained",
                     detail="superseded by a later round in the same phase")
        summary["superseded"] += 1
    summary["pending"] += len(held)
    for entry in entries:
        try:
            envelope = entry.get("envelope")
            verification = verify_fn(envelope) if envelope else {
                "state": "unknown", "changed": []}
            if verification.get("state") == "changed":
                summary["unverifiable"] += 1
            ok = bool(score_fn(entry, verification))
        except Exception:  # noqa: BLE001 - a bad entry never breaks the run
            ok = False
        if ok:
            mark_drained(queue_path, entry.get("entry_id"))
            summary["drained"] += 1
        else:
            # Left pending on purpose: the next drain retries it, and until then
            # the record reports it as pending rather than as scored.
            summary["failed"] += 1
            summary["pending"] += 1
    return summary


# --------------------------------------------------------------------------- #
# Isolation.                                                                  #
# --------------------------------------------------------------------------- #


def evaluator_identity(identities, role):
    """The controller/model an isolated evaluator must run on for `role`'s seat.

    The evaluator KEEPS the role's own controller and model (P5). The project
    exists to compare Claude against Codex; collapsing every evaluation onto one
    controller would make this run's scores incomparable with the 62 evaluations
    already recorded in the two sessions on disk.
    """
    identity = (identities or {}).get(role)
    if not isinstance(identity, dict):
        return {"tool": None, "model": None, "state": "unknown"}
    return {
        "tool": identity.get("tool") or identity.get("controller"),
        "model": identity.get("model"),
        "effort": identity.get("effort"),
        "state": "ok" if identity.get("tool") else "unknown",
    }


def isolation_violations(trace_events, operational_session_ids):
    """Find evaluation turns that ran on an OPERATIONAL role's session.

    The invariant is the whole point of D2: an evaluation turn must never share
    a controller session with the work it scores. Asserted against the trace
    rather than trusted, because the failure is silent — a contaminated score
    looks exactly like a clean one.
    """
    violations = []
    operational = set(operational_session_ids or ())
    for event in trace_events or []:
        if event.get("work_class") != "evaluation":
            continue
        identity = event.get("identity") or {}
        session_id = identity.get("controller_session_id")
        if session_id and session_id in operational:
            violations.append({
                "work_id": event.get("work_id"),
                "controller_session_id": session_id,
                "role": event.get("role"),
            })
    return violations


def ordering_violations(trace_events):
    """Find rounds whose evaluation turn started BEFORE the correction handoff
    was recorded.

    Criterion 4's ordering invariant: scoring must never sit between a
    reviewer's verdict and the fix going back to the role. A violation here
    means measurement delayed the work, which is the one thing a passive
    measurement system may not do.
    """
    handoffs = {}
    violations = []
    for event in trace_events or []:
        name = event.get("event")
        key = (event.get("phase"), event.get("round"))
        if name == "review.handoff.recorded":
            handoffs.setdefault(key, event.get("ts"))
        elif name == "eval.turn.start":
            recorded = handoffs.get(key)
            if recorded is None or (event.get("ts") or "") < recorded:
                violations.append({
                    "phase": key[0], "round": key[1],
                    "eval_started_at": event.get("ts"),
                    "handoff_recorded_at": recorded,
                })
    return violations
