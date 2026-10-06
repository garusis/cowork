#!/usr/bin/env python3
"""Pure per-round finding lifecycle derivation, reconciliation and reported
counter validation.

PURE BY CONSTRUCTION. Every function takes plain lists and dicts and returns
plain lists and dicts. Nothing here reads a file, a clock, the network or any
other cowork module, and no input is ever mutated: rows and mismatches are
fresh copies. A caller hands in the transition events of a folded chain
(`[{round, finding_id, kind}, ...]`, kind in opened / reopened / closed /
withdrawn / duplicate) or the persisted per-round snapshot rows
(`{round_id, phase, seat, verdict_copy_path, verdict_copy_sha256, reported:
{finding_ids, closed_ids, withdrawn_ids, duplicate_ids, reopened_ids},
trace_findings_count}`).

TWO INDEPENDENT DERIVATIONS. `deltas_from_transitions` and
`deltas_from_snapshots` read different inputs through different adapters and
share only the state-machine walker that counts. `reconcile_derivations`
compares their rows by round id; a record that is altered on one side makes the
two sides differ, so corruption is reported rather than absorbed.

THE LIFECYCLE. Per finding id: open -> closed, open -> withdrawn | duplicate
(terminal retraction), closed -> open (reopen). An `opened` entry for an id that
is already known is a re-report and changes nothing, so a re-report never
raises `newly_opened`. A reopen raises `reopened` and `discovery_yield`
(`newly_opened + reopened`) and leaves `cumulative_unique` alone. `still_open`
is a level (findings open after the round), never a flow, and never feeds
`discovery_yield`. `cumulative_closed` and `cumulative_retracted` are running
sums of the per-round `closed` and `retracted` (withdrawn + duplicate) counts.
A snapshot lists its ids in a fixed canonical order: finding_ids, reopened_ids,
closed_ids, withdrawn_ids, duplicate_ids.

ROWS. Every row carries the round id. Besides the counters, a row carries the
sorted finding ids behind each transition counter (`newly_opened_ids`, ...), so
an id that is swapped for another without changing any count still makes the
derivations differ.

ILLEGAL TRANSITIONS. A well-formed entry that the state machine cannot apply
(closing an unknown id, reopening an open one, ...) is ignored by default so the
corrupted side diverges and reconciliation reports it; `strict=True` raises
`LifecycleError` naming the round and finding instead. Malformed shapes always
raise.

UNAVAILABLE DATA IS NEVER ZEROS. A missing source raises `LifecycleUnavailable`
from the derivations and becomes a typed `unavailable` mismatch in the
comparison helpers. A snapshot row without its persisted verdict copy yields
`{round, unavailable: 'verdict_copy_missing'}` and every later row yields
`{round, unavailable: 'upstream_unavailable'}`, because cumulative columns past
a gap cannot be derived honestly.

MISMATCHES are plain dicts `{round, column, code, ...}`; codes are listed in
`MISMATCH_CODES`.

Python 3.9+, stdlib only.
"""

COUNT_COLUMNS = (
    "newly_opened",
    "reopened",
    "closed",
    "withdrawn",
    "duplicate",
    "retracted",
    "still_open",
    "open_at_end",
    "cumulative_unique",
    "cumulative_closed",
    "cumulative_retracted",
    "discovery_yield",
)
ID_COLUMN_FOR_COUNTER = {
    "newly_opened": "newly_opened_ids",
    "reopened": "reopened_ids",
    "closed": "closed_ids",
    "withdrawn": "withdrawn_ids",
    "duplicate": "duplicate_ids",
}
ID_COLUMNS = tuple(ID_COLUMN_FOR_COUNTER.values())
ROW_COLUMNS = ("round",) + COUNT_COLUMNS + ID_COLUMNS
SNAPSHOT_ROW_FIELDS = (
    "round_id",
    "phase",
    "seat",
    "verdict_copy_path",
    "verdict_copy_sha256",
    "reported",
    "trace_findings_count",
)
REPORTED_FIELDS = (
    "finding_ids",
    "closed_ids",
    "withdrawn_ids",
    "duplicate_ids",
    "reopened_ids",
)
MISMATCH_CODES = (
    "column_mismatch",
    "missing_round",
    "unavailable",
    "unknown_column",
    "invalid_value",
    "duplicate_round",
)

_EVENT_KINDS = ("opened", "reopened", "closed", "withdrawn", "duplicate")
_COUNTER_FOR_ID_COLUMN = {v: k for k, v in ID_COLUMN_FOR_COUNTER.items()}
# Snapshot reported list -> transition kind, in canonical replay order.
_SNAPSHOT_REPLAY = (
    ("finding_ids", "opened"),
    ("reopened_ids", "reopened"),
    ("closed_ids", "closed"),
    ("withdrawn_ids", "withdrawn"),
    ("duplicate_ids", "duplicate"),
)


class LifecycleError(ValueError):
    """A lifecycle input is malformed (or, under strict, illegal)."""


class LifecycleUnavailable(LifecycleError):
    """A lifecycle source is missing and nothing honest can be derived."""


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_round_id(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value != ""
    return isinstance(value, int)


def _valid_finding_id(value):
    return isinstance(value, str) and value != ""


def _usable_text(value):
    return isinstance(value, str) and value.strip() != ""


def _mismatch(code, round_id, column, **extra):
    out = {"round": round_id, "column": column, "code": code}
    out.update(extra)
    return out


# ----------------------------------------------------------------- adapters


def _steps_from_events(events, rounds):
    """Chain events -> [(round_id, [(finding_id, kind), ...], None)]."""
    if events is None:
        raise LifecycleUnavailable("chain transition events are unavailable")
    if not isinstance(events, list):
        raise LifecycleError("events must be a list")
    grouped = []
    seen = set()
    for position, event in enumerate(events):
        if not isinstance(event, dict):
            raise LifecycleError("event %d is not a dict" % position)
        kind = event.get("kind")
        if kind not in _EVENT_KINDS:
            raise LifecycleError("event %d has unknown kind %r" % (position, kind))
        round_id = event.get("round")
        if not _valid_round_id(round_id):
            raise LifecycleError("event %d has no usable round" % position)
        finding_id = event.get("finding_id")
        if not _valid_finding_id(finding_id):
            raise LifecycleError("event %d has no usable finding_id" % position)
        if not grouped or grouped[-1][0] != round_id:
            if round_id in seen:
                raise LifecycleError(
                    "round %r reappears after a later round began" % (round_id,)
                )
            seen.add(round_id)
            grouped.append((round_id, []))
        grouped[-1][1].append((finding_id, kind))
    if rounds is None:
        return [(round_id, steps, None) for round_id, steps in grouped]
    if not isinstance(rounds, list):
        raise LifecycleError("rounds must be a list")
    position_of = {}
    for round_id in rounds:
        if not _valid_round_id(round_id):
            raise LifecycleError("rounds holds an unusable round id %r" % (round_id,))
        if round_id in position_of:
            raise LifecycleError("rounds repeats round %r" % (round_id,))
        position_of[round_id] = len(position_of)
    last = -1
    for round_id, _steps in grouped:
        if round_id not in position_of:
            raise LifecycleError("event round %r is missing from rounds" % (round_id,))
        if position_of[round_id] < last:
            raise LifecycleError("event rounds are out of order against rounds")
        last = position_of[round_id]
    by_round = dict(grouped)
    return [(round_id, by_round.get(round_id, []), None) for round_id in rounds]


def _id_list(value, where):
    if not isinstance(value, list) or not all(_valid_finding_id(v) for v in value):
        raise LifecycleError("%s must be a list of finding id strings" % where)
    return value


def _steps_from_snapshots(snapshot_rows):
    """Snapshot rows -> [(round_id, steps | None, unavailable_reason | None)]."""
    if snapshot_rows is None:
        raise LifecycleUnavailable("snapshot rows are unavailable")
    if not isinstance(snapshot_rows, list):
        raise LifecycleError("snapshot_rows must be a list")
    out = []
    seen = set()
    for position, row in enumerate(snapshot_rows):
        if not isinstance(row, dict):
            raise LifecycleError("snapshot row %d is not a dict" % position)
        round_id = row.get("round_id")
        if not _valid_round_id(round_id):
            raise LifecycleError("snapshot row %d has no usable round_id" % position)
        if round_id in seen:
            raise LifecycleError("snapshot round_id %r repeats" % (round_id,))
        seen.add(round_id)
        count = row.get("trace_findings_count")
        if count is not None and (not _is_count(count) or count < 0):
            raise LifecycleError(
                "snapshot round %r has an unusable trace_findings_count" % (round_id,)
            )
        reported = row.get("reported")
        if reported is not None and not isinstance(reported, dict):
            raise LifecycleError("snapshot round %r reported is not a dict" % (round_id,))
        if (
            reported is None
            or not _usable_text(row.get("verdict_copy_path"))
            or not _usable_text(row.get("verdict_copy_sha256"))
        ):
            out.append((round_id, None, "verdict_copy_missing"))
            continue
        for key in reported:
            if key not in REPORTED_FIELDS:
                raise LifecycleError(
                    "snapshot round %r reported has unknown field %r" % (round_id, key)
                )
        steps = []
        for key, kind in _SNAPSHOT_REPLAY:
            where = "snapshot round %r reported.%s" % (round_id, key)
            for finding_id in _id_list(reported.get(key, []), where):
                steps.append((finding_id, kind))
        out.append((round_id, steps, None))
    return out


# ------------------------------------------------------------------- walker


def _walk(round_steps, strict):
    """Apply the transition table round by round and emit rows."""
    state = {}
    open_count = 0
    cumulative_unique = 0
    cumulative_closed = 0
    cumulative_retracted = 0
    blocked = False
    rows = []
    for round_id, steps, reason in round_steps:
        if blocked:
            rows.append({"round": round_id, "unavailable": "upstream_unavailable"})
            continue
        if steps is None:
            blocked = True
            rows.append({"round": round_id, "unavailable": reason})
            continue
        ids = {counter: [] for counter in ID_COLUMN_FOR_COUNTER}
        for finding_id, kind in steps:
            current = state.get(finding_id)
            if kind == "opened":
                if current is None:
                    state[finding_id] = "open"
                    open_count += 1
                    ids["newly_opened"].append(finding_id)
                continue
            if kind == "reopened" and current == "closed":
                state[finding_id] = "open"
                open_count += 1
                ids["reopened"].append(finding_id)
            elif kind == "closed" and current == "open":
                state[finding_id] = "closed"
                open_count -= 1
                ids["closed"].append(finding_id)
            elif kind in ("withdrawn", "duplicate") and current == "open":
                state[finding_id] = kind
                open_count -= 1
                ids[kind].append(finding_id)
            elif strict:
                raise LifecycleError(
                    "illegal %s transition of finding %r in round %r"
                    % (kind, finding_id, round_id)
                )
        counts = {counter: len(found) for counter, found in ids.items()}
        retracted = counts["withdrawn"] + counts["duplicate"]
        cumulative_unique += counts["newly_opened"]
        cumulative_closed += counts["closed"]
        cumulative_retracted += retracted
        row = {
            "round": round_id,
            "newly_opened": counts["newly_opened"],
            "reopened": counts["reopened"],
            "closed": counts["closed"],
            "withdrawn": counts["withdrawn"],
            "duplicate": counts["duplicate"],
            "retracted": retracted,
            "still_open": open_count,
            "open_at_end": open_count,
            "cumulative_unique": cumulative_unique,
            "cumulative_closed": cumulative_closed,
            "cumulative_retracted": cumulative_retracted,
            "discovery_yield": counts["newly_opened"] + counts["reopened"],
        }
        for counter, column in ID_COLUMN_FOR_COUNTER.items():
            row[column] = sorted(ids[counter])
        rows.append(row)
    return rows


# --------------------------------------------------------------- derivation


def deltas_from_transitions(events, rounds=None, strict=False):
    """Per-round rows derived from the chain's ordered transition events.

    `rounds` optionally lists every round id in order so rounds without events
    still produce rows that carry the cumulative columns and open level.
    """
    return _walk(_steps_from_events(events, rounds), strict)


def deltas_from_snapshots(snapshot_rows, strict=False):
    """Per-round rows derived from persisted verdict-copy snapshot rows."""
    return _walk(_steps_from_snapshots(snapshot_rows), strict)


def discovery_yield(row):
    """Findings a round surfaced: newly opened plus reopened."""
    if not isinstance(row, dict):
        raise LifecycleError("row must be a dict")
    if "unavailable" in row:
        raise LifecycleUnavailable("row is unavailable: %s" % row["unavailable"])
    newly_opened = row.get("newly_opened")
    reopened = row.get("reopened")
    if not _is_count(newly_opened) or not _is_count(reopened):
        raise LifecycleError("row lacks integer newly_opened and reopened")
    return newly_opened + reopened


# ---------------------------------------------------------------- comparison


def _index_rows(rows, side):
    """Return ({round: row}, [round order], mismatches) for one side."""
    if not isinstance(rows, list):
        raise LifecycleError("%s rows must be a list" % side)
    by_round = {}
    order = []
    problems = []
    for row in rows:
        if not isinstance(row, dict):
            raise LifecycleError("%s row is not a dict" % side)
        round_id = row.get("round")
        if not _valid_round_id(round_id):
            problems.append(
                _mismatch("invalid_value", None, "round", side=side, value=round_id)
            )
            continue
        if round_id in by_round:
            problems.append(_mismatch("duplicate_round", round_id, None, side=side))
            continue
        by_round[round_id] = row
        order.append(round_id)
    return by_round, order, problems


def _ids_of(value):
    return sorted(value) if isinstance(value, list) else value


def reconcile_derivations(a, b):
    """Compare two derived row lists by round id; return typed mismatches.

    `a` and `b` are the outputs of the two derivations (or None when a side
    could not be derived). Counters and the finding ids behind each transition
    counter are compared; an id-only difference is reported under the matching
    counter column with the id lists in `a_ids` / `b_ids`.
    """
    mismatches = []
    for side, rows in (("a", a), ("b", b)):
        if rows is None:
            mismatches.append(_mismatch("unavailable", None, None, side=side))
    if a is None or b is None:
        return mismatches
    by_a, order_a, problems_a = _index_rows(a, "a")
    by_b, order_b, problems_b = _index_rows(b, "b")
    mismatches.extend(problems_a)
    mismatches.extend(problems_b)
    for round_id in order_a:
        row_a = by_a[round_id]
        if round_id not in by_b:
            mismatches.append(_mismatch("missing_round", round_id, None, present_in="a"))
            continue
        row_b = by_b[round_id]
        gap_a = row_a.get("unavailable")
        gap_b = row_b.get("unavailable")
        if "unavailable" in row_a or "unavailable" in row_b:
            if "unavailable" in row_a and "unavailable" in row_b and gap_a == gap_b:
                continue
            mismatches.append(_mismatch("unavailable", round_id, None, a=gap_a, b=gap_b))
            continue
        for column in COUNT_COLUMNS:
            value_a = row_a.get(column)
            value_b = row_b.get(column)
            id_column = ID_COLUMN_FOR_COUNTER.get(column)
            ids_a = _ids_of(row_a.get(id_column)) if id_column else None
            ids_b = _ids_of(row_b.get(id_column)) if id_column else None
            if value_a == value_b and ids_a == ids_b:
                continue
            extra = {"a": value_a, "b": value_b}
            if ids_a != ids_b:
                extra["a_ids"] = ids_a
                extra["b_ids"] = ids_b
            mismatches.append(_mismatch("column_mismatch", round_id, column, **extra))
    for round_id in order_b:
        if round_id not in by_a:
            mismatches.append(_mismatch("missing_round", round_id, None, present_in="b"))
    return mismatches


def validate_reported_counters(reported_rows, derived_rows):
    """Compare reviewer-reported counters with derived rows.

    Only the columns a reported row carries are compared, and only for the
    rounds it reports. Disagreements come back as typed mismatches naming round
    and column; neither input is modified and no reported value is replaced.
    """
    mismatches = []
    for side, rows in (("reported", reported_rows), ("derived", derived_rows)):
        if rows is None:
            mismatches.append(_mismatch("unavailable", None, None, side=side))
    if reported_rows is None or derived_rows is None:
        return mismatches
    by_reported, order_reported, problems_reported = _index_rows(
        reported_rows, "reported"
    )
    by_derived, _order_derived, problems_derived = _index_rows(derived_rows, "derived")
    mismatches.extend(problems_reported)
    mismatches.extend(problems_derived)
    for round_id in order_reported:
        reported = by_reported[round_id]
        derived = by_derived.get(round_id)
        if derived is None:
            mismatches.append(
                _mismatch("missing_round", round_id, None, present_in="reported")
            )
            continue
        if "unavailable" in derived:
            mismatches.append(
                _mismatch("unavailable", round_id, None, reason=derived["unavailable"])
            )
            continue
        found = {}
        for column, value in reported.items():
            if column == "round":
                continue
            if column in COUNT_COLUMNS:
                if not _is_count(value):
                    mismatches.append(
                        _mismatch("invalid_value", round_id, column, value=value)
                    )
                    continue
                entry = found.setdefault(column, {})
                entry["reported"] = value
                entry["derived"] = derived.get(column)
            elif column in _COUNTER_FOR_ID_COLUMN:
                if not isinstance(value, list) or not all(
                    _valid_finding_id(v) for v in value
                ):
                    mismatches.append(
                        _mismatch("invalid_value", round_id, column, value=value)
                    )
                    continue
                counter = _COUNTER_FOR_ID_COLUMN[column]
                entry = found.setdefault(counter, {})
                entry["reported_ids"] = sorted(value)
                entry["derived_ids"] = _ids_of(derived.get(column))
            else:
                mismatches.append(_mismatch("unknown_column", round_id, column))
        for column in COUNT_COLUMNS:
            entry = found.get(column)
            if entry is None:
                continue
            differs = "reported" in entry and entry["reported"] != entry["derived"]
            if "reported_ids" in entry and entry["reported_ids"] != entry["derived_ids"]:
                differs = True
            else:
                entry.pop("reported_ids", None)
                entry.pop("derived_ids", None)
            if differs:
                mismatches.append(
                    _mismatch("column_mismatch", round_id, column, **entry)
                )
    return mismatches
