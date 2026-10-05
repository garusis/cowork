#!/usr/bin/env python3
"""Focused tests for the recovery-episode evidence module.

Every fixture is synthetic: ids are placeholders, digests are built at runtime
and every directory is a throwaway temp dir. Nothing here launches a provider
or reads a real session store.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_recovery_evidence
"""

import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_recovery_evidence as rec  # noqa: E402


def _sha(n):
    return hashlib.sha256(("neutral-%s" % n).encode("utf-8")).hexdigest()


def _art(path, n):
    return {"path": path, "sha256": _sha(n)}


def _finding(fid, state="open", **extra):
    out = {"id": fid, "kind": "finding", "state": state}
    out.update(extra)
    return out


def _marker(fid, state):
    return {"id": fid, "kind": "finding", "state": state, "marker": True}


def _closure(**fields):
    out = {"kind": "closure"}
    out.update(fields)
    return out


def _tmpdir(case):
    path = tempfile.mkdtemp(prefix="recev-")
    case.addCleanup(shutil.rmtree, path, True)
    return path


def _episode(failed="W-fail-1", recovery="W-rec-1", **kwargs):
    args = dict(
        failed_role="engineer", failed_work_id=failed,
        recovery_work_id=recovery, reason_code="schema_repair",
        before_artifacts=[_art("a.txt", 1)],
        after_artifacts=[_art("a.txt", 2)],
        before_records=[], after_records=[])
    args.update(kwargs)
    return rec.build_episode(**args)


class RecoveryClassificationTests(unittest.TestCase):
    def test_each_code_maps_to_its_class_with_recovery_cost(self):
        for code in ("schema_repair", "orchestration_debugging",
                     "unchanged_replay"):
            self.assertEqual(
                rec.classify_recovery_turn(code),
                {"reason_class": code, "cost_class": "recovery"})

    def test_reason_classes_are_the_closed_set(self):
        self.assertEqual(
            rec.REASON_CLASSES,
            ("schema_repair", "orchestration_debugging", "unchanged_replay"))

    def test_unknown_codes_are_refused_not_guessed(self):
        for bad in ("other", "", None, "Schema_Repair", " schema_repair",
                    "schema_repair ", 7, ["schema_repair"]):
            with self.assertRaises(rec.UnknownReasonCode):
                rec.classify_recovery_turn(bad)

    def test_unknown_code_is_a_value_error(self):
        self.assertTrue(issubclass(rec.UnknownReasonCode, ValueError))

    def test_build_episode_with_unknown_code_raises_and_writes_nothing(self):
        directory = _tmpdir(self)
        with self.assertRaises(rec.UnknownReasonCode):
            _episode(reason_code="mystery")
        self.assertEqual(os.listdir(directory), [])


class ArtifactDeltaTests(unittest.TestCase):
    def test_identical_lists_are_unchanged(self):
        side = [_art("a", 1), _art("b", 2)]
        delta = rec.artifact_delta(side, list(side))
        self.assertEqual(delta["state"], "unchanged")
        self.assertEqual(delta["paths"], {"a": "unchanged", "b": "unchanged"})

    def test_changed_hash_is_changed_and_listed(self):
        delta = rec.artifact_delta([_art("a", 1)], [_art("a", 2)])
        self.assertEqual(delta["state"], "changed")
        self.assertEqual(delta["changed"], ["a"])

    def test_added_and_removed_paths(self):
        delta = rec.artifact_delta([_art("a", 1)], [_art("b", 1)])
        self.assertEqual(delta["state"], "changed")
        self.assertEqual(delta["added"], ["b"])
        self.assertEqual(delta["removed"], ["a"])

    def test_both_empty_lists_are_a_real_unchanged_state(self):
        self.assertEqual(rec.artifact_delta([], [])["state"], "unchanged")

    def test_order_does_not_matter(self):
        left = [_art("a", 1), _art("b", 2)]
        right = [_art("b", 2), _art("a", 1)]
        self.assertEqual(rec.artifact_delta(left, right)["state"],
                         "unchanged")

    def test_uppercase_hex_equals_lowercase_hex(self):
        upper = {"path": "a", "sha256": _sha(1).upper()}
        self.assertEqual(
            rec.artifact_delta([_art("a", 1)], [upper])["state"], "unchanged")

    def test_missing_or_non_list_side_is_unknown_never_unchanged(self):
        for bad in (None, "x", {"a": 1}, 3):
            for delta in (rec.artifact_delta(bad, [_art("a", 1)]),
                          rec.artifact_delta([_art("a", 1)], bad),
                          rec.artifact_delta(bad, bad)):
                self.assertEqual(delta["state"], "unknown")
                self.assertNotEqual(delta["state"], "unchanged")

    def test_structurally_invalid_entries_are_unknown(self):
        good = [_art("a", 1)]
        for bad in ([{"sha256": _sha(1)}], [{"path": "", "sha256": _sha(1)}],
                    ["a"], [_art("a", 1), _art("a", 2)]):
            self.assertEqual(rec.artifact_delta(good, bad)["state"],
                             "unknown")
            self.assertEqual(rec.artifact_delta(bad, good)["state"],
                             "unknown")

    def test_invalid_hash_makes_that_path_unknown(self):
        for sha in (None, "short", "z" * 64, 5):
            bad = [{"path": "a", "sha256": sha}]
            delta = rec.artifact_delta(bad, [_art("a", 1)])
            self.assertEqual(delta["paths"]["a"], "unknown")
            self.assertEqual(delta["state"], "unknown")
            delta = rec.artifact_delta(bad, bad)
            self.assertEqual(delta["state"], "unknown")

    def test_definite_change_stands_beside_an_unknown_path(self):
        before = [_art("a", 1), {"path": "b", "sha256": None}]
        after = [_art("a", 2), {"path": "b", "sha256": None}]
        delta = rec.artifact_delta(before, after)
        self.assertEqual(delta["state"], "changed")
        self.assertEqual(delta["paths"]["b"], "unknown")

    def test_normalize_keeps_invalid_hash_as_none(self):
        norm = rec.normalize_artifacts(
            [{"path": "b", "sha256": "bad"}, _art("a", 1)])
        self.assertEqual(norm, [{"path": "a", "sha256": _sha(1)},
                                {"path": "b", "sha256": None}])
        self.assertIsNone(rec.normalize_artifacts(None))
        self.assertIsNone(rec.normalize_artifacts([_art("a", 1)] * 2))


class FindingDeltaTests(unittest.TestCase):
    def test_no_change_is_unchanged(self):
        history = [_finding("F-1")]
        delta = rec.finding_delta(history, list(history))
        self.assertEqual(delta, {"state": "unchanged", "new": [],
                                 "closed": [], "retired": []})

    def test_empty_histories_are_unchanged_not_unknown(self):
        self.assertEqual(rec.finding_delta([], [])["state"], "unchanged")

    def test_new_open_finding(self):
        delta = rec.finding_delta([], [_finding("F-1")])
        self.assertEqual(delta["state"], "changed")
        self.assertEqual(delta["new"], ["F-1"])

    def test_open_to_closed_state_is_closed(self):
        before = [_finding("F-1")]
        after = before + [_marker("F-1", "closed")]
        delta = rec.finding_delta(before, after)
        self.assertEqual(delta["closed"], ["F-1"])
        self.assertEqual(delta["retired"], [])

    def test_withdrawn_and_superseded_are_retired_not_closed(self):
        before = [_finding("F-1"), _finding("F-2")]
        after = before + [_marker("F-1", "withdrawn"),
                          _marker("F-2", "superseded")]
        delta = rec.finding_delta(before, after)
        self.assertEqual(delta["retired"], ["F-1", "F-2"])
        self.assertEqual(delta["closed"], [])
        self.assertEqual(delta["new"], [])

    def test_marker_records_fold_to_latest_state(self):
        before = [_finding("F-1")]
        after = before + [_marker("F-1", "withdrawn"),
                          _marker("F-1", "open")]
        self.assertEqual(rec.finding_delta(before, after)["state"],
                         "unchanged")

    def test_source_ids_key_findings_apart_from_plain_ids(self):
        before = [_finding("F-1", source_session="S1",
                           source_finding_id="F-9")]
        keyed = rec.open_finding_keys(before)
        self.assertEqual(keyed, ["src:S1:F-9"])
        after = before + [_finding("F-2")]
        delta = rec.finding_delta(before, after)
        self.assertEqual(delta["new"], ["F-2"])
        # a freshly minted id spelled like the source id is a different finding
        reminted = before + [_finding("F-9")]
        delta = rec.finding_delta(before, reminted)
        self.assertEqual(delta["new"], ["F-9"])
        self.assertEqual(delta["closed"], [])
        self.assertEqual(rec.open_finding_keys(reminted),
                         ["F-9", "src:S1:F-9"])

    def test_other_kinds_are_ignored(self):
        before = [{"id": "D-1", "kind": "decision", "state": "open"}]
        after = before + [{"id": "D-2", "kind": "decision"},
                          {"kind": "attempt", "id": "V-1"}]
        self.assertEqual(rec.finding_delta(before, after)["state"],
                         "unchanged")

    def test_closure_by_closes_replacement_id_closes_the_finding(self):
        before = [_finding("F-1", source_session="S1",
                           source_finding_id="F-1")]
        after = before + [
            _finding("F-7", source_session="S1", source_finding_id="F-1"),
            _closure(closes="F-7")]
        delta = rec.finding_delta(before, after)
        self.assertEqual(delta["closed"], ["src:S1:F-1"])
        self.assertEqual(delta["new"], [])

    def test_closure_by_source_ids_closes_the_finding(self):
        before = [_finding("F-1", source_session="S1",
                           source_finding_id="F-4")]
        after = before + [_closure(source_session="S1",
                                   source_finding_id="F-4")]
        delta = rec.finding_delta(before, after)
        self.assertEqual(delta["closed"], ["src:S1:F-4"])
        self.assertEqual(delta["retired"], [])

    def test_closure_plus_closed_state_counts_once(self):
        before = [_finding("F-1")]
        after = before + [_marker("F-1", "closed"), _closure(closes="F-1")]
        self.assertEqual(rec.finding_delta(before, after)["closed"], ["F-1"])

    def test_already_closed_before_is_not_open_before(self):
        history = [_finding("F-1"), _closure(closes="F-1")]
        self.assertEqual(rec.open_finding_keys(history), [])
        delta = rec.finding_delta(history, history + [_finding("F-2")])
        self.assertEqual(delta["closed"], [])
        self.assertEqual(delta["new"], ["F-2"])

    def test_closure_naming_nothing_open_credits_nothing(self):
        before = [_finding("F-1")]
        for closure in (_closure(closes="F-404"),
                        _closure(source_session="S9",
                                 source_finding_id="F-404"),
                        _closure()):
            delta = rec.finding_delta(before, before + [closure])
            self.assertEqual(delta["state"], "unchanged")
            self.assertEqual(delta["closed"], [])

    def test_closure_alone_does_not_make_the_delta_unknown(self):
        delta = rec.finding_delta([], [_closure(closes="F-1")])
        self.assertEqual(delta["state"], "unchanged")

    def test_closure_with_conflicting_ids_is_ignored(self):
        before = [_finding("F-1", source_session="S1",
                           source_finding_id="F-1"),
                  _finding("F-2", source_session="S1",
                           source_finding_id="F-2")]
        after = before + [_closure(closes="F-1", source_session="S1",
                                   source_finding_id="F-2")]
        delta = rec.finding_delta(before, after)
        self.assertEqual(delta["closed"], [])
        self.assertEqual(delta["state"], "unchanged")

    def test_unreadable_sides_are_unknown(self):
        good = [_finding("F-1")]
        collapse_shaped = {"F-1": _finding("F-1")}
        for bad in (None, "x", collapse_shaped, [1, 2], [None]):
            self.assertEqual(rec.finding_delta(bad, good)["state"],
                             "unknown")
            self.assertEqual(rec.finding_delta(good, bad)["state"],
                             "unknown")

    def test_open_finding_missing_from_a_filtered_after_is_unknown(self):
        before = [_finding("F-1"), _finding("F-2")]
        after = [_finding("F-2")]
        delta = rec.finding_delta(before, after)
        self.assertEqual(delta["state"], "unknown")
        self.assertEqual(delta["closed"], [])

    def test_unrecognised_state_is_unknown(self):
        before = [_finding("F-1")]
        after = before + [_marker("F-1", "mystery")]
        self.assertEqual(rec.finding_delta(before, after)["state"],
                         "unknown")

    def test_open_finding_keys_is_none_for_unreadable_input(self):
        self.assertIsNone(rec.open_finding_keys(None))
        self.assertIsNone(rec.open_finding_keys({"F-1": {}}))
        self.assertEqual(rec.open_finding_keys([]), [])


class ValueAttributionTests(unittest.TestCase):
    def _a(self, state):
        return {"state": state}

    def test_unchanged_turn_is_zero_with_full_overhead(self):
        value = rec.attribute_value(
            rec.artifact_delta([_art("a", 1)], [_art("a", 1)]),
            rec.finding_delta([], []))
        self.assertEqual(value["state"], "zero")
        self.assertEqual(value["recovery_overhead"], "full")

    def test_changed_artifact_is_positive(self):
        value = rec.attribute_value(
            rec.artifact_delta([_art("a", 1)], [_art("a", 2)]),
            rec.finding_delta([], []))
        self.assertEqual(value["state"], "positive")
        self.assertEqual(value["recovery_overhead"], "partial")

    def test_closure_earns_value_once(self):
        before = [_finding("F-1", source_session="S1",
                           source_finding_id="F-1")]
        for closure in (_closure(closes="F-1"),
                        _closure(source_session="S1",
                                 source_finding_id="F-1"),
                        _marker("F-1", "closed")):
            delta = rec.finding_delta(before, before + [closure])
            same = rec.artifact_delta([_art("a", 1)], [_art("a", 1)])
            first = rec.attribute_value(same, delta)
            self.assertEqual(first["state"], "positive")
            self.assertEqual(first["credited_finding_keys"], ["src:S1:F-1"])
            replay = rec.attribute_value(same, delta, first[
                "credited_finding_keys"])
            self.assertEqual(replay["state"], "zero")
            self.assertEqual(replay["credited_finding_keys"], [])

    def test_credit_memory_comes_from_stored_episodes(self):
        before = [_finding("F-1")]
        after = before + [_closure(closes="F-1")]
        first = _episode(before_artifacts=[_art("a", 1)],
                         after_artifacts=[_art("a", 1)],
                         before_records=before, after_records=after)
        self.assertEqual(first["value"]["state"], "positive")
        credited = rec.credited_finding_keys([first, "junk", {"value": 3}])
        self.assertEqual(credited, ["F-1"])
        replay = _episode(recovery="W-rec-2",
                          before_artifacts=[_art("a", 1)],
                          after_artifacts=[_art("a", 1)],
                          before_records=before, after_records=after,
                          credited_keys=credited)
        self.assertEqual(replay["value"]["state"], "zero")

    def test_new_finding_is_positive(self):
        value = rec.attribute_value(
            self._a("unchanged"), rec.finding_delta([], [_finding("F-1")]))
        self.assertEqual(value["state"], "positive")

    def test_retired_only_is_zero(self):
        before = [_finding("F-1")]
        delta = rec.finding_delta(before, before + [_marker("F-1",
                                                            "withdrawn")])
        self.assertEqual(
            rec.attribute_value(self._a("unchanged"), delta)["state"],
            "zero")

    def test_closure_naming_nothing_open_is_zero(self):
        before = [_finding("F-1")]
        delta = rec.finding_delta(before,
                                  before + [_closure(closes="F-404")])
        self.assertEqual(
            rec.attribute_value(self._a("unchanged"), delta)["state"],
            "zero")

    def test_missing_input_is_unknown_never_zero(self):
        known = rec.finding_delta([], [])
        unknown_art = rec.artifact_delta(None, [_art("a", 1)])
        unknown_find = rec.finding_delta(None, [])
        unchanged_art = rec.artifact_delta([], [])
        for value in (rec.attribute_value(unknown_art, known),
                      rec.attribute_value(unchanged_art, unknown_find),
                      rec.attribute_value(unknown_art, unknown_find)):
            self.assertEqual(value["state"], "unknown")
            self.assertEqual(value["recovery_overhead"], "unknown")

    def test_unknown_artifact_with_creditable_finding_is_positive(self):
        delta = rec.finding_delta([], [_finding("F-1")])
        value = rec.attribute_value(rec.artifact_delta(None, None), delta)
        self.assertEqual(value["state"], "positive")

    def test_garbage_delta_input_is_unknown(self):
        for bad in (None, "x", 3, [], {"state": "mystery"}, {}):
            self.assertEqual(rec.attribute_value(bad, bad)["state"],
                             "unknown")
        self.assertEqual(
            rec.attribute_value(self._a("unchanged"), None)["state"],
            "unknown")


class LeadReopenDecisionTests(unittest.TestCase):
    def test_durable_reference_is_recorded_without_flags(self):
        out = rec.lead_reopen_decision("D-1", ["D-1", "D-2"])
        self.assertEqual(out["outcome"], "recorded")
        self.assertEqual(out["reason_state"], "durable")
        self.assertEqual(out["flags"], [])

    def test_absent_reference_is_flagged(self):
        for ref in (None, "", 123, ["D-1"]):
            out = rec.lead_reopen_decision(ref, ["D-1"])
            self.assertEqual(out["outcome"], "flagged")
            self.assertEqual(out["reason_state"], "absent")
            self.assertEqual(out["flags"], [rec.FLAG_REOPEN_WITHOUT_REASON])
            self.assertIsNone(out["reason_ref"])

    def test_unknown_reference_is_flagged(self):
        out = rec.lead_reopen_decision("D-9", ["D-1"])
        self.assertEqual(out["outcome"], "flagged")
        self.assertEqual(out["reason_state"], "unknown")
        self.assertEqual(out["reason_ref"], "D-9")

    def test_unreadable_known_ids_record_without_a_flag(self):
        for known in (None, "D-1", 5):
            out = rec.lead_reopen_decision("D-1", known)
            self.assertEqual(out["reason_state"], "unverified")
            self.assertEqual(out["outcome"], "recorded")
            self.assertEqual(out["flags"], [])

    def test_result_never_carries_an_action_and_never_says_prevented(self):
        for ref in (None, "", "D-1", "D-9", 4):
            for known in (None, [], ["D-1"], ("D-1",), {"D-1"}):
                out = rec.lead_reopen_decision(ref, known)
                for banned in ("action", "dispatch", "block", "prevent"):
                    self.assertFalse(
                        [k for k in out if banned in k], out)
                self.assertNotIn("prevented", json.dumps(out))


class InventoryReasonStateTests(unittest.TestCase):
    def test_repeated_with_durable_reason_has_no_flag(self):
        out = rec.inventory_reason_state("D-1", ["D-1"], True)
        self.assertEqual(out["flags"], [])
        self.assertEqual(out["reason_state"], "durable")

    def test_repeated_without_reason_is_flagged(self):
        for ref in (None, "", "D-9"):
            out = rec.inventory_reason_state(ref, ["D-1"], True)
            self.assertEqual(out["flags"], [rec.FLAG_REPEATED_INVENTORY])

    def test_not_repeated_or_unknown_repetition_raises_no_flag(self):
        for repeated in (False, None):
            out = rec.inventory_reason_state(None, ["D-1"], repeated)
            self.assertEqual(out["flags"], [])
        self.assertIsNone(
            rec.inventory_reason_state(None, [], None)["repeated"])

    def test_unreadable_known_ids_raise_no_flag(self):
        out = rec.inventory_reason_state("D-1", None, True)
        self.assertEqual(out["reason_state"], "unverified")
        self.assertEqual(out["flags"], [])

    def test_reread_state_is_echoed_or_refused(self):
        self.assertEqual(rec.REREAD_STATES, ("instructed", "detected"))
        for state in rec.REREAD_STATES:
            out = rec.inventory_reason_state("D-1", ["D-1"], True, state)
            self.assertEqual(out["reread_state"], state)
        for bad in ("prevented", "other", "", 3):
            with self.assertRaises(ValueError):
                rec.inventory_reason_state("D-1", ["D-1"], True, bad)

    def test_prevented_is_not_representable(self):
        self.assertNotIn("prevented", rec.REREAD_STATES)
        for ref in (None, "D-1", "D-9"):
            for known in (None, ["D-1"]):
                for repeated in (True, False, None):
                    for state in (None,) + rec.REREAD_STATES:
                        out = rec.inventory_reason_state(
                            ref, known, repeated, state)
                        self.assertNotIn("prevented", json.dumps(out))
                        self.assertFalse(
                            [k for k in out if "action" in k or
                             "block" in k or "dispatch" in k])


class EpisodeSchemaTests(unittest.TestCase):
    def test_episode_has_exactly_the_contracted_keys(self):
        episode = _episode()
        self.assertEqual(set(episode), set(rec.EPISODE_KEYS))
        self.assertEqual(rec.validate_episode(episode), [])
        json.dumps(episode)

    def test_episode_id_is_deterministic_and_pair_specific(self):
        first = _episode()["episode_id"]
        self.assertEqual(first, _episode()["episode_id"])
        self.assertNotEqual(first, _episode(recovery="W-rec-2")["episode_id"])
        self.assertNotEqual(first, _episode(failed="W-fail-2")["episode_id"])
        self.assertTrue(first.startswith("RE-"))

    def test_unchanged_turn_is_zero_with_recovery_cost(self):
        episode = _episode(after_artifacts=[_art("a.txt", 1)])
        self.assertEqual(episode["value"]["state"], "zero")
        self.assertEqual(episode["value"]["recovery_overhead"], "full")
        self.assertEqual(episode["cost_class"], "recovery")

    def test_changed_turn_has_value(self):
        self.assertEqual(_episode()["value"]["state"], "positive")

    def test_missing_artifact_side_is_stored_none_and_unknown(self):
        episode = _episode(after_artifacts=None)
        self.assertIsNone(episode["after"]["artifacts"])
        self.assertEqual(episode["artifact_delta"]["state"], "unknown")
        self.assertEqual(episode["value"]["state"], "unknown")

    def test_missing_records_store_none_and_unknown(self):
        episode = _episode(after_artifacts=[_art("a.txt", 1)],
                           before_records=None, after_records=None)
        self.assertIsNone(episode["before"]["open_finding_ids"])
        self.assertEqual(episode["finding_delta"]["state"], "unknown")
        self.assertEqual(episode["value"]["state"], "unknown")

    def test_open_finding_ids_are_sorted_keys(self):
        episode = _episode(after_records=[_finding("F-2"), _finding("F-1")])
        self.assertEqual(episode["after"]["open_finding_ids"],
                         ["F-1", "F-2"])

    def test_reason_ref_is_stored_verbatim(self):
        self.assertEqual(_episode(reason_ref="D-3")["reason_ref"], "D-3")
        self.assertIsNone(_episode()["reason_ref"])

    def test_bad_identity_arguments_are_refused(self):
        for kwargs in ({"failed_role": ""}, {"failed": ""},
                       {"recovery": ""}, {"failed": None},
                       {"reason_ref": 5}):
            with self.assertRaises(ValueError):
                _episode(**kwargs)

    def test_validate_reports_tampering(self):
        episode = _episode()
        bad = dict(episode, episode_id="RE-0000000000000000")
        self.assertTrue(rec.validate_episode(bad))
        self.assertTrue(rec.validate_episode(
            dict(episode, reason_class="mystery")))
        self.assertTrue(rec.validate_episode(
            dict(episode, cost_class="other")))
        self.assertTrue(rec.validate_episode(
            dict(episode, value={"state": "maybe"})))
        missing = dict(episode)
        del missing["finding_delta"]
        self.assertTrue(rec.validate_episode(missing))
        self.assertTrue(rec.validate_episode(None))


class EpisodePersistenceTests(unittest.TestCase):
    def _lines(self, directory):
        with open(os.path.join(directory, rec.EPISODES_FILENAME)) as fh:
            return fh.read().split("\n")

    def test_first_append_writes_one_line(self):
        directory = os.path.join(_tmpdir(self), "nested", "eps")
        stored, created = rec.append_episode(directory, _episode())
        self.assertTrue(created)
        self.assertEqual(stored["failed_work_id"], "W-fail-1")
        self.assertEqual(self._lines(directory)[1:], [""])
        self.assertEqual(len([ln for ln in self._lines(directory) if ln]), 1)

    def test_same_key_is_idempotent_even_with_different_content(self):
        directory = _tmpdir(self)
        first = _episode()
        rec.append_episode(directory, first)
        other = _episode(reason_code="unchanged_replay")
        stored, created = rec.append_episode(directory, other)
        self.assertFalse(created)
        self.assertEqual(stored["reason_class"], "schema_repair")
        self.assertEqual(len(rec.read_episodes(directory)), 1)

    def test_different_recovery_work_id_appends_a_second_line(self):
        directory = _tmpdir(self)
        rec.append_episode(directory, _episode())
        _, created = rec.append_episode(directory, _episode(recovery="W-rec-2"))
        self.assertTrue(created)
        self.assertEqual(
            [e["recovery_work_id"] for e in rec.read_episodes(directory)],
            ["W-rec-1", "W-rec-2"])

    def test_restart_stays_idempotent(self):
        directory = _tmpdir(self)
        rec.append_episode(directory, _episode())
        before = rec.read_episodes(directory)
        _, created = rec.append_episode(directory, _episode())
        self.assertFalse(created)
        self.assertEqual(rec.read_episodes(directory), before)

    def test_only_the_episodes_file_is_written(self):
        root = _tmpdir(self)
        directory = os.path.join(root, "eps")
        sibling = os.path.join(root, "other")
        os.makedirs(sibling)
        rec.append_episode(directory, _episode())
        rec.append_episode(directory, _episode(recovery="W-rec-2"))
        self.assertEqual(os.listdir(directory), [rec.EPISODES_FILENAME])
        self.assertEqual(os.listdir(sibling), [])
        self.assertEqual(sorted(os.listdir(root)), ["eps", "other"])

    def test_torn_partial_tail_is_skipped_and_next_append_is_terminated(self):
        directory = _tmpdir(self)
        rec.append_episode(directory, _episode())
        path = os.path.join(directory, rec.EPISODES_FILENAME)
        with open(path, "ab") as fh:
            fh.write(b'{"failed_work_id": "W-torn", "recovery_work')
        self.assertEqual(len(rec.read_episodes(directory)), 1)
        _, created = rec.append_episode(directory, _episode(recovery="W-rec-2"))
        self.assertTrue(created)
        episodes = rec.read_episodes(directory)
        self.assertEqual([e["recovery_work_id"] for e in episodes],
                         ["W-rec-1", "W-rec-2"])
        with open(path, "rb") as fh:
            self.assertTrue(fh.read().endswith(b"\n"))

    def test_complete_object_without_newline_counts_for_idempotency(self):
        directory = _tmpdir(self)
        path = os.path.join(directory, rec.EPISODES_FILENAME)
        with open(path, "wb") as fh:
            fh.write(json.dumps(_episode(), sort_keys=True).encode("utf-8"))
        self.assertEqual(len(rec.read_episodes(directory)), 1)
        _, created = rec.append_episode(directory, _episode())
        self.assertFalse(created)
        _, created = rec.append_episode(directory, _episode(recovery="W-rec-2"))
        self.assertTrue(created)
        self.assertEqual(len(rec.read_episodes(directory)), 2)

    def test_garbage_blank_and_non_object_lines_are_skipped(self):
        directory = _tmpdir(self)
        path = os.path.join(directory, rec.EPISODES_FILENAME)
        good = json.dumps(_episode(), sort_keys=True)
        with open(path, "wb") as fh:
            fh.write(b"\n   \nnot json\n[1, 2]\n3\n"
                     b'{"failed_work_id": "x"}\n'
                     b'{"failed_work_id": "", "recovery_work_id": "y"}\n'
                     + good.encode("utf-8") + b"\n\xff\xfe\n")
        episodes = rec.read_episodes(directory)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["failed_work_id"], "W-fail-1")

    def test_first_line_per_key_wins_on_read(self):
        directory = _tmpdir(self)
        path = os.path.join(directory, rec.EPISODES_FILENAME)
        first = _episode()
        second = dict(first, reason_class="unchanged_replay")
        with open(path, "w") as fh:
            fh.write(json.dumps(first) + "\n" + json.dumps(second) + "\n")
        episodes = rec.read_episodes(directory)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["reason_class"], "schema_repair")

    def test_missing_directory_reads_empty_and_creates_nothing(self):
        root = _tmpdir(self)
        directory = os.path.join(root, "absent")
        self.assertEqual(rec.read_episodes(directory), [])
        self.assertFalse(os.path.exists(directory))
        self.assertEqual(rec.read_episodes(None), [])
        self.assertEqual(rec.read_episodes(""), [])

    def test_invalid_input_raises_and_creates_nothing(self):
        root = _tmpdir(self)
        directory = os.path.join(root, "eps")
        with self.assertRaises(ValueError):
            rec.append_episode(directory, {"failed_work_id": "x"})
        with self.assertRaises(ValueError):
            rec.append_episode(directory, None)
        with self.assertRaises(ValueError):
            rec.append_episode("", _episode())
        with self.assertRaises(ValueError):
            rec.append_episode(None, _episode())
        self.assertEqual(os.listdir(root), [])

    def test_unserialisable_episode_is_refused(self):
        directory = _tmpdir(self)
        episode = _episode()
        episode["extra"] = object()
        with self.assertRaises(ValueError):
            rec.append_episode(directory, episode)
        self.assertEqual(os.listdir(directory), [])

    def test_concurrent_appends_of_one_key_yield_one_line(self):
        directory = _tmpdir(self)
        results = []

        def worker():
            results.append(rec.append_episode(directory, _episode())[1])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(True), 1)
        lines = [ln for ln in self._lines(directory) if ln]
        self.assertEqual(len(lines), 1)


class ModuleBoundaryTests(unittest.TestCase):
    def _imports(self):
        path = os.path.join(_HERE, "cowork_recovery_evidence.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names.add((node.module or "").split(".")[0])
        return names

    def test_imports_no_sibling_module(self):
        siblings = {os.path.splitext(f)[0] for f in os.listdir(_HERE)
                    if f.endswith(".py")}
        names = self._imports()
        self.assertFalse(names & siblings, names & siblings)
        self.assertFalse([n for n in names if n.startswith("cowork")])

    def test_imports_no_process_or_network_module(self):
        banned = {"subprocess", "socket", "urllib", "http", "requests",
                  "multiprocessing", "threading", "asyncio"}
        self.assertFalse(self._imports() & banned)

    def test_contracted_public_names_exist(self):
        for name in ("classify_recovery_turn", "artifact_delta",
                     "finding_delta", "attribute_value",
                     "lead_reopen_decision", "inventory_reason_state",
                     "build_episode", "append_episode", "read_episodes"):
            self.assertTrue(callable(getattr(rec, name, None)), name)


if __name__ == "__main__":
    unittest.main()
