#!/usr/bin/env python3
"""Activity/status contracts — M4 Package A.

Pure/versioned data contracts for M4's truthful-liveness surface: the closed
`ActivityClass` taxonomy, the `ActivityRecord` schema, an append-only
reconciliation-record schema, the `ScheduledReviewRecord` schema (issue
#58's durable next-inspection source of truth), the `WatchdogDecision`
schema (with its mandatory dual-evidence requirement), the canonical
`project_compact_state` projection, the `ControllerTurnOutcome` extension
(`no_first_token`/`refused`), and the exact pinned cross-package function
signatures later M4 packages build against. This module is inert
infrastructure: it performs no file or network I/O, spawns no child
process, forks nothing, and imports nothing beyond the Python standard
library — every value that could vary at runtime (a "now", a live process
handle) is an explicit caller-supplied input, never read or created here.

Public API:
    ACTIVITY_CLASSES, ACTIVITY_CLASS_SET
    ACTIVITY_SOURCES, ACTIVITY_SOURCE_SET
    PROVIDER_HEALTH_STATES, PROVIDER_HEALTH_STATE_SET
    WATCHDOG_VERDICTS, WATCHDOG_VERDICT_SET
    CONTROLLER_TURN_OUTCOME_EXTENSIONS, CONTROLLER_TURN_OUTCOME_EXTENSION_SET
    FAILURE_CLASSES, FAILURE_CLASS_SET
    PINNED_SIGNATURES
    validate_activity_record(record) -> dict (normalized) or raises ValueError
    validate_activity_reconciliation_record(record) -> dict or raises ValueError
    validate_scheduled_review_record(record) -> dict or raises ValueError
    validate_watchdog_decision(record) -> dict or raises ValueError
    validate_controller_turn_outcome(record) -> dict or raises ValueError
    project_compact_state(activity_record, health_record, schedule_record,
                           reconciliation_record=None) -> dict (pure)

Records are ordinary JSON-native dictionaries, matching the convention
already used by the repository's other pure-schema modules
(`cowork_control_plane.py`, `cowork_workunit.py`, `cowork_capacity.py`).
`schema_version` is integer 1 throughout. Every validator returns a
normalized copy and never mutates its input; unknown keys are always
rejected.

Scope note: the real per-turn foreground spinner/status label Package C
constructs inside three of its six named `cowork_bridge.py` methods is
explicitly OUT of this module's scope — it is ephemeral, unpersisted
terminal text, never validated by any validator here, and never confused
with the durable `ActivityRecord`/`WatchdogDecision`/compact-state objects
this module defines. Likewise, the `activity` key Package D adds to
`cowork_measure.py`'s `build_record` output is a measurement-record field
consumed by the report leg, not an Activity Contracts schema object of its
own.

Append-only law: a later record may supersede an earlier one, never
overwrite it in place. `validate_activity_reconciliation_record` enforces
this at the schema level by requiring both `original_classification` and
`reconciled_classification` as distinct, always-present fields — a
reconciliation is always a new, linked record naming both the classification
it supersedes and the one it establishes, never a silent in-place edit.

Dual-evidence law: `validate_watchdog_decision` requires BOTH a non-null
`durable_evidence_ref` AND a non-null `process_probe_ref` on every
`soft_warning` or `hard_stall_eligible` verdict. A decision carrying only
one of the two is rejected exactly like a decision carrying neither — two
independently-tested rejection paths, never folded into a single OR-check.
This is what keeps a normal long-running turn from being killed merely
because it is quiet: a terminal verdict requires live process/controller
evidence, not durable silence alone. `project_compact_state` re-applies this
same law by fully revalidating every record it is given (through the exact
`validate_*()` functions above) before projecting anything — it never trusts
a caller-supplied dict's shape alone — and it carries both evidence
references through into the returned compact state, so a terminal verdict
in a rendered or reported surface always cites the evidence that justified
it.

Cross-package signature pinning: `live_child_handle(session)` is pinned
here by SIGNATURE ONLY — its truthful process-health semantics (non-null
whenever any controller child is alive, including Claude's session-lifetime
child spawned in `ClaudeSession.__init__` and torn down only in `close()`;
null only before spawn and after reap/close) are documented in
`PINNED_SIGNATURES` for cross-package review discipline, but the function's
body is Package C's exclusive property in `scripts/cowork_bridge.py`. This
module spawns and touches no live process, and never defines
`live_child_handle` itself.
"""

import math
import re
import types

SCHEMA_VERSION = 1

# `\Z` (true end-of-string), never `$` (which also matches immediately
# before a trailing "\n"): every identifier/timestamp pattern below must
# refuse a trailing-newline variant of an otherwise-valid string.
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z',
    re.IGNORECASE,
)
_HEX64_RE = re.compile(r'^[0-9a-f]{64}\Z')
# Deliberately shape-only (matched by `_check_rfc3339`'s own docstring and
# error text, which both say "RFC3339-shaped", never "valid RFC3339"): this
# module performs no calendar-range validation (no month-13 or day-32
# rejection) and therefore uses plain, non-capturing groups throughout —
# never named capture groups — so the pattern cannot imply a range check
# that does not exist anywhere in this file.
_RFC3339_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z')

# ---------------------------------------------------------------------------
# Closed ActivityClass taxonomy — exactly eight distinguishable states.
# ---------------------------------------------------------------------------
#
# `productive_model_work` and `provider_wait` are deliberately distinct
# members so that time spent waiting on a provider is never attributable as
# productive role work in measurement (issue #41 criterion 4). `no_evidence_
# silence` is reserved for the genuine absence of any evidence at all — a
# first-token timeout, a refusal, a crash, or a hung descendant are each
# their OWN typed, evidenced class, never collapsed into silence.
ACTIVITY_CLASSES = (
    "productive_model_work",
    "local_tool_work",
    "owned_verification",
    "provider_wait",
    "policy_denial",
    "process_crash",
    "hung_descendant",
    "no_evidence_silence",
)
ACTIVITY_CLASS_SET = frozenset(ACTIVITY_CLASSES)

# Evidence source: which controller (or the controller's own native tooling,
# distinct from any provider round-trip) produced the classified evidence.
ACTIVITY_SOURCES = ("claude", "codex", "opencode", "controller_native_tool")
ACTIVITY_SOURCE_SET = frozenset(ACTIVITY_SOURCES)

# Provider-health vocabulary, matching the status vocabulary already used by
# scripts/cowork.py's own ProviderHealth writes (_PROVIDER_HEALTH_STATUS_FOR_
# OUTCOME): "healthy", "degraded", "unavailable". Read-only alignment; this
# module never imports scripts/cowork.py.
PROVIDER_HEALTH_STATES = ("healthy", "degraded", "unavailable")
PROVIDER_HEALTH_STATE_SET = frozenset(PROVIDER_HEALTH_STATES)

WATCHDOG_VERDICTS = ("no_action", "soft_warning", "hard_stall_eligible")
WATCHDOG_VERDICT_SET = frozenset(WATCHDOG_VERDICTS)

# Verdicts that REQUIRE both evidence references — see the dual-evidence law
# in the module docstring.
_WATCHDOG_DUAL_EVIDENCE_VERDICTS = frozenset({"soft_warning", "hard_stall_eligible"})

# ControllerTurnOutcome extension (extends the roadmap's existing, M3-owned
# ControllerOutcome taxonomy in cowork_capacity.py with exactly two new
# typed values consumed by Package C's stream-loop edits). Deliberately
# defined as its own closed set here rather than merged into cowork_capacity
# .CONTROLLER_OUTCOMES: this module imports nothing beyond the standard
# library, so the extension is pinned independently and Package C's own
# integration is responsible for keeping the two taxonomies from colliding
# by name (they do not: neither "no_first_token" nor "refused" appears in
# cowork_capacity.CONTROLLER_OUTCOMES).
CONTROLLER_TURN_OUTCOME_EXTENSIONS = ("no_first_token", "refused")
CONTROLLER_TURN_OUTCOME_EXTENSION_SET = frozenset(CONTROLLER_TURN_OUTCOME_EXTENSIONS)

FAILURE_CLASSES = ("quota", "balance", "auth", "overload", "transport",
                    "unknown_provider_failure")
FAILURE_CLASS_SET = frozenset(FAILURE_CLASSES)

# Integration note for Package C, which must emit both this closed set and
# M3's `cowork_capacity.CONTROLLER_OUTCOMES` (a materially different closed
# set this module does not import, per the non-collision note above): the
# fixed M4 plan pins FAILURE_CLASSES with this exact spelling verbatim, so
# the two vocabularies are NOT merged or renamed here. The intended informal
# correspondence for a human integrator, documentation only and enforced
# nowhere in code, is:
#   "quota"                    <-> "quota_limited"
#   "auth"                     <-> "authentication_failed"
#   "overload"                 <-> "overloaded"
#   "transport"                <-> "transport_failed"
#   "balance"                  <-> (no M3 counterpart; a subscription-balance
#                                   depletion signal new to this extension)
#   "unknown_provider_failure" <-> "unknown_provider_failure" (shared verbatim)
# Neither taxonomy is changed by this note.


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------

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


def _check_record_kind(record, expected_kind):
    if record.get("record") != expected_kind:
        raise ValueError("record field must be %r, got %r" % (expected_kind, record.get("record")))


def _check_uuid(value, field):
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ValueError("%s must be a UUID-shaped string, got %r" % (field, value))
    return value.lower()


def _check_nonempty_str(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a nonempty string, got %r" % (field, value))


def _check_nonempty_str_or_null(value, field):
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("%s must be null or a nonempty string, got %r" % (field, value))


def _check_hex64(value, field):
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        raise ValueError("%s must be 64 lowercase hex chars, got %r" % (field, value))


def _check_rfc3339(value, field):
    if not isinstance(value, str) or not _RFC3339_RE.match(value):
        raise ValueError("%s must be an RFC3339-shaped timestamp string, got %r" % (field, value))


def _check_nonneg_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("%s must be a nonnegative number, got %r" % (field, value))
    if not math.isfinite(value):
        # `value < 0` above is False for both NaN and +Infinity, so neither
        # is caught by the range check alone (a JSON-native record can
        # contain neither value in the first place).
        raise ValueError("%s must be finite, got %r" % (field, value))


def _check_positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("%s must be a positive integer, got %r" % (field, value))


def _check_enum(value, allowed_set, field):
    if value not in allowed_set:
        raise ValueError("%s must be one of %s, got %r" % (field, sorted(allowed_set), value))


def _check_artifact_fingerprint(value, field):
    """Either null (no artifacts observed) or a nonempty dict of
    {path: 64-hex-char digest}."""
    if value is None:
        return
    if not isinstance(value, dict) or not value:
        raise ValueError("%s must be null or a nonempty dict, got %r" % (field, value))
    for path, digest in value.items():
        if not isinstance(path, str) or not path:
            raise ValueError("%s has a non-string/empty key: %r" % (field, path))
        _check_hex64(digest, "%s[%r]" % (field, path))


def _check_artifact_delta(value, field, fingerprint):
    """A tuple/list of paths that changed since the previous classified
    record. Every entry must be a nonempty string present in `fingerprint`'s
    keys (when `fingerprint` is non-null) and unique — the delta names EACH
    changed path exactly once, never a duplicate, matching its set-like
    "paths that changed" semantics; the delta must be empty when
    `fingerprint` is null (no artifacts observed means nothing could have
    changed)."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("%s must be a list or tuple, got %r" % (field, type(value)))
    if fingerprint is None:
        if len(value) != 0:
            raise ValueError(
                "%s must be empty when artifact_fingerprint is null, got %r" % (field, value))
        return ()
    seen = set()
    normalized = []
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise ValueError("%s entry must be a nonempty string, got %r" % (field, entry))
        if entry not in fingerprint:
            raise ValueError(
                "%s entry %r is not a key of artifact_fingerprint" % (field, entry))
        if entry in seen:
            raise ValueError("%s entry %r is duplicated; each changed path is named once" %
                              (field, entry))
        seen.add(entry)
        normalized.append(entry)
    return tuple(normalized)


# ---------------------------------------------------------------------------
# ActivityRecord
# ---------------------------------------------------------------------------

_ACTIVITY_RECORD_KEYS = frozenset({
    "schema_version", "record", "work_id", "time",
    "activity_class", "source",
    "artifact_fingerprint", "artifact_delta",
    "provider_health", "age_seconds",
})


def validate_activity_record(record):
    """Return a normalized copy of an ActivityRecord, or raise ValueError.

    Never mutates input. Rejects missing or extra keys. `activity_class`
    must be a member of ACTIVITY_CLASS_SET (the closed, eight-value
    taxonomy); `source` must be a member of ACTIVITY_SOURCE_SET.
    `artifact_fingerprint` is either null or a nonempty {path: hex64 digest}
    dict; `artifact_delta` names a subset of that dict's keys and must be
    empty when `artifact_fingerprint` is null. `provider_health` is either
    null or a member of PROVIDER_HEALTH_STATE_SET. `age_seconds` is a
    nonnegative number — a pure recorded fact, never computed here from a
    wall clock this module never reads.
    """
    if not isinstance(record, dict):
        raise ValueError("ActivityRecord must be a dict, got %r" % type(record))
    _check_exact_keys(record, _ACTIVITY_RECORD_KEYS, "ActivityRecord")
    _check_schema_version(record)
    _check_record_kind(record, "ActivityRecord")

    work_id = _check_uuid(record["work_id"], "work_id")
    _check_rfc3339(record["time"], "time")
    _check_enum(record["activity_class"], ACTIVITY_CLASS_SET, "activity_class")
    _check_enum(record["source"], ACTIVITY_SOURCE_SET, "source")

    _check_artifact_fingerprint(record["artifact_fingerprint"], "artifact_fingerprint")
    artifact_delta = _check_artifact_delta(
        record["artifact_delta"], "artifact_delta", record["artifact_fingerprint"])

    provider_health = record["provider_health"]
    if provider_health is not None:
        _check_enum(provider_health, PROVIDER_HEALTH_STATE_SET, "provider_health")

    _check_nonneg_number(record["age_seconds"], "age_seconds")

    normalized = dict(record)
    normalized["work_id"] = work_id
    normalized["artifact_delta"] = artifact_delta
    if record["artifact_fingerprint"] is not None:
        normalized["artifact_fingerprint"] = dict(record["artifact_fingerprint"])
    return normalized


# ---------------------------------------------------------------------------
# Append-only reconciliation record
# ---------------------------------------------------------------------------

_RECONCILIATION_KEYS = frozenset({
    "schema_version", "record", "work_id", "time",
    "original_classification", "reconciled_classification",
    "revision_digest", "quiescence_marker",
})


def validate_activity_reconciliation_record(record):
    """Return a normalized copy of an append-only reconciliation record, or
    raise ValueError.

    Append-only law: `original_classification` and `reconciled_classification`
    are BOTH always-present, distinct fields — `_check_exact_keys` alone
    already refuses a record that omits either, so a reconciliation can
    never collapse to a single classification field or silently overwrite
    the record it supersedes. Both must be members of ACTIVITY_CLASS_SET.
    `revision_digest` identifies the artifact revision the re-read observed;
    `quiescence_marker` is a nonempty string recording how quiescence was
    established before the re-read (e.g. a wait/poll strategy tag).
    """
    if not isinstance(record, dict):
        raise ValueError("ActivityReconciliationRecord must be a dict, got %r" % type(record))
    _check_exact_keys(record, _RECONCILIATION_KEYS, "ActivityReconciliationRecord")
    _check_schema_version(record)
    _check_record_kind(record, "ActivityReconciliationRecord")

    work_id = _check_uuid(record["work_id"], "work_id")
    _check_rfc3339(record["time"], "time")
    _check_enum(record["original_classification"], ACTIVITY_CLASS_SET, "original_classification")
    _check_enum(record["reconciled_classification"], ACTIVITY_CLASS_SET, "reconciled_classification")
    _check_hex64(record["revision_digest"], "revision_digest")
    _check_nonempty_str(record["quiescence_marker"], "quiescence_marker")

    normalized = dict(record)
    normalized["work_id"] = work_id
    return normalized


# ---------------------------------------------------------------------------
# ScheduledReviewRecord — durable source of truth for issue #58
# ---------------------------------------------------------------------------

_SCHEDULED_REVIEW_KEYS = frozenset({
    "schema_version", "record", "work_id",
    "next_inspection_at", "interval_seconds", "last_inspection_result_ref",
})


def validate_scheduled_review_record(record):
    """Return a normalized copy of a ScheduledReviewRecord, or raise
    ValueError.

    `next_inspection_at` is the durable, authoritative next-review instant
    (RFC3339) — the sole source of truth issue #58 requires; no code or
    wording anywhere in this contract recomputes "elapsed since last event"
    as a proxy for whether a review is due. `interval_seconds` is the
    review-cadence policy, a positive integer. `last_inspection_result_ref`
    is either null (no prior inspection yet) or a nonempty reference string
    to the last inspection's durable result.
    """
    if not isinstance(record, dict):
        raise ValueError("ScheduledReviewRecord must be a dict, got %r" % type(record))
    _check_exact_keys(record, _SCHEDULED_REVIEW_KEYS, "ScheduledReviewRecord")
    _check_schema_version(record)
    _check_record_kind(record, "ScheduledReviewRecord")

    work_id = _check_uuid(record["work_id"], "work_id")
    _check_rfc3339(record["next_inspection_at"], "next_inspection_at")
    _check_positive_int(record["interval_seconds"], "interval_seconds")
    _check_nonempty_str_or_null(record["last_inspection_result_ref"], "last_inspection_result_ref")

    normalized = dict(record)
    normalized["work_id"] = work_id
    return normalized


# ---------------------------------------------------------------------------
# WatchdogDecision — mandatory dual-evidence requirement
# ---------------------------------------------------------------------------

_WATCHDOG_DECISION_KEYS = frozenset({
    "schema_version", "record", "work_id", "time", "verdict",
    "durable_evidence_ref", "process_probe_ref",
})


def validate_watchdog_decision(record):
    """Return a normalized copy of a WatchdogDecision, or raise ValueError.

    `verdict` must be a member of WATCHDOG_VERDICT_SET. Every
    `soft_warning` or `hard_stall_eligible` verdict REQUIRES both
    `durable_evidence_ref` and `process_probe_ref` to be non-null — a
    decision carrying only one of the two is rejected exactly like a
    decision carrying neither (see the module docstring's dual-evidence
    law). `no_action` carries no such requirement; either reference may be
    null or set.
    """
    if not isinstance(record, dict):
        raise ValueError("WatchdogDecision must be a dict, got %r" % type(record))
    _check_exact_keys(record, _WATCHDOG_DECISION_KEYS, "WatchdogDecision")
    _check_schema_version(record)
    _check_record_kind(record, "WatchdogDecision")

    work_id = _check_uuid(record["work_id"], "work_id")
    _check_rfc3339(record["time"], "time")
    _check_enum(record["verdict"], WATCHDOG_VERDICT_SET, "verdict")

    durable_evidence_ref = record["durable_evidence_ref"]
    process_probe_ref = record["process_probe_ref"]
    _check_nonempty_str_or_null(durable_evidence_ref, "durable_evidence_ref")
    _check_nonempty_str_or_null(process_probe_ref, "process_probe_ref")

    if record["verdict"] in _WATCHDOG_DUAL_EVIDENCE_VERDICTS:
        missing = []
        if durable_evidence_ref is None:
            missing.append("durable_evidence_ref")
        if process_probe_ref is None:
            missing.append("process_probe_ref")
        if missing:
            raise ValueError(
                "WatchdogDecision verdict %r requires both durable_evidence_ref "
                "and process_probe_ref to be non-null; missing: %s"
                % (record["verdict"], missing))

    normalized = dict(record)
    normalized["work_id"] = work_id
    return normalized


# ---------------------------------------------------------------------------
# ControllerTurnOutcome extension
# ---------------------------------------------------------------------------

_CONTROLLER_TURN_OUTCOME_KEYS = frozenset({
    "schema_version", "record", "outcome", "failure_class",
})


def validate_controller_turn_outcome(record):
    """Return a normalized copy of a ControllerTurnOutcome extension record,
    or raise ValueError.

    `outcome` must be a member of CONTROLLER_TURN_OUTCOME_EXTENSION_SET
    (`no_first_token` or `refused`). `failure_class` must be null when
    `outcome == "no_first_token"` (a deadline expiry carries no provider
    failure reason) and a member of FAILURE_CLASS_SET when
    `outcome == "refused"` (every refusal is typed, never left as bare
    unclassified text).
    """
    if not isinstance(record, dict):
        raise ValueError("ControllerTurnOutcome must be a dict, got %r" % type(record))
    _check_exact_keys(record, _CONTROLLER_TURN_OUTCOME_KEYS, "ControllerTurnOutcome")
    _check_schema_version(record)
    _check_record_kind(record, "ControllerTurnOutcome")

    outcome = record["outcome"]
    _check_enum(outcome, CONTROLLER_TURN_OUTCOME_EXTENSION_SET, "outcome")

    failure_class = record["failure_class"]
    if outcome == "refused":
        _check_enum(failure_class, FAILURE_CLASS_SET, "failure_class")
    else:
        if failure_class is not None:
            raise ValueError(
                "failure_class must be null when outcome='no_first_token', got %r"
                % (failure_class,))

    return dict(record)


# ---------------------------------------------------------------------------
# Compact-state projection
# ---------------------------------------------------------------------------

def project_compact_state(activity_record, health_record, schedule_record,
                           reconciliation_record=None):
    """Pure projection of the canonical compact-state dict every renderer
    (Package E's render_compact_activity/render_headless_activity) and
    report section (Package D's _section_activity) consumes as their SOLE
    source of fact — no renderer or report section recomputes any fact
    independently of this function.

    Every argument is FULLY REVALIDATED here, through the exact same
    `validate_activity_record`/`validate_watchdog_decision`/
    `validate_scheduled_review_record`/`validate_activity_reconciliation_record`
    functions a caller would use to validate the record on its own — never a
    shape-only or shallow check. This is deliberate, not merely defensive:
    `health_record` is a `WatchdogDecision`, so revalidating it is what
    re-applies the module's dual-evidence law (see the module docstring) at
    the projection boundary itself. A raw dict that carries the right KEYS
    but a `hard_stall_eligible`/`soft_warning` verdict with one or both
    evidence references missing is REJECTED here with a ValueError, exactly
    as `validate_watchdog_decision` alone would reject it — there is no
    lenient, shape-only path through this function that could let an
    unbacked terminal verdict reach a renderer. Likewise an out-of-enum
    `activity_class`, a non-numeric `age_seconds`, or any other field-level
    defect in any of the four arguments is rejected here, not passed through
    verbatim. This function never reaches outside its four arguments for
    any fact (no file/network I/O, no clock read).

    Every fact a `soft_warning`/`hard_stall_eligible` verdict could need to
    be cited against — `durable_evidence_ref` and `process_probe_ref` — is
    carried through into the returned dict, alongside `interval_seconds`
    and `artifact_delta`, so this projection is genuinely the sole surface
    Packages D and E need; neither has to reach back into a raw
    `WatchdogDecision`/`ScheduledReviewRecord`/`ActivityRecord` for a fact
    already available here.

    False-productive-attribution guard: the returned `activity_class` is
    always taken directly from `reconciliation_record["reconciled_classification"]`
    when a reconciliation is given, else directly from
    `activity_record["activity_class"]` — never fabricated, upgraded, or
    inferred from any other field. A projection backed only by a
    `provider_wait` activity record (no reconciliation) always reports
    `provider_wait`, never `productive_model_work`.
    """
    activity_record = validate_activity_record(activity_record)
    health_record = validate_watchdog_decision(health_record)
    schedule_record = validate_scheduled_review_record(schedule_record)
    if reconciliation_record is not None:
        reconciliation_record = validate_activity_reconciliation_record(reconciliation_record)

    work_id = activity_record["work_id"]
    if health_record["work_id"] != work_id:
        raise ValueError("health_record.work_id does not match activity_record.work_id")
    if schedule_record["work_id"] != work_id:
        raise ValueError("schedule_record.work_id does not match activity_record.work_id")
    if reconciliation_record is not None and reconciliation_record["work_id"] != work_id:
        raise ValueError("reconciliation_record.work_id does not match activity_record.work_id")

    if reconciliation_record is not None:
        effective_class = reconciliation_record["reconciled_classification"]
        original_class = reconciliation_record["original_classification"]
    else:
        effective_class = activity_record["activity_class"]
        original_class = None

    return {
        "schema_version": SCHEMA_VERSION,
        "work_id": work_id,
        "activity_class": effective_class,
        "original_classification": original_class,
        "reconciled": reconciliation_record is not None,
        "source": activity_record["source"],
        "age_seconds": activity_record["age_seconds"],
        "artifact_delta": activity_record["artifact_delta"],
        "provider_health": activity_record["provider_health"],
        "watchdog_verdict": health_record["verdict"],
        "durable_evidence_ref": health_record["durable_evidence_ref"],
        "process_probe_ref": health_record["process_probe_ref"],
        "next_inspection_at": schedule_record["next_inspection_at"],
        "interval_seconds": schedule_record["interval_seconds"],
    }


# ---------------------------------------------------------------------------
# Pinned cross-package signatures (M4R-F03: Package A pins SIGNATURE ONLY;
# the function bodies for every entry other than project_compact_state are
# owned exclusively by the named package, in the named module).
# ---------------------------------------------------------------------------

PINNED_SIGNATURES = types.MappingProxyType({
    "project_compact_state": types.MappingProxyType({
        "owner": "A-activity-contracts",
        "module": "cowork_activity",
        "params": ("activity_record", "health_record", "schedule_record",
                   "reconciliation_record=None"),
        "returns": "dict",
        "body_owner": "A-activity-contracts",
    }),
    "render_compact_activity": types.MappingProxyType({
        "owner": "E-cross-surface-rendering",
        "module": "cowork_ui",
        "params": ("io_out", "compact_state", "enabled=None"),
        "returns": "None",
        "body_owner": "E-cross-surface-rendering",
    }),
    "render_headless_activity": types.MappingProxyType({
        "owner": "E-cross-surface-rendering",
        "module": "cowork_ui",
        "params": ("io_out", "compact_state"),
        "returns": "None",
        "body_owner": "E-cross-surface-rendering",
    }),
    "_section_activity": types.MappingProxyType({
        "owner": "D-watchdog-active-review",
        "module": "cowork_report",
        "params": ("record",),
        "returns": "list[str]",
        "body_owner": "D-watchdog-active-review",
    }),
    "live_child_handle": types.MappingProxyType({
        "owner": "C-controller-adapters",
        "module": "cowork_bridge",
        "params": ("session",),
        "returns": "subprocess.Popen | None",
        "body_owner": "C-controller-adapters",
        "semantics": (
            "Truthful process-health state: non-null whenever any "
            "controller child is alive, including Claude's session-lifetime "
            "child (spawned in ClaudeSession.__init__, torn down only in "
            "close()) and Codex's/OpenCode's per-turn handles; null only "
            "before spawn and after reap/close. Package A pins this "
            "signature only; Package C's own scripts/cowork_bridge.py "
            "additive grant is the sole owner of the function body."
        ),
    }),
})
