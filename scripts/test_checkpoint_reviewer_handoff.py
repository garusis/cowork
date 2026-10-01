#!/usr/bin/env python3
"""Per-checkpoint receipt handoff and stale-claim suppression.

Covers the receipt/overlay half: the checkpoint scope on
`_latest_verification_disposition`/`_emit_verification_disposition`/
`verification_overlay`/`_current_verification_overlay` and the
`checkpoint_`-prefixed helpers (`scripts/cowork.py`), and the
`checkpoint_receipt` handoff slot/rendering/route declarations
(`scripts/cowork_handoff.py`). Checkpoint pointer bindings are constructed
directly here rather than driven through the dispatch loop.

Run standalone:

    python3 -m unittest scripts/test_checkpoint_reviewer_handoff.py -v
"""

import os
import shutil
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

# =========================================================================== #
# Fixture base: an isolated COWORK_SESSIONS_ROOT per test.                    #
# =========================================================================== #


class _SessionFixture(unittest.TestCase):

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

    def _publish_checkpoint(self, work_id, checkpoint_id, verdict,
                            phase="building", rejection_reason=None):
        request_raw = {
            "checkpoint_schema_version": verification.CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id, "session_uuid": self.session_uuid,
            "work_id": work_id, "phase": phase,
            "candidate_digest": "d" * 64,
            "argv": ["python3", "-c", "pass"], "cwd": os.getcwd(),
            "mutation_class": verification.MUTATION_CLASS_READ_ONLY,
            "status": verification.CHECKPOINT_STATUS_REQUIRED,
        }
        request = verification.normalize_checkpoint_request(request_raw)
        state_store.write_json_atomic(
            state_store.checkpoint_request_path_for(
                self.session_uuid, checkpoint_id), request)
        receipt_raw = {
            "checkpoint_schema_version": verification.CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id, "session_uuid": self.session_uuid,
            "work_id": work_id, "phase": phase,
            "candidate_digest": "d" * 64, "verdict": verdict, "terminal": True,
        }
        if rejection_reason:
            receipt_raw["rejection_reason"] = rejection_reason
        receipt = verification.normalize_checkpoint_receipt(receipt_raw)
        ok, published = verification.publish_checkpoint_receipt(
            self.session_uuid, checkpoint_id, receipt)
        self.assertTrue(ok)
        return published

    def _bind_current_checkpoint(self, work_id, checkpoint_id,
                                 disposition=None):
        pointer = {"checkpoint_id": checkpoint_id}
        if disposition is not None:
            pointer["disposition"] = disposition
        state_store.write_json_atomic(
            state_store.current_checkpoint_pointer_path_for(
                self.session_uuid, work_id), pointer)
        return pointer


class RecordingTrace:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


# =========================================================================== #
# Default whole-transaction compatibility (negative regression controls).     #
# =========================================================================== #


class WholeTransactionDefaultCompatibilityTests(_SessionFixture):
    """Every one of the four extended functions must behave EXACTLY as
    before for every existing (non-checkpoint) caller."""

    def test_latest_disposition_default_signature_unaffected(self):
        txn_id = "T-" + uuid.uuid4().hex[:8]
        self.assertIsNone(
            cowork._latest_verification_disposition(self.session_uuid, txn_id))
        state_store.write_verification_disposition(
            self.session_uuid, {"transaction_id": txn_id,
                               "disposition": "accepted"})
        self.assertEqual(
            cowork._latest_verification_disposition(self.session_uuid, txn_id),
            "accepted")
        # Explicit checkpoint_id=None must be indistinguishable from omitting
        # it entirely.
        self.assertEqual(
            cowork._latest_verification_disposition(
                self.session_uuid, txn_id, checkpoint_id=None),
            "accepted")

    def test_emit_disposition_default_signature_unaffected(self):
        txn_id = "T-" + uuid.uuid4().hex[:8]
        trace = RecordingTrace()
        pointer_path = state_store.current_receipt_pointer_path_for(
            self.session_uuid)
        state_store.write_json_atomic(
            pointer_path, {"transaction_id": txn_id,
                          "disposition": "pending_review"})
        cowork._emit_verification_disposition(
            self.session_uuid, trace, txn_id, verification.DISPOSITION_ACCEPTED,
            review_round=2, reviewed_manifest_digest="m" * 8)
        self.assertEqual(
            cowork._latest_verification_disposition(self.session_uuid, txn_id),
            "accepted")
        self.assertEqual(trace.events[0][0], "verification.disposition")
        self.assertEqual(trace.events[0][1]["transaction_id"], txn_id)
        self.assertNotIn("checkpoint_id", trace.events[0][1])
        pointer = state_store.read_json_tolerant(pointer_path)
        self.assertEqual(pointer["disposition"], "accepted")

    def test_verification_overlay_default_signature_unaffected(self):
        pointer = {
            "transaction_id": "T-1", "manifest_digest": "m" * 64,
            "index_digest": "i" * 64, "verdict": "green",
            "final_suite_label": "full_unit_suite",
            "final_suite_binding": "ran_once", "command_count": 3,
            "disposition": "pending_review", "contradiction": False,
        }
        overlay = cowork.verification_overlay(pointer)
        self.assertEqual(overlay, {
            "txn_id": "T-1", "manifest_digest": "m" * 64,
            "index_digest": "i" * 64, "verdict": "green",
            "final_suite_label": "full_unit_suite",
            "final_suite_binding": "ran_once", "command_count": 3,
            "disposition": "pending_review", "contradiction": False,
        })

    def test_current_verification_overlay_default_signature_unaffected(self):
        state_store.write_current_receipt_pointer(self.session_uuid, {
            "transaction_id": "T-2", "manifest_digest": "m" * 64,
            "index_digest": "i" * 64, "verdict": "green",
            "final_suite_label": "full_unit_suite",
            "final_suite_binding": "ran_once", "command_count": 1,
            "disposition": "pending_review", "contradiction": False,
        })
        overlay, pointer = cowork._current_verification_overlay(
            self.session_uuid)
        self.assertEqual(overlay["txn_id"], "T-2")
        self.assertEqual(pointer["transaction_id"], "T-2")
        # work_id=None must be indistinguishable from omitting it.
        overlay2, pointer2 = cowork._current_verification_overlay(
            self.session_uuid, work_id=None)
        self.assertEqual(overlay2, overlay)
        self.assertEqual(pointer2, pointer)

    def test_no_bound_transaction_returns_none_pair(self):
        overlay, pointer = cowork._current_verification_overlay(
            self.session_uuid)
        self.assertIsNone(overlay)
        self.assertIsNone(pointer)


# =========================================================================== #
# Checkpoint overlay/disposition scope.                                      #
# =========================================================================== #


class CheckpointDispositionTests(_SessionFixture):

    def test_latest_disposition_absent_is_none(self):
        self.assertIsNone(
            cowork.checkpoint_latest_disposition(self.session_uuid, "C-1"))
        self.assertIsNone(
            cowork._latest_verification_disposition(
                self.session_uuid, None, checkpoint_id="C-1"))

    def test_emit_and_read_round_trips(self):
        cid = "C-" + uuid.uuid4().hex[:8]
        trace = RecordingTrace()
        cowork._emit_verification_disposition(
            self.session_uuid, trace, None, verification.DISPOSITION_ACCEPTED,
            checkpoint_id=cid)
        self.assertEqual(
            cowork.checkpoint_latest_disposition(self.session_uuid, cid),
            "accepted")
        self.assertEqual(
            cowork._latest_verification_disposition(
                self.session_uuid, None, checkpoint_id=cid),
            "accepted")
        self.assertEqual(trace.events[0][0], "verification.disposition")
        self.assertEqual(trace.events[0][1]["checkpoint_id"], cid)
        self.assertNotIn("transaction_id", trace.events[0][1])

    def test_emit_patches_the_named_current_checkpoint_pointer_in_place(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        cid = "C-" + uuid.uuid4().hex[:8]
        self._bind_current_checkpoint(work_id, cid, disposition="pending_review")
        cowork._emit_verification_disposition(
            self.session_uuid, None, None, verification.DISPOSITION_REJECTED,
            checkpoint_id=cid, work_id=work_id)
        pointer = state_store.read_json_tolerant(
            state_store.current_checkpoint_pointer_path_for(
                self.session_uuid, work_id))
        self.assertEqual(pointer["disposition"], "rejected")

    def test_emit_never_patches_a_pointer_naming_a_different_checkpoint(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        bound_cid = "C-bound"
        other_cid = "C-other"
        self._bind_current_checkpoint(work_id, bound_cid,
                                      disposition="pending_review")
        cowork._emit_verification_disposition(
            self.session_uuid, None, None, verification.DISPOSITION_ACCEPTED,
            checkpoint_id=other_cid, work_id=work_id)
        pointer = state_store.read_json_tolerant(
            state_store.current_checkpoint_pointer_path_for(
                self.session_uuid, work_id))
        self.assertEqual(pointer["disposition"], "pending_review")

    def test_invalid_disposition_writes_nothing(self):
        cid = "C-" + uuid.uuid4().hex[:8]
        cowork.checkpoint_emit_disposition(
            self.session_uuid, None, cid, "not-a-real-disposition")
        self.assertIsNone(
            cowork.checkpoint_latest_disposition(self.session_uuid, cid))


class CheckpointOverlayTests(_SessionFixture):

    def test_overlay_pure_builder_returns_closed_field_set(self):
        pointer = {
            "checkpoint_id": "C-1", "work_id": "W-1", "phase": "building",
            "candidate_digest": "d" * 64, "verdict": "accepted",
            "rejection_reason": None, "disposition": "pending_review",
        }
        overlay = cowork.checkpoint_overlay(pointer)
        self.assertEqual(set(overlay), {
            "checkpoint_id", "work_id", "phase", "candidate_digest",
            "verdict", "rejection_reason", "disposition",
        })
        self.assertEqual(overlay["checkpoint_id"], "C-1")
        self.assertEqual(overlay["disposition"], "pending_review")

    def test_overlay_returns_none_without_a_checkpoint_id(self):
        self.assertIsNone(cowork.checkpoint_overlay({}))
        self.assertIsNone(cowork.checkpoint_overlay(None))

    def test_overlay_defaults_disposition_to_pending_review(self):
        pointer = {"checkpoint_id": "C-1"}
        overlay = cowork.checkpoint_overlay(pointer)
        self.assertEqual(overlay["disposition"], "pending_review")

    def test_overlay_join_prefers_the_explicit_disposition_argument(self):
        pointer = {"checkpoint_id": "C-1", "disposition": "pending_review"}
        overlay = cowork.checkpoint_overlay(pointer, disposition="accepted")
        self.assertEqual(overlay["disposition"], "accepted")

    def test_verification_overlay_dispatches_to_checkpoint_by_shape(self):
        pointer = {"checkpoint_id": "C-1", "work_id": "W-1",
                  "phase": "building", "verdict": "accepted"}
        overlay = cowork.verification_overlay(pointer)
        self.assertEqual(overlay["checkpoint_id"], "C-1")
        self.assertNotIn("txn_id", overlay)

    def test_verification_overlay_still_returns_none_for_neither_key(self):
        self.assertIsNone(cowork.verification_overlay({"unrelated": 1}))
        self.assertIsNone(cowork.verification_overlay(None))


class CheckpointCurrentOverlayTests(_SessionFixture):

    def test_no_binding_returns_none_pair(self):
        overlay, pointer = cowork.checkpoint_current_overlay(
            self.session_uuid, "W-nope")
        self.assertIsNone(overlay)
        self.assertIsNone(pointer)

    def test_resolves_from_the_terminal_receipt_on_disk(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        cid = "C-" + uuid.uuid4().hex[:8]
        receipt = self._publish_checkpoint(
            work_id, cid, verification.CHECKPOINT_ACCEPTED)
        self._bind_current_checkpoint(work_id, cid)
        overlay, pointer = cowork.checkpoint_current_overlay(
            self.session_uuid, work_id)
        self.assertIsInstance(overlay, dict)
        self.assertEqual(overlay["checkpoint_id"], cid)
        self.assertEqual(overlay["verdict"], receipt["verdict"])
        self.assertEqual(overlay["phase"], receipt["phase"])
        self.assertEqual(overlay["disposition"], "pending_review")
        self.assertEqual(pointer["receipt_path"],
                         state_store.checkpoint_receipt_path_for(
                             self.session_uuid, cid))

    def test_joins_the_latest_recorded_disposition(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        cid = "C-" + uuid.uuid4().hex[:8]
        self._publish_checkpoint(work_id, cid, verification.CHECKPOINT_ACCEPTED)
        self._bind_current_checkpoint(work_id, cid)
        cowork.checkpoint_emit_disposition(
            self.session_uuid, None, cid, verification.DISPOSITION_ACCEPTED)
        overlay, _pointer = cowork.checkpoint_current_overlay(
            self.session_uuid, work_id)
        self.assertEqual(overlay["disposition"], "accepted")

    def test_no_terminal_receipt_yet_returns_none_pair(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        cid = "C-not-terminal-yet"
        self._bind_current_checkpoint(work_id, cid)
        overlay, pointer = cowork.checkpoint_current_overlay(
            self.session_uuid, work_id)
        self.assertIsNone(overlay)
        self.assertIsNone(pointer)

    def test_current_verification_overlay_routes_to_checkpoint_by_work_id(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        cid = "C-" + uuid.uuid4().hex[:8]
        self._publish_checkpoint(work_id, cid, verification.CHECKPOINT_REJECTED,
                                 rejection_reason="nonzero_exit")
        self._bind_current_checkpoint(work_id, cid)
        overlay, pointer = cowork._current_verification_overlay(
            self.session_uuid, work_id=work_id)
        self.assertEqual(overlay["checkpoint_id"], cid)
        self.assertEqual(overlay["verdict"], "rejected")
        # A bound whole-transaction pointer for the SAME session must be
        # completely uninvolved when work_id names a checkpoint engagement.
        state_store.write_current_receipt_pointer(self.session_uuid, {
            "transaction_id": "T-should-not-leak", "verdict": "green",
            "disposition": "pending_review",
        })
        overlay2, _ = cowork._current_verification_overlay(
            self.session_uuid, work_id=work_id)
        self.assertEqual(overlay2["checkpoint_id"], cid)
        self.assertNotIn("txn_id", overlay2)


# =========================================================================== #
# Stale/superseded checkpoint-claim suppression.                             #
# =========================================================================== #


class CheckpointSupersededSuppressionTests(_SessionFixture):

    def test_no_other_checkpoints_yields_empty_list(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        cid = "C-" + uuid.uuid4().hex[:8]
        self._publish_checkpoint(work_id, cid, verification.CHECKPOINT_ACCEPTED)
        self.assertEqual(
            cowork.checkpoint_superseded_ids(self.session_uuid, work_id, cid),
            [])

    def test_earlier_checkpoint_for_the_same_work_is_superseded(self):
        work_id = "W-" + uuid.uuid4().hex[:8]
        first = "C-first"
        second = "C-second"
        self._publish_checkpoint(work_id, first,
                                 verification.CHECKPOINT_REJECTED,
                                 rejection_reason="timed_out")
        self._publish_checkpoint(work_id, second,
                                 verification.CHECKPOINT_ACCEPTED)
        superseded = cowork.checkpoint_superseded_ids(
            self.session_uuid, work_id, second)
        self.assertEqual(superseded, [first])

    def test_a_different_work_ids_checkpoint_is_never_superseded(self):
        work_id_a = "W-a"
        work_id_b = "W-b"
        cid_a = "C-a"
        cid_b = "C-b"
        self._publish_checkpoint(work_id_a, cid_a,
                                 verification.CHECKPOINT_ACCEPTED)
        self._publish_checkpoint(work_id_b, cid_b,
                                 verification.CHECKPOINT_ACCEPTED)
        self.assertEqual(
            cowork.checkpoint_superseded_ids(self.session_uuid, work_id_a,
                                             cid_a),
            [])

    def test_no_session_or_work_id_returns_empty_list(self):
        self.assertEqual(
            cowork.checkpoint_superseded_ids(None, "W-1", "C-1"), [])
        self.assertEqual(
            cowork.checkpoint_superseded_ids(self.session_uuid, None, "C-1"),
            [])


# =========================================================================== #
# cowork_handoff.py: slot label, rendering block, and route declarations.    #
# =========================================================================== #


class HandoffSlotLabelTests(unittest.TestCase):

    def test_checkpoint_receipt_slot_label_exists_and_is_content_free(self):
        label = handoff.SLOT_LABELS.get("checkpoint_receipt")
        self.assertIsInstance(label, str)
        self.assertTrue(label)
        self.assertNotEqual(label, handoff.SLOT_LABELS["verification_receipt"])


class RenderOwnedVerificationBlockTests(unittest.TestCase):
    """Default (whole-transaction-only) behavior must be byte-identical to
    the signed base; checkpoint facts are purely additive."""

    def test_no_facts_at_all_renders_empty_string(self):
        self.assertEqual(handoff._render_owned_verification_block({}), "")

    def test_whole_transaction_only_matches_the_legacy_shape(self):
        facts = {
            "txn_id": "T-1", "verdict": "green",
            "final_suite_label": "full_unit_suite",
            "final_suite_binding": "ran_once",
            "manifest_digest": "m" * 64, "index_digest": "i" * 64,
            "command_count": 2, "disposition": "pending_review",
            "contradiction": False,
        }
        rendered = handoff._render_owned_verification_block(facts)
        self.assertIn("Owned verification receipt", rendered)
        self.assertIn("transaction=T-1", rendered)
        self.assertIn("verdict=green", rendered)
        self.assertIn("disposition=pending_review", rendered)
        self.assertNotIn("checkpoint", rendered.lower())
        self.assertNotIn("CONTRADICTION", rendered)

    def test_contradiction_flag_still_renders_the_warning(self):
        facts = {"txn_id": "T-1", "verdict": "red", "contradiction": True}
        rendered = handoff._render_owned_verification_block(facts)
        self.assertIn("CONTRADICTION", rendered)

    def test_checkpoint_only_facts_render_without_any_transaction_block(self):
        facts = {
            "checkpoint_id": "C-1", "checkpoint_phase": "building",
            "checkpoint_verdict": "accepted",
            "checkpoint_disposition": "pending_review",
        }
        rendered = handoff._render_owned_verification_block(facts)
        self.assertIn("Owned checkpoint receipt", rendered)
        self.assertIn("checkpoint=C-1", rendered)
        self.assertIn("verdict=accepted", rendered)
        self.assertNotIn("Owned verification receipt", rendered)

    def test_both_transaction_and_checkpoint_facts_render_both_blocks(self):
        facts = {
            "txn_id": "T-1", "verdict": "green", "disposition": "accepted",
            "checkpoint_id": "C-1", "checkpoint_phase": "building",
            "checkpoint_verdict": "accepted",
            "checkpoint_disposition": "pending_review",
        }
        rendered = handoff._render_owned_verification_block(facts)
        self.assertIn("Owned verification receipt", rendered)
        self.assertIn("Owned checkpoint receipt", rendered)

    def test_superseded_count_renders_a_count_only_suppression_note(self):
        facts = {
            "checkpoint_id": "C-current", "checkpoint_phase": "building",
            "checkpoint_verdict": "accepted",
            "checkpoint_disposition": "pending_review",
            "checkpoint_superseded_count": 2,
        }
        rendered = handoff._render_owned_verification_block(facts)
        self.assertIn("2 earlier checkpoint claim(s)", rendered)
        # The count-only note never names a superseded id — only the CURRENT
        # checkpoint's own id may appear.
        self.assertNotIn("C-old", rendered)

    def test_superseded_count_alone_with_no_current_checkpoint(self):
        facts = {"checkpoint_superseded_count": 1}
        rendered = handoff._render_owned_verification_block(facts)
        self.assertIn("1 earlier checkpoint claim(s)", rendered)
        self.assertNotIn("Owned checkpoint receipt", rendered)
        self.assertNotIn("Owned verification receipt", rendered)


class RouteDeclarationTests(unittest.TestCase):

    def _assert_edge_extended(self, edge_id):
        edge = handoff.EDGES[edge_id]
        self.assertIn("checkpoint_receipt", edge["sources"])
        self.assertNotIn("checkpoint_receipt", edge["required"])
        for fact in ("checkpoint_id", "checkpoint_phase", "checkpoint_verdict",
                    "checkpoint_disposition", "checkpoint_superseded_count"):
            self.assertIn(fact, edge["facts"])
        # Existing required/optional whole-transaction slots are untouched.
        self.assertIn("verification_receipt", edge["sources"])
        self.assertNotIn("verification_receipt", edge["required"])

    def test_review_ctx_edge_gained_the_checkpoint_receipt_slot(self):
        self._assert_edge_extended("builder->build-reviewer:review_ctx")

    def test_review_resume_edge_gained_the_checkpoint_receipt_slot(self):
        self._assert_edge_extended("builder->build-reviewer:review_resume")


# =========================================================================== #
# Non-vacuous path-only delivery, default compatibility, and                  #
# no-agent-restatement, exercised through the real render_handoff choke      #
# point.                                                                      #
# =========================================================================== #


class RenderHandoffEndToEndTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _abs(self, name, content="x"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(content)
        return path

    def _base_artifacts(self):
        return [
            {"path": self._abs("context.md"), "source": "context"},
            {"path": self._abs("plan.json", "{}"), "source": "plan_json"},
            {"path": self._abs("plan.md"), "source": "plan_md"},
            {"path": self._abs("status.json", "{}"), "source": "build_status"},
            {"path": self._abs("baseline.txt"), "source": "build_baseline"},
        ]

    def test_default_call_has_no_checkpoint_content_at_all(self):
        block = handoff.render_handoff(
            "builder->build-reviewer:review_ctx",
            artifacts=self._base_artifacts(), facts={"team": []},
            ctx={"repos": []})
        self.assertNotIn("checkpoint", str(block).lower())
        self.assertNotIn("checkpoint_receipt", block.descriptors and
                         [d.get("path") for d in block.descriptors] or [])

    def test_checkpoint_receipt_delivers_only_a_path_and_tokens(self):
        receipt_path = self._abs(
            "checkpoint.receipt.json",
            '{"stdout_digest": "SHOULD-NEVER-APPEAR-INLINE"}')
        artifacts = self._base_artifacts() + [
            {"path": receipt_path, "source": "checkpoint_receipt"},
        ]
        facts = {
            "team": [], "checkpoint_id": "C-42", "checkpoint_phase": "building",
            "checkpoint_verdict": "accepted",
            "checkpoint_disposition": "pending_review",
        }
        block = handoff.render_handoff(
            "builder->build-reviewer:review_ctx", artifacts=artifacts,
            facts=facts, ctx={"repos": []})
        text = str(block)
        self.assertIn(receipt_path, text)
        self.assertIn("C-42", text)
        self.assertIn("checkpoint_receipt", text)
        # The receipt's own would-be file content never rides inline in the
        # prompt — only the path and the closed-schema tokens do.
        self.assertNotIn("SHOULD-NEVER-APPEAR-INLINE", text)

    def test_superseded_checkpoints_are_never_named_only_counted(self):
        receipt_path = self._abs("checkpoint.receipt.json", "{}")
        artifacts = [
            {"path": self._abs("plan2.json", "{}"), "source": "plan_json"},
            {"path": self._abs("plan2.md"), "source": "plan_md"},
            {"path": self._abs("status2.json", "{}"), "source": "build_status"},
            {"path": self._abs("baseline2.txt"), "source": "build_baseline"},
            {"path": receipt_path, "source": "checkpoint_receipt"},
        ]
        facts = {
            "team": [], "checkpoint_id": "C-current",
            "checkpoint_phase": "building", "checkpoint_verdict": "accepted",
            "checkpoint_disposition": "pending_review",
            "checkpoint_superseded_count": 3,
        }
        block = handoff.render_handoff(
            "builder->build-reviewer:review_resume", artifacts=artifacts,
            facts=facts, ctx={})
        text = str(block)
        self.assertIn("3 earlier checkpoint claim(s)", text)
        self.assertIn("C-current", text)

    def test_a_content_bearing_checkpoint_fact_value_is_rejected(self):
        # The content-free fact gate applies to the new checkpoint fact keys
        # exactly like the existing ones — free-form text cannot ride inline.
        with self.assertRaises(handoff.ContentFreeError):
            handoff.render_handoff(
                "builder->build-reviewer:review_ctx",
                artifacts=self._base_artifacts(),
                facts={"team": [], "checkpoint_id": "C-1",
                      "checkpoint_verdict": "this is definitely agent "
                                            "prose, not a token"},
                ctx={"repos": []})

    def test_an_undeclared_checkpoint_fact_on_an_unrelated_edge_is_rejected(self):
        with self.assertRaises(handoff.ContentFreeError):
            handoff.render_handoff(
                "scout->planner:seed",
                artifacts=[
                    {"path": self._abs("c.md"), "source": "context"},
                    {"path": self._abs("i.json", "{}"), "source": "intel_json"},
                ],
                facts={"checkpoint_id": "C-1"})


if __name__ == "__main__":
    unittest.main()
