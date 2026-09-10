#!/usr/bin/env python3
"""Focused regression for garusis/cowork-internal#45: an authored final-suite
label must never abort the builder -> build-reviewer handoff.

A planner that names its final suite with an ordinary human phrase (the field
observation is `focused regression suite`) used to kill the run at context
assembly: `cowork.verification_overlay` copied the label verbatim into the
reviewer edge facts, and `cowork_handoff._assert_content_free` rejects any
value that is not a single whitespace-free token -- so the reviewer never
started at all.

What is proven here:

  - both reviewer edges (fresh and resumed) render with a spaced label;
  - the RAW payload still fails the gate, so the crash was fixed by
    sanitizing the overlay, not by loosening the content-free boundary;
  - the authored wording survives verbatim in the receipt and the pointer on
    disk, and rides in NEITHER reviewer prompt;
  - the substituted code is content-free, deterministic in-process AND across
    a fresh interpreter, and distinct for variants a naive slug would merge;
  - already-safe token labels pass through byte-identically;
  - the checkpoint pointer dispatch is untouched;
  - an already-accepted pointer is reused for a resumed handoff with no new
    verification dispatch.

Run standalone:

    python3 -m unittest scripts/test_final_suite_handoff.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

# The exact label observed in the field (skills session33627541 / P3
# session39176912). Kept as the primary fixture value so the regression is
# driven by the real reported input, not a synthetic stand-in.
OBSERVED_LABEL = "focused regression suite"

# Labels a naive slugger would collapse onto each other, plus the shapes the
# ticket names explicitly: newline/tab, repeated whitespace, non-ASCII, and
# over-length (with and without whitespace).
LABEL_VARIANTS = (
    OBSERVED_LABEL,
    "focused_regression_suite",
    "focused\nregression\tsuite",
    "focused  regression  suite",
    "enfoque de regresión enfocado ✅",
    "over long " + ("x" * handoff._MAX_TOKEN),
    "y" * (handoff._MAX_TOKEN + 1),
)

REVIEWER_EDGES = ("builder->build-reviewer:review_ctx",
                  "builder->build-reviewer:review_resume")


class RecordingTrace:
    """Minimal trace double: records `(name, fields)` for every event."""

    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class _HandoffFixture(unittest.TestCase):
    """An isolated `COWORK_SESSIONS_ROOT` per test, plus the receipt-persist /
    pointer-bind / render sequence the real readiness gate performs."""

    MANIFEST = "ab" * 32
    INDEX = "cd" * 32

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]

    # -- fixture construction ------------------------------------------- #

    def _txn_result(self, label):
        """A schema-2 green TransactionResult carrying `label` as its
        final-suite identity."""
        return {
            "transaction_id": "T-" + uuid.uuid4().hex[:8],
            "request_key": "k1",
            "verdict": verification.VERDICT_GREEN,
            "final_suite_label": label,
            "final_suite_binding": "ran_once",
            "attempts": [
                {"label": "compile_check", "kind": verification.KIND_BASELINE,
                 "exit_code": 0, "evidence_state": "present",
                 "wall_time_s": 0.4},
                {"label": "final_suite", "kind": verification.KIND_FINAL_SUITE,
                 "exit_code": 0, "evidence_state": "present",
                 "wall_time_s": 4.0},
            ],
            "mutation": None,
            "worker_identity_verified": True,
            "reused_lock_result": False,
            "snapshot": {"manifest_digest": self.MANIFEST,
                         "index_digest": self.INDEX},
            "created_at": "2026-09-01T00:00:00Z",
            "finished_at": "2026-09-01T00:00:10Z",
        }

    def _bind(self, label):
        """Persist the receipt and bind the current-receipt pointer through
        the REAL readiness path. Returns `(result, receipt_path,
        status_path)`."""
        result = self._txn_result(label)
        receipt_path = state_store.verification_result_path_for(
            self.session_uuid, result["transaction_id"])
        self.assertTrue(state_store.write_json_atomic(receipt_path, result))
        assets = state_store.session_assets_dir(self.session_uuid)
        os.makedirs(assets, exist_ok=True)
        status_path = os.path.join(assets, "builder.status.json")
        with open(status_path, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {
                "verification": [{"label": "final_suite", "ok": True,
                                  "source_manifest": self.MANIFEST}]}}, fh)
        summary_path = os.path.join(assets, "builder.summary.md")
        with open(summary_path, "w") as fh:
            fh.write("# build summary\n")
        pointer = cowork._update_receipt_pointer_for_readiness(
            self.session_uuid, "builder", 1, None, result,
            {"state": "verified", "transaction_id": result["transaction_id"]},
            status_path, summary_path=summary_path)
        self.assertIsInstance(pointer, dict)
        return result, receipt_path, status_path

    def _plan_files(self):
        assets = state_store.session_assets_dir(self.session_uuid)
        os.makedirs(assets, exist_ok=True)
        plan_json = os.path.join(assets, "planner.plan.json")
        plan_md = os.path.join(assets, "planner.plan.md")
        with open(plan_json, "w") as fh:
            fh.write("{}")
        with open(plan_md, "w") as fh:
            fh.write("# plan\n")
        return plan_json, plan_md

    def _live_overlay(self):
        overlay, _pointer = cowork._current_verification_overlay(
            self.session_uuid)
        self.assertIsInstance(overlay, dict)
        return overlay

    def _fresh_context(self, status_path, receipt_path):
        plan_json, plan_md = self._plan_files()
        return cowork.assemble_build_reviewer_context(
            "goal", ["builder"], plan_json, plan_md, status_path,
            verification_receipt_path=receipt_path,
            verification_overlay=self._live_overlay())

    def _resumed_context(self, status_path, receipt_path):
        plan_json, plan_md = self._plan_files()
        return cowork.assemble_build_reviewer_resume_context(
            plan_json, plan_md, status_path,
            verification_receipt_path=receipt_path,
            verification_overlay=self._live_overlay())

    def _transaction_dirs(self):
        root = os.path.dirname(state_store.verification_transaction_dir(
            self.session_uuid, "probe"))
        try:
            return sorted(os.listdir(root))
        except OSError:
            return []


class SpacedFinalSuiteLabelHandoffTests(_HandoffFixture):
    """The reported crash: an authored label with whitespace aborted context
    assembly on both reviewer edges before the reviewer could start."""

    def test_spaced_final_suite_label_renders_the_fresh_reviewer_edge(self):
        result, receipt_path, status_path = self._bind(OBSERVED_LABEL)
        # Pre-fix this call raised handoff.ContentFreeError.
        rendered = self._fresh_context(status_path, receipt_path)
        self.assertTrue(str(rendered).strip())
        self.assertIn(result["transaction_id"], rendered)
        self.assertIn(receipt_path, rendered)

    def test_spaced_final_suite_label_renders_the_resumed_reviewer_edge(self):
        result, receipt_path, status_path = self._bind(OBSERVED_LABEL)
        # The resumed edge fails identically pre-fix -- a reviewer already
        # mid-loop is exactly as blocked as one that never started.
        rendered = self._resumed_context(status_path, receipt_path)
        self.assertTrue(str(rendered).strip())
        self.assertIn(result["transaction_id"], rendered)
        self.assertIn(receipt_path, rendered)

    def test_the_unsanitized_fact_payload_still_raises_content_free_error(self):
        """The control: the gate itself was NOT loosened. Feeding the raw
        authored label straight to the choke point still fails closed."""
        for edge_id in REVIEWER_EDGES:
            with self.subTest(edge=edge_id):
                with self.assertRaises(handoff.ContentFreeError):
                    handoff.render_handoff(
                        edge_id, artifacts=[],
                        facts={"final_suite_label": OBSERVED_LABEL})

    def test_authored_label_survives_on_disk_and_never_rides_inline(self):
        _result, receipt_path, status_path = self._bind(OBSERVED_LABEL)
        fresh = self._fresh_context(status_path, receipt_path)
        resumed = self._resumed_context(status_path, receipt_path)

        on_disk = state_store.read_json_tolerant(receipt_path)
        self.assertEqual(on_disk["final_suite_label"], OBSERVED_LABEL)
        pointer = state_store.read_current_receipt_pointer(self.session_uuid)
        self.assertEqual(pointer["final_suite_label"], OBSERVED_LABEL)

        for surface in (fresh, resumed):
            self.assertNotIn(OBSERVED_LABEL, surface)


class SubstitutedLabelCodeTests(_HandoffFixture):
    """Properties of the substituted code -- never its literal wording."""

    def _overlay_label(self, label):
        overlay = cowork.verification_overlay(
            {"transaction_id": "T-x", "final_suite_label": label})
        return overlay["final_suite_label"]

    def test_substituted_codes_are_content_free_deterministic_and_distinct(
            self):
        produced = {}
        for label in LABEL_VARIANTS:
            with self.subTest(label=label):
                code = self._overlay_label(label)
                self.assertTrue(
                    handoff.is_content_free_token(code),
                    "overlay label for %r is not a content-free token" % label)
                self.assertEqual(code, self._overlay_label(label),
                                 "repeated calls disagreed for %r" % label)
                produced[label] = code
        self.assertEqual(
            len(set(produced.values())), len(LABEL_VARIANTS),
            "two label variants collided onto one code: %r" % (produced,))

    def test_a_non_string_label_is_digested_rather_than_raised_on(self):
        """The overlay must never itself abort context assembly -- the exact
        failure mode this fix removes."""
        for value in ([OBSERVED_LABEL], {"label": OBSERVED_LABEL}):
            with self.subTest(value=value):
                code = self._overlay_label(value)
                self.assertTrue(handoff.is_content_free_token(code))
                self.assertNotIn(OBSERVED_LABEL, code)

    def test_codes_are_stable_across_a_fresh_interpreter(self):
        """A per-process-salted builtin `hash()` would pass every in-process
        assertion above and still break resume stability, so the code is
        recomputed in a brand new interpreter and required to agree."""
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import cowork\n"
            "print(cowork.verification_overlay("
            "{'transaction_id': 'T-x', 'final_suite_label': %r})"
            "['final_suite_label'])\n" % (_HERE, OBSERVED_LABEL))
        # Bounded: this suite runs inside an owned verification transaction
        # with a per-command budget, so an unbounded child could burn the
        # whole budget and report nothing. A timeout fails this test with a
        # named cause instead.
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(),
                         self._overlay_label(OBSERVED_LABEL))

    def test_safe_token_labels_pass_through_unchanged(self):
        """Already-safe labels render byte-identically to before the fix --
        including the schema-1 `legacy_unknown` value."""
        for label in ("full_unit_suite", "final_suite",
                      verification.FINAL_SUITE_LEGACY_UNKNOWN):
            with self.subTest(label=label):
                self.assertEqual(self._overlay_label(label), label)
        self.assertIsNone(
            cowork.verification_overlay({"transaction_id": "T-x"})[
                "final_suite_label"])


class ContentFreeBoundaryIntactTests(unittest.TestCase):
    """Machine-checks that the crash was fixed by sanitizing the overlay, not
    by weakening the boundary the overlay is sanitized FOR."""

    def test_final_suite_label_still_validates_as_a_content_free_token(self):
        self.assertIs(handoff._FACT_SCHEMAS["final_suite_label"],
                      handoff.is_content_free_token)
        self.assertFalse(handoff.is_content_free_token("a b"))
        self.assertFalse(
            handoff.is_content_free_token("x" * (handoff._MAX_TOKEN + 1)))
        self.assertTrue(handoff.is_content_free_token("legacy_unknown"))

    def test_both_reviewer_edges_still_declare_the_fact(self):
        for edge_id in REVIEWER_EDGES:
            with self.subTest(edge=edge_id):
                self.assertIn("final_suite_label",
                              handoff.EDGES[edge_id]["facts"])


class CheckpointDispatchUnchangedTests(unittest.TestCase):
    """The checkpoint branch of `verification_overlay` is untouched."""

    def test_a_checkpoint_pointer_still_routes_to_checkpoint_overlay(self):
        pointer = {"checkpoint_id": "C-1", "work_id": "W-1",
                   "phase": "building", "candidate_digest": "d" * 64,
                   "verdict": "accepted"}
        overlay = cowork.verification_overlay(pointer)
        self.assertEqual(overlay, cowork.checkpoint_overlay(pointer))
        self.assertEqual(overlay["checkpoint_id"], "C-1")
        self.assertNotIn("txn_id", overlay)
        self.assertNotIn("final_suite_label", overlay)

    def test_a_pointer_binding_neither_still_returns_none(self):
        self.assertIsNone(cowork.verification_overlay({}))
        self.assertIsNone(cowork.verification_overlay(None))


class AcceptedPointerReuseTests(_HandoffFixture):
    """A resumed reviewer handoff reuses the already-accepted result; it never
    dispatches verification again."""

    def test_accepted_pointer_is_reused_for_a_resumed_edge_without_dispatch(
            self):
        result, receipt_path, status_path = self._bind(OBSERVED_LABEL)
        txn_id = result["transaction_id"]
        trace = RecordingTrace()
        cowork._emit_verification_disposition(
            self.session_uuid, trace, txn_id,
            verification.DISPOSITION_ACCEPTED, review_round=1,
            reviewed_manifest_digest=self.MANIFEST)
        self.assertEqual(
            cowork._latest_verification_disposition(self.session_uuid, txn_id),
            verification.DISPOSITION_ACCEPTED)
        self.assertIn("verification.disposition",
                      [name for name, _fields in trace.events])

        def _fail_on_dispatch(*args, **kwargs):
            self.fail("assembling a resumed reviewer context dispatched a new "
                      "verification transaction")

        original = verification.run_transaction
        verification.run_transaction = _fail_on_dispatch
        self.addCleanup(setattr, verification, "run_transaction", original)

        before = self._transaction_dirs()
        resumed = self._resumed_context(status_path, receipt_path)
        self.assertEqual(before, self._transaction_dirs())

        self.assertIn("disposition=accepted", resumed)
        self.assertIn(txn_id, resumed)
        self.assertNotIn(OBSERVED_LABEL, resumed)


if __name__ == "__main__":
    unittest.main()
