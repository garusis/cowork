#!/usr/bin/env python3
"""M3 Package D -- scheduler lease decisions.

A platform-neutral, fake-clock-testable DECISION layer over Package B's
lock-protected PauseLease accessors (`cowork_state.py`'s
`create_pause_lease`/`claim_pause_lease`/`cancel_pause_lease`/
`mark_pause_lease_consumed`/`replace_pause_lease`/`mark_pause_lease_expired`/
`record_pause_lease_failed_wake_attempt`). This module owns no storage and no
lock: every mutating decision below ends in exactly one call to a Package B
accessor, which is itself the sole real OS-level `fcntl.flock` boundary. This
module never opens a lock file, never writes a PauseLease/CapacityPacket
record directly, and never bypasses Package B's binding index or monotonic
`failed_wake_attempts` counter contract (`cowork_capacity.
next_pause_lease_after_replacement`) -- every counter/binding invariant
Package B already enforces is preserved exactly, never re-derived here.

Time and entropy are always explicit caller inputs: every decision function
below takes an RFC3339 `now` string (and, for jitter, an explicit seed
string) as a parameter -- this module never reads the wall clock
(no `time`/`datetime` import), never sleeps, and never touches global
`random` state (no `random` import; `deterministic_jitter_seconds` derives
its bounded offset from a pure `hashlib.sha256` hash of caller-supplied seed
material instead).

M3A-REV-014: this module is a PURE DECISION LAYER. It emits no
control-plane event -- it never imports `cowork_control_plane` and calls no
`advance()`/event-emission surface of any kind. If a future change ever adds
one, M3A-REV-014 requires it be refused unless a non-null exact candidate
binding and a production refusal seam are both proven; Package G audits
this. Absent such a surface, the requirement is vacuously satisfied by this
module's own shape: no event emitter exists here at all.

Two closed decision paths for claiming a lease early:

  * `claim` -- the ORDINARY path. Always refuses `now < not_before` for a
    `scheduled`-mode lease. Takes NO override parameter of any kind; there
    is no way to pass this function evidence that would make it grant an
    early claim. It also NEVER grants a `manual_signal`-mode lease (that
    mode has no `not_before` to gate on at all, so this path has no honest
    way to verify a manual signal genuinely occurred) -- only
    `claim_with_authorized_early_override` may claim one.
  * `claim_with_authorized_early_override` -- the SEPARATE verified-evidence
    path. Grants an early (or manual_signal-mode) claim ONLY after the
    supplied manual-capacity signal record's BINDING is checked against the
    lease and it is genuinely, cryptographically verified against a
    caller-pinned public-key registry via Package B's own
    `write_manual_capacity_signal` (real Ed25519 verification, never a
    plaintext trust flag). Refuses reuse of the SAME evidence to authorize a
    DIFFERENT lease_id (single-use per lease). A successful override
    additionally appends a DURABLE, DISTINCT linkage record (this module's
    own versioned "manual signal journal", written via Package B's public
    `append_jsonl_atomic` primitive -- not a bespoke lock/storage scheme)
    tying the specific claim decision to the specific verified evidence
    that authorized it -- and that linkage append is itself IDEMPOTENT and
    CRASH-REPAIRABLE: a retry after a crash between the claim succeeding
    and the journal append completing repairs the missing entry instead of
    losing the attribution forever.

Same-owner duplicate claims are idempotent (including across real,
separate OS processes racing Package B's own lock); a different-owner live
claim is always an explicit conflict, never a silent overwrite.
`automation_ref` is a REQUIRED argument on every mutating decision below and
is always checked against the stored lease's own value first. Replacement
(`replace`) delegates entirely to `cowork_state.replace_pause_lease` --
same-binding and counter-monotonic by construction -- and starting a brand
new, independent pause episode for a genuinely terminal binding is a
SEPARATE, distinctly named function (`start_new_episode`, a thin wrapper
over `cowork_state.create_pause_lease`) that can never be confused with, or
silently substituted for, a replacement. `start_new_episode` additionally
enforces the M3R-N06 ceiling PER BINDING across that reset boundary: it
refuses to start a fresh episode for a binding whose most recently resolved
(terminal, non-live) lease already reached
`cowork_capacity.FAILED_WAKE_ATTEMPT_CEILING` -- reclaiming (expiring) a
ceiling-exhausted lease and then starting a new episode can never silently
reset the counter back to zero.

This module performs no I/O of its own beyond: `cowork_state.
append_jsonl_atomic` (the manual-signal-journal linkage append),
`cowork_state.read_jsonl_tolerant` (reading that journal back), and
`cowork_state.read_json_tolerant` plus `cowork_state.read_pause_lease` (the
read-only binding-current resolution seam, see `resolve_current_lease_for_
binding`) -- every other durability guarantee is Package B's.

Public API:
    SchedulerError, SchedulerLeaseConflict, SchedulerOverrideRecordingFailed
    resolve_effective_now_epoch(now, reference_now=None, max_clock_skew_seconds=...)
    deterministic_jitter_seconds(seed_material, max_jitter_seconds)
    next_scheduled_wake_epoch(not_before_epoch, seed_material, max_jitter_seconds=...)
    resolve_current_lease_for_binding(session_uuid, binding)
    claim(session_uuid, lease_id, claimant_ref, now, automation_ref, ...)
    claim_with_authorized_early_override(session_uuid, lease_id, claimant_ref,
        now, manual_signal_record, pinned_public_keys, automation_ref)
    cancel(session_uuid, lease_id, automation_ref)
    mark_consumed(session_uuid, lease_id, automation_ref)
    replace(session_uuid, old_lease_id, new_pause_lease, automation_ref)
    start_new_episode(session_uuid, pause_lease)
    reclaim_if_expired(session_uuid, lease_id, now, expiry_after_seconds, automation_ref, ...)
    wake_decision(session_uuid, lease_id)
    record_failed_wake_attempt(session_uuid, lease_id, automation_ref)
    MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION, validate_manual_signal_journal_entry
    manual_signal_journal_path_for, read_manual_signal_journal
    WAKE_TRIGGER_CONTRACT_VERSION, WAKE_TRIGGER_EXIT_CODES
    build_wake_trigger_arg_parser, run_wake_trigger

Python 3.9+, stdlib plus `cowork_capacity` (Package A) and `cowork_state`
(Package B) only.
"""

import argparse
import hashlib
import json
import os
import re
import sys

import cowork_capacity as capacity
import cowork_state as state_store

# --------------------------------------------------------------------------- #
# Versioning.                                                                #
# --------------------------------------------------------------------------- #

SCHEDULER_DECISION_LAYER_VERSION = 2

DEFAULT_MAX_CLOCK_SKEW_SECONDS = 30.0
DEFAULT_MAX_JITTER_SECONDS = 60.0
DEFAULT_EXPIRY_GRACE_SECONDS = 300.0

_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


# --------------------------------------------------------------------------- #
# Exceptions.                                                                 #
# --------------------------------------------------------------------------- #


class SchedulerError(Exception):
    """Base class for every error this module itself raises (as opposed to
    a Package A/B exception that propagates through unchanged, e.g. a plain
    `ValueError` from a malformed input shape, or
    `cowork_state.CrossBindingReplacementError`)."""


class SchedulerLeaseConflict(SchedulerError):
    """Raised by every mutating decision below when the requested
    transition is refused -- either because this module's own explicit
    rule refuses it (early refusal, automation_ref mismatch, a
    different-owner live claim, invalid/mismatched/reused override
    evidence, a ceiling-exhausted binding) or because Package B's own
    accessor refused it (`PauseLeaseConflict`, translated here 1:1 by
    `reason`/`state`). Never raised for the documented idempotent-retry
    cases (`claim`'s same-owner duplicate, `mark_consumed`'s
    already-consumed no-op) -- those return normally.

    Carries the structured `lease_id`/`reason` a caller needs to report a
    truthful conflict, plus any additional structured `details` (e.g.
    `state=...`, `claimant_ref=...`, `expected=...`/`got=...`)."""

    def __init__(self, lease_id, reason, **details):
        self.lease_id = lease_id
        self.reason = reason
        self.details = details
        super().__init__(
            "PauseLease %r: %s (%s)" % (lease_id, reason, details))


class SchedulerOverrideRecordingFailed(SchedulerError):
    """Raised by `claim_with_authorized_early_override` when the underlying
    PauseLease claim itself durably succeeded (via Package B) but the
    distinct manual-signal-journal linkage record (this module's own
    append, via `cowork_state.append_jsonl_atomic`) failed to durably
    write. The claim is NOT rolled back -- Package B's own durable state is
    the source of truth and is already committed -- this exception exists
    only so a genuine I/O failure on the audit trail is never silently
    swallowed. A subsequent retry of the SAME call is REPAIR-SAFE (see the
    module docstring's crash-safety note): it will re-attempt exactly this
    append without re-attempting the (already-durable) claim."""

    def __init__(self, lease_id, detail):
        self.lease_id = lease_id
        super().__init__(
            "PauseLease %r: claim succeeded but the manual-signal-journal "
            "linkage record failed to durably append (%s)" % (lease_id, detail))


# --------------------------------------------------------------------------- #
# Explicit-clock / deterministic-jitter primitives. Pure functions of their  #
# own arguments only -- no wall clock, no sleep, no global random state.     #
# --------------------------------------------------------------------------- #


def resolve_effective_now_epoch(now, reference_now=None,
                                max_clock_skew_seconds=DEFAULT_MAX_CLOCK_SKEW_SECONDS):
    """Pure clock-skew resolution: `now` and (when supplied) `reference_now`
    are both explicit, caller-supplied RFC3339 strings -- never read from a
    real clock. Returns `(effective_now_epoch, skew_detected)`.

    When `reference_now` is omitted, `now` is trusted as-is (no independent
    reference to compare against) and `skew_detected` is always False.

    When both are supplied and they disagree by more than
    `max_clock_skew_seconds`, `now` is NOT trusted verbatim: the effective
    reading is clamped to `reference_now` +/- `max_clock_skew_seconds`
    (whichever bound `now` overshot) and `skew_detected` is True. This is
    deliberately conservative for the early-refusal check `claim` builds on
    top of this: a `now` that runs FAST relative to the trusted reference
    can never be used to claim a lease before its genuine `not_before`
    merely by overstating the clock."""
    now_epoch = capacity.rfc3339_to_epoch_seconds(now)
    if now_epoch is None:
        raise ValueError(
            "now must be an RFC3339-shaped timestamp string, got %r" % (now,))
    if reference_now is None:
        return now_epoch, False
    reference_epoch = capacity.rfc3339_to_epoch_seconds(reference_now)
    if reference_epoch is None:
        raise ValueError(
            "reference_now must be an RFC3339-shaped timestamp string, got %r"
            % (reference_now,))
    if not isinstance(max_clock_skew_seconds, (int, float)) or isinstance(
            max_clock_skew_seconds, bool) or max_clock_skew_seconds < 0:
        raise ValueError(
            "max_clock_skew_seconds must be a nonnegative number, got %r"
            % (max_clock_skew_seconds,))
    skew = now_epoch - reference_epoch
    if skew > max_clock_skew_seconds:
        return reference_epoch + max_clock_skew_seconds, True
    if skew < -max_clock_skew_seconds:
        return reference_epoch - max_clock_skew_seconds, True
    return now_epoch, False


def deterministic_jitter_seconds(seed_material, max_jitter_seconds):
    """Bounded, DETERMINISTIC jitter in `[0, max_jitter_seconds)`, derived
    purely from `sha256(seed_material)` -- the SAME `seed_material` always
    yields the SAME jitter, and this never consults or mutates any global
    random state (no `random` import anywhere in this module). Callers
    seeking a fresh value per attempt pass distinguishing `seed_material`
    (e.g. `"%s:%d" % (lease_id, attempt)`) themselves; this function makes
    no attempt-tracking decision of its own."""
    if not isinstance(seed_material, str) or not seed_material:
        raise ValueError("seed_material must be a nonempty string")
    if not isinstance(max_jitter_seconds, (int, float)) or isinstance(
            max_jitter_seconds, bool) or max_jitter_seconds < 0:
        raise ValueError(
            "max_jitter_seconds must be a nonnegative number, got %r"
            % (max_jitter_seconds,))
    if max_jitter_seconds == 0:
        return 0.0
    digest = hashlib.sha256(seed_material.encode("utf-8")).digest()
    numerator = int.from_bytes(digest[:8], "big")
    fraction = numerator / float(1 << 64)
    return fraction * max_jitter_seconds


def next_scheduled_wake_epoch(not_before_epoch, seed_material,
                              max_jitter_seconds=DEFAULT_MAX_JITTER_SECONDS):
    """`not_before_epoch` (an explicit, already-canonical epoch-seconds
    float/int) plus a deterministic, bounded jitter offset -- the wake
    scheduler's own thundering-herd avoidance. Never earlier than
    `not_before_epoch` itself (jitter is always `>= 0`)."""
    if not isinstance(not_before_epoch, (int, float)) or isinstance(
            not_before_epoch, bool):
        raise ValueError(
            "not_before_epoch must be a number, got %r" % (not_before_epoch,))
    return not_before_epoch + deterministic_jitter_seconds(
        seed_material, max_jitter_seconds)


# --------------------------------------------------------------------------- #
# Shared lease-read/automation_ref/binding-resolution helpers.               #
# --------------------------------------------------------------------------- #


def _read_canonical_lease(session_uuid, lease_id):
    """Returns `(stored, canonical)`: `stored` is Package B's raw enriched
    store record (carries bookkeeping fields like `claimant_ref`),
    `canonical` is that same record re-validated through
    `cowork_capacity.validate_pause_lease` after projecting the bookkeeping
    fields away (`cowork_state.pause_lease_from_stored_record`). Both are
    `None` when no schema-valid lease exists at `lease_id`. This never
    mutates anything -- read-only."""
    stored = state_store.read_pause_lease(session_uuid, lease_id)
    if stored is None:
        return None, None
    canonical = capacity.validate_pause_lease(
        state_store.pause_lease_from_stored_record(stored))
    return stored, canonical


def _check_automation_ref(lease_id, canonical_lease, automation_ref):
    if automation_ref != canonical_lease["automation_ref"]:
        raise SchedulerLeaseConflict(
            lease_id, "automation_ref_mismatch",
            expected=canonical_lease["automation_ref"], got=automation_ref)


def _resolve_claim_race(session_uuid, lease_id, claimant_ref, exc):
    """Translate a `PauseLeaseConflict` raised by Package B's own
    `claim_pause_lease` (reached only when two callers -- possibly two
    genuinely separate OS processes -- raced this exact lease_id) into
    either the documented same-owner-idempotent outcome or an explicit
    `SchedulerLeaseConflict`. Re-reads the CURRENT durable state fresh
    (never trusts a value observed before the race) to decide which."""
    if exc.reason == "not_found":
        raise SchedulerLeaseConflict(lease_id, "not_found")
    current = state_store.read_pause_lease(session_uuid, lease_id)
    if current is None:
        raise SchedulerLeaseConflict(lease_id, "not_found")
    if (current.get("consumption_state") == "claimed"
            and current.get("claimant_ref") == claimant_ref):
        return {"outcome": "already_claimed", "lease": current}
    raise SchedulerLeaseConflict(
        lease_id, "different_owner_conflict",
        state=current.get("consumption_state"),
        claimant_ref=current.get("claimant_ref"))


_MAX_BINDING_RESOLUTION_HOPS = 64


def resolve_current_lease_for_binding(session_uuid, binding):
    """D-N12: a TRUTHFUL, read-only binding-current resolution seam --
    resolves the CURRENT (or most recently known) PauseLease for one
    binding identity (`role`/`provider_session_id`/`controller_policy_
    digest`/`candidate_digest`), using ONLY Package B's own PUBLIC
    binding-index file and per-lease store (`pause_lease_binding_index_
    path_for`, `read_json_tolerant`, `read_pause_lease`). This mirrors
    `cowork_state._resolve_current_pause_lease`'s replaced_by-walk logic at
    this module's own layer, WITHOUT acquiring any lock and WITHOUT writing
    anything -- a plain read seam, never a reimplementation of Package B's
    storage or locking.

    Never raises for ordinary absence: returns `None` when the binding has
    never had a lease, or its index entry/chain is unreadable -- truthful
    in the sense that it reports exactly what it observes and never
    fabricates a lease that does not exist. (A malformed `session_uuid`
    still raises `ValueError`, exactly like every other accessor in this
    module -- that is a caller contract violation, not "ordinary
    absence".)

    NOTE: unlike the locked Package B twin, this performs no locking, so a
    concurrent writer could in principle be observed mid-chain. Callers use
    this only for an ADVISORY check (`start_new_episode`'s ceiling
    enforcement); the actual mutation always still goes through Package
    B's own locked accessors, which remain the true source of truth."""
    index_path = state_store.pause_lease_binding_index_path_for(session_uuid, binding)
    index = state_store.read_json_tolerant(index_path)
    if not isinstance(index, dict):
        return None
    lease_id = index.get("current_lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return None
    record = None
    for _ in range(_MAX_BINDING_RESOLUTION_HOPS):
        record = state_store.read_pause_lease(session_uuid, lease_id)
        if record is None:
            return None
        if record.get("consumption_state") != "replaced":
            return record
        next_id = record.get("replaced_by")
        if not isinstance(next_id, str) or not next_id:
            return record
        lease_id = next_id
    return record


# --------------------------------------------------------------------------- #
# claim / claim_with_authorized_early_override.                              #
# --------------------------------------------------------------------------- #


def claim(session_uuid, lease_id, claimant_ref, now, automation_ref,
          reference_now=None, max_clock_skew_seconds=DEFAULT_MAX_CLOCK_SKEW_SECONDS):
    """The ORDINARY claim path. Takes NO override parameter -- there is no
    way to pass this function evidence that grants an early claim; that is
    `claim_with_authorized_early_override`'s sole job.

    Refuses (`SchedulerLeaseConflict`, writing nothing) when: the lease
    does not exist (`reason='not_found'`); `automation_ref` does not match
    the stored lease's own (`reason='automation_ref_mismatch'`); the lease
    is currently `claimed` by a DIFFERENT claimant_ref
    (`reason='different_owner_conflict'`); the lease is in any other
    non-claimable state -- consumed/cancelled/replaced/expired
    (`reason='not_claimable'`); the lease is `manual_signal`-mode and still
    `unclaimed` (`reason='manual_signal_requires_verified_evidence'` --
    D-N03: this path has no way to honestly verify a manual signal ever
    occurred, so it never silently grants one); or -- for a
    `scheduled`-mode lease still `unclaimed` -- the effective clock (see
    `resolve_effective_now_epoch`) is strictly before `not_before`
    (`reason='early_refusal'`).

    `now`/`reference_now` are validated for well-formedness UNCONDITIONALLY
    up front, before any state is even read -- every outcome, including the
    idempotent-retry one, requires a genuinely well-formed clock reading
    (D-N05/fake-clock-testable discipline). Every returned dict (success or
    idempotent) carries `"clock_skew_detected"` (D-N05) reflecting this
    exact call's own skew computation, not merely refusal paths.

    IDEMPOTENT for a same-owner duplicate: if the lease is already
    `claimed` by this exact `claimant_ref` (observed either on the initial
    read or after losing a genuine cross-process race to itself), this
    returns `{"outcome": "already_claimed", "lease": ...}` rather than
    conflicting -- true even across two real, separate OS processes racing
    with the SAME claimant_ref.

    On success, delegates the actual mutation entirely to
    `cowork_state.claim_pause_lease` (the real cross-process `fcntl.flock`
    boundary) and returns `{"outcome": "claimed", "lease": ...}`."""
    if not isinstance(claimant_ref, str) or not claimant_ref:
        raise ValueError("claimant_ref must be a nonempty string")
    effective_now_epoch, skewed = resolve_effective_now_epoch(
        now, reference_now, max_clock_skew_seconds)

    stored, lease_ = _read_canonical_lease(session_uuid, lease_id)
    if lease_ is None:
        raise SchedulerLeaseConflict(lease_id, "not_found")
    _check_automation_ref(lease_id, lease_, automation_ref)

    state = lease_["consumption_state"]
    if state == "claimed":
        if stored.get("claimant_ref") == claimant_ref:
            return {"outcome": "already_claimed", "lease": stored,
                   "clock_skew_detected": skewed}
        raise SchedulerLeaseConflict(
            lease_id, "different_owner_conflict",
            claimant_ref=stored.get("claimant_ref"))
    if state != "unclaimed":
        raise SchedulerLeaseConflict(lease_id, "not_claimable", state=state)

    if lease_["resume_mode"] == "manual_signal":
        raise SchedulerLeaseConflict(
            lease_id, "manual_signal_requires_verified_evidence")

    not_before_epoch = capacity.rfc3339_to_epoch_seconds(lease_["not_before"])
    if effective_now_epoch < not_before_epoch:
        raise SchedulerLeaseConflict(
            lease_id, "early_refusal", not_before=lease_["not_before"],
            clock_skew_detected=skewed)

    try:
        rec = state_store.claim_pause_lease(session_uuid, lease_id, claimant_ref)
    except state_store.PauseLeaseConflict as exc:
        result = _resolve_claim_race(session_uuid, lease_id, claimant_ref, exc)
        result["clock_skew_detected"] = skewed
        return result
    return {"outcome": "claimed", "lease": rec, "clock_skew_detected": skewed}


_MANUAL_SIGNAL_JOURNAL_KEYS = frozenset({
    "schema_version", "lease_id", "claimant_ref", "automation_ref",
    "requested_at", "signal_journal_ref", "signer_public_key_id",
    "role", "provider_session_id", "controller_policy_digest", "candidate_digest",
})

MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION = 1


def validate_manual_signal_journal_entry(entry):
    """Return a normalized copy of one manual-signal-journal linkage entry,
    or raise ValueError. Exact key set (`_MANUAL_SIGNAL_JOURNAL_KEYS`) --
    this is the concrete, versioned shape Packages E and F may depend on;
    a new field bumps `MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION`, it is never
    added or removed silently under the same version number.

    `requested_at` (D-N09) must be a genuine, well-formed RFC3339 timestamp
    string (checked via `cowork_capacity.rfc3339_to_epoch_seconds`), not
    merely any nonempty string."""
    if not isinstance(entry, dict):
        raise ValueError(
            "manual signal journal entry must be a dict, got %r" % type(entry))
    extra = set(entry) - _MANUAL_SIGNAL_JOURNAL_KEYS
    missing = _MANUAL_SIGNAL_JOURNAL_KEYS - set(entry)
    if missing:
        raise ValueError(
            "manual signal journal entry missing keys: %s" % sorted(missing))
    if extra:
        raise ValueError(
            "manual signal journal entry has extra keys: %s" % sorted(extra))
    if (isinstance(entry["schema_version"], bool)
            or entry["schema_version"] != MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION):
        raise ValueError(
            "schema_version must be integer %d, got %r"
            % (MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION, entry["schema_version"]))
    for field in ("lease_id", "claimant_ref", "automation_ref",
                 "signal_journal_ref", "signer_public_key_id", "role",
                 "provider_session_id"):
        if not isinstance(entry[field], str) or not entry[field]:
            raise ValueError("%s must be a nonempty string, got %r" % (field, entry[field]))
    for field in ("controller_policy_digest", "candidate_digest"):
        if not isinstance(entry[field], str) or not _HEX64_RE.match(entry[field]):
            raise ValueError(
                "%s must be 64 lowercase hex chars, got %r" % (field, entry[field]))
    if (not isinstance(entry["requested_at"], str)
            or capacity.rfc3339_to_epoch_seconds(entry["requested_at"]) is None):
        raise ValueError(
            "requested_at must be an RFC3339-shaped timestamp string, got %r"
            % (entry["requested_at"],))
    return dict(entry)


def manual_signal_journal_path_for(session_uuid):
    """Path of this session's append-only manual-signal-journal linkage
    log -- a DISTINCT record from both the PauseLease store and Package
    B's own `manual_signals/` verified-evidence store; this is only the
    linkage between one claim decision and the evidence that authorized
    it."""
    return os.path.join(state_store.capacity_dir_for(session_uuid),
                        "scheduler_manual_signal_overrides.jsonl")


def read_manual_signal_journal(session_uuid):
    """Every durably recorded manual-signal-journal linkage entry for this
    session, oldest first. Tolerant: a missing journal yields `[]`."""
    return state_store.read_jsonl_tolerant(
        manual_signal_journal_path_for(session_uuid))


def _find_existing_override_journal_entry(session_uuid, lease_id, signal_journal_ref):
    for entry in read_manual_signal_journal(session_uuid):
        if (entry.get("lease_id") == lease_id
                and entry.get("signal_journal_ref") == signal_journal_ref):
            return entry
    return None


def _record_manual_signal_override(session_uuid, lease_id, claimant_ref,
                                   automation_ref, now, verified_signal):
    """Durably append the manual-signal-journal linkage entry.

    D-MJ-03: IDEMPOTENT and CRASH-REPAIRABLE -- if an entry already exists
    for this EXACT `(lease_id, signal_journal_ref)` pair (e.g. a prior call
    durably won the claim via Package B but crashed before this append
    itself completed, and a later retry reaches this point again), this
    returns the EXISTING entry unchanged rather than appending a
    duplicate. This is what makes `claim_with_authorized_early_override`
    safe to retry after a crash landing strictly between the durable claim
    and the durable journal append: the retry's idempotent path repairs the
    missing attribution instead of returning success with it permanently
    lost."""
    existing = _find_existing_override_journal_entry(
        session_uuid, lease_id, verified_signal["signal_journal_ref"])
    if existing is not None:
        return existing
    entry = validate_manual_signal_journal_entry({
        "schema_version": MANUAL_SIGNAL_JOURNAL_SCHEMA_VERSION,
        "lease_id": lease_id,
        "claimant_ref": claimant_ref,
        "automation_ref": automation_ref,
        "requested_at": now,
        "signal_journal_ref": verified_signal["signal_journal_ref"],
        "signer_public_key_id": verified_signal["signer_public_key_id"],
        "role": verified_signal["role"],
        "provider_session_id": verified_signal["provider_session_id"],
        "controller_policy_digest": verified_signal["controller_policy_digest"],
        "candidate_digest": verified_signal["candidate_digest"],
    })
    path = manual_signal_journal_path_for(session_uuid)
    if not state_store.append_jsonl_atomic(path, entry):
        raise SchedulerOverrideRecordingFailed(
            lease_id, "append_jsonl_atomic reported failure for %r" % path)
    return entry


def _verify_and_persist_override_evidence(session_uuid, manual_signal_record,
                                          pinned_public_keys, lease_id):
    try:
        return state_store.write_manual_capacity_signal(
            session_uuid, manual_signal_record, pinned_public_keys)
    except state_store.ManualSignalSignatureError as exc:
        raise SchedulerLeaseConflict(
            lease_id, "override_evidence_invalid", detail=str(exc))
    except ValueError as exc:
        raise SchedulerLeaseConflict(
            lease_id, "override_evidence_conflict", detail=str(exc))


def claim_with_authorized_early_override(session_uuid, lease_id, claimant_ref,
                                         now, manual_signal_record,
                                         pinned_public_keys, automation_ref):
    """The SEPARATE verified-evidence early-claim path. `now` is an
    explicit RFC3339 string, required and validated, but -- unlike
    `claim` -- never itself compared against `not_before`: the whole point
    of this function is that already-verified evidence, not a clock
    reading, authorizes bypassing that ordinary refusal.

    Order of checks (D-N02: binding check BEFORE persistence):
      1. `claimant_ref`/`now` well-formedness, lease existence,
         `automation_ref` match.
      2. `manual_signal_record` SHAPE-validated (pure, no I/O, via
         `cowork_capacity.validate_manual_capacity_signal`) and its binding
         identity (role/provider_session_id/controller_policy_digest/
         candidate_digest) compared against the lease's own EXACTLY --
         `reason='override_binding_mismatch'` on any mismatch. This runs
         BEFORE any durable write of the evidence.
      3. D-N01 single-use/fresh check: refuses (`reason=
         'override_evidence_already_used'`) if this exact
         `signal_journal_ref` is already durably linked (in this module's
         own manual-signal journal) to a DIFFERENT lease_id -- the same
         evidence can never authorize two different leases. Reusing it for
         THIS SAME lease_id is the documented idempotent-retry/repair case
         below, not reuse.
      4. Same-owner-idempotent / different-owner-conflict / not-claimable,
         exactly like `claim`. The idempotent path additionally REPAIRS a
         crash-truncated journal entry (D-MJ-03) rather than returning
         early with the attribution silently missing.
      5. GENUINE Ed25519 cryptographic verification (never a plaintext
         "authorized" boolean) against the caller's pinned public-key
         registry, via Package B's own `write_manual_capacity_signal`
         (write-once; durably persists the verified record).
      6. Delegates the mutation to `cowork_state.claim_pause_lease` (same
         genuine cross-process race handling as `claim`).
      7. Appends the durable, distinct manual-signal-journal linkage entry
         (idempotent/crash-repairable, see `_record_manual_signal_override`)
         -- raising `SchedulerOverrideRecordingFailed` (the underlying
         claim is NOT rolled back) only if THIS append itself fails.

    Returns `{"outcome": "claimed_via_override"|"already_claimed", "lease":
    ..., "override_record": ...}`."""
    if not isinstance(claimant_ref, str) or not claimant_ref:
        raise ValueError("claimant_ref must be a nonempty string")
    now_epoch = capacity.rfc3339_to_epoch_seconds(now)
    if now_epoch is None:
        raise ValueError(
            "now must be an RFC3339-shaped timestamp string, got %r" % (now,))

    stored, lease_ = _read_canonical_lease(session_uuid, lease_id)
    if lease_ is None:
        raise SchedulerLeaseConflict(lease_id, "not_found")
    _check_automation_ref(lease_id, lease_, automation_ref)

    try:
        shape_checked = capacity.validate_manual_capacity_signal(manual_signal_record)
    except ValueError as exc:
        raise SchedulerLeaseConflict(
            lease_id, "override_evidence_invalid", detail=str(exc))
    for field in ("role", "provider_session_id", "controller_policy_digest",
                 "candidate_digest"):
        if shape_checked[field] != lease_[field]:
            raise SchedulerLeaseConflict(
                lease_id, "override_binding_mismatch", field=field,
                expected=lease_[field], got=shape_checked[field])

    signal_journal_ref = shape_checked["signal_journal_ref"]
    for entry in read_manual_signal_journal(session_uuid):
        if (entry.get("signal_journal_ref") == signal_journal_ref
                and entry.get("lease_id") != lease_id):
            raise SchedulerLeaseConflict(
                lease_id, "override_evidence_already_used",
                other_lease_id=entry.get("lease_id"))

    state = lease_["consumption_state"]
    if state == "claimed":
        if stored.get("claimant_ref") == claimant_ref:
            verified_signal = _verify_and_persist_override_evidence(
                session_uuid, manual_signal_record, pinned_public_keys, lease_id)
            override_record = _record_manual_signal_override(
                session_uuid, lease_id=lease_id, claimant_ref=claimant_ref,
                automation_ref=automation_ref, now=now,
                verified_signal=verified_signal)
            return {"outcome": "already_claimed", "lease": stored,
                   "override_record": override_record}
        raise SchedulerLeaseConflict(
            lease_id, "different_owner_conflict",
            claimant_ref=stored.get("claimant_ref"))
    if state != "unclaimed":
        raise SchedulerLeaseConflict(lease_id, "not_claimable", state=state)

    verified_signal = _verify_and_persist_override_evidence(
        session_uuid, manual_signal_record, pinned_public_keys, lease_id)

    try:
        rec = state_store.claim_pause_lease(session_uuid, lease_id, claimant_ref)
    except state_store.PauseLeaseConflict as exc:
        result = _resolve_claim_race(session_uuid, lease_id, claimant_ref, exc)
        override_record = _record_manual_signal_override(
            session_uuid, lease_id=lease_id, claimant_ref=claimant_ref,
            automation_ref=automation_ref, now=now, verified_signal=verified_signal)
        result["override_record"] = override_record
        return result

    override_record = _record_manual_signal_override(
        session_uuid, lease_id=lease_id, claimant_ref=claimant_ref,
        automation_ref=automation_ref, now=now, verified_signal=verified_signal)

    return {"outcome": "claimed_via_override", "lease": rec,
           "override_record": override_record}


# --------------------------------------------------------------------------- #
# cancel / mark_consumed / replace / start_new_episode.                      #
# --------------------------------------------------------------------------- #


def cancel(session_uuid, lease_id, automation_ref):
    """Thin decision wrapper over `cowork_state.cancel_pause_lease`: checks
    `automation_ref` (D-N04: REQUIRED) against the stored lease first, then
    delegates the mutation. NOT idempotent -- mirrors Package B's own
    `cancel_pause_lease` exactly: a second cancel of an already-terminal
    lease is `SchedulerLeaseConflict(reason='not_cancellable')`, never a
    silent no-op."""
    stored, lease_ = _read_canonical_lease(session_uuid, lease_id)
    if lease_ is None:
        raise SchedulerLeaseConflict(lease_id, "not_found")
    _check_automation_ref(lease_id, lease_, automation_ref)
    try:
        rec = state_store.cancel_pause_lease(session_uuid, lease_id)
    except state_store.PauseLeaseConflict as exc:
        raise SchedulerLeaseConflict(lease_id, exc.reason, state=exc.state)
    return {"outcome": "cancelled", "lease": rec}


def mark_consumed(session_uuid, lease_id, automation_ref):
    """Thin decision wrapper over `cowork_state.mark_pause_lease_consumed`:
    checks `automation_ref` (D-N04: REQUIRED, when the lease still exists)
    first, then delegates. IDEMPOTENT -- mirrors Package B's own
    idempotent-consume contract exactly: consuming an already-`consumed`
    lease again is a harmless no-op, never a conflict.

    D-N07: `idempotent` is computed FROM THE LOCKED RESULT, not from the
    pre-lock read alone -- it is True only when the RETURNED record's own
    `consumed_at` exactly matches what this call already observed on its
    pre-lock read, i.e. nothing changed between that read and the locked
    call. If a THIRD PARTY consumed the lease in the intervening window,
    this now correctly reports `idempotent=False` (this call did not
    itself confirm a pre-existing no-op it had already observed) rather
    than trusting the stale pre-read snapshot alone."""
    stored, lease_ = _read_canonical_lease(session_uuid, lease_id)
    if lease_ is not None:
        _check_automation_ref(lease_id, lease_, automation_ref)
    try:
        rec = state_store.mark_pause_lease_consumed(session_uuid, lease_id)
    except state_store.PauseLeaseConflict as exc:
        raise SchedulerLeaseConflict(lease_id, exc.reason, state=exc.state)
    idempotent = (stored is not None
                 and stored.get("consumption_state") == "consumed"
                 and stored.get("consumed_at") == rec.get("consumed_at"))
    return {"outcome": "consumed", "lease": rec, "idempotent": idempotent}


def replace(session_uuid, old_lease_id, new_pause_lease, automation_ref):
    """Thin decision wrapper over `cowork_state.replace_pause_lease`:
    checks `automation_ref` (D-N04: REQUIRED) against the OLD lease first,
    then delegates the entire replacement -- same-binding enforcement,
    monotonic `failed_wake_attempts` carry-forward, and lock ordering are
    ALL Package B's, unchanged. `cowork_state.CrossBindingReplacementError`
    and plain `ValueError` (malformed shape) propagate through unchanged
    (structural errors, not business conflicts); only Package B's own
    `PauseLeaseConflict` is translated to `SchedulerLeaseConflict`.

    A genuinely terminal old lease (`consumed`/`cancelled`/already
    `replaced`) is `reason='not_replaceable'` here exactly as it is in
    Package B -- this function NEVER falls back to minting a fresh,
    independent episode for it; that is `start_new_episode`'s distinct
    job, never silently substituted for a replacement."""
    stored_old, lease_old = _read_canonical_lease(session_uuid, old_lease_id)
    if lease_old is None:
        raise SchedulerLeaseConflict(old_lease_id, "not_found")
    _check_automation_ref(old_lease_id, lease_old, automation_ref)
    try:
        rec = state_store.replace_pause_lease(
            session_uuid, old_lease_id, new_pause_lease)
    except state_store.PauseLeaseConflict as exc:
        raise SchedulerLeaseConflict(old_lease_id, exc.reason, state=exc.state)
    return {"outcome": "replaced", "lease": rec}


_TERMINAL_NON_LIVE_STATES = ("consumed", "cancelled", "expired")


def start_new_episode(session_uuid, pause_lease):
    """Start a BRAND NEW, independent pause episode for a binding whose
    prior lease is genuinely terminal -- a thin, distinctly named wrapper
    over `cowork_state.create_pause_lease` (Package B's own binding-live
    check refuses this outright if the binding still resolves to a LIVE
    lease). Deliberately a SEPARATE function from `replace`: an episode
    reset must never masquerade as a replacement, and this function's name
    alone makes that distinction impossible to blur at a call site.

    D-MJ-01: the M3R-N06 `failed_wake_attempts` ceiling is enforced PER
    BINDING across this reset boundary, not merely per lease_id. Before
    delegating to `create_pause_lease`, this reads the binding's most
    recently resolved lease via `resolve_current_lease_for_binding`
    (D-N12's read-only seam); if that prior lease is genuinely terminal
    (`consumed`/`cancelled`/`expired`) AND had already reached
    `cowork_capacity.FAILED_WAKE_ATTEMPT_CEILING`, this refuses
    (`reason='binding_ceiling_exhausted'`, writing nothing) rather than
    silently minting a fresh, zero-attempt episode -- closing the loophole
    where `reclaim_if_expired` (expire) followed by `start_new_episode`
    would otherwise reset a ceiling-exhausted binding's automatic-retry
    counter back to zero. A binding whose prior lease is still LIVE
    (`unclaimed`/`claimed`) is refused by Package B's own
    `binding_already_live` check instead, unchanged."""
    validated = capacity.validate_pause_lease(pause_lease)
    prior = resolve_current_lease_for_binding(session_uuid, validated)
    if (prior is not None
            and prior.get("consumption_state") in _TERMINAL_NON_LIVE_STATES
            and prior.get("failed_wake_attempts", 0) >= capacity.FAILED_WAKE_ATTEMPT_CEILING):
        raise SchedulerLeaseConflict(
            validated["lease_id"], "binding_ceiling_exhausted",
            blocking_lease_id=prior.get("lease_id"),
            failed_wake_attempts=prior.get("failed_wake_attempts"))
    return {"outcome": "episode_started",
           "lease": state_store.create_pause_lease(session_uuid, pause_lease)}


# --------------------------------------------------------------------------- #
# Truthful expiry / reclaim.                                                  #
# --------------------------------------------------------------------------- #

_PAUSE_LEASE_EXPIRABLE_STATES = ("unclaimed",)


def reclaim_if_expired(session_uuid, lease_id, now, expiry_after_seconds,
                       automation_ref, reference_now=None,
                       max_clock_skew_seconds=DEFAULT_MAX_CLOCK_SKEW_SECONDS):
    """TRUTHFUL expiry decision: never reports (or durably records) a lease
    as expired before it has genuinely, explicitly been shown to be due.
    `automation_ref` (D-N04) is REQUIRED and checked against the stored
    lease first.

    D-MJ-04: applies ONLY to a still-`unclaimed` lease. A `claimed` lease
    is refused outright (`reason='not_expirable', state='claimed'`) rather
    than anchoring its expiry on `not_before`/`issued_at` -- a claimant may
    legitimately claim long after `not_before`, and anchoring on either
    would falsely report an actively-held lease as expired the instant it
    was claimed. A stuck claimed lease is not this function's business;
    use `cancel` instead.

    The expiry anchor is `not_before` for a `scheduled`-mode lease, or
    `issued_at` for a `manual_signal`-mode lease (which has no
    `not_before`) -- plus the caller-supplied `expiry_after_seconds` grace
    window. When the effective clock (see `resolve_effective_now_epoch`,
    same conservative skew handling as `claim`) has not yet reached
    `anchor + expiry_after_seconds`, this returns `{"outcome":
    "not_yet_expired", ...}` and mutates NOTHING -- it never fabricates an
    early expiry. Only once genuinely due does it delegate to
    `cowork_state.mark_pause_lease_expired`. Every returned dict carries
    `"clock_skew_detected"` (D-N05).

    Raises `SchedulerLeaseConflict` when the lease does not exist
    (`reason='not_found'`) or is not currently `unclaimed`
    (`reason='not_expirable'`) -- reclaim never applies to a
    claimed/already-consumed/cancelled/replaced/expired lease.

    NOTE: reclaiming (expiring) a lease never itself resets any counter --
    `failed_wake_attempts` lives on the (now terminal) lease record
    unchanged. The ceiling-per-binding enforcement that matters across this
    boundary lives in `start_new_episode` (D-MJ-01), the actual reset
    point."""
    if not isinstance(expiry_after_seconds, (int, float)) or isinstance(
            expiry_after_seconds, bool) or expiry_after_seconds < 0:
        raise ValueError(
            "expiry_after_seconds must be a nonnegative number, got %r"
            % (expiry_after_seconds,))
    stored, lease_ = _read_canonical_lease(session_uuid, lease_id)
    if lease_ is None:
        raise SchedulerLeaseConflict(lease_id, "not_found")
    _check_automation_ref(lease_id, lease_, automation_ref)
    if lease_["consumption_state"] not in _PAUSE_LEASE_EXPIRABLE_STATES:
        raise SchedulerLeaseConflict(
            lease_id, "not_expirable", state=lease_["consumption_state"])

    anchor = lease_["not_before"] if lease_["resume_mode"] == "scheduled" else lease_["issued_at"]
    anchor_epoch = capacity.rfc3339_to_epoch_seconds(anchor)
    expiry_epoch = anchor_epoch + expiry_after_seconds

    effective_now_epoch, skewed = resolve_effective_now_epoch(
        now, reference_now, max_clock_skew_seconds)
    if effective_now_epoch < expiry_epoch:
        return {"outcome": "not_yet_expired", "lease": stored,
               "expiry_epoch": expiry_epoch, "clock_skew_detected": skewed}

    try:
        rec = state_store.mark_pause_lease_expired(session_uuid, lease_id)
    except state_store.PauseLeaseConflict as exc:
        raise SchedulerLeaseConflict(lease_id, exc.reason, state=exc.state)
    return {"outcome": "expired", "lease": rec, "clock_skew_detected": skewed}


# --------------------------------------------------------------------------- #
# Wake-attempt bookkeeping (thin wrappers; M3R-N06 ceiling lives in          #
# Package A/B, unchanged).                                                   #
# --------------------------------------------------------------------------- #


def wake_decision(session_uuid, lease_id):
    """Read-side pass-through to `cowork_state.pause_lease_wake_decision`
    -- `'wake_retry_eligible'` or `'wake_attempts_exhausted'`. Raises
    ValueError when the lease does not exist or is no longer schema-valid;
    mutates nothing."""
    return state_store.pause_lease_wake_decision(session_uuid, lease_id)


def record_failed_wake_attempt(session_uuid, lease_id, automation_ref):
    """Thin decision wrapper over
    `cowork_state.record_pause_lease_failed_wake_attempt`. `automation_ref`
    (D-N04) is REQUIRED and checked against the stored lease first (when it
    exists). Raises `SchedulerLeaseConflict` when the lease does not exist
    (`reason='not_found'`), is not currently `unclaimed`
    (`reason='not_unclaimed'`), or has already reached
    `cowork_capacity.FAILED_WAKE_ATTEMPT_CEILING`
    (`reason='ceiling_exhausted'`) -- a caller must consult `wake_decision`
    first and, once exhausted, use `replace` for a fresh lease rather than
    calling this again."""
    stored, lease_ = _read_canonical_lease(session_uuid, lease_id)
    if lease_ is not None:
        _check_automation_ref(lease_id, lease_, automation_ref)
    try:
        rec = state_store.record_pause_lease_failed_wake_attempt(
            session_uuid, lease_id)
    except state_store.PauseLeaseConflict as exc:
        raise SchedulerLeaseConflict(lease_id, exc.reason, state=exc.state)
    return {"outcome": "failed_wake_attempt_recorded", "lease": rec}


def _record_failed_wake_attempt_best_effort(session_uuid, lease_id, automation_ref):
    """Best-effort, SINGLE call site for durably counting a genuine
    operational wake failure (see `run_wake_trigger`'s OSError handling,
    D-MJ-02) -- swallows every exception (the lease may already be
    claimed/terminal/ceiling-exhausted by the time this runs, or a SECOND
    genuine I/O failure may hit the counter write itself) so a secondary
    failure here never masks or replaces the internal_error already being
    reported for the ORIGINAL failure, and is never itself a source of a
    second increment."""
    try:
        record_failed_wake_attempt(session_uuid, lease_id, automation_ref)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Wake-trigger contract: concrete, versioned CLI arguments and exit codes    #
# Packages E and F can consume independently.                                #
# --------------------------------------------------------------------------- #

WAKE_TRIGGER_CONTRACT_VERSION = 2

WAKE_TRIGGER_EXIT_SUCCESS = 0
WAKE_TRIGGER_EXIT_INTERNAL_ERROR = 1
WAKE_TRIGGER_EXIT_INVALID_ARGUMENTS = 2
WAKE_TRIGGER_EXIT_NOT_DUE = 3
WAKE_TRIGGER_EXIT_CONFLICT = 4
WAKE_TRIGGER_EXIT_ATTEMPTS_EXHAUSTED = 5

# The concrete, versioned outcome-name -> exit-code contract. Packages E and
# F must consult THIS mapping by name (never a bare literal integer) so a
# future contract version can extend it without breaking either consumer.
WAKE_TRIGGER_EXIT_CODES = {
    "success": WAKE_TRIGGER_EXIT_SUCCESS,
    "internal_error": WAKE_TRIGGER_EXIT_INTERNAL_ERROR,
    "invalid_arguments": WAKE_TRIGGER_EXIT_INVALID_ARGUMENTS,
    "not_due": WAKE_TRIGGER_EXIT_NOT_DUE,
    "conflict": WAKE_TRIGGER_EXIT_CONFLICT,
    "attempts_exhausted": WAKE_TRIGGER_EXIT_ATTEMPTS_EXHAUSTED,
}


def build_wake_trigger_arg_parser():
    """Build the versioned wake-trigger argument parser (see
    `WAKE_TRIGGER_CONTRACT_VERSION`). Every argument that could vary with
    time is explicit and required -- `--now` is never defaulted from the
    real clock. `--max-jitter-seconds` (v2, D-N08) bounds the deterministic
    `next_wake_epoch` hint exposed on a `not_due` result."""
    parser = argparse.ArgumentParser(
        prog="cowork_capacity_scheduler.wake_trigger", add_help=True,
        description=(
            "M3 Package D versioned wake-trigger contract (v%d): attempt "
            "one explicit-clock claim decision for one PauseLease."
            % WAKE_TRIGGER_CONTRACT_VERSION))
    parser.add_argument("--session-uuid", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--claimant-ref", required=True)
    parser.add_argument("--automation-ref", required=True)
    parser.add_argument(
        "--now", required=True,
        help="explicit RFC3339 clock reading; never read from the wall clock")
    parser.add_argument(
        "--reference-now", default=None,
        help="optional independent RFC3339 reference clock reading, for "
             "clock-skew detection")
    parser.add_argument(
        "--max-clock-skew-seconds", type=float,
        default=DEFAULT_MAX_CLOCK_SKEW_SECONDS)
    parser.add_argument(
        "--max-jitter-seconds", type=float, default=DEFAULT_MAX_JITTER_SECONDS,
        help="bound for the deterministic jitter applied to the exposed "
             "next_wake_epoch hint on a not_due result")
    return parser


def _write_internal_error(write, lease_id, exc):
    write(json.dumps({
        "outcome": "internal_error", "lease_id": lease_id,
        "detail": str(exc)}) + "\n")
    return WAKE_TRIGGER_EXIT_INTERNAL_ERROR


def run_wake_trigger(argv, output=None):
    """Versioned wake-trigger entrypoint (see `WAKE_TRIGGER_CONTRACT_VERSION`):
    parses `argv` per `build_wake_trigger_arg_parser`, writes exactly one
    JSON result line to `output` (a single-argument callable; defaults to
    `sys.stdout.write`, so tests may inject their own capture instead of
    touching real stdout), and returns one of `WAKE_TRIGGER_EXIT_CODES` --
    NEVER calls `sys.exit` itself, so it is safe to call repeatedly,
    including from tests, without terminating the process.

    D-MJ-02 flow: (1) a malformed `argv` (argparse raises `SystemExit`) is
    `invalid_arguments`; (2) `wake_decision` is consulted FIRST -- an
    already-ceiling-exhausted lease returns `attempts_exhausted` WITHOUT
    ever attempting a claim (a missing/invalid lease at this step is
    reported as a `not_found` conflict); (3) otherwise a `claim` is
    attempted -- `early_refusal` maps to `not_due` (D-N08: with a
    deterministic jittered `next_wake_epoch` hint included), every other
    `SchedulerLeaseConflict` maps to `conflict`, a malformed-input
    `ValueError` maps to `invalid_arguments`, and a genuine lock/I/O
    failure (`OSError`, D-N06) maps to ONE consistent `internal_error` JSON
    shape -- which ALSO durably counts as one failed wake attempt
    (D-MJ-02, via the single `_record_failed_wake_attempt_best_effort` call
    site, never double counted); (4) success carries the resulting
    `outcome` and `clock_skew_detected` (D-N05)."""
    write = output if output is not None else sys.stdout.write
    parser = build_wake_trigger_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return WAKE_TRIGGER_EXIT_INVALID_ARGUMENTS

    try:
        decision = wake_decision(args.session_uuid, args.lease_id)
    except ValueError:
        write(json.dumps({
            "outcome": "conflict", "lease_id": args.lease_id,
            "reason": "not_found"}) + "\n")
        return WAKE_TRIGGER_EXIT_CONFLICT
    except OSError as exc:
        return _write_internal_error(write, args.lease_id, exc)
    if decision == "wake_attempts_exhausted":
        write(json.dumps({
            "outcome": "attempts_exhausted", "lease_id": args.lease_id}) + "\n")
        return WAKE_TRIGGER_EXIT_ATTEMPTS_EXHAUSTED

    try:
        result = claim(
            args.session_uuid, args.lease_id, args.claimant_ref, args.now,
            args.automation_ref, reference_now=args.reference_now,
            max_clock_skew_seconds=args.max_clock_skew_seconds)
    except SchedulerLeaseConflict as exc:
        payload = {"outcome": "conflict", "lease_id": exc.lease_id,
                  "reason": exc.reason}
        if exc.reason == "early_refusal":
            not_before_epoch = capacity.rfc3339_to_epoch_seconds(
                exc.details["not_before"])
            payload["next_wake_epoch"] = next_scheduled_wake_epoch(
                not_before_epoch, "%s:%s" % (args.lease_id, args.now),
                args.max_jitter_seconds)
            write(json.dumps(payload) + "\n")
            return WAKE_TRIGGER_EXIT_NOT_DUE
        write(json.dumps(payload) + "\n")
        return WAKE_TRIGGER_EXIT_CONFLICT
    except ValueError as exc:
        write(json.dumps({
            "outcome": "invalid_arguments", "detail": str(exc)}) + "\n")
        return WAKE_TRIGGER_EXIT_INVALID_ARGUMENTS
    except OSError as exc:
        _record_failed_wake_attempt_best_effort(
            args.session_uuid, args.lease_id, args.automation_ref)
        return _write_internal_error(write, args.lease_id, exc)

    write(json.dumps({
        "outcome": result["outcome"], "lease_id": args.lease_id,
        "clock_skew_detected": result.get("clock_skew_detected", False)}) + "\n")
    return WAKE_TRIGGER_EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(run_wake_trigger(sys.argv[1:]))
