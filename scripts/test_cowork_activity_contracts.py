#!/usr/bin/env python3
"""Tests for cowork_activity: M4 Package A activity/status contracts.

Run standalone:

    python3 -m unittest scripts/test_cowork_activity_contracts.py -v
"""

import ast
import os
import sys
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_activity as activity  # noqa: E402

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_TIME_1 = "2026-08-25T10:00:00Z"
_TIME_2 = "2026-08-25T10:05:00Z"

# Runtime modules that would break this module's inertness if imported.
_FORBIDDEN_RUNTIME_MODULES = frozenset({
    "cowork", "cowork_bridge", "cowork_state", "cowork_ledger", "cowork_ui",
    "cowork_report", "cowork_measure", "cowork_watchdog",
    "cowork_preflight", "cowork_dispatch", "cowork_dispatch_manifest",
    "cowork_policy", "cowork_action_policy", "cowork_guard_broker",
    "cowork_trace", "subprocess", "socket",
})


def _uuid():
    return str(uuid.uuid4())


def _activity_record(**overrides):
    base = dict(
        schema_version=1, record="ActivityRecord",
        work_id=_uuid(), time=_TIME_1,
        activity_class="productive_model_work", source="claude",
        artifact_fingerprint=None, artifact_delta=(),
        provider_health="healthy", age_seconds=0,
    )
    base.update(overrides)
    return base


def _reconciliation_record(**overrides):
    base = dict(
        schema_version=1, record="ActivityReconciliationRecord",
        work_id=_uuid(), time=_TIME_2,
        original_classification="no_evidence_silence",
        reconciled_classification="local_tool_work",
        revision_digest=_HASH_A, quiescence_marker="digest_compare_after_wait",
    )
    base.update(overrides)
    return base


def _scheduled_review_record(**overrides):
    base = dict(
        schema_version=1, record="ScheduledReviewRecord",
        work_id=_uuid(), next_inspection_at=_TIME_2,
        interval_seconds=300, last_inspection_result_ref=None,
    )
    base.update(overrides)
    return base


def _watchdog_decision(**overrides):
    base = dict(
        schema_version=1, record="WatchdogDecision",
        work_id=_uuid(), time=_TIME_2, verdict="no_action",
        durable_evidence_ref=None, process_probe_ref=None,
    )
    base.update(overrides)
    return base


def _controller_turn_outcome(**overrides):
    base = dict(
        schema_version=1, record="ControllerTurnOutcome",
        outcome="no_first_token", failure_class=None,
    )
    base.update(overrides)
    return base


class ActivityRecordTest(unittest.TestCase):

    def test_valid_record_round_trips(self):
        record = _activity_record()
        normalized = activity.validate_activity_record(record)
        self.assertEqual(normalized["activity_class"], "productive_model_work")
        self.assertEqual(normalized["work_id"], record["work_id"].lower())

    def test_uuid_casing_normalized_to_lowercase(self):
        record = _activity_record(work_id=_uuid().upper())
        normalized = activity.validate_activity_record(record)
        self.assertEqual(normalized["work_id"], record["work_id"].lower())

    def test_rejects_work_id_with_trailing_newline(self):
        record = _activity_record(work_id=_uuid() + "\n")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_does_not_mutate_input(self):
        record = _activity_record()
        original = dict(record)
        activity.validate_activity_record(record)
        self.assertEqual(record, original)

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            activity.validate_activity_record(["not", "a", "dict"])

    def test_rejects_missing_key(self):
        record = _activity_record()
        del record["age_seconds"]
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_unknown_key(self):
        record = _activity_record(extra_field="not part of the schema")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_wrong_record_kind(self):
        record = _activity_record(record="SomethingElse")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_bad_schema_version(self):
        record = _activity_record(schema_version=2)
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_bool_schema_version(self):
        record = _activity_record(schema_version=True)
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_activity_class_outside_closed_enum(self):
        record = _activity_record(activity_class="totally_made_up_class")
        with self.assertRaisesRegex(ValueError, "activity_class must be one of"):
            activity.validate_activity_record(record)

    def test_rejects_source_outside_closed_enum(self):
        record = _activity_record(source="gpt5")
        with self.assertRaisesRegex(ValueError, "source must be one of"):
            activity.validate_activity_record(record)

    def test_rejects_bad_time(self):
        record = _activity_record(time="not-a-timestamp")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_time_with_trailing_newline(self):
        # `\Z`, not `$`: `$` also matches immediately before a trailing
        # "\n", which would otherwise let a newline-suffixed timestamp slip
        # through as valid.
        record = _activity_record(time=_TIME_1 + "\n")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_accepts_shape_valid_but_calendar_invalid_timestamp(self):
        # Documents the deliberate policy: `_check_rfc3339` validates SHAPE
        # only (matching its own docstring/error text, "RFC3339-shaped",
        # never "valid RFC3339") -- this module performs no calendar-range
        # validation anywhere, so month 99 / day 99 / hour 99 is accepted.
        record = _activity_record(time="2026-99-99T99:99:99Z")
        activity.validate_activity_record(record)

    def test_rejects_negative_age(self):
        record = _activity_record(age_seconds=-1)
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_bool_age(self):
        record = _activity_record(age_seconds=True)
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_nan_age(self):
        record = _activity_record(age_seconds=float("nan"))
        with self.assertRaisesRegex(ValueError, "must be finite"):
            activity.validate_activity_record(record)

    def test_rejects_positive_infinite_age(self):
        record = _activity_record(age_seconds=float("inf"))
        with self.assertRaisesRegex(ValueError, "must be finite"):
            activity.validate_activity_record(record)

    def test_rejects_negative_infinite_age(self):
        # Caught by the plain range check (-inf < 0), not the finiteness
        # check -- covered here so both rejection paths for -inf are pinned.
        record = _activity_record(age_seconds=float("-inf"))
        with self.assertRaisesRegex(ValueError, "must be a nonnegative number"):
            activity.validate_activity_record(record)

    def test_accepts_zero_age(self):
        record = _activity_record(age_seconds=0)
        activity.validate_activity_record(record)

    def test_accepts_float_age(self):
        record = _activity_record(age_seconds=1.5)
        activity.validate_activity_record(record)

    def test_rejects_bad_provider_health(self):
        record = _activity_record(provider_health="fine_i_guess")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_accepts_null_provider_health(self):
        record = _activity_record(provider_health=None)
        activity.validate_activity_record(record)

    def test_artifact_fingerprint_and_delta_round_trip(self):
        record = _activity_record(
            artifact_fingerprint={"scripts/foo.py": _HASH_A, "scripts/bar.py": _HASH_B},
            artifact_delta=["scripts/foo.py"],
        )
        normalized = activity.validate_activity_record(record)
        self.assertEqual(normalized["artifact_delta"], ("scripts/foo.py",))
        self.assertEqual(
            normalized["artifact_fingerprint"],
            {"scripts/foo.py": _HASH_A, "scripts/bar.py": _HASH_B})

    def test_artifact_fingerprint_copy_is_independent_of_input(self):
        fingerprint = {"scripts/foo.py": _HASH_A}
        record = _activity_record(artifact_fingerprint=fingerprint, artifact_delta=[])
        normalized = activity.validate_activity_record(record)
        normalized["artifact_fingerprint"]["scripts/foo.py"] = _HASH_B
        self.assertEqual(fingerprint["scripts/foo.py"], _HASH_A)

    def test_rejects_empty_dict_fingerprint(self):
        record = _activity_record(artifact_fingerprint={}, artifact_delta=[])
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_fingerprint_entry_with_non_hex64_digest(self):
        # Exercises _check_artifact_fingerprint's own digest check (the
        # delta is empty here, so _check_artifact_delta never inspects any
        # entry) -- renamed from a prior name that implied this was a
        # delta-path assertion.
        record = _activity_record(
            artifact_fingerprint={"scripts/foo.py": "not-hex"}, artifact_delta=[])
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_duplicate_delta_entries(self):
        record = _activity_record(
            artifact_fingerprint={"scripts/foo.py": _HASH_A},
            artifact_delta=["scripts/foo.py", "scripts/foo.py"])
        with self.assertRaisesRegex(ValueError, "duplicated"):
            activity.validate_activity_record(record)

    def test_rejects_delta_with_null_fingerprint(self):
        record = _activity_record(artifact_fingerprint=None, artifact_delta=["scripts/foo.py"])
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_rejects_delta_entry_absent_from_fingerprint(self):
        record = _activity_record(
            artifact_fingerprint={"scripts/foo.py": _HASH_A},
            artifact_delta=["scripts/other.py"])
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)

    def test_accepts_tuple_delta(self):
        record = _activity_record(
            artifact_fingerprint={"scripts/foo.py": _HASH_A},
            artifact_delta=("scripts/foo.py",))
        activity.validate_activity_record(record)

    def test_rejects_non_list_delta(self):
        record = _activity_record(
            artifact_fingerprint={"scripts/foo.py": _HASH_A}, artifact_delta="scripts/foo.py")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(record)


class ClosedEnumTest(unittest.TestCase):
    """ActivityClass is closed to exactly the eight named states."""

    def test_activity_class_is_exactly_eight_named_values(self):
        self.assertEqual(
            activity.ACTIVITY_CLASS_SET,
            frozenset({
                "productive_model_work", "local_tool_work", "owned_verification",
                "provider_wait", "policy_denial", "process_crash",
                "hung_descendant", "no_evidence_silence",
            }))
        self.assertEqual(len(activity.ACTIVITY_CLASSES), 8)
        self.assertEqual(len(set(activity.ACTIVITY_CLASSES)), 8, "no duplicate members")

    def test_activity_source_is_exactly_four_named_values(self):
        self.assertEqual(
            activity.ACTIVITY_SOURCE_SET,
            frozenset({"claude", "codex", "opencode", "controller_native_tool"}))

    def test_watchdog_verdict_is_exactly_three_named_values(self):
        self.assertEqual(
            activity.WATCHDOG_VERDICT_SET,
            frozenset({"no_action", "soft_warning", "hard_stall_eligible"}))

    def test_controller_turn_outcome_extension_is_exactly_two_named_values(self):
        self.assertEqual(
            activity.CONTROLLER_TURN_OUTCOME_EXTENSION_SET,
            frozenset({"no_first_token", "refused"}))

    def test_failure_class_is_exactly_six_named_values(self):
        self.assertEqual(
            activity.FAILURE_CLASS_SET,
            frozenset({"quota", "balance", "auth", "overload", "transport",
                       "unknown_provider_failure"}))


class ReconciliationRecordTest(unittest.TestCase):

    def test_valid_record_round_trips(self):
        record = _reconciliation_record()
        normalized = activity.validate_activity_reconciliation_record(record)
        self.assertEqual(normalized["original_classification"], "no_evidence_silence")
        self.assertEqual(normalized["reconciled_classification"], "local_tool_work")

    def test_append_only_law_rejects_write_omitting_original_classification(self):
        record = _reconciliation_record()
        del record["original_classification"]
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)

    def test_append_only_law_rejects_write_omitting_reconciled_classification(self):
        record = _reconciliation_record()
        del record["reconciled_classification"]
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)

    def test_both_classification_fields_may_be_read_back_distinctly(self):
        # Never collapsed to one: both fields are independently readable
        # after normalization even when a caller (incorrectly) tries to
        # reason about only one of them.
        record = _reconciliation_record(
            original_classification="provider_wait",
            reconciled_classification="process_crash")
        normalized = activity.validate_activity_reconciliation_record(record)
        self.assertNotEqual(
            normalized["original_classification"], normalized["reconciled_classification"])
        self.assertEqual(normalized["original_classification"], "provider_wait")
        self.assertEqual(normalized["reconciled_classification"], "process_crash")

    def test_rejects_original_classification_outside_closed_enum(self):
        record = _reconciliation_record(original_classification="not_a_real_class")
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)

    def test_rejects_reconciled_classification_outside_closed_enum(self):
        record = _reconciliation_record(reconciled_classification="not_a_real_class")
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)

    def test_rejects_unknown_key(self):
        record = _reconciliation_record(unexpected="field")
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)

    def test_rejects_bad_revision_digest(self):
        record = _reconciliation_record(revision_digest="not-hex64")
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)

    def test_rejects_revision_digest_with_trailing_newline(self):
        record = _reconciliation_record(revision_digest=_HASH_A + "\n")
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)

    def test_rejects_empty_quiescence_marker(self):
        record = _reconciliation_record(quiescence_marker="")
        with self.assertRaises(ValueError):
            activity.validate_activity_reconciliation_record(record)


class ScheduledReviewRecordTest(unittest.TestCase):

    def test_valid_record_round_trips(self):
        record = _scheduled_review_record()
        normalized = activity.validate_scheduled_review_record(record)
        self.assertEqual(normalized["interval_seconds"], 300)

    def test_rejects_non_positive_interval(self):
        record = _scheduled_review_record(interval_seconds=0)
        with self.assertRaises(ValueError):
            activity.validate_scheduled_review_record(record)

    def test_rejects_negative_interval(self):
        record = _scheduled_review_record(interval_seconds=-60)
        with self.assertRaises(ValueError):
            activity.validate_scheduled_review_record(record)

    def test_accepts_null_last_inspection_result_ref(self):
        record = _scheduled_review_record(last_inspection_result_ref=None)
        activity.validate_scheduled_review_record(record)

    def test_accepts_nonempty_last_inspection_result_ref(self):
        record = _scheduled_review_record(last_inspection_result_ref="ref-123")
        activity.validate_scheduled_review_record(record)

    def test_rejects_empty_last_inspection_result_ref(self):
        record = _scheduled_review_record(last_inspection_result_ref="")
        with self.assertRaises(ValueError):
            activity.validate_scheduled_review_record(record)

    def test_rejects_bad_next_inspection_at(self):
        record = _scheduled_review_record(next_inspection_at="soon")
        with self.assertRaises(ValueError):
            activity.validate_scheduled_review_record(record)

    def test_rejects_unknown_key(self):
        record = _scheduled_review_record(bogus="x")
        with self.assertRaises(ValueError):
            activity.validate_scheduled_review_record(record)


class WatchdogDecisionDualEvidenceTest(unittest.TestCase):
    """Gate 5: the four-way dual-evidence matrix, as four distinct
    assertions (never folded into one OR-check)."""

    def test_durable_only_is_rejected(self):
        record = _watchdog_decision(
            verdict="soft_warning", durable_evidence_ref="journal-ref-1",
            process_probe_ref=None)
        with self.assertRaisesRegex(
                ValueError, r"requires both durable_evidence_ref and process_probe_ref"):
            activity.validate_watchdog_decision(record)

    def test_probe_only_is_rejected(self):
        record = _watchdog_decision(
            verdict="soft_warning", durable_evidence_ref=None,
            process_probe_ref="pid-4242")
        with self.assertRaisesRegex(
                ValueError, r"requires both durable_evidence_ref and process_probe_ref"):
            activity.validate_watchdog_decision(record)

    def test_neither_is_rejected(self):
        record = _watchdog_decision(
            verdict="soft_warning", durable_evidence_ref=None, process_probe_ref=None)
        with self.assertRaisesRegex(
                ValueError, r"requires both durable_evidence_ref and process_probe_ref"):
            activity.validate_watchdog_decision(record)

    def test_both_is_accepted(self):
        record = _watchdog_decision(
            verdict="soft_warning", durable_evidence_ref="journal-ref-1",
            process_probe_ref="pid-4242")
        normalized = activity.validate_watchdog_decision(record)
        self.assertEqual(normalized["verdict"], "soft_warning")

    def test_hard_stall_eligible_also_requires_both(self):
        record = _watchdog_decision(
            verdict="hard_stall_eligible", durable_evidence_ref="journal-ref-1",
            process_probe_ref=None)
        with self.assertRaisesRegex(
                ValueError, r"requires both durable_evidence_ref and process_probe_ref"):
            activity.validate_watchdog_decision(record)

    def test_hard_stall_eligible_accepted_with_both(self):
        record = _watchdog_decision(
            verdict="hard_stall_eligible", durable_evidence_ref="journal-ref-1",
            process_probe_ref="pid-4242")
        activity.validate_watchdog_decision(record)

    def test_no_action_requires_neither(self):
        record = _watchdog_decision(
            verdict="no_action", durable_evidence_ref=None, process_probe_ref=None)
        activity.validate_watchdog_decision(record)

    def test_no_action_permits_both_present_too(self):
        record = _watchdog_decision(
            verdict="no_action", durable_evidence_ref="journal-ref-1",
            process_probe_ref="pid-4242")
        activity.validate_watchdog_decision(record)


class WatchdogDecisionShapeTest(unittest.TestCase):

    def test_rejects_verdict_outside_closed_enum(self):
        record = _watchdog_decision(verdict="mostly_fine")
        with self.assertRaisesRegex(ValueError, "verdict must be one of"):
            activity.validate_watchdog_decision(record)

    def test_rejects_empty_string_evidence_ref(self):
        record = _watchdog_decision(
            verdict="soft_warning", durable_evidence_ref="", process_probe_ref="pid-1")
        with self.assertRaises(ValueError):
            activity.validate_watchdog_decision(record)

    def test_rejects_unknown_key(self):
        record = _watchdog_decision(extra="nope")
        with self.assertRaises(ValueError):
            activity.validate_watchdog_decision(record)

    def test_does_not_mutate_input(self):
        record = _watchdog_decision(
            verdict="soft_warning", durable_evidence_ref="r1", process_probe_ref="p1")
        original = dict(record)
        activity.validate_watchdog_decision(record)
        self.assertEqual(record, original)


class ControllerTurnOutcomeTest(unittest.TestCase):

    def test_no_first_token_with_null_failure_class_accepted(self):
        record = _controller_turn_outcome(outcome="no_first_token", failure_class=None)
        normalized = activity.validate_controller_turn_outcome(record)
        self.assertEqual(normalized["outcome"], "no_first_token")

    def test_no_first_token_with_failure_class_rejected(self):
        record = _controller_turn_outcome(outcome="no_first_token", failure_class="quota")
        with self.assertRaises(ValueError):
            activity.validate_controller_turn_outcome(record)

    def test_refused_requires_failure_class(self):
        record = _controller_turn_outcome(outcome="refused", failure_class=None)
        with self.assertRaises(ValueError):
            activity.validate_controller_turn_outcome(record)

    def test_refused_with_each_failure_class_accepted(self):
        for failure_class in activity.FAILURE_CLASSES:
            with self.subTest(failure_class=failure_class):
                record = _controller_turn_outcome(outcome="refused", failure_class=failure_class)
                normalized = activity.validate_controller_turn_outcome(record)
                self.assertEqual(normalized["failure_class"], failure_class)

    def test_refused_with_unknown_failure_class_rejected(self):
        record = _controller_turn_outcome(outcome="refused", failure_class="made_up_reason")
        with self.assertRaisesRegex(ValueError, "failure_class must be one of"):
            activity.validate_controller_turn_outcome(record)

    def test_outcome_outside_closed_enum_rejected(self):
        record = _controller_turn_outcome(outcome="rate_limited", failure_class=None)
        with self.assertRaisesRegex(ValueError, "outcome must be one of"):
            activity.validate_controller_turn_outcome(record)

    def test_rejects_unknown_key(self):
        record = _controller_turn_outcome(extra="nope")
        with self.assertRaises(ValueError):
            activity.validate_controller_turn_outcome(record)

    def test_rejects_wrong_record_kind(self):
        record = _controller_turn_outcome(record="SomethingElse")
        with self.assertRaises(ValueError):
            activity.validate_controller_turn_outcome(record)


class ProjectCompactStateTest(unittest.TestCase):

    def _make(self, work_id, activity_class="productive_model_work", verdict="no_action",
              durable_evidence_ref=None, process_probe_ref=None, reconciled=False):
        activity_record = _activity_record(work_id=work_id, activity_class=activity_class)
        health_record = activity.validate_watchdog_decision(_watchdog_decision(
            work_id=work_id, verdict=verdict,
            durable_evidence_ref=durable_evidence_ref, process_probe_ref=process_probe_ref))
        schedule_record = activity.validate_scheduled_review_record(
            _scheduled_review_record(work_id=work_id))
        reconciliation_record = None
        if reconciled:
            reconciliation_record = activity.validate_activity_reconciliation_record(
                _reconciliation_record(
                    work_id=work_id, original_classification=activity_class,
                    reconciled_classification="local_tool_work"))
        return (activity.validate_activity_record(activity_record), health_record,
                schedule_record, reconciliation_record)

    def test_projects_facts_from_activity_record_when_no_reconciliation(self):
        work_id = _uuid()
        activity_record, health_record, schedule_record, _ = self._make(work_id)
        state = activity.project_compact_state(activity_record, health_record, schedule_record)
        self.assertEqual(state["activity_class"], "productive_model_work")
        self.assertIsNone(state["original_classification"])
        self.assertFalse(state["reconciled"])
        self.assertEqual(state["work_id"], work_id)
        self.assertEqual(state["watchdog_verdict"], "no_action")
        self.assertIsNone(state["durable_evidence_ref"])
        self.assertIsNone(state["process_probe_ref"])
        self.assertEqual(state["interval_seconds"], 300)
        self.assertEqual(state["artifact_delta"], ())

    def test_carries_both_evidence_refs_into_compact_state_for_terminal_verdict(self):
        # Closes M4A-R-MAJ-01's evidence-retention requirement: a
        # soft_warning/hard_stall_eligible verdict's compact state must
        # carry BOTH evidence references, so a renderer can cite the
        # evidence behind a stall verdict without reaching outside the
        # projection.
        work_id = _uuid()
        activity_record, health_record, schedule_record, _ = self._make(
            work_id, verdict="hard_stall_eligible",
            durable_evidence_ref="journal-ref-9", process_probe_ref="pid-9999")
        state = activity.project_compact_state(activity_record, health_record, schedule_record)
        self.assertEqual(state["watchdog_verdict"], "hard_stall_eligible")
        self.assertEqual(state["durable_evidence_ref"], "journal-ref-9")
        self.assertEqual(state["process_probe_ref"], "pid-9999")

    def test_projects_reconciled_classification_when_reconciliation_given(self):
        work_id = _uuid()
        activity_record, health_record, schedule_record, reconciliation_record = self._make(
            work_id, activity_class="no_evidence_silence", reconciled=True)
        state = activity.project_compact_state(
            activity_record, health_record, schedule_record, reconciliation_record)
        self.assertEqual(state["activity_class"], "local_tool_work")
        self.assertEqual(state["original_classification"], "no_evidence_silence")
        self.assertTrue(state["reconciled"])

    def test_false_productive_attribution_guard(self):
        """A projection backed only by a provider_wait record must never
        report productive_model_work."""
        work_id = _uuid()
        activity_record, health_record, schedule_record, _ = self._make(
            work_id, activity_class="provider_wait")
        state = activity.project_compact_state(activity_record, health_record, schedule_record)
        self.assertEqual(state["activity_class"], "provider_wait")
        self.assertNotEqual(state["activity_class"], "productive_model_work")

    def test_rejects_refused_decision_projecting_anyway(self):
        """Closes M4A-R-MAJ-01: a raw dict carrying the exact
        WatchdogDecision key set with a terminal verdict and BOTH evidence
        references missing -- exactly the shape validate_watchdog_decision()
        itself refuses -- must be refused by project_compact_state() too,
        not merely accepted because it superficially has the right keys.
        This is a non-vacuous reproduction of the review's exact finding:
        the raw (unvalidated) dict is passed directly as health_record,
        bypassing any prior call to validate_watchdog_decision."""
        work_id = _uuid()
        activity_record, _, schedule_record, _ = self._make(work_id)
        raw_refused_decision = _watchdog_decision(
            work_id=work_id, verdict="hard_stall_eligible",
            durable_evidence_ref=None, process_probe_ref=None)
        with self.assertRaisesRegex(
                ValueError, r"requires both durable_evidence_ref and process_probe_ref"):
            activity.project_compact_state(activity_record, raw_refused_decision, schedule_record)

    def test_rejects_refused_decision_one_sided_durable_only_projecting_anyway(self):
        work_id = _uuid()
        activity_record, _, schedule_record, _ = self._make(work_id)
        raw_one_sided_decision = _watchdog_decision(
            work_id=work_id, verdict="soft_warning",
            durable_evidence_ref="journal-ref-1", process_probe_ref=None)
        with self.assertRaisesRegex(
                ValueError, r"requires both durable_evidence_ref and process_probe_ref"):
            activity.project_compact_state(
                activity_record, raw_one_sided_decision, schedule_record)

    def test_rejects_no_evidence_silence_with_terminal_verdict_lacking_process_health_evidence(self):
        """The fixed plan's negative control 1 (M4A-R-MIN-09), now
        genuinely reachable and tested through project_compact_state: an
        ActivityRecord classed no_evidence_silence combined with a raw,
        unvalidated terminal WatchdogDecision missing its process-health
        probe reference must never project cleanly (prevents
        hard-timeout-from-silence)."""
        work_id = _uuid()
        activity_record, _, schedule_record, _ = self._make(
            work_id, activity_class="no_evidence_silence")
        raw_decision_missing_probe = _watchdog_decision(
            work_id=work_id, verdict="hard_stall_eligible",
            durable_evidence_ref="journal-ref-1", process_probe_ref=None)
        with self.assertRaisesRegex(
                ValueError, r"requires both durable_evidence_ref and process_probe_ref"):
            activity.project_compact_state(
                activity_record, raw_decision_missing_probe, schedule_record)

    def test_rejects_out_of_enum_activity_class_via_raw_dict(self):
        """A raw activity_record dict with an out-of-enum class must be
        rejected by full revalidation, not passed through verbatim."""
        work_id = _uuid()
        _, health_record, schedule_record, _ = self._make(work_id)
        raw_activity_record = _activity_record(
            work_id=work_id, activity_class="totally_made_up")
        with self.assertRaisesRegex(ValueError, "activity_class must be one of"):
            activity.project_compact_state(raw_activity_record, health_record, schedule_record)

    def test_rejects_wrong_typed_age_seconds_via_raw_dict(self):
        work_id = _uuid()
        _, health_record, schedule_record, _ = self._make(work_id)
        raw_activity_record = _activity_record(work_id=work_id, age_seconds="banana")
        with self.assertRaises(ValueError):
            activity.project_compact_state(raw_activity_record, health_record, schedule_record)

    def test_rejects_mismatched_health_record_work_id(self):
        activity_record, health_record, schedule_record, _ = self._make(_uuid())
        other_health_record = activity.validate_watchdog_decision(
            _watchdog_decision(work_id=_uuid()))
        with self.assertRaises(ValueError):
            activity.project_compact_state(activity_record, other_health_record, schedule_record)

    def test_rejects_mismatched_schedule_record_work_id(self):
        activity_record, health_record, schedule_record, _ = self._make(_uuid())
        other_schedule_record = activity.validate_scheduled_review_record(
            _scheduled_review_record(work_id=_uuid()))
        with self.assertRaises(ValueError):
            activity.project_compact_state(activity_record, health_record, other_schedule_record)

    def test_rejects_non_dict_activity_record(self):
        _, health_record, schedule_record, _ = self._make(_uuid())
        with self.assertRaises(ValueError):
            activity.project_compact_state("not-a-dict", health_record, schedule_record)

    def test_rejects_unnormalized_activity_record_shape(self):
        _, health_record, schedule_record, _ = self._make(_uuid())
        with self.assertRaises(ValueError):
            activity.project_compact_state({"missing": "keys"}, health_record, schedule_record)

    def test_is_pure_same_inputs_same_output(self):
        activity_record, health_record, schedule_record, _ = self._make(_uuid())
        first = activity.project_compact_state(activity_record, health_record, schedule_record)
        second = activity.project_compact_state(activity_record, health_record, schedule_record)
        self.assertEqual(first, second)


class SignatureShapeTest(unittest.TestCase):
    """Gate 6: signature-shape controls for the pinned cross-package
    signatures, including live_child_handle's Package-A-pinned-signature/
    Package-C-owned-body split (M4R-F03)."""

    _EXPECTED_NAMES = frozenset({
        "project_compact_state", "render_compact_activity",
        "render_headless_activity", "_section_activity", "live_child_handle",
    })

    def test_pinned_signatures_names_exact(self):
        self.assertEqual(set(activity.PINNED_SIGNATURES), self._EXPECTED_NAMES)

    def test_each_entry_has_required_fields(self):
        for name, spec in activity.PINNED_SIGNATURES.items():
            with self.subTest(name=name):
                for field in ("owner", "module", "params", "returns", "body_owner"):
                    self.assertIn(field, spec)

    def test_project_compact_state_owned_and_bodied_by_package_a(self):
        spec = activity.PINNED_SIGNATURES["project_compact_state"]
        self.assertEqual(spec["owner"], "A-activity-contracts")
        self.assertEqual(spec["body_owner"], "A-activity-contracts")
        self.assertEqual(spec["module"], "cowork_activity")
        self.assertEqual(
            spec["params"],
            ("activity_record", "health_record", "schedule_record",
             "reconciliation_record=None"))

    def test_live_child_handle_signature_pinned_by_a_body_owned_by_c(self):
        spec = activity.PINNED_SIGNATURES["live_child_handle"]
        self.assertEqual(spec["owner"], "C-controller-adapters")
        self.assertEqual(spec["body_owner"], "C-controller-adapters")
        self.assertEqual(spec["module"], "cowork_bridge")
        self.assertEqual(spec["params"], ("session",))
        self.assertEqual(spec["returns"], "subprocess.Popen | None")
        self.assertNotEqual(spec["owner"], "A-activity-contracts",
                             "Package A pins the signature only, never the body")

    def test_render_functions_owned_by_package_e_in_cowork_ui(self):
        for name in ("render_compact_activity", "render_headless_activity"):
            spec = activity.PINNED_SIGNATURES[name]
            self.assertEqual(spec["owner"], "E-cross-surface-rendering")
            self.assertEqual(spec["module"], "cowork_ui")

    def test_render_compact_activity_params(self):
        spec = activity.PINNED_SIGNATURES["render_compact_activity"]
        self.assertEqual(spec["params"], ("io_out", "compact_state", "enabled=None"))

    def test_render_headless_activity_params(self):
        spec = activity.PINNED_SIGNATURES["render_headless_activity"]
        self.assertEqual(spec["params"], ("io_out", "compact_state"))

    def test_section_activity_owned_by_package_d_in_cowork_report(self):
        spec = activity.PINNED_SIGNATURES["_section_activity"]
        self.assertEqual(spec["owner"], "D-watchdog-active-review")
        self.assertEqual(spec["module"], "cowork_report")
        self.assertEqual(spec["params"], ("record",))
        self.assertEqual(spec["returns"], "list[str]")

    def test_pinned_signatures_is_immutable(self):
        with self.assertRaises(TypeError):
            activity.PINNED_SIGNATURES["project_compact_state"] = {}


class ImportAndIOBoundaryTest(unittest.TestCase):
    """cowork_activity.py imports nothing beyond the standard library and
    performs no file/network/subprocess I/O (gate 4: purity/static import
    boundary)."""

    def _module_path(self):
        return os.path.join(_HERE, "cowork_activity.py")

    def _source(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            return fh.read()

    def _all_imports(self):
        # Named for what it actually does (ast.walk, not just the top-level
        # body): it collects nested and function-level imports too, which is
        # the stricter and therefore correct direction for a purity gate.
        tree = ast.parse(self._source(), filename=self._module_path())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_imports_are_stdlib_only(self):
        self.assertEqual(self._all_imports(), {"math", "re", "types"})

    def test_imports_no_runtime_module(self):
        imported = self._all_imports()
        forbidden_hit = imported & _FORBIDDEN_RUNTIME_MODULES
        self.assertFalse(forbidden_hit,
                          "cowork_activity.py imports runtime module(s): %s"
                          % sorted(forbidden_hit))

    def test_module_has_no_os_import(self):
        self.assertNotIn("os", self._all_imports())

    def test_source_contains_no_forbidden_ast_calls_or_attributes(self):
        # AST-based, not substring-based: the module's own docstrings
        # legitimately DISCUSS "subprocess"/"socket"/"fork" in prose to
        # document that it never uses them, so a raw text grep would
        # false-positive on its own compliance explanation. Only real
        # Call/Attribute/Import nodes count as a purity violation.
        tree = ast.parse(self._source(), filename=self._module_path())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(
                    node.func.id, {"open", "eval", "exec", "compile", "__import__"},
                    "found forbidden call: %s" % node.func.id)
            if isinstance(node, ast.Attribute) and node.attr in (
                    "system", "popen", "fork", "Popen", "socket", "connect"):
                self.fail("found forbidden I/O attribute access: %s" % node.attr)

    def test_module_compiles_clean(self):
        compile(self._source(), self._module_path(), "exec")


if __name__ == "__main__":
    unittest.main()
