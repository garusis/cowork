#!/usr/bin/env python3
"""Tests for M4 Package D's report activity leg: `cowork_report.
_section_activity`, its LINEAGE entries, and `cowork_measure.build_record`'s
unconditional `record["activity"]` field."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_activity as activity  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_report as report  # noqa: E402
import cowork_state as state_store  # noqa: E402

WORK_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_ACTIVITY_FIGURES = tuple(
    figure for figure, _ in report.LINEAGE if figure.startswith("activity."))


def _populated_activity():
    return {
        "work_id": WORK_ID, "activity_class": "productive_model_work",
        "original_classification": None, "reconciled": False,
        "source": "claude", "age_seconds": 4.5, "artifact_delta": [],
        "provider_health": None, "watchdog_verdict": "no_action",
        "durable_evidence_ref": None, "process_probe_ref": None,
        "next_inspection_at": "2026-01-01T00:05:00Z", "interval_seconds": 300,
    }


class SectionActivityTest(unittest.TestCase):
    def test_absent_activity_key_renders_placeholder(self):
        lines = report._section_activity({})
        self.assertIn("Activity (durable, cross-surface)", lines[0])
        self.assertTrue(
            any("no durable activity" in line for line in lines))

    def test_unknown_shape_activity_still_renders_every_figure(self):
        record = {"activity": dict(measure._ACTIVITY_UNKNOWN_SHAPE)}
        text = "\n".join(report._section_activity(record))
        for key in ("work_id", "activity_class", "reconciled", "source",
                    "age_seconds", "artifact_delta", "provider_health",
                    "watchdog_verdict", "next_inspection_at",
                    "interval_seconds"):
            self.assertIn(measure.UNKNOWN, text)

    def test_populated_activity_renders_real_facts(self):
        record = {"activity": _populated_activity()}
        text = "\n".join(report._section_activity(record))
        self.assertIn("productive_model_work", text)
        self.assertIn("claude", text)
        self.assertIn("no_action", text)
        self.assertIn("2026-01-01T00:05:00Z", text)

    def test_reconciled_activity_shows_original_classification(self):
        rec = dict(_populated_activity())
        rec["reconciled"] = True
        rec["original_classification"] = "hung_descendant"
        record = {"activity": rec}
        text = "\n".join(report._section_activity(record))
        self.assertIn("hung_descendant", text)
        self.assertIn("yes", text)  # reconciled -> "yes"

    def test_section_activity_is_wired_into_render_report(self):
        record = {"activity": _populated_activity()}
        full = report.render_report(record)
        self.assertIn("Activity (durable, cross-surface)", full)
        self.assertIn("productive_model_work", full)

    def test_render_report_activity_section_is_last(self):
        record = {"activity": _populated_activity()}
        full = report.render_report(record)
        activity_pos = full.index("Activity (durable, cross-surface)")
        incomplete_pos = full.index("What this record does NOT know")
        self.assertGreater(activity_pos, incomplete_pos)


class LineageTest(unittest.TestCase):
    def test_every_activity_figure_is_in_lineage(self):
        expected = {
            "activity.work_id", "activity.class",
            "activity.original_classification", "activity.reconciled",
            "activity.source", "activity.age_seconds",
            "activity.artifact_delta", "activity.provider_health",
            "activity.watchdog_verdict", "activity.durable_evidence_ref",
            "activity.process_probe_ref", "activity.next_inspection_at",
            "activity.interval_seconds",
        }
        self.assertEqual(set(_ACTIVITY_FIGURES), expected)

    def test_every_activity_figure_resolves_when_populated(self):
        record = {"activity": _populated_activity()}
        lineage = report.rendered_lineage(record)
        for figure in _ACTIVITY_FIGURES:
            self.assertTrue(lineage[figure]["resolved"],
                            "%s did not resolve" % figure)

    def test_every_activity_figure_resolves_when_unknown_shape(self):
        record = {"activity": dict(measure._ACTIVITY_UNKNOWN_SHAPE)}
        lineage = report.rendered_lineage(record)
        for figure in _ACTIVITY_FIGURES:
            self.assertTrue(lineage[figure]["resolved"],
                            "%s did not resolve" % figure)
            self.assertEqual(lineage[figure]["value"], measure.UNKNOWN)

    def test_unresolved_lineage_negative_when_activity_key_absent(self):
        # A record predating this feature (no "activity" key at all): every
        # activity.* figure is genuinely unresolved, not silently defaulted.
        record = {}
        lineage = report.rendered_lineage(record)
        for figure in _ACTIVITY_FIGURES:
            self.assertFalse(lineage[figure]["resolved"],
                             "%s should be unresolved" % figure)


class BuildRecordActivityFieldTest(unittest.TestCase):
    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._tmp = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._tmp

    def tearDown(self):
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_activity_field_always_present_even_with_no_durable_activity(self):
        session_uuid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        record = measure.build_record(session_uuid)
        self.assertIn("activity", record)
        self.assertEqual(record["activity"], measure._ACTIVITY_UNKNOWN_SHAPE)

    def test_activity_field_populated_from_durable_evidence(self):
        session_uuid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        state_store.append_activity_record(session_uuid, {
            "schema_version": 1, "record": "ActivityRecord",
            "work_id": WORK_ID, "time": "2026-01-01T00:00:00Z",
            "activity_class": "local_tool_work", "source": "codex",
            "artifact_fingerprint": None, "artifact_delta": [],
            "provider_health": "healthy", "age_seconds": 12.0,
        })
        state_store.write_scheduled_review(session_uuid, {
            "schema_version": 1, "record": "ScheduledReviewRecord",
            "work_id": WORK_ID, "next_inspection_at": "2026-01-01T00:05:00Z",
            "interval_seconds": 300, "last_inspection_result_ref": None,
        })
        record = measure.build_record(session_uuid)
        self.assertEqual(record["activity"]["work_id"], WORK_ID)
        self.assertEqual(record["activity"]["activity_class"],
                         "local_tool_work")
        self.assertEqual(record["activity"]["source"], "codex")
        self.assertEqual(record["activity"]["watchdog_verdict"], "no_action")
        self.assertIsNone(record["activity"]["durable_evidence_ref"])
        self.assertEqual(record["activity"]["next_inspection_at"],
                         "2026-01-01T00:05:00Z")

    def test_activity_field_no_pre_existing_key_changed(self):
        # Additive-only proof: every OTHER top-level key build_record already
        # produced for an empty session is unaffected by the activity field.
        session_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        record = measure.build_record(session_uuid)
        self.assertIn("schema_version", record)
        self.assertIn("built_at", record)
        self.assertIn("work", record)
        self.assertIn("cost", record)

    def test_activity_field_reflects_latest_across_multiple_work_ids(self):
        session_uuid = "12345678-1234-1234-1234-123456789abc"
        older_work_id = "11111111-2222-3333-4444-555555555555"
        newer_work_id = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        state_store.append_activity_record(session_uuid, {
            "schema_version": 1, "record": "ActivityRecord",
            "work_id": older_work_id, "time": "2026-01-01T00:00:00Z",
            "activity_class": "process_crash", "source": "claude",
            "artifact_fingerprint": None, "artifact_delta": [],
            "provider_health": None, "age_seconds": 1.0,
        })
        state_store.append_activity_record(session_uuid, {
            "schema_version": 1, "record": "ActivityRecord",
            "work_id": newer_work_id, "time": "2026-01-01T01:00:00Z",
            "activity_class": "productive_model_work", "source": "opencode",
            "artifact_fingerprint": None, "artifact_delta": [],
            "provider_health": None, "age_seconds": 1.0,
        })
        record = measure.build_record(session_uuid)
        self.assertEqual(record["activity"]["work_id"], newer_work_id)
        self.assertEqual(record["activity"]["activity_class"],
                         "productive_model_work")

    def test_corrupt_activity_history_never_raises_build_record(self):
        session_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        path = state_store.activity_history_path_for(session_uuid, WORK_ID)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write('{"not": "valid ActivityRecord shape"}\n')
        record = measure.build_record(session_uuid)
        self.assertEqual(record["activity"], measure._ACTIVITY_UNKNOWN_SHAPE)


if __name__ == "__main__":
    unittest.main()
