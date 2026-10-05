#!/usr/bin/env python3
"""Tests for the lineage helpers in `cowork_lineage`.

Source-session fixtures for the finding reader are built with the real writers
(`cowork._record_findings`, `cowork._freeze_round_evidence`,
`state_store.next_phase_round`) under a temporary COWORK_SESSIONS_ROOT, so the
reader stays aligned with the labels, counters and file names production
produces. Hand-built files are used only for hostile shapes (unusable bodies, a
verdict that was never recorded, unreadable live files).

Every test that reads a source asserts the source bytes are unchanged.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_lineage
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_lineage as lineage  # noqa: E402
import cowork_state as state_store  # noqa: E402

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _finding(summary, severity="major", criterion="c1", **extra):
    out = {"summary": summary, "severity": severity, "criterion": criterion}
    out.update(extra)
    return out


def _verdict(*findings, verdict="revise"):
    return {"verdict": verdict, "findings": ["prose"],
            "corrective_findings": list(findings)}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _snapshot(*roots):
    """Relative path -> sha256 for every file under (or at) each root."""
    def digest(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    out = {}
    for root in roots:
        if os.path.isfile(root):
            out[root] = digest(root)
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                out[path] = digest(path)
    return out


class _TempRootMixin:
    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = os.path.join(self._root, "sessions")
        self.addCleanup(self._restore_root)

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root


class _Source:
    """A synthetic source session on disk: session file, assets folder, ledger,
    live verdict file and frozen-evidence folder for one phase."""

    def __init__(self, root, uuid="11111111-1111-4111-8111-111111111111",
                 phase="scouting", state_extra=None):
        self.uuid = uuid
        self.phase = phase
        self.assets = state_store.session_assets_dir(uuid)
        os.makedirs(self.assets, exist_ok=True)
        self.session_path = os.path.join(root, "cwd", ".cowork",
                                         "session.%s.json" % uuid)
        self.state = {"session_uuid": uuid, "phase": phase, "team": [],
                      "config": {}, "sessions": {}}
        self.state.update(state_extra or {})
        _write_json(self.session_path, self.state)
        self.ledger = state_store.ledger_path_for(uuid)
        self.evidence_dir = os.path.join(self.assets, "evidence")
        helper = {"scouting": state_store.review_path_for,
                  "planning": state_store.planner_review_path_for,
                  "building": state_store.build_review_path_for}[phase]
        self.review_path = helper(self.assets, uuid)
        self.reviewer = lineage.PHASE_REVIEWER[phase]
        self.lead = lineage.PHASE_LEAD[phase]

    def write_live(self, body):
        if isinstance(body, str):
            _write_text(self.review_path, body)
        else:
            _write_json(self.review_path, body)

    def clear_live(self):
        if os.path.exists(self.review_path):
            os.unlink(self.review_path)

    def record(self, body, round_index, phase=None, discoverer=None):
        return cowork._record_findings(
            self.uuid, body, discoverer or self.reviewer,
            phase or self.phase, round_index, review_path=self.review_path)

    def freeze(self, round_index, label=None, phase=None):
        return cowork._freeze_round_evidence(
            self.uuid, self.review_path, phase or self.phase, round_index,
            label or self.reviewer)

    def read(self, with_live=True, with_evidence=True):
        return lineage.read_unresolved_findings(
            self.ledger, lineage.load_source_state(self.session_path),
            review_path=self.review_path if with_live else None,
            evidence_dir=self.evidence_dir if with_evidence else None)

    def snapshot(self):
        return _snapshot(self.assets, self.session_path)


class LineageRecordTests(unittest.TestCase):
    def test_schema_fields_complete(self):
        record = lineage.new_lineage_record(
            source_session="s-1", replacement_session="s-2",
            reason="correction", start_role="builder",
            imported_artifacts=[{
                "role": "scout", "logical_path": "scout.intel.json",
                "source_path": "/x/scout.intel.json", "sha256": DIGEST_A,
                "approval_baseline": {"reviewer": "scout-reviewer",
                                      "hash": DIGEST_B, "epoch": 0,
                                      "context_revision": 1}}],
            unresolved_finding_ids=["F-0001"],
            finding_packet={"path": "/x/packet.json", "sha256": DIGEST_A})
        self.assertEqual(set(record) - {"schema"},
                         set(lineage.LINEAGE_SCHEMA["fields"]))
        self.assertEqual(record["unresolved_finding_ids"], ["F-0001"])
        with self.assertRaises(ValueError):
            lineage.new_lineage_record(
                source_session="s-1", replacement_session="s-2",
                reason="x", start_role="reviewer")
        with self.assertRaises(ValueError):
            lineage.new_lineage_record(
                source_session="s-1", replacement_session="s-2", reason="x",
                start_role="scout", surprise=True)
        with self.assertRaises(ValueError):
            lineage.new_lineage_record(
                source_session="s-1", replacement_session="s-2", reason="x",
                start_role="scout", imported_artifacts=[{"role": "scout"}])

    def test_unresolved_basis_fields_present(self):
        record = lineage.new_lineage_record(
            source_session="s-1", replacement_session="s-2", reason="x",
            start_role="scout")
        fields = set(lineage.LINEAGE_SCHEMA["unresolved_basis_fields"])
        self.assertEqual(set(record["unresolved_basis"]), fields)
        self.assertEqual(record["unresolved_basis"]["reason"], "none")
        self.assertIn(record["unresolved_basis"]["reason"],
                      lineage.UNRESOLVED_REASONS)
        self.assertEqual(
            set(record["unresolved_basis"]["verdict_ref"]),
            set(lineage.LINEAGE_SCHEMA["verdict_ref_fields"]))
        broken = dict(record["unresolved_basis"])
        broken.pop("rule_version")
        with self.assertRaises(ValueError):
            lineage.new_lineage_record(
                source_session="s-1", replacement_session="s-2", reason="x",
                start_role="scout", unresolved_basis=broken)


class PlanImportTests(_TempRootMixin, unittest.TestCase):
    UUID = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        super().setUp()
        self.src = _Source(self._root, uuid=self.UUID, phase="building")
        self.assets = self.src.assets
        self.intel = [os.path.join(self.assets, "scout.intel.json"),
                      os.path.join(self.assets, "scout.intel.md")]
        self.plan = [os.path.join(self.assets, "planner.plan.json"),
                     os.path.join(self.assets, "planner.plan.md")]
        for path in self.intel + self.plan:
            _write_text(path, "body of %s\n" % os.path.basename(path))
        self.set_state()

    def set_state(self, scouting_epoch=1, planning_epoch=2, scout_baseline=True,
                  plan_baseline=True, scout_epoch=None, plan_epoch=None):
        sessions = {}
        if scout_baseline:
            sessions["scout-reviewer"] = {"last_approved_baseline": {
                "epoch": scouting_epoch if scout_epoch is None
                else scout_epoch,
                "context_revision": 1,
                "hash": state_store.composite_artifact_hash(self.intel)}}
        if plan_baseline:
            sessions["planning-advisor"] = {"last_approved_baseline": {
                "epoch": planning_epoch if plan_epoch is None else plan_epoch,
                "context_revision": 1,
                "hash": state_store.composite_artifact_hash(self.plan)}}
        state = {"session_uuid": self.UUID, "phase": "building", "team": [],
                 "config": {}, "sessions": sessions,
                 "scouting_epoch": scouting_epoch,
                 "planning_epoch": planning_epoch}
        _write_json(self.src.session_path, state)
        return state

    def plan_for(self, role):
        return lineage.plan_import(self.src.session_path, self.assets, role)

    def test_scout_start_always_reachable(self):
        self.set_state(scout_baseline=False, plan_baseline=False)
        plan = self.plan_for("scout")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["imported_artifacts"], [])
        self.assertEqual(plan["source_session"], self.UUID)
        self.assertIsNone(plan["refusal"])

    def test_planner_start_with_approved_intel(self):
        plan = self.plan_for("planner")
        self.assertTrue(plan["ok"], plan)
        self.assertEqual([a["logical_path"] for a in plan["imported_artifacts"]],
                         ["scout.intel.json", "scout.intel.md"])
        for entry in plan["imported_artifacts"]:
            self.assertEqual(set(entry),
                             set(lineage.LINEAGE_SCHEMA[
                                 "imported_artifact_fields"]))
            self.assertEqual(entry["sha256"],
                             lineage.sha256_file(entry["source_path"]))
            self.assertEqual(entry["approval_baseline"]["reviewer"],
                             "scout-reviewer")

    def test_builder_start_with_approved_intel_and_plan(self):
        plan = self.plan_for("builder")
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(
            [a["logical_path"] for a in plan["imported_artifacts"]],
            ["scout.intel.json", "scout.intel.md", "planner.plan.json",
             "planner.plan.md"])
        self.assertEqual(
            lineage.reachable_start_roles(self.src.session_path, self.assets),
            ["scout", "planner", "builder"])

    def test_artifact_byte_changed_refused(self):
        _write_text(self.plan[1], "different bytes\n")
        plan = self.plan_for("builder")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["refusal"], "hash_mismatch")
        self.assertEqual(plan["imported_artifacts"], [])

    def test_baseline_absent_refused(self):
        self.set_state(scout_baseline=False)
        plan = self.plan_for("planner")
        self.assertEqual(plan["refusal"], "baseline_missing")
        self.assertEqual(plan["imported_artifacts"], [])

    def test_baseline_malformed_refused(self):
        state = self.set_state()
        state["sessions"]["scout-reviewer"]["last_approved_baseline"][
            "hash"] = "not-a-digest"
        _write_json(self.src.session_path, state)
        self.assertEqual(self.plan_for("planner")["refusal"],
                         "baseline_malformed")
        state["sessions"]["scout-reviewer"]["last_approved_baseline"] = "x"
        _write_json(self.src.session_path, state)
        self.assertEqual(self.plan_for("planner")["refusal"],
                         "baseline_malformed")

    def test_composite_hash_mismatch_refused(self):
        state = self.set_state()
        state["sessions"]["planning-advisor"]["last_approved_baseline"][
            "hash"] = DIGEST_A
        _write_json(self.src.session_path, state)
        plan = self.plan_for("builder")
        self.assertEqual(plan["refusal"], "hash_mismatch")
        self.assertEqual(plan["imported_artifacts"], [])
        self.assertEqual(
            lineage.reachable_start_roles(self.src.session_path, self.assets),
            ["scout", "planner"])

    def test_planner_unreachable_without_approved_intel(self):
        for path in self.intel:
            os.unlink(path)
        self.set_state(scout_baseline=False)
        plan = self.plan_for("planner")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["refusal"], "role_unreachable")
        self.assertEqual(
            lineage.reachable_start_roles(self.src.session_path, self.assets),
            ["scout"])

    def test_builder_unreachable_without_approved_plan(self):
        for path in self.plan:
            os.unlink(path)
        self.set_state(plan_baseline=False)
        plan = self.plan_for("builder")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["refusal"], "role_unreachable")
        self.assertEqual(plan["imported_artifacts"], [])
        self.assertTrue(self.plan_for("planner")["ok"])

    def test_unknown_role_refused(self):
        for role in ("reviewer", "", None):
            plan = self.plan_for(role)
            self.assertEqual(plan["refusal"], "unknown_role")
            self.assertEqual(plan["imported_artifacts"], [])

    def test_every_refusal_code_in_closed_tuple(self):
        seen = set()

        def collect(plan):
            self.assertFalse(plan["ok"])
            self.assertIn(plan["refusal"], lineage.REFUSAL_CODES)
            seen.add(plan["refusal"])

        collect(lineage.plan_import(
            os.path.join(self._root, "missing.json"), self.assets, "scout"))
        collect(self.plan_for("nobody"))
        self.set_state(scout_baseline=False)
        collect(self.plan_for("planner"))
        state = self.set_state()
        state["sessions"]["scout-reviewer"]["last_approved_baseline"][
            "hash"] = "short"
        _write_json(self.src.session_path, state)
        collect(self.plan_for("planner"))
        self.set_state(planning_epoch=3, plan_epoch=2)
        collect(self.plan_for("builder"))
        self.set_state()
        _write_text(self.intel[0], "changed\n")
        collect(self.plan_for("planner"))
        self.set_state()
        os.unlink(self.plan[1])
        collect(self.plan_for("builder"))
        for path in self.intel:
            os.unlink(path)
        self.set_state(scout_baseline=False)
        collect(self.plan_for("planner"))
        self.assertEqual(seen, set(lineage.REFUSAL_CODES))

    def test_source_files_byte_identical_after_plan(self):
        before = self.src.snapshot()
        for role in lineage.START_ROLES + ("nobody",):
            self.plan_for(role)
        lineage.reachable_start_roles(self.src.session_path, self.assets)
        self.assertEqual(self.src.snapshot(), before)

    def test_handback_stale_baseline_refused(self):
        # The planning epoch moved on after the plan was approved: the hash
        # still matches but the approval is for a phase that was re-entered.
        self.set_state(planning_epoch=3, plan_epoch=2)
        plan = self.plan_for("builder")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["refusal"], "baseline_stale")
        self.assertEqual(plan["imported_artifacts"], [])
        self.assertTrue(self.plan_for("planner")["ok"])

    def test_scout_handback_stale_baseline_refused(self):
        self.set_state(scouting_epoch=2, scout_epoch=1)
        for role in ("planner", "builder"):
            plan = self.plan_for(role)
            self.assertEqual(plan["refusal"], "baseline_stale")
            self.assertEqual(plan["imported_artifacts"], [])
        self.assertEqual(
            lineage.reachable_start_roles(self.src.session_path, self.assets),
            ["scout"])


class FindingImportReaderTests(_TempRootMixin, unittest.TestCase):
    def source(self, phase="scouting", **kwargs):
        return _Source(self._root, phase=phase, **kwargs)

    def assertImported(self, result, ids, reason="latest_verdict_findings"):
        self.assertEqual([f["source_finding_id"] for f in result["findings"]],
                         ids)
        self.assertEqual(result["unresolved_basis"]["reason"], reason)
        self.assertIn(reason, lineage.UNRESOLVED_REASONS)

    def test_latest_verdict_open_finding_imported(self):
        src = self.source()
        body = _verdict(_finding("first"), _finding("second", "minor", "c2"))
        src.write_live(body)
        src.record(body, 1)
        result = src.read()
        self.assertImported(result, ["F-0001", "F-0002"])
        first = result["findings"][0]
        self.assertEqual(set(first),
                         set(lineage.LINEAGE_SCHEMA["finding_fields"]))
        self.assertEqual(first["summary"], "first")
        self.assertEqual(first["severity"], "major")
        self.assertEqual(first["criterion"], "c1")
        self.assertEqual(first["discoverer"], "scout-reviewer")
        self.assertEqual(first["phase"], "scouting")
        self.assertEqual(first["evidence_path"], src.review_path)
        self.assertIn(first["evidence_state"], lineage.EVIDENCE_STATES)
        self.assertEqual(len(first["source_record_sha256"]), 64)
        basis = result["unresolved_basis"]
        self.assertEqual(basis["phase"], "scouting")
        self.assertEqual(basis["discoverer"], "scout-reviewer")
        self.assertEqual(basis["round"], 1)
        self.assertEqual(basis["rule_version"], lineage.RULE_VERSION)
        self.assertEqual(basis["verdict_source"], "live")
        self.assertEqual(set(basis), set(
            lineage.LINEAGE_SCHEMA["unresolved_basis_fields"]))
        self.assertEqual(basis["verdict_ref"]["sha256"],
                         lineage.sha256_file(src.review_path))

    def test_empty_latest_verdict_imports_zero(self):
        src = self.source()
        earlier = _verdict(_finding("earlier"))
        src.record(earlier, 1)
        src.write_live(_verdict())
        result = src.read()
        self.assertImported(result, [], "latest_verdict_no_typed_findings")
        # Prose-only latest verdict: still zero, never an earlier round.
        src.write_live({"verdict": "revise", "findings": ["only prose"]})
        self.assertImported(src.read(), [], "latest_verdict_no_typed_findings")
        self.assertEqual(len(ledger.read_ledger(src.ledger)), 1)

    def test_approved_phase_findings_not_imported(self):
        src = self.source(phase="planning")
        old = _verdict(_finding("scouting era"))
        src.record(old, 1, phase="scouting", discoverer="scout-reviewer")
        body = _verdict(_finding("planning one"))
        src.record(body, 1)
        src.write_live(body)
        result = src.read()
        self.assertImported(result, ["F-0002"])
        self.assertEqual(result["findings"][0]["summary"], "planning one")
        self.assertEqual(result["unresolved_basis"]["phase"], "planning")
        self.assertEqual(result["unresolved_basis"]["discoverer"],
                         "planning-advisor")

    def test_earlier_round_superseded_by_later_revise_not_imported(self):
        src = self.source()
        src.record(_verdict(_finding("a1"), _finding("a2", criterion="c2")), 1)
        later = _verdict(_finding("b1"))
        src.record(later, 2)
        src.write_live(later)
        result = src.read()
        self.assertImported(result, ["F-0003"])
        self.assertEqual(result["unresolved_basis"]["round"], 2)

    def test_withdrawn_superseded_closure_targeted_not_imported(self):
        src = self.source()
        body = _verdict(
            _finding("withdrawn", criterion="c1"),
            _finding("superseded", criterion="c2"),
            _finding("closed by closure row", criterion="c3"),
            _finding("mechanically superseded", criterion="c4",
                     closure="superseded"),
            _finding("still open", criterion="c5"))
        src.write_live(body)
        src.record(body, 1)
        ledger.withdraw(src.ledger, "F-0001", reason="retracted")
        ledger.append_record(src.ledger, "finding", {"summary": "newer"},
                             supersedes="F-0002")
        with open(src.ledger, "a") as fh:
            fh.write(json.dumps({
                "id": "C-0001", "kind": "closure", "closes": "F-0003",
                "source_finding_id": "F-0003", "source_session": "s-x"}) + "\n")
        result = src.read()
        self.assertImported(result, ["F-0005"])
        # Everything matched resolved: zero with its own reason.
        ledger.withdraw(src.ledger, "F-0005")
        self.assertImported(src.read(), [], "latest_verdict_all_resolved")

    def test_no_qualifying_phase_imports_zero_basis_none(self):
        src = self.source()
        body = _verdict(_finding("x"))
        src.write_live(body)
        src.record(body, 1)
        for state in (None, "not a dict", {"phase": "retired"}):
            result = lineage.read_unresolved_findings(
                src.ledger, state, review_path=src.review_path,
                evidence_dir=src.evidence_dir)
            self.assertEqual(result["findings"], [])
            self.assertEqual(result["unresolved_basis"]["reason"], "none")
            self.assertIsNone(result["unresolved_basis"]["phase"])
            self.assertIsNone(result["unresolved_basis"]["round"])

    def test_latest_verdict_approve_imports_zero(self):
        src = self.source()
        src.record(_verdict(_finding("earlier")), 1)
        src.write_live({"verdict": "approve", "findings": ["looks good"],
                        "corrective_findings": [_finding("earlier")]})
        self.assertImported(src.read(), [], "latest_verdict_approve")

    def test_no_verdict_evidence_imports_zero_flagged(self):
        src = self.source()
        src.record(_verdict(_finding("earlier")), 1)
        result = src.read()
        self.assertImported(result, [], "no_verdict_evidence")
        self.assertEqual(result["unresolved_basis"]["verdict_source"], "none")
        result = lineage.read_unresolved_findings(
            src.ledger, lineage.load_source_state(src.session_path))
        self.assertImported(result, [], "no_verdict_evidence")

    def test_latest_verdict_at_round_cap_unrecorded_imports_zero_flagged(self):
        src = self.source()
        src.record(_verdict(_finding("earlier one"),
                            _finding("earlier two", criterion="c2")), 1)
        cap = _verdict(_finding("cap verdict finding"))
        src.write_live(cap)  # never recorded: round cap / needs_user / reject
        result = src.read()
        self.assertImported(result, [], "latest_verdict_unrecorded")
        self.assertIsNone(result["unresolved_basis"]["round"])
        self.assertEqual(result["unresolved_basis"]["verdict_source"], "live")

    def test_cap_verdict_identical_to_earlier_recorded_round_imports_that_rounds_rows(self):
        src = self.source()
        first = _verdict(_finding("repeat one"),
                         _finding("repeat two", criterion="c2"))
        src.record(first, 1)
        src.record(_verdict(_finding("different")), 2)
        src.write_live(first)  # same content again, never recorded
        result = src.read()
        self.assertImported(result, ["F-0001", "F-0002"])
        self.assertEqual(result["unresolved_basis"]["round"], 1)

    def test_same_round_number_collision_after_resume_matches_by_position(self):
        src = self.source()
        # Pre-resume round 1, then the loop counter restarted and wrote round 1
        # again for a different verdict.
        src.record(_verdict(_finding("pre one"),
                            _finding("pre two", criterion="c2")), 1)
        after = _verdict(_finding("post resume"))
        src.record(after, 1)
        src.write_live(after)
        self.assertImported(src.read(), ["F-0003"])
        # And the pre-resume verdict is bound to its own rows when it is the
        # latest one again.
        src.write_live(_verdict(_finding("pre one"),
                                _finding("pre two", criterion="c2")))
        self.assertImported(src.read(), ["F-0001", "F-0002"])

    def test_partly_withdrawn_round_binds_on_raw_rows_then_filters(self):
        src = self.source()
        body = _verdict(_finding("one"), _finding("two", criterion="c2"),
                        _finding("three", criterion="c3"))
        src.write_live(body)
        src.record(body, 1)
        ledger.withdraw(src.ledger, "F-0002")
        self.assertImported(src.read(), ["F-0001", "F-0003"])

    def test_absent_field_equals_none_normalisation(self):
        src = self.source()
        body = _verdict({"summary": "no weight"},
                        {"summary": "explicit none", "criterion": None,
                         "severity": None})
        src.record(body, 1)
        src.write_live(body)
        self.assertImported(src.read(), ["F-0001", "F-0002"])
        # An empty string is a value, not an absence.
        empty = _verdict({"summary": "blank criterion", "criterion": ""})
        src.record(empty, 2)
        src.write_live(_verdict({"summary": "blank criterion"}))
        self.assertImported(src.read(), [], "latest_verdict_unrecorded")
        src.write_live(empty)
        self.assertImported(src.read(), ["F-0003"])

    def test_lead_seat_counter_differs_from_ledger_round_still_matches(self):
        src = self.source()
        body = _verdict(_finding("counted differently"))
        src.write_live(body)
        # The ledger carries the reviewer seat counter; the frozen copy is named
        # with the lead seat and its own counter.
        src.record(body, 5)
        frozen = src.freeze(2, label=src.lead)
        self.assertTrue(os.path.basename(frozen).startswith("scouting-r2-scout-"))
        src.clear_live()
        result = src.read()
        self.assertImported(result, ["F-0001"])
        basis = result["unresolved_basis"]
        self.assertEqual(basis["round"], 5)
        self.assertEqual(basis["verdict_source"], "frozen")
        self.assertEqual(basis["verdict_ref"]["round"], 2)
        self.assertEqual(basis["verdict_ref"]["label"], "scout")

    def test_same_round_multi_digest_frozen_files_deterministic(self):
        src = self.source()
        one, two = _verdict(_finding("variant one")), _verdict(
            _finding("variant two"))
        paths = []
        for index, body in enumerate((one, two), start=1):
            src.write_live(body)
            src.record(body, index)
            paths.append(src.freeze(1))
        src.clear_live()
        self.assertNotEqual(paths[0], paths[1])
        for path in paths:
            os.utime(path, ns=(10**18, 10**18))
        higher = max(paths, key=os.path.basename)
        expected = "variant one" if higher == paths[0] else "variant two"
        for _ in range(2):
            result = src.read()
            self.assertEqual([f["summary"] for f in result["findings"]],
                             [expected])
        # A newer mtime beats the name order.
        lower = min(paths, key=os.path.basename)
        os.utime(lower, ns=(2 * 10**18, 2 * 10**18))
        result = src.read()
        other = "variant one" if lower == paths[0] else "variant two"
        self.assertEqual([f["summary"] for f in result["findings"]], [other])

    def test_frozen_fallback_across_label_families_newest_mtime_then_name(self):
        src = self.source()
        lead_body = _verdict(_finding("lead labelled"))
        rev_body = _verdict(_finding("reviewer labelled"))
        src.write_live(lead_body)
        src.record(lead_body, 1)
        lead_file = src.freeze(1, label=src.lead)
        src.write_live(rev_body)
        src.record(rev_body, 2)
        rev_file = src.freeze(9, label=src.reviewer)  # higher counter
        src.clear_live()
        os.utime(lead_file, ns=(2 * 10**18, 2 * 10**18))
        os.utime(rev_file, ns=(10**18, 10**18))
        # The lead-labelled file is newer; the higher round never decides.
        self.assertEqual([f["summary"] for f in src.read()["findings"]],
                         ["lead labelled"])
        os.utime(rev_file, ns=(3 * 10**18, 3 * 10**18))
        self.assertEqual([f["summary"] for f in src.read()["findings"]],
                         ["reviewer labelled"])

    def test_live_file_unparseable_or_not_dict_falls_to_frozen_then_none(self):
        src = self.source()
        body = _verdict(_finding("from frozen"))
        src.write_live(body)
        src.record(body, 1)
        src.freeze(1)
        for broken in ("{{ not json", "[1, 2, 3]", ""):
            src.write_live(broken)
            result = src.read()
            self.assertImported(result, ["F-0001"])
            self.assertEqual(result["unresolved_basis"]["verdict_source"],
                             "frozen")
        shutil.rmtree(src.evidence_dir)
        src.write_live("{{ not json")
        self.assertImported(src.read(), [], "no_verdict_evidence")

    def test_live_file_invalid_verdict_value_falls_to_frozen(self):
        src = self.source()
        body = _verdict(_finding("from frozen"))
        src.write_live(body)
        src.record(body, 1)
        src.freeze(1)
        for unusable in ({"verdict": "maybe",
                          "corrective_findings": [_finding("other")]},
                         {"verdict": "needs_user", "user_question": "  ",
                          "corrective_findings": [_finding("other")]},
                         {"findings": ["no verdict key"]}):
            src.write_live(unusable)
            result = src.read()
            self.assertImported(result, ["F-0001"])
            self.assertEqual(result["unresolved_basis"]["verdict_source"],
                             "frozen")

    def test_live_file_absent_after_clear_uses_latest_usable_frozen(self):
        src = self.source()
        older = _verdict(_finding("older usable"))
        src.write_live(older)
        src.record(older, 1)
        usable = src.freeze(1)
        src.write_live("{{ truncated")
        broken = src.freeze(2)
        src.clear_live()
        os.utime(usable, ns=(10**18, 10**18))
        os.utime(broken, ns=(2 * 10**18, 2 * 10**18))
        result = src.read()
        self.assertImported(result, ["F-0001"])
        self.assertEqual(result["unresolved_basis"]["verdict_source"],
                         "frozen")
        self.assertEqual(result["unresolved_basis"]["verdict_ref"]["sha256"],
                         lineage.sha256_file(usable))

    def test_artifact_label_files_never_parsed_as_verdict(self):
        src = self.source()
        body = _verdict(_finding("looks like a verdict"))
        src.record(body, 1)
        src.write_live(body)
        # An artifact-under-review copy is named `<seat>-reviewed`.
        for label in ("scout-reviewer-reviewed", "scout-reviewed"):
            src.freeze(1, label=label)
        src.clear_live()
        names = os.listdir(src.evidence_dir)
        self.assertEqual(len(names), 2)
        self.assertImported(src.read(), [], "no_verdict_evidence")

    def test_missing_evidence_flagged_not_dropped(self):
        src = self.source()
        gone = os.path.join(self._root, "nowhere", "evidence.txt")
        body = _verdict(_finding("cites a missing file", evidence_path=gone))
        src.record(body, 1)
        src.write_live(body)
        result = src.read()
        self.assertImported(result, ["F-0001"])
        self.assertEqual(result["findings"][0]["evidence_state"], "missing")
        self.assertEqual(result["findings"][0]["evidence_path"], gone)

    def test_sha_mismatch_flagged_not_dropped(self):
        src = self.source()
        evidence = os.path.join(self._root, "evidence.txt")
        _write_text(evidence, "observed bytes\n")
        good = lineage.sha256_file(evidence)
        body = _verdict(
            _finding("wrong digest", evidence_path=evidence,
                     evidence_sha256=DIGEST_A),
            _finding("right digest", criterion="c2", evidence_path=evidence,
                     evidence_sha256=good))
        src.record(body, 1)
        src.write_live(body)
        result = src.read()
        self.assertImported(result, ["F-0001", "F-0002"])
        wrong, right = result["findings"]
        self.assertEqual(wrong["evidence_state"], "sha_mismatch")
        self.assertEqual(wrong["evidence_sha256"], DIGEST_A)
        self.assertTrue(wrong["evidence_pinned"])
        self.assertEqual(right["evidence_state"], "verified")
        self.assertTrue(right["evidence_pinned"])

    def test_unpinned_evidence_flagged_evidence_pinned_false(self):
        src = self.source()
        evidence = os.path.join(self._root, "evidence.txt")
        _write_text(evidence, "observed bytes\n")
        body = _verdict(_finding("no recorded digest", evidence_path=evidence))
        src.record(body, 1)
        src.write_live(body)
        row = src.read()["findings"][0]
        self.assertEqual(row["evidence_state"], "verified")
        self.assertFalse(row["evidence_pinned"])
        self.assertEqual(row["evidence_sha256"],
                         lineage.sha256_file(evidence))

    def test_source_files_byte_identical_after_read(self):
        src = self.source()
        body = _verdict(_finding("one"), _finding("two", criterion="c2"))
        src.write_live(body)
        src.record(body, 1)
        src.freeze(1)
        src.freeze(1, label=src.lead)
        before = src.snapshot()
        for with_live in (True, False):
            for with_evidence in (True, False):
                src.read(with_live=with_live, with_evidence=with_evidence)
        self.assertEqual(src.snapshot(), before)
        src.clear_live()
        before = src.snapshot()
        src.read()
        self.assertEqual(src.snapshot(), before)


class ContentAddressedStoreTests(_TempRootMixin, unittest.TestCase):
    def _source_file(self, text="payload\n"):
        path = os.path.join(self._root, "src", "artifact.txt")
        _write_text(path, text)
        return path

    def test_stage_place_idempotent(self):
        store = os.path.join(self._root, "store")
        source = self._source_file()
        staged, digest = lineage.stage_content(store, source)
        self.assertEqual(digest, lineage.content_address(source))
        self.assertFalse(os.path.exists(os.path.join(store, digest)))
        final = lineage.place_content(store, staged)
        self.assertEqual(final, os.path.join(store, digest))
        self.assertFalse(os.path.exists(staged))
        with open(final) as fh:
            self.assertEqual(fh.read(), "payload\n")
        # Importing the same bytes again leaves exactly one file.
        staged_again, digest_again = lineage.stage_content(store, source)
        self.assertEqual(digest_again, digest)
        self.assertEqual(lineage.place_content(store, staged_again), final)
        self.assertEqual(os.listdir(store), [digest])
        with self.assertRaises(ValueError):
            lineage.place_content(store, source)

    def test_crash_mid_stage_leaves_no_final_file(self):
        store = os.path.join(self._root, "store")
        source = self._source_file()
        digest = lineage.content_address(source)
        staged, _ = lineage.stage_content(store, source)
        real_replace = os.replace

        def crash(*_args, **_kwargs):
            raise OSError("simulated crash")

        os.replace = crash
        try:
            with self.assertRaises(OSError):
                lineage.place_content(store, staged)
        finally:
            os.replace = real_replace
        self.assertFalse(os.path.exists(os.path.join(store, digest)))
        self.assertEqual([n for n in os.listdir(store) if n == digest], [])
        # A rerun is idempotent and completes the import.
        staged_again, _ = lineage.stage_content(store, source)
        final = lineage.place_content(store, staged_again)
        self.assertEqual(final, os.path.join(store, digest))
        self.assertTrue(os.path.exists(final))

    def test_content_address_changes_with_bytes(self):
        one = self._source_file("one\n")
        first = lineage.content_address(one)
        _write_text(one, "two\n")
        self.assertNotEqual(first, lineage.content_address(one))
        self.assertEqual(first, hashlib.sha256(b"one\n").hexdigest())
        with self.assertRaises(OSError):
            lineage.content_address(os.path.join(self._root, "absent"))


class CohortComparabilityTests(unittest.TestCase):
    BASE = {"recovery": False, "recovery_reason": None,
            "evaluation_policy": "all_rounds"}

    def test_recovery_flag_difference_refused(self):
        other = dict(self.BASE, recovery=True)
        result = lineage.cohort_comparable(self.BASE, other)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["code"], "recovery_flag_differs")

    def test_recovery_reason_difference_refused(self):
        a = dict(self.BASE, recovery=True, recovery_reason="provider_error")
        b = dict(self.BASE, recovery=True, recovery_reason="capacity")
        result = lineage.cohort_comparable(a, b)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["code"], "recovery_reason_differs")

    def test_evaluation_policy_difference_refused(self):
        other = dict(self.BASE, evaluation_policy="sampled")
        result = lineage.cohort_comparable(self.BASE, other)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["code"], "evaluation_policy_differs")
        self.assertIn(result["code"], lineage.COHORT_REFUSAL_CODES)

    def test_identical_descriptors_comparable(self):
        self.assertEqual(lineage.cohort_comparable(self.BASE, dict(self.BASE)),
                         {"comparable": True, "code": None})
        # A missing policy resolves to the default, so these are comparable.
        self.assertTrue(lineage.cohort_comparable(
            self.BASE, {"recovery": False, "recovery_reason": None})[
                "comparable"])
        missing = lineage.cohort_comparable(self.BASE, None)
        self.assertFalse(missing["comparable"])
        self.assertIn(missing["code"], lineage.COHORT_REFUSAL_CODES)


class ReconcileLineageTests(unittest.TestCase):
    SOURCE = "src-session"
    REPLACEMENT = "rep-session"

    def _entry(self, **kwargs):
        entry = {"usage": {"input_tokens": 100, "output_tokens": 10}}
        entry.update(kwargs)
        return entry

    def test_each_session_work_id_counted_once(self):
        source = {"session_uuid": self.SOURCE,
                  "work": {"w1": self._entry(), "w2": self._entry()}}
        replacement = {"session_uuid": self.REPLACEMENT,
                       "work": {"w1": self._entry(),  # same id, other session
                                "w2": self._entry(
                                    session_uuid=self.SOURCE)}}  # listed again
        result = lineage.reconcile_lineage(source, replacement)
        self.assertEqual(len(result["preserved"]), 2)
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["new"][0],
                         {"session_uuid": self.REPLACEMENT, "work_id": "w1"})
        # The same (session, work) pair twice is one entry.
        again = lineage.reconcile_lineage(
            source, {"session_uuid": self.SOURCE,
                     "work": {"w1": self._entry()}})
        self.assertEqual(again["duplicates_skipped"],
                         [{"session_uuid": self.SOURCE, "work_id": "w1"}])
        self.assertEqual(again["new"], [])

    def test_preserved_cost_not_added_to_replacement_totals(self):
        source = {"session_uuid": self.SOURCE,
                  "work": {"w1": self._entry(
                      usage={"input_tokens": 1000, "output_tokens": 5})}}
        replacement = {
            "session_uuid": self.REPLACEMENT,
            "work": {
                "w1": self._entry(usage={"input_tokens": 7, "output_tokens": 1}),
                "w2": self._entry(usage={"input_tokens": 3, "output_tokens": 1},
                                  repeated_context=True)}}
        result = lineage.reconcile_lineage(source, replacement)
        totals = result["totals"]
        self.assertEqual(totals["preserved"]["usage"],
                         {"input_tokens": 1000, "output_tokens": 5})
        self.assertEqual(totals["new"]["usage"],
                         {"input_tokens": 7, "output_tokens": 1})
        self.assertEqual(totals["repeated"]["usage"],
                         {"input_tokens": 3, "output_tokens": 1})
        self.assertEqual(totals["new"]["turns"], 1)
        self.assertEqual(len(result["repeated"]), 1)

    def test_incomparable_usage_is_unknown_not_zero(self):
        source = {"session_uuid": self.SOURCE, "work": {}}
        replacement = {"session_uuid": self.REPLACEMENT, "work": {
            "w1": self._entry(usage_scope="incomparable")}}
        result = lineage.reconcile_lineage(source, replacement)
        self.assertEqual(result["totals"]["new"]["usage"], lineage.UNKNOWN)
        self.assertEqual(result["totals"]["new"]["unknown_count"], 1)
        self.assertEqual(result["unknown"][0]["reason"], "usage_incomparable")
        self.assertEqual(result["unknown"][0]["work_id"], "w1")

    def test_missing_usage_is_unknown_not_zero(self):
        source = {"session_uuid": self.SOURCE, "work": {
            "w1": {}, "w2": {"usage": {}}, "w3": {"usage": "n/a"}}}
        replacement = {"session_uuid": self.REPLACEMENT, "work": {
            "w4": self._entry(), "w5": {}}}
        result = lineage.reconcile_lineage(source, replacement)
        self.assertEqual(result["totals"]["preserved"]["usage"], lineage.UNKNOWN)
        self.assertEqual(result["totals"]["preserved"]["unknown_count"], 3)
        # Known and unknown entries mix: the known sum is kept, the unknown
        # entry is counted rather than added as zero.
        new = result["totals"]["new"]
        self.assertEqual(new["usage"], {"input_tokens": 100,
                                        "output_tokens": 10})
        self.assertEqual(new["unknown_count"], 1)
        self.assertEqual({u["reason"] for u in result["unknown"]},
                         {"usage_missing"})


class ClosureLinkTests(unittest.TestCase):
    def _imported(self):
        return [
            {"source_finding_id": "F-0001", "summary": "one",
             "severity": "major", "criterion": "c1"},
            {"source_finding_id": "F-0002", "summary": "two",
             "severity": "minor", "criterion": "c2"}]

    def test_unknown_cited_id_rejected_not_recorded(self):
        result = lineage.closure_link(
            self._imported(), ["F-0001", "F-0099", "", 7, "R-0001"],
            replacement_findings=[{"id": "R-0001", "summary": "x"}])
        self.assertEqual([l["source_finding_id"] for l in result["links"]],
                         ["F-0001"])
        self.assertEqual(len(result["rejected"]), 4)
        self.assertEqual(result["value"]["closures"], 1)

    def test_source_id_kept_verbatim_distinct_from_reminted_id(self):
        # The replacement ledger restarts its own ids, so its F-0001 is a
        # different finding from the source's F-0001.
        result = lineage.closure_link(
            self._imported(),
            [{"source_finding_id": "F-0002", "closes": "F-0001"}],
            replacement_findings=[
                {"id": "F-0001", "summary": "brand new", "severity": "major",
                 "criterion": "z"}])
        link = result["links"][0]
        self.assertEqual(link["source_finding_id"], "F-0002")
        self.assertEqual(link["closes"], "F-0001")
        self.assertNotEqual(link["source_finding_id"], link["closes"])
        self.assertEqual(result["replacement"],
                         [{"id": "F-0001", "class": "new"}])

    def test_closure_attributed_once(self):
        result = lineage.closure_link(
            self._imported(), ["F-0001", "F-0001", "F-0002"],
            existing_closures=["F-0002"])
        statuses = [(l["source_finding_id"], l["status"], l["value"])
                    for l in result["links"]]
        self.assertEqual(statuses, [("F-0001", "attributed", 1),
                                    ("F-0001", "already_attributed", 0),
                                    ("F-0002", "already_attributed", 0)])
        self.assertEqual(result["value"]["closures"], 1)

    def test_unchanged_replay_earns_zero(self):
        result = lineage.closure_link(
            self._imported(), [],
            replacement_findings=[
                {"id": "F-0001", "summary": "one", "severity": "major",
                 "criterion": "c1"}])
        self.assertEqual(result["replacement"],
                         [{"id": "F-0001", "class": "replay"}])
        self.assertEqual(result["value"],
                         {"closures": 0, "new_findings": 0, "replay_earned": 0})

    def test_unrelated_new_finding_is_new(self):
        result = lineage.closure_link(
            self._imported(), [],
            replacement_findings=[
                {"id": "F-0007", "summary": "unrelated", "severity": "major",
                 "criterion": "c9"}])
        self.assertEqual(result["replacement"],
                         [{"id": "F-0007", "class": "new"}])
        self.assertEqual(result["value"]["new_findings"], 1)
        self.assertEqual(result["value"]["replay_earned"], 0)


if __name__ == "__main__":
    unittest.main()
