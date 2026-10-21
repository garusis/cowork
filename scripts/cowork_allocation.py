#!/usr/bin/env python3
"""Package dispatch allocation — the pure policy half (issue #116).

WHAT THIS BOUNDS
----------------
A *dispatch allocation* is an explicit, orchestrator-supplied ceiling on how
many REAL PROVIDER LEAD DISPATCHES one work package may spend per phase. It
exists because a package may authorize, in prose, "build2/correction1" while
nothing in the runtime can refuse the third builder turn: the automatic
reviewer-revise and owned-verification-red paths reopen the lead on their own,
with no human attached, and the next supervisor wake is minutes away.

This module owns ONLY the pure arithmetic and the grammar. It performs no I/O,
imports nothing from this repo, and holds no state. The durable store (admission,
atomic consumption, the package-scoped ledger) lives in `cowork_state.py`, which
already owns session persistence and the single `fcntl.flock` locking authority;
the call sites live in `cowork.py`.

WHAT IT IS NOT
--------------
  - It is NOT provider capacity or credits. A quota/overload pause
    (`awaiting_capacity`, rc 5) is the provider declining to serve a dispatch
    this allocation had already authorized and charged. The two never read or
    write each other's records.
  - It is NOT a token/context envelope. Those bound the BYTES of one turn;
    this bounds the NUMBER of turns.
  - It is NOT controller authentication or an execution profile. Execution
    profile policy stays in `cowork_execution_profiles.py`; authentication
    stays in `cowork_profiles.py`. This module touches neither.
  - It does NOT review anything. The paired reviewer gate is untouched: an
    allocation refusal can only end a phase UNAPPROVED. Nothing here can turn
    a refusal into an approval, and no allocation ever substitutes for a
    reviewer.

COUNTER SEMANTICS (the exact thing #116 got wrong)
--------------------------------------------------
`build=2,correction=1` means **the initial builder dispatch plus at most one
correction — two real builder dispatches in total**. It does NOT mean two extra
turns, and it does NOT mean 2+1=3.

For a phase P, let `consumed` be the number of real lead dispatches already
charged to P on this package's durable ledger. The dispatch about to be made is
number `consumed + 1` (its `ordinal`). It is the INITIAL dispatch iff
`consumed == 0`; otherwise it is correction number `consumed`.

It is admitted iff BOTH hold:
    dispatches is None  or  consumed + 1 <= dispatches
    corrections is None or  consumed == 0  or  consumed <= corrections

Worked, for `dispatches=2, corrections=1`:
    consumed=0 -> ordinal 1, initial     -> admitted (ledger -> 1)
    consumed=1 -> ordinal 2, correction 1 -> admitted (ledger -> 2)
    consumed=2 -> ordinal 3, correction 2 -> REFUSED (2+1 > 2)

Zero is meaningful and is not "unlimited":
    dispatches=0 -> the phase may not dispatch at all; the very first lead
                    launch is refused before the provider is constructed.
    corrections=0 with dispatches=5 -> the initial dispatch only.

An ABSENT phase key means "unallocated": that phase keeps its existing,
unbounded-by-this-mechanism behavior. A package with no allocation at all
behaves exactly as before this module existed.

A dispatch is charged when it is ATTEMPTED, not when it succeeds. A real
provider launch costs real money whether or not the turn returns a usable
status, so a failed send does not refund its unit, and a later plain resume
that re-sends the same pending turn is a second real dispatch and is charged
again. This is deliberately the conservative direction: the mechanism can only
ever permit FEWER provider launches than the stated ceiling, never more.
"""

import re

SCHEMA_VERSION = 1

# The three allocatable lead phases, named as the package authorizes them,
# each mapped to the internal `_role_loop` phase string it governs.
PHASES = ("discovery", "planning", "build")

PHASE_BY_LOOP_PHASE = {
    "scouting": "discovery",
    "planning": "planning",
    "building": "build",
}

# Closed refusal vocabulary. Every one of these ends a phase UNAPPROVED and
# requires an operator (a new package), never an answer.
REFUSAL_CODES = (
    "dispatch_limit_exhausted",     # consumed + 1 > dispatches
    "correction_limit_exhausted",   # this would be correction number > corrections
    "allocation_unreadable",        # the ledger is missing, corrupt, or unparseable
    "allocation_binding_mismatch",  # the ledger's limits are not the admitted ones
)

_INT_RE = re.compile(r"^(0|[1-9][0-9]{0,6})$")

# Grammar keys -> (phase, field). `correction`/`corrections` are the package
# vocabulary's alias for the builder correction sub-cap.
_SPEC_KEYS = {}
for _phase in PHASES:
    _SPEC_KEYS[_phase] = (_phase, "dispatches")
    _SPEC_KEYS[_phase + "_corrections"] = (_phase, "corrections")
_SPEC_KEYS["correction"] = ("build", "corrections")
_SPEC_KEYS["corrections"] = ("build", "corrections")
del _phase

SPEC_KEYS = tuple(sorted(_SPEC_KEYS))


class AllocationError(ValueError):
    """A malformed or self-contradictory allocation spec. `reason` is a short
    stable code for the CLI refusal line."""

    def __init__(self, reason, message):
        ValueError.__init__(self, message)
        self.reason = reason


def parse_spec(spec):
    """Parse the `--dispatch-allocation` token into a normalized limits record.

    Grammar: comma-separated `KEY=VALUE`, VALUE a non-negative decimal integer
    (at most 7 digits). KEY is one of `SPEC_KEYS`. At least one key is
    required; a repeated key — including one reached through an alias — is
    refused rather than silently last-wins.

    Returns `{phase: {"dispatches": int|None, "corrections": int|None}}` for
    every phase in `PHASES`; a phase nobody mentioned has both fields None
    ("unallocated", existing behavior). Raises `AllocationError`."""
    if not isinstance(spec, str):
        raise AllocationError("malformed", "the allocation spec must be a string")
    text = spec.strip()
    if not text:
        raise AllocationError("empty", "the allocation spec is empty")
    limits = {p: {"dispatches": None, "corrections": None} for p in PHASES}
    seen = set()
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            raise AllocationError(
                "malformed", "empty entry in the allocation spec %r" % spec)
        if item.count("=") != 1:
            raise AllocationError(
                "malformed",
                "allocation entry %r is not exactly one KEY=VALUE pair" % item)
        key, value = (part.strip() for part in item.split("="))
        key = key.lower()
        if key not in _SPEC_KEYS:
            raise AllocationError(
                "unknown_key",
                "unknown allocation key %r (known: %s)"
                % (key, ", ".join(SPEC_KEYS)))
        if not _INT_RE.match(value):
            raise AllocationError(
                "malformed_value",
                "allocation value for %r must be a non-negative integer, got %r"
                % (key, value))
        phase, field = _SPEC_KEYS[key]
        if (phase, field) in seen:
            raise AllocationError(
                "duplicate_key",
                "allocation key %r sets %s.%s, which was already set"
                % (key, phase, field))
        seen.add((phase, field))
        limits[phase][field] = int(value)
    if not seen:
        raise AllocationError("empty", "the allocation spec sets no limits")
    return validate_limits(limits)


def validate_limits(limits):
    """Validate a limits record (freshly parsed, or read back from a durable
    ledger that may have been edited) and return a normalized copy.

    This is the fail-closed readback gate: anything this rejects makes the
    allocation unusable, and an unusable allocation REFUSES dispatch — it never
    degrades to "unlimited"."""
    if not isinstance(limits, dict):
        raise AllocationError("malformed", "limits must be an object")
    extra = set(limits) - set(PHASES)
    if extra:
        raise AllocationError(
            "unknown_key", "limits has unknown phases: %s" % sorted(extra))
    out = {}
    allocated = False
    for phase in PHASES:
        entry = limits.get(phase)
        if entry is None:
            out[phase] = {"dispatches": None, "corrections": None}
            continue
        if not isinstance(entry, dict):
            raise AllocationError(
                "malformed", "limits.%s must be an object" % phase)
        bad = set(entry) - {"dispatches", "corrections"}
        if bad:
            raise AllocationError(
                "unknown_key",
                "limits.%s has unknown fields: %s" % (phase, sorted(bad)))
        normalized = {}
        for field in ("dispatches", "corrections"):
            value = entry.get(field)
            if value is None:
                normalized[field] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise AllocationError(
                    "malformed_value",
                    "limits.%s.%s must be an integer or null, got %r"
                    % (phase, field, value))
            if value < 0:
                raise AllocationError(
                    "malformed_value",
                    "limits.%s.%s must not be negative, got %r"
                    % (phase, field, value))
            normalized[field] = value
            allocated = True
        out[phase] = normalized
    if not allocated:
        raise AllocationError("empty", "the allocation sets no limits")
    return out


def phase_for_loop_phase(loop_phase):
    """The allocatable phase name governing `_role_loop`'s `phase` string, or
    None when that phase is not allocatable (the pre-phase worktree role, an
    unrecognized phase). A None answer means "no allocation applies"."""
    return PHASE_BY_LOOP_PHASE.get(loop_phase)


def corrections_consumed(consumed):
    """How many of `consumed` charged dispatches were corrections: the first
    one is the initial dispatch, every later one is a correction."""
    return max(0, int(consumed) - 1)


def classify(consumed):
    """`"initial"` for the first dispatch of a phase, `"correction"` after."""
    return "initial" if int(consumed) == 0 else "correction"


def evaluate(limits, phase, consumed):
    """Decide the dispatch that would be number `consumed + 1` for `phase`.

    Returns `{"allowed": bool, "refusal_code": str|None, "ordinal": int,
    "kind": "initial"|"correction", "limit_dispatches": int|None,
    "limit_corrections": int|None, "consumed_dispatches": int,
    "consumed_corrections": int}`.

    Pure: it reads nothing and writes nothing. The caller performs this same
    evaluation INSIDE the durable lock so the decision and the increment are
    one transaction."""
    consumed = int(consumed)
    if consumed < 0:
        raise AllocationError(
            "malformed_value", "consumed must not be negative, got %r" % consumed)
    entry = (limits or {}).get(phase) or {}
    dispatches = entry.get("dispatches")
    corrections = entry.get("corrections")
    ordinal = consumed + 1
    kind = classify(consumed)
    verdict = {
        "allowed": True,
        "refusal_code": None,
        "ordinal": ordinal,
        "kind": kind,
        "limit_dispatches": dispatches,
        "limit_corrections": corrections,
        "consumed_dispatches": consumed,
        "consumed_corrections": corrections_consumed(consumed),
    }
    if dispatches is not None and ordinal > dispatches:
        verdict["allowed"] = False
        verdict["refusal_code"] = "dispatch_limit_exhausted"
        return verdict
    # `consumed` is also the number this dispatch would make the correction
    # count — the initial dispatch is never a correction and is never screened
    # by the correction sub-cap.
    if corrections is not None and consumed > 0 and consumed > corrections:
        verdict["allowed"] = False
        verdict["refusal_code"] = "correction_limit_exhausted"
    return verdict


def describe(limits):
    """One compact, human- and machine-stable rendering of a limits record:
    only the phases that actually carry a bound. Used in trace events and the
    refusal line; never used to make a decision."""
    parts = []
    for phase in PHASES:
        entry = (limits or {}).get(phase) or {}
        if entry.get("dispatches") is not None:
            parts.append("%s=%d" % (phase, entry["dispatches"]))
        if entry.get("corrections") is not None:
            parts.append("%s_corrections=%d" % (phase, entry["corrections"]))
    return ",".join(parts)
