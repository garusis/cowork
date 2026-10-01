#!/usr/bin/env python3
"""Offline behavior checks for the Jev observational reports.

Pilots are seeded with synthetic capture records and the real observer and
client code driven by a scripted fake transport; every id, probability and
count is neutral and invented. Run through the offline harness with this
module's name as the explicit id.
"""

import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_jev_capture as cap  # noqa: E402
import cowork_jev_client as jc  # noqa: E402
import cowork_jev_observer as obs  # noqa: E402
import cowork_jev_report as rep  # noqa: E402
import test_cowork_jev_observer as ot  # noqa: E402

NOW_OPEN = "2026-10-15T00:00:00Z"
NOW_CLOSED = "2026-10-31T00:00:00Z"


class ScriptedTransport:
    def __init__(self, script):
        self.script = script
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, headers, body, timeout):
        data = json.loads(body)
        tag = data["state"].split("|", 1)[1]
        with self.lock:
            self.calls.append(tag)
        rule = self.script.get(tag, {})
        if "status" in rule:
            return rule["status"], b""
        ids = list(data["questions"])
        probs = rule.get("p", {})
        default = rule.get("default", 0.1)
        answers = {q: {"type": "noul", "noul": probs.get(q, default)}
                   for q in ids}
        return 200, json.dumps({
            "model": jc.MODEL, "answers": answers,
            "usage": {"input_tokens": 9000 * len(ids),
                      "output_tokens": 3}}).encode()


class ReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="jevrep-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "pilot")
        os.makedirs(self.root)
        ot.write_pilot_files(self.root, [])
        self.pilot = obs.load_pilot(self.root)
        self.assertIsNotNone(self.pilot)
        self.clock = ot.StepClock()
        self.script = {}
        self.transport = ScriptedTransport(self.script)
        obs.reset_overrides()
        obs.configure(transport=self.transport, clock=self.clock,
                      credential_provider=lambda: "fake-credential")
        self.addCleanup(obs.reset_overrides)

    def seed(self, ticket, session, units, comparator="verifiable",
             query=True, seal=True):
        """units: [(tag, kind, rank_int)]. Returns the candidate id."""
        cid = cap.candidate_id_for(ticket)
        capture_id = hashlib.sha256(
            (ticket + session).encode()).hexdigest()[:32]
        record = {"capture_id": capture_id, "candidate_id": cid, "units": [
            {"unit_id": "%s/%s" % (cid, tag), "unit_key": tag, "kind": kind,
             "statement": "statement %s" % tag, "rank": "%016x" % rank,
             "applicable": True, "not_applicable_reason": None,
             "state_text": "STATE|%s" % tag, "state_tokens_est": 5,
             "omission": None, "symbols": []}
            for tag, kind, rank in units]}
        directory = os.path.join(self.pilot.captures_dir, capture_id)
        os.makedirs(directory)
        with open(os.path.join(directory, "record.json"), "w") as fh:
            json.dump(record, fh)
        line = cap.record_acceptance(
            self.pilot.acceptance_dir,
            {"ok": True, "capture_id": capture_id, "ticket_ref": ticket,
             "session_ids": [session],
             "record": {"record_digest": "x", "candidate_id": cid,
                        "ticket_key": ticket, "cohort_id": "cohort-1"}},
            clock=self.clock)
        obs._append(self.pilot, {
            "schema": obs.SCHEMA_STATE, "session_id": session,
            "state": "captured", "exclusion_reason": None,
            "ticket_ref": ticket, "ticket_key": ticket, "candidate_id": cid,
            "capture_id": capture_id, "worktree_root": "/neutral/wt",
            "instrumentation_ms": 5,
            "acceptance_seq": line["acceptance_seq"],
            "accepted_at": line["accepted_at"]})
        if query:
            obs.run_query(self.pilot, cid, capture_id)
        if seal:
            self.seal(cid, comparator)
        return cid

    def seal(self, cid, comparator="verifiable"):
        obs._append(self.pilot, {
            "schema": obs.SCHEMA_ORDINARY, "candidate_id": cid,
            "sealed_at": self.clock(), "reviewed_raw_fingerprint": "f",
            "comparator_status": comparator, "verdict": "approve",
            "findings": [], "instrumentation_ms": 7})

    def duplicate_session(self, ticket, session):
        cid = cap.candidate_id_for(ticket)
        line = cap.record_acceptance(
            self.pilot.acceptance_dir,
            {"ok": True, "capture_id": "dd" * 8, "ticket_ref": ticket,
             "session_ids": [session],
             "record": {"record_digest": "x", "candidate_id": cid,
                        "ticket_key": ticket, "cohort_id": "cohort-1"}},
            clock=self.clock)
        obs._append(self.pilot, {
            "schema": obs.SCHEMA_STATE, "session_id": session,
            "state": "duplicate_ticket",
            "exclusion_reason": "duplicate_ticket", "candidate_id": cid,
            "acceptance_seq": line["acceptance_seq"],
            "accepted_at": line["accepted_at"]})

    def adjudicate(self, unit_id, **kw):
        begun = obs.begin_adjudication(self.pilot, unit_id)
        self.assertIn(begun["state"], ("started", "already_started"), begun)
        result = obs.submit_adjudication(
            self.pilot, ot.adjudication_record(unit_id, **kw))
        self.assertTrue(result["accepted"], result)

    def report(self, now=NOW_CLOSED, **kw):
        return rep.compute_report(self.pilot, now, **kw)


def seed_w3(case):
    """The protocol's W3 shape with neutral ids."""
    script = case.script
    script["C1u2"] = {"p": {"Q-SUF": 0.12, "Q-REQ-1": 0.91,
                            "Q-REQ-2": 0.20}}
    script["C1u5"] = {"p": {"Q-ERR-1": 0.90}}
    script["C3c1"] = {"p": {"Q-SUF": 0.83}}
    script["C3c2"] = {"status": 429}
    script["C3c3"] = {"default": 0.22, "p": {"Q-SUF": 0.20}}
    ranks = {"u1": 12, "u2": 27, "u3": 41, "u4": 58, "u5": 73, "u6": 90,
             "u7": 96}
    kinds = {"u2": "requirement"}
    c1 = case.seed("demo#1", "s-a", [
        ("C1" + t, kinds.get(t, "error_behavior"), r)
        for t, r in ranks.items()])
    case.duplicate_session("demo#1", "s-b")
    c2 = case.seed("demo#2", "s-c", [
        ("C2" + t, "error_behavior", r) for t, r in
        (("a", 8), ("b", 35), ("c", 60), ("d", 82))])
    c3 = case.seed("demo#3", "s-d", [
        ("C3c1", "requirement", 5), ("C3c2", "error_behavior", 30),
        ("C3c3", "error_behavior", 77)])
    uid = lambda c, tag: "%s/%s" % (c, tag)  # noqa: E731
    selection = obs.select_adjudication(case.pilot)
    case.assertEqual([s["selected"] for s in selection], [
        [uid(c1, "C1u1"), uid(c1, "C1u2")], [uid(c2, "C2a")],
        [uid(c3, "C3c3")]])
    case.adjudicate(uid(c1, "C1u1"), outcome="refuted")
    case.adjudicate(uid(c1, "C1u2"), symbol="export_report",
                    cls="missing_behavior", origin="introduced")
    case.adjudicate(uid(c2, "C2a"), symbol="load_config",
                    cls="swallowed_error", origin="preexisting")
    case.adjudicate(uid(c3, "C3c3"), outcome="unknown")
    return c1, c2, c3


class KnownFixtureTests(ReportCase):
    def test_w3_counts_costs_and_decision(self):
        c1, _c2, _c3 = seed_w3(self)
        report = self.report()
        m = report["metrics"]
        self.assertEqual((m["M1"]["n"], m["M1"]["d"]), (1, 1))
        self.assertEqual((m["M2"]["n"], m["M2"]["d"]), (1, 1))
        self.assertEqual((m["M3"]["overall"]["n"], m["M3"]["overall"]["d"]),
                         (1, 4))
        self.assertEqual((m["M3"]["alert"]["n"], m["M3"]["alert"]["d"]),
                         (0, 1))
        self.assertEqual((m["M3"]["no_alert"]["n"],
                          m["M3"]["no_alert"]["d"]), (1, 3))
        self.assertEqual((m["M4"]["n"], m["M4"]["d"]), (1, 2))
        self.assertEqual(m["M5"]["D_add"], 1)
        self.assertEqual(m["M5"]["defect_keys"],
                         ["demo#1@1|export_report|missing_behavior"])
        self.assertEqual((m["M5"]["candidates"]["n"],
                          m["M5"]["candidates"]["d"]), (1, 3))
        self.assertEqual(m["M6"]["introduced"],
                         {"detected_by_jev": 1, "not_detected": 0})
        self.assertEqual(m["M6"]["preexisting"],
                         {"detected_by_jev": 0, "not_detected": 1})
        self.assertEqual(m["M6"]["unknown"],
                         {"detected_by_jev": 0, "not_detected": 0})
        self.assertEqual(m["M7"]["queried"], 13)
        self.assertEqual((m["M7"]["service_failure"]["n"],
                          m["M7"]["service_failure"]["d"]), (1, 13))
        self.assertEqual(
            (m["M7"]["abstain"]["insufficient_evidence"]["n"],
             m["M7"]["abstain"]["insufficient_evidence"]["d"]), (1, 13))
        self.assertEqual(m["M7"]["outside_denominator"]["dropped_cap"], 1)
        self.assertEqual((m["M9"]["n"], m["M9"]["d"]), (3, 3))
        m10 = m["M10"]
        self.assertEqual(m10["candidates_eligible"], 3)
        self.assertEqual(m10["sessions_duplicate_ticket"], 1)
        self.assertEqual(m10["candidates_intervened"], [])
        self.assertEqual(m10["units_by_state"], {
            "alert": 2, "no_alert": 9, "dropped_cap": 1,
            "abstain:insufficient_evidence": 1, "service_failure": 1})
        counts = report["counts"]
        self.assertEqual(counts["candidates"], {
            "eligible": 3, "excluded": 0, "intervened": 0, "duplicate": 1,
            "queried": 3})
        self.assertEqual((counts["units_queried"], counts["adjudicated"],
                          counts["alerts_investigated"],
                          counts["confirmed_defects"],
                          counts["refutations"], counts["unknowns"],
                          counts["failures"], counts["exclusions"]),
                         (13, 4, 1, 2, 1, 1, 1, 0))
        # Costs: 12 known attempts summed once, one unknown kept null.
        m8 = m["M8"]
        self.assertEqual(m8["jev_attempts_known"], 12)
        self.assertEqual(len(m8["jev_attempts_unknown"]), 1)
        unknown = m8["jev_attempts_unknown"][0]
        self.assertIsNone(unknown["usd"])
        self.assertTrue(unknown["unit_id"].endswith("C3c2"))
        self.assertEqual(m8["jev_usd_known"], 0.017388)
        self.assertAlmostEqual(
            m8["jev_usd_conservative"],
            0.017388 + float(jc.RESERVATION_USD), places=12)
        self.assertEqual(m8["adjudication"]["units_known"], 4)
        self.assertEqual(m8["adjudication"]["units_unknown"], [])
        self.assertEqual(m8["adjudication"]["usage"],
                         {"tokens": 4000, "tool_calls": 12, "minutes": 8})
        self.assertEqual(
            report["inputs"], dict(report["inputs"], N_comp=3, A_det=1,
                                   NA_det=2, U=0.25, D_add=1, H=False))
        self.assertAlmostEqual(report["inputs"]["SF"], 1 / 13)
        decision = report["decision"]
        self.assertEqual(decision["outcome"], "INSUFFICIENT_EVIDENCE")
        gaps = {g["quantity"]: g["missing"] for g in decision["gaps"]}
        self.assertEqual(gaps, {"N_comp": 7, "A_det": 7, "NA_det": 6})
        self.assertEqual(report["trigger"]["trigger"], "time_30d")
        self.assertEqual(report["status"], "closed")
        self.assertIn(c1, report["case_ids"])

    def test_w1_known_cost_is_summed_once_across_replay(self):
        seed_w3(self)
        before = self.report()["metrics"]["M8"]["jev_usd_known"]
        records = obs._read_jsonl(self.pilot.client_registry)
        outcome = next(r for r in records if r["schema"] == jc.SCHEMA_OUTCOME
                       and r["unit_id"].endswith("C1u2"))
        self.assertEqual(outcome["cost"]["usd"], 0.001134)
        self.assertEqual(outcome["cost"]["input_tokens"], 27000)
        with open(self.pilot.client_registry, "a") as fh:
            fh.write(json.dumps(outcome) + "\n")
        after = self.report()["metrics"]["M8"]
        self.assertEqual(after["jev_usd_known"], before)
        self.assertEqual(after["jev_attempts_known"], 12)

    def test_w2_no_alert_case_lands_in_preexisting_cell(self):
        c2 = self.seed("demo#2", "s-c", [
            ("C2" + t, "error_behavior", r) for t, r in
            (("a", 8), ("b", 35), ("c", 60), ("d", 82))])
        selection = obs.select_adjudication(self.pilot)[0]
        self.assertEqual(selection["stratum_empty"], ["alert"])
        self.adjudicate(selection["selected"][0], symbol="load_config",
                        cls="swallowed_error", origin="preexisting")
        m = self.report()["metrics"]
        self.assertEqual(m["M5"]["D_add"], 0)
        self.assertEqual(m["M6"]["preexisting"]["not_detected"], 1)
        self.assertEqual((m["M4"]["n"], m["M4"]["d"]), (1, 1))
        self.assertEqual(m["M5"]["candidates"]["n"], 0)
        self.assertEqual(self.report()["metrics"]["M7"]["queried"], 4)
        self.assertEqual(c2, "demo#2@1")

    def test_multi_unit_candidate_counts_once_and_runs_are_deterministic(self):
        seed_w3(self)
        first = rep.to_json(self.report())
        second = rep.to_json(self.report())
        self.assertEqual(first, second)
        self.assertEqual(self.report()["counts"]["candidates"]["eligible"], 3)
        self.assertIn("jev_obs M1", rep.render_text(self.report()))


class PendingAdjudicationCostTests(ReportCase):
    def seeded(self):
        self.script["P1a"] = {"p": {"Q-REQ-1": 0.9}}
        self.seed("demo#1", "s-a", [("P1a", "requirement", 1),
                                    ("P1b", "error_behavior", 2)])
        selection = obs.select_adjudication(self.pilot)[0]
        self.assertEqual(len(selection["selected"]), 2)
        first, second = selection["selected"]
        self.adjudicate(first)
        self.assertEqual(obs.begin_adjudication(self.pilot, second)["state"],
                         "started")
        return first, second

    def test_in_flight_adjudication_is_pending_and_never_exact(self):
        first, second = self.seeded()
        m8 = self.report()["metrics"]["M8"]
        adj = m8["adjudication"]
        self.assertEqual(self.report()["metrics"]["M5"]["D_add"], 1)
        self.assertEqual(adj["units_known"], 1)
        self.assertEqual(adj["units_unknown"], [])
        self.assertEqual(adj["units_pending"], [{
            "unit_id": second, "usage": None, "reserved_tokens": 400000,
            "reason": "in_flight"}])
        self.assertEqual(adj["retained_reservation_tokens"], 400000)
        self.assertEqual(adj["usage"]["tokens"], 1000)
        self.assertFalse(adj["exact"])
        self.assertFalse(m8["cost_exact"])
        per_defect = m8["cost_per_additional_defect"]
        self.assertEqual(per_defect["adjudication_tokens"],
                         rep.NOT_COMPUTABLE)
        self.assertEqual(per_defect["jev_usd"], rep.NOT_COMPUTABLE)
        self.assertEqual(self.report()["pending"]["adjudications"],
                         [second])

    def test_interrupted_adjudication_is_unknown_with_reservation(self):
        first, second = self.seeded()
        obs.recover_adjudications(self.pilot)
        m8 = self.report()["metrics"]["M8"]
        adj = m8["adjudication"]
        self.assertEqual(adj["units_pending"], [])
        self.assertEqual(adj["units_unknown"], [{
            "unit_id": second, "usage": None, "reserved_tokens": 400000,
            "reason": "interrupted_unknown"}])
        self.assertEqual(adj["retained_reservation_tokens"], 400000)
        self.assertFalse(m8["cost_exact"])
        self.assertEqual(m8["cost_per_additional_defect"][
            "adjudication_tokens"], rep.NOT_COMPUTABLE)


class ExclusionTests(ReportCase):
    def test_unverifiable_comparator_leaves_comparison_metrics_only(self):
        self.seed("demo#1", "s-a", [("u1", "error_behavior", 1),
                                    ("u2", "requirement", 2)],
                  comparator="unverifiable_changed")
        report = self.report()
        m = report["metrics"]
        self.assertEqual(report["inputs"]["N_comp"], 0)
        self.assertEqual((m["M9"]["n"], m["M9"]["d"]), (0, 1))
        self.assertEqual(m["M7"]["queried"], 2)
        self.assertEqual(report["counts"]["candidates"]["eligible"], 1)

    def test_unverifiable_defect_is_not_additional_signal(self):
        cid = self.seed("demo#1", "s-a", [("u1", "error_behavior", 1),
                                          ("u2", "requirement", 2)],
                        comparator="unverifiable_unknown")
        selection = obs.select_adjudication(self.pilot)[0]
        self.adjudicate(selection["selected"][0])
        report = self.report()
        self.assertEqual(report["metrics"]["M5"]["D_add"], 0)
        self.assertEqual(report["metrics"]["M6"]["introduced"],
                         {"detected_by_jev": 0, "not_detected": 0})
        self.assertEqual(report["counts"]["confirmed_defects"], 1)
        self.assertEqual(report["metrics"]["M10"]["candidates_eligible"], 1)
        self.assertEqual(cid, "demo#1@1")

    def test_capture_verification_failure_is_excluded_with_reason(self):
        cid = self.seed("demo#1", "s-a", [("u1", "error_behavior", 1)])
        capture_id = obs.build_view(self.pilot)["candidates"][0]["capture_id"]
        obs._append(self.pilot, {
            "schema": obs.SCHEMA_STATE, "session_id": "s-a",
            "state": "excluded", "candidate_id": cid,
            "capture_id": capture_id,
            "exclusion_reason": "capture_verification_failed",
            "problems": ["record_digest_mismatch"]})
        report = self.report()
        m10 = report["metrics"]["M10"]
        self.assertEqual(m10["candidates_excluded"],
                         {"capture_verification_failed": 1})
        self.assertEqual(m10["capture_verification_failed"], [cid])
        self.assertEqual(m10["candidates_eligible"], 0)
        self.assertEqual(report["metrics"]["M7"]["queried"], 0)
        self.assertEqual(report["counts"]["exclusions"], 1)

    def test_intervened_candidate_is_its_own_group(self):
        cid = self.seed("demo#1", "s-a", [("u1", "error_behavior", 1)],
                        seal=False)
        obs.record_intervention(self.pilot, cid, "defect told to builder")
        self.seal(cid)
        report = self.report()
        m10 = report["metrics"]["M10"]
        self.assertEqual(m10["candidates_intervened"], [
            {"candidate_id": cid, "reasons": ["defect told to builder"]}])
        self.assertEqual(m10["candidates_eligible"], 1)
        self.assertEqual(report["metrics"]["M9"]["d"], 0)
        self.assertEqual(report["metrics"]["M7"]["queried"], 0)
        self.assertEqual(report["inputs"]["N_comp"], 0)


class CascadeTests(unittest.TestCase):
    BASE = {"N_comp": 10, "A_det": 8, "NA_det": 8, "U": 0.1, "SF": 0.05,
            "P": 0.60, "M4": 0.10, "D_add": 4, "p95": 40, "JC": 0.03,
            "H": False}

    def decide(self, **kw):
        return rep.cascade(dict(self.BASE, **kw))

    def test_protocol_decision_table_examples(self):
        self.assertEqual(self.decide()["outcome"], "RECOMMEND_ASSISTANCE")
        self.assertEqual(self.decide(P=0.20, D_add=2)["outcome"],
                         "STOP_OBSERVATION")
        self.assertEqual(self.decide(P=0.40, D_add=2)["outcome"],
                         "NO_RECOMMENDATION_CONTINUE")

    def test_hard_stop_and_insufficient(self):
        stop = self.decide(H=True)
        self.assertEqual((stop["outcome"], stop["step"], stop["reason"]),
                         ("STOP_OBSERVATION", 1, "hard_stop"))
        self.assertEqual(self.decide(N_comp=9)["outcome"],
                         "INSUFFICIENT_EVIDENCE")
        self.assertEqual(self.decide(U=0.5)["outcome"],
                         "INSUFFICIENT_EVIDENCE")

    def test_undefined_clause_is_false(self):
        nc = rep.NOT_COMPUTABLE
        self.assertEqual(self.decide(U=nc)["outcome"],
                         "INSUFFICIENT_EVIDENCE")
        self.assertEqual(self.decide(SF=nc)["outcome"],
                         "INSUFFICIENT_EVIDENCE")
        self.assertEqual(self.decide(P=nc)["outcome"],
                         "NO_RECOMMENDATION_CONTINUE")
        self.assertEqual(self.decide(P=nc, D_add=0)["outcome"],
                         "STOP_OBSERVATION")
        self.assertEqual(self.decide(JC=nc)["outcome"],
                         "NO_RECOMMENDATION_CONTINUE")
        self.assertEqual(self.decide(p95=nc)["outcome"],
                         "NO_RECOMMENDATION_CONTINUE")


class CloseTriggerTests(ReportCase):
    def test_trigger_selection_and_hard_stop(self):
        self.seed("demo#1", "s-a", [("u1", "error_behavior", 1)])
        self.seed("demo#2", "s-b", [("u1", "error_behavior", 1)])
        view = obs.build_view(self.pilot)
        self.assertIsNone(rep.effective_close(self.pilot, view, NOW_OPEN))
        deadline = rep.effective_close(self.pilot, view, NOW_CLOSED)
        self.assertEqual((deadline["trigger"], deadline["close_seq"]),
                         ("time_30d", 2))
        obs._stamp_close(self.pilot, "count", "2026-10-20T00:00:00Z",
                         close_seq=1)
        obs._stamp_close(self.pilot, "cap", "2026-10-25T00:00:00Z",
                         close_seq=2)
        view = obs.build_view(self.pilot)
        first = rep.effective_close(self.pilot, view, NOW_CLOSED)
        self.assertEqual((first["trigger"], first["close_seq"]),
                         ("count", 1))
        report = self.report()
        self.assertEqual(report["counts"]["candidates"]["eligible"], 1)
        self.assertEqual(report["metrics"]["M10"][
            "candidates_outside_cohort_window"], 1)
        obs.record_hard_stop(self.pilot, "credential_exposure",
                             "2026-10-03T00:00:00Z")
        stopped = self.report(NOW_OPEN)
        self.assertEqual(stopped["trigger"]["trigger"], "hard_stop")
        self.assertEqual(stopped["decision"]["outcome"], "STOP_OBSERVATION")
        self.assertEqual(stopped["decision"]["reason"], "hard_stop")
        self.assertIsNone(obs.record_hard_stop(self.pilot, "nonsense"))


class HardStopBoundaryTests(ReportCase):
    def test_equal_timestamp_acceptance_before_stop_stays_inside(self):
        obs.configure(clock=ot.ConstClock())
        self.clock = ot.ConstClock()
        c1 = self.seed("demo#1", "s-a", [("A1", "error_behavior", 1)])
        c2 = self.seed("demo#2", "s-b", [("B1", "error_behavior", 1)],
                       query=False, seal=False)
        unit = obs.select_adjudication(self.pilot)[0]["selected"][0]
        self.assertEqual(obs.begin_adjudication(self.pilot, unit)["state"],
                         "started")
        stop = obs.record_hard_stop(self.pilot, "budget_revocation")
        self.assertEqual(stop["close_seq"], 2)
        c3 = self.seed("demo#3", "s-c", [("C1", "error_behavior", 1)],
                       query=False, seal=False)

        report = self.report(NOW_OPEN)
        self.assertEqual((report["trigger"]["trigger"],
                          report["trigger"]["close_seq"]), ("hard_stop", 2))
        self.assertEqual(report["case_ids"], sorted([c1, c2]))
        self.assertNotIn(c3, report["case_ids"])
        self.assertEqual(report["counts"]["candidates"]["eligible"], 2)
        self.assertEqual(report["metrics"]["M10"][
            "candidates_outside_cohort_window"], 1)
        stamps = [s for s in obs.build_view(self.pilot)["stamps"]
                  if s["trigger"] == "hard_stop"]
        self.assertEqual(len(stamps), 1)
        self.assertEqual(report["decision"]["reason"], "hard_stop")
        self.assertIn(c2, report["pending"]["units"][0])

        closed = rep.close_cohort(self.pilot, NOW_OPEN)
        with open(closed["json"], "rb") as fh:
            original = fh.read()
        # An already-started adjudication still settles after the stop and
        # reaches the dated supplement without reopening the frozen report.
        obs.configure(clock=lambda: "2026-11-02T00:00:00Z")
        settled = obs.submit_adjudication(self.pilot,
                                          ot.adjudication_record(unit))
        self.assertTrue(settled["accepted"])
        supplement = rep.write_supplement(self.pilot, "2026-11-02T12:00:00Z")
        self.assertEqual(supplement["item_keys"], ["adjudication:" + unit])
        with open(closed["json"], "rb") as fh:
            self.assertEqual(fh.read(), original)

    def test_legacy_unstamped_stop_uses_documented_fallback(self):
        self.seed("demo#1", "s-a", [("A1", "error_behavior", 1)])
        obs._append(self.pilot, {"schema": obs.SCHEMA_HARD_STOP,
                                 "kind": "budget_revocation",
                                 "event_at": "2026-10-20T00:00:00Z"})
        view = obs.build_view(self.pilot)
        close = rep.effective_close(self.pilot, view, NOW_OPEN)
        self.assertEqual((close["trigger"], close["close_seq"]),
                         ("hard_stop", 1))


class ImmutableCloseTests(ReportCase):
    def test_close_is_immutable_and_late_results_go_to_supplements(self):
        c1 = self.seed("demo#1", "s-a", [("A1", "error_behavior", 1),
                                         ("A2", "requirement", 2)])
        c2 = self.seed("demo#2", "s-b", [("B1", "error_behavior", 1),
                                         ("B2", "requirement", 2)],
                       query=False, seal=False)
        self.assertEqual(rep.write_supplement(self.pilot, NOW_CLOSED),
                         {"created": False, "reason": "not_closed"})
        closed = rep.close_cohort(self.pilot, NOW_CLOSED)
        self.assertTrue(closed["created"])
        with open(closed["json"], "rb") as fh:
            original = fh.read()
        original_report = json.loads(original)
        self.assertEqual(original_report["closed_at"], NOW_CLOSED)
        self.assertEqual(sorted(original_report["pending"]["units"]),
                         sorted(["%s/B1" % c2, "%s/B2" % c2]))
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         closed["sha256"])
        again = rep.close_cohort(self.pilot, "2026-11-05T00:00:00Z")
        self.assertFalse(again["created"])

        # Results arrive after the close.
        self.clock.t = datetime.datetime(2026, 11, 2, 0, 0, 0)
        obs.run_query(self.pilot, c2, obs.build_view(self.pilot)[
            "by_id"][c2]["capture_id"])
        self.seal(c2)
        now = "2026-11-02T12:00:00Z"
        first = rep.write_supplement(self.pilot, now)
        self.assertTrue(first["created"])
        self.assertTrue(first["path"].endswith(
            "cohort_1_supplement_2026-11-02.json"))
        self.assertTrue(any(k.startswith("unit:") for k in
                            first["item_keys"]))
        self.assertTrue(any(k.startswith("attempt:") for k in
                            first["item_keys"]))
        self.assertFalse(any(c1 in k for k in first["item_keys"]))
        self.assertEqual(rep.write_supplement(self.pilot, now),
                         {"created": False, "reason": "nothing_new"})

        # A second batch on the same date gets its own file.
        selection = [s for s in obs.select_adjudication(self.pilot)
                     if s["candidate_id"] == c2][0]
        self.adjudicate(selection["selected"][0])
        second = rep.write_supplement(self.pilot, now)
        self.assertTrue(second["path"].endswith(
            "cohort_1_supplement_2026-11-02_2.json"))
        self.assertTrue(all(k.startswith("adjudication:")
                            for k in second["item_keys"]))
        note = {"candidate_id": c1, "note": "regression seen after close"}
        third = rep.write_supplement(self.pilot, now,
                                     regression_notes=[note])
        self.assertTrue(third["path"].endswith("_3.json"))
        with open(third["path"]) as fh:
            body = json.load(fh)
        self.assertEqual(body["items"][0]["observed_on"], "2026-11-02")
        self.assertEqual(rep.write_supplement(
            self.pilot, now, regression_notes=[note])["created"], False)

        # The closed report is untouched and no case was added.
        with open(closed["json"], "rb") as fh:
            self.assertEqual(fh.read(), original)
        with open(closed["json"].replace(".json", ".sha256")) as fh:
            self.assertEqual(fh.read().strip(), closed["sha256"])
        self.assertEqual(original_report["case_ids"], sorted([c1, c2]))
        frozen = rep.compute_report(self.pilot, NOW_CLOSED,
                                    as_of=NOW_CLOSED)
        self.assertEqual(frozen["pending"], original_report["pending"])
        self.assertEqual(frozen["metrics"], original_report["metrics"])
        for path in (first["path"], second["path"], third["path"]):
            with open(path) as fh:
                for item in json.load(fh)["items"]:
                    self.assertIn(item["candidate_id"],
                                  original_report["case_ids"])


if __name__ == "__main__":
    unittest.main()
