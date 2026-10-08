#!/usr/bin/env python3
"""Wiring tests for the review-loop correction packet, the review scope and the
prior-green binding at the builder gate.

Two halves:

- the builder gate of a profiled session: after a revise that moved only
  session artifacts, the gate binds the exact prior green owned transaction
  instead of running a new one, and every refusal runs the ordinary
  transaction (real owned transactions in a throwaway repository, as
  `test_execution_profile_flow` does);
- the review pass of a profiled session: from the second round the resumed
  paired reviewer gets a computed correction packet and a targeted prompt only
  when every closed condition holds, and the full prompt otherwise.

An unprofiled session and an assurance session are the neutral contrast
throughout: they take the unchanged paths. Fake runners replace the
reviewers; COWORK_SESSIONS_ROOT is a temp directory; inputs are synthetic.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_correction_wiring
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_authority_chain as authority_chain  # noqa: E402
import cowork_correction as correction_packets  # noqa: E402
import cowork_execution_profiles as profiles  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_verification as verification  # noqa: E402
import test_execution_profile_flow as flow  # noqa: E402

SAME = object()
HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_F = "f" * 64


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def write_review_file(path, content):
    """Mimic the reviewer's write: a dict is JSON, a str is raw text and None
    leaves no file."""
    if content is None:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w") as fh:
        if isinstance(content, str):
            fh.write(content)
        else:
            json.dump(content, fh)


def revise_with(*findings, **extra):
    verdict = {"verdict": "revise", "findings": ["prose summary"],
               "corrective_findings": list(findings)}
    verdict.update(extra)
    return verdict


def finding(summary="narrow the scope", severity="major", **extra):
    out = {"summary": summary, "severity": severity}
    out.update(extra)
    return out


APPROVE = {"verdict": "approve", "corrective_findings": []}


# --------------------------------------------------------------------------- #
# Builder gate: prior-green binding.                                           #
# --------------------------------------------------------------------------- #


class _GateCase(flow._RoleLoopCase):
    """Drives `_role_loop` as the builder with real owned transactions, a
    reviewer verdict file at a real review path and a spy on the transaction
    runner."""

    def setUp(self):
        super().setUp()
        self.review_path = os.path.join(
            os.path.dirname(self.status_path), "build-review.json")

    def write_verdict_file(self, content):
        write_review_file(self.review_path, content)

    def touch_session_artifact(self):
        with open(os.path.join(os.path.dirname(self.status_path),
                               "notes.txt"), "a") as fh:
            fh.write("session note\n")

    def plan_result(self):
        return {
            "batch": {"artifacts": ["docs/a.md", "docs/b.md"],
                      "derivatives": {"docs/a.md": ["docs/index.md"]}},
            "verification": [
                flow.check_entry("doc a", ["docs/a.md"],
                                 command=self.command("doc a")),
                flow.check_entry("doc b", ["docs/b.md"],
                                 command=self.command("doc b")),
                flow.check_entry("index", ["docs/index.md"],
                                 command=self.command("index")),
                flow.check_entry("unit", ["src/", "tests/"],
                                 kind="final_suite",
                                 command=self.command("unit"))],
            "verification_schema": 2}

    def unprofiled_session(self):
        """No profile: the plan sits where an ordinary session keeps it."""
        directory = os.path.dirname(self.status_path)
        with open(os.path.join(directory, "planner.plan.json"), "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": self.plan_result()}, fh)
        self.trace = trace_store.Trace(
            self.trace_path, session_uuid=self.suid, run_id="R")

    def drive_review(self, session, edits, verdicts, result_fn=None):
        """Like the flow tests' driver, with a review path. A verdict item is
        a dict (returned and written to the review file), a
        `(returned, file_content)` pair, or a callable producing either.
        `result_fn(send_number)`, when given, supplies the status `result`."""
        status_path = self.status_path
        sent = []
        case = self

        class FakeSession:
            def send(self_inner, text):
                sent.append(text)
                edit = edits.pop(0) if edits else None
                if edit is not None:
                    edit()
                result = result_fn(len(sent)) if result_fn else {}
                with open(status_path, "w") as fh:
                    json.dump({"session": "X", "role": "builder",
                               "status": "ready_for_review",
                               "result": result}, fh)

            def close(self_inner):
                pass

        reviewed = []

        def review_fn(_path, round_index):
            reviewed.append(round_index)
            item = verdicts.pop(0)
            if callable(item):
                item = item()
            returned, content = (item if isinstance(item, tuple)
                                 else (item, item))
            case.write_verdict_file(content)
            return returned

        rc, outcome, payload = cowork._role_loop(
            FakeSession(), "seed", status_path, context="",
            io_out=io.StringIO(), role="builder", review_fn=review_fn,
            trace=self.trace, reviewer_role=cowork.BUILD_REVIEWER,
            artifact_noun="build", phase="building",
            session_uuid=self.suid, profile_session=session,
            plan_json_path=self.plan_path if session is not None else None,
            review_path=self.review_path)
        self.sent, self.reviewed = sent, reviewed
        return rc, outcome, payload

    def drive_spied(self, session, edits, verdicts, result_fn=None):
        with mock.patch.object(
                verification, "run_transaction",
                wraps=verification.run_transaction) as spy:
            result = self.drive_review(session, edits, verdicts, result_fn)
        self.transaction_calls = spy.call_count
        return result

    def chain_finding_ids(self):
        read = authority_chain.read_chain(
            state_store.authority_chain_path_for(self.suid))
        return [r["id"] for r in read["records"] if r["kind"] == "finding"]

    def closing_recommendation(self):
        """A revise whose one entry recommends closing the first chain
        finding. It carries a valid severity because the prior-green binder
        refuses an entry without one."""
        return revise_with(finding(
            "fixed", "minor", finding_ref=self.chain_finding_ids()[0],
            closure="fixed"))

    def proposing_on_send(self, number):
        """A `result_fn`: the lead's status after send `number` proposes the
        closure of the first chain finding."""
        def result_fn(sent):
            if sent != number:
                return {}
            return {"resolution_proposals": [{
                "authority_ids": self.chain_finding_ids()[:1],
                "changed_evidence_paths": []}]}
        return result_fn

    def disposition_values(self, transaction_id):
        return [e.get("disposition")
                for e in self.events("verification.disposition")
                if e.get("transaction_id") == transaction_id]

    def pointer(self):
        return state_store.read_current_receipt_pointer(self.suid)

    def bound_events(self):
        return [e for e in self.events("verification.transaction")
                if e.get("bound_reuse") is True]

    def assert_ordinary_second_transaction(self):
        self.assertEqual(self.transaction_calls, 2)
        self.assertEqual(self.bound_events(), [])
        self.assertEqual(len(self.events("verification.transaction")), 2)


class ArtifactOnlyBindingFlowTests(_GateCase):
    def test_a_blocking_revise_then_an_artifact_only_fix_binds_the_prior_green(
            self):
        session = self.profile_session("standard")
        with mock.patch.object(
                session, "on_transaction",
                wraps=session.on_transaction) as on_transaction, \
                mock.patch.object(
                    session, "on_builder_ready",
                    wraps=session.on_builder_ready) as on_ready:
            # The blocking finding stays blocking, so the approval is only
            # possible once the control plane closes it: the lead proposes
            # the closure on its second send, a second review round
            # recommends it closed, and only then does the reviewer approve.
            rc, outcome, _payload = self.drive_spied(
                session,
                [None, self.touch_session_artifact,
                 self.touch_session_artifact],
                [revise_with(finding("section is unclear", "blocking")),
                 self.closing_recommendation, APPROVE],
                result_fn=self.proposing_on_send(2))
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(self.reviewed, [1, 2, 3])
        # The finding was closed by the control plane, through a resolution
        # that cites the lead's proposal and the round-2 recommendation.
        resolutions = [
            r for r in authority_chain.read_chain(
                state_store.authority_chain_path_for(self.suid))["records"]
            if r["kind"] == "resolution"]
        self.assertEqual([r["finding_id"] for r in resolutions],
                         self.chain_finding_ids())
        # One owned transaction ran: the second and third gates bound it.
        self.assertEqual(self.transaction_calls, 1)
        self.assertEqual(self.runs(),
                         ["doc a", "doc b", "index", "unit"])
        first, second, third = self.events("verification.transaction")
        self.assertNotIn("bound_reuse", first)
        for bound in (second, third):
            self.assertIs(bound["bound_reuse"], True)
            self.assertEqual(bound["transaction_id"],
                             first["transaction_id"])
            self.assertEqual(bound["bound_prior_transaction_id"],
                             first["transaction_id"])
            self.assertIs(bound["reused_lock_result"], False)
        self.assertEqual(len(self.bound_events()), 2)
        # The disposition is truthful: the blocking finding superseded the
        # transaction, and the rebind made it a real pending review again.
        self.assertEqual(
            self.disposition_values(first["transaction_id"]),
            ["superseded_by_finding", "pending_review", "accepted"])
        # No work ran, so the accounting did not move; promotion signals did
        # still get evaluated at every gate.
        self.assertEqual(on_transaction.call_count, 1)
        self.assertEqual(on_ready.call_count, 3)
        self.assertEqual(session.record["counters"],
                         {"verification_executed": 4,
                          "verification_reused": 0})
        self.assertEqual(session.effective, "standard")
        readiness = [e for e in self.events("role.readiness")
                     if e.get("state") == "verified"]
        self.assertEqual(
            [e["transaction_id"] for e in readiness],
            [first["transaction_id"]] * 3)
        pointer = self.pointer()
        self.assertEqual(
            pointer["inventory_identity"],
            cowork._gate_inventory_identity(self.suid, self.plan_path))
        self.assertEqual(len(pointer["inventory_identity"]), 64)

    def test_a_non_blocking_revise_leaves_the_pointer_pending_and_still_binds(
            self):
        session = self.profile_session("light")
        rc, outcome, _payload = self.drive_spied(
            session, [None, self.touch_session_artifact],
            [revise_with(finding("tidy a heading", "minor")), APPROVE])
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(self.transaction_calls, 1)
        first, second = self.events("verification.transaction")
        self.assertIs(second["bound_reuse"], True)
        self.assertEqual(self.disposition_values(first["transaction_id"]),
                         ["accepted"])
        self.assertEqual(session.effective, "light")

    def test_a_bound_result_still_needs_the_reviewer_to_approve(self):
        session = self.profile_session("standard")
        rc, outcome, payload = self.drive_spied(
            session, [None, self.touch_session_artifact],
            [revise_with(finding("section is unclear", "blocking")),
             {"verdict": "needs_user", "user_question": "which one?"}])
        self.assertEqual(self.transaction_calls, 1)
        self.assertEqual(len(self.bound_events()), 1)
        self.assertNotEqual(outcome, "approved")
        self.assertEqual(payload["kind"], "reviewer_question")
        self.assertEqual(self.reviewed, [1, 2])

    def test_an_approve_carrying_a_corrective_finding_is_still_rejected(self):
        session = self.profile_session("standard")
        rc, outcome, payload = self.drive_spied(
            session, [None, self.touch_session_artifact],
            [revise_with(finding("section is unclear", "blocking")),
             {"verdict": "approve",
              "corrective_findings": [finding("still wrong", "minor")]}])
        self.assertEqual(len(self.bound_events()), 1)
        self.assertEqual((rc, outcome), (0, "ended"))
        self.assertEqual(payload["kind"], "review_profile_rejected")


class FailClosedBindingWiringTests(_GateCase):
    def run_refusal(self, edit, first_verdict=None, profile="standard"):
        session = self.profile_session(profile)
        rc, outcome, _payload = self.drive_spied(
            session, [None, edit],
            [first_verdict
             or revise_with(finding("section is unclear", "major")),
             APPROVE])
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assert_ordinary_second_transaction()
        return session

    def test_a_repository_edit_runs_a_real_transaction(self):
        self.run_refusal(lambda: self.write("docs/a.md", "a edited\n"))
        first, second = self.events("verification.transaction")
        self.assertNotEqual(first["transaction_id"],
                            second["transaction_id"])

    def test_a_changed_inventory_runs_a_real_transaction(self):
        def edit():
            plan = read_json(self.plan_path)
            plan["result"]["verification"][0]["command"] = self.command(
                "doc a v2")
            with open(self.plan_path, "w") as fh:
                json.dump(plan, fh)
        self.run_refusal(edit)

    def test_a_validly_cited_challenge_runs_a_real_transaction(self):
        def challenge():
            transaction_id = self.pointer()["transaction_id"]
            return revise_with(finding(
                "the receipt is wrong", "major",
                verification_challenge={"transaction_id": transaction_id}))
        self.run_refusal(self.touch_session_artifact,
                         first_verdict=challenge)

    def test_an_absent_verdict_file_runs_a_real_transaction(self):
        self.run_refusal(
            self.touch_session_artifact,
            first_verdict=(revise_with(finding("x", "major")), None))

    def test_a_malformed_verdict_file_runs_a_real_transaction(self):
        self.run_refusal(
            self.touch_session_artifact,
            first_verdict=(revise_with(finding("x", "major")),
                           {"verdict": "maybe"}))

    def test_a_question_less_needs_user_file_runs_a_real_transaction(self):
        self.run_refusal(
            self.touch_session_artifact,
            first_verdict=(revise_with(finding("x", "major")),
                           {"verdict": "needs_user"}))

    def test_a_non_revise_verdict_file_runs_a_real_transaction(self):
        self.run_refusal(
            self.touch_session_artifact,
            first_verdict=(revise_with(finding("x", "major")), APPROVE))

    def test_a_pointer_without_an_identity_runs_a_real_transaction(self):
        def edit():
            path = state_store.current_receipt_pointer_path_for(self.suid)
            pointer = read_json(path)
            pointer.pop("inventory_identity")
            with open(path, "w") as fh:
                json.dump(pointer, fh)
        self.run_refusal(edit)


class UnprofiledAssuranceUnchangedTests(_GateCase):
    def assert_untouched(self):
        self.assert_ordinary_second_transaction()
        self.assertNotIn("inventory_identity", self.pointer())
        correction_dir = os.path.join(
            state_store.session_assets_dir(self.suid), "correction")
        self.assertFalse(os.path.exists(correction_dir))

    def test_an_unprofiled_session_never_attempts_binding(self):
        self.unprofiled_session()
        rc, outcome, _payload = self.drive_spied(
            None, [None, self.touch_session_artifact],
            [revise_with(finding("section is unclear", "major")),
             APPROVE])
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assert_untouched()

    def test_an_assurance_session_never_attempts_binding(self):
        session = self.profile_session("assurance")
        self.assertIsNone(session.reuse_policy())
        rc, outcome, _payload = self.drive_spied(
            session, [None, self.touch_session_artifact],
            [revise_with(finding("section is unclear", "major")),
             APPROVE])
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assert_untouched()
        self.assertEqual(session.record["counters"]["verification_reused"], 0)


class BindingRefusalHelperTests(_GateCase):
    """The binder helper over one prepared green transaction: each refusal
    returns its closed code, and none fabricates a result."""

    def prepare(self, profile="standard"):
        session = self.profile_session(profile)
        rc, _outcome, payload = self.drive_review(
            session, [None],
            [({"verdict": "needs_user", "user_question": "which one?"},
              revise_with(finding("section is unclear", "blocking")))])
        self.assertEqual(payload["kind"], "reviewer_question")
        self.transaction_id = self.pointer()["transaction_id"]
        return session

    def bind(self, session, identity="current"):
        if identity == "current":
            identity = cowork._gate_inventory_identity(
                self.suid, self.plan_path)
        return cowork._bind_prior_green_at_gate(
            self.suid, self.plan_path, session, self.review_path, identity,
            repo=self.repo)

    @contextlib.contextmanager
    def altered(self, path, mutate):
        with open(path) as fh:
            original = fh.read()
        document = json.loads(original)
        mutate(document)
        with open(path, "w") as fh:
            json.dump(document, fh)
        try:
            yield
        finally:
            with open(path, "w") as fh:
                fh.write(original)

    @contextlib.contextmanager
    def removed(self, path):
        with open(path, "rb") as fh:
            original = fh.read()
        os.remove(path)
        try:
            yield
        finally:
            with open(path, "wb") as fh:
                fh.write(original)

    def assert_refused(self, session, code, identity="current"):
        bound, refusal = self.bind(session, identity)
        self.assertIsNone(bound)
        self.assertEqual(refusal, code)

    def test_the_prepared_transaction_binds_by_id(self):
        session = self.prepare()
        bound, refusal = self.bind(session)
        self.assertIsNone(refusal)
        self.assertIs(bound["bound_reuse"], True)
        self.assertEqual(bound["transaction_id"], self.transaction_id)
        self.assertEqual(bound["bound_prior_transaction_id"],
                         self.transaction_id)
        self.assertIs(bound["reused_lock_result"], False)
        stored = read_json(state_store.verification_result_path_for(
            self.suid, self.transaction_id))
        self.assertNotIn("bound_reuse", stored)

    def test_a_prose_only_revise_binds(self):
        session = self.prepare()
        self.write_verdict_file({"verdict": "revise",
                                 "findings": ["prose only"]})
        self.assertIsNone(self.bind(session)[1])

    def test_an_untrusted_verdict_never_binds(self):
        session = self.prepare()
        cases = [
            ("absent", None),
            ("unparseable text", "{not json"),
            ("unknown verdict", {"verdict": "maybe"}),
            ("question-less needs_user", {"verdict": "needs_user"}),
            ("approve", APPROVE),
            ("findings not a list", {"verdict": "revise",
                                     "corrective_findings": "nope"}),
            ("finding not an entry", {"verdict": "revise",
                                      "corrective_findings": ["x"]}),
        ]
        for label, content in cases:
            with self.subTest(label):
                self.write_verdict_file(content)
                self.assert_refused(session, "verdict_unusable")

    def test_open_finding_classes_refuse_and_defeated_challenges_do_not(self):
        session = self.prepare()
        txn = self.transaction_id
        refusing = [
            ("validly cited blocking challenge",
             finding("c", "blocking",
                     verification_challenge={"transaction_id": txn})),
            ("validly cited non-blocking challenge",
             finding("c", "major",
                     verification_challenge={"transaction_id": txn})),
            ("architectural", finding("c", "blocking",
                                      risk_class="architectural")),
            ("malformed risk class", finding("c", "blocking",
                                             risk_class="weird")),
            ("unknown severity", finding("c", "critical")),
        ]
        for label, entry in refusing:
            with self.subTest(label):
                self.write_verdict_file(revise_with(entry))
                self.assert_refused(session, "verification_finding_open")
        binding = [
            ("uncited challenge",
             finding("c", "blocking", verification_challenge={})),
            ("contradicted challenge",
             finding("c", "blocking",
                     verification_challenge={"transaction_id": "T-other"})),
            ("challenge to another transaction",
             finding("c", "major",
                     verification_challenge={"transaction_id": "T-other"})),
        ]
        for label, entry in binding:
            with self.subTest(label):
                self.write_verdict_file(revise_with(entry))
                self.assertIsNone(self.bind(session)[1])

    def test_stale_foreign_or_torn_state_never_binds(self):
        session = self.prepare()
        self.write_verdict_file(revise_with(finding("c", "blocking")))
        pointer_path = state_store.current_receipt_pointer_path_for(self.suid)
        result_path = state_store.verification_result_path_for(
            self.suid, self.transaction_id)
        request_path = state_store.verification_request_path_for(
            self.suid, self.transaction_id)
        cases = [
            ("pointer without identity", pointer_path,
             lambda d: d.pop("inventory_identity"),
             "legacy_pointer_no_identity"),
            ("pointer manifest", pointer_path,
             lambda d: d.__setitem__("manifest_digest", HEX_F),
             "manifest_mismatch"),
            ("pointer index", pointer_path,
             lambda d: d.__setitem__("index_digest", HEX_F),
             "index_mismatch"),
            ("red prior", result_path,
             lambda d: d.__setitem__("verdict", "red"), "not_green"),
            ("deferred prior", result_path,
             lambda d: d.__setitem__("deferred_reconciliation",
                                     {"still_pending": ["x"]}),
             "deferred"),
            ("non-final binding", result_path,
             lambda d: d.__setitem__("final_suite_binding", "not_reached"),
             "binding_not_final"),
            ("foreign session request", request_path,
             lambda d: d.__setitem__("session_uuid", "S-other"),
             "session_mismatch"),
            ("foreign transaction request", request_path,
             lambda d: d.__setitem__("transaction_id", "T-other"),
             "request_mismatch"),
        ]
        for label, path, mutate, code in cases:
            with self.subTest(label), self.altered(path, mutate):
                self.assert_refused(session, code)
        with self.subTest("missing result"), self.removed(result_path):
            self.assert_refused(session, "result_unreadable")
        with self.subTest("missing request"), self.removed(request_path):
            self.assert_refused(session, "request_mismatch")
        with self.subTest("no identity"):
            self.assert_refused(session, "inventory_mismatch", identity=None)
        with self.subTest("foreign session id"):
            bound, refusal = cowork._bind_prior_green_at_gate(
                "S-other", self.plan_path, session, self.review_path,
                cowork._gate_inventory_identity(self.suid, self.plan_path),
                repo=self.repo)
            self.assertEqual((bound, refusal), (None, "no_pointer"))
        # Nothing above altered the prepared state for good.
        self.assertIsNone(self.bind(session)[1])

    def test_a_moved_repository_or_inventory_never_binds(self):
        session = self.prepare()
        self.write_verdict_file(revise_with(finding("c", "blocking")))
        self.write("docs/a.md", "a edited\n")
        self.assert_refused(session, "manifest_mismatch")
        self.write("docs/a.md", "a\n")
        self.assertIsNone(self.bind(session)[1])
        original = read_json(self.plan_path)
        changed = json.loads(json.dumps(original))
        changed["result"]["verification"][1]["command"] = self.command("b2")
        edits = [
            ("command", changed),
            ("dependency", None),
        ]
        for label, document in edits:
            with self.subTest(label):
                if document is None:
                    document = json.loads(json.dumps(original))
                    document["result"]["verification"][0]["depends_on"] = [
                        "docs/b.md"]
                with open(self.plan_path, "w") as fh:
                    json.dump(document, fh)
                try:
                    self.assert_refused(session, "inventory_mismatch")
                finally:
                    with open(self.plan_path, "w") as fh:
                        json.dump(original, fh)
        self.assertIsNone(self.bind(session)[1])

    def test_a_judged_disposition_never_binds(self):
        session = self.prepare()
        self.write_verdict_file(revise_with(finding("c", "blocking")))
        cowork._emit_verification_disposition(
            self.suid, self.trace, self.transaction_id,
            verification.DISPOSITION_SUPERSEDED_BY_FINDING)
        self.assertIsNone(self.bind(session)[1])
        for judged in (verification.DISPOSITION_REJECTED,
                       verification.DISPOSITION_ACCEPTED):
            with self.subTest(judged):
                cowork._emit_verification_disposition(
                    self.suid, self.trace, self.transaction_id, judged)
                self.assert_refused(session, "disposition_not_bindable")

    def test_only_a_profiled_light_or_standard_session_binds(self):
        session = self.prepare()
        self.write_verdict_file(revise_with(finding("c", "blocking")))
        assurance = profiles.ProfileSession(
            profiles.new_record("assurance", None, flow.NOW),
            lambda record: None, now_fn=lambda: flow.NOW)
        bound, refusal = self.bind(assurance)
        self.assertEqual((bound, refusal), (None, "reuse_mode_not_allowed"))
        bound, refusal = self.bind(None)
        self.assertEqual((bound, refusal), (None, "no_session"))


class InventoryIdentityTests(_GateCase):
    def identity(self):
        return cowork._gate_inventory_identity(self.suid, self.plan_path)

    def rewrite(self, mutate):
        document = read_json(self.plan_path)
        mutate(document["result"])
        with open(self.plan_path, "w") as fh:
            json.dump(document, fh)

    def test_the_identity_follows_the_inventory_and_nothing_else(self):
        self.profile_session("standard")
        base = self.identity()
        self.assertEqual(len(base), 64)
        self.assertEqual(self.identity(), base)

        def command(result):
            result["verification"][0]["command"] = self.command("other")

        def depends(result):
            result["verification"][0]["depends_on"] = ["docs/b.md"]

        def order(result):
            result["verification"][0:2] = result["verification"][1::-1]

        def extra(result):
            result["verification"].insert(
                1, flow.check_entry("extra", ["docs/a.md"]))

        def unrelated(result):
            result["batch"]["artifacts"] = ["docs/a.md"]

        for label, mutate in (("command", command), ("depends_on", depends),
                              ("order", order), ("extra entry", extra)):
            with self.subTest(label):
                self.profile_session("standard")
                self.rewrite(mutate)
                self.assertNotEqual(self.identity(), base)
        with self.subTest("a field outside the inventory"):
            self.profile_session("standard")
            self.rewrite(unrelated)
            self.assertEqual(self.identity(), base)

    def test_a_missing_or_invalid_inventory_has_no_identity(self):
        self.profile_session("standard")
        self.assertIsNone(
            cowork._gate_inventory_identity(self.suid, self.plan_path + ".x"))
        self.rewrite(lambda result: result.__setitem__("verification", []))
        self.assertIsNone(self.identity())

    def test_the_pointer_carries_the_identity_only_when_one_is_supplied(self):
        self.profile_session("standard")
        result = {"transaction_id": "T-synthetic", "verdict": "green",
                  "snapshot": {"manifest_digest": HEX_A,
                               "index_digest": HEX_B}}
        readiness = {"state": "verified"}
        pointer = cowork._update_receipt_pointer_for_readiness(
            self.suid, "builder", 1, None, result, readiness,
            self.status_path)
        self.assertNotIn("inventory_identity", pointer)
        pointer = cowork._update_receipt_pointer_for_readiness(
            self.suid, "builder", 1, None, result, readiness,
            self.status_path, inventory_identity=HEX_A)
        self.assertEqual(pointer["inventory_identity"], HEX_A)
        self.assertEqual(
            read_json(state_store.current_receipt_pointer_path_for(
                self.suid))["inventory_identity"], HEX_A)


# --------------------------------------------------------------------------- #
# Review pass: correction packet and review scope.                             #
# --------------------------------------------------------------------------- #

EDGES = {
    "scout": (cowork.SCOUT_REVIEWER, "scouting"),
    "planner": (cowork.PLANNING_ADVISOR, "planning"),
    "builder": (cowork.BUILD_REVIEWER, "building"),
}
INDEX_DIGEST = "b" * 64


def manifest_entry(char, mode="100644"):
    return {"type": "file", "sha256": char * 64, "mode": mode,
            "symlink_target": None}


class _ReviewCase(unittest.TestCase):
    def setUp(self):
        self.sessions_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.sessions_root,
                                              ignore_errors=True))
        patch = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.sessions_root})
        patch.start()
        self.addCleanup(patch.stop)

    def edge(self, kind, profile="standard", paths=True):
        return _Edge(self, kind, profile, paths)

    def assert_full_prompt(self, prompt):
        self.assertIn(handoff.FULL_REREAD_INSTRUCTION, prompt)
        self.assertNotIn(handoff.TARGETED_REREAD_NOTICE, prompt)

    def assert_targeted_prompt(self, prompt, packet_path):
        self.assertNotIn(handoff.FULL_REREAD_INSTRUCTION, prompt)
        self.assertIn(handoff.TARGETED_REREAD_NOTICE, prompt)
        self.assertIn(packet_path, prompt)
        self.assertIn("scope=targeted", prompt)


class _Edge:
    """One paired-reviewer edge (scout, planner or build) driven through the
    real `make_review_fn` with a fake runner that renders the resume prompt
    the real runner closures would render."""

    def __init__(self, case, kind, profile, paths):
        self.case, self.kind = case, kind
        self.role, self.phase = EDGES[kind]
        self.suid = "S-" + uuid.uuid4().hex[:8]
        self.assets = state_store.session_assets_dir(self.suid)
        os.makedirs(self.assets)
        self.review_path = os.path.join(self.assets, "review.json")
        names = (("scout.intel.json", "scout.intel.md") if kind == "scout"
                 else ("planner.plan.json", "planner.plan.md"))
        self.primary = os.path.join(self.assets, names[0])
        self.secondary = os.path.join(self.assets, names[1])
        self.status = os.path.join(self.assets, "builder.status.json")
        for path in (self.primary, self.secondary, self.status):
            with open(path, "w") as fh:
                fh.write("initial %s\n" % os.path.basename(path))
        self.artifact = self.status if kind == "builder" else self.primary
        self.queue = []
        self.seen = {}
        self.profile = None
        kwargs = {}
        if profile is not None:
            self.profile = profiles.ProfileSession(
                profiles.new_record(profile, None, flow.NOW),
                lambda record: None, now_fn=lambda: flow.NOW)
            kwargs["profile_session"] = self.profile
            if kind != "builder" and paths:
                kwargs["correction_artifact_paths"] = [
                    self.primary, self.secondary]
        self.review_fn = cowork.make_review_fn(
            {}, "context", [self.role], self.review_path,
            reviewer_runner=self._runner, reviewer_role=self.role,
            phase=self.phase, session_uuid=self.suid, **kwargs)

    # -- fake runner ------------------------------------------------------

    def _runner(self, config, context, selected, artifact_path, review_path,
                **kwargs):
        returned, content = self.queue.pop(0)
        self.seen["ctx"] = cowork._CORRECTION_CTX.get()
        self.seen["prompt"] = self.resume()
        if isinstance(returned, Exception):
            raise returned
        write_review_file(review_path, content)
        return returned

    def resume(self):
        if self.kind == "scout":
            return cowork.assemble_reviewer_resume_context(
                self.primary, self.secondary, assets_dir=self.assets)
        if self.kind == "planner":
            return cowork.assemble_advisor_resume_context(
                self.primary, self.secondary, assets_dir=self.assets)
        return cowork.assemble_build_reviewer_resume_context(
            self.primary, self.secondary, self.status, baseline_repos=[],
            assets_dir=self.assets)

    # -- driving ----------------------------------------------------------

    def run(self, round_index, returned, content=SAME,
            force_full_reread=False):
        self.queue.append((returned, returned if content is SAME
                           else content))
        self.seen = {}
        return self.review_fn(self.artifact, round_index,
                              force_full_reread=force_full_reread)

    def record_findings(self, verdict, round_index=1):
        return cowork._record_findings(
            self.suid, verdict, self.role, self.phase, round_index,
            self.review_path)

    def first_round(self, verdict=None):
        verdict = verdict or revise_with(finding())
        self.run(1, verdict)
        self.record_findings(verdict)
        return verdict

    def set_candidate(self, transaction_id, files):
        digest = verification.manifest_fingerprint(files)
        state_store.write_json_atomic(
            state_store.verification_snapshot_manifest_path_for(
                self.suid, transaction_id),
            {"manifest_digest": digest, "files": files})
        state_store.write_current_receipt_pointer(self.suid, {
            "transaction_id": transaction_id, "manifest_digest": digest,
            "index_digest": INDEX_DIGEST, "disposition": "pending_review",
            "verdict": "green"})

    def rewrite_secondary(self, text="edited\n"):
        with open(self.secondary, "w") as fh:
            fh.write(text)

    # -- reading ----------------------------------------------------------

    def baseline_prompt(self):
        return self.resume()

    def packet(self):
        return read_json(self.seen["ctx"]["packet_path"])

    def correction_files(self):
        directory = os.path.join(self.assets, "correction")
        if not os.path.isdir(directory):
            return []
        return sorted(n for n in os.listdir(directory)
                      if n.startswith("correction."))

    def reference_path(self):
        return cowork._reviewed_reference_path(
            self.suid, self.phase, self.role)

    def edit_reference(self, mutate):
        document = read_json(self.reference_path())
        mutate(document)
        with open(self.reference_path(), "w") as fh:
            json.dump(document, fh)


class _ScopedReviewCases:
    """The scoped-review flow, run once per paired-reviewer edge by the
    subclasses below (`KIND` names the edge)."""

    KIND = None

    def make_edge(self):
        edge = self.edge(self.KIND)
        if self.KIND == "builder":
            edge.set_candidate("T-one", {
                "docs/a.md": manifest_entry("1"),
                "docs/b.md": manifest_entry("2")})
        return edge

    def assert_ordinary(self, edge):
        self.assertIsNone(edge.seen["ctx"])
        self.assertEqual(edge.seen["prompt"], edge.baseline_prompt())
        self.assertEqual(edge.correction_files(), [])

    def assert_packet_scope(self, edge, scope, reason):
        packet = edge.packet()
        self.assertEqual(correction_packets.validate_packet(packet),
                         (True, None))
        self.assertEqual(packet["review_scope"],
                         {"scope": scope, "reason_code": reason})
        return packet

    def test_the_first_round_is_full_and_records_what_it_judged(self):
        for edge in [self.make_edge()]:
            edge.first_round()
            self.assert_ordinary(edge)
            self.assertTrue(os.path.exists(edge.reference_path()))

    def test_an_unchanged_candidate_is_reviewed_targeted(self):
        for edge in [self.make_edge()]:
            edge.first_round()
            verdict = edge.run(2, APPROVE)
            self.assertEqual(verdict["verdict"], "approve")
            packet = self.assert_packet_scope(
                edge, "targeted", "targeted_ok")
            self.assertEqual(packet["risk_class"], "artifact_only")
            self.assertEqual(packet["outcome"], "pending")
            self.assertEqual(packet["round"], 2)
            self.assertEqual(packet["role"], edge.role)
            self.assertEqual(
                edge.seen["ctx"]["facts"],
                {"correction_kind": "correction",
                 "correction_scope": "targeted",
                 "correction_finding_count": 1,
                 "correction_max_severity": "major"})
            self.assert_targeted_prompt(edge.seen["prompt"],
                                        edge.seen["ctx"]["packet_path"])
            self.assertIn("findings=1 max_severity=major",
                          edge.seen["prompt"])
            self.assertIsNone(cowork._CORRECTION_CTX.get())

    def test_a_changed_artifact_is_still_targeted_and_scoped(self):
        for edge in [self.make_edge()]:
            edge.first_round()
            if edge.kind == "builder":
                edge.set_candidate("T-two", {
                    "docs/a.md": manifest_entry("3"),
                    "docs/b.md": manifest_entry("2")})
            else:
                edge.rewrite_secondary()
            edge.run(2, APPROVE)
            packet = self.assert_packet_scope(
                edge, "targeted", "targeted_ok")
            self.assertEqual(packet["risk_class"], "focused_code")
            self.assert_targeted_prompt(edge.seen["prompt"],
                                        edge.seen["ctx"]["packet_path"])
            if edge.kind == "builder":
                self.assertEqual(edge.seen["ctx"]["changed_paths"],
                                 ["docs/a.md"])
                self.assertIn("- docs/a.md", edge.seen["prompt"])
                self.assertEqual(packet["links"]["verification_transaction_id"],
                                 "T-two")
            else:
                self.assertEqual(edge.seen["ctx"]["changed_paths"], None)

    def test_an_executable_build_delta_is_reviewed_in_full(self):
        if self.KIND != "builder":
            self.skipTest("a build-edge delta")
        edge = self.edge("builder")
        edge.set_candidate("T-one", {"docs/a.md": manifest_entry("1")})
        edge.first_round()
        edge.set_candidate("T-two", {"docs/a.md": manifest_entry("1"),
                                     "src/new.py": manifest_entry("4")})
        edge.run(2, APPROVE)
        self.assert_packet_scope(edge, "full", "executable_delta_full")
        self.assert_full_prompt(edge.seen["prompt"])
        self.assertIn("scope=full", edge.seen["prompt"])
        self.assertNotIn("- src/new.py", edge.seen["prompt"])

    def test_a_forced_full_reread_and_round_one_carry_no_packet(self):
        for edge in [self.make_edge()]:
            edge.first_round()
            edge.run(2, APPROVE, force_full_reread=True)
            self.assert_ordinary(edge)
            self.assertIsNone(cowork._CORRECTION_CTX.get())

    def test_the_next_pass_without_a_packet_renders_the_ordinary_prompt(self):
        for edge in [self.make_edge()]:
            edge.first_round()
            edge.run(2, revise_with(finding()))
            self.assertIsNotNone(edge.seen["ctx"])
            edge.run(3, APPROVE, force_full_reread=True)
            self.assert_ordinary_after_packet(edge)

    def assert_ordinary_after_packet(self, edge):
        self.assertIsNone(edge.seen["ctx"])
        self.assertEqual(edge.seen["prompt"], edge.baseline_prompt())

    def test_a_missing_reference_resolves_to_full(self):
        for edge in [self.make_edge()]:
            edge.first_round()
            os.remove(edge.reference_path())
            edge.run(2, APPROVE)
            self.assert_packet_scope(edge, "full", "malformed_signal_full")
            self.assert_full_prompt(edge.seen["prompt"])
            self.assertIn(edge.seen["ctx"]["packet_path"],
                          edge.seen["prompt"])

    def test_a_tampered_reference_resolves_to_full(self):
        def edited_map(document):
            first = sorted(document["artifacts"])[0]
            document["artifacts"][first] = HEX_F

        def deleted_digest(document):
            document["artifacts_digest"] = None

        def null_map(document):
            document["artifacts"] = None

        def edited_transaction(document):
            document["transaction"]["manifest_digest"] = HEX_F

        scenarios = [
            ("scout", edited_map, "prior_ref_mismatch_full"),
            ("planner", edited_map, "prior_ref_mismatch_full"),
            ("scout", deleted_digest, "prior_ref_missing_full"),
            ("planner", deleted_digest, "prior_ref_missing_full"),
            ("scout", null_map, "malformed_signal_full"),
            ("builder", edited_transaction, "prior_ref_mismatch_full"),
        ]
        for kind, mutate, reason in scenarios:
            if kind != self.KIND:
                continue
            with self.subTest("%s %s" % (kind, mutate.__name__)):
                edge = self.make_edge()
                edge.first_round()
                edge.edit_reference(mutate)
                edge.run(2, APPROVE)
                self.assert_packet_scope(edge, "full", reason)
                self.assert_full_prompt(edge.seen["prompt"])

    def test_an_unmeasurable_delta_resolves_to_full(self):
        # A build with no current receipt, an artifact that vanished, and an
        # edge that was never given its artifact list all have no measurable
        # delta.
        variants = [("no receipt or artifact list", False, False)]
        if self.KIND != "builder":
            variants = [("artifact vanished", True, True),
                        ("no artifact list", False, False)]
        for label, with_paths, vanish in variants:
            with self.subTest(label):
                edge = self.edge(self.KIND, paths=with_paths)
                edge.first_round()
                if vanish:
                    os.remove(edge.secondary)
                edge.run(2, APPROVE)
                packet = self.assert_packet_scope(
                    edge, "full", "malformed_signal_full")
                self.assertEqual(packet["risk_class"], "architectural")
                self.assert_full_prompt(edge.seen["prompt"])

    def test_a_typed_architectural_or_malformed_finding_resolves_to_full(self):
        for risk_class, reason in (("architectural", "architectural_full"),
                                   ("sideways", "malformed_signal_full")):
            for edge in [self.make_edge()]:
                with self.subTest(risk_class):
                    edge.first_round(revise_with(
                        finding(risk_class=risk_class)))
                    edge.run(2, APPROVE)
                    self.assert_packet_scope(edge, "full", reason)
                    self.assert_full_prompt(edge.seen["prompt"])

    def test_a_minor_finding_is_outside_the_revise_threshold(self):
        for edge in [self.make_edge()]:
            edge.first_round(revise_with(finding("tidy", "minor")))
            edge.run(2, APPROVE)
            self.assert_packet_scope(edge, "full", "severity_threshold_full")
            self.assert_full_prompt(edge.seen["prompt"])

    def test_assurance_and_unprofiled_sessions_get_no_packet(self):
        for profile in ("assurance", None):
            with self.subTest(str(profile)):
                edge = self.edge(self.KIND, profile=profile)
                edge.first_round()
                edge.run(2, APPROVE)
                self.assert_ordinary(edge)
                self.assertFalse(os.path.exists(edge.reference_path()))
                self.assertFalse(os.path.exists(
                    os.path.join(edge.assets, "correction")))

    def test_findings_that_cannot_be_joined_to_the_ledger_yield_no_packet(
            self):
        for edge in [self.make_edge()]:
            edge.run(1, revise_with(finding()))
            # Nothing was ever ledgered for the round.
            edge.run(2, APPROVE)
            self.assert_ordinary(edge)
        for edge in [self.make_edge()]:
            edge.run(1, revise_with(finding()))
            ledger.append_finding(
                state_store.ledger_path_for(edge.suid),
                summary="a different finding", severity="major",
                discoverer=edge.role, round_index=1, phase=edge.phase)
            edge.run(2, APPROVE)
            self.assert_ordinary(edge)
        for edge in [self.make_edge()]:
            verdict = revise_with(finding(), finding("second", "major"))
            edge.run(1, verdict)
            ledger.append_finding(
                state_store.ledger_path_for(edge.suid),
                summary="narrow the scope", severity="major",
                discoverer=edge.role, round_index=1, phase=edge.phase)
            edge.run(2, APPROVE)
            self.assert_ordinary(edge)

    def test_a_verdict_without_typed_findings_yields_no_packet(self):
        previous = [
            ("prose only", {"verdict": "revise", "findings": ["prose"]}, SAME),
            ("empty typed list", revise_with(), SAME),
            ("malformed file", revise_with(finding()),
             {"verdict": "maybe"}),
            ("non-revise file", revise_with(finding()), APPROVE),
            ("absent file", revise_with(finding()), None),
        ]
        for label, returned, content in previous:
            for edge in [self.make_edge()]:
                with self.subTest(label):
                    edge.run(1, returned, content)
                    edge.record_findings(returned)
                    edge.run(2, APPROVE)
                    self.assert_ordinary(edge)

    def test_a_packet_that_cannot_be_written_yields_the_ordinary_prompt(self):
        real = state_store.write_json_atomic

        def refuse_packets(path, data):
            if os.path.basename(path).startswith("correction."):
                return False
            return real(path, data)

        for edge in [self.make_edge()]:
            edge.first_round()
            with mock.patch.object(state_store, "write_json_atomic",
                                   side_effect=refuse_packets):
                edge.run(2, APPROVE)
            self.assert_ordinary(edge)

    def test_the_in_flight_value_is_reset_after_every_pass(self):
        edge = self.edge("scout")
        edge.first_round()
        with self.assertRaises(RuntimeError):
            edge.run(2, RuntimeError("runner failed"))
        self.assertIsNotNone(edge.seen["ctx"])
        self.assertIsNone(cowork._CORRECTION_CTX.get())
        edge.run(3, APPROVE)
        self.assertIsNone(cowork._CORRECTION_CTX.get())

    def test_a_failed_pass_does_not_replace_the_recorded_reference(self):
        edge = self.edge("scout")
        edge.first_round()
        before = read_json(edge.reference_path())
        edge.rewrite_secondary()
        edge.run(2, {"verdict": "maybe"}, {"verdict": "maybe"})
        self.assertEqual(read_json(edge.reference_path()), before)


class ScopedReviewFlowScoutTests(_ScopedReviewCases, _ReviewCase):
    KIND = "scout"


class ScopedReviewFlowPlannerTests(_ScopedReviewCases, _ReviewCase):
    KIND = "planner"


class ScopedReviewFlowBuilderTests(_ScopedReviewCases, _ReviewCase):
    KIND = "builder"


class AssembleCorrectionKeywordTests(_ReviewCase):
    FACTS = {"correction_kind": "correction", "correction_scope": "targeted",
             "correction_finding_count": 2, "correction_max_severity": "major"}

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.directory,
                                              ignore_errors=True))
        self.paths = {}
        for name in ("intel.json", "intel.md", "plan.json", "plan.md",
                     "status.json", "packet.json"):
            self.paths[name] = os.path.join(self.directory, name)
            with open(self.paths[name], "w") as fh:
                fh.write("{}\n")

    def correction(self, scope="targeted", **extra):
        value = {"packet_path": self.paths["packet.json"],
                 "facts": dict(self.FACTS, correction_scope=scope)}
        value.update(extra)
        return value

    def scout(self, **kwargs):
        return cowork.assemble_reviewer_resume_context(
            self.paths["intel.json"], self.paths["intel.md"], **kwargs)

    def advisor(self, **kwargs):
        return cowork.assemble_advisor_resume_context(
            self.paths["plan.json"], self.paths["plan.md"], **kwargs)

    def builder(self, **kwargs):
        return cowork.assemble_build_reviewer_resume_context(
            self.paths["plan.json"], self.paths["plan.md"],
            self.paths["status.json"], baseline_repos=[],
            assets_dir=self.directory, **kwargs)

    def assemblers(self):
        return (("scout", self.scout), ("planner", self.advisor),
                ("builder", self.builder))

    def test_without_a_correction_the_prompt_is_the_ordinary_one(self):
        expected_scout = handoff.render_handoff(
            "scout->scout-reviewer:review_resume",
            artifacts=cowork._intel_artifacts(
                self.paths["intel.json"], self.paths["intel.md"]))
        self.assertEqual(self.scout(), expected_scout)
        self.assertEqual(self.scout(correction=None), expected_scout)
        expected_advisor = handoff.render_handoff(
            "planner->planning-advisor:review_resume",
            artifacts=cowork._plan_artifacts(
                self.paths["plan.json"], self.paths["plan.md"]),
            facts={"team": []})
        self.assertEqual(self.advisor(), expected_advisor)

    def test_a_targeted_correction_names_the_packet_and_drops_the_reread(self):
        for label, assemble in self.assemblers():
            with self.subTest(label):
                prompt = assemble(correction=self.correction())
                self.assert_targeted_prompt(prompt,
                                            self.paths["packet.json"])
                self.assertIn("findings=2 max_severity=major", prompt)

    def test_a_full_correction_keeps_the_reread_and_the_facts(self):
        for label, assemble in self.assemblers():
            with self.subTest(label):
                prompt = assemble(correction=self.correction("full"))
                self.assert_full_prompt(prompt)
                self.assertIn(self.paths["packet.json"], prompt)
                self.assertIn("scope=full", prompt)

    def test_the_build_edge_scopes_the_recipe_only_for_a_targeted_scope(self):
        targeted = self.builder(correction=self.correction(
            changed_paths=["docs/a.md", "docs/b.md"]))
        self.assertIn("- docs/a.md", targeted)
        self.assertIn("- docs/b.md", targeted)
        full = self.builder(correction=self.correction(
            "full", changed_paths=["docs/a.md"]))
        self.assertNotIn("- docs/a.md", full)
        unscoped = self.builder(correction=self.correction())
        self.assertNotIn("- docs/a.md", unscoped)
        self.assert_targeted_prompt(unscoped, self.paths["packet.json"])

    def test_an_unusable_correction_is_ignored(self):
        baselines = {label: assemble() for label, assemble
                     in self.assemblers()}
        missing_fact = self.correction()
        del missing_fact["facts"]["correction_max_severity"]
        unusable = [
            ("relative path", self.correction(packet_path="packet.json")),
            ("missing fact", missing_fact),
            ("extra fact", self.correction(
                facts=dict(self.FACTS, team=["x"]))),
            ("unknown scope", self.correction("sideways")),
            ("not a dict", "targeted"),
        ]
        for name, value in unusable:
            for label, assemble in self.assemblers():
                with self.subTest("%s %s" % (name, label)):
                    self.assertEqual(assemble(correction=value),
                                     baselines[label])

    def test_the_in_flight_value_applies_only_to_its_own_role(self):
        baselines = {label: assemble() for label, assemble
                     in self.assemblers()}
        roles = {"scout": cowork.SCOUT_REVIEWER,
                 "planner": cowork.PLANNING_ADVISOR,
                 "builder": cowork.BUILD_REVIEWER}
        for owner, role in roles.items():
            token = cowork._CORRECTION_CTX.set(
                dict(self.correction(), role=role))
            try:
                for label, assemble in self.assemblers():
                    with self.subTest("%s value on %s" % (owner, label)):
                        prompt = assemble()
                        if label == owner:
                            self.assert_targeted_prompt(
                                prompt, self.paths["packet.json"])
                        else:
                            self.assertEqual(prompt, baselines[label])
            finally:
                cowork._CORRECTION_CTX.reset(token)
        self.assertIsNone(cowork._CORRECTION_CTX.get())


class PacketLineageWiringTests(_ReviewCase):
    def test_unverifiable_evidence_is_flagged_and_the_finding_kept(self):
        edge = self.edge("scout")
        real = os.path.join(edge.assets, "evidence.txt")
        with open(real, "w") as fh:
            fh.write("evidence\n")
        edge.first_round(revise_with(
            finding("first", "major",
                    evidence_path=os.path.join(edge.assets, "gone.txt"),
                    evidence_sha256=HEX_A),
            finding("second", "major", evidence_path=real,
                    evidence_sha256=HEX_B),
            finding("third", "major")))
        edge.run(2, APPROVE)
        states = [e["evidence_state"] for e in edge.packet()["findings"]]
        self.assertEqual(states, ["missing", "sha_mismatch", "missing"])
        self.assertEqual(edge.seen["ctx"]["facts"]["correction_finding_count"],
                         3)

    def test_ids_and_lineage_are_carried_verbatim_and_none_is_minted(self):
        edge = self.edge("planner")
        edge.run(1, revise_with(finding("lineage", "blocking")))
        path = state_store.ledger_path_for(edge.suid)
        record = ledger.append_finding(
            path, summary="lineage", severity="blocking",
            discoverer=edge.role, round_index=1, phase=edge.phase,
            source_finding_id="F-0007", source_session="S-prior")
        before = ledger.read_ledger(path)
        edge.run(2, APPROVE)
        self.assertEqual(ledger.read_ledger(path), before)
        entry, = edge.packet()["findings"]
        self.assertEqual(entry["finding_id"], record["id"])
        self.assertEqual(entry["source_finding_id"], "F-0007")
        self.assertEqual(entry["source_session"], "S-prior")
        self.assertEqual(edge.packet()["risk_class"], "artifact_only")

    def test_a_ledgered_finding_without_lineage_keeps_it_unknown(self):
        edge = self.edge("scout")
        edge.first_round()
        edge.run(2, APPROVE)
        entry, = edge.packet()["findings"]
        self.assertIsNone(entry["source_finding_id"])
        self.assertIsNone(entry["source_session"])

    def test_a_packet_entry_is_never_closed_or_approved(self):
        edge = self.edge("scout")
        edge.first_round(revise_with(finding(), finding("second", "blocking")))
        edge.run(2, APPROVE)
        packet = edge.packet()
        self.assertEqual(len(packet["findings"]), 2)
        for entry in packet["findings"]:
            self.assertEqual(entry["state"], "open")
            for key in correction_packets.FORBIDDEN_PACKET_KEYS:
                self.assertNotIn(key, entry)
        for key in correction_packets.FORBIDDEN_PACKET_KEYS:
            self.assertNotIn(key, packet)
        self.assertEqual(edge.seen["ctx"]["facts"]["correction_max_severity"],
                         "blocking")


if __name__ == "__main__":
    unittest.main()
