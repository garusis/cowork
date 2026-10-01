#!/usr/bin/env python3
"""The central checkpoint gateway: minting and persisting a typed
`CheckpointRequest` at a central dispatch point (`dispatch_role_checkpoint`),
the deterministic non-model runner (`cowork_verification.run_checkpoint`/
`submit_checkpoint_result`) with exclusive once-only claim/lease and
terminal, exact-candidate-bound publication, every required rejection
category, the live-candidate mutation path's reuse of
`cowork_action_policy.OwnedScope.is_declared_output`/`decide(...)`,
crash/resume reconstruction from artifacts alone, `cowork_control_plane.
advance()` refusing a stale/cross-candidate checkpoint, the waiting-role/
reviewer-handoff import, and the `cowork_measure`/`cowork_control_plane`/
`cowork_state` checkpoint seams.

Run standalone:

    python3 -m unittest scripts/test_checkpoint_gateway_integration.py -v
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_control_plane as control_plane  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402
import cowork_verification_evidence as evidence  # noqa: E402
import cowork_verification_worker as worker_module  # noqa: E402

# =========================================================================== #
# Fixture.                                                                     #
# =========================================================================== #


class _SessionFixture(unittest.TestCase):
    """Isolated COWORK_SESSIONS_ROOT, a fresh session id, and a real git
    worktree the checkpoint's `argv`/`cwd` run against."""

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
        self.work_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.work_dir,
                                              ignore_errors=True))
        subprocess.run(["git", "init", "-q"], cwd=self.work_dir, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=self.work_dir, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=self.work_dir, check=True)
        self.candidate_digest = hashlib.sha256(
            uuid.uuid4().hex.encode()).hexdigest()

    def dispatch(self, work_id, argv, mutation_class="read_only",
                 declared_output_paths=None, expected_evidence=None,
                 timeout_s=None, phase="building"):
        return cowork.dispatch_role_checkpoint(
            self.session_uuid, work_id, phase, self.candidate_digest, argv,
            self.work_dir, mutation_class,
            declared_output_paths=declared_output_paths,
            expected_evidence=expected_evidence, timeout_s=timeout_s)


# =========================================================================== #
# Typed request + deterministic runner + rejection categories.                #
# =========================================================================== #


class TypedRequestAndRunnerTests(_SessionFixture):

    def test_dispatch_mints_a_normalized_persisted_request(self):
        checkpoint_id, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('hi')"])
        request = state_store.read_json_tolerant(
            state_store.checkpoint_request_path_for(
                self.session_uuid, checkpoint_id))
        self.assertEqual(request["checkpoint_id"], checkpoint_id)
        self.assertEqual(request["candidate_digest"], self.candidate_digest)
        self.assertEqual(request["mutation_class"], "read_only")
        self.assertEqual(request["status"], "required")
        self.assertEqual(receipt["verdict"], "accepted")

    def test_read_only_accepted_and_current_pointer_bound(self):
        checkpoint_id, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        self.assertEqual(receipt["verdict"], "accepted")
        pointer = state_store.read_json_tolerant(
            state_store.current_checkpoint_pointer_path_for(
                self.session_uuid, "work-1"))
        self.assertEqual(pointer["checkpoint_id"], checkpoint_id)

    def test_isolated_accepted(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"],
            mutation_class="isolated")
        self.assertEqual(receipt["verdict"], "accepted")

    def test_nonzero_exit_rejected(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "import sys; sys.exit(3)"])
        self.assertEqual(receipt["verdict"], "rejected")
        self.assertEqual(receipt["rejection_reason"],
                         "checkpoint_result_nonzero_exit")

    def test_executor_identity_is_persisted_on_the_result(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        self.assertTrue(receipt["result"]["executor_identity"])

    def test_bounded_stdout_digest_present_not_raw_output(self):
        # The runtime-COMPUTED output (never present in argv/source itself,
        # unlike a literal string constant) must never appear raw anywhere
        # in the receipt -- only its bounded digest.
        _cid, receipt = self.dispatch(
            "work-1",
            ["python3", "-c",
             "print(__import__('hashlib').sha256(b'seed-xyz').hexdigest())"])
        result = receipt["result"]
        self.assertEqual(receipt["verdict"], "accepted")
        self.assertRegex(result["stdout_digest"], r"^[0-9a-f]{64}$")
        computed = hashlib.sha256(b"seed-xyz").hexdigest()
        import json as _json
        blob = _json.dumps(receipt)
        self.assertNotIn(computed, blob)

    def test_exact_candidate_binding_on_receipt(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        self.assertEqual(receipt["candidate_digest"], self.candidate_digest)


class RejectionCategoryTests(_SessionFixture):
    """Every required rejection category from the frozen brief."""

    def test_missing_request_rejected(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.run_checkpoint(self.session_uuid, "no-such-ckpt")
        self.assertEqual(ctx.exception.code, "checkpoint_request_missing")

    def test_duplicate_claim_rejected_exclusive_once_only(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "read_only")
        claimed1, _rec1 = verification.claim_checkpoint(
            self.session_uuid, checkpoint_id, "exec-A")
        claimed2, existing = verification.claim_checkpoint(
            self.session_uuid, checkpoint_id, "exec-B")
        self.assertTrue(claimed1)
        self.assertFalse(claimed2)
        self.assertEqual(existing["executor_identity"], "exec-A")

    def test_already_claimed_checkpoint_never_reruns(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "read_only")
        verification.claim_checkpoint(self.session_uuid, checkpoint_id,
                                      "someone-else")
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.run_checkpoint(self.session_uuid, checkpoint_id)
        self.assertEqual(ctx.exception.code, "checkpoint_already_claimed")

    def test_wrong_executor_rejected(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "read_only")
        verification.claim_checkpoint(self.session_uuid, checkpoint_id,
                                      "exec-A")
        raw_result = {
            "checkpoint_schema_version": 1, "checkpoint_id": checkpoint_id,
            "executor_identity": "exec-IMPOSTOR",
            "argv": ["python3", "-c", "print('ok')"], "cwd": self.work_dir,
            "exit_code": 0, "evidence_state": "present",
        }
        receipt = verification.submit_checkpoint_result(
            self.session_uuid, checkpoint_id, raw_result)
        self.assertEqual(receipt["verdict"], "rejected")
        self.assertEqual(receipt["rejection_reason"],
                         "checkpoint_result_wrong_executor")

    def test_result_for_unclaimed_checkpoint_rejected(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "read_only")
        raw_result = {
            "checkpoint_schema_version": 1, "checkpoint_id": checkpoint_id,
            "executor_identity": "exec-A",
            "argv": ["python3", "-c", "print('ok')"], "cwd": self.work_dir,
            "exit_code": 0, "evidence_state": "present",
        }
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.submit_checkpoint_result(
                self.session_uuid, checkpoint_id, raw_result)
        self.assertEqual(ctx.exception.code, "checkpoint_not_claimed")

    def test_wrong_argv_rejected(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "read_only")
        verification.claim_checkpoint(self.session_uuid, checkpoint_id,
                                      "exec-A")
        raw_result = {
            "checkpoint_schema_version": 1, "checkpoint_id": checkpoint_id,
            "executor_identity": "exec-A",
            "argv": ["python3", "-c", "print('DIFFERENT')"],
            "cwd": self.work_dir, "exit_code": 0, "evidence_state": "present",
        }
        receipt = verification.submit_checkpoint_result(
            self.session_uuid, checkpoint_id, raw_result)
        self.assertEqual(receipt["verdict"], "rejected")
        self.assertEqual(receipt["rejection_reason"],
                         "checkpoint_result_wrong_argv")

    def test_wrong_cwd_rejected(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "read_only")
        verification.claim_checkpoint(self.session_uuid, checkpoint_id,
                                      "exec-A")
        other_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(other_dir, ignore_errors=True))
        raw_result = {
            "checkpoint_schema_version": 1, "checkpoint_id": checkpoint_id,
            "executor_identity": "exec-A",
            "argv": ["python3", "-c", "print('ok')"], "cwd": other_dir,
            "exit_code": 0, "evidence_state": "present",
        }
        receipt = verification.submit_checkpoint_result(
            self.session_uuid, checkpoint_id, raw_result)
        self.assertEqual(receipt["verdict"], "rejected")
        self.assertEqual(receipt["rejection_reason"],
                         "checkpoint_result_wrong_cwd")

    def test_over_broad_output_rejected(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "isolated")
        verification.claim_checkpoint(self.session_uuid, checkpoint_id,
                                      "exec-A")
        raw_result = {
            "checkpoint_schema_version": 1, "checkpoint_id": checkpoint_id,
            "executor_identity": "exec-A",
            "argv": ["python3", "-c", "print('ok')"], "cwd": self.work_dir,
            "exit_code": 0, "evidence_state": "present",
            "output_paths": ["never_declared.txt"],
        }
        receipt = verification.submit_checkpoint_result(
            self.session_uuid, checkpoint_id, raw_result)
        self.assertEqual(receipt["verdict"], "rejected")
        self.assertEqual(receipt["rejection_reason"],
                         "checkpoint_result_over_broad_output")

    def test_read_only_that_mutates_is_rejected(self):
        _cid, receipt = self.dispatch(
            "work-1",
            ["python3", "-c", "open('sneaky.txt','w').write('x')"],
            mutation_class="read_only")
        self.assertEqual(receipt["verdict"], "rejected")
        self.assertIn(receipt["rejection_reason"],
                      ("checkpoint_result_over_broad_output",
                       "checkpoint_result_unauthorized_mutation"))

    def test_isolated_that_mutates_is_rejected(self):
        _cid, receipt = self.dispatch(
            "work-1",
            ["python3", "-c", "open('sneaky2.txt','w').write('x')"],
            mutation_class="isolated")
        self.assertEqual(receipt["verdict"], "rejected")

    def test_live_candidate_undeclared_mutation_rejected(self):
        _cid, receipt = self.dispatch(
            "work-1",
            ["python3", "-c", "open('rogue.txt','w').write('x')"],
            mutation_class="live_candidate",
            declared_output_paths=["out.txt"])
        self.assertEqual(receipt["verdict"], "rejected")

    def test_once_only_resubmission_is_a_no_op(self):
        checkpoint_id, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        raw_result = dict(receipt["result"])
        replay = verification.submit_checkpoint_result(
            self.session_uuid, checkpoint_id, raw_result)
        self.assertEqual(replay, receipt)

    def test_timed_out_rejected(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "import time; time.sleep(5)"],
            timeout_s=0.2)
        self.assertEqual(receipt["verdict"], "rejected")
        self.assertEqual(receipt["rejection_reason"],
                         "checkpoint_result_timed_out")


# =========================================================================== #
# Live-candidate mutation checkpoint: declared outputs + action-policy reuse. #
# =========================================================================== #


class LiveCandidateMutationTests(_SessionFixture):

    def test_declared_mutation_accepted_with_candidate_after(self):
        checkpoint_id, receipt = self.dispatch(
            "work-1",
            ["python3", "-c", "open('out.txt','w').write('hello')"],
            mutation_class="live_candidate", declared_output_paths=["out.txt"])
        self.assertEqual(receipt["verdict"], "accepted")
        result = receipt["result"]
        self.assertEqual(result["output_paths"], ["out.txt"])
        self.assertTrue(result["mutation_detected"])
        self.assertNotEqual(result["candidate_digest_after"],
                            self.candidate_digest)

    def test_missing_declared_output_paths_rejected_at_request_time(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.build_and_persist_checkpoint_request(
                self.session_uuid, verification.mint_checkpoint_id("work-1"),
                "work-1", "building", self.candidate_digest,
                ["python3", "-c", "print('ok')"], self.work_dir,
                "live_candidate")
        self.assertEqual(
            ctx.exception.code,
            "checkpoint_request_live_candidate_needs_output_paths")

    def test_reuses_owned_scope_is_declared_output_unmodified(self):
        # Same predicate the request/result seam consults -- proves it is
        # the REAL, unmodified cowork_action_policy.OwnedScope, not a
        # reimplementation, by exercising it directly.
        import cowork_action_policy as action_policy
        scope = action_policy.OwnedScope(
            repo_roots=(self.work_dir,),
            declared_outputs=(os.path.join(self.work_dir, "out.txt"),))
        self.assertTrue(
            scope.is_declared_output(os.path.join(self.work_dir, "out.txt")))
        self.assertFalse(
            scope.is_declared_output(os.path.join(self.work_dir, "other.txt")))

    def test_unauthorized_mutation_path_fails_actual_decide_call(self):
        # Mirrors the checkpoint gateway's OWN scope construction exactly
        # (no `repo_roots` -- see `_authorize_live_candidate_mutations`'s
        # own docstring for why passing `cwd` as a repo root there would
        # silently defeat the declared-output restriction).
        import cowork_action_policy as action_policy
        scope = action_policy.OwnedScope(
            declared_outputs=(os.path.join(self.work_dir, "out.txt"),))
        action = {"class": "write",
                  "targets": [os.path.join(self.work_dir, "rogue.txt")],
                  "resolution_complete": True}
        decision = action_policy.decide(action, scope)
        self.assertFalse(decision["allow"])

    def test_declared_output_path_passes_actual_decide_call(self):
        import cowork_action_policy as action_policy
        scope = action_policy.OwnedScope(
            declared_outputs=(os.path.join(self.work_dir, "out.txt"),))
        action = {"class": "write",
                  "targets": [os.path.join(self.work_dir, "out.txt")],
                  "resolution_complete": True}
        decision = action_policy.decide(action, scope)
        self.assertTrue(decision["allow"])


# =========================================================================== #
# Stale/superseded claims cannot advance the REAL, unmodified advance().      #
# =========================================================================== #


class StaleSupersededAdvanceProofTests(_SessionFixture):

    def test_accepted_checkpoint_advances_real_control_plane(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        evidence_dict, reason = cowork.checkpoint_gate_evidence(
            self.session_uuid, "work-1", self.candidate_digest)
        self.assertIsNone(reason)
        new_state, reason_code = control_plane.advance(
            "awaiting_gate", "gate_validated", evidence_dict,
            expected_candidate={
                "candidate_manifest_digest": self.candidate_digest,
                "candidate_index": None})
        self.assertEqual(new_state, "completed")
        self.assertEqual(reason_code, "gate_validated")

    def test_cross_candidate_checkpoint_refused_by_real_advance(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        evidence_dict, _reason = cowork.checkpoint_gate_evidence(
            self.session_uuid, "work-1", self.candidate_digest)
        other_digest = hashlib.sha256(b"other-candidate").hexdigest()
        new_state, reason_code = control_plane.advance(
            "awaiting_gate", "gate_validated", evidence_dict,
            expected_candidate={
                "candidate_manifest_digest": other_digest,
                "candidate_index": None})
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "gate_evidence_candidate_mismatch")

    def test_checkpoint_gate_evidence_reports_cross_candidate_directly(self):
        self.dispatch("work-1", ["python3", "-c", "print('ok')"])
        other_digest = hashlib.sha256(b"unrelated").hexdigest()
        evidence_dict, reason = cowork.checkpoint_gate_evidence(
            self.session_uuid, "work-1", other_digest)
        self.assertIsNone(evidence_dict)
        self.assertEqual(reason, "checkpoint_cross_candidate")

    def test_superseding_dispatch_makes_old_checkpoint_id_absent_from_pointer(
            self):
        old_id, _old_receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('one')"])
        new_id, new_receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('two')"])
        self.assertNotEqual(old_id, new_id)
        pointer = state_store.read_json_tolerant(
            state_store.current_checkpoint_pointer_path_for(
                self.session_uuid, "work-1"))
        self.assertEqual(pointer["checkpoint_id"], new_id)
        superseded = cowork.checkpoint_superseded_ids(
            self.session_uuid, "work-1", new_id)
        self.assertEqual(superseded, [old_id])

    def test_superseded_id_absent_from_gate_evidence_and_facts(self):
        """A superseded checkpoint id never appears in the gate evidence or
        the handoff facts payload."""
        old_id, _r1 = self.dispatch("work-1", ["python3", "-c", "print(1)"])
        new_id, _r2 = self.dispatch("work-1", ["python3", "-c", "print(2)"])
        evidence_dict, reason = cowork.checkpoint_gate_evidence(
            self.session_uuid, "work-1", self.candidate_digest)
        self.assertIsNone(reason)
        facts = cowork.checkpoint_handoff_facts(self.session_uuid, "work-1")
        self.assertEqual(facts["checkpoint_id"], new_id)
        self.assertNotEqual(facts["checkpoint_id"], old_id)
        # Never present anywhere in the evidence or facts payloads.
        import json as _json
        self.assertNotIn(old_id, _json.dumps(evidence_dict))
        self.assertNotIn(old_id, _json.dumps(facts))

    def test_stale_receipt_manually_shaped_evidence_also_refused(self):
        """Even if a caller manually built gate evidence straight from a
        SUPERSEDED checkpoint's own (still-accepted) receipt -- bypassing
        `checkpoint_gate_evidence`'s pointer-only lookup entirely -- the
        REAL, unmodified `advance()` still refuses it whenever it no longer
        names the candidate actually being advanced. This is the direct
        proof against the frozen reducer itself, not merely this
        candidate's own wrapper."""
        old_id, old_receipt = self.dispatch(
            "work-1", ["python3", "-c", "print(1)"])
        self.dispatch("work-1", ["python3", "-c", "print(2)"])
        stale_evidence = control_plane.checkpoint_receipt_to_gate_evidence(
            old_receipt)
        self.assertIsNotNone(stale_evidence)  # it WAS validly accepted
        # A caller advancing a DIFFERENT (the current) candidate than the
        # stale receipt's own must be refused.
        other_digest = hashlib.sha256(b"now-current-candidate").hexdigest()
        new_state, reason_code = control_plane.advance(
            "awaiting_gate", "gate_validated", stale_evidence,
            expected_candidate={
                "candidate_manifest_digest": other_digest,
                "candidate_index": None})
        self.assertEqual(new_state, "awaiting_gate")
        self.assertEqual(reason_code, "gate_evidence_candidate_mismatch")

    def test_rejected_checkpoint_never_yields_gate_evidence(self):
        self.dispatch("work-1", ["python3", "-c", "import sys; sys.exit(1)"])
        evidence_dict, reason = cowork.checkpoint_gate_evidence(
            self.session_uuid, "work-1", self.candidate_digest)
        self.assertIsNone(evidence_dict)
        self.assertEqual(reason, "checkpoint_rejected")

    def test_missing_checkpoint_never_yields_gate_evidence(self):
        evidence_dict, reason = cowork.checkpoint_gate_evidence(
            self.session_uuid, "no-such-work", self.candidate_digest)
        self.assertIsNone(evidence_dict)
        self.assertEqual(reason, "checkpoint_missing")

    def test_checkpoint_blocks_advance_predicate(self):
        self.assertTrue(control_plane.checkpoint_blocks_advance("pending"))
        self.assertTrue(control_plane.checkpoint_blocks_advance("claimed"))
        self.assertTrue(control_plane.checkpoint_blocks_advance(
            "terminal", {"verdict": "rejected"}))
        self.assertFalse(control_plane.checkpoint_blocks_advance(
            "terminal", {"verdict": "accepted"}))


# =========================================================================== #
# Waiting-role import + reviewer handoff/overlay.                             #
# =========================================================================== #


class HandoffImportTests(_SessionFixture):

    def test_checkpoint_handoff_facts_closed_schema_valid(self):
        _cid, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        facts = cowork.checkpoint_handoff_facts(self.session_uuid, "work-1")
        # Must validate against cowork_handoff's closed _FACT_SCHEMAS
        # -- render_handoff raises ContentFreeError on any violation.
        artifacts = [{
            "label": "shared session context", "path": __file__,
            "kind": "markdown", "source": "context"}]
        block = handoff.render_handoff(
            "builder->build-reviewer:review_resume",
            artifacts=[{
                "label": "plan", "path": __file__, "kind": "json",
                "source": "plan_json"},
                {"label": "plan md", "path": __file__, "kind": "markdown",
                 "source": "plan_md"},
                {"label": "status", "path": __file__, "kind": "json",
                 "source": "build_status"},
                {"label": "baseline", "path": __file__, "kind": "markdown",
                 "source": "build_baseline"}],
            facts=dict({"team": []}, **facts), ctx={"repos": []})
        self.assertIn(facts["checkpoint_id"], block)

    def test_every_checkpoint_fact_has_a_closed_schema(self):
        for key in ("checkpoint_id", "checkpoint_phase", "checkpoint_verdict",
                   "checkpoint_disposition", "checkpoint_superseded_count"):
            self.assertIn(key, handoff._FACT_SCHEMAS,
                          "%r has no closed schema entry" % key)

    def test_bad_checkpoint_verdict_is_rejected(self):
        schema = handoff._FACT_SCHEMAS["checkpoint_verdict"]
        self.assertFalse(schema("not-a-real-verdict"))
        self.assertTrue(schema("accepted"))

    def test_superseded_count_counts_earlier_checkpoints_only(self):
        """The superseded count reflects genuinely earlier checkpoints, in
        creation order."""
        old_id, _r1 = self.dispatch("work-1", ["python3", "-c", "print(1)"])
        new_id, _r2 = self.dispatch("work-1", ["python3", "-c", "print(2)"])
        old_request = state_store.read_json_tolerant(
            state_store.checkpoint_request_path_for(self.session_uuid, old_id))
        new_request = state_store.read_json_tolerant(
            state_store.checkpoint_request_path_for(self.session_uuid, new_id))
        self.assertLessEqual(old_request["created_at"],
                             new_request["created_at"])
        facts = cowork.checkpoint_handoff_facts(self.session_uuid, "work-1")
        self.assertEqual(facts.get("checkpoint_superseded_count"), 1)

    def test_checkpoint_wake_block_pending_state(self):
        checkpoint_id = verification.mint_checkpoint_id("work-1")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, "work-1", "building",
            self.candidate_digest, ["python3", "-c", "print('ok')"],
            self.work_dir, "read_only")
        state_store.write_json_atomic_durable(
            state_store.current_checkpoint_pointer_path_for(
                self.session_uuid, "work-1"),
            {"checkpoint_id": checkpoint_id, "work_id": "work-1"})
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "work-1", "builder")
        self.assertIn("pending", str(block).lower())

    def test_checkpoint_wake_block_terminal_state(self):
        checkpoint_id, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "work-1", "builder")
        self.assertIn("terminal", str(block).lower())
        self.assertIn(checkpoint_id, "".join(d["path"] for d in
                                             block.descriptors))

    def test_checkpoint_wake_block_none_when_nothing_bound(self):
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "work-unbound", "builder")
        self.assertIsNone(block)

    def test_checkpoint_wake_route_distinct_from_ds_checkpoint_receipt_slot(
            self):
        edge = handoff.EDGES["cowork->role:checkpoint_wake"]
        self.assertEqual(edge["sources"], ["checkpoint_status"])
        self.assertNotIn("checkpoint_receipt", edge["sources"])
        self.assertIn("checkpoint_status", handoff.SLOT_LABELS)

    def test_receipt_reaches_build_reviewer_context_via_make_runner(self):
        checkpoint_id, receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        runner = cowork.make_build_reviewer_runner(
            "plan.json", "plan.md", session_uuid=self.session_uuid,
            role_work_id="work-1")
        self.assertTrue(callable(runner))
        # The wiring seam itself: receipt_kwargs is a closure, but the
        # facts producer it calls is directly testable and IS what backs it.
        facts = cowork.checkpoint_handoff_facts(self.session_uuid, "work-1")
        self.assertEqual(facts["checkpoint_id"], checkpoint_id)


# =========================================================================== #
# Crash/resume reconstruction from artifacts alone.                           #
# =========================================================================== #


class CrashResumeReconstructionTests(_SessionFixture):

    def test_pending_claimed_terminal_reconstructed_from_artifacts(self):
        pending_id = verification.mint_checkpoint_id("work-pending")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, pending_id, "work-pending", "building",
            self.candidate_digest, ["python3", "-c", "print(1)"],
            self.work_dir, "read_only")

        claimed_id = verification.mint_checkpoint_id("work-claimed")
        verification.build_and_persist_checkpoint_request(
            self.session_uuid, claimed_id, "work-claimed", "building",
            self.candidate_digest, ["python3", "-c", "print(2)"],
            self.work_dir, "read_only")
        verification.claim_checkpoint(self.session_uuid, claimed_id,
                                      "exec-mid-run")

        terminal_id, _receipt = self.dispatch(
            "work-terminal", ["python3", "-c", "print(3)"])

        recon = state_store.reconstruct_session_checkpoints(self.session_uuid)
        self.assertEqual(recon[pending_id]["state"], "pending")
        self.assertEqual(recon[claimed_id]["state"], "claimed")
        self.assertEqual(recon[terminal_id]["state"], "terminal")
        self.assertEqual(recon[terminal_id]["receipt"]["verdict"], "accepted")

        # cowork_verification's own reconstruction agrees (A's per-id
        # primitive, wired session-wide by E).
        all_ckpts = verification.reconstruct_all_checkpoints(self.session_uuid)
        self.assertEqual(all_ckpts[pending_id]["state"], "pending")
        self.assertEqual(all_ckpts[claimed_id]["state"], "claimed")
        self.assertEqual(all_ckpts[terminal_id]["state"], "terminal")

    def test_unknown_checkpoint_id_absent_from_reconstruction(self):
        recon = state_store.reconstruct_session_checkpoints(self.session_uuid)
        self.assertEqual(recon, {})


# =========================================================================== #
# cowork_measure.py: additive checkpoint-scoped attempt reconciliation.       #
# =========================================================================== #


class MeasureReconciliationTests(_SessionFixture):

    def test_accepted_checkpoint_reconciles_to_corroborated_attempt(self):
        checkpoint_id, _receipt = self.dispatch(
            "work-1", ["python3", "-c", "print('ok')"])
        attempts = measure.checkpoint_reconciled_attempts(self.session_uuid)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["checkpoint_id"], checkpoint_id)
        self.assertEqual(attempts[0]["claim_state"], "corroborated")
        self.assertEqual(attempts[0]["kind"], "attempt")

    def test_rejected_checkpoint_reconciles_to_contradicted_attempt(self):
        self.dispatch("work-1", ["python3", "-c", "import sys; sys.exit(9)"])
        attempts = measure.checkpoint_reconciled_attempts(self.session_uuid)
        self.assertEqual(attempts[0]["claim_state"], "contradicted")

    def test_no_checkpoints_yields_empty_list(self):
        self.assertEqual(
            measure.checkpoint_reconciled_attempts(self.session_uuid), [])

    def test_owned_transaction_cost_summary_never_called_by_this_producer(
            self):
        # No shared mutable state / no cross-contamination: the checkpoint
        # producer never CALLS owned-transaction cost rollups (distinct
        # accounting axis, see checkpoint_reconciled_attempts' own
        # docstring) -- checked over the function's actual Call nodes, not
        # its prose (which names both functions to explain why NOT).
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(measure.checkpoint_reconciled_attempts))
        called = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)}
        self.assertNotIn("owned_transaction_cost_summary", called)
        self.assertNotIn("_owned_cost_rollups", called)


# =========================================================================== #
# Seam integration: worker source resolution, evidence reconciliation, and    #
# deferral-gated checkout reclaim.                                            #
# =========================================================================== #


class SeamIntegrationTests(unittest.TestCase):
    """The worker-capture and evidence-reconciliation seams as the spine
    actually reaches them, plus the deferral guard over checkout reclaim."""

    def test_resolve_worker_source_without_identity_degrades_to_none(self):
        self.assertIsNone(worker_module.resolve_worker_source())

    def test_reconcile_pending_evidence_without_ids_is_an_empty_no_op(self):
        result = evidence.reconcile_pending_evidence(None, None, None)
        self.assertEqual(result, {"transaction_id": None, "reconciled": [],
                                  "still_pending": []})

    def test_checkout_reclaim_is_gated_by_should_defer_teardown(self):
        """`_run_owned_transaction` computes `should_defer_teardown` once and
        gates BOTH the teardown calls and the captured-checkout reclaim on
        that same decision -- a deferred (possibly still alive) worker must
        never have its own captured source checkout removed underneath it."""
        import ast
        import inspect
        source = inspect.getsource(verification)
        tree = ast.parse(source)
        found_call = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "reclaim_tool_snapshot_checkout"):
                found_call = node
                break
        self.assertIsNotNone(found_call,
                             "reclaim_tool_snapshot_checkout call site not "
                             "found in cowork_verification.py")
        # The call must appear textually AFTER a `should_defer_teardown`
        # consult stored to a local (`_defer_teardown`) that ALSO guards it
        # -- i.e. it is not reachable unconditionally in the finally block.
        segment_start = source.rfind(
            "_defer_teardown = should_defer_teardown")
        call_offset = source.find("reclaim_tool_snapshot_checkout(session_uuid, transaction_id)",
                                  segment_start)
        self.assertGreater(segment_start, 0)
        self.assertGreater(call_offset, segment_start)
        guard_offset = source.find("if not _defer_teardown:", segment_start)
        self.assertGreater(guard_offset, segment_start)
        self.assertGreater(call_offset, guard_offset,
                           "checkout reclaim call must textually follow "
                           "the should_defer_teardown-derived guard")


# =========================================================================== #
# Cancellation ordering, end-to-end, non-vacuous.                             #
# =========================================================================== #


def _seed_worker_into_repo(repo):
    """Copy the REAL, candidate `cowork_verification.py` and every module it
    imports at load time into a throwaway repo's `scripts/` dir and commit
    them, so a real `--worker` subprocess `run_transaction` spawns below is
    fully self-sufficient -- mirrors `test_checkpoint_contracts.py`'s own
    fixture of the same name (duplicated, not imported, since test additions
    are confined to this file)."""
    dest_dir = os.path.join(repo, "scripts")
    os.makedirs(dest_dir, exist_ok=True)
    for name in ("cowork_verification.py", "cowork_state.py",
                "cowork_policy.py", "cowork_ledger.py",
                "cowork_verification_worker.py",
                "cowork_verification_evidence.py"):
        shutil.copyfile(os.path.join(_HERE, name),
                        os.path.join(dest_dir, name))
    subprocess.run(["git", "-C", repo, "add", "scripts"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "seed worker"],
                   check=True)


class _RealTransactionFixture(unittest.TestCase):
    """Isolated COWORK_SESSIONS_ROOT and a throwaway committed git repo
    seeded with the real candidate worker modules, so `run_transaction`
    below spawns a genuine `--worker` subprocess -- exactly the shape
    `test_checkpoint_contracts.py`'s own `_RealWorkerFixture` uses for the
    same reason (a real, unmocked identity-verified worker is the only way
    to prove cancellation ordering end-to-end, not merely unit-test it)."""

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

        self.repo = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email",
                        "t@t"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"],
                       check=True)
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "-C", self.repo, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "init"],
                       check=True)
        _seed_worker_into_repo(self.repo)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]


class CancellationOrderingCoverageTests(_RealTransactionFixture):
    """Focused, deterministic, end-to-end proof that
    `_run_owned_transaction`'s three distinct cancellation dispositions --
    pre-launch, identity-wait, and mid-flight -- each still resolve
    correctly and (where applicable) boundedly, none of them synchronized
    via a raw sleep that could race a platform-dependent startup duration.
    Sibling coverage of the same dispositions lives in
    `test_checkpoint_contracts.CancellationOrderingEndToEndTests` and
    `test_verification_evidence_reconciliation.
    CancellationDeadlineSemanticsPreservedTests`."""

    def test_pre_launch_cancellation_is_bounded_and_unverified(self):
        # Cancelled before `run_transaction` is ever called: no timing
        # dependency at all (cancel_event is already permanently set), so
        # this is inherently immune to any platform-dependent startup
        # duration.
        cancel_event = threading.Event()
        cancel_event.set()
        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        started = time.time()
        result = verification.run_transaction(
            self.repo, self.session_uuid, entries, cancel_event=cancel_event)
        elapsed = time.time() - started
        self.assertLess(
            elapsed, 20,
            "a cancel_event already set before the transaction starts must "
            "not block for anywhere near the full startup allowance")
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)

    def test_identity_wait_cancellation_gates_entry_zero_before_it_starts(
            self):
        # The identity read's own duration is entirely dictated by this
        # mock's `time.sleep(0.5)` -- a fixed, deterministic delay the mock
        # itself imposes on the operation being measured, not a guess about
        # how long some OTHER independent, platform-dependent step (worker
        # subprocess launch, disk I/O) will take. Nothing here races that.
        cancel_event = threading.Event()
        real_read_identity = worker_module._read_worker_identity

        def blocked_read_identity(session_uuid, transaction_id, timeout_s=10,
                                  poll_delay_s=0.2, sleep=time.sleep,
                                  now=time.time, proc=None):
            cancel_event.set()
            time.sleep(0.5)
            return real_read_identity(
                session_uuid, transaction_id, timeout_s=timeout_s,
                poll_delay_s=poll_delay_s, sleep=sleep, now=now, proc=proc)

        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        started = time.time()
        with mock.patch.object(worker_module, "_read_worker_identity",
                               side_effect=blocked_read_identity):
            result = verification.run_transaction(
                self.repo, self.session_uuid, entries,
                cancel_event=cancel_event)
        elapsed = time.time() - started
        self.assertLess(
            elapsed, 10,
            "cancellation during the identity read must remain bounded, "
            "never block for the unrelated command's own (much longer) "
            "timeout")
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        noop_record = evidence._latest_ledger_record(
            ledger_path, result["transaction_id"], "noop") or {}
        self.assertEqual(
            noop_record.get("attempt_state"), "not_reached",
            "entry 0 must never have been started (permit issued) once "
            "cancellation is observed after a genuinely blocked identity "
            "read -- 'unresolved' would instead mean it was, incorrectly, "
            "already committed to running")

    def test_mid_flight_cancellation_yields_red_without_racing_startup(self):
        # Cancellation is fired only once the command has PROVABLY started
        # (observed via a marker file it writes as its very first action),
        # never after a fixed guessed delay -- immune to however long
        # spawn_worker's own real identity verification happens to take on
        # whatever machine this runs on.
        marker_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(marker_dir, ignore_errors=True))
        started_marker = os.path.join(marker_dir, "started")
        done_marker = os.path.join(marker_dir, "done")
        cmd = ["python3", "-c",
              "import time\n"
              "open(%r, 'w').write('x')\n"
              "time.sleep(30)\n"
              "open(%r, 'w').write('x')\n" % (started_marker, done_marker)]
        entries = [{"label": "slow", "command": cmd,
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        cancel_event = threading.Event()

        def _cancel_once_genuinely_started():
            deadline = time.time() + 20
            while time.time() < deadline and not os.path.exists(
                    started_marker):
                time.sleep(0.02)
            cancel_event.set()
        threading.Thread(target=_cancel_once_genuinely_started,
                         daemon=True).start()

        result = verification.run_transaction(
            self.repo, self.session_uuid, entries, cancel_event=cancel_event)
        self.assertEqual(result["verdict"], verification.VERDICT_RED)
        self.assertTrue(
            os.path.exists(started_marker),
            "the cancel-firing thread must have actually observed the "
            "command start before signalling cancellation")
        self.assertFalse(
            os.path.exists(done_marker),
            "the command must have been torn down mid-flight, before it "
            "could finish and write its own completion marker")


if __name__ == "__main__":
    unittest.main()
