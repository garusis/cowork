#!/usr/bin/env python3
"""Tests for per-round finding lifecycle derivation in `cowork_finding_lifecycle`.

Both derivations (chain transition events and persisted verdict-copy snapshot
rows) are exercised on a small synthetic story: four findings opened in round
1; three closed, one withdrawn and two opened in round 2; two closed and two
opened in round 3. Hand-computed columns are asserted from both derivations,
then lifecycle variants, reconciliation under single-record corruption, and
reported-counter validation. Every input is synthetic and built in this file.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_authority_core_lifecycle
"""

import copy
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_finding_lifecycle as lifecycle  # noqa: E402

SHA = "c" * 64


def _events(*rounds):
    """Build chain events from (round_id, [(kind, finding_id), ...]) pairs."""
    out = []
    for round_id, items in rounds:
        for kind, finding_id in items:
            out.append(
                {
                    "seq": len(out) + 1,
                    "round": round_id,
                    "finding_id": finding_id,
                    "kind": kind,
                    "id": "rec-%d" % (len(out) + 1),
                }
            )
    return out


def _snap(round_id, finding_ids=(), closed=(), withdrawn=(), duplicate=(), reopened=(), **over):
    row = {
        "round_id": round_id,
        "phase": "build",
        "seat": "reviewer",
        "verdict_copy_path": "verdicts/%s.json" % (round_id,),
        "verdict_copy_sha256": SHA,
        "reported": {
            "finding_ids": list(finding_ids),
            "closed_ids": list(closed),
            "withdrawn_ids": list(withdrawn),
            "duplicate_ids": list(duplicate),
            "reopened_ids": list(reopened),
        },
        "trace_findings_count": None,
    }
    row.update(over)
    return row


def _fixture_events():
    return _events(
        (1, [("opened", "F1"), ("opened", "F2"), ("opened", "F3"), ("opened", "F4")]),
        (
            2,
            [
                ("closed", "F1"),
                ("closed", "F2"),
                ("closed", "F3"),
                ("withdrawn", "F4"),
                ("opened", "F5"),
                ("opened", "F6"),
            ],
        ),
        (3, [("closed", "F5"), ("closed", "F6"), ("opened", "F7"), ("opened", "F8")]),
    )


def _fixture_snapshots():
    return [
        _snap(1, finding_ids=["F1", "F2", "F3", "F4"]),
        _snap(2, finding_ids=["F5", "F6"], closed=["F1", "F2", "F3"], withdrawn=["F4"]),
        _snap(3, finding_ids=["F7", "F8"], closed=["F5", "F6"]),
    ]


def _column(rows, name):
    return [row[name] for row in rows]


class FixtureColumnsTests(unittest.TestCase):
    def _both(self):
        return (
            lifecycle.deltas_from_transitions(_fixture_events()),
            lifecycle.deltas_from_snapshots(_fixture_snapshots()),
        )

    def test_columns_from_both_derivations(self):
        expected = {
            "round": [1, 2, 3],
            "newly_opened": [4, 2, 2],
            "reopened": [0, 0, 0],
            "closed": [0, 3, 2],
            "withdrawn": [0, 1, 0],
            "duplicate": [0, 0, 0],
            "retracted": [0, 1, 0],
            "still_open": [4, 2, 2],
            "open_at_end": [4, 2, 2],
            "cumulative_unique": [4, 6, 8],
            "cumulative_closed": [0, 3, 5],
            "cumulative_retracted": [0, 1, 1],
            "discovery_yield": [4, 2, 2],
        }
        for rows in self._both():
            self.assertEqual(len(rows), 3)
            for name, values in expected.items():
                self.assertEqual(_column(rows, name), values, name)

    def test_row_columns_are_exactly_the_declared_set(self):
        for rows in self._both():
            for row in rows:
                self.assertEqual(set(row), set(lifecycle.ROW_COLUMNS))

    def test_identity_columns_list_the_findings(self):
        for rows in self._both():
            self.assertEqual(rows[0]["newly_opened_ids"], ["F1", "F2", "F3", "F4"])
            self.assertEqual(rows[1]["closed_ids"], ["F1", "F2", "F3"])
            self.assertEqual(rows[1]["withdrawn_ids"], ["F4"])
            self.assertEqual(rows[2]["newly_opened_ids"], ["F7", "F8"])
            for row in rows:
                for counter, column in lifecycle.ID_COLUMN_FOR_COUNTER.items():
                    self.assertEqual(len(row[column]), row[counter])

    def test_both_derivations_agree(self):
        events_rows, snapshot_rows = self._both()
        self.assertEqual(events_rows, snapshot_rows)
        self.assertEqual(lifecycle.reconcile_derivations(events_rows, snapshot_rows), [])

    def test_string_round_ids_are_carried_verbatim(self):
        events = _events(
            ("r-a", [("opened", "F1")]),
            ("r-b", [("closed", "F1")]),
        )
        rows = lifecycle.deltas_from_transitions(events)
        self.assertEqual(_column(rows, "round"), ["r-a", "r-b"])
        snaps = [_snap("r-a", finding_ids=["F1"]), _snap("r-b", closed=["F1"])]
        self.assertEqual(lifecycle.deltas_from_snapshots(snaps), rows)

    def test_inputs_are_not_mutated(self):
        events = _fixture_events()
        snaps = _fixture_snapshots()
        before = copy.deepcopy((events, snaps))
        lifecycle.deltas_from_transitions(events)
        lifecycle.deltas_from_snapshots(snaps)
        self.assertEqual((events, snaps), before)


class LifecycleVariantTests(unittest.TestCase):
    def test_rereport_never_raises_newly_opened(self):
        events = _events(
            (1, [("opened", "F1")]),
            (2, [("opened", "F1"), ("opened", "F2")]),
        )
        rows = lifecycle.deltas_from_transitions(events)
        self.assertEqual(_column(rows, "newly_opened"), [1, 1])
        self.assertEqual(_column(rows, "cumulative_unique"), [1, 2])
        snaps = [_snap(1, finding_ids=["F1"]), _snap(2, finding_ids=["F1", "F2"])]
        self.assertEqual(lifecycle.deltas_from_snapshots(snaps), rows)

    def test_rereport_of_closed_finding_changes_nothing(self):
        events = _events(
            (1, [("opened", "F1")]),
            (2, [("closed", "F1")]),
            (3, [("opened", "F1")]),
        )
        rows = lifecycle.deltas_from_transitions(events)
        self.assertEqual(_column(rows, "newly_opened"), [1, 0, 0])
        self.assertEqual(_column(rows, "still_open"), [1, 0, 0])

    def test_reopen_after_close_counts_as_discovery_not_unique(self):
        events = _events(
            (1, [("opened", "F1")]),
            (2, [("closed", "F1")]),
            (3, [("reopened", "F1")]),
        )
        snaps = [
            _snap(1, finding_ids=["F1"]),
            _snap(2, closed=["F1"]),
            _snap(3, reopened=["F1"]),
        ]
        for rows in (
            lifecycle.deltas_from_transitions(events),
            lifecycle.deltas_from_snapshots(snaps),
        ):
            self.assertEqual(_column(rows, "reopened"), [0, 0, 1])
            self.assertEqual(_column(rows, "newly_opened"), [1, 0, 0])
            self.assertEqual(_column(rows, "discovery_yield"), [1, 0, 1])
            self.assertEqual(_column(rows, "cumulative_unique"), [1, 1, 1])
            self.assertEqual(_column(rows, "still_open"), [1, 0, 1])
            self.assertEqual(_column(rows, "cumulative_closed"), [0, 1, 1])

    def test_plain_still_open_does_not_raise_yield(self):
        snaps = [
            _snap(1, finding_ids=["F1", "F2"]),
            _snap(2, finding_ids=["F1", "F2"]),
            _snap(3, finding_ids=["F1", "F2"]),
        ]
        rows = lifecycle.deltas_from_snapshots(snaps)
        self.assertEqual(_column(rows, "discovery_yield"), [2, 0, 0])
        self.assertEqual(_column(rows, "still_open"), [2, 2, 2])

    def test_listing_only_new_or_every_open_derives_identically(self):
        only_new = _fixture_snapshots()
        every_open = [
            _snap(1, finding_ids=["F1", "F2", "F3", "F4"]),
            _snap(
                2,
                finding_ids=["F1", "F2", "F3", "F4", "F5", "F6"],
                closed=["F1", "F2", "F3"],
                withdrawn=["F4"],
            ),
            _snap(3, finding_ids=["F5", "F6", "F7", "F8"], closed=["F5", "F6"]),
        ]
        self.assertEqual(
            lifecycle.deltas_from_snapshots(only_new),
            lifecycle.deltas_from_snapshots(every_open),
        )

    def test_duplicate_is_counted_apart_from_withdrawn(self):
        events = _events(
            (1, [("opened", "F1"), ("opened", "F2")]),
            (2, [("duplicate", "F1"), ("withdrawn", "F2")]),
        )
        snaps = [
            _snap(1, finding_ids=["F1", "F2"]),
            _snap(2, duplicate=["F1"], withdrawn=["F2"]),
        ]
        for rows in (
            lifecycle.deltas_from_transitions(events),
            lifecycle.deltas_from_snapshots(snaps),
        ):
            self.assertEqual(_column(rows, "withdrawn"), [0, 1])
            self.assertEqual(_column(rows, "duplicate"), [0, 1])
            self.assertEqual(_column(rows, "retracted"), [0, 2])
            self.assertEqual(_column(rows, "cumulative_retracted"), [0, 2])
            self.assertEqual(_column(rows, "still_open"), [2, 0])
        only_duplicate = _events(
            (1, [("opened", "F1")]),
            (2, [("duplicate", "F1")]),
        )
        rows = lifecycle.deltas_from_transitions(only_duplicate)
        self.assertEqual(_column(rows, "withdrawn"), [0, 0])
        self.assertEqual(_column(rows, "duplicate"), [0, 1])

    def test_retraction_in_the_round_of_opening(self):
        events = _events((1, [("opened", "F1"), ("withdrawn", "F1"), ("opened", "F2")]))
        snaps = [_snap(1, finding_ids=["F1", "F2"], withdrawn=["F1"])]
        for rows in (
            lifecycle.deltas_from_transitions(events),
            lifecycle.deltas_from_snapshots(snaps),
        ):
            self.assertEqual(_column(rows, "newly_opened"), [2])
            self.assertEqual(_column(rows, "withdrawn"), [1])
            self.assertEqual(_column(rows, "still_open"), [1])
            self.assertEqual(_column(rows, "cumulative_unique"), [2])

    def test_retracted_finding_is_terminal(self):
        events = _events(
            (1, [("opened", "F1")]),
            (2, [("withdrawn", "F1")]),
            (3, [("reopened", "F1")]),
        )
        rows = lifecycle.deltas_from_transitions(events)
        self.assertEqual(_column(rows, "reopened"), [0, 0, 0])
        self.assertEqual(_column(rows, "still_open"), [1, 0, 0])
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.deltas_from_transitions(events, strict=True)

    def test_empty_rounds_carry_cumulative_values_and_open_level(self):
        events = _events(
            (1, [("opened", "F1"), ("opened", "F2")]),
            (3, [("closed", "F1")]),
        )
        rows = lifecycle.deltas_from_transitions(events, rounds=[1, 2, 3])
        self.assertEqual(_column(rows, "round"), [1, 2, 3])
        self.assertEqual(_column(rows, "newly_opened"), [2, 0, 0])
        self.assertEqual(_column(rows, "closed"), [0, 0, 1])
        self.assertEqual(_column(rows, "still_open"), [2, 2, 1])
        self.assertEqual(_column(rows, "cumulative_unique"), [2, 2, 2])
        self.assertEqual(_column(rows, "cumulative_closed"), [0, 0, 1])
        self.assertEqual(_column(rows, "discovery_yield"), [2, 0, 0])

    def test_no_events_yield_no_rows_and_listed_rounds_are_all_zero(self):
        self.assertEqual(lifecycle.deltas_from_transitions([]), [])
        rows = lifecycle.deltas_from_transitions([], rounds=[1, 2])
        for row in rows:
            for column in lifecycle.COUNT_COLUMNS:
                self.assertEqual(row[column], 0, column)
            for column in lifecycle.ID_COLUMNS:
                self.assertEqual(row[column], [])
        self.assertEqual(lifecycle.deltas_from_snapshots([]), [])
        empty = lifecycle.deltas_from_snapshots([_snap(1), _snap(2)])
        self.assertEqual(empty, rows)

    def test_rounds_argument_must_cover_event_rounds_in_order(self):
        events = _events((1, [("opened", "F1")]), (2, [("opened", "F2")]))
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.deltas_from_transitions(events, rounds=[1])
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.deltas_from_transitions(events, rounds=[2, 1])
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.deltas_from_transitions(events, rounds=[1, 1, 2])

    def test_discovery_yield_is_opened_plus_reopened_on_every_row(self):
        events = _events(
            (1, [("opened", "F1"), ("opened", "F2")]),
            (2, [("closed", "F1")]),
            (3, [("reopened", "F1"), ("opened", "F3")]),
        )
        rows = lifecycle.deltas_from_transitions(events)
        for row in rows:
            self.assertEqual(
                lifecycle.discovery_yield(row), row["newly_opened"] + row["reopened"]
            )
            self.assertEqual(row["discovery_yield"], lifecycle.discovery_yield(row))
        self.assertEqual(_column(rows, "discovery_yield"), [2, 0, 2])

    def test_discovery_yield_rejects_unusable_rows(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.discovery_yield({"newly_opened": True, "reopened": 0})
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.discovery_yield({"newly_opened": 1})
        with self.assertRaises(lifecycle.LifecycleUnavailable):
            lifecycle.discovery_yield({"round": 1, "unavailable": "verdict_copy_missing"})

    def test_illegal_transitions_are_ignored_unless_strict(self):
        illegal = [
            ("closed", "GHOST"),
            ("withdrawn", "GHOST"),
            ("duplicate", "GHOST"),
            ("reopened", "GHOST"),
        ]
        for kind, finding_id in illegal:
            events = _events((1, [("opened", "F1")]), (2, [(kind, finding_id)]))
            rows = lifecycle.deltas_from_transitions(events)
            self.assertEqual(_column(rows, "still_open"), [1, 1], kind)
            self.assertEqual(rows[1][kind], 0, kind)
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                lifecycle.deltas_from_transitions(events, strict=True)
            self.assertIn("GHOST", str(caught.exception))
            self.assertIn("2", str(caught.exception))
        for first, second in (
            ("opened", "reopened"),
            ("closed", "closed"),
            ("closed", "withdrawn"),
            ("withdrawn", "closed"),
            ("duplicate", "duplicate"),
        ):
            items = [("opened", "F1")]
            if first != "opened":
                items.append((first, "F1"))
            events = _events((1, items), (2, [(second, "F1")]))
            lifecycle.deltas_from_transitions(events)
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.deltas_from_transitions(events, strict=True)

    def test_malformed_events_raise(self):
        base = {"seq": 1, "round": 1, "finding_id": "F1", "kind": "opened", "id": "x"}
        bad = [
            "not a list",
            ["not a dict"],
            [dict(base, kind="resolved")],
            [dict(base, round=None)],
            [dict(base, round=True)],
            [dict(base, finding_id=7)],
            [dict(base, finding_id="")],
            [{k: v for k, v in base.items() if k != "round"}],
        ]
        for events in bad:
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.deltas_from_transitions(events)
        interleaved = _events(
            (1, [("opened", "F1")]),
            (2, [("opened", "F2")]),
            (1, [("opened", "F3")]),
        )
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.deltas_from_transitions(interleaved)

    def test_missing_sources_raise_unavailable_never_zeros(self):
        with self.assertRaises(lifecycle.LifecycleUnavailable):
            lifecycle.deltas_from_transitions(None)
        with self.assertRaises(lifecycle.LifecycleUnavailable):
            lifecycle.deltas_from_snapshots(None)

    def test_illegal_snapshot_entries_follow_the_same_posture(self):
        snaps = [_snap(1, finding_ids=["F1"]), _snap(2, closed=["GHOST"])]
        rows = lifecycle.deltas_from_snapshots(snaps)
        self.assertEqual(_column(rows, "closed"), [0, 0])
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.deltas_from_snapshots(snaps, strict=True)


class SnapshotShapeTests(unittest.TestCase):
    def test_trace_findings_count_is_type_checked_only(self):
        for good in (None, 0, 5):
            rows = lifecycle.deltas_from_snapshots(
                [_snap(1, finding_ids=["F1"], trace_findings_count=good)]
            )
            self.assertEqual(rows[0]["still_open"], 1)
        for bad in (-1, True, "3", 1.5):
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.deltas_from_snapshots(
                    [_snap(1, finding_ids=["F1"], trace_findings_count=bad)]
                )

    def test_repeated_round_id_raises(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.deltas_from_snapshots([_snap(1), _snap(1)])

    def test_malformed_rows_raise(self):
        row = _snap(1, finding_ids=["F1"])
        bad_reported = copy.deepcopy(row)
        bad_reported["reported"]["closed_ids"] = "F1"
        unknown_field = copy.deepcopy(row)
        unknown_field["reported"]["closed_id"] = []
        no_round = {k: v for k, v in row.items() if k != "round_id"}
        for rows in (
            "nope",
            ["nope"],
            [no_round],
            [bad_reported],
            [unknown_field],
            [dict(row, reported=[])],
            [_snap(True)],
        ):
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.deltas_from_snapshots(rows)

    def test_missing_copy_marks_the_row_and_every_later_row(self):
        for broken in (
            {"verdict_copy_path": None},
            {"verdict_copy_path": ""},
            {"verdict_copy_sha256": None},
            {"verdict_copy_sha256": "   "},
            {"reported": None},
        ):
            snaps = _fixture_snapshots()
            snaps[1].update(broken)
            rows = lifecycle.deltas_from_snapshots(snaps)
            self.assertEqual(rows[0]["newly_opened"], 4)
            self.assertEqual(rows[1], {"round": 2, "unavailable": "verdict_copy_missing"})
            self.assertEqual(rows[2], {"round": 3, "unavailable": "upstream_unavailable"})

    def test_absent_copy_keys_are_unavailable(self):
        snaps = _fixture_snapshots()
        del snaps[0]["verdict_copy_sha256"]
        rows = lifecycle.deltas_from_snapshots(snaps)
        self.assertEqual(
            [row.get("unavailable") for row in rows],
            ["verdict_copy_missing", "upstream_unavailable", "upstream_unavailable"],
        )

    def test_omitted_reported_lists_are_empty(self):
        row = _snap(1, finding_ids=["F1"])
        row["reported"] = {"finding_ids": ["F1"]}
        rows = lifecycle.deltas_from_snapshots([row])
        self.assertEqual(rows[0]["newly_opened"], 1)
        self.assertEqual(rows[0]["closed"], 0)


_LISTS = ("finding_ids", "closed_ids", "withdrawn_ids", "duplicate_ids", "reopened_ids")
_RETYPE = {
    "opened": "duplicate",
    "closed": "withdrawn",
    "withdrawn": "duplicate",
    "duplicate": "withdrawn",
    "reopened": "closed",
}


def _event_corruptions(events):
    """Yield (label, round, corrupted events) for every single-record change."""
    for index, event in enumerate(events):
        finding = event["finding_id"]
        other = "F1" if finding != "F1" else "F2"
        dropped = events[:index] + events[index + 1 :]
        yield "drop", event["round"], dropped, index
        retyped = copy.deepcopy(events)
        retyped[index]["kind"] = _RETYPE[event["kind"]]
        yield "retype", event["round"], retyped, index
        for target in ("F99", other):
            renamed = copy.deepcopy(events)
            renamed[index]["finding_id"] = target
            yield "rename:" + target, event["round"], renamed, index


def _snapshot_records(snaps):
    """Every transition-bearing record: (row index, list name, position).

    Re-report entries (a known id listed again in finding_ids) are not
    transition records and are excluded.
    """
    known = set()
    for row_index, row in enumerate(snaps):
        reported = row["reported"]
        for name in _LISTS:
            for position, finding in enumerate(reported[name]):
                if name == "finding_ids" and finding in known:
                    continue
                yield row_index, name, position
        known.update(reported["finding_ids"])


def _snapshot_corruptions(snaps):
    for row_index, name, position in list(_snapshot_records(snaps)):
        round_id = snaps[row_index]["round_id"]
        finding = snaps[row_index]["reported"][name][position]
        other = "F1" if finding != "F1" else "F2"
        dropped = copy.deepcopy(snaps)
        del dropped[row_index]["reported"][name][position]
        yield "drop", round_id, dropped, (row_index, name, position)
        moved = copy.deepcopy(snaps)
        del moved[row_index]["reported"][name][position]
        target = _LISTS[(_LISTS.index(name) + 1) % len(_LISTS)]
        moved[row_index]["reported"][target].append(finding)
        yield "retype", round_id, moved, (row_index, name, position)
        for new_id in ("F99", other):
            renamed = copy.deepcopy(snaps)
            renamed[row_index]["reported"][name][position] = new_id
            yield "rename:" + new_id, round_id, renamed, (row_index, name, position)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.clean_events = lifecycle.deltas_from_transitions(_fixture_events())
        self.clean_snaps = lifecycle.deltas_from_snapshots(_fixture_snapshots())

    def _assert_typed_and_local(self, mismatches, round_id, context):
        self.assertTrue(mismatches, context)
        for item in mismatches:
            self.assertEqual({"round", "column", "code"} <= set(item), True, context)
            self.assertIn(item["code"], lifecycle.MISMATCH_CODES, context)
        local = [
            item
            for item in mismatches
            if item["round"] == round_id
            and (
                item["code"] == "missing_round"
                or item["column"] in lifecycle.COUNT_COLUMNS
            )
        ]
        self.assertTrue(local, "%s: no mismatch names round %r: %r" % (context, round_id, mismatches))

    def test_clean_rows_reconcile(self):
        self.assertEqual(
            lifecycle.reconcile_derivations(self.clean_events, self.clean_snaps), []
        )

    def test_every_single_event_corruption_is_reported(self):
        events = _fixture_events()
        count = 0
        for label, round_id, corrupted, index in _event_corruptions(events):
            rows = lifecycle.deltas_from_transitions(corrupted)
            context = "events %s #%s" % (label, index)
            self._assert_typed_and_local(
                lifecycle.reconcile_derivations(rows, self.clean_snaps), round_id, context
            )
            self._assert_typed_and_local(
                lifecycle.reconcile_derivations(self.clean_snaps, rows), round_id, context
            )
            count += 1
        self.assertEqual(count, 4 * len(events))

    def test_every_single_snapshot_corruption_is_reported(self):
        snaps = _fixture_snapshots()
        count = 0
        for label, round_id, corrupted, where in _snapshot_corruptions(snaps):
            rows = lifecycle.deltas_from_snapshots(corrupted)
            context = "snapshots %s %s" % (label, where)
            self._assert_typed_and_local(
                lifecycle.reconcile_derivations(self.clean_events, rows), round_id, context
            )
            self._assert_typed_and_local(
                lifecycle.reconcile_derivations(rows, self.clean_events), round_id, context
            )
            count += 1
        self.assertEqual(count, 4 * len(list(_snapshot_records(snaps))))

    def test_count_preserving_renames_are_caught_by_membership(self):
        events = _fixture_events()
        for index, event in enumerate(events):
            if event["finding_id"] in ("F7", "F1") and event["kind"] == "opened":
                renamed = copy.deepcopy(events)
                renamed[index]["finding_id"] = "F99"
                rows = lifecycle.deltas_from_transitions(renamed)
                found = lifecycle.reconcile_derivations(rows, self.clean_snaps)
                hits = [
                    m
                    for m in found
                    if m["round"] == event["round"] and m["column"] == "newly_opened"
                ]
                self.assertEqual(len(hits), 1, found)
                self.assertIn("a_ids", hits[0])
                self.assertIn("F99", hits[0]["a_ids"])
        snaps = _fixture_snapshots()
        snaps[2]["reported"]["finding_ids"][0] = "F99"
        rows = lifecycle.deltas_from_snapshots(snaps)
        self.assertEqual(rows[2]["newly_opened"], self.clean_events[2]["newly_opened"])
        found = lifecycle.reconcile_derivations(self.clean_events, rows)
        hits = [m for m in found if m["round"] == 3 and m["column"] == "newly_opened"]
        self.assertEqual(len(hits), 1, found)
        self.assertEqual(hits[0]["a"], hits[0]["b"])
        self.assertEqual(hits[0]["a_ids"], ["F7", "F8"])
        self.assertEqual(hits[0]["b_ids"], ["F8", "F99"])

    def test_count_and_ids_differing_give_one_mismatch_for_the_column(self):
        snaps = _fixture_snapshots()
        del snaps[0]["reported"]["finding_ids"][0]
        rows = lifecycle.deltas_from_snapshots(snaps)
        found = lifecycle.reconcile_derivations(self.clean_events, rows)
        hits = [m for m in found if m["round"] == 1 and m["column"] == "newly_opened"]
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]["a"], hits[0]["b"]), (4, 3))
        self.assertIn("a_ids", hits[0])

    def test_rereport_entries_are_not_transition_records(self):
        with_rereport = _fixture_snapshots()
        with_rereport[1]["reported"]["finding_ids"] = ["F1", "F2", "F5", "F6"]
        rows = lifecycle.deltas_from_snapshots(with_rereport)
        self.assertEqual(rows, self.clean_snaps)
        self.assertEqual(lifecycle.reconcile_derivations(self.clean_events, rows), [])
        events = _fixture_events()
        events.insert(
            4,
            {"seq": 99, "round": 2, "finding_id": "F1", "kind": "opened", "id": "x"},
        )
        rows = lifecycle.deltas_from_transitions(events)
        self.assertEqual(rows, self.clean_events)

    def test_dropping_the_only_record_of_a_round_names_the_round(self):
        events = _events(
            (1, [("opened", "F1")]),
            (2, [("opened", "F2")]),
        )
        full = lifecycle.deltas_from_transitions(events)
        short = lifecycle.deltas_from_transitions(events[:1])
        found = lifecycle.reconcile_derivations(full, short)
        self.assertEqual(
            found, [{"round": 2, "column": None, "code": "missing_round", "present_in": "a"}]
        )
        found = lifecycle.reconcile_derivations(short, full)
        self.assertEqual(found[0]["present_in"], "b")
        self.assertEqual(found[0]["round"], 2)

    def test_rounds_align_by_id_not_position(self):
        shuffled = list(reversed(self.clean_snaps))
        self.assertEqual(lifecycle.reconcile_derivations(self.clean_events, shuffled), [])

    def test_missing_side_is_unavailable(self):
        found = lifecycle.reconcile_derivations(None, self.clean_snaps)
        self.assertEqual([m["code"] for m in found], ["unavailable"])
        self.assertEqual(found[0]["side"], "a")
        found = lifecycle.reconcile_derivations(None, None)
        self.assertEqual([m["side"] for m in found], ["a", "b"])

    def test_snapshot_gap_is_reported_against_a_readable_chain(self):
        snaps = _fixture_snapshots()
        snaps[1]["verdict_copy_sha256"] = ""
        rows = lifecycle.deltas_from_snapshots(snaps)
        found = lifecycle.reconcile_derivations(self.clean_events, rows)
        self.assertEqual(
            [(m["round"], m["code"]) for m in found],
            [(2, "unavailable"), (3, "unavailable")],
        )
        self.assertEqual(lifecycle.reconcile_derivations(rows, rows), [])

    def test_duplicate_round_rows_are_reported(self):
        doubled = self.clean_events + [copy.deepcopy(self.clean_events[0])]
        found = lifecycle.reconcile_derivations(doubled, self.clean_snaps)
        self.assertEqual([(m["round"], m["code"]) for m in found], [(1, "duplicate_round")])

    def test_inputs_are_not_mutated(self):
        before = copy.deepcopy((self.clean_events, self.clean_snaps))
        snaps = _fixture_snapshots()
        snaps[0]["reported"]["finding_ids"].append("F9")
        other = lifecycle.deltas_from_snapshots(snaps)
        other_before = copy.deepcopy(other)
        lifecycle.reconcile_derivations(self.clean_events, other)
        self.assertEqual((self.clean_events, self.clean_snaps), before)
        self.assertEqual(other, other_before)


class ValidateReportedTests(unittest.TestCase):
    def setUp(self):
        self.derived = lifecycle.deltas_from_transitions(_fixture_events())

    def _counters_only(self):
        return [
            {"round": row["round"], **{c: row[c] for c in lifecycle.COUNT_COLUMNS}}
            for row in self.derived
        ]

    def test_equal_reports_have_no_mismatch(self):
        self.assertEqual(
            lifecycle.validate_reported_counters(self._counters_only(), self.derived), []
        )
        self.assertEqual(
            lifecycle.validate_reported_counters(copy.deepcopy(self.derived), self.derived),
            [],
        )

    def test_one_off_in_any_column_is_exactly_one_typed_mismatch(self):
        for index, row in enumerate(self.derived):
            for column in lifecycle.COUNT_COLUMNS:
                for delta in (1, -1):
                    reported = self._counters_only()
                    reported[index][column] += delta
                    found = lifecycle.validate_reported_counters(reported, self.derived)
                    self.assertEqual(len(found), 1, (row["round"], column, delta))
                    item = found[0]
                    self.assertEqual(item["code"], "column_mismatch")
                    self.assertEqual(item["round"], row["round"])
                    self.assertEqual(item["column"], column)
                    self.assertEqual(item["reported"], row[column] + delta)
                    self.assertEqual(item["derived"], row[column])

    def test_inputs_survive_validation_unchanged(self):
        reported = self._counters_only()
        reported[1]["closed"] += 1
        before = copy.deepcopy((reported, self.derived))
        found = lifecycle.validate_reported_counters(reported, self.derived)
        self.assertEqual(len(found), 1)
        self.assertEqual((reported, self.derived), before)

    def test_partial_rows_compare_only_present_columns(self):
        reported = [{"round": 2, "closed": 3}, {"round": 3, "still_open": 2}]
        self.assertEqual(lifecycle.validate_reported_counters(reported, self.derived), [])
        reported = [{"round": 2, "closed": 4}]
        found = lifecycle.validate_reported_counters(reported, self.derived)
        self.assertEqual([(m["round"], m["column"]) for m in found], [(2, "closed")])
        self.assertEqual(lifecycle.validate_reported_counters([], self.derived), [])

    def test_id_columns_are_compared_only_when_reported(self):
        reported = [{"round": 2, "closed": 3}]
        self.assertEqual(lifecycle.validate_reported_counters(reported, self.derived), [])
        reported = [{"round": 2, "closed_ids": ["F3", "F2", "F1"]}]
        self.assertEqual(lifecycle.validate_reported_counters(reported, self.derived), [])
        reported = [{"round": 2, "closed_ids": ["F1", "F2", "F9"]}]
        found = lifecycle.validate_reported_counters(reported, self.derived)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["round"], found[0]["column"]), (2, "closed"))
        self.assertEqual(found[0]["code"], "column_mismatch")
        self.assertEqual(found[0]["derived_ids"], ["F1", "F2", "F3"])
        reported = [{"round": 2, "closed": 3, "closed_ids": ["F1", "F2", "F9"]}]
        found = lifecycle.validate_reported_counters(reported, self.derived)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["reported"], 3)

    def test_unknown_column_and_invalid_values(self):
        reported = [{"round": 1, "blocking": 2}]
        found = lifecycle.validate_reported_counters(reported, self.derived)
        self.assertEqual(
            found, [{"round": 1, "column": "blocking", "code": "unknown_column"}]
        )
        for value in ("4", 4.0, True, None, [4]):
            found = lifecycle.validate_reported_counters(
                [{"round": 1, "newly_opened": value}], self.derived
            )
            self.assertEqual(len(found), 1, value)
            self.assertEqual(found[0]["code"], "invalid_value")
            self.assertEqual(found[0]["column"], "newly_opened")
        found = lifecycle.validate_reported_counters(
            [{"round": 1, "closed_ids": "F1"}], self.derived
        )
        self.assertEqual(found[0]["code"], "invalid_value")
        found = lifecycle.validate_reported_counters([{"closed": 1}], self.derived)
        self.assertEqual(found[0]["code"], "invalid_value")
        self.assertIsNone(found[0]["round"])

    def test_missing_and_duplicate_rounds(self):
        found = lifecycle.validate_reported_counters(
            [{"round": 9, "closed": 0}], self.derived
        )
        self.assertEqual(
            found,
            [{"round": 9, "column": None, "code": "missing_round", "present_in": "reported"}],
        )
        found = lifecycle.validate_reported_counters(
            [{"round": 1, "closed": 0}, {"round": 1, "closed": 5}], self.derived
        )
        self.assertEqual([(m["round"], m["code"]) for m in found], [(1, "duplicate_round")])

    def test_unavailable_derived_data_is_never_zeros(self):
        snaps = _fixture_snapshots()
        snaps[1]["verdict_copy_sha256"] = ""
        derived = lifecycle.deltas_from_snapshots(snaps)
        reported = [{"round": 1, "newly_opened": 4}, {"round": 2, "closed": 0}, {"round": 3, "closed": 0}]
        found = lifecycle.validate_reported_counters(reported, derived)
        self.assertEqual(
            [(m["round"], m["code"]) for m in found],
            [(2, "unavailable"), (3, "unavailable")],
        )
        self.assertEqual(found[0]["reason"], "verdict_copy_missing")
        self.assertEqual(found[1]["reason"], "upstream_unavailable")

    def test_missing_side_is_unavailable(self):
        found = lifecycle.validate_reported_counters(None, self.derived)
        self.assertEqual([(m["code"], m["side"]) for m in found], [("unavailable", "reported")])
        found = lifecycle.validate_reported_counters(self._counters_only(), None)
        self.assertEqual([(m["code"], m["side"]) for m in found], [("unavailable", "derived")])

    def test_trace_count_can_be_validated_as_reported_still_open(self):
        snaps = _fixture_snapshots()
        for row, level in zip(snaps, (4, 2, 3)):
            row["trace_findings_count"] = level
        derived = lifecycle.deltas_from_snapshots(snaps)
        reported = [
            {"round": row["round_id"], "still_open": row["trace_findings_count"]}
            for row in snaps
        ]
        found = lifecycle.validate_reported_counters(reported, derived)
        self.assertEqual([(m["round"], m["column"]) for m in found], [(3, "still_open")])


if __name__ == "__main__":
    unittest.main()
