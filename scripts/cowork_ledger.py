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
import fcntl
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


def _append_locked(path, records):
    """The raw append write — NO locking of its own. Callers that already
    hold the session-ledger allocation lock (`reconcile_attempts`,
    `mint_owned_attempts_batch`'s legacy-path callers) use this directly to
    avoid the self-deadlock of a process trying to `flock` a file it is
    already holding an exclusive lock on via a different fd. `_append`
    (below) is the locking public entry point for every OTHER caller."""
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


def _with_ledger_lock(path, fn):
    """Acquire the ONE session-ledger allocation lock (the same lock
    `mint_owned_attempts_batch` and `reconcile_attempts` hold across their
    own read/allocate/append sequences) and call `fn()` while holding it.
    Every writer to this ledger file — not just the ones minting a V-id —
    goes through this one lock, so a plain append (an ordinary finding,
    decision, or revision) can never land in the narrow window between a
    batch-mint's read and its atomic replace and be silently discarded by
    it, and two independent allocators (an owned mint and a legacy
    reconcile) can never race each other for the same next id."""
    lock_path = _mint_lock_path(path)
    try:
        dirname = os.path.dirname(lock_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        lock_fh = open(lock_path, "a+")
    except OSError:
        return False
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_fh.close()


def _append(path, records):
    """Append records to the ledger, under the ledger's ONE allocation
    lock. Returns True on success.

    All-or-nothing per call: the batch is serialized first, so a record
    that cannot be encoded aborts the write instead of leaving a partial
    batch. This is the PUBLIC, locking entry point for the plain-append
    write path — callers that already hold the lock themselves (inside
    `_with_ledger_lock`) must call `_append_locked` directly instead, to
    avoid `flock`-ing a file this process is already holding a lock on via
    a different fd (a self-deadlock, not a wait)."""
    if not path or not records:
        return False
    return _with_ledger_lock(path, lambda: _append_locked(path, records))


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

    Runs its ENTIRE read/allocate/append sequence under the SAME session-
    ledger allocation lock `mint_owned_attempts_batch` holds — this is what
    makes `V-000n` a single, process-boundary-safe namespace shared by
    BOTH allocators. Without this, an owned mint and a legacy reconcile
    running concurrently in the same session (a report/drain racing a
    builder's owned transaction) could each read the same "next number"
    from a stale, unlocked snapshot and allocate the identical id — the
    single-flight lock over a transaction's OWN request key does not
    protect against this, since it only prevents one EQUIVALENT request
    from double-running, not two DIFFERENT allocators from colliding.

    Returns `{"minted": [...], "superseded": [...], "unchanged": n,
              "state": "ok"|"failed"}`.
    """
    result = {"minted": [], "superseded": [], "unchanged": 0, "state": "ok"}
    if not path:
        result["state"] = "failed"
        return result
    outcome = _with_ledger_lock(path, lambda: _reconcile_attempts_locked(
        path, observations, result))
    if outcome is False:
        return {"minted": [], "superseded": [], "unchanged": 0,
               "state": "failed"}
    return outcome


def _reconcile_attempts_locked(path, observations, result):
    """The body of `reconcile_attempts`, run while the caller already holds
    the session-ledger lock. Returns the result dict, or False on failure
    (the sentinel `reconcile_attempts` maps to its own failure shape)."""
    try:
        try:
            raw_prefix, existing = _strict_raw_and_records_for_allocation(
                path)
        except LedgerAllocationReadError:
            # Same fail-closed posture as `mint_owned_attempts_batch`: an
            # unreadable/malformed ledger is never silently treated as
            # empty for an ALLOCATION decision.
            return {"minted": [], "superseded": [], "unchanged": 0,
                   "state": "failed"}
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
        # `_atomic_commit_batch`, NOT the line-at-a-time `_append`/
        # `_append_locked`: reconcile's own documented "any failure leaves
        # the ledger untouched... never half-written" guarantee needs the
        # SAME real atomicity `mint_owned_attempts_batch` uses — a plain
        # append can leave a durable partial batch on a real I/O failure
        # partway through. `raw_prefix` was read under this same lock,
        # right before this batch was computed, so it is exactly what
        # `path` still contains (no concurrent writer could have changed
        # it — every writer goes through this one lock).
        if batch and not _atomic_commit_batch(path, raw_prefix, batch):
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
# Owned verification transactions: orchestrator-owned mint/revise path.       #
#                                                                              #
# Unlike `reconcile_attempts` (which turns id-free, controller-log-DERIVED    #
# observations into ledger records after the fact), an owned transaction's    #
# attempt identity must exist BEFORE the command launches — the transaction   #
# id and label are already known the instant the orchestrator decides to run  #
# it. `mint_owned_attempt` allocates that stable V id up front;               #
# `revise_owned_attempt` appends terminal facts under the SAME id as evidence #
# arrives. Both use `owned_attempt_key`, a natural key derived from           #
# (transaction_id, label) — disjoint by construction from                     #
# `attempt_key`'s (controller_session_id, tool_call_id) key space, so an      #
# owned attempt can never collide with, or be reconstructed by,               #
# `reconcile_attempts`/`active_attempts`'s legacy controller-log path.        #
# --------------------------------------------------------------------------- #


def owned_attempt_key(transaction_id, label):
    """The natural key of one owned-transaction attempt: `(transaction_id,
    label)`. Distinct in shape from `attempt_key`'s controller-derived key so
    the two identity spaces can never be confused by a reader that only looks
    at the key string."""
    if not transaction_id or not label:
        return None
    return "owned:%s:%s" % (transaction_id, label)


def _mint_lock_path(path):
    return path + ".mint.lock"


class LedgerAllocationReadError(Exception):
    """Raised by `_strict_raw_and_records_for_allocation` — the ledger
    exists but cannot be trusted for an ALLOCATION decision (unreadable,
    or contains a line that cannot be parsed). Callers must fail the WHOLE
    mint/reconcile operation closed, never proceed as if the ledger were
    simply empty or as if the bad line just weren't there."""


def _strict_raw_and_records_for_allocation(path):
    """The ledger's raw bytes AND parsed records, for an ALLOCATION
    decision (minting or reconciling a `V-xxxx` id) — deliberately NOT the
    same tolerance `read_ledger` uses for report-rendering, where a
    best-effort partial view is the right tradeoff.

    Distinguishes MISSING (the ledger has simply never been created yet —
    legitimately empty: returns `(b"", [])`) from UNREADABLE (the file
    exists but a permissions/I-O error prevents reading it) or MALFORMED
    (any single line is not valid JSON) — either of the latter two RAISES
    `LedgerAllocationReadError` instead of silently treating the ledger as
    empty or silently skipping the bad line. `_read_raw_ledger_bytes`
    (the prior implementation) returned `b""` for BOTH missing and
    unreadable, and callers used `read_ledger`'s tolerant, line-skipping
    scan for the natural-key/`next_id` check — either one could let an
    allocator (a) commit a fresh batch as if the ledger's real, merely
    temporarily-unreadable history had never existed (replacing it via
    the atomic-commit path), or (b) allocate a DUPLICATE id for a key
    that was actually already present, just hidden inside a line the
    tolerant scan silently skipped.
    """
    if not path or not os.path.exists(path):
        return b"", []
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise LedgerAllocationReadError(
            "ledger exists but is unreadable: %s" % exc)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerAllocationReadError(
            "ledger contains undecodable bytes: %s" % exc)
    records = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            records.append(json.loads(stripped))
        except (ValueError, TypeError) as exc:
            raise LedgerAllocationReadError(
                "malformed ledger line %d: %s" % (lineno, exc))
    return raw, records


def _atomic_commit_batch(path, raw_prefix, batch):
    """Commit `batch` (a list of new records) onto `raw_prefix` (the exact
    existing ledger bytes) with REAL atomicity: build the complete new
    ledger content in a temp file on the SAME directory (so the final
    rename is on one filesystem) and `os.replace()` it onto `path` in one
    step.

    `_append`'s own line-at-a-time `open(path, "a")` loop does NOT give
    this guarantee: a real I/O failure after writing N of M lines leaves a
    durable, on-disk PARTIAL batch — some new records visible, others not.
    `os.replace` is a single atomic rename on POSIX; a reader of `path` at
    any point during this function's execution sees EITHER the complete
    `raw_prefix` (nothing from this batch yet) or the complete
    `raw_prefix + batch` (the whole batch, in one atomic step) — never
    anything in between. If the process fails ANY time before the
    `os.replace` call (including after real bytes have already been
    written to the temp file — a genuine on-disk "prefix write"), `path`
    itself is untouched, because nothing ever writes to `path` directly.

    Returns True on success, False on any failure (temp file cleaned up
    either way, never left renamed onto `path` unless fully committed).

    `raw_prefix` is concatenated directly with the new batch's bytes, not
    re-joined line by line — so a `raw_prefix` whose last byte is not a
    newline (a valid, parseable JSONL file simply written without a final
    trailing newline; `_strict_raw_and_records_for_allocation`'s
    `splitlines()`-based scan accepts that shape happily) would otherwise
    glue the first new record directly onto the tail of the existing last
    line, corrupting BOTH into one unparseable line on disk. The separator
    is inserted deliberately here, once, rather than trusting every
    producer of `raw_prefix` to have already terminated it."""
    if not batch:
        return True
    if raw_prefix and not raw_prefix.endswith(b"\n"):
        raw_prefix = raw_prefix + b"\n"
    try:
        lines = b"".join(
            (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8")
            for rec in batch)
    except (TypeError, ValueError):
        return False
    dirname = os.path.dirname(path) or "."
    try:
        os.makedirs(dirname, exist_ok=True)
    except OSError:
        return False
    tmp_path = os.path.join(
        dirname, ".%s.tmp-%d-%s" % (os.path.basename(path), os.getpid(),
                                    _utc_now().replace(":", "")))
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(raw_prefix)
            fh.write(lines)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        return True
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def mint_owned_attempts_batch(path, transaction_id, entries):
    """Allocate a stable V id for EVERY `(label, fields)` pair in `entries`,
    ALL-OR-NOTHING, under one held session-ledger allocation lock.

    This is the ONLY minting entry point for owned attempts — replacing an
    earlier design that read the ledger, computed `next_id`, and appended
    per entry with NO lock held across that sequence: two transactions in
    the same session (two processes/threads calling this concurrently)
    could both read the same "highest id so far" and mint the identical
    V-xxxx for two different attempts, and a failure partway through a
    multi-entry mint left the earlier entries durably minted while later
    ones were not — exactly the "partial owned inventory" this exists to
    rule out.

    The lock (`fcntl.flock`, exclusive, held for the WHOLE re-read ->
    natural-key-check -> allocate -> append sequence — never just around
    the final write) makes two concurrent callers, however many entries
    each requests, allocate from one strictly increasing, globally unique
    id space. Existing records for a `(transaction_id, label)` already
    minted are reused idempotently (safe to call again for the same
    entries). ALL NEW records for THIS CALL are computed fully in memory
    before a single `_append` — if anything about the batch is invalid (a
    malformed entry, a natural-key collision, the append itself failing),
    NOTHING from this call is written: not the entries validated before the
    bad one, not any of them.

    Returns `{label: record}` for every entry on success (both freshly
    minted and idempotently reused), or `None` if the WHOLE batch failed —
    the caller must treat `None` as "not one of these ids is durable yet",
    never assume the first N succeeded.
    """
    if not path or not entries:
        return None
    lock_path = _mint_lock_path(path)
    try:
        dirname = os.path.dirname(lock_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        lock_fh = open(lock_path, "a+")
    except OSError:
        return None
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            try:
                raw_prefix, existing = _strict_raw_and_records_for_allocation(
                    path)
            except LedgerAllocationReadError:
                # FAIL CLOSED: an unreadable or malformed ledger is NEVER
                # treated as empty. Minting on top of it (via the atomic
                # replace below) could silently discard real history, or
                # allocate a duplicate id for a key hidden in the bad data.
                return None
            by_key = {}
            for rec in existing:
                if rec.get("kind") == "attempt" and not rec.get("marker"):
                    k = rec.get("attempt_key")
                    if k:
                        by_key[k] = rec
            next_number = int(next_id(existing, "attempt").split("-")[1])

            results = {}
            batch = []
            seen_keys_this_call = set()
            for label, fields in entries:
                key = owned_attempt_key(transaction_id, label)
                if not key or key in seen_keys_this_call:
                    # A malformed entry, or the SAME label twice in one
                    # batch (a caller bug — never silently mint two ids for
                    # one label), aborts the WHOLE batch. Nothing is
                    # appended for this call at all.
                    return None
                seen_keys_this_call.add(key)
                prior = by_key.get(key)
                if prior is not None:
                    results[label] = prior  # idempotent reuse
                    continue
                record = dict(fields or {})
                record.update({
                    "id": "V-%04d" % next_number,
                    "kind": "attempt",
                    "attempt_key": key,
                    "transaction_id": transaction_id,
                    "label": label,
                    "recorded_at": _utc_now(),
                    "state": "open",
                    "attempt_state": "pending",
                    "owned": True,
                })
                next_number += 1
                batch.append(record)
                by_key[key] = record
                results[label] = record

            if batch and not _atomic_commit_batch(path, raw_prefix, batch):
                return None
            return results
        except Exception:  # noqa: BLE001 - the ledger never breaks a run
            return None
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_fh.close()


def mint_owned_attempt(path, transaction_id, label, fields=None):
    """Single-entry convenience wrapper over `mint_owned_attempts_batch` —
    kept for callers that genuinely only ever mint one attempt at a time
    (and for backward compatibility), but routed through the SAME locked
    batch path so a lone mint can never race a concurrent batch mint in the
    same session. Returns the minted (or idempotently reused) record, or
    `None` on failure."""
    results = mint_owned_attempts_batch(path, transaction_id,
                                        [(label, fields)])
    return (results or {}).get(label)


def revise_owned_attempt(path, transaction_id, label, fields,
                         attempt_state="terminal"):
    """Append a revision under the SAME stable V id a prior
    `mint_owned_attempt` call allocated for `(transaction_id, label)`.

    FAILS CLOSED, deliberately, unlike the legacy `reconcile_attempts` path:
    a missing prior mint is NOT filled in by minting one here. The owned
    contract requires every attempt id to exist BEFORE its command can
    launch (see `run_transaction`); a revision arriving with no matching
    mint means that guarantee was violated somewhere upstream, and inventing
    an id at revision time would silently launder that violation into a
    normal-looking record instead of surfacing it. The caller (the parent,
    in `cowork_verification.py`) is REQUIRED to treat a `None` return here
    as invalidating the transaction, never as "nothing to revise."

    Returns the appended record, or None when there was no prior mint to
    revise, or on any other failure.
    """
    key = owned_attempt_key(transaction_id, label)
    if not key:
        return None
    try:
        existing = read_ledger(path)
        canonical_id = None
        for rec in existing:
            if (rec.get("kind") == "attempt" and not rec.get("marker")
                    and rec.get("attempt_key") == key):
                canonical_id = rec.get("id")
        if canonical_id is None:
            return None
        record = dict(fields or {})
        record.update({
            "id": canonical_id,
            "kind": "attempt",
            "attempt_key": key,
            "transaction_id": transaction_id,
            "label": label,
            "recorded_at": _utc_now(),
            "state": "open",
            "attempt_state": attempt_state,
            "owned": True,
        })
        if not _append(path, [record]):
            return None
        return record
    except Exception:  # noqa: BLE001 - the ledger never breaks a run
        return None


def materialize_attempts(records, observations):
    """PURE, in-memory materialization: combine existing ledger `records`
    with a list of ingested `observations`, using the SAME identity rules as
    the persistent `reconcile_attempts` path (natural key via `attempt_key`,
    latest-non-superseded-wins), but performing no disk writes at all.

    This exists so a read-only clean checkout (a fixture/session directory
    whose ignored `ledger.jsonl` is absent or not writable) can still
    reproduce the identical attempts/verdict a persisted reconciliation would
    have produced, by materializing directly from tracked controller-log
    inputs in memory.

    Returns the list of active attempt records (same shape `active_attempts`
    returns), as if `reconcile_attempts` had been called against `records`
    with `observations` and then `active_attempts` had been read back —
    without ever touching `path`.
    """
    by_key = {}
    canonical_id = {}
    for rec in records or []:
        if rec.get("kind") != "attempt" or rec.get("marker"):
            continue
        key = rec.get("attempt_key")
        if key:
            by_key[key] = rec
            canonical_id.setdefault(key, rec.get("id"))
    next_number = int(next_id(list(records or []), "attempt").split("-")[1])
    materialized = dict(by_key)
    for observation in observations or []:
        key = attempt_key(observation)
        if not key:
            continue
        fields = {k: observation.get(k) for k in _ATTEMPT_FIELDS
                  if observation.get(k) is not None}
        prior = materialized.get(key)
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
            materialized[key] = record
            canonical_id[key] = record["id"]
            continue
        if _same_attempt(prior, fields):
            continue
        if _terminal_attempt(prior):
            continue
        record = dict(fields)
        record.update({
            "id": canonical_id.get(key, prior.get("id")),
            "kind": "attempt",
            "attempt_key": key,
            "recorded_at": _utc_now(),
            "state": "open",
            "attempt_state": "revised",
            "revises_recorded_at": prior.get("recorded_at"),
        })
        materialized[key] = record
    # Fold through the same collapse/latest-wins rule `active_attempts` uses,
    # but starting from the in-memory materialized view rather than a second
    # read of `path`.
    return active_attempts(list(materialized.values()) + [
        rec for rec in (records or [])
        if rec.get("marker") or rec.get("kind") != "attempt"])


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
