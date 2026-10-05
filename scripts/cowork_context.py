#!/usr/bin/env python3
"""Context envelope vocabulary and decision ladder.

A pure leaf: stdlib-only, no I/O, and no import of any sibling module. It owns
the closed vocabularies a context envelope is measured in (metrics, timing,
decisions, reason codes, boundaries, event names, record field names) and two
pure functions: `observe` turns work-entry rows and a delivery measurement into
an observation, and `evaluate` compares an observation with an envelope and
returns one decision.

Nothing here reads a ledger, writes a trace event or alters dispatch. The
envelope LIMITS live in `cowork_execution_profiles`; this module only reads one
that a caller passes in. Unknown is advisory: a metric that cannot be measured
is reported as 'unknown', never as 0, and an unknown value or an absent limit
can never produce a stop.

Python 3.9+.
"""

from collections.abc import Mapping

UNKNOWN = "unknown"

# Metrics known before a turn is dispatched (the size of what is about to be
# sent) versus metrics of the running session window, observed after a turn.
CURRENT_TURN_METRICS = ("prompt_bytes", "artifact_bytes")
SESSION_WINDOW_METRICS = ("repository_reads", "elapsed_ms",
                          "reported_input_tokens", "cache_read_tokens")
METRICS = CURRENT_TURN_METRICS + SESSION_WINDOW_METRICS

TIMING = ("pre_dispatch_known", "post_turn_observed")
TIMING_BY_METRIC = {
    "prompt_bytes": "pre_dispatch_known",
    "artifact_bytes": "pre_dispatch_known",
    "repository_reads": "post_turn_observed",
    "elapsed_ms": "post_turn_observed",
    "reported_input_tokens": "post_turn_observed",
    "cache_read_tokens": "post_turn_observed",
}

DECISIONS = ("within", "warn", "expand_delegated", "rotate_recommended",
             "needs_authority")
# Strongest first. The strongest per-metric decision wins; metrics with the
# same decision are tie-broken by METRICS order.
DECISION_PRECEDENCE = ("needs_authority", "rotate_recommended",
                       "expand_delegated", "warn", "within")

REASON_CODES = (
    "within_limits",
    "unprofiled_no_envelope",
    "warn_limit_reached",
    "hard_limit_within_expansion_bound",
    "session_window_hard_limit_beyond_bound",
    "current_turn_hard_limit_beyond_bound",
    "authority_granted_expansion",
)

# Points at which a rotation may take effect.
BOUNDARIES = ("lead_after_phase_approved", "reviewer_between_rounds",
              "lead_before_correction_round")

# Record families and the trace event names reserved for them. Plain strings:
# later stages import these rather than invent their own.
FAMILIES = ("envelope_and_expansion", "correction_packet", "recovery_episode",
            "rotation_record", "lineage_record")
EVENT_NAMES = {
    "envelope_and_expansion": (
        "context.envelope.dispatch", "context.envelope.warning",
        "context.envelope.expansion", "context.envelope.rotate_recommended"),
    "rotation_record": ("context.rotation",),
    "correction_packet": ("context.correction",),
    "recovery_episode": ("context.recovery.episode",),
    "lineage_record": ("context.lineage",),
}

EXPANSION_RECORD_FIELDS = ("role", "work_id", "chain", "metric",
                           "prior_limit", "new_limit", "reason_code",
                           "envelope_digest", "authorized_by")
ROTATION_EVENT_FIELDS = ("role", "chain", "boundary", "boundary_seq",
                         "metric", "reason_code")

_CHILD_KINDS = ("child", "child_attempt")


def _plain_count(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def _measured(value):
    return value if _plain_count(value) else UNKNOWN


def _row_order(row):
    started = row.get("started_at")
    work_id = row.get("work_id")
    return (started if isinstance(started, str) else "",
            work_id if isinstance(work_id, str) else "")


def _last_row_tokens(row):
    """(reported_input_tokens, cache_read_tokens) of one row, each 'unknown'
    when the row's usage cannot be compared or the value is not a count."""
    if row is None or row.get("usage_scope") == "incomparable":
        return UNKNOWN, UNKNOWN
    usage = row.get("usage")
    if not isinstance(usage, Mapping):
        return UNKNOWN, UNKNOWN
    return (_measured(usage.get("input_tokens")),
            _measured(usage.get("cache_read_input_tokens")))


def observe(turn_rows, delivery):
    """Observation of one role's session window.

    Participating rows are completed work entries that are not child work.
    Token metrics are the LAST participating row's values (a per-turn report
    already reads the context size, so summing would double-count a resumed
    session) and never fall back to an earlier row. `elapsed_ms` is the SUM of
    participating durations and is unknown as a whole if any one is. The
    pre-dispatch metrics come from `delivery`. Returns
    `{metric: {"value": int | "unknown", "timing": ...}}` for every metric in
    METRICS; missing data is 'unknown', never 0. The inputs are not mutated.
    """
    rows = [row for row in turn_rows or ()
            if isinstance(row, Mapping)
            and row.get("work_state") == "complete"
            and row.get("work_kind") not in _CHILD_KINDS]
    last = sorted(rows, key=_row_order)[-1] if rows else None
    input_tokens, cache_tokens = _last_row_tokens(last)
    durations = [row.get("duration_ms") for row in rows]
    if rows and all(_plain_count(d) for d in durations):
        elapsed = sum(durations)
    else:
        elapsed = UNKNOWN
    delivered = delivery if isinstance(delivery, Mapping) else {}
    values = {
        "prompt_bytes": _measured(delivered.get("prompt_bytes")),
        "artifact_bytes": _measured(delivered.get("artifact_bytes")),
        "repository_reads": _measured(delivered.get("repository_reads")),
        "elapsed_ms": elapsed,
        "reported_input_tokens": input_tokens,
        "cache_read_tokens": cache_tokens,
    }
    return {metric: {"value": values[metric],
                     "timing": TIMING_BY_METRIC[metric]}
            for metric in METRICS}


def _bound(limits, name):
    value = limits.get(name) if isinstance(limits, Mapping) else None
    return value if _plain_count(value) else None


def _max_per_chain(envelope):
    expansion = envelope.get("expansion")
    value = expansion.get("max_per_chain") if isinstance(
        expansion, Mapping) else None
    return value if _plain_count(value) else 0


def _metric_outcome(metric, value, limits, expansions_used, max_per_chain):
    """(decision, reason_code) for one metric, or None when it neither warns
    nor breaches."""
    hard = _bound(limits, "hard")
    warn = _bound(limits, "warn")
    if hard is not None and value >= hard:
        if expansions_used < max_per_chain:
            return "expand_delegated", "hard_limit_within_expansion_bound"
        if metric in CURRENT_TURN_METRICS:
            return "needs_authority", "current_turn_hard_limit_beyond_bound"
        return ("rotate_recommended",
                "session_window_hard_limit_beyond_bound")
    if warn is not None and value >= warn:
        return "warn", "warn_limit_reached"
    return None


def evaluate(envelope, observation, expansions_used):
    """One decision for an observation against an envelope.

    No envelope (an unprofiled caller) is 'within'. An envelope that is not a
    mapping, or an `expansions_used` that is not a non-negative int, is a
    caller error and raises ValueError. Unknown data is not an error: an
    unknown value, an absent metric or a None limit is skipped. The strongest
    per-metric outcome by DECISION_PRECEDENCE wins and a tie names the first
    metric in METRICS order. Returns exactly
    `{"decision", "metric", "reason_code"}`.
    """
    if envelope is None:
        return {"decision": "within", "metric": None,
                "reason_code": "unprofiled_no_envelope"}
    if not isinstance(envelope, Mapping):
        raise ValueError("context envelope must be a mapping")
    if not _plain_count(expansions_used):
        raise ValueError("expansions_used must be a non-negative integer")
    observed = observation if isinstance(observation, Mapping) else {}
    all_limits = envelope.get("limits")
    all_limits = all_limits if isinstance(all_limits, Mapping) else {}
    max_per_chain = _max_per_chain(envelope)
    best = None
    for metric in METRICS:
        entry = observed.get(metric)
        value = entry.get("value") if isinstance(entry, Mapping) else None
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        outcome = _metric_outcome(metric, value, all_limits.get(metric),
                                  expansions_used, max_per_chain)
        if outcome is None:
            continue
        if best is None or (DECISION_PRECEDENCE.index(outcome[0])
                            < DECISION_PRECEDENCE.index(best[1])):
            best = (metric, outcome[0], outcome[1])
    if best is None:
        return {"decision": "within", "metric": None,
                "reason_code": "within_limits"}
    return {"decision": best[1], "metric": best[0], "reason_code": best[2]}
