#!/usr/bin/env python3
"""Focused permanent tests for execution-profile policy
(`cowork_execution_profiles`) and its per-entry evidence reuse inside the owned
verification transaction (`cowork_verification`).

Every input is neutral and synthetic: made-up documentation paths, throwaway
git repositories in temp directories and a pinned COWORK_SESSIONS_ROOT. Nothing
here asserts anything about this repository's own suite, a delivery receipt or
a point in time.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_execution_profiles
"""

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_execution_profiles as profiles  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

NOW = "2000-01-01T00:00:00Z"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _record(selected="light"):
    return profiles.new_record(selected, "neutral rationale", NOW)


def _session(selected="light", validator=None):
    saved = []
    events = []
    session = profiles.ProfileSession(
        _record(selected), saved.append,
        trace_fn=lambda name, **fields: events.append((name, fields)),
        now_fn=lambda: NOW)
    return session, saved, events


def _entry(label, depends_on=None, kind="baseline", command=None, **extra):
    entry = {"label": label,
             "command": command or ["python3", "-c", "pass"],
             "execution_mode": "isolated_snapshot", "kind": kind}
    if depends_on is not None:
        entry["depends_on"] = list(depends_on)
    entry.update(extra)
    return entry


def _ok_validator(raw, schema):
    return None


def _bad_validator(raw, schema):
    raise ValueError("invalid inventory")


def _light_intel(**overrides):
    result = {
        "batch": {"artifacts": ["docs/a.md", "docs/b.md"],
                  "derivatives": {"docs/a.md": ["docs/index.md"]}},
        "verification": [_entry("unit", kind="final_suite")],
        "verification_schema": 2,
    }
    result.update(overrides)
    return result


class PreviewTests(unittest.TestCase):
    REQUIRED_KEYS = ("preview_schema", "profile", "policy_version",
                     "policy_digest", "roles", "phases", "batch", "checks",
                     "review", "thresholds", "invalidation",
                     "required_checks", "concurrency", "promotion")

    def test_every_profile_previews_its_complete_policy(self):
        for name in profiles.PROFILES:
            preview = profiles.preview(name)
            for key in self.REQUIRED_KEYS:
                self.assertIn(key, preview, (name, key))
                self.assertTrue(preview[key] not in (None, [], {}, ""),
                                (name, key))
            self.assertEqual(preview["profile"], name)
            json.dumps(preview)

    def test_promotion_table_lists_the_ten_rows_in_order(self):
        triggers = profiles.preview("light")["promotion"]["triggers"]
        self.assertEqual(
            [t["reason_code"] for t in triggers],
            ["scope_expansion", "batch_undeclared", "executable_change",
             "source_conflict", "evidence_failed", "material_finding",
             "architectural_risk", "repeated_rejection", "explicit_request",
             "signal_malformed"])
        for trigger in triggers:
            for key in ("trigger", "signal", "seam", "target", "reason_code"):
                self.assertTrue(trigger[key])

    def test_concurrency_contract_is_serial_for_every_profile(self):
        for name in profiles.PROFILES:
            self.assertEqual(
                profiles.preview(name)["concurrency"],
                {"contract_version": 1, "mode": "serial",
                 "max_parallel_vertices": 1, "fan_out_shapes": []})
            self.assertEqual(profiles.concurrency_contract(name),
                             profiles.preview(name)["concurrency"])

    def test_unknown_profile_is_refused(self):
        with self.assertRaises(profiles.UnknownProfile):
            profiles.preview("nope")
        with self.assertRaises(profiles.UnknownProfile):
            profiles.team_for("nope")

    def test_topology_per_profile(self):
        self.assertEqual(profiles.team_for("light"),
                         ["scout", "scout-reviewer", "builder",
                          "build-reviewer"])
        for name in ("standard", "assurance"):
            self.assertEqual(
                profiles.team_for(name),
                ["scout", "scout-reviewer", "planner", "planning-advisor",
                 "builder", "build-reviewer"])

    def test_digest_is_stable_and_tracks_the_policy(self):
        first = profiles.policy_digest()
        self.assertEqual(first, profiles.policy_digest())
        with mock.patch.object(profiles, "POLICY_VERSION", 99):
            self.assertNotEqual(first, profiles.policy_digest())

    def test_policy_is_frozen_and_previews_are_independent_copies(self):
        with self.assertRaises(TypeError):
            profiles.PROFILE_DEFINITIONS["light"]["roles"] = ()
        with self.assertRaises(TypeError):
            profiles.PROFILE_DEFINITIONS["light"]["batch"]["x"] = 1
        preview = profiles.preview("light")
        preview["roles"].append("extra")
        self.assertNotIn("extra", profiles.preview("light")["roles"])


class PromotionTableTests(unittest.TestCase):
    TARGETS = {
        "batch_undeclared": "standard", "executable_change": "standard",
        "source_conflict": "standard", "evidence_failed": "standard",
        "material_finding": "standard", "architectural_risk": "assurance",
        "repeated_rejection": "assurance", "signal_malformed": "assurance"}

    def _expected(self, start, code):
        rank = profiles.RANK
        if code == "scope_expansion":
            return profiles.PROFILES[min(rank[start] + 1, 2)]
        target = self.TARGETS[code]
        return profiles.PROFILES[max(rank[start], rank[target])]

    def test_every_row_from_every_start_gives_the_tabled_target(self):
        codes = ["scope_expansion"] + sorted(self.TARGETS)
        for start in profiles.PROFILES:
            for code in codes:
                first = profiles.promote(start, [code])
                second = profiles.promote(start, [code])
                self.assertEqual(first, second)
                self.assertEqual(first[0], self._expected(start, code),
                                 (start, code))
                self.assertEqual(first[1], [code])

    def test_explicit_request_targets_the_requested_profile(self):
        for start in profiles.PROFILES:
            for requested in profiles.PROFILES:
                effective, matched = profiles.promote(
                    start, ["explicit_request"], requested=requested)
                self.assertEqual(
                    effective,
                    profiles.PROFILES[max(profiles.RANK[start],
                                          profiles.RANK[requested])])
                self.assertEqual(matched, ["explicit_request"])

    def test_signal_order_never_changes_the_outcome(self):
        codes = ["architectural_risk", "scope_expansion", "source_conflict"]
        expected = profiles.promote("light", codes)
        for permutation in itertools.permutations(codes):
            self.assertEqual(profiles.promote("light", list(permutation)),
                             expected)
        # Matched codes come back in table order, not signal order.
        self.assertEqual(expected[1],
                         ["scope_expansion", "source_conflict",
                          "architectural_risk"])

    def test_nothing_ever_lowers_the_profile(self):
        for start in profiles.PROFILES:
            for code in list(self.TARGETS) + ["scope_expansion"]:
                effective, _ = profiles.promote(start, [code])
                self.assertGreaterEqual(profiles.RANK[effective],
                                        profiles.RANK[start])
        effective, _ = profiles.promote(
            "assurance", ["explicit_request"], requested="light")
        self.assertEqual(effective, "assurance")

    def test_an_unknown_reason_code_is_refused(self):
        with self.assertRaises(ValueError):
            profiles.promote("light", ["not_a_trigger"])

    def test_apply_promotion_records_one_entry_and_touches_nothing_else(self):
        record = _record("light")
        record["batch"] = {"source": "intel", "artifacts": ["docs/a.md"],
                           "derivatives": {}, "digest": "d"}
        record["plan_source"] = "intel"
        record["building_baseline"] = {"building_epoch": 1,
                                       "manifest_fingerprint": "f"}
        promoted, changed = profiles.apply_promotion(
            record, ["executable_change"], "seam", NOW)
        self.assertTrue(changed)
        self.assertEqual(promoted["effective"], "standard")
        self.assertEqual(len(promoted["promotion_history"]), 1)
        entry = promoted["promotion_history"][0]
        self.assertEqual(
            (entry["seq"], entry["from"], entry["to"],
             entry["reason_codes"], entry["seam"]),
            (1, "light", "standard", ["executable_change"], "seam"))
        untouched = [k for k in record
                     if k not in ("effective", "promotion_history")]
        for key in untouched:
            self.assertEqual(promoted[key], record[key], key)
        again, changed_again = profiles.apply_promotion(
            promoted, ["executable_change"], "seam", NOW)
        self.assertFalse(changed_again)
        self.assertEqual(again, promoted)

    def test_session_promotion_validates_as_a_record(self):
        session, saved, events = _session("light")
        self.assertTrue(session.promote_explicit("assurance"))
        self.assertEqual(session.effective, "assurance")
        self.assertEqual(len(saved), 1)
        self.assertEqual(events[0][0], "profile.promotion")
        record, reason = profiles.validate_record(
            json.loads(json.dumps(session.record)),
            profiles.binding_for(session.record))
        self.assertIsNone(reason)
        self.assertEqual(record["promotion_history"][0]["reason_codes"],
                         ["explicit_request"])


class SignalExtractorTests(unittest.TestCase):
    def test_light_intel_without_a_batch_or_inventory_is_undeclared(self):
        self.assertEqual(
            profiles.intel_triggers("light", _light_intel(),
                                    inventory_validator=_ok_validator), [])
        no_batch = _light_intel()
        del no_batch["batch"]
        self.assertEqual(profiles.intel_triggers(
            "light", no_batch, inventory_validator=_ok_validator),
            ["batch_undeclared"])
        no_inventory = _light_intel()
        del no_inventory["verification"]
        self.assertEqual(profiles.intel_triggers(
            "light", no_inventory, inventory_validator=_ok_validator),
            ["batch_undeclared"])

    def test_malformed_signals_fail_closed(self):
        self.assertEqual(profiles.intel_triggers(
            "light", _light_intel(), inventory_validator=_bad_validator),
            ["signal_malformed"])
        for bad in ({"artifacts": []}, {"artifacts": ["/abs.md"]},
                    {"artifacts": ["../x.md"]},
                    {"artifacts": ["a.md"], "derivatives": {"b.md": []}},
                    {"artifacts": ["a.md", "a.md"]}, "text"):
            self.assertEqual(profiles.intel_triggers(
                "light", _light_intel(batch=bad),
                inventory_validator=_ok_validator),
                ["signal_malformed"], bad)
        for bad in ("x", [{"summary": "s"}], [{"summary": "", "sources": ["a"]}],
                    [{"summary": "s", "sources": []}]):
            self.assertEqual(profiles.intel_triggers(
                "light", _light_intel(source_conflicts=bad),
                inventory_validator=_ok_validator),
                ["signal_malformed"], bad)
        self.assertEqual(profiles.intel_triggers(
            "light", _light_intel(risk_class="other"),
            inventory_validator=_ok_validator), ["signal_malformed"])

    def test_cap_executable_conflict_and_risk_signals(self):
        many = _light_intel(batch={
            "artifacts": ["docs/%d.md" % i for i in range(9)]})
        self.assertEqual(profiles.intel_triggers(
            "light", many, inventory_validator=_ok_validator),
            ["scope_expansion"])
        eight = _light_intel(batch={
            "artifacts": ["docs/%d.md" % i for i in range(8)]})
        self.assertEqual(profiles.intel_triggers(
            "light", eight, inventory_validator=_ok_validator), [])
        code = _light_intel(batch={"artifacts": ["tools/run.py"]})
        self.assertEqual(profiles.intel_triggers(
            "light", code, inventory_validator=_ok_validator),
            ["executable_change"])
        conflict = _light_intel(source_conflicts=[
            {"summary": "two sources disagree", "sources": ["a", "b"]}])
        self.assertEqual(profiles.intel_triggers(
            "light", conflict, inventory_validator=_ok_validator),
            ["source_conflict"])
        self.assertEqual(profiles.intel_triggers(
            "light", _light_intel(source_conflicts=[]),
            inventory_validator=_ok_validator), [])
        self.assertEqual(profiles.intel_triggers(
            "light", _light_intel(risk_class="architectural"),
            inventory_validator=_ok_validator), ["architectural_risk"])

    def test_standard_intel_needs_no_batch(self):
        self.assertEqual(profiles.intel_triggers("standard", {}), [])
        self.assertEqual(profiles.intel_triggers(
            "standard", {"batch": {"artifacts": []}}), ["signal_malformed"])

    def test_builder_ready_signals(self):
        batch = {"source": "intel", "artifacts": ["docs/a.md"],
                 "derivatives": {"docs/a.md": ["docs/index.md"]},
                 "digest": "d"}
        green = {"verdict": "green"}

        def triggers(changed, executable=(), status=None, txn=green):
            return profiles.builder_ready_triggers(
                "light", batch, changed, list(executable), txn, [], status)

        self.assertEqual(triggers([]), [])
        self.assertEqual(triggers(["docs/a.md", "docs/index.md"]), [])
        self.assertEqual(triggers(["README.md"]), ["scope_expansion"])
        self.assertEqual(
            triggers(["tools/run.py"], ["tools/run.py"]),
            ["scope_expansion", "executable_change"])
        self.assertEqual(triggers(None), ["signal_malformed"])
        self.assertEqual(
            triggers(["scripts/__pycache__/x.cpython-39.pyc"]), [])
        self.assertEqual(
            triggers([], status={"source_conflicts": [
                {"summary": "s", "sources": ["a"]}]}), ["source_conflict"])
        self.assertEqual(
            triggers([], status={"source_conflicts": "bad"}),
            ["signal_malformed"])
        self.assertEqual(triggers([], txn={"verdict": "red"}),
                         ["evidence_failed"])
        # A mode flip on a documentation file is an executable change.
        self.assertEqual(triggers(["docs/a.md"], ["docs/a.md"]),
                         ["executable_change"])

    def test_a_missing_boundary_with_changes_is_malformed(self):
        self.assertEqual(profiles.builder_ready_triggers(
            "standard", None, ["a.md"], [], {"verdict": "green"}, [], None),
            ["signal_malformed"])

    def test_evidence_failed_rules(self):
        lint = _entry("lint", check_class="lint")
        unit = _entry("unit", kind="final_suite")
        inventory = [lint, unit]

        def attempt(label, exit_code=0, **extra):
            return dict({"label": label, "exit_code": exit_code,
                         "evidence_state": "present", "timed_out": False},
                        **extra)

        self.assertFalse(profiles.evidence_failed(
            {"verdict": "green"}, inventory))
        self.assertTrue(profiles.evidence_failed(
            {"verdict": "unverified"}, inventory))
        deferred = {"verdict": "unverified",
                    "deferred_reconciliation": {"state": "pending"}}
        self.assertFalse(profiles.evidence_failed(deferred, inventory))
        red_lint = {"verdict": "red", "attempts": [attempt("lint", 1)],
                    "mutation": None}
        self.assertFalse(profiles.evidence_failed(red_lint, inventory))
        red_unit = {"verdict": "red", "attempts": [attempt("unit", 1)]}
        self.assertTrue(profiles.evidence_failed(red_unit, inventory))
        both = {"verdict": "red",
                "attempts": [attempt("lint", 1), attempt("unit", 1)]}
        self.assertTrue(profiles.evidence_failed(both, inventory))
        mutated = dict(red_lint, mutation={"reason": "x"})
        self.assertTrue(profiles.evidence_failed(mutated, inventory))
        timed = {"verdict": "red",
                 "attempts": [attempt("lint", None, timed_out=True)]}
        self.assertFalse(profiles.evidence_failed(timed, inventory))
        self.assertTrue(profiles.evidence_failed(None, inventory))

    def test_verdict_signals(self):
        def verdict(*findings):
            return {"verdict": "revise", "corrective_findings": [
                dict(f) for f in findings]}

        self.assertEqual(profiles.verdict_triggers(
            "build-reviewer", verdict({"severity": "major"})),
            ["material_finding"])
        self.assertEqual(profiles.verdict_triggers(
            "build-reviewer", verdict({"severity": "minor"})), [])
        self.assertEqual(profiles.verdict_triggers(
            "planning-advisor", verdict({"severity": "blocking"})), [])
        self.assertEqual(profiles.verdict_triggers(
            "scout-reviewer",
            verdict({"severity": "minor", "risk_class": "architectural"})),
            ["architectural_risk"])
        self.assertEqual(profiles.verdict_triggers(
            "build-reviewer",
            verdict({"severity": "minor", "risk_class": "x"})),
            ["signal_malformed"])
        self.assertEqual(profiles.verdict_triggers("build-reviewer", {}), [])


class InvalidationTests(unittest.TestCase):
    ACCEPTED = {
        "doc a": {"depends_on": ["docs/a.md", "tools/check_doc.py"]},
        "doc b": {"depends_on": ["docs/b.md", "tools/check_doc.py"]},
        "index": {"depends_on": ["docs/index.md"]},
        "always": {"depends_on": []},
    }
    DERIVATIVES = {"docs/a.md": ["docs/index.md"]}

    def test_a_changed_artifact_invalidates_it_and_its_direct_derivatives(self):
        self.assertEqual(
            profiles.invalidated_evidence(
                ["docs/a.md"], self.ACCEPTED, self.DERIVATIVES, False),
            ["always", "doc a", "index"])

    def test_an_independent_artifact_invalidates_only_itself(self):
        self.assertEqual(
            profiles.invalidated_evidence(
                ["docs/b.md"], self.ACCEPTED, self.DERIVATIVES, False),
            ["always", "doc b"])

    def test_an_executable_change_invalidates_everything(self):
        self.assertEqual(
            profiles.invalidated_evidence(
                [], self.ACCEPTED, self.DERIVATIVES, True),
            ["always", "doc a", "doc b", "index"])

    def test_bytecode_never_invalidates(self):
        self.assertEqual(
            profiles.invalidated_evidence(
                ["pkg/__pycache__/x.pyc"], self.ACCEPTED, {}, False),
            ["always"])

    def test_dependency_patterns(self):
        self.assertTrue(profiles.dependency_matches("a/b.md", ["a/b.md"]))
        self.assertTrue(profiles.dependency_matches("src/x/y.py", ["src/"]))
        self.assertTrue(profiles.dependency_matches("docs/z.md", ["docs/*.md"]))
        self.assertFalse(profiles.dependency_matches("docs2/z.md", ["docs/"]))
        self.assertFalse(profiles.dependency_matches("a/b.md", ["a/c.md"]))


class ReusePolicyTests(unittest.TestCase):
    def setUp(self):
        self.entry = _entry("doc a", ["docs/a.md"])
        self.prior = dict(self.entry, dependency_digest="d1",
                          source_transaction_id="t1", manifest_digest="m")
        self.policy = profiles.ReusePolicy(
            "light", {"doc a": self.prior}, {})

    def may(self, entry=None, prior=None, changed=("docs/b.md",),
            executable=False, digest="d1", policy=None):
        return (policy or self.policy).may_reuse(
            entry or self.entry, prior or self.prior, list(changed),
            executable, digest)

    def test_reuse_requires_every_condition(self):
        self.assertTrue(self.may())
        self.assertFalse(self.may(digest="d2"))
        self.assertFalse(self.may(digest=None))
        self.assertFalse(self.may(executable=True))
        self.assertFalse(self.may(changed=["docs/a.md"]))
        self.assertFalse(self.may(entry=dict(self.entry, command=["x"])))
        self.assertFalse(self.may(entry=_entry("doc a")))
        self.assertFalse(self.may(
            entry=dict(self.entry, kind="preflight"),
            prior=dict(self.prior, kind="preflight")))

    def test_a_direct_derivative_of_a_changed_artifact_is_not_reused(self):
        policy = profiles.ReusePolicy(
            "light", {"doc a": self.prior}, {"docs/x.md": ["docs/a.md"]})
        self.assertFalse(self.may(changed=["docs/x.md"], policy=policy))
        self.assertTrue(self.may(changed=["docs/y.md"], policy=policy))

    def test_assurance_never_reuses(self):
        record = _record("assurance")
        record["accepted_evidence"] = {"doc a": self.prior}
        self.assertIsNone(profiles.reuse_policy_for(record))
        self.assertFalse(self.may(policy=profiles.ReusePolicy(
            "assurance", {"doc a": self.prior}, {})))
        self.assertIsNotNone(profiles.reuse_policy_for(_record("light")))
        self.assertIsNotNone(profiles.reuse_policy_for(_record("standard")))

    def test_prior_lookup_is_by_label(self):
        self.assertEqual(self.policy.prior_for(self.entry), self.prior)
        self.assertIsNone(self.policy.prior_for(_entry("other", ["x"])))


class VerdictClassificationTests(unittest.TestCase):
    NOTE = {"summary": "tidy a heading", "evidence_path": "/tmp/x",
            "evidence_sha256": SHA_A}

    def approve(self, **extra):
        return dict({"verdict": "approve", "corrective_findings": []}, **extra)

    def test_deferred_notes_ride_an_approve_under_light_and_standard(self):
        for name in ("light", "standard"):
            action, notes = profiles.classify_build_verdict(
                name, self.approve(deferred_minor_notes=[self.NOTE]))
            self.assertEqual(action, "approve")
            self.assertEqual(notes[0]["summary"], "tidy a heading")

    def test_assurance_refuses_deferred_notes(self):
        self.assertEqual(
            profiles.classify_build_verdict(
                "assurance", self.approve(deferred_minor_notes=[self.NOTE])),
            ("reject", "deferred_notes_refused"))

    def test_an_approve_with_a_corrective_finding_is_never_an_approval(self):
        for name in profiles.PROFILES:
            self.assertEqual(
                profiles.classify_build_verdict(
                    name, {"verdict": "approve", "corrective_findings": [
                        {"summary": "s", "severity": "minor"}]}),
                ("reject", "corrective_findings_on_approve"))

    def test_malformed_notes_are_rejected(self):
        for bad in ("text", [{"summary": ""}], [{"summary": "s",
                                                  "evidence_sha256": "zz"}],
                    ["not an object"],
                    [{"summary": "s", "evidence_path": 3}]):
            self.assertEqual(
                profiles.classify_build_verdict(
                    "light", self.approve(deferred_minor_notes=bad)),
                ("reject", "deferred_notes_malformed"), bad)

    def test_a_revise_always_passes_through(self):
        for name in profiles.PROFILES:
            self.assertEqual(
                profiles.classify_build_verdict(
                    name, {"verdict": "revise", "corrective_findings": [
                        {"summary": "s", "severity": "minor"}]}),
                ("pass", None))

    def test_a_plain_approve_carries_no_notes(self):
        self.assertEqual(
            profiles.classify_build_verdict("light", self.approve()),
            ("approve", []))

    def test_the_session_screen_applies_only_to_the_builder_phase(self):
        session, _saved, _events = _session("assurance")
        verdict = self.approve(deferred_minor_notes=[self.NOTE])
        self.assertEqual(session.screen_verdict("builder", verdict),
                         "deferred_notes_refused")
        self.assertIsNone(session.screen_verdict("scout", verdict))
        self.assertIsNone(session.screen_verdict("planner", verdict))

    def test_approved_notes_are_persisted_outside_the_findings(self):
        session, saved, events = _session("light")
        count = session.on_build_approved(
            self.approve(deferred_minor_notes=[self.NOTE]), 2, "manifest")
        self.assertEqual(count, 1)
        note = session.record["deferred_minor_notes"][0]
        self.assertEqual((note["review_round"], note["manifest_digest"]),
                         (2, "manifest"))
        self.assertTrue(saved)
        self.assertEqual(events[-1][0], "profile.deferred_notes")


class RecordValidationTests(unittest.TestCase):
    def check(self, mutate, binding_mutate=None):
        record = _record("light")
        binding = profiles.binding_for(record)
        mutate(record)
        if binding_mutate:
            binding_mutate(binding)
        return profiles.validate_record(record, binding)[1]

    def test_a_new_record_round_trips(self):
        record = _record("standard")
        again = json.loads(json.dumps(record))
        self.assertEqual(
            profiles.validate_record(again, profiles.binding_for(record)),
            (again, None))

    def test_each_invalid_shape_has_its_closed_reason(self):
        self.assertEqual(profiles.validate_record(None, {})[1],
                         "record_missing")
        self.assertEqual(profiles.validate_record("x", {})[1],
                         "record_unparseable")
        self.assertEqual(self.check(lambda r: r.pop("batch")),
                         "record_schema")
        self.assertEqual(self.check(lambda r: r.update(extra=1)),
                         "record_schema")
        self.assertEqual(self.check(lambda r: r.update(selected="nope")),
                         "record_schema")
        self.assertEqual(
            self.check(lambda r: None,
                       lambda b: b.update(selected="standard")),
            "binding_mismatch")
        self.assertEqual(
            self.check(lambda r: None, lambda b: b.pop("policy_digest")),
            "binding_mismatch")

        def forge(record):
            record["policy_digest"] = "0" * 64

        self.assertEqual(
            self.check(forge, lambda b: b.update(policy_digest="0" * 64)),
            "policy_digest_mismatch")

    def test_effective_below_selected_is_invalid(self):
        record = _record("assurance")
        record["effective"] = "light"
        self.assertEqual(
            profiles.validate_record(
                record, profiles.binding_for(_record("assurance")))[1],
            "effective_below_selected")

    def test_history_must_match_the_effective_profile(self):
        record = _record("light")
        binding = profiles.binding_for(record)
        record["effective"] = "standard"
        self.assertEqual(profiles.validate_record(record, binding)[1],
                         "history_inconsistent")
        record["promotion_history"] = [{
            "seq": 1, "from": "light", "to": "standard",
            "reason_codes": ["executable_change"], "seam": "s", "at": NOW}]
        self.assertIsNone(profiles.validate_record(record, binding)[1])
        record["promotion_history"][0]["from"] = "assurance"
        self.assertEqual(profiles.validate_record(record, binding)[1],
                         "history_inconsistent")

    def test_evidence_entries_are_validated(self):
        record = _record("light")
        binding = profiles.binding_for(record)
        record["accepted_evidence"] = {"x": {"label": "y"}}
        self.assertEqual(profiles.validate_record(record, binding)[1],
                         "record_schema")


class NonWeakeningTests(unittest.TestCase):
    def test_every_profile_requires_the_same_checks(self):
        for name in profiles.PROFILES:
            self.assertEqual(profiles.non_weakening_violations(name), [])
            preview = profiles.preview(name)
            self.assertEqual(
                preview["required_checks"],
                ["owned_verification_final_suite",
                 "paired_reviewer_approval", "user_declared_checks"])
            self.assertEqual(preview["checks"]["full"],
                             "final_suite_required_every_transaction")
            self.assertTrue(
                preview["review"]["approve_requires_zero_corrective_findings"])

    def test_every_phase_that_runs_keeps_its_paired_reviewer(self):
        for name in profiles.PROFILES:
            roles = profiles.preview(name)["roles"]
            for phase in profiles.preview(name)["phases"]:
                self.assertIn(phase["lead"], roles)
                self.assertIn(phase["reviewer"], roles)

    def test_the_per_vertex_policy_carries_the_required_checks(self):
        for name in profiles.PROFILES:
            for role in profiles.team_for(name):
                policy = profiles.resolved_vertex_policy(name, role)
                self.assertEqual(policy["profile"], name)
                self.assertEqual(policy["required_checks"],
                                 list(profiles.REQUIRED_CHECKS))
                self.assertEqual(policy["concurrency"]["mode"], "serial")
                self.assertIn(policy["phase"],
                              ("scouting", "planning", "building"))

    def test_a_weakened_definition_is_reported(self):
        weakened = profiles._freeze({"light": {
            "roles": ["scout", "builder"],
            "phases": [{"phase": "building", "lead": "builder",
                        "reviewer": None}],
            "checks": {"full": "optional"},
            "review": {"approve_requires_zero_corrective_findings": False},
            "required_checks": ["user_declared_checks"],
            "concurrency": {"mode": "parallel"}}})
        with mock.patch.object(profiles, "PROFILE_DEFINITIONS", weakened):
            problems = profiles.non_weakening_violations("light")
        self.assertIn("required_checks_differ", problems)
        self.assertIn("final_suite_not_required", problems)
        self.assertIn("approve_allows_corrective_findings", problems)
        self.assertIn("unpaired_phase:building", problems)
        self.assertIn("concurrency_not_serial", problems)


class InventoryFieldTests(unittest.TestCase):
    def normalize(self, entries):
        return verification.normalize_inventory(
            entries, declared_schema=2)

    def test_depends_on_and_check_class_are_normalized_and_copied(self):
        _schema, entries, final = self.normalize([
            _entry("lint", ["docs/a.md", "tools/", "docs/*.md"],
                   check_class="lint"),
            _entry("unit", ["src/"], kind="final_suite")])
        self.assertEqual(final, "unit")
        self.assertEqual(entries[0]["depends_on"],
                         ["docs/a.md", "tools/", "docs/*.md"])
        self.assertEqual(entries[0]["check_class"], "lint")
        self.assertNotIn("check_class", entries[1])

    def test_bad_values_raise_closed_codes(self):
        final = _entry("unit", kind="final_suite")
        for bad in ("docs/a.md", [], [3], ["/abs.md"], ["../x.md"],
                    ["a\\b.md"], [""], ["a"] * 65):
            with self.assertRaises(verification.InventoryError) as ctx:
                self.normalize([dict(_entry("a"), depends_on=bad), final])
            self.assertEqual(ctx.exception.code, "bad_depends_on", bad)
        with self.assertRaises(verification.InventoryError) as ctx:
            self.normalize([dict(_entry("a"), check_class="weird"), final])
        self.assertEqual(ctx.exception.code, "bad_check_class")

    def test_the_inventory_key_ignores_the_new_fields(self):
        plain = [_entry("a"), _entry("unit", kind="final_suite")]
        declared = [_entry("a", ["docs/a.md"], check_class="lint"),
                    _entry("unit", ["src/"], kind="final_suite")]
        key = lambda raw: verification.normalized_inventory_key(  # noqa: E731
            *self.normalize(raw)[:2])
        self.assertEqual(key(plain), key(declared))


class ManifestDiffTests(unittest.TestCase):
    @staticmethod
    def file(sha, mode="644", kind="file", target=None):
        return {"type": kind, "sha256": sha, "size": 1, "mode": mode,
                "symlink_target": target}

    def test_changes_are_found_on_content_type_mode_and_membership(self):
        base = {"a.md": self.file(SHA_A), "b.md": self.file(SHA_A),
                "c.md": self.file(SHA_A), "d.md": self.file(SHA_A),
                "gone.md": self.file(SHA_A)}
        new = {"a.md": self.file(SHA_B),                       # content
               "b.md": self.file(SHA_A, mode="755"),           # mode flip
               "c.md": self.file(None, kind="symlink", target="a.md"),
               "d.md": self.file(SHA_A),                       # unchanged
               "added.md": self.file(SHA_A)}
        changed, executable = profiles.diff_manifests(base, new)
        self.assertEqual(changed,
                         ["a.md", "added.md", "b.md", "c.md", "gone.md"])
        self.assertEqual(executable, ["b.md", "c.md"])

    def test_executable_paths_are_classified_by_the_closed_rule(self):
        changed, executable = profiles.diff_manifests(
            {"x.py": self.file(SHA_A), "notes.txt": self.file(SHA_A)},
            {"x.py": self.file(SHA_B), "notes.txt": self.file(SHA_B)})
        self.assertEqual(changed, ["notes.txt", "x.py"])
        self.assertEqual(executable, ["x.py"])

    def test_bytecode_byproducts_are_dropped(self):
        changed, executable = profiles.diff_manifests(
            {}, {"pkg/__pycache__/m.cpython-39.pyc": self.file(SHA_A),
                 "m.pyc": self.file(SHA_A)})
        self.assertEqual((changed, executable), ([], []))
        self.assertTrue(profiles.is_bytecode_byproduct("a/__pycache__/b"))
        self.assertTrue(profiles.is_bytecode_byproduct("a.pyo"))
        self.assertFalse(profiles.is_bytecode_byproduct("a.py"))

    def test_path_classification(self):
        for path in ("a.md", "A.MD", "x.markdown", "x.rst", "x.txt", "x.adoc"):
            self.assertEqual(profiles.classify_path(path), "documentation")
        for path in ("x.py", "x.json", "x.yaml", "Makefile", "x.png", "x"):
            self.assertEqual(profiles.classify_path(path), "executable")
        self.assertEqual(profiles.classify_path("a.md", "755"), "executable")
        self.assertEqual(profiles.classify_path("a.md", "644"),
                         "documentation")

    def test_plan_batch_resolution(self):
        batch, triggers = profiles.resolve_plan_batch(
            {"batch": {"artifacts": ["a.py"], "derivatives": {}}})
        self.assertEqual((batch["source"], batch["artifacts"], triggers),
                         ("plan", ["a.py"], []))
        batch, triggers = profiles.resolve_plan_batch({"implementation": [
            {"file": "a.py, b.py"}, {"file": "/repo/root/c.py"},
            {"file": "a.py"}]}, "/repo/root")
        self.assertEqual(
            (batch["source"], batch["artifacts"], triggers),
            ("plan_implementation_files", ["a.py", "b.py", "c.py"], []))
        for plan in ({}, {"implementation": []},
                     {"implementation": [{"file": 3}]},
                     {"implementation": [{"file": "a.py, ../b.py"}]},
                     {"batch": {"artifacts": []}}):
            batch, triggers = profiles.resolve_plan_batch(plan)
            self.assertEqual(triggers, ["signal_malformed"], plan)
            self.assertEqual(batch["source"], "plan_unresolved")
        batch, _ = profiles.resolve_plan_batch(
            {"implementation": [{"file": "a.py, ../b.py"}]})
        self.assertEqual(batch["artifacts"], ["a.py"])


class BatchPersistenceTests(unittest.TestCase):
    def test_intel_approval_records_the_batch_and_derivative_edges(self):
        session, saved, _events = _session("light")
        route = session.on_intel_approved(
            _light_intel(), inventory_validator=_ok_validator)
        self.assertEqual(route, "building")
        batch = session.record["batch"]
        self.assertEqual(batch["source"], "intel")
        self.assertEqual(batch["artifacts"], ["docs/a.md", "docs/b.md"])
        self.assertEqual(batch["cap"], 8)
        self.assertEqual(
            session.record["invalidation_graph"]["artifact_derivatives"],
            {"docs/a.md": ["docs/index.md"]})
        self.assertTrue(saved)
        self.assertEqual(session.effective, "light")

    def test_an_executable_intel_batch_promotes_and_routes_to_planning(self):
        session, _saved, events = _session("light")
        route = session.on_intel_approved(
            _light_intel(batch={"artifacts": ["tools/run.py"]}),
            inventory_validator=_ok_validator)
        self.assertEqual(route, "planning")
        self.assertEqual(session.effective, "standard")
        entry = session.record["promotion_history"][0]
        self.assertEqual((entry["reason_codes"], entry["seam"]),
                         (["executable_change"], "intel_approval"))
        self.assertEqual(session.record["batch"]["artifacts"],
                         ["tools/run.py"])
        self.assertIn("profile.promotion", [e[0] for e in events])

    def test_standard_intel_always_routes_to_planning(self):
        session, _saved, _events = _session("standard")
        self.assertEqual(session.on_intel_approved({}), "planning")
        self.assertEqual(session.effective, "standard")

    def test_plan_approval_replaces_the_provisional_batch(self):
        session, saved, _events = _session("standard")
        session.on_intel_approved(_light_intel())
        session.on_plan_approved({"implementation": [
            {"file": "a.py, b.py"}]}, "/repo")
        batch = session.record["batch"]
        self.assertEqual(
            (batch["source"], batch["artifacts"]),
            ("plan_implementation_files", ["a.py", "b.py"]))
        self.assertEqual(
            session.record["invalidation_graph"]["artifact_derivatives"], {})
        self.assertEqual(session.effective, "standard")

    def test_an_unresolvable_plan_boundary_promotes_to_assurance(self):
        session, _saved, _events = _session("standard")
        session.on_plan_approved({}, "/repo")
        self.assertEqual(session.record["batch"]["source"], "plan_unresolved")
        self.assertEqual(session.effective, "assurance")
        self.assertEqual(
            session.record["promotion_history"][0]["reason_codes"],
            ["signal_malformed"])

    def test_a_transaction_records_dependencies_and_accepted_evidence(self):
        session, saved, events = _session("light")
        inventory = [_entry("doc a", ["docs/a.md"]),
                     _entry("unit", kind="final_suite")]
        result = {
            "verdict": "green", "transaction_id": "t1",
            "snapshot": {"manifest_digest": "m1"},
            "attempts": [
                {"label": "doc a", "exit_code": 0,
                 "evidence_state": "present"},
                {"label": "unit", "exit_code": 0,
                 "evidence_state": "present"}],
            "dependency_digests": {"doc a": "d1"}}
        session.on_transaction(result, inventory)
        record = session.record
        self.assertEqual(
            record["invalidation_graph"]["entry_dependencies"],
            {"doc a": ["docs/a.md"], "unit": []})
        self.assertEqual(list(record["accepted_evidence"]), ["doc a"])
        evidence = record["accepted_evidence"]["doc a"]
        self.assertEqual(
            (evidence["source_transaction_id"], evidence["dependency_digest"],
             evidence["manifest_digest"]), ("t1", "d1", "m1"))
        self.assertEqual(record["counters"],
                         {"verification_executed": 2,
                          "verification_reused": 0})
        self.assertTrue(saved)
        self.assertEqual(events[-1][0], "profile.evidence")
        # A red transaction never records accepted evidence.
        session.on_transaction(
            dict(result, verdict="red", transaction_id="t2"), inventory)
        self.assertEqual(
            session.record["accepted_evidence"]["doc a"][
                "source_transaction_id"], "t1")

    def test_promotion_never_touches_batch_evidence_or_baseline(self):
        session, _saved, _events = _session("light")
        session.on_intel_approved(_light_intel(),
                                  inventory_validator=_ok_validator)
        session.set_plan_source("intel")
        session.set_building_baseline(1, "fingerprint")
        before = json.loads(json.dumps(session.record))
        session.on_verdict("scout-reviewer", {
            "verdict": "revise", "corrective_findings": [
                {"severity": "minor", "risk_class": "architectural"}]})
        self.assertEqual(session.effective, "assurance")
        for key in ("batch", "accepted_evidence", "invalidation_graph",
                    "plan_source", "building_baseline"):
            self.assertEqual(session.record[key], before[key], key)

    def test_baseline_bookkeeping_is_per_building_epoch(self):
        session, _saved, _events = _session("light")
        self.assertTrue(session.needs_building_baseline(1))
        session.set_building_baseline(1, "f")
        self.assertFalse(session.needs_building_baseline(1))
        self.assertTrue(session.needs_building_baseline(2))

    def test_the_round_cap_promotes_to_assurance(self):
        session, _saved, _events = _session("standard")
        session.on_round_cap("builder")
        self.assertEqual(session.effective, "assurance")
        self.assertEqual(
            session.record["promotion_history"][0]["reason_codes"],
            ["repeated_rejection"])

    def test_builder_ready_signals_promote_with_their_reasons(self):
        session, _saved, _events = _session("light")
        session.on_intel_approved(_light_intel(),
                                  inventory_validator=_ok_validator)
        session.on_builder_ready(["docs/a.md"], [], {"verdict": "green"},
                                 [], {})
        self.assertEqual(session.effective, "light")
        session.on_builder_ready(["tools/run.py"], ["tools/run.py"],
                                 {"verdict": "green"}, [], {})
        self.assertEqual(session.effective, "standard")
        self.assertEqual(
            session.record["promotion_history"][0]["reason_codes"],
            ["scope_expansion", "executable_change"])


class _RepoCase(unittest.TestCase):
    """A throwaway committed git repo seeded with the real worker modules,
    plus an isolated COWORK_SESSIONS_ROOT, so every transaction below runs
    the real snapshot, worker and process-group machinery."""

    WORKER_MODULES = ("cowork_verification.py", "cowork_state.py",
                      "cowork_policy.py", "cowork_ledger.py")

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
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        self._git("config", "commit.gpgsign", "false")
        for rel, text in (("docs/a.md", "a\n"), ("docs/b.md", "b\n"),
                          ("docs/index.md", "index\n"),
                          ("src/core.py", "VALUE = 1\n"),
                          ("tests/test_core.py", "VALUE = 1\n")):
            self.write(rel, text)
        scripts = os.path.join(self.repo, "scripts")
        os.makedirs(scripts)
        for name in self.WORKER_MODULES:
            shutil.copyfile(os.path.join(_HERE, name),
                            os.path.join(scripts, name))
        self._git("add", ".")
        self._git("commit", "-qm", "init")
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        fd, self.marker = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.marker)
                        and os.remove(self.marker))

    def _git(self, *args):
        subprocess.run(["git", "-C", self.repo] + list(args), check=True)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def command(self, label):
        # Appends the label to a marker file outside the snapshot: the
        # observable proof of whether, and how often, a check really ran.
        return ["python3", "-c",
                "open(%r, 'a').write(%r + chr(10))" % (self.marker, label)]

    def inventory(self):
        return [
            _entry("doc a", ["docs/a.md"], command=self.command("doc a")),
            _entry("doc b", ["docs/b.md"], command=self.command("doc b")),
            _entry("unit", ["src/", "tests/"], kind="final_suite",
                   command=self.command("unit"))]

    def runs(self):
        with open(self.marker) as fh:
            lines = fh.read().split("\n")
        return [line for line in lines if line]

    def transact(self, session=None, reuse=True):
        raw = self.inventory()
        kwargs = {}
        if reuse:
            kwargs["reuse_policy"] = session.reuse_policy()
        result = verification.run_transaction(
            self.repo, self.session_uuid, raw, **kwargs)
        if session is not None:
            session.on_transaction(
                result, verification.normalize_inventory(raw)[1])
        return result


class TransactionReuseTests(_RepoCase):
    def test_a_docs_only_edit_reuses_the_final_suite_and_untouched_checks(self):
        session, _saved, _events = _session("standard")
        first = self.transact(session)
        self.assertEqual(first["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(first["final_suite_binding"], "ran_once")
        self.assertEqual(first["evidence_reuse"], [])
        self.assertEqual(sorted(self.runs()), ["doc a", "doc b", "unit"])
        self.assertEqual(len(session.record["accepted_evidence"]), 3)

        self.write("docs/b.md", "b changed\n")
        second = self.transact(session)
        self.assertEqual(second["verdict"], verification.VERDICT_GREEN)
        self.assertEqual([a["label"] for a in second["attempts"]], ["doc b"])
        reused = {r["label"]: r for r in second["evidence_reuse"]}
        self.assertEqual(sorted(reused), ["doc a", "unit"])
        for row in reused.values():
            self.assertEqual(row["source_transaction_id"],
                             first["transaction_id"])
            self.assertTrue(row["dependency_digest"])
        self.assertEqual(second["final_suite_binding"],
                         "reused_dependency_bound")
        self.assertTrue(second["final_suite_reused"])
        # executed union reused is exactly the inventory.
        self.assertEqual(
            sorted([a["label"] for a in second["attempts"]] + list(reused)),
            ["doc a", "doc b", "unit"])
        self.assertEqual(sorted(self.runs()),
                         ["doc a", "doc b", "doc b", "unit"])
        self.assertEqual(
            session.record["counters"],
            {"verification_executed": 4, "verification_reused": 2})

    def test_without_a_reuse_policy_everything_reruns_and_nothing_is_stamped(
            self):
        first = self.transact(None, reuse=False)
        self.write("docs/b.md", "b changed\n")
        second = self.transact(None, reuse=False)
        self.assertEqual(len(first["attempts"]), 3)
        self.assertEqual(len(second["attempts"]), 3)
        for result in (first, second):
            self.assertNotIn("evidence_reuse", result)
            self.assertNotIn("final_suite_reused", result)
            self.assertEqual(result["final_suite_binding"], "ran_once")
        self.assertEqual(len(self.runs()), 6)

    def test_an_executable_change_reruns_every_entry(self):
        session, _saved, _events = _session("standard")
        self.transact(session)
        self.write("src/core.py", "VALUE = 2\n")
        second = self.transact(session)
        self.assertEqual(len(second["attempts"]), 3)
        self.assertEqual(second["evidence_reuse"], [])
        self.assertEqual(second["final_suite_binding"], "ran_once")

    def test_editing_one_artifact_reruns_only_its_own_check(self):
        session, _saved, _events = _session("standard")
        self.transact(session)
        self.write("docs/a.md", "a changed\n")
        second = self.transact(session)
        self.assertEqual([a["label"] for a in second["attempts"]], ["doc a"])
        self.assertEqual(
            sorted(r["label"] for r in second["evidence_reuse"]),
            ["doc b", "unit"])

    def test_a_declared_derivative_reruns_with_the_artifact_it_derives_from(
            self):
        session, _saved, _events = _session("standard")
        updated = dict(session.record)
        updated["invalidation_graph"] = {
            "artifact_derivatives": {"docs/a.md": ["docs/index.md"]},
            "entry_dependencies": {}}
        session = profiles.ProfileSession(
            updated, lambda record: None, now_fn=lambda: NOW)
        raw = self.inventory()
        raw.insert(2, _entry("index", ["docs/index.md"],
                             command=self.command("index")))
        entries = verification.normalize_inventory(raw)[1]
        first = verification.run_transaction(
            self.repo, self.session_uuid, raw,
            reuse_policy=session.reuse_policy())
        session.on_transaction(first, entries)
        self.write("docs/a.md", "a changed\n")
        second = verification.run_transaction(
            self.repo, self.session_uuid, raw,
            reuse_policy=session.reuse_policy())
        self.assertEqual(sorted(a["label"] for a in second["attempts"]),
                         ["doc a", "index"])
        self.assertEqual(
            sorted(r["label"] for r in second["evidence_reuse"]),
            ["doc b", "unit"])

    def test_an_all_reused_transaction_runs_no_worker(self):
        session, _saved, _events = _session("standard")
        first = self.transact(session)
        runs_before = self.runs()
        second = self.transact(session)
        self.assertEqual(second["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(second["attempts"], [])
        self.assertEqual(len(second["evidence_reuse"]), 3)
        self.assertEqual(second["final_suite_binding"],
                         "reused_dependency_bound")
        self.assertFalse(second["worker_identity_verified"])
        self.assertNotEqual(second["transaction_id"],
                            first["transaction_id"])
        self.assertEqual(self.runs(), runs_before)
        on_disk = state_store.read_json_tolerant(
            state_store.verification_result_path_for(
                self.session_uuid, second["transaction_id"]))
        self.assertEqual(on_disk["final_suite_binding"],
                         "reused_dependency_bound")

    def test_an_all_reused_transaction_is_red_when_the_tree_moved(self):
        session, _saved, _events = _session("standard")
        self.transact(session)
        with mock.patch.object(
                verification, "detect_mutation",
                side_effect=lambda *a, **k: {
                    "reason": "source_or_index_mutated",
                    "changed_paths": ["docs/a.md"]}):
            second = self.transact(session)
        self.assertEqual(second["verdict"], verification.VERDICT_RED)
        self.assertEqual(second["final_suite_binding"], "not_reached")

    def test_assurance_session_never_gets_a_reuse_policy(self):
        session, _saved, _events = _session("assurance")
        self.assertIsNone(session.reuse_policy())
        self.transact(session, reuse=False)
        self.write("docs/b.md", "b changed\n")
        second = self.transact(session, reuse=False)
        self.assertEqual(len(second["attempts"]), 3)

    def test_a_pattern_that_matches_nothing_is_never_reused(self):
        session, _saved, _events = _session("standard")
        raw = [_entry("ghost", ["docs/missing.md"],
                      command=self.command("ghost")),
               _entry("unit", ["src/"], kind="final_suite",
                      command=self.command("unit"))]
        entries = verification.normalize_inventory(raw)[1]
        first = verification.run_transaction(
            self.repo, self.session_uuid, raw,
            reuse_policy=session.reuse_policy())
        session.on_transaction(first, entries)
        second = verification.run_transaction(
            self.repo, self.session_uuid, raw,
            reuse_policy=session.reuse_policy())
        self.assertEqual([a["label"] for a in second["attempts"]], ["ghost"])
        self.assertEqual([r["label"] for r in second["evidence_reuse"]],
                         ["unit"])

    def test_the_green_binding_helper(self):
        helper = verification._green_final_suite_binding
        self.assertEqual(helper("unit", "not_reached", False), "ran_once")
        self.assertEqual(helper("unit", "not_reached", True),
                         "reused_dependency_bound")
        self.assertEqual(
            helper(verification.FINAL_SUITE_LEGACY_UNKNOWN, "not_reached",
                   True), "not_reached")


class DeferredReconciliationBindingTests(_RepoCase):
    """A transaction whose final suite was reused and whose only executed entry
    was deferred must reconcile to `reused_dependency_bound`, never to
    `ran_once`: the suite never ran inside it."""

    def test_reconciling_a_deferred_transaction_keeps_the_reused_binding(self):
        transaction_id = "txn-deferred"
        request_key = "key-deferred"
        request = {
            "transaction_id": transaction_id, "request_key": request_key,
            "final_suite_label": "unit", "final_suite_reused": True,
            "inventory": [{"label": "doc b", "ledger_attempt_id": "V-1"}]}
        state_store.write_json_atomic(
            state_store.verification_request_path_for(
                self.session_uuid, transaction_id), request)
        stored = verification.TransactionResult({
            "transaction_id": transaction_id, "request_key": request_key,
            "verdict": verification.VERDICT_UNVERIFIED,
            "final_suite_label": "unit", "final_suite_binding": "not_reached",
            "final_suite_reused": True,
            "attempts": [{"label": "doc b", "exit_code": None,
                          "evidence_state": "unresolved",
                          "timed_out": False, "ledger_attempt_id": "V-1"}],
            "mutation": None, "worker_identity_verified": True,
            "ledger_failure": None, "startup_failure": None,
            "snapshot": {"manifest_digest": "m", "index_digest": "i"},
            "deferred_reconciliation": {
                "state": "pending", "transaction_id": transaction_id,
                "still_pending": ["doc b"], "deadline_hit": False,
                "next_action": "x"}})
        terminal = {"attempt_state": "terminal", "exit_code": 0,
                    "evidence_state": "present", "timed_out": False,
                    "wall_time_s": 0.1, "exit_status": "pass",
                    "adjudication": "pass", "reconciled_at": "t"}
        with mock.patch.object(
                verification, "reconcile_pending_evidence",
                return_value={"still_pending": []}), \
                mock.patch.object(
                    verification, "_read_deferred_marker",
                    return_value={"labels": {}}), \
                mock.patch.object(
                    verification, "_latest_ledger_record",
                    return_value=terminal), \
                mock.patch.object(
                    verification, "detect_mutation", return_value=None):
            result, outcome = verification.reconcile_deferred_transaction(
                self.repo, self.session_uuid, stored)
        self.assertEqual(outcome, "reconciled")
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(result["final_suite_binding"],
                         "reused_dependency_bound")
        self.assertNotEqual(result["final_suite_binding"], "ran_once")


if __name__ == "__main__":
    unittest.main()
