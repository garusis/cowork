#!/usr/bin/env python3
"""The SOLE writer of `ledger.jsonl` and the SOLE minter of stable IDs (P3).

Everything cowork makes a durable claim about — a finding, a decision, a human
amendment, an escaped defect, a verification attempt — gets its identity here
and nowhere else. That single rule is what makes the history checkable: an
evaluator cannot invent an `F-0007` that never existed, renumber the findings so
an old one reads as new, or resurrect one that was withdrawn, because it never
holds the pen.

APPEND-ONLY. A later record may ADD or SUPERSEDE; it may never rewrite or
delete. A read returns the FULL history including superseded and withdrawn
records, because "this finding was withdrawn" is itself a fact worth keeping —
dropping it would make a round that correctly retracted a false finding
indistinguishable from one that never found anything.

WHO WRITES WHAT, AND WHEN:

- Findings, decisions, amendments and escapes are appended LIVE by the
  orchestrator as they happen. Their identity across rounds cannot be
  reconstructed afterwards — "is this the same finding as last round?" is only
  answerable while both are in hand.
- Verification attempts arrive later, via `reconcile_attempts`, because they are
  DERIVED from controller logs rather than from anything cowork does. Ingestion
  (cowork_ingest) produces id-free observations; this is the one place they
  become identified, durable records.

BEST-EFFORT. Both writers swallow their own failures and leave the ledger
untouched rather than half-written. A ledger is measurement: it must never break
a run.

Python 3.9+, stdlib only.
"""

import datetime
import json
import os

# Record kinds and their id prefixes. The prefix is part of the contract: an id
# names what kind of thing it identifies, so a citation can be checked for shape
# before it is looked up.
KINDS = {
    "finding": "F",
    "decision": "D",
    "amendment": "A",
    "escape": "E",
    "attempt": "V",
}

# Record states. `superseded` and `withdrawn` are terminal for the record but
# NOT deletions — the record stays readable, tagged with what replaced it.
STATES = ("open", "superseded", "withdrawn", "closed")


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def read_ledger(path):
    """The full ledger as a list of records, oldest first.

    Tolerant: a missing file is an empty ledger, and an unparseable line is
    skipped rather than raised — the ledger is append-only and may be read while
    it is being written.
    """
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", errors="replace") as fh:
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


def next_id(records, kind):
    """The next id for `kind`, derived from what is already on disk.

    Derived rather than counted in memory, so two processes appending to the
    same session (a drain on the next start, say) cannot both mint `V-0001`.
    """
    prefix = KINDS.get(kind)
    if not prefix:
        raise ValueError("unknown ledger kind %r" % (kind,))
    highest = 0
    for rec in records or []:
        rid = rec.get("id")
        if not isinstance(rid, str) or not rid.startswith(prefix + "-"):
            continue
        try:
            highest = max(highest, int(rid.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return "%s-%04d" % (prefix, highest + 1)


def _append(path, records):
    """Append records to the ledger. Returns True on success.

    All-or-nothing per call: the batch is serialized first, so a record that
    cannot be encoded aborts the write instead of leaving a partial batch.
    """
    if not path or not records:
        return False
    try:
        lines = [json.dumps(rec, sort_keys=True) for rec in records]
    except (TypeError, ValueError):
        return False
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(path, "a") as fh:
            for line in lines:
                fh.write(line + "\n")
    except OSError:
        return False
    return True


def append_record(path, kind, fields=None, supersedes=None):
    """Mint an id for one record and append it. Returns the record, or None.

    `supersedes` names an existing id this record replaces; the earlier record
    is left exactly as it was and a `superseded_by` marker is appended for it,
    so the history reads forwards without any entry ever being edited.
    """
    if kind not in KINDS:
        return None
    try:
        existing = read_ledger(path)
        record = dict(fields or {})
        record["id"] = next_id(existing, kind)
        record["kind"] = kind
        record["recorded_at"] = _utc_now()
        record.setdefault("state", "open")
        batch = [record]
        if supersedes:
            record["supersedes"] = supersedes
            batch.append({
                "id": supersedes,
                "kind": kind,
                "recorded_at": record["recorded_at"],
                "state": "superseded",
                "superseded_by": record["id"],
                "marker": True,
            })
        if not _append(path, batch):
            return None
        return record
    except Exception:  # noqa: BLE001 - the ledger never breaks a run
        return None


def withdraw(path, record_id, reason=None):
    """Mark a record withdrawn WITHOUT removing it.

    A withdrawn finding must survive as withdrawn: it is evidence that a
    reviewer raised something and then retracted it, which is a different fact
    from never having raised it — and it is exactly the record an evaluator
    must not be able to count as real.
    """
    if not record_id:
        return None
    kind = next((k for k, p in KINDS.items()
                 if record_id.startswith(p + "-")), None)
    if kind is None:
        return None
    marker = {
        "id": record_id,
        "kind": kind,
        "recorded_at": _utc_now(),
        "state": "withdrawn",
        "reason": reason,
        "marker": True,
    }
    return marker if _append(path, [marker]) else None


def current_state(records, record_id):
    """The LATEST state of one id across the whole history (`open` when a record
    exists with no later marker, None when the id was never minted)."""
    state = None
    for rec in records or []:
        if rec.get("id") == record_id:
            state = rec.get("state") or "open"
    return state


def collapse(records):
    """Fold the append-only history into the current view of each id.

    Returns `{id: record}` where each record carries its LATEST `state` and its
    original fields. Nothing is dropped — a withdrawn record is present with
    `state='withdrawn'` — because the point of the ledger is that a claim can be
    checked against what actually happened, including the retractions.
    """
    out = {}
    for rec in records or []:
        rid = rec.get("id")
        if not rid:
            continue
        if rec.get("marker"):
            if rid in out:
                out[rid] = dict(out[rid])
                out[rid]["state"] = rec.get("state") or out[rid].get("state")
                if rec.get("superseded_by"):
                    out[rid]["superseded_by"] = rec["superseded_by"]
                if rec.get("reason"):
                    out[rid]["withdrawn_reason"] = rec["reason"]
            continue
        out[rid] = dict(rec)
    return out


def active_attempts(records):
    """Return one active attempt for each controller-owned natural key.

    Old ledgers may contain one V id per parser revision. New reconciliation
    keeps a stable id, but report/readiness readers must also handle those
    legacy histories without counting every superseded reading as a separate
    attempt. The latest non-superseded reading wins per natural key; raw JSONL
    remains untouched and retains every historical version.
    """
    by_key = {}
    for rec in collapse(records).values():
        if rec.get("kind") != "attempt" or rec.get("marker"):
            continue
        key = rec.get("attempt_key") or attempt_key(rec) or rec.get("id")
        prior = by_key.get(key)
        rank = (
            rec.get("state") != "superseded",
            rec.get("recorded_at") or "",
            rec.get("id") or "",
        )
        if prior is None or rank > prior[0]:
            by_key[key] = (rank, rec)
    return [item[1] for item in by_key.values()]


def attempt_key(observation):
    """The natural key of a verification-attempt observation.

    `(controller_session_id, tool_call_id)` — both are the CONTROLLER's own
    identifiers, so the same log always reconciles to the same ledger entry no
    matter how many times it is read. This is what makes reconciliation
    idempotent, and it is why ingestion is forbidden from minting ids: an
    ingestion-minted id would change every time the log was re-read.
    """
    if not isinstance(observation, dict):
        return None
    session = observation.get("controller_session_id")
    call = observation.get("tool_call_id")
    if not call:
        return None
    return "%s:%s" % (session or "", call)


# Fields carried from an observation onto its ledger record. Listed explicitly
# so a new ingestion field cannot silently start appearing in the ledger, and so
# nothing content-bearing can arrive by accident.
_ATTEMPT_FIELDS = (
    "role", "controller", "controller_session_id", "tool_call_id", "tool_name",
    "command_identity", "intent", "purpose", "exit_status", "adjudication",
    "executed_count", "expected_count", "expected_polarity", "timed_out",
    "interrupted", "pipeline", "tty_stdin_mode", "wall_time_s", "started_at",
    "ended_at", "output_bytes", "source_manifest", "source_state",
    "evidence_lost_after", "failure_class", "attempt_number", "retries",
    "retry_of", "retry_state", "overlap_state", "overlaps",
    "environment_recurrence", "evidence_safety", "refusal_reason",
    "mutations_during_run", "command_fingerprint",
    "abandoned",
    # Cowork's own first-hand observation of which tree an attempt ran against.
    "observed_source_digest",
)

def _terminal_attempt(record):
    """Whether this reading represents an explicitly terminal unresolved run.

    A timeout or explicit abandonment is terminal. A tool call whose result has
    merely not reached the asynchronous controller log yet is provisional and
    must be allowed to resolve when the matching result arrives.
    """
    return bool(record.get("timed_out") or record.get("abandoned"))


def reconcile_attempts(path, observations):
    """Turn id-free ingestion observations into identified ledger records.

    THE ONLY MINTING POINT for verification attempts (P3). For each observation:

    - a natural key not yet in the ledger MINTS the next `V-000n` and appends;
    - a key already present appends a REVISION under the SAME stable V id when
      the observed state changed, and appends nothing when it did not.

    That makes reconciliation idempotent: replaying the same log appends nothing
    the second time, so the ten report runs a user might make cannot inflate the
    attempt count or renumber the history.

    Best-effort: any failure leaves the ledger untouched and is reported in the
    result, never half-written.

    Returns `{"minted": [...], "superseded": [...], "unchanged": n,
              "state": "ok"|"failed"}`.
    """
    result = {"minted": [], "superseded": [], "unchanged": 0, "state": "ok"}
    if not path:
        result["state"] = "failed"
        return result
    try:
        existing = read_ledger(path)
        by_key = {}
        canonical_id = {}
        for rec in existing:
            if rec.get("kind") != "attempt" or rec.get("marker"):
                continue
            key = rec.get("attempt_key")
            if key:
                by_key[key] = rec
                canonical_id.setdefault(key, rec.get("id"))
        batch = []
        next_number = int(next_id(existing, "attempt").split("-")[1])
        for observation in observations or []:
            key = attempt_key(observation)
            if not key:
                continue
            fields = {k: observation.get(k) for k in _ATTEMPT_FIELDS
                      if observation.get(k) is not None}
            prior = by_key.get(key)
            if prior is None:
                record = dict(fields)
                record.update({
                    "id": "V-%04d" % next_number,
                    "kind": "attempt",
                    "attempt_key": key,
                    "recorded_at": _utc_now(),
                    "state": "open",
                    "attempt_state": "fresh",
                })
                next_number += 1
                batch.append(record)
                result["minted"].append(record["id"])
                by_key[key] = record
                canonical_id[key] = record["id"]
                continue
            if _same_attempt(prior, fields):
                result["unchanged"] += 1
                continue
            if _terminal_attempt(prior):
                # Terminal: the earlier attempt's outcome stands. A later
                # reading cannot resolve a timeout/explicit abandonment.
                result["unchanged"] += 1
                continue
            record = dict(fields)
            record.update({
                "id": canonical_id[key],
                "kind": "attempt",
                "attempt_key": key,
                "recorded_at": _utc_now(),
                "state": "open",
                "attempt_state": "revised",
                "revises_recorded_at": prior.get("recorded_at"),
            })
            batch.append(record)
            result["superseded"].append(record["id"])
            by_key[key] = record
        if batch and not _append(path, batch):
            return {"minted": [], "superseded": [], "unchanged": 0,
                    "state": "failed"}
    except Exception as exc:  # noqa: BLE001 - reconciliation never breaks a run
        return {"minted": [], "superseded": [], "unchanged": 0,
                "state": "failed", "detail": str(exc)}
    return result


# The fields whose change means the ATTEMPT itself changed, rather than merely
# being read again. Timestamps and byte counts are deliberately excluded: a
# re-read of the same log must compare equal.
_SIGNIFICANT = ("exit_status", "adjudication", "executed_count", "timed_out",
                "interrupted", "command_identity", "observed_source_digest")


def _same_attempt(prior, fields):
    return all(prior.get(k) == fields.get(k) for k in _SIGNIFICANT)


# --------------------------------------------------------------------------- #
# Convenience writers used by the orchestrator.                               #
# --------------------------------------------------------------------------- #


def append_finding(path, summary=None, severity=None, criterion=None,
                   evidence_path=None, evidence_sha256=None, discoverer=None,
                   round_index=None, phase=None, disposition=None,
                   supersedes=None, **extra):
    """Record one corrective finding. `severity` and `criterion` are typed so a
    finding's weight is a property of the record rather than of how strongly its
    prose was worded."""
    fields = {
        "summary": summary, "severity": severity, "criterion": criterion,
        "evidence_path": evidence_path, "evidence_sha256": evidence_sha256,
        "discoverer": discoverer, "round": round_index, "phase": phase,
        "disposition": disposition,
    }
    fields.update(extra)
    return append_record(path, "finding",
                         {k: v for k, v in fields.items() if v is not None},
                         supersedes=supersedes)


def append_decision(path, summary=None, phase=None, round_index=None,
                    rationale=None, source=None, **extra):
    """Record one decision. The orchestrator hands the minted id to the role, so
    a later question citing a decision cites an id cowork assigned rather than
    one an agent invented (CV-015, CV-018)."""
    fields = {"summary": summary, "phase": phase, "round": round_index,
              "rationale": rationale, "source": source}
    fields.update(extra)
    return append_record(path, "decision",
                         {k: v for k, v in fields.items() if v is not None})


def append_amendment(path, gate=None, summary=None, phase=None,
                     round_index=None, **extra):
    """Record one human amendment at a gate — what the user changed, and where.
    Distinct from a finding: the user is not a reviewer, and folding their
    interventions into the finding count would misattribute their work."""
    fields = {"gate": gate, "summary": summary, "phase": phase,
              "round": round_index}
    fields.update(extra)
    return append_record(path, "amendment",
                         {k: v for k, v in fields.items() if v is not None})


def append_escape(path, summary=None, severity=None, discovered_in=None,
                  origin_phase=None, origin_round=None, **extra):
    """Record one ESCAPED defect: something a later phase found that an earlier
    phase's review should have caught. The gap between `origin_phase` and
    `discovered_in` is the measurement — a review that approves everything and
    a review that catches everything look identical until escapes are counted."""
    fields = {"summary": summary, "severity": severity,
              "discovered_in": discovered_in, "origin_phase": origin_phase,
              "origin_round": origin_round}
    fields.update(extra)
    return append_record(path, "escape",
                         {k: v for k, v in fields.items() if v is not None})


def validate_citations(records, cited_ids, allow_withdrawn=False):
    """Check ids an evaluator cited against what the ledger actually holds.

    Returns `{"valid": [...], "invented": [...], "withdrawn": [...]}`. This is
    what stops a claim of value from resting on history that never happened: an
    id that was never minted is INVENTED, and a withdrawn finding counted as
    real is WITHDRAWN. Both make the claim unverifiable rather than merely
    wrong.
    """
    collapsed = collapse(records)
    out = {"valid": [], "invented": [], "withdrawn": []}
    for rid in cited_ids or []:
        record = collapsed.get(rid)
        if record is None:
            out["invented"].append(rid)
        elif record.get("state") == "withdrawn" and not allow_withdrawn:
            out["withdrawn"].append(rid)
        else:
            out["valid"].append(rid)
    return out
