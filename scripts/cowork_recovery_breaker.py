#!/usr/bin/env python3
"""Durable recovery circuit breaker — M2 Package D.

Decides trip/no-trip for a repair attempt keyed by Package A's causal
fingerprint tuple `(role, config_digest, provider, candidate, reason)`
(`cowork_control_plane.fingerprint`). It answers exactly one question,
durably: "has this exact cause already been tried too many times?" It does
NOT choose or execute whatever happens after that answer — deciding which
recovery action to take, and any interactive recovery-choice UX around that
decision, is an explicit later-milestone assumption this package does not
implement or claim complete.

Public API:
    TRIP_THRESHOLD
    attempt(ledger_path, role, config_digest, provider, candidate, reason,
            fields=None) -> dict
    history(ledger_path, role, config_digest, provider, candidate, reason)
        -> list[dict]

Trip history is append-only and lives in the same ledger every other durable
cowork record lives in, through `cowork_ledger.append_breaker_attempt` /
`cowork_ledger.read_breaker_history` — themselves built on the ledger's
existing `_with_ledger_lock` / `_atomic_commit_batch` primitives, so a trip
decision always reads durable, crash-safe history rather than anything held
only in this process's memory, and survives a crash/restart between one
attempt and the next.

The fingerprint is derived ONLY by calling `cowork_control_plane.
fingerprint` — this module performs no parallel string construction of its
own. Because history is keyed by that exact digest, any differing tuple
field (a different role, config, provider, candidate, or reason) hashes to a
different fingerprint and therefore starts wholly independent history; it
never inherits a trip from a different cause.
"""

import cowork_control_plane as control_plane
import cowork_ledger

# Three identical-cause attempts is the trip threshold. One failed attempt
# for a given (role, config_digest, provider, candidate, reason) tuple could
# be transient (a flaky provider hiccup); a second is suspicious but still
# plausibly coincidence; a THIRD attempt failing for the exact same cause is
# strong evidence the cause is persistent rather than transient. Tripping at
# 3 caps the wasted-dispatch cost of a persistent cause at 3 attempts while
# still giving genuinely transient failures more than one chance to clear on
# their own before the breaker refuses further dispatch.
TRIP_THRESHOLD = 3


def attempt(ledger_path, role, config_digest, provider, candidate, reason,
           fields=None):
    """Ask the breaker to record one more recovery attempt for this exact
    cause, atomically deciding trip/no-trip against durable history first.

    Returns `{"fingerprint": <sha256 hex>, "threshold": TRIP_THRESHOLD,
    "attempt_count": <durable count BEFORE this call>, "tripped": <bool>,
    "record": <the appended record> or None}`.

    When `tripped` is True, this attempt (the `threshold + 1`th for this
    exact cause) was refused BEFORE dispatch: nothing is appended to
    history, and the caller must not dispatch. The breaker has made the
    only decision it is responsible for; choosing or executing whatever
    happens next (retry with a different config, escalate to a human,
    abandon) belongs to a later milestone, not to this function.

    Raises `ValueError` for the same malformed `(role, config_digest,
    provider, candidate, reason)` inputs `cowork_control_plane.fingerprint`
    itself rejects — this function never substitutes its own fingerprint
    construction for a bad input.
    """
    fp = control_plane.fingerprint(
        role, config_digest, provider, candidate, reason)
    record_fields = dict(fields or {})
    record_fields.update({
        "role": role,
        "config_digest": config_digest,
        "provider": provider,
        "candidate": candidate,
        "reason": reason,
    })
    outcome = cowork_ledger.append_breaker_attempt(
        ledger_path, fp, TRIP_THRESHOLD, record_fields)
    return {
        "fingerprint": fp,
        "threshold": TRIP_THRESHOLD,
        "attempt_count": outcome["attempt_count"],
        "tripped": outcome["tripped"],
        "record": outcome["record"],
    }


def history(ledger_path, role, config_digest, provider, candidate, reason):
    """Durable, read-only trip history for this exact cause, oldest first.

    For inspection/diagnostics only: does not itself decide trip/no-trip
    (see `attempt`) and never appends. Reads straight from `ledger_path` on
    every call, so it reflects durable truth even immediately after a
    crash/restart.
    """
    fp = control_plane.fingerprint(
        role, config_digest, provider, candidate, reason)
    return cowork_ledger.read_breaker_history(ledger_path, fp)
