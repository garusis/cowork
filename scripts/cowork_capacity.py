#!/usr/bin/env python3
"""Provider-capacity contracts — M3 Package A.

Pure/versioned data contracts for everything later M3 packages consume when
provider capacity is exhausted: the closed `ControllerOutcome` taxonomy, the
`CapacityPacket` and `PauseLease` schemas (per
`references/artifact-contract-schema.md`'s `capacity.json` shape), the
`InvalidationRecord` schema (issue #34), the signed manual-capacity-signal
record schema, a pure trust-source classifier, and pure reset/retry-after
text-shape parsing plus canonical RFC3339 comparison. This module is inert
infrastructure: it performs no file, socket, process, or wall-clock access,
spawns nothing, and imports no runtime module — every value that could vary
with time (a "now") is an explicit caller-supplied input, never read from
the environment.

Public API:
    CONTROLLER_OUTCOMES, CONTROLLER_OUTCOME_SET
    CAPACITY_ELIGIBLE_OUTCOMES, NON_CAPACITY_TERMINAL_OUTCOMES
    TRUST_SOURCE_KINDS
    CONSUMPTION_STATES, CONSUMPTION_STATE_SET
    RESUME_MODES, RESUME_MODE_SET
    PROVIDER_CAPACITY_CLASSES
    FAILED_WAKE_ATTEMPT_CEILING
    MAX_RETRY_HORIZON_SECONDS
    PAUSE_UNTIL_ELIGIBLE_EQUIVALENCE
    classify_trust_source(kind) -> "trustworthy" | "untrustworthy"
    parse_retry_after_text(raw) -> dict or None
    rfc3339_to_epoch_seconds(text) -> float or None
    validate_capacity_source(record) -> dict (normalized) or raises ValueError
    validate_capacity_packet(record) -> dict (normalized) or raises ValueError
    validate_pause_lease(record) -> dict (normalized) or raises ValueError
    validate_invalidation_record(record) -> dict (normalized) or raises ValueError
    validate_manual_capacity_signal(record) -> dict (normalized) or raises ValueError
    is_pause_until_eligible(capacity_packet=None, pause_lease=None) -> bool
    record_failed_wake_attempt(pause_lease) -> dict (normalized, incremented)
    next_pause_lease_after_replacement(old_lease, new_lease) -> dict (normalized)
    wake_attempts_exhausted(pause_lease) -> bool
    capacity_wake_decision(pause_lease) -> "wake_retry_eligible" | "wake_attempts_exhausted"

Naming note: this module's persisted `CapacityPacket`/`PauseLease` records
use the canonical `artifact-contract-schema.md` field name `candidate_digest`
(no separate index) for the bound candidate. `cowork_control_plane.py`'s
in-memory reducer *evidence* dicts instead reuse the WorkUnit-consistent
`candidate_manifest_digest`/`candidate_index` pair already established by
M2's `_gate_evidence_valid` — two different consumers of the same underlying
identity, each following its own file's pre-existing convention.
"""

import re

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Closed ControllerOutcome taxonomy
# ---------------------------------------------------------------------------
#
# `malformed_output` is reserved for a SUCCESSFUL turn that returned bad
# JSON; `unknown_provider_failure` is reserved for a FAILED turn whose raw
# shape no classifier recognizes. The two are deliberately distinct members
# so a parse failure on a successful turn is never conflated with a raw,
# unclassifiable provider failure.
CONTROLLER_OUTCOMES = (
    "quota_limited",
    "overloaded",
    "authentication_failed",
    "policy_blocked",
    "guard_unavailable",
    "transport_failed",
    "malformed_output",
    "local_guard_exhausted",
    "unknown_provider_failure",
)
CONTROLLER_OUTCOME_SET = frozenset(CONTROLLER_OUTCOMES)

# The only two outcomes that may ever justify entering or resuming provider
# capacity (a genuine provider-side quota/rate signal). Every other outcome
# — including, by name, `local_guard_exhausted` (a LOCAL guard/budget stop,
# never provider-supplied evidence) and `unknown_provider_failure` (an
# unrecognized raw shape carrying no automatic-retry permission) — is
# terminal-non-capacity and can never enter or resume capacity.
CAPACITY_ELIGIBLE_OUTCOMES = frozenset({"quota_limited", "overloaded"})

# Named per the frozen brief: these two ControllerOutcome members are
# reachable in the taxonomy but must never be treated as capacity evidence.
NON_CAPACITY_TERMINAL_OUTCOMES = frozenset({
    "local_guard_exhausted", "unknown_provider_failure",
})


def validate_controller_outcome(value):
    """True only for a member of the closed CONTROLLER_OUTCOME_SET."""
    return isinstance(value, str) and value in CONTROLLER_OUTCOME_SET


# ---------------------------------------------------------------------------
# Trust-source classification (closed set, fail-closed)
# ---------------------------------------------------------------------------

# The only trustworthy capacity-evidence source kinds. Anything else —
# "unknown", absent, malformed, or any value never enumerated here —
# classifies untrustworthy. This is total: classify_trust_source never
# raises, so a malformed/absent `kind` degrades to "untrustworthy" rather
# than crashing a caller that has not yet validated the record shape.
TRUST_SOURCE_KINDS = frozenset({"provider_event", "provider_header", "provider_api"})


def classify_trust_source(kind):
    """Pure function of `kind` alone: "trustworthy" iff `kind` is exactly one
    of TRUST_SOURCE_KINDS, else "untrustworthy" — including None, an empty
    string, an unhashable type (list/dict), or any string outside the closed
    set. Never inferred from any other field; never defaults to trustworthy
    on an unrecognized value."""
    if isinstance(kind, str) and kind in TRUST_SOURCE_KINDS:
        return "trustworthy"
    return "untrustworthy"


# ---------------------------------------------------------------------------
# Canonical RFC3339 parsing/comparison (pure; no wall-clock access)
# ---------------------------------------------------------------------------
#
# Two RFC3339 strings that name the same instant are not always byte-equal
# (".100" vs ".1" fractional seconds, "Z" vs "+00:00", or two different but
# equivalent offsets) — a raw lexicographic string comparison of two such
# strings can give the WRONG ordering. Every comparison in this module (and
# duplicated, for the same stdlib-only-independence reason as
# CAPACITY_ELIGIBLE_OUTCOMES, in cowork_control_plane.py's
# `_capacity_wake_evidence_valid`) goes through `rfc3339_to_epoch_seconds`
# instead, never through `<=`/`<`/`==` on the raw strings.

_RFC3339_RE = re.compile(
    r'^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T'
    r'(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(?P<frac>\.\d+)?'
    r'(?P<offset>Z|[+-]\d{2}:\d{2})$')
_DURATION_SECONDS_RE = re.compile(r'^\d+(\.\d+)?s?$')


def _is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _days_in_month(year, month):
    if month == 2 and _is_leap_year(year):
        return 29
    return _DAYS_IN_MONTH[month - 1]


def _days_from_civil(year, month, day):
    """Howard Hinnant's days-from-civil algorithm: proleptic-Gregorian
    (year, month, day) -> days since the Unix epoch (1970-01-01). Pure
    integer arithmetic; no imports, no library calendar support needed."""
    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def rfc3339_to_epoch_seconds(text):
    """Pure, canonical RFC3339 -> UTC epoch-seconds (float) conversion, or
    None when `text` is not a well-formed RFC3339 timestamp (wrong type,
    malformed shape, or a calendrically invalid date/time — e.g. month 13
    or a February 30th). Normalizes fractional seconds and any
    Z/+HH:MM/-HH:MM offset to one comparable UTC instant: two textually
    different but instant-equal timestamps compare equal via the returned
    numbers. No wall-clock access — purely a function of `text`."""
    if not isinstance(text, str):
        return None
    m = _RFC3339_RE.match(text)
    if not m:
        return None
    year = int(m.group("year"))
    month = int(m.group("month"))
    day = int(m.group("day"))
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    second = int(m.group("second"))
    frac = m.group("frac")
    frac_seconds = float(frac) if frac else 0.0
    if not (1 <= month <= 12):
        return None
    if not (1 <= day <= _days_in_month(year, month)):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 60):
        return None  # :60 permitted only for a leap second's textual shape
    offset = m.group("offset")
    if offset == "Z":
        offset_seconds = 0
    else:
        sign = 1 if offset[0] == "+" else -1
        offset_seconds = sign * (int(offset[1:3]) * 3600 + int(offset[4:6]) * 60)
    days = _days_from_civil(year, month, day)
    return days * 86400 + hour * 3600 + minute * 60 + second + frac_seconds - offset_seconds


# Reset/retry evidence, CapacityPacket.retry_after, and PauseLease.not_before
# are all refused beyond this horizon relative to issued_at: a
# subscription-quota reset legitimately claiming more than 7 days out is not
# a genuine short-term capacity signal — fail closed on an implausible (or
# malicious) horizon rather than schedule a speculative far-future wake or
# accept an unbounded duration string.
MAX_RETRY_HORIZON_SECONDS = 7 * 24 * 3600  # 604800


def parse_retry_after_text(raw):
    """Parse a provider-supplied retry-after/reset text shape.

    Returns `{"kind": "timestamp", "value": raw}` when `raw` is an
    RFC3339-shaped timestamp string, `{"kind": "duration_seconds", "value":
    <float>}` when `raw` is a bare or `s`-suffixed nonnegative number of
    seconds no larger than MAX_RETRY_HORIZON_SECONDS, or None for anything
    else (missing, wrong type, malformed text, or a duration beyond the
    horizon) — malformed/out-of-bound text classifies as unparseable, it
    never raises. Timestamp-kind results are NOT horizon-checked here (this
    function has no `issued_at` reference to check them against); callers
    validating a CapacityPacket/PauseLease enforce that bound themselves via
    `rfc3339_to_epoch_seconds`. This checks TEXT SHAPE only; it performs no
    calendar/timezone math for duration values and reads no clock."""
    if not isinstance(raw, str) or not raw:
        return None
    if _RFC3339_RE.match(raw):
        return {"kind": "timestamp", "value": raw}
    if _DURATION_SECONDS_RE.match(raw):
        seconds = float(raw[:-1] if raw.endswith("s") else raw)
        if seconds > MAX_RETRY_HORIZON_SECONDS:
            return None
        return {"kind": "duration_seconds", "value": seconds}
    return None


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------

_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


def _check_exact_keys(record, expected_keys, record_name):
    extra = set(record) - expected_keys
    missing = expected_keys - set(record)
    if missing:
        raise ValueError("%s missing keys: %s" % (record_name, sorted(missing)))
    if extra:
        raise ValueError("%s has extra keys: %s" % (record_name, sorted(extra)))


def _check_schema_version(record):
    v = record.get("schema_version")
    if isinstance(v, bool) or v != SCHEMA_VERSION:
        raise ValueError("schema_version must be integer %d, got %r" % (SCHEMA_VERSION, v))


def _check_nonempty_str(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a nonempty string, got %r" % (field, value))


def _check_str_or_null(value, field):
    if value is not None and not isinstance(value, str):
        raise ValueError("%s must be a string or null, got %r" % (field, value))


def _check_hex64(value, field):
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        raise ValueError("%s must be 64 lowercase hex chars, got %r" % (field, value))


def _check_nonneg_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("%s must be a nonnegative integer, got %r" % (field, value))


def _check_rfc3339(value, field):
    if not isinstance(value, str) or not _RFC3339_RE.match(value):
        raise ValueError("%s must be an RFC3339-shaped timestamp string, got %r" % (field, value))


def _check_artifact_hashes(value, field):
    if not isinstance(value, dict) or not value:
        raise ValueError("%s must be a nonempty dict, got %r" % (field, value))
    for name, digest in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError("%s has a non-string/empty key: %r" % (field, name))
        _check_hex64(digest, "%s[%r]" % (field, name))


def _check_within_retry_horizon(issued_epoch, target_epoch, field):
    """Shared horizon-bound check: `target_epoch` must be at or after
    `issued_epoch` and no more than MAX_RETRY_HORIZON_SECONDS beyond it —
    both already-canonical epoch-seconds values (never raw strings)."""
    if issued_epoch is None or target_epoch is None:
        raise ValueError(
            "%s: issued_at/target must both be canonical RFC3339 timestamps" % field)
    delta = target_epoch - issued_epoch
    if delta < 0:
        raise ValueError("%s must not precede issued_at" % field)
    if delta > MAX_RETRY_HORIZON_SECONDS:
        raise ValueError(
            "%s exceeds MAX_RETRY_HORIZON_SECONDS=%d beyond issued_at"
            % (field, MAX_RETRY_HORIZON_SECONDS))


# ---------------------------------------------------------------------------
# CapacityPacket
# ---------------------------------------------------------------------------

RESUME_MODES = ("scheduled", "manual_signal")
RESUME_MODE_SET = frozenset(RESUME_MODES)

# `subscription_only` backend per the frozen brief: no credits, overage,
# alias, or fallback capacity class exists to name.
PROVIDER_CAPACITY_CLASSES = frozenset({"subscription_quota_exhausted"})

_CAPACITY_SOURCE_KEYS = frozenset({"kind", "sha256"})
_BINDING_KEYS = frozenset({
    "role", "provider_session_id", "controller_policy_digest",
    "candidate_digest", "artifact_hashes",
})
_WAKEUP_KEYS = frozenset({"lease_id", "automation_ref", "not_before"})
_MANUAL_RESUME_KEYS = frozenset({"condition", "accepted_source", "signal_journal_ref"})
_MANUAL_RESUME_ACCEPTED_SOURCES = frozenset({
    "external_application", "top_level_authority_adapter",
})
_CAPACITY_PACKET_KEYS = frozenset({
    "schema_version", "package_id", "provider_capacity_class", "provider",
    "resume_mode", "retry_after", "capacity_source", "binding", "wakeup",
    "manual_resume", "issued_at",
})


def validate_capacity_source(record):
    """Return a normalized copy of a `capacity_source` record, or raise
    ValueError. Shape only — `kind`'s trustworthiness is a separate question
    answered by `classify_trust_source`, never by this validator."""
    if not isinstance(record, dict):
        raise ValueError("capacity_source must be a dict, got %r" % type(record))
    _check_exact_keys(record, _CAPACITY_SOURCE_KEYS, "capacity_source")
    _check_nonempty_str(record["kind"], "capacity_source.kind")
    _check_hex64(record["sha256"], "capacity_source.sha256")
    return dict(record)


def _validate_binding(record, field):
    if not isinstance(record, dict):
        raise ValueError("%s must be a dict, got %r" % (field, type(record)))
    _check_exact_keys(record, _BINDING_KEYS, field)
    _check_nonempty_str(record["role"], "%s.role" % field)
    _check_nonempty_str(record["provider_session_id"], "%s.provider_session_id" % field)
    _check_hex64(record["controller_policy_digest"], "%s.controller_policy_digest" % field)
    _check_hex64(record["candidate_digest"], "%s.candidate_digest" % field)
    _check_artifact_hashes(record["artifact_hashes"], "%s.artifact_hashes" % field)
    # Deep-copy the nested artifact_hashes dict: `dict(record)` alone only
    # copies the top level, leaving the inner dict aliased to the caller's
    # own object — a caller mutating the returned normalized copy would
    # otherwise silently mutate the input too.
    normalized = dict(record)
    normalized["artifact_hashes"] = dict(record["artifact_hashes"])
    return normalized


def _validate_wakeup(record, resume_mode, field):
    if not isinstance(record, dict):
        raise ValueError("%s must be a dict, got %r" % (field, type(record)))
    _check_exact_keys(record, _WAKEUP_KEYS, field)
    # automation_ref is explicitly populated always (M3R-N04): a durable
    # scheduler reference exists regardless of resume_mode.
    _check_nonempty_str(record["automation_ref"], "%s.automation_ref" % field)
    if resume_mode == "scheduled":
        _check_nonempty_str(record["lease_id"], "%s.lease_id" % field)
        _check_rfc3339(record["not_before"], "%s.not_before" % field)
    else:
        # manual_signal has no wake lease.
        if record["lease_id"] is not None:
            raise ValueError("%s.lease_id must be null when resume_mode='manual_signal'" % field)
        if record["not_before"] is not None:
            raise ValueError("%s.not_before must be null when resume_mode='manual_signal'" % field)
    return dict(record)


def _validate_manual_resume(record, resume_mode, field):
    if not isinstance(record, dict):
        raise ValueError("%s must be a dict, got %r" % (field, type(record)))
    _check_exact_keys(record, _MANUAL_RESUME_KEYS, field)
    _check_str_or_null(record["condition"], "%s.condition" % field)
    accepted_source = record["accepted_source"]
    if accepted_source is not None and accepted_source not in _MANUAL_RESUME_ACCEPTED_SOURCES:
        raise ValueError(
            "%s.accepted_source must be null or one of %s, got %r"
            % (field, sorted(_MANUAL_RESUME_ACCEPTED_SOURCES), accepted_source))
    _check_str_or_null(record["signal_journal_ref"], "%s.signal_journal_ref" % field)
    if resume_mode == "manual_signal":
        _check_nonempty_str(record["condition"], "%s.condition" % field)
        if accepted_source is None:
            raise ValueError(
                "%s.accepted_source is required when resume_mode='manual_signal'" % field)
    return dict(record)


def validate_capacity_packet(record):
    """Return a normalized copy of a CapacityPacket record, or raise
    ValueError. Matches the canonical `artifact-contract-schema.md`
    `capacity.json` shape exactly, plus the required `schema_version`/
    `package_id` pair every validated artifact carries.

    `binding.artifact_hashes` and `wakeup.automation_ref` are both required,
    non-null, well-shaped fields — never left undeclared (M3R-N04).

    `retry_after` (when `resume_mode == 'scheduled'`) and `wakeup.not_before`
    are both bound to MAX_RETRY_HORIZON_SECONDS relative to `issued_at`,
    compared via canonical `rfc3339_to_epoch_seconds` — never a raw
    lexicographic string comparison, and never accepted unboundedly far in
    the future or before `issued_at`."""
    if not isinstance(record, dict):
        raise ValueError("CapacityPacket must be a dict, got %r" % type(record))
    _check_exact_keys(record, _CAPACITY_PACKET_KEYS, "CapacityPacket")
    _check_schema_version(record)
    _check_nonempty_str(record["package_id"], "package_id")
    _check_rfc3339(record["issued_at"], "issued_at")
    issued_epoch = rfc3339_to_epoch_seconds(record["issued_at"])

    if record["provider_capacity_class"] not in PROVIDER_CAPACITY_CLASSES:
        raise ValueError(
            "provider_capacity_class must be one of %s, got %r"
            % (sorted(PROVIDER_CAPACITY_CLASSES), record["provider_capacity_class"]))
    _check_nonempty_str(record["provider"], "provider")

    resume_mode = record["resume_mode"]
    if resume_mode not in RESUME_MODE_SET:
        raise ValueError(
            "resume_mode must be one of %s, got %r" % (sorted(RESUME_MODE_SET), resume_mode))

    retry_after = record["retry_after"]
    if resume_mode == "scheduled":
        parsed_retry = parse_retry_after_text(retry_after)
        if parsed_retry is None:
            raise ValueError(
                "retry_after is required and must be a well-shaped, in-horizon "
                "timestamp/duration when resume_mode='scheduled', got %r" % (retry_after,))
        if parsed_retry["kind"] == "timestamp":
            retry_epoch = rfc3339_to_epoch_seconds(parsed_retry["value"])
        else:
            retry_epoch = (
                issued_epoch + parsed_retry["value"] if issued_epoch is not None else None)
        _check_within_retry_horizon(issued_epoch, retry_epoch, "retry_after")
    elif retry_after is not None:
        raise ValueError("retry_after must be null when resume_mode='manual_signal'")

    capacity_source = validate_capacity_source(record["capacity_source"])
    binding = _validate_binding(record["binding"], "binding")
    wakeup = _validate_wakeup(record["wakeup"], resume_mode, "wakeup")
    if resume_mode == "scheduled":
        wakeup_epoch = rfc3339_to_epoch_seconds(wakeup["not_before"])
        _check_within_retry_horizon(issued_epoch, wakeup_epoch, "wakeup.not_before")
    manual_resume = _validate_manual_resume(record["manual_resume"], resume_mode, "manual_resume")

    normalized = dict(record)
    normalized["capacity_source"] = capacity_source
    normalized["binding"] = binding
    normalized["wakeup"] = wakeup
    normalized["manual_resume"] = manual_resume
    return normalized


# ---------------------------------------------------------------------------
# PauseLease
# ---------------------------------------------------------------------------

CONSUMPTION_STATES = ("unclaimed", "claimed", "consumed", "cancelled", "replaced", "expired")
CONSUMPTION_STATE_SET = frozenset(CONSUMPTION_STATES)

# M3R-N06: durable, bounded failed-wake-attempt ceiling. Persistence
# (Package B) and the wake decision (Package D) both cite this exact
# constant rather than re-deriving their own magic number, so the bound is
# a single source of truth. Exceeding it stops automatic wake cycling with
# the truthful typed outcome "wake_attempts_exhausted" — see
# capacity_wake_decision — never a silent, unbounded retry.
FAILED_WAKE_ATTEMPT_CEILING = 5

_PAUSE_LEASE_KEYS = frozenset({
    "schema_version", "package_id",
    "lease_id", "role", "provider_session_id", "controller_policy_digest",
    "candidate_digest", "resume_mode", "not_before", "automation_ref",
    "artifact_hashes", "consumption_state", "failed_wake_attempts",
    "issued_at",
})


def validate_pause_lease(record):
    """Return a normalized copy of a PauseLease record, or raise ValueError.

    Carries the same binding/wakeup identity fields as CapacityPacket
    (role, provider_session_id, controller_policy_digest, candidate_digest,
    artifact_hashes, automation_ref), plus `consumption_state`
    (unclaimed/claimed/consumed/cancelled/replaced/expired, initial value
    "unclaimed") and `failed_wake_attempts` — the M3R-N06 durable,
    bounded failed-wake-attempt counter (nonnegative int, initial 0, never
    exceeding FAILED_WAKE_ATTEMPT_CEILING; a value beyond the ceiling is
    refused as malformed rather than silently accepted).

    When `resume_mode == 'scheduled'`, `not_before` is bound to
    MAX_RETRY_HORIZON_SECONDS relative to `issued_at`, compared via
    canonical `rfc3339_to_epoch_seconds` — never a raw string comparison."""
    if not isinstance(record, dict):
        raise ValueError("PauseLease must be a dict, got %r" % type(record))
    _check_exact_keys(record, _PAUSE_LEASE_KEYS, "PauseLease")
    _check_schema_version(record)
    _check_nonempty_str(record["package_id"], "package_id")
    _check_rfc3339(record["issued_at"], "issued_at")
    issued_epoch = rfc3339_to_epoch_seconds(record["issued_at"])

    _check_nonempty_str(record["lease_id"], "lease_id")
    _check_nonempty_str(record["role"], "role")
    _check_nonempty_str(record["provider_session_id"], "provider_session_id")
    _check_hex64(record["controller_policy_digest"], "controller_policy_digest")
    _check_hex64(record["candidate_digest"], "candidate_digest")

    resume_mode = record["resume_mode"]
    if resume_mode not in RESUME_MODE_SET:
        raise ValueError(
            "resume_mode must be one of %s, got %r" % (sorted(RESUME_MODE_SET), resume_mode))
    if resume_mode == "scheduled":
        _check_rfc3339(record["not_before"], "not_before")
        not_before_epoch = rfc3339_to_epoch_seconds(record["not_before"])
        _check_within_retry_horizon(issued_epoch, not_before_epoch, "not_before")
    else:
        if record["not_before"] is not None:
            raise ValueError("not_before must be null when resume_mode='manual_signal'")

    _check_nonempty_str(record["automation_ref"], "automation_ref")
    _check_artifact_hashes(record["artifact_hashes"], "artifact_hashes")

    consumption_state = record["consumption_state"]
    if consumption_state not in CONSUMPTION_STATE_SET:
        raise ValueError(
            "consumption_state must be one of %s, got %r"
            % (sorted(CONSUMPTION_STATE_SET), consumption_state))

    failed_wake_attempts = record["failed_wake_attempts"]
    _check_nonneg_int(failed_wake_attempts, "failed_wake_attempts")
    if failed_wake_attempts > FAILED_WAKE_ATTEMPT_CEILING:
        raise ValueError(
            "failed_wake_attempts must not exceed FAILED_WAKE_ATTEMPT_CEILING=%d, got %r"
            % (FAILED_WAKE_ATTEMPT_CEILING, failed_wake_attempts))

    # Deep-copy the nested artifact_hashes dict — see the identical note in
    # _validate_binding above; the same aliasing risk applies here.
    normalized = dict(record)
    normalized["artifact_hashes"] = dict(record["artifact_hashes"])
    return normalized


def record_failed_wake_attempt(pause_lease):
    """Pure increment: return a new, normalized PauseLease dict with
    `failed_wake_attempts` incremented by exactly 1. Never mutates the
    input. Raises ValueError when the lease has already reached
    FAILED_WAKE_ATTEMPT_CEILING — callers must consult
    `capacity_wake_decision`/`wake_attempts_exhausted` BEFORE attempting
    (and recording) another wake, so the ceiling is enforced at the
    decision point and can never be silently bypassed by incrementing past
    it."""
    lease = validate_pause_lease(pause_lease)
    if lease["failed_wake_attempts"] >= FAILED_WAKE_ATTEMPT_CEILING:
        raise ValueError(
            "failed_wake_attempts is already at or beyond FAILED_WAKE_ATTEMPT_CEILING=%d; "
            "call capacity_wake_decision first" % FAILED_WAKE_ATTEMPT_CEILING)
    new_lease = dict(lease)
    new_lease["failed_wake_attempts"] = lease["failed_wake_attempts"] + 1
    return validate_pause_lease(new_lease)


def next_pause_lease_after_replacement(old_lease, new_lease):
    """M3R-N06 monotonicity across lease replacement.

    When a PauseLease for a binding is replaced by a fresh one (the old
    lease's own `consumption_state` becomes `"replaced"` by the caller/
    ledger, independently of this function — this function does not itself
    mutate or re-validate the OLD lease's consumption_state), the fresh
    lease's `failed_wake_attempts` must never regress below the old lease's
    count. Without this, a caller could defeat the bounded M3R-N06
    wake-attempt ceiling simply by minting a nominally "new" lease for the
    same binding, restarting the count at 0 and cycling forever.

    Returns a normalized copy of `new_lease` with `failed_wake_attempts` set
    to `max(old_lease.failed_wake_attempts, new_lease.failed_wake_attempts)`
    — monotonic, never decreasing. Raises ValueError if that carried-forward
    value would exceed FAILED_WAKE_ATTEMPT_CEILING (mirroring
    `record_failed_wake_attempt`'s own ceiling enforcement): a replacement
    can never resurrect a ceiling-exhausted binding's automatic wake
    cycling."""
    old = validate_pause_lease(old_lease)
    new = validate_pause_lease(new_lease)
    carried = max(old["failed_wake_attempts"], new["failed_wake_attempts"])
    if carried > FAILED_WAKE_ATTEMPT_CEILING:
        raise ValueError(
            "carried-forward failed_wake_attempts=%d exceeds "
            "FAILED_WAKE_ATTEMPT_CEILING=%d; replacement cannot resurrect a "
            "ceiling-exhausted binding" % (carried, FAILED_WAKE_ATTEMPT_CEILING))
    result = dict(new)
    result["failed_wake_attempts"] = carried
    return validate_pause_lease(result)


def wake_attempts_exhausted(pause_lease):
    """True iff `pause_lease.failed_wake_attempts` has reached
    FAILED_WAKE_ATTEMPT_CEILING (a bounded, testable boolean — never an
    unbounded/open-ended check)."""
    lease = validate_pause_lease(pause_lease)
    return lease["failed_wake_attempts"] >= FAILED_WAKE_ATTEMPT_CEILING


def capacity_wake_decision(pause_lease):
    """M3R-N06's bounded terminal/stop decision contract.

    Returns "wake_retry_eligible" when another automatic wake attempt is
    still permitted, or the truthful typed stop outcome
    "wake_attempts_exhausted" once FAILED_WAKE_ATTEMPT_CEILING has been
    reached — never a value implying silent, unbounded retry. This package
    defines the decision only; Package D is responsible for actually
    applying it (declining to schedule a further automatic wake once this
    returns "wake_attempts_exhausted")."""
    return "wake_attempts_exhausted" if wake_attempts_exhausted(pause_lease) else "wake_retry_eligible"


# ---------------------------------------------------------------------------
# pause_until_eligible equivalence declaration
# ---------------------------------------------------------------------------

PAUSE_UNTIL_ELIGIBLE_EQUIVALENCE = (
    "pause_until_eligible is not a new PhaseState or event: it is this "
    "plan's name for the scheduled-resume-mode wait already carried by "
    "CapacityPacket.resume_mode == 'scheduled' (equivalently, PauseLease."
    "resume_mode == 'scheduled') plus PauseLease.not_before — an explicit, "
    "tested equivalence declaration, not a second, undefined concept living "
    "alongside the awaiting_capacity phase. Every later package must cite "
    "this constant by name instead of restating the equivalence informally."
)


def is_pause_until_eligible(capacity_packet=None, pause_lease=None):
    """True iff the supplied record(s) are in the scheduled-resume-mode
    wait this plan calls `pause_until_eligible` — resume_mode == 'scheduled'
    with a populated not_before. At least one of capacity_packet/pause_lease
    must be supplied; each is validated via its own schema validator before
    being consulted.

    When BOTH are supplied, they must name the exact same binding identity
    (role, provider_session_id, controller_policy_digest, candidate_digest,
    and — when the packet's wakeup names one — a lease_id matching the
    lease's own lease_id): two unrelated records that merely happen to
    agree on the boolean pause_until_eligible value are refused with
    ValueError, not silently accepted as "the same wait". Only once
    identity is confirmed to bind is the boolean equivalence itself
    compared and required to agree."""
    if capacity_packet is None and pause_lease is None:
        raise ValueError("at least one of capacity_packet/pause_lease is required")

    packet = validate_capacity_packet(capacity_packet) if capacity_packet is not None else None
    lease = validate_pause_lease(pause_lease) if pause_lease is not None else None

    if packet is not None and lease is not None:
        packet_binding = packet["binding"]
        for name, packet_value, lease_value in (
            ("role", packet_binding["role"], lease["role"]),
            ("provider_session_id", packet_binding["provider_session_id"],
             lease["provider_session_id"]),
            ("controller_policy_digest", packet_binding["controller_policy_digest"],
             lease["controller_policy_digest"]),
            ("candidate_digest", packet_binding["candidate_digest"], lease["candidate_digest"]),
        ):
            if packet_value != lease_value:
                raise ValueError(
                    "capacity_packet and pause_lease disagree on binding identity %r: "
                    "%r != %r" % (name, packet_value, lease_value))
        packet_lease_id = packet["wakeup"]["lease_id"]
        if packet_lease_id is not None and packet_lease_id != lease["lease_id"]:
            raise ValueError(
                "capacity_packet.wakeup.lease_id %r != pause_lease.lease_id %r"
                % (packet_lease_id, lease["lease_id"]))

    results = []
    if packet is not None:
        results.append(packet["resume_mode"] == "scheduled" and packet["wakeup"]["not_before"] is not None)
    if lease is not None:
        results.append(lease["resume_mode"] == "scheduled" and lease["not_before"] is not None)

    if len(results) == 2 and results[0] != results[1]:
        raise ValueError(
            "capacity_packet and pause_lease disagree on pause_until_eligible equivalence")
    return results[0]


# ---------------------------------------------------------------------------
# InvalidationRecord (issue #34)
# ---------------------------------------------------------------------------

_INVALIDATION_RECORD_KEYS = frozenset({
    "schema_version", "package_id",
    "invalidated_candidate_digest", "invalidated_session_id", "invalidated_work_id",
    "invalidating_principal", "reason", "evidence_refs", "issued_at",
})


def validate_invalidation_record(record):
    """Return a normalized copy of an InvalidationRecord, or raise
    ValueError. Names the exact prior completed-paired-work candidate/
    session/work_id it invalidates, the invalidating principal/authority
    reference, a reason, and supporting evidence references.

    `evidence_refs` normalizes to a `list` (not a `tuple`) so a record that
    has round-tripped through `json.dumps`/`json.loads` (JSON has no tuple
    type — it always decodes an array to a `list`) validates to the exact
    same normalized shape as one validated directly, keeping the two paths
    type-stable and equality-comparable.

    This validator only checks shape; append-only enforcement (never
    editing or removing a prior completed-work record) is a durable-storage
    property this pure/no-I/O module cannot itself enforce — it is the
    owning ledger's (Package B's) responsibility to only ever append these
    records, never mutate or delete one."""
    if not isinstance(record, dict):
        raise ValueError("InvalidationRecord must be a dict, got %r" % type(record))
    _check_exact_keys(record, _INVALIDATION_RECORD_KEYS, "InvalidationRecord")
    _check_schema_version(record)
    _check_nonempty_str(record["package_id"], "package_id")

    _check_hex64(record["invalidated_candidate_digest"], "invalidated_candidate_digest")
    _check_nonempty_str(record["invalidated_session_id"], "invalidated_session_id")
    _check_nonempty_str(record["invalidated_work_id"], "invalidated_work_id")
    _check_nonempty_str(record["invalidating_principal"], "invalidating_principal")
    _check_nonempty_str(record["reason"], "reason")

    evidence_refs = record["evidence_refs"]
    if not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
        raise ValueError("evidence_refs must be a nonempty list, got %r" % (evidence_refs,))
    normalized_refs = []
    for ref in evidence_refs:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise ValueError("evidence_refs entry must be {'path','sha256'}, got %r" % (ref,))
        _check_nonempty_str(ref["path"], "evidence_refs[].path")
        _check_hex64(ref["sha256"], "evidence_refs[].sha256")
        normalized_refs.append(dict(ref))

    _check_rfc3339(record["issued_at"], "issued_at")

    normalized = dict(record)
    normalized["evidence_refs"] = normalized_refs
    return normalized


# ---------------------------------------------------------------------------
# Signed manual-capacity-signal record
# ---------------------------------------------------------------------------

_MANUAL_CAPACITY_SIGNAL_KEYS = frozenset({
    "schema_version", "package_id",
    "candidate_digest", "role", "provider_session_id", "controller_policy_digest",
    "signal_journal_ref", "detached_signature", "signer_public_key_id",
    "issued_at",
})

# Detached-signature material must itself be signature-shaped, not merely
# any nonempty string ("x" is not a signature). Lowercase hex, at least 32
# characters (16 bytes) — a shape-only floor consistent with this module's
# existing hex-digest convention; it does NOT itself perform asymmetric
# cryptographic verification (see validate_manual_capacity_signal's
# docstring).
_SIGNATURE_HEX_RE = re.compile(r'^[0-9a-f]{32,}$')


def _check_signature_hex(value, field):
    if not isinstance(value, str) or not _SIGNATURE_HEX_RE.match(value):
        raise ValueError(
            "%s must be a lowercase hex string of at least 32 characters "
            "(16 bytes) — shape-only validation, not cryptographic "
            "verification, got %r" % (field, value))


def validate_manual_capacity_signal(record):
    """Return a normalized copy of a signed manual-capacity-signal record,
    or raise ValueError.

    Names the packet/candidate/session/policy binding it authorizes, a
    detached cryptographic signature, and a signer public-key identifier.
    The record's key set is exactly `_MANUAL_CAPACITY_SIGNAL_KEYS` — there
    is no plaintext "authorized"/"trusted" boolean field a validator could
    be tricked into trusting without verifying the signature; any such
    extra key is refused as an unknown key like any other malformed shape.

    `detached_signature` must be a lowercase hex string of at least 32
    characters (16 bytes) — see `_check_signature_hex`. This validator
    checks that structural shape only; it does not itself perform
    asymmetric cryptographic verification math, which belongs to a later,
    non-pure runtime-wiring package."""
    if not isinstance(record, dict):
        raise ValueError("manual capacity signal must be a dict, got %r" % type(record))
    _check_exact_keys(record, _MANUAL_CAPACITY_SIGNAL_KEYS, "manual capacity signal")
    _check_schema_version(record)
    _check_nonempty_str(record["package_id"], "package_id")

    _check_hex64(record["candidate_digest"], "candidate_digest")
    _check_nonempty_str(record["role"], "role")
    _check_nonempty_str(record["provider_session_id"], "provider_session_id")
    _check_hex64(record["controller_policy_digest"], "controller_policy_digest")

    _check_nonempty_str(record["signal_journal_ref"], "signal_journal_ref")
    _check_signature_hex(record["detached_signature"], "detached_signature")
    _check_nonempty_str(record["signer_public_key_id"], "signer_public_key_id")

    _check_rfc3339(record["issued_at"], "issued_at")

    return dict(record)
