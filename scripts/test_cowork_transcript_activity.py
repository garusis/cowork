#!/usr/bin/env python3
"""Tests for the M4 activity snapshot in the run transcript
(`cowork_transcript.render_activity`).

The renderer under test consumes ONLY an already-produced Package A
`cowork_activity.project_compact_state()` dict, so every fixture here is
built via real `cowork_activity` validators/projection rather than a
hand-rolled shape that might drift from the actual contract.

Run standalone:

    python3 -m unittest scripts.test_cowork_transcript_activity -v
"""

import io
import inspect
import os
import sys
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_activity as activity  # noqa: E402
import cowork_transcript as transcript  # noqa: E402


_TIME = "2026-08-25T10:00:00Z"
_NEXT = "2026-08-25T10:05:00Z"


def _uuid():
    return str(uuid.uuid4())


def _compact_state(work_id=None, activity_class="productive_model_work",
                    source="claude", age_seconds=12, artifact_fingerprint=None,
                    artifact_delta=(), provider_health="healthy",
                    verdict="no_action", durable_evidence_ref=None,
                    process_probe_ref=None, interval_seconds=300,
                    next_inspection_at=_NEXT, reconciled=False,
                    original_classification="no_evidence_silence"):
    """Build a genuine compact-state dict by round-tripping real
    `cowork_activity` records through `project_compact_state` — never a
    hand-authored dict that could drift from Package A's actual contract."""
    work_id = work_id or _uuid()
    activity_record = activity.validate_activity_record(dict(
        schema_version=1, record="ActivityRecord",
        work_id=work_id, time=_TIME,
        activity_class=activity_class, source=source,
        artifact_fingerprint=artifact_fingerprint, artifact_delta=artifact_delta,
        provider_health=provider_health, age_seconds=age_seconds,
    ))
    health_record = activity.validate_watchdog_decision(dict(
        schema_version=1, record="WatchdogDecision",
        work_id=work_id, time=_TIME, verdict=verdict,
        durable_evidence_ref=durable_evidence_ref,
        process_probe_ref=process_probe_ref,
    ))
    schedule_record = activity.validate_scheduled_review_record(dict(
        schema_version=1, record="ScheduledReviewRecord",
        work_id=work_id, next_inspection_at=next_inspection_at,
        interval_seconds=interval_seconds, last_inspection_result_ref=None,
    ))
    reconciliation_record = None
    if reconciled:
        reconciliation_record = activity.validate_activity_reconciliation_record(dict(
            schema_version=1, record="ActivityReconciliationRecord",
            work_id=work_id, time=_TIME,
            original_classification=original_classification,
            reconciled_classification=activity_class,
            revision_digest="a" * 64, quiescence_marker="digest_compare_after_wait",
        ))
    return activity.project_compact_state(
        activity_record, health_record, schedule_record, reconciliation_record)



def _text(state):
    out = io.StringIO()
    transcript.render_activity(out, state)
    return out.getvalue()


class PinnedSignatureTest(unittest.TestCase):
    def test_render_activity_matches_pinned_signature(self):
        spec = activity.PINNED_SIGNATURES["render_activity"]
        self.assertEqual(spec["owner"], "E-cross-surface-rendering")
        self.assertEqual(spec["module"], "cowork_transcript")
        params = tuple(str(p) for p in inspect.signature(
            transcript.render_activity).parameters.values())
        self.assertEqual(params, spec["params"])


class ActivitySnapshotTest(unittest.TestCase):
    def test_plain_text_states_every_fact(self):
        state = _compact_state(
            activity_class="hung_descendant", verdict="hard_stall_eligible",
            durable_evidence_ref="journal-ref-1", process_probe_ref="pid-42",
            artifact_delta=("a.py",), artifact_fingerprint={"a.py": "b" * 64})
        text = _text(state)
        self.assertIn("hung descendant", text)
        self.assertIn("age: 12s", text)
        self.assertIn("provider health: healthy", text)
        self.assertIn("hard-stall eligible", text)
        self.assertIn("durable=journal-ref-1", text)
        self.assertIn("process=pid-42", text)
        self.assertIn(_NEXT, text)
        self.assertIn("every 300s", text)
        self.assertIn("a.py", text)

    def test_reconciled_state_shows_original_classification(self):
        text = _text(_compact_state(
            activity_class="local_tool_work", reconciled=True,
            original_classification="no_evidence_silence"))
        self.assertIn("local tool work", text)
        self.assertIn("reconciled from no evidence (silence)", text)

    def test_no_animation_or_escape_framing(self):
        text = _text(_compact_state())
        self.assertTrue(text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\033[", text)


class EquivalenceFixtureTest(unittest.TestCase):
    """For a battery of fixtures, each mutating exactly one canonical fact
    relative to a shared baseline, the rendered text must change on exactly
    that fact. Non-vacuous: a renderer that ignored `compact_state` entirely
    (returning constant text) would fail here."""

    def _facts(self, state):
        return _text(state)

    def test_baseline_is_stable_and_non_empty(self):
        text = self._facts(_compact_state())
        self.assertTrue(text.strip())

    def test_activity_class_mutation_is_rendered(self):
        base = self._facts(_compact_state(activity_class="productive_model_work"))
        mutated = self._facts(_compact_state(activity_class="hung_descendant"))
        self.assertNotEqual(base, mutated)  # non-vacuous
        self.assertIn("hung descendant", mutated)
        self.assertNotIn("hung descendant", base)

    def test_age_mutation_is_rendered(self):
        base = self._facts(_compact_state(age_seconds=5))
        mutated = self._facts(_compact_state(age_seconds=999))
        self.assertNotEqual(base, mutated)
        self.assertIn("age: 999s", mutated)
        self.assertIn("age: 5s", base)

    def test_next_inspection_mutation_is_rendered(self):
        base = self._facts(_compact_state(next_inspection_at=_NEXT))
        other_next = "2026-08-25T11:30:00Z"
        mutated = self._facts(_compact_state(next_inspection_at=other_next))
        self.assertNotEqual(base, mutated)
        self.assertIn(other_next, mutated)
        self.assertIn(_NEXT, base)

    def test_evidence_refs_mutation_is_rendered(self):
        base = self._facts(_compact_state(
            verdict="hard_stall_eligible",
            durable_evidence_ref="journal-ref-A", process_probe_ref="pid-1"))
        mutated = self._facts(_compact_state(
            verdict="hard_stall_eligible",
            durable_evidence_ref="journal-ref-B", process_probe_ref="pid-2"))
        self.assertNotEqual(base, mutated)
        self.assertIn("durable=journal-ref-B", mutated)
        self.assertIn("process=pid-2", mutated)
        self.assertIn("durable=journal-ref-A", base)

    def test_watchdog_verdict_mutation_is_rendered(self):
        base = self._facts(_compact_state(verdict="no_action"))
        mutated = self._facts(_compact_state(
            verdict="soft_warning",
            durable_evidence_ref="journal-ref-1", process_probe_ref="pid-1"))
        self.assertNotEqual(base, mutated)
        self.assertIn("soft warning", mutated)
        self.assertIn("no action", base)

    def test_provider_health_mutation_is_rendered(self):
        base = self._facts(_compact_state(provider_health="healthy"))
        mutated = self._facts(_compact_state(provider_health="degraded"))
        self.assertNotEqual(base, mutated)
        self.assertIn("degraded", mutated)
        self.assertIn("healthy", base)

    def test_reconciled_mutation_is_rendered(self):
        base = self._facts(_compact_state(reconciled=False))
        mutated = self._facts(_compact_state(
            reconciled=True, activity_class="local_tool_work",
            original_classification="no_evidence_silence"))
        self.assertNotEqual(base, mutated)
        self.assertIn("reconciled from", mutated)
        self.assertNotIn("reconciled from", base)

    def test_artifact_delta_mutation_is_rendered(self):
        base = self._facts(_compact_state(artifact_delta=(), artifact_fingerprint=None))
        mutated = self._facts(_compact_state(
            artifact_delta=("x.py", "y.py"),
            artifact_fingerprint={"x.py": "a" * 64, "y.py": "b" * 64}))
        self.assertNotEqual(base, mutated)
        self.assertIn("x.py, y.py", mutated)
        self.assertIn("artifact changes: none", base)

    def test_source_mutation_is_rendered(self):
        base = self._facts(_compact_state(source="claude"))
        mutated = self._facts(_compact_state(source="codex"))
        self.assertNotEqual(base, mutated)
        self.assertIn("source: codex", mutated)
        self.assertIn("source: claude", base)


class NegativeControlTest(unittest.TestCase):
    def test_invented_field_is_never_rendered(self):
        state = dict(_compact_state())
        state["fabricated_status"] = "ALL SYSTEMS GO — TOTALLY FINE"
        text = _text(state)
        self.assertNotIn("ALL SYSTEMS GO", text)
        self.assertNotIn("fabricated_status", text)

    def test_missing_field_renders_truthfully_not_fabricated(self):
        state = dict(_compact_state())
        del state["durable_evidence_ref"]
        text = _text(state)
        self.assertIn("durable=(not reported)", text)
        self.assertNotIn("durable=None", text)


if __name__ == "__main__":
    unittest.main()
