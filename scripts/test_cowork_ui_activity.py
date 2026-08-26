#!/usr/bin/env python3
"""Tests for M4 Package E: cross-surface activity rendering
(`cowork_ui.render_compact_activity` / `cowork_ui.render_headless_activity`).

Both renderers under test consume ONLY an already-produced Package A
`cowork_activity.project_compact_state()` dict, so every fixture here is
built via real `cowork_activity` validators/projection rather than a
hand-rolled shape that might drift from the actual contract.

Run standalone:

    python3 -m unittest scripts.test_cowork_ui_activity -v
"""

import io
import inspect
import os
import re
import sys
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_activity as activity  # noqa: E402
import cowork_ui as ui  # noqa: E402

try:
    import rich  # noqa: F401
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal, so is_tty() returns True —
    same fixture `test_cowork.py` uses for every other TTY-branch test."""

    def isatty(self):
        return True


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


# --------------------------------------------------------------------------- #
# Gate 4/5-adjacent: exact pinned signatures.                                  #
# --------------------------------------------------------------------------- #

class PinnedSignatureTest(unittest.TestCase):
    def test_render_compact_activity_matches_pinned_signature(self):
        spec = activity.PINNED_SIGNATURES["render_compact_activity"]
        self.assertEqual(spec["owner"], "E-cross-surface-rendering")
        self.assertEqual(spec["module"], "cowork_ui")
        params = tuple(str(p) for p in
                        inspect.signature(ui.render_compact_activity).parameters.values())
        self.assertEqual(params, spec["params"])

    def test_render_headless_activity_matches_pinned_signature(self):
        spec = activity.PINNED_SIGNATURES["render_headless_activity"]
        self.assertEqual(spec["owner"], "E-cross-surface-rendering")
        self.assertEqual(spec["module"], "cowork_ui")
        params = tuple(str(p) for p in
                        inspect.signature(ui.render_headless_activity).parameters.values())
        self.assertEqual(params, spec["params"])


# --------------------------------------------------------------------------- #
# render_headless_activity: never TTY-dependent.                              #
# --------------------------------------------------------------------------- #

class HeadlessActivityTest(unittest.TestCase):
    def test_plain_text_states_every_fact(self):
        state = _compact_state(
            activity_class="hung_descendant", verdict="hard_stall_eligible",
            durable_evidence_ref="journal-ref-1", process_probe_ref="pid-42",
            artifact_delta=("a.py",), artifact_fingerprint={"a.py": "b" * 64})
        out = io.StringIO()
        ui.render_headless_activity(out, state)
        text = out.getvalue()
        self.assertIn("hung descendant", text)
        self.assertIn("age: 12s", text)
        self.assertIn("provider health: healthy", text)
        self.assertIn("hard-stall eligible", text)
        self.assertIn("durable=journal-ref-1", text)
        self.assertIn("process=pid-42", text)
        self.assertIn(_NEXT, text)
        self.assertIn("every 300s", text)
        self.assertIn("a.py", text)

    def test_identical_output_pty_like_and_stringio(self):
        """The headless surface's output does not depend on isatty() at
        all: a PTY-like FakeTTY and a plain StringIO given the identical
        compact_state must produce byte-identical output."""
        state = _compact_state()
        pty_out = FakeTTY()
        plain_out = io.StringIO()
        ui.render_headless_activity(pty_out, state)
        ui.render_headless_activity(plain_out, state)
        self.assertEqual(pty_out.getvalue(), plain_out.getvalue())
        self.assertTrue(pty_out.getvalue())  # non-vacuous: actually wrote something

    def test_never_consults_is_tty(self):
        """Patch is_tty to explode; render_headless_activity must not call
        it at all, proving its output cannot depend on TTY inference."""
        state = _compact_state()
        original = ui.is_tty
        def _boom(_stream):
            raise AssertionError("render_headless_activity must not call is_tty()")
        ui.is_tty = _boom
        try:
            out = io.StringIO()
            ui.render_headless_activity(out, state)
            self.assertTrue(out.getvalue())
        finally:
            ui.is_tty = original

    def test_reconciled_state_shows_original_classification(self):
        state = _compact_state(
            activity_class="local_tool_work", reconciled=True,
            original_classification="no_evidence_silence")
        out = io.StringIO()
        ui.render_headless_activity(out, state)
        text = out.getvalue()
        self.assertIn("local tool work", text)
        self.assertIn("reconciled from no evidence (silence)", text)

    def test_no_claim_of_continuous_update_markers(self):
        """The headless surface never emits spinner/carriage-return framing
        (that is Package C's per-turn spinner's exclusive territory)."""
        state = _compact_state()
        out = io.StringIO()
        ui.render_headless_activity(out, state)
        self.assertNotIn("\r", out.getvalue())


# --------------------------------------------------------------------------- #
# render_compact_activity: TTY-aware, Rich/plain, explicitly overridable.      #
# --------------------------------------------------------------------------- #

class CompactActivityTest(unittest.TestCase):
    def test_non_tty_writes_plain_text(self):
        state = _compact_state(activity_class="provider_wait")
        out = io.StringIO()
        ui.render_compact_activity(out, state)
        text = out.getvalue()
        self.assertIn("waiting on provider", text)
        self.assertNotIn("\033[", text)  # no ANSI/Rich escape codes

    @unittest.skipUnless(HAS_RICH, "rich not installed")
    def test_tty_renders_rich_panel(self):
        state = _compact_state(activity_class="productive_model_work")
        out = FakeTTY()
        ui.render_compact_activity(out, state)
        text = out.getvalue()
        self.assertIn("productive model work", text)
        self.assertTrue(any(ch in text for ch in "─│╭╰╮╯┌└"))

    @unittest.skipUnless(HAS_RICH, "rich not installed")
    def test_enabled_false_forces_plain_even_on_real_tty_stream(self):
        state = _compact_state()
        out = FakeTTY()
        ui.render_compact_activity(out, state, enabled=False)
        text = out.getvalue()
        self.assertNotIn("\033[", text)
        self.assertFalse(any(ch in text for ch in "─│╭╰╮╯┌└"))

    @unittest.skipUnless(HAS_RICH, "rich not installed")
    def test_enabled_true_forces_rich_even_off_tty_stream(self):
        state = _compact_state()
        out = io.StringIO()
        ui.render_compact_activity(out, state, enabled=True)
        text = out.getvalue()
        self.assertTrue(any(ch in text for ch in "─│╭╰╮╯┌└"))

    def test_hard_stall_eligible_carries_both_evidence_refs_in_plain_output(self):
        state = _compact_state(
            verdict="hard_stall_eligible",
            durable_evidence_ref="journal-ref-9", process_probe_ref="pid-9999")
        out = io.StringIO()
        ui.render_compact_activity(out, state, enabled=False)
        text = out.getvalue()
        self.assertIn("durable=journal-ref-9", text)
        self.assertIn("process=pid-9999", text)


# --------------------------------------------------------------------------- #
# Gate 6: non-vacuous equivalence — mutate each canonical fact independently   #
# and prove both surfaces agree.                                              #
# --------------------------------------------------------------------------- #

def _headless_text(state):
    out = io.StringIO()
    ui.render_headless_activity(out, state)
    return out.getvalue()


def _compact_plain_text(state):
    out = io.StringIO()
    ui.render_compact_activity(out, state, enabled=False)
    return out.getvalue()


class EquivalenceFixtureTest(unittest.TestCase):
    """For a battery of fixtures, each mutating exactly one canonical fact
    relative to a shared baseline, both renderers must produce the SAME
    textual fact for that dimension. Non-vacuous: every fixture pair below
    differs from the baseline (asserted first), so a renderer that ignored
    `compact_state` entirely (returning constant text) would fail here."""

    def _facts(self, state):
        headless = _headless_text(state)
        compact = _compact_plain_text(state)
        self.assertEqual(headless, compact,
                          "headless and compact-plain must be fact-identical")
        return headless

    def test_baseline_is_stable_and_non_empty(self):
        text = self._facts(_compact_state())
        self.assertTrue(text.strip())

    def test_activity_class_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(activity_class="productive_model_work"))
        mutated = self._facts(_compact_state(activity_class="hung_descendant"))
        self.assertNotEqual(base, mutated)  # non-vacuous
        self.assertIn("hung descendant", mutated)
        self.assertNotIn("hung descendant", base)

    def test_age_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(age_seconds=5))
        mutated = self._facts(_compact_state(age_seconds=999))
        self.assertNotEqual(base, mutated)
        self.assertIn("age: 999s", mutated)
        self.assertIn("age: 5s", base)

    def test_next_inspection_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(next_inspection_at=_NEXT))
        other_next = "2026-08-25T11:30:00Z"
        mutated = self._facts(_compact_state(next_inspection_at=other_next))
        self.assertNotEqual(base, mutated)
        self.assertIn(other_next, mutated)
        self.assertIn(_NEXT, base)

    def test_evidence_refs_mutation_agrees_across_surfaces(self):
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

    def test_watchdog_verdict_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(verdict="no_action"))
        mutated = self._facts(_compact_state(
            verdict="soft_warning",
            durable_evidence_ref="journal-ref-1", process_probe_ref="pid-1"))
        self.assertNotEqual(base, mutated)
        self.assertIn("soft warning", mutated)
        self.assertIn("no action", base)

    def test_provider_health_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(provider_health="healthy"))
        mutated = self._facts(_compact_state(provider_health="degraded"))
        self.assertNotEqual(base, mutated)
        self.assertIn("degraded", mutated)
        self.assertIn("healthy", base)

    def test_reconciled_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(reconciled=False))
        mutated = self._facts(_compact_state(
            reconciled=True, activity_class="local_tool_work",
            original_classification="no_evidence_silence"))
        self.assertNotEqual(base, mutated)
        self.assertIn("reconciled from", mutated)
        self.assertNotIn("reconciled from", base)

    def test_artifact_delta_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(artifact_delta=(), artifact_fingerprint=None))
        mutated = self._facts(_compact_state(
            artifact_delta=("x.py", "y.py"),
            artifact_fingerprint={"x.py": "a" * 64, "y.py": "b" * 64}))
        self.assertNotEqual(base, mutated)
        self.assertIn("x.py, y.py", mutated)
        self.assertIn("artifact changes: none", base)

    def test_source_mutation_agrees_across_surfaces(self):
        base = self._facts(_compact_state(source="claude"))
        mutated = self._facts(_compact_state(source="codex"))
        self.assertNotEqual(base, mutated)
        self.assertIn("source: codex", mutated)
        self.assertIn("source: claude", base)


# --------------------------------------------------------------------------- #
# Gate 7: negative controls.                                                   #
# --------------------------------------------------------------------------- #

class NegativeControlTest(unittest.TestCase):
    def test_invented_field_refused_by_both_surfaces(self):
        """An extra, unknown key on compact_state (a fabricated claim no
        Package A projection would ever emit) must never surface in either
        renderer's output."""
        state = dict(_compact_state())
        state["fabricated_status"] = "ALL SYSTEMS GO — TOTALLY FINE"
        headless_text = _headless_text(state)
        compact_text = _compact_plain_text(state)
        for text in (headless_text, compact_text):
            self.assertNotIn("ALL SYSTEMS GO", text)
            self.assertNotIn("fabricated_status", text)
        # ...and the two surfaces still agree with each other on everything
        # they DO print.
        self.assertEqual(headless_text, compact_text)

    def test_missing_optional_field_renders_truthfully_not_fabricated(self):
        """A compact_state dict missing an otherwise-always-present key
        (simulating a malformed/partial caller input) must render an
        honest 'not reported' marker, never a guessed value, and must not
        raise."""
        state = dict(_compact_state())
        del state["durable_evidence_ref"]
        headless_text = _headless_text(state)
        self.assertIn("durable=(not reported)", headless_text)
        self.assertNotIn("durable=None", headless_text)

    def test_same_fixture_class_age_next_inspection_do_not_diverge(self):
        """The exact fixture rendered on both surfaces must never disagree
        on activity class, age, or next-inspection — the facts this
        contract cares most about."""
        state = _compact_state(
            activity_class="owned_verification", age_seconds=77,
            next_inspection_at="2026-08-25T12:00:00Z")
        headless_text = _headless_text(state)
        compact_text = _compact_plain_text(state)

        def _fact(text, prefix):
            for line in text.splitlines():
                if line.startswith(prefix):
                    return line
            self.fail("missing line with prefix %r in %r" % (prefix, text))

        self.assertEqual(_fact(headless_text, "activity:"), _fact(compact_text, "activity:"))
        self.assertEqual(_fact(headless_text, "age:"), _fact(compact_text, "age:"))
        self.assertEqual(_fact(headless_text, "next inspection:"),
                          _fact(compact_text, "next inspection:"))

    def test_headless_output_identical_pty_like_and_non_tty(self):
        state = _compact_state(activity_class="policy_denial")
        pty_out = FakeTTY()
        non_tty_out = io.StringIO()
        ui.render_headless_activity(pty_out, state)
        ui.render_headless_activity(non_tty_out, state)
        self.assertEqual(pty_out.getvalue(), non_tty_out.getvalue())

    @unittest.skipUnless(HAS_RICH, "rich not installed")
    def test_interactive_enabled_disabled_and_tty_plain_branches(self):
        state = _compact_state(activity_class="process_crash")
        auto_tty = FakeTTY()
        ui.render_compact_activity(auto_tty, state)  # enabled=None -> auto TTY
        auto_plain = io.StringIO()
        ui.render_compact_activity(auto_plain, state)  # enabled=None -> auto plain

        forced_plain_on_tty = FakeTTY()
        ui.render_compact_activity(forced_plain_on_tty, state, enabled=False)
        forced_rich_on_plain = io.StringIO()
        ui.render_compact_activity(forced_rich_on_plain, state, enabled=True)

        box_chars = "─│╭╰╮╯┌└"
        self.assertTrue(any(ch in auto_tty.getvalue() for ch in box_chars))
        self.assertFalse(any(ch in auto_plain.getvalue() for ch in box_chars))
        self.assertFalse(any(ch in forced_plain_on_tty.getvalue() for ch in box_chars))
        self.assertTrue(any(ch in forced_rich_on_plain.getvalue() for ch in box_chars))


if __name__ == "__main__":
    unittest.main()
