#!/usr/bin/env python3
"""Flow tests for execution profiles: the CLI surface, persistence and resume,
light topology and promotion routing, the building baseline, the build-review
cadence under each profile (real owned transactions) and the measurement and
report stamping.

Fake lead runners replace the controllers (as the other phase-flow tests do);
every repository is a throwaway git repository in a temp directory and
COWORK_SESSIONS_ROOT is pinned to a temp directory. Inputs are neutral and
synthetic.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_execution_profile_flow
"""

import contextlib
import io
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

import cowork  # noqa: E402
import cowork_execution_profiles as profiles  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_report  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

NOW = "2000-01-01T00:00:00Z"
ALL_ROLES = ["scout", "scout-reviewer", "planner", "planning-advisor",
             "builder", "build-reviewer"]
LIGHT_ROLES = ["scout", "scout-reviewer", "builder", "build-reviewer"]
WORKER_MODULES = ("cowork_verification.py", "cowork_state.py",
                  "cowork_policy.py", "cowork_ledger.py")


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def check_entry(label, depends_on=None, kind="baseline", command=None):
    entry = {"label": label,
             "command": command or ["python3", "-c", "pass"],
             "execution_mode": "isolated_snapshot", "kind": kind}
    if depends_on is not None:
        entry["depends_on"] = list(depends_on)
    return entry


def intel_doc(**result_overrides):
    result = {
        "batch": {"artifacts": ["docs/a.md"], "derivatives": {}},
        "verification": [check_entry("docs check", ["docs/a.md"]),
                         check_entry("unit", ["src/"], kind="final_suite")],
        "verification_schema": 2,
    }
    result.update(result_overrides)
    return {"status": "ready_for_review", "result": result}


def write_intel(kw, doc):
    os.makedirs(os.path.dirname(kw["intel_path"]), exist_ok=True)
    with open(kw["intel_path"], "w") as fh:
        json.dump(doc, fh)
    with open(kw["intel_md_path"], "w") as fh:
        fh.write("# intel\n")


def write_plan(kw, result):
    os.makedirs(os.path.dirname(kw["plan_json_path"]), exist_ok=True)
    with open(kw["plan_json_path"], "w") as fh:
        json.dump({"status": "ready_for_review", "result": result}, fh)
    with open(kw["plan_md_path"], "w") as fh:
        fh.write("# plan\n")


def init_repo(files):
    repo = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", repo], check=True)
    for args in (("config", "user.email", "t@t"), ("config", "user.name", "t"),
                 ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", repo] + list(args), check=True)
    for rel, text in files.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True)
    return repo


class _FlowCase(unittest.TestCase):
    """A throwaway git repo as the launch directory, an isolated sessions
    root, and fake lead runners that fail the test if an unexpected role is
    dispatched."""

    FILES = {"docs/a.md": "a\n", "docs/b.md": "b\n", "docs/index.md": "i\n",
             "src/core.py": "VALUE = 1\n"}

    def setUp(self):
        self.sessions_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.sessions_root,
                                              ignore_errors=True))
        patch = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.sessions_root})
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = init_repo(self.FILES)
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        prior = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, prior)
        self.reset_calls()
        self.session_dirs = []

    def reset_calls(self):
        self.calls = {"scout": [], "planner": [], "builder": []}

    def new_session_path(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return os.path.join(directory, ".cowork", "session.json")

    def args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def forbidden(self, role):
        def fake(*_a, **_k):
            self.fail("%s must not run" % role)
        return fake

    def fake(self, role, outcome, payload=None, before=None,
             save_session=True):
        def run(config, context, selected, on_outcome=None, on_session=None,
                resume_id=None, **kw):
            self.calls[role].append(dict(
                kw, context=str(context), resume_id=resume_id,
                selected=list(selected)))
            if before is not None:
                before(kw)
            if save_session and on_session and resume_id is None:
                on_session("claude", "%s-%d" % (role, len(self.calls[role])))
            if on_outcome:
                on_outcome(outcome, payload)
            return 0
        return run

    def run_flow(self, argv, scout=None, planner=None, builder=None):
        box = {}
        out = io.StringIO()
        rc = cowork.run_flow(
            self.args(argv), io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=scout or self.forbidden("scout"),
            run_planner_fn=planner or self.forbidden("planner"),
            run_builder_fn=builder or self.forbidden("builder"),
            result_box=box)
        return rc, out.getvalue(), box

    def make_session(self, profile, rationale="why not"):
        spath = self.new_session_path()
        rc, _out, box = self.run_flow(
            ["--profile", profile, "--profile-rationale", rationale,
             "--context", "neutral brief", "--session-file", spath],
            scout=self.fake("scout", "ended"))
        self.assertEqual(rc, 1)
        self.reset_calls()
        return spath, box["session_uuid"]

    def record_of(self, spath):
        kind, record, reason = state_store.read_execution_profile(
            state_store.load(spath))
        self.assertEqual((kind, reason), ("valid", None))
        return record

    def asset_path(self, suid):
        return state_store.execution_profile_path_for(suid)


class PreviewCommandTests(_FlowCase):
    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(cowork, "run_flow",
                                  side_effect=AssertionError("must not run")):
            rc = cowork.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_preview_prints_one_json_object_per_profile(self):
        for name in profiles.PROFILES:
            rc, out, _err = self.main(["--preview-profile", name])
            self.assertEqual(rc, 0)
            self.assertEqual(len(out.strip().splitlines()), 1)
            self.assertEqual(json.loads(out), profiles.preview(name))

    def test_an_unknown_profile_is_refused_with_a_json_error(self):
        rc, out, err = self.main(["--preview-profile", "nope"])
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "unknown_profile")
        self.assertEqual(payload["requested"], "nope")
        self.assertEqual(payload["known_profiles"],
                         list(profiles.PROFILES))
        self.assertIn("unknown execution profile", err)

    def test_preview_creates_nothing(self):
        self.main(["--preview-profile", "light"])
        self.assertEqual(os.listdir(self.sessions_root), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".cowork")))

    def test_preview_cannot_be_combined_with_a_profile_selection(self):
        rc, out, _err = self.main(
            ["--preview-profile", "light", "--profile", "light"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["reason"],
                         "conflicting_arguments")


class RunFlowRefusalTests(_FlowCase):
    def refused(self, argv, reason):
        rc, _out, box = self.run_flow(argv)
        self.assertEqual(rc, 2)
        self.assertEqual(box["reason"], reason)
        self.assertEqual(self.calls, {"scout": [], "planner": [],
                                      "builder": []})

    def test_an_unknown_profile_is_refused_before_any_session_exists(self):
        spath = self.new_session_path()
        self.refused(["--profile", "nope", "--context", "x",
                      "--session-file", spath], "unknown_profile")
        self.assertFalse(os.path.exists(spath))
        self.assertEqual(os.listdir(self.sessions_root), [])

    def test_a_profile_cannot_ride_no_session(self):
        self.refused(["--profile", "light", "--no-session",
                      "--context", "x"], "profile_requires_session")

    def test_a_profile_derives_the_team(self):
        spath = self.new_session_path()
        self.refused(["--profile", "light", "--team", "scout,scout-reviewer",
                      "--context", "x", "--session-file", spath],
                     "profile_team_conflict")

    def test_a_profile_cannot_be_attached_to_an_unprofiled_session(self):
        spath = self.new_session_path()
        rc, _out, _box = self.run_flow(
            ["--team", "scout,scout-reviewer", "--context", "x",
             "--session-file", spath], scout=self.fake("scout", "ended"))
        self.assertEqual(rc, 1)
        self.reset_calls()
        before = read_bytes(spath)
        self.refused(["--profile", "light", "--session-file", spath],
                     "profile_not_bound")
        self.assertEqual(read_bytes(spath), before)

    def test_a_lower_profile_on_resume_is_refused_with_files_untouched(self):
        spath, suid = self.make_session("standard")
        before = (read_bytes(spath), read_bytes(self.asset_path(suid)))
        self.refused(["--profile", "light", "--session-file", spath],
                     "profile_demotion_refused")
        self.assertEqual(
            (read_bytes(spath), read_bytes(self.asset_path(suid))), before)

    def test_a_damaged_record_stops_the_run_before_dispatch(self):
        def truncate(spath, suid):
            raw = read_bytes(self.asset_path(suid))
            with open(self.asset_path(suid), "wb") as fh:
                fh.write(raw[:len(raw) // 2])

        def delete(spath, suid):
            os.remove(self.asset_path(suid))

        def mismatch(spath, suid):
            state = state_store.load(spath)
            state[state_store.EXECUTION_PROFILE_KEY]["selected"] = "assurance"
            state_store.save(spath, state)

        def below_selected(spath, suid):
            record = json.loads(read_bytes(self.asset_path(suid)))
            record["effective"] = "light"
            with open(self.asset_path(suid), "w") as fh:
                json.dump(record, fh)

        for damage in (truncate, delete, mismatch, below_selected):
            with self.subTest(damage=damage.__name__):
                spath, suid = self.make_session("standard")
                damage(spath, suid)
                before = read_bytes(spath)
                self.refused(["--session-file", spath],
                             "execution_profile_unreadable")
                self.assertEqual(read_bytes(spath), before)


class LegacySessionTests(_FlowCase):
    def test_an_unprofiled_session_carries_no_profile_anywhere(self):
        spath = self.new_session_path()
        rc, _out, box = self.run_flow(
            ["--team", "scout,scout-reviewer", "--context", "x",
             "--session-file", spath], scout=self.fake("scout", "approved"))
        self.assertEqual(rc, 0)
        state = state_store.load(spath)
        self.assertNotIn(state_store.EXECUTION_PROFILE_KEY, state)
        self.assertEqual(state_store.read_execution_profile(state),
                         ("absent", None, None))
        self.assertNotIn("execution_profile", box)
        self.assertNotIn("execution_profile",
                         cowork.build_run_result(rc, box))
        self.assertFalse(os.path.exists(
            self.asset_path(box["session_uuid"])))
        self.assertEqual(len(self.calls["scout"]), 1)
        self.assertNotIn("profile_session", self.calls["scout"][0])


class PersistenceResumeTests(_FlowCase):
    def test_a_plain_resume_reloads_every_recorded_field(self):
        spath, suid = self.make_session("standard")
        record = self.record_of(spath)
        session = profiles.ProfileSession(
            record, lambda r: state_store.write_execution_profile_record(
                suid, r), now_fn=lambda: NOW)
        batch = {"artifacts": ["docs/a.md"],
                 "derivatives": {"docs/a.md": ["docs/index.md"]}}
        session.on_intel_approved({"batch": batch})
        session.on_plan_approved({"batch": batch}, self.repo)
        inventory = [check_entry("docs check", ["docs/a.md"]),
                     check_entry("unit", ["src/"], kind="final_suite")]
        session.on_transaction({
            "verdict": "green", "transaction_id": "t1",
            "snapshot": {"manifest_digest": "m"},
            "attempts": [{"label": "docs check", "exit_code": 0,
                          "evidence_state": "present"}],
            "dependency_digests": {"docs check": "d"}}, inventory)
        session.on_build_approved(
            {"verdict": "approve", "corrective_findings": [],
             "deferred_minor_notes": [{"summary": "tidy"}]}, 1, "m")
        session.set_building_baseline(1, "fingerprint")
        session.on_verdict("build-reviewer", {
            "verdict": "revise", "corrective_findings": [
                {"severity": "minor", "risk_class": "architectural"}]})
        # Every field is non-default BEFORE the resume, so equality after it
        # proves something.
        written = json.loads(read_bytes(self.asset_path(suid)))
        self.assertTrue(written["batch"]["artifacts"])
        self.assertEqual(
            written["invalidation_graph"]["artifact_derivatives"],
            {"docs/a.md": ["docs/index.md"]})
        self.assertTrue(written["accepted_evidence"])
        self.assertTrue(written["invalidation_graph"]["entry_dependencies"])
        self.assertEqual(len(written["deferred_minor_notes"]), 1)
        self.assertEqual(len(written["promotion_history"]), 1)
        self.assertEqual(written["effective"], "assurance")
        self.assertEqual(written["rationale"]["note"], "why not")
        self.assertEqual(written["building_baseline"],
                         {"building_epoch": 1,
                          "manifest_fingerprint": "fingerprint"})
        before = read_bytes(self.asset_path(suid))

        rc, _out, _box = self.run_flow(
            ["--session-file", spath], scout=self.fake("scout", "ended"))
        self.assertEqual(rc, 1)
        self.assertEqual(read_bytes(self.asset_path(suid)), before)
        reloaded = self.record_of(spath)
        self.assertEqual(reloaded, json.loads(before))
        fresh = profiles.ProfileSession(reloaded, lambda r: None)
        self.assertEqual(fresh.effective, "assurance")
        self.assertEqual(fresh.record["batch"], session.record["batch"])
        self.assertIsNone(fresh.reuse_policy())

    def test_a_higher_profile_promotes_explicitly_and_grows_the_team(self):
        spath, suid = self.make_session("light")
        self.assertEqual(state_store.load(spath)["team"], LIGHT_ROLES)
        before_config = dict(state_store.load(spath)["config"])
        rc, _out, _box = self.run_flow(
            ["--profile", "standard", "--session-file", spath],
            scout=self.fake("scout", "ended"))
        self.assertEqual(rc, 1)
        record = self.record_of(spath)
        self.assertEqual(record["effective"], "standard")
        self.assertEqual(record["selected"], "light")
        entry, = record["promotion_history"]
        self.assertEqual((entry["reason_codes"], entry["seam"]),
                         (["explicit_request"], "invocation"))
        state = state_store.load(spath)
        self.assertEqual(state["team"], ALL_ROLES)
        normalize = cowork.normalize_role_config
        for role in LIGHT_ROLES:
            self.assertEqual(normalize(state["config"][role]),
                             normalize(before_config[role]))
        for role in ("planner", "planning-advisor"):
            self.assertEqual(
                normalize(state["config"][role]),
                normalize(cowork.default_config([role])[role]))
        self.assertEqual(self.calls["scout"][0]["selected"], ALL_ROLES)

    def test_resuming_with_the_same_profile_changes_nothing(self):
        spath, suid = self.make_session("light")
        before = (read_bytes(spath), read_bytes(self.asset_path(suid)))
        rc, _out, _box = self.run_flow(
            ["--profile", "light", "--session-file", spath],
            scout=self.fake("scout", "ended"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.record_of(spath)["promotion_history"], [])
        self.assertEqual(state_store.load(spath)["team"], LIGHT_ROLES)
        self.assertEqual(read_bytes(self.asset_path(suid)), before[1])


class LightTopologyTests(_FlowCase):
    def light_run(self, doc, planner=None, builder=None):
        spath = self.new_session_path()
        scout = self.fake("scout", "approved",
                          before=lambda kw: write_intel(kw, doc))
        rc, _out, box = self.run_flow(
            ["--profile", "light", "--context", "docs batch",
             "--session-file", spath], scout=scout, planner=planner,
            builder=builder)
        return spath, rc, box

    def test_a_clean_documentation_batch_goes_straight_to_building(self):
        spath, rc, box = self.light_run(
            intel_doc(), builder=self.fake("builder", "approved"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls["scout"]), 1)
        self.assertEqual(self.calls["planner"], [])
        intel_path = self.calls["scout"][0]["intel_path"]
        builder, = self.calls["builder"]
        self.assertEqual(builder["plan_json_path"], intel_path)
        self.assertEqual(builder["plan_md_path"],
                         self.calls["scout"][0]["intel_md_path"])
        self.assertIn("light execution profile", builder["context"])
        self.assertIn(intel_path, builder["context"])
        self.assertNotIn("planning-advisor APPROVED", builder["context"])
        state = state_store.load(spath)
        self.assertEqual(state_store.get_phase(state), "building")
        self.assertEqual(state["team"], LIGHT_ROLES)
        record = self.record_of(spath)
        self.assertEqual(record["plan_source"], "intel")
        self.assertEqual(record["effective"], "light")
        self.assertEqual(record["promotion_history"], [])
        self.assertEqual(record["batch"]["artifacts"], ["docs/a.md"])
        self.assertEqual(record["batch"]["source"], "intel")
        self.assertEqual(record["building_baseline"]["building_epoch"],
                         builder["building_epoch"])
        baseline = json.loads(read_bytes(
            state_store.execution_profile_baseline_path_for(
                box["session_uuid"])))
        self.assertEqual(baseline["manifest_fingerprint"],
                         record["building_baseline"]["manifest_fingerprint"])
        self.assertIn("docs/a.md", baseline["files"])
        self.assertEqual(
            cowork.build_run_result(rc, box)["execution_profile"],
            {"selected": "light", "effective": "light",
             "promotion_count": 0, "deferred_minor_note_count": 0})

    def test_an_executable_batch_promotes_and_goes_through_planning(self):
        doc = intel_doc(batch={"artifacts": ["tools/run.py"]})
        spath = self.new_session_path()
        state_store.ensure_session(spath, None, "S-light")
        suid = "S-light"
        epoch = state_store.get_scouting_epoch(state_store.load(spath))
        work_before = cowork._role_work_id(suid, "scout", epoch, 0)
        scout = self.fake("scout", "approved",
                          before=lambda kw: write_intel(kw, doc))
        rc, _out, box = self.run_flow(
            ["--profile", "light", "--context", "x", "--session-file", spath],
            scout=scout, planner=self.fake("planner", "ended"))
        self.assertEqual(rc, 1)
        self.assertEqual(box["session_uuid"], suid)
        self.assertEqual(len(self.calls["planner"]), 1)
        self.assertEqual(self.calls["builder"], [])
        self.assertEqual(self.calls["planner"][0]["intel_path"],
                         self.calls["scout"][0]["intel_path"])
        state = state_store.load(spath)
        self.assertEqual(state_store.get_phase(state), "planning")
        self.assertEqual(state["team"], ALL_ROLES)
        record = self.record_of(spath)
        self.assertEqual(record["effective"], "standard")
        entry, = record["promotion_history"]
        self.assertEqual((entry["reason_codes"], entry["seam"]),
                         (["executable_change"], "intel_approval"))
        self.assertEqual(record["batch"]["artifacts"], ["tools/run.py"])
        self.assertEqual(record["plan_source"], "plan")
        # Work identity survives the promotion.
        self.assertEqual(
            cowork._role_work_id(suid, "scout",
                                 state_store.get_scouting_epoch(state), 0),
            work_before)

    def test_an_architectural_tag_promotes_straight_to_assurance(self):
        spath, rc, _box = self.light_run(
            intel_doc(risk_class="architectural"),
            planner=self.fake("planner", "ended"))
        self.assertEqual(rc, 1)
        record = self.record_of(spath)
        self.assertEqual(record["effective"], "assurance")
        self.assertEqual(record["promotion_history"][0]["reason_codes"],
                         ["architectural_risk"])
        self.assertEqual(len(self.calls["planner"]), 1)

    def test_a_missing_inventory_is_undeclared_and_goes_through_planning(self):
        doc = intel_doc()
        del doc["result"]["verification"]
        spath, rc, _box = self.light_run(
            doc, planner=self.fake("planner", "ended"))
        record = self.record_of(spath)
        self.assertEqual(record["effective"], "standard")
        self.assertEqual(record["promotion_history"][0]["reason_codes"],
                         ["batch_undeclared"])

    def test_a_light_resume_into_building_gets_the_light_seed(self):
        # The first builder never reports a session id, so the resumed phase
        # starts a FRESH builder: it must be seeded from the intel (the light
        # plan source), not from a planner plan that does not exist.
        spath, rc, box = self.light_run(
            intel_doc(),
            builder=self.fake("builder", "ended", save_session=False))
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.calls["builder"]), 1)
        self.reset_calls()
        rc, _out, _box = self.run_flow(
            ["--session-file", spath],
            builder=self.fake("builder", "ended", save_session=False))
        builder, = self.calls["builder"]
        self.assertIsNone(builder["resume_id"])
        self.assertIn("light execution profile", builder["context"])
        self.assertNotIn("planning-advisor APPROVED", builder["context"])
        record = self.record_of(spath)
        self.assertEqual(record["plan_source"], "intel")


class PlanBatchTests(_FlowCase):
    def standard_run(self, plan_result):
        spath = self.new_session_path()
        scout = self.fake("scout", "approved",
                          before=lambda kw: write_intel(kw, intel_doc()))
        planner = self.fake("planner", "approved",
                            before=lambda kw: write_plan(kw, plan_result))
        rc, _out, _box = self.run_flow(
            ["--profile", "standard", "--context", "x",
             "--session-file", spath], scout=scout, planner=planner,
            builder=self.fake("builder", "approved"))
        return spath, rc

    def test_the_boundary_is_derived_from_the_per_file_list(self):
        spath, rc = self.standard_run({"implementation": [
            {"file": "docs/a.md, docs/b.md"}, {"file": "docs/index.md"}]})
        self.assertEqual(rc, 0)
        record = self.record_of(spath)
        self.assertEqual(record["batch"]["source"],
                         "plan_implementation_files")
        self.assertEqual(record["batch"]["artifacts"],
                         ["docs/a.md", "docs/b.md", "docs/index.md"])
        self.assertEqual(record["effective"], "standard")
        self.assertEqual(record["plan_source"], "plan")
        # A later out-of-boundary change expands the scope.
        session = profiles.ProfileSession(record, lambda r: None,
                                          now_fn=lambda: NOW)
        session.on_builder_ready(["src/other.py"], [], {"verdict": "green"},
                                 [], {})
        self.assertEqual(session.effective, "assurance")
        self.assertEqual(session.record["promotion_history"][0][
            "reason_codes"], ["scope_expansion"])

    def test_a_declared_batch_wins_over_the_per_file_list(self):
        spath, rc = self.standard_run({
            "batch": {"artifacts": ["docs/a.md"], "derivatives": {}},
            "implementation": [{"file": "docs/b.md"}]})
        record = self.record_of(spath)
        self.assertEqual(record["batch"]["source"], "plan")
        self.assertEqual(record["batch"]["artifacts"], ["docs/a.md"])

    def test_an_unresolvable_boundary_promotes_to_assurance(self):
        spath, rc = self.standard_run({"goal": "no boundary at all"})
        record = self.record_of(spath)
        self.assertEqual(record["batch"]["source"], "plan_unresolved")
        self.assertEqual(record["effective"], "assurance")
        self.assertEqual(record["promotion_history"][0]["reason_codes"],
                         ["signal_malformed"])


class HandbackPromotionTests(_FlowCase):
    def test_a_light_handback_is_authorized_by_promoting_in_the_same_call(self):
        spath = self.new_session_path()
        scout = self.fake("scout", "approved",
                          before=lambda kw: write_intel(kw, intel_doc()))
        handback = cowork._agent_stop_payload(
            "handoff_requested", "builder", requires="authorization",
            handoff="re-plan the batch", to_role="planner")
        rc, _out, box = self.run_flow(
            ["--profile", "light", "--context", "x", "--session-file", spath],
            scout=scout, builder=self.fake("builder", "stopped", handback))
        self.assertEqual(rc, cowork.AGENT_STOP_EXIT_CODE)
        suid = box["session_uuid"]
        request_id = state_store.read_decision_request(suid)["request_id"]
        self.reset_calls()
        # Without a promotion the planner is not on the team.
        rc, _out, box = self.run_flow(
            ["--session-file", spath, "--authorize-handoff", request_id])
        self.assertEqual(rc, 2)
        self.assertEqual(box["reason"], "handoff_target_not_selected")
        self.assertEqual(self.calls["planner"], [])
        rc, _out, _box = self.run_flow(
            ["--session-file", spath, "--authorize-handoff", request_id,
             "--profile", "standard"],
            planner=self.fake("planner", "ended"))
        planner, = self.calls["planner"]
        self.assertIn("scout.intel.json", planner["context"])
        self.assertIn("handback.planner.txt", planner["context"])
        record = self.record_of(spath)
        self.assertEqual(record["effective"], "standard")
        self.assertEqual(state_store.load(spath)["team"], ALL_ROLES)
        self.assertEqual(record["promotion_history"][0]["reason_codes"],
                         ["explicit_request"])


class BuildingBaselineTests(_FlowCase):
    def setUp(self):
        super().setUp()
        self.suid = "S-" + uuid.uuid4().hex[:8]
        os.makedirs(state_store.session_assets_dir(self.suid))
        # Present BEFORE building starts: they are part of the baseline.
        self.write("docs/notes.md", "pre-existing untracked note\n")
        self.write("scripts/__pycache__/x.cpython-39.pyc", "bytecode\n")
        self.record = profiles.new_record("light", None, NOW)
        manifest = verification.candidate_manifest(self.repo)
        fingerprint = verification.manifest_fingerprint(manifest)
        state_store.write_json_atomic_durable(
            state_store.execution_profile_baseline_path_for(self.suid),
            {"building_epoch": 1, "manifest_fingerprint": fingerprint,
             "files": manifest})
        self.record["building_baseline"] = {
            "building_epoch": 1, "manifest_fingerprint": fingerprint}

    def write(self, rel, text, mode=None):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(path, mode)

    def measure(self):
        manifest = verification.candidate_manifest(self.repo)
        state_store.write_json_atomic(
            state_store.verification_snapshot_manifest_path_for(
                self.suid, "T1"), {"files": manifest})
        return cowork._profile_changed_paths(
            self.suid, self.record, {"transaction_id": "T1"})

    def test_untouched_trees_report_no_change(self):
        self.assertEqual(self.measure(), ([], []))

    def test_preexisting_untracked_files_and_bytecode_never_count(self):
        self.write("scripts/__pycache__/y.cpython-39.pyc", "new bytecode\n")
        self.write("scripts/__pycache__/x.cpython-39.pyc", "changed\n")
        self.assertEqual(self.measure(), ([], []))

    def test_edits_are_changed_and_only_executable_ones_are_executable(self):
        self.write("docs/a.md", "edited\n")
        self.write("src/new.py", "VALUE = 2\n")
        changed, executable = self.measure()
        self.assertEqual(changed, ["docs/a.md", "src/new.py"])
        self.assertEqual(executable, ["src/new.py"])

    def test_a_mode_flip_on_a_document_is_an_executable_change(self):
        os.chmod(os.path.join(self.repo, "docs/b.md"), 0o755)
        changed, executable = self.measure()
        self.assertEqual((changed, executable),
                         (["docs/b.md"], ["docs/b.md"]))

    def test_a_missing_or_tampered_baseline_measures_nothing(self):
        path = state_store.execution_profile_baseline_path_for(self.suid)
        raw = read_bytes(path)
        doc = json.loads(raw)
        doc["files"]["docs/a.md"]["sha256"] = "0" * 64
        with open(path, "w") as fh:
            json.dump(doc, fh)
        self.assertEqual(self.measure(), (None, None))
        os.remove(path)
        self.assertEqual(self.measure(), (None, None))
        no_reference = dict(self.record, building_baseline=None)
        self.assertEqual(cowork._profile_changed_paths(
            self.suid, no_reference, {"transaction_id": "T1"}),
            (None, None))

    def test_an_unreadable_baseline_promotes_on_a_malformed_signal(self):
        self.assertEqual(
            profiles.builder_ready_triggers(
                "standard", None, None, None, {"verdict": "green"}, [], {}),
            ["signal_malformed"])


class _RoleLoopCase(_FlowCase):
    """Drives `_role_loop` as the builder with real owned transactions in a
    throwaway repository seeded with the worker modules."""

    FILES = dict(_FlowCase.FILES, **{"tests/test_core.py": "VALUE = 1\n"})

    def setUp(self):
        super().setUp()
        scripts = os.path.join(self.repo, "scripts")
        os.makedirs(scripts)
        for name in WORKER_MODULES:
            shutil.copyfile(os.path.join(_HERE, name),
                            os.path.join(scripts, name))
        subprocess.run(["git", "-C", self.repo, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "seed"],
                       check=True)
        self.suid = "S-" + uuid.uuid4().hex[:8]
        assets = state_store.session_assets_dir(self.suid)
        os.makedirs(assets)
        fd, self.marker = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.marker)
                        and os.remove(self.marker))
        self.status_path = os.path.join(assets, "builder.status.json")
        self.plan_path = os.path.join(assets, "scout.intel.json")
        # Present BEFORE building: part of the baseline, never a signal.
        self.write("docs/notes.md", "pre-existing untracked note\n")
        self.write("scripts/__pycache__/x.cpython-39.pyc", "bytecode\n")
        self.trace_path = trace_store.trace_path_for(self.suid)
        os.makedirs(os.path.dirname(self.trace_path), exist_ok=True)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def command(self, label):
        return ["python3", "-c",
                "open(%r, 'a').write(%r + chr(10))" % (self.marker, label)]

    def runs(self):
        with open(self.marker) as fh:
            return sorted(line for line in fh.read().split("\n") if line)

    def profile_session(self, profile):
        result = {
            "batch": {"artifacts": ["docs/a.md", "docs/b.md"],
                      "derivatives": {"docs/a.md": ["docs/index.md"]}},
            "verification": [
                check_entry("doc a", ["docs/a.md"], command=self.command("doc a")),
                check_entry("doc b", ["docs/b.md"], command=self.command("doc b")),
                check_entry("index", ["docs/index.md"],
                            command=self.command("index")),
                check_entry("unit", ["src/", "tests/"], kind="final_suite",
                            command=self.command("unit"))],
            "verification_schema": 2}
        with open(self.plan_path, "w") as fh:
            json.dump({"status": "ready_for_review", "result": result}, fh)
        self.trace = trace_store.Trace(
            self.trace_path, session_uuid=self.suid, run_id="R")
        session = profiles.ProfileSession(
            profiles.new_record(profile, None, NOW),
            lambda r: state_store.write_execution_profile_record(self.suid, r),
            trace_fn=self.trace.event, now_fn=lambda: NOW)
        session.on_intel_approved(result)
        # The light chaining makes the approved intel the plan source.
        session.set_plan_source(profiles.PLAN_SOURCE_INTEL)
        manifest = verification.candidate_manifest(self.repo)
        fingerprint = verification.manifest_fingerprint(manifest)
        state_store.write_json_atomic_durable(
            state_store.execution_profile_baseline_path_for(self.suid),
            {"building_epoch": 1, "manifest_fingerprint": fingerprint,
             "files": manifest})
        session.set_building_baseline(1, fingerprint)
        return session

    def drive(self, session, edits, verdicts, work_id=None):
        status_path = self.status_path
        sent = []

        class FakeSession:
            def send(self_inner, text):
                sent.append(text)
                if edits:
                    edits.pop(0)()
                with open(status_path, "w") as fh:
                    json.dump({"session": "X", "role": "builder",
                               "status": "ready_for_review", "result": {}},
                              fh)

            def close(self_inner):
                pass

        reviewed = []

        def review_fn(_path, _round):
            reviewed.append(_round)
            return verdicts.pop(0)

        rc, outcome, payload = cowork._role_loop(
            FakeSession(), "seed", status_path, context="",
            io_out=io.StringIO(), role="builder", review_fn=review_fn,
            trace=self.trace, reviewer_role=cowork.BUILD_REVIEWER,
            artifact_noun="build", phase="building",
            session_uuid=self.suid, profile_session=session,
            plan_json_path=self.plan_path, role_work_id=work_id)
        self.sent, self.reviewed = sent, reviewed
        return rc, outcome, payload

    def events(self, name):
        with open(self.trace_path) as fh:
            return [event for event in
                    (json.loads(line) for line in fh if line.strip())
                    if event.get("event") == name]

    @staticmethod
    def minor_revise():
        return {"verdict": "revise", "corrective_findings": [
            {"summary": "tidy a heading", "severity": "minor"}]}

    @staticmethod
    def approve_with_note():
        return {"verdict": "approve", "corrective_findings": [],
                "deferred_minor_notes": [{
                    "summary": "reword one sentence later",
                    "evidence_path": "/tmp/note",
                    "evidence_sha256": "a" * 64}]}


class LightDocsCadenceTests(_RoleLoopCase):
    def edit(self, rel, text):
        return lambda: self.write(rel, text)

    def both(self):
        def edit():
            self.write("docs/a.md", "a edited\n")
            self.write("docs/b.md", "b edited\n")
        return edit

    def test_a_minor_correction_reruns_only_the_touched_check(self):
        session = self.profile_session("light")
        rc, outcome, payload = self.drive(
            session, [self.both(), self.edit("docs/b.md", "b again\n")],
            [self.minor_revise(), self.approve_with_note()])
        self.assertEqual((rc, outcome), (0, "approved"))
        # A revise reopens the builder exactly once; the approve with a
        # deferred note needs no further builder round.
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.reviewed), 2)
        self.assertEqual(self.runs(),
                         ["doc a", "doc b", "doc b", "index", "unit"])
        transactions = self.events("verification.transaction")
        self.assertEqual(
            [t["final_suite_binding"] for t in transactions],
            ["ran_once", "reused_dependency_bound"])
        record = session.record
        self.assertEqual(record["counters"],
                         {"verification_executed": 5,
                          "verification_reused": 3})
        self.assertEqual(record["promotion_history"], [])
        self.assertEqual(session.effective, "light")
        note, = record["deferred_minor_notes"]
        self.assertEqual(note["summary"], "reword one sentence later")
        self.assertIn("review_round", note)
        self.assertEqual(record["last_invalidated"], ["doc b"])

    def test_changing_one_artifact_reruns_it_and_its_derivatives_only(self):
        session = self.profile_session("light")
        rc, outcome, _payload = self.drive(
            session, [self.both(), self.edit("docs/a.md", "a again\n")],
            [self.minor_revise(), self.approve_with_note()])
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(
            self.runs(),
            ["doc a", "doc a", "doc b", "index", "index", "unit"])
        self.assertEqual(session.record["counters"],
                         {"verification_executed": 6,
                          "verification_reused": 2})

    def test_an_approve_carrying_a_corrective_finding_stops_the_phase(self):
        session = self.profile_session("light")
        verdict = {"verdict": "approve", "corrective_findings": [
            {"summary": "still wrong", "severity": "minor"}]}
        rc, outcome, payload = self.drive(session, [self.both()], [verdict])
        self.assertEqual((rc, outcome), (0, "ended"))
        self.assertEqual(payload["kind"], "review_profile_rejected")
        self.assertEqual(payload["profile_rejected"],
                         "corrective_findings_on_approve")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.reviewed, [1])
        self.assertEqual(session.record["deferred_minor_notes"], [])

    def test_an_executable_change_promotes_mid_build_and_keeps_the_evidence(
            self):
        session = self.profile_session("light")
        before = {key: json.loads(json.dumps(session.record[key]))
                  for key in ("batch", "plan_source", "building_baseline")}

        def edit():
            self.write("docs/a.md", "a edited\n")
            self.write("src/new.py", "VALUE = 2\n")

        work_id = "W-%s" % uuid.uuid4().hex[:8]
        # Round 1 promotes; a minor revise then runs a second round AFTER
        # the promotion, so work identity is observed across it.
        rc, outcome, _payload = self.drive(
            session, [edit, lambda: self.write("docs/b.md", "b again\n")],
            [self.minor_revise(),
             {"verdict": "approve", "corrective_findings": []}],
            work_id=work_id)
        self.assertEqual((rc, outcome), (0, "approved"))
        # The out-of-batch executable file is still present in round 2, so
        # the stricter profile's own scope check promotes once more: one
        # history entry per change, monotonic, never a demotion.
        self.assertEqual(session.effective, "assurance")
        first, second = session.record["promotion_history"]
        self.assertEqual(
            (first["from"], first["to"], first["seam"], first["reason_codes"]),
            ("light", "standard", "builder_ready",
             ["scope_expansion", "executable_change"]))
        self.assertEqual((second["from"], second["to"], second["seam"]),
                         ("standard", "assurance", "builder_ready"))
        self.assertIn("scope_expansion", second["reason_codes"])
        for key, value in before.items():
            self.assertEqual(session.record[key], value, key)
        self.assertEqual(sorted(session.record["accepted_evidence"]),
                         ["doc a", "doc b", "index", "unit"])
        self.assertEqual(session.plan_source, "intel")
        # Every owned transaction, before and after the promotion, is bound
        # to the same engagement identity.
        transactions = self.events("verification.transaction")
        self.assertEqual(len(transactions), 2)
        for event in transactions:
            request = state_store.read_json_tolerant(
                state_store.verification_request_path_for(
                    self.suid, event["transaction_id"]))
            self.assertEqual(request["work_id"], work_id)

    def test_preexisting_untracked_files_and_bytecode_never_promote(self):
        session = self.profile_session("light")
        self.write("scripts/__pycache__/y.cpython-39.pyc", "new bytecode\n")
        rc, outcome, _payload = self.drive(
            session, [self.both()],
            [{"verdict": "approve", "corrective_findings": []}])
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(session.record["promotion_history"], [])


class AssuranceCadenceTests(_RoleLoopCase):
    def test_everything_reruns_and_deferred_notes_stop_the_phase(self):
        session = self.profile_session("assurance")
        self.assertIsNone(session.reuse_policy())

        def first():
            self.write("docs/a.md", "a edited\n")
            self.write("docs/b.md", "b edited\n")

        rc, outcome, payload = self.drive(
            session, [first, lambda: self.write("docs/b.md", "b again\n")],
            [self.minor_revise(), self.approve_with_note()])
        self.assertEqual((rc, outcome), (0, "ended"))
        self.assertEqual(payload["kind"], "review_profile_rejected")
        self.assertEqual(payload["profile_rejected"], "deferred_notes_refused")
        self.assertEqual(
            self.runs(),
            ["doc a", "doc a", "doc b", "doc b", "index", "index", "unit",
             "unit"])
        # No retry of the reviewer: the rejection is not an unavailable
        # reviewer, and nothing was sent back.
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.reviewed), 2)
        failures = self.events("review.failure")
        self.assertEqual([f.get("profile_rejected") for f in failures],
                         ["deferred_notes_refused"])
        self.assertEqual(session.record["deferred_minor_notes"], [])
        self.assertEqual(session.record["counters"]["verification_reused"], 0)
        self.assertEqual(
            [t["final_suite_binding"]
             for t in self.events("verification.transaction")],
            ["ran_once", "ran_once"])


class ReportStampingTests(_FlowCase):
    def build(self, suid):
        return measure.build_record(suid)

    def test_a_profiled_session_carries_the_cohort_fields(self):
        suid = "S-" + uuid.uuid4().hex[:8]
        os.makedirs(state_store.session_assets_dir(suid))
        session = profiles.ProfileSession(
            profiles.new_record("light", "why", NOW),
            lambda r: state_store.write_execution_profile_record(suid, r),
            now_fn=lambda: NOW)
        session.on_intel_approved(intel_doc()["result"])
        session.on_transaction({
            "verdict": "green", "transaction_id": "t1",
            "snapshot": {"manifest_digest": "m"},
            "attempts": [{"label": "docs check", "exit_code": 0,
                          "evidence_state": "present"}],
            "evidence_reuse": [{"label": "unit", "kind": "final_suite",
                                "source_transaction_id": "t0",
                                "dependency_digest": "d"}],
            "dependency_digests": {"docs check": "d"}},
            [check_entry("docs check", ["docs/a.md"]),
             check_entry("unit", ["src/"], kind="final_suite")])
        session.on_build_approved(
            {"verdict": "approve", "corrective_findings": [],
             "deferred_minor_notes": [{"summary": "tidy"}]}, 1, "m")
        session.promote_explicit("standard")
        record = self.build(suid)
        view = record["execution_profile"]
        self.assertEqual((view["selected"], view["effective"]),
                         ("light", "standard"))
        self.assertEqual(view["promotion_count"], 1)
        self.assertEqual(view["promotion_history"][0]["reason_codes"],
                         ["explicit_request"])
        self.assertEqual(view["deferred_minor_note_count"], 1)
        self.assertEqual(view["batch_artifact_count"], 1)
        self.assertEqual(view["verification"], {"executed": 1, "reused": 1})
        self.assertIn("execution_profile", record["built_from"])
        text = "\n".join(cowork_report._section_execution_profile(record))
        self.assertIn("Execution profile", text)
        self.assertIn("selected=light  effective=standard", text)
        self.assertIn("promotions: 1", text)
        self.assertIn("executed=1  reused=1", text)

    def test_an_unprofiled_session_record_and_report_are_unchanged(self):
        suid = "S-" + uuid.uuid4().hex[:8]
        os.makedirs(state_store.session_assets_dir(suid))
        record = self.build(suid)
        self.assertNotIn("execution_profile", record)
        self.assertNotIn("execution_profile", record["built_from"])
        self.assertEqual(cowork_report._section_execution_profile(record), [])

    def test_transaction_cost_summaries_count_executed_and_reused(self):
        with_reuse = measure.owned_transaction_cost_summary({
            "transaction_id": "t", "attempts": [{"label": "a"}],
            "evidence_reuse": [{"label": "b"}, {"label": "c"}]})
        self.assertEqual((with_reuse["executed_entry_count"],
                          with_reuse["reused_entry_count"]), (1, 2))
        without = measure.owned_transaction_cost_summary({
            "transaction_id": "t", "attempts": [{"label": "a"}]})
        self.assertNotIn("executed_entry_count", without)
        self.assertNotIn("reused_entry_count", without)


if __name__ == "__main__":
    unittest.main()
