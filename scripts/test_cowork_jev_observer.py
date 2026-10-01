#!/usr/bin/env python3
"""Offline behavior checks for the Jev observer.

Every case uses a throwaway git repository, a fake transport, fake credentials
and neutral invented content; nothing touches the network. Run through the
offline harness with this module's name as the explicit id.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_jev_capture as cap  # noqa: E402
import cowork_jev_client as jc  # noqa: E402
import cowork_jev_observer as obs  # noqa: E402
import test_cowork_jev_capture as capt  # noqa: E402

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
FAKE_CRED_ENV = "JEV_TEST_FAKE_CRED"


class StepClock:
    def __init__(self, start="2026-10-02T00:00:00Z"):
        self.t = datetime.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.t += datetime.timedelta(seconds=1)
            return self.t.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeTransport:
    def __init__(self, probs=None, status=200):
        self.probs = probs or {}
        self.status = status
        self.gate = None
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, headers, body, timeout):
        with self.lock:
            self.calls.append(body)
        if self.gate is not None:
            self.gate.wait(10)
        if self.status != 200:
            return self.status, b""
        ids = json.loads(body)["questions"]
        answers = {q: {"type": "noul", "noul": self.probs.get(q, 0.1)}
                   for q in ids}
        return 200, json.dumps({
            "model": jc.MODEL, "answers": answers,
            "usage": {"input_tokens": 9000, "output_tokens": 5}}).encode()


REPO_NAME = "neutral-repo"


def entry_list(sessions, repo, base=None, repository=REPO_NAME):
    entries = []
    for sid, ref in sessions.items():
        entry = {"session_id": sid, "ticket_ref": ref,
                 "objective_text": capt.OBJECTIVE,
                 "requirement_text": capt.REQUIREMENT,
                 "repository": repository,
                 "repo_root": os.path.realpath(repo)}
        if base:
            entry["base_ref"] = base
        entries.append(entry)
    return entries


def write_pilot_files(root, entries, **act_kw):
    """Inclusion list plus an activation bound to its digest."""
    with open(os.path.join(root, "inclusion_list.json"), "w") as fh:
        json.dump(entries, fh)
    with open(os.path.join(root, "cohort_activation.json"), "w") as fh:
        json.dump(activation(inclusion_digest=obs.inclusion_digest(entries),
                             **act_kw), fh)


def activation(activated_at="2026-10-01T00:00:00Z", jev_caps=None,
               adj_caps=None, inclusion_digest="0" * 64):
    jev = {"candidates_per_cohort": 20, "max_units_queried": 120,
           "proposed_cap_usd": 2}
    jev.update(jev_caps or {})
    adj = {"cohort_caps": {"tokens": 16000000, "tool_calls": 1200,
                           "wall_minutes": 800},
           "units_cap": 40,
           "per_unit_envelope": {"tokens": 400000, "tool_calls": 30,
                                 "minutes": 20}}
    adj.update(adj_caps or {})
    return {"schema": "cohort_activation.v1", "cohort_id": "cohort-1",
            "cohort_number": 1, "activated_at": activated_at,
            "model": jc.MODEL, "question_set": jc.QUESTION_SET,
            "question_set_digest": jc.QUESTION_SET_DIGEST,
            "thresholds": dict(obs.THRESHOLDS), "salt": "jev-obs-q1/cohort-1",
            "caps": {"jev": jev, "adjudication": adj},
            "adjudicator": {"model_id": "fake-model",
                            "provider": "fake-provider",
                            "brief_version": obs.BRIEF_VERSION},
            "authorizations": {
                "budget_ref": "budget-1",
                "credential_source_ref": FAKE_CRED_ENV,
                "data_scope": {"repositories": [REPO_NAME]},
                "recipients": ["TypeSafe", "fake-provider"],
                "vendor_retention_status": "non_zero_assumed",
                "docs_recheck": {"checked_at": "2026-09-30T00:00:00Z",
                                 "price_usd_per_million_input": "0.042",
                                 "limits_recorded": True}},
            "inclusion_list_digest": inclusion_digest}


def adjudication_record(unit_id, outcome="confirmed",
                        symbol="export_report", cls="missing_behavior",
                        origin="introduced", tokens=1000):
    rec = {"schema": "jev_adjudication.v1", "unit_id": unit_id,
           "adjudicator": {"model_id": "fake-model", "session_ref": "ref-1"},
           "outcome": outcome,
           "evidence": [{"capture_ref": "capture", "rel_path": "app.py",
                         "line_range": [1, 2], "explanation": "shown"}],
           "usage": {"tool_calls": 3, "minutes": 2, "tokens": tokens},
           "finished_at": "2026-10-03T00:00:00Z"}
    if outcome == "confirmed":
        rec.update(trigger={"input": "read-only dir", "expected": "exit 1",
                            "actual": "exit 0"}, primary_symbol=symbol,
                   defect_class=cls, origin=origin)
    return rec


class ObsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="jevobs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "pilot")
        os.makedirs(self.root)
        self.repo = os.path.join(self.tmp, "wt")
        self.base = capt.make_repo(self.repo, {"app.py": capt.BASE_APP,
                                               "README.md": "docs\n"})
        capt.write(self.repo, "app.py", capt.NEW_APP)
        self.clock = StepClock()
        self.transport = FakeTransport()
        self.jobs = []
        patcher = mock.patch.dict(os.environ, {
            obs.PILOT_ENV: self.root, FAKE_CRED_ENV: "fake-credential"})
        patcher.start()
        self.addCleanup(patcher.stop)
        obs.reset_overrides()
        obs.configure(transport=self.transport, clock=self.clock,
                      thread_starter=self.jobs.append)
        self.addCleanup(obs.reset_overrides)

    def write_pilot(self, sessions=None, with_base=True, **act_kw):
        sessions = sessions or {"sess-a": "Demo#1"}
        write_pilot_files(self.root, entry_list(
            sessions, self.repo, self.base if with_base else None),
            **act_kw)
        self.pilot = obs.load_pilot(self.root)
        self.assertIsNotNone(self.pilot)
        return self.pilot

    def promote(self, session="sess-a"):
        return obs.hook_promoted(session, self.repo)

    def run_jobs(self):
        while self.jobs:
            self.jobs.pop(0)()

    def review(self, token, verdict=None, between=None):
        obs.hook_review_start(token, self.repo)
        if between:
            between()
        return obs.hook_review_sealed(
            token, self.repo, verdict or {"verdict": "approve",
                                          "findings": []})

    def cand(self):
        return obs.build_view(self.pilot)["candidates"][0]

    def records(self, schema):
        return [r for r in obs._records(self.pilot)
                if r.get("schema") == schema]

    def tamper(self, capture_id):
        path = os.path.join(self.pilot.captures_dir, capture_id,
                            "record.json")
        os.chmod(path, 0o644)
        with open(path) as fh:
            rec = json.load(fh)
        rec["objective_text"] = "tampered"
        with open(path, "w") as fh:
            json.dump(rec, fh)

    def sealed_candidate(self, probs=None, **kw):
        self.transport.probs = probs or {}
        self.write_pilot(**kw)
        token = self.promote()
        self.run_jobs()
        self.review(token)
        return token

    def adjudicate(self, unit_id, **kw):
        begun = obs.begin_adjudication(self.pilot, unit_id)
        self.assertIn(begun["state"], ("started", "already_started"), begun)
        return obs.submit_adjudication(
            self.pilot, adjudication_record(unit_id, **kw))


class InertTests(ObsCase):
    def test_unset_env_is_inert(self):
        self.write_pilot()
        with mock.patch.dict(os.environ):
            os.environ.pop(obs.PILOT_ENV)
            self.assertIsNone(obs.hook_promoted("sess-a", self.repo))
            self.assertIsNone(obs.hook_review_start({}, self.repo))
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(os.path.exists(self.pilot.registry_path))
        self.assertFalse(os.path.exists(self.pilot.captures_dir))

    def test_invalid_activation_is_inert(self):
        self.write_pilot()
        path = os.path.join(self.root, "cohort_activation.json")
        for mutate in (lambda a: a["caps"].pop("jev"),
                       lambda a: a.update(model="other"),
                       lambda a: a["caps"]["adjudication"].update(
                           per_unit_envelope={"tokens": 1}),
                       lambda a: a.update(thresholds={})):
            act = activation()
            mutate(act)
            with open(path, "w") as fh:
                json.dump(act, fh)
            self.assertIsNone(obs.load_pilot(self.root))
            self.assertIsNone(obs.hook_promoted("sess-a", self.repo))
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(os.path.exists(os.path.join(
            self.root, "observer_registry.jsonl")))

    def test_hook_exception_is_swallowed_and_logged(self):
        self.write_pilot()
        with mock.patch.object(obs.cap, "capture_candidate",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(self.promote())
        self.assertEqual(self.records(obs.SCHEMA_ERROR)[0]["where"],
                         "hook_promoted")


class PromotionTests(ObsCase):
    def test_capture_query_and_link(self):
        self.write_pilot()
        token = self.promote()
        self.assertEqual(token["candidate_id"], "demo#1@1")
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.transport.calls, [])
        self.run_jobs()
        cand = self.cand()
        self.assertTrue(cand["final"])
        applicable = [u for u in cand["units"] if u["applicable"]]
        self.assertEqual(len(self.transport.calls), len(applicable))
        self.assertTrue(all(u["status"] == "no_alert" for u in applicable))
        link = self.records(obs.SCHEMA_LINK)[0]
        self.assertEqual(set(link), {"schema", "candidate_id", "capture_id",
                                     "unit_ids", "at"})

    def test_once_per_session(self):
        self.write_pilot()
        self.assertIsNotNone(self.promote())
        self.assertIsNone(self.promote())
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len([s for s in self.records(obs.SCHEMA_STATE)
                              if s["state"] == "captured"]), 1)

    def test_not_in_inclusion_list(self):
        self.write_pilot()
        self.assertIsNone(self.promote("sess-unknown"))
        self.assertEqual(self.jobs, [])
        state = self.records(obs.SCHEMA_STATE)[0]
        self.assertEqual(state["exclusion_reason"], "not_in_inclusion_list")

    def test_duplicate_ticket_is_never_queried(self):
        self.write_pilot({"sess-a": "Demo#1", "sess-b": "Demo#1"})
        self.assertIsNotNone(self.promote("sess-a"))
        self.assertIsNone(self.promote("sess-b"))
        self.assertEqual(len(self.jobs), 1)
        states = {s["session_id"]: s["state"]
                  for s in self.records(obs.SCHEMA_STATE)}
        self.assertEqual(states, {"sess-a": "captured",
                                  "sess-b": "duplicate_ticket"})
        self.run_jobs()
        queried = len(self.transport.calls)
        self.assertGreater(queried, 0)
        self.assertEqual(len(obs.build_view(self.pilot)["candidates"]), 1)

    def test_outside_cohort_window(self):
        self.write_pilot(activated_at="2026-12-01T00:00:00Z")
        self.assertIsNone(self.promote())
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.records(obs.SCHEMA_STATE)[0]["state"],
                         "outside_cohort_window")

    def test_twentieth_candidate_stamps_close_seq(self):
        self.write_pilot({"sess-a": "Demo#1", "sess-b": "Demo#2",
                          "sess-c": "Demo#3"},
                         jev_caps={"candidates_per_cohort": 2})
        self.assertIsNotNone(self.promote("sess-a"))
        self.assertEqual(self.records(obs.SCHEMA_STAMP), [])
        self.assertIsNotNone(self.promote("sess-b"))
        stamp = self.records(obs.SCHEMA_STAMP)[0]
        self.assertEqual((stamp["trigger"], stamp["close_seq"]), ("count", 2))
        self.assertIsNone(self.promote("sess-c"))
        self.assertEqual(self.records(obs.SCHEMA_STATE)[-1]["state"],
                         "outside_cohort_window")

    def test_omitted_units_get_zero_transport_calls(self):
        self.write_pilot()
        token = self.promote()
        path = os.path.join(self.pilot.captures_dir, token["capture_id"],
                            "record.json")
        os.chmod(path, 0o644)
        with open(path) as fh:
            rec = json.load(fh)
        omitted = rec["units"][0]
        omitted["omission"] = "data_withheld"
        omitted["state_text"] = ""
        with open(path, "w") as fh:
            json.dump(rec, fh)
        self.run_jobs()
        applicable = [u for u in rec["units"] if u["applicable"]]
        self.assertEqual(len(self.transport.calls), len(applicable) - 1)
        statuses = {u["unit_id"]: u for u in self.cand()["units"]}
        unit = statuses[omitted["unit_id"]]
        self.assertEqual((unit["status"], unit["abstain_reason"]),
                         ("abstain", "data_withheld"))

    def test_review_not_blocked_by_transport(self):
        self.transport.gate = threading.Event()
        obs.configure(thread_starter=None)
        self.write_pilot()
        token = self.promote()
        sealed = self.review(token)
        self.assertIsNotNone(sealed)
        worker = obs._WORKERS[token["candidate_id"]]
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.records(obs.SCHEMA_RESULT), [])
        self.transport.gate.set()
        obs.join_workers(20)
        self.assertEqual(len(self.records(obs.SCHEMA_RESULT)), 1)
        self.assertTrue(self.cand()["final"])


class ComparatorTests(ObsCase):
    def sealed(self, between=None, start=True, before_start=None):
        self.write_pilot()
        token = self.promote()
        self.run_jobs()
        if before_start:
            before_start()
        if start:
            obs.hook_review_start(token, self.repo)
        if between:
            between()
        record = obs.hook_review_sealed(token, self.repo,
                                        {"verdict": "approve"})
        return token, record

    def edit(self):
        capt.write(self.repo, "app.py", capt.NEW_APP + "# edit\n")

    def test_verifiable(self):
        _token, record = self.sealed()
        self.assertEqual(record["comparator_status"], "verifiable")

    def test_changed_before_review(self):
        _token, record = self.sealed(before_start=self.edit)
        self.assertEqual(record["comparator_status"], "unverifiable_changed")
        self.assertTrue(self.cand()["final"])

    def test_changed_during_review(self):
        _token, record = self.sealed(between=self.edit)
        self.assertEqual(record["comparator_status"], "unverifiable_changed")
        self.assertEqual(self.cand()["comparator"], "unverifiable_changed")

    def test_missing_reviewed_fingerprint(self):
        _token, record = self.sealed(start=False)
        self.assertEqual(record["comparator_status"], "unverifiable_unknown")

    def test_tampered_capture_is_excluded_with_reason(self):
        self.write_pilot()
        token = self.promote()
        self.run_jobs()
        self.tamper(token["capture_id"])
        obs.hook_review_start(token, self.repo)
        obs.hook_review_sealed(token, self.repo, {"verdict": "approve"})
        cand = self.cand()
        self.assertEqual(cand["excluded"], "capture_verification_failed")
        self.assertTrue(cand["excluded_problems"])
        self.assertIsNotNone(cand["ordinary"])

    def test_second_ordinary_record_refused(self):
        token, first = self.sealed()
        again = obs.hook_review_sealed(token, self.repo,
                                       {"verdict": "revise"})
        self.assertIsNone(again)
        records = self.records(obs.SCHEMA_ORDINARY)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["verdict"], "approve")

    def test_comparator_function(self):
        self.assertEqual(obs.comparator_status("a", "a", "a"), "verifiable")
        self.assertEqual(obs.comparator_status("a", "a", "b"),
                         "unverifiable_changed")
        self.assertEqual(obs.comparator_status("a", None, "a"),
                         "unverifiable_unknown")
        self.assertEqual(obs.comparator_status(None, "a", "a"),
                         "unverifiable_unknown")


class SuspensionRecoveryTests(ObsCase):
    def test_suspended_before_promotion_captures_nothing(self):
        self.write_pilot()
        obs.suspend(self.pilot)
        self.assertIsNone(self.promote())
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.records(obs.SCHEMA_STATE)[0][
            "exclusion_reason"], "observer_suspended")

    def test_suspend_then_resume_never_sends(self):
        self.write_pilot()
        token = self.promote()
        obs.suspend(self.pilot)
        self.run_jobs()
        self.review(token)
        self.assertEqual(self.transport.calls, [])
        self.assertIsNotNone(self.cand()["ordinary"])
        result = obs.resume(self.pilot)
        self.assertEqual(result["abandoned"], [token["candidate_id"]])
        self.assertEqual(obs.resume(self.pilot)["abandoned"], [])
        self.assertEqual(self.transport.calls, [])
        cand = self.cand()
        self.assertTrue(cand["final"])
        self.assertTrue(all(u["status"] == obs.NOT_QUERIED_INTERRUPTED
                            for u in cand["units"]))
        selection = obs.select_adjudication(self.pilot)[0]
        self.assertEqual(selection["selected"], [])
        self.assertEqual(sorted(selection["stratum_empty"]),
                         ["alert", "no_alert"])

    def test_recovery_is_idempotent_and_keeps_reservation(self):
        self.write_pilot()
        token = self.promote()
        unit_id = cap.load_capture(self.pilot.captures_dir,
                                   token["capture_id"])["units"][0]["unit_id"]
        os.makedirs(self.pilot.jev_dir, exist_ok=True)
        store = jc.FileStore(self.pilot.jev_dir)
        store.append({"schema": jc.SCHEMA_STARTED, "cohort_id": "cohort-1",
                      "candidate_id": token["candidate_id"],
                      "unit_id": unit_id, "attempt_id": "a1",
                      "request_digest": "d1", "state_tokens_est": 10,
                      "reserved_tokens": jc.RESERVATION_TOKENS,
                      "reserved_usd": str(jc.RESERVATION_USD),
                      "started_at": "2026-10-02T00:00:00Z"})
        first = obs.recover(self.pilot)
        second = obs.recover(self.pilot)
        self.assertEqual(first["recovered_attempts"], 1)
        self.assertEqual(second["recovered_attempts"], 0)
        outcomes = [r for r in store.records()
                    if r["schema"] == jc.SCHEMA_OUTCOME]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["response"]["failure_class"],
                         "interrupted_unknown")
        self.assertIsNone(outcomes[0]["cost"]["usd"])
        self.assertEqual(outcomes[0]["committed_usd"],
                         str(jc.RESERVATION_USD))
        self.assertEqual(self.transport.calls, [])

    def test_recover_skips_while_lock_held(self):
        self.write_pilot()
        os.makedirs(self.pilot.jev_dir, exist_ok=True)
        with obs._locked(self.pilot.cohort_lock) as held:
            self.assertTrue(held)
            self.assertEqual(obs.recover(self.pilot),
                             {"skipped": "live_attempt"})
            code = ("import sys, json; sys.path.insert(0, %r); "
                    "import cowork_jev_observer as o; "
                    "print(json.dumps(o.recover(o.load_pilot(%r))))"
                    % (SCRIPTS, self.root))
            out = subprocess.run([sys.executable, "-c", code],
                                 capture_output=True, text=True)
            self.assertEqual(json.loads(out.stdout),
                             {"skipped": "live_attempt"})
        self.assertNotIn("skipped", obs.recover(self.pilot))

    def test_budget_reservations_are_atomic_across_threads(self):
        self.write_pilot({"sess-a": "Demo#1", "sess-b": "Demo#2",
                          "sess-c": "Demo#3"},
                         jev_caps={"proposed_cap_usd": 0.005})
        for sid in ("sess-a", "sess-b", "sess-c"):
            self.assertIsNotNone(self.promote(sid))
        # One reservation (about 0.0041 USD) fits the cap, two never do while
        # the first attempt is still in flight.
        self.transport.gate = threading.Event()
        threads = [threading.Thread(target=job) for job in self.jobs]
        for thread in threads:
            thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not any(
                r["schema"] == jc.SCHEMA_CLOSED
                for r in obs._read_jsonl(self.pilot.client_registry)):
            time.sleep(0.01)
        self.transport.gate.set()
        for thread in threads:
            thread.join(30)
        records = obs._read_jsonl(self.pilot.client_registry)
        started = [r for r in records if r["schema"] == jc.SCHEMA_STARTED]
        closed = [r for r in records if r["schema"] == jc.SCHEMA_CLOSED]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(closed), 1)
        self.assertEqual(len(self.transport.calls), 1)
        view = obs.build_view(self.pilot)
        statuses = [u["status"] for c in view["candidates"]
                    for u in c["units"]]
        self.assertIn("not_queried_cap", statuses)
        stamp = self.records(obs.SCHEMA_STAMP)[0]
        self.assertEqual((stamp["trigger"], stamp["close_seq"]), ("cap", 3))


class AdjudicationTests(ObsCase):
    def units(self):
        return sorted(self.cand()["units"], key=obs._rank_key)

    def test_selection_waits_for_seal_and_is_stable(self):
        self.transport.probs = {"Q-REQ-1": 0.9}
        self.write_pilot()
        token = self.promote()
        self.run_jobs()
        pending = obs.select_adjudication(self.pilot)[0]
        self.assertTrue(pending["pending"])
        self.assertEqual(pending["selected"], [])
        self.review(token)
        first = obs.select_adjudication(self.pilot)[0]
        statuses = {u["unit_id"]: u["status"] for u in self.units()}
        self.assertEqual(sorted(statuses.values()), ["alert", "no_alert"])
        self.assertEqual(first["selected"],
                         [u["unit_id"] for u in self.units()])
        self.assertEqual(first["stratum_empty"], [])
        self.assertEqual(obs.select_adjudication(self.pilot)[0]["selected"],
                         first["selected"])
        self.assertEqual(len(self.records(obs.SCHEMA_SELECTION)), 1)

    def test_empty_stratum_is_recorded_without_backfill(self):
        self.sealed_candidate()
        selection = obs.select_adjudication(self.pilot)[0]
        self.assertEqual(selection["stratum_empty"], ["alert"])
        self.assertEqual(len(selection["selected"]), 1)
        self.assertEqual(selection["selected"][0], self.units()[0]["unit_id"])

    def test_failed_units_are_never_selected(self):
        self.transport.status = 429
        self.write_pilot()
        token = self.promote()
        self.run_jobs()
        self.review(token)
        selection = obs.select_adjudication(self.pilot)[0]
        self.assertEqual(selection["selected"], [])
        self.assertEqual(sorted(selection["stratum_empty"]),
                         ["alert", "no_alert"])

    def test_cohort_unit_cap(self):
        self.sealed_candidate(probs={"Q-REQ-1": 0.9},
                              adj_caps={"units_cap": 1})
        selection = obs.select_adjudication(self.pilot)[0]
        self.assertEqual(len(selection["selected"]), 1)
        self.assertEqual(len(selection["not_selected"]), 1)

    def test_begin_refused_before_ordinary_record(self):
        self.transport.probs = {"Q-REQ-1": 0.9}
        self.write_pilot()
        self.promote()
        self.run_jobs()
        unit_id = self.units()[0]["unit_id"]
        self.assertEqual(obs.begin_adjudication(self.pilot, unit_id),
                         {"state": "refused", "reason":
                          "no_ordinary_record"})

    def test_begin_reserves_envelope_once_and_respects_cap(self):
        self.sealed_candidate(probs={"Q-REQ-1": 0.9},
                              adj_caps={"cohort_caps": {
                                  "tokens": 500000, "tool_calls": 1200,
                                  "wall_minutes": 800}})
        first, second = [u["unit_id"] for u in self.units()]
        begun = obs.begin_adjudication(self.pilot, first)
        self.assertEqual(begun["state"], "started")
        self.assertEqual(obs.begin_adjudication(self.pilot, first)["state"],
                         "already_started")
        view = obs.build_view(self.pilot)
        self.assertEqual(obs._consumed(view)["tokens"], 400000)
        refused = obs.begin_adjudication(self.pilot, second)
        self.assertEqual(refused, {"state": "refused",
                                   "reason": "cap_reached"})
        self.assertEqual(self.records(obs.SCHEMA_STAMP)[0]["trigger"],
                         "adjudication_cap")

    def test_unselected_unit_is_refused(self):
        self.sealed_candidate()
        unit_id = self.units()[1]["unit_id"]
        self.assertEqual(obs.begin_adjudication(self.pilot, unit_id)[
            "reason"], "not_selected")

    def test_submit_validation_and_immutable_first_outcome(self):
        self.sealed_candidate(probs={"Q-REQ-1": 0.9})
        unit_id = self.units()[0]["unit_id"]
        obs.begin_adjudication(self.pilot, unit_id)
        bad_cases = []
        no_trigger = adjudication_record(unit_id)
        del no_trigger["trigger"]
        bad_cases.append(no_trigger)
        bad_class = adjudication_record(unit_id, cls="nonsense")
        bad_cases.append(bad_class)
        no_evidence = adjudication_record(unit_id, outcome="refuted")
        no_evidence["evidence"] = []
        bad_cases.append(no_evidence)
        over = adjudication_record(unit_id, tokens=400001)
        bad_cases.append(over)
        for bad in bad_cases:
            result = obs.submit_adjudication(self.pilot, bad)
            self.assertFalse(result["accepted"], bad)
        self.assertEqual(self.records(obs.SCHEMA_ADJ_OUTCOME), [])
        good = obs.submit_adjudication(self.pilot,
                                       adjudication_record(unit_id))
        self.assertTrue(good["accepted"])
        again = obs.submit_adjudication(
            self.pilot, adjudication_record(unit_id, outcome="refuted"))
        self.assertEqual(again["problems"], ["duplicate_outcome"])
        unknown_unit = obs.submit_adjudication(
            self.pilot, adjudication_record("nope/x"))
        self.assertFalse(unknown_unit["accepted"])

    def test_interrupted_adjudication_is_unknown_and_never_restarted(self):
        self.sealed_candidate(probs={"Q-REQ-1": 0.9})
        unit_id = self.units()[0]["unit_id"]
        obs.begin_adjudication(self.pilot, unit_id)
        self.assertEqual(obs.recover_adjudications(self.pilot), [unit_id])
        self.assertEqual(obs.recover_adjudications(self.pilot), [])
        out = self.records(obs.SCHEMA_ADJ_OUTCOME)[0]
        self.assertEqual((out["outcome"], out["charge_status"], out["usage"]),
                         ("unknown", "unknown", None))
        self.assertIsNone(out["cost"]["usd"])
        view = obs.build_view(self.pilot)
        self.assertEqual(obs._consumed(view)["tokens"], 400000)
        late = obs.submit_adjudication(self.pilot,
                                       adjudication_record(unit_id))
        self.assertEqual(late["problems"], ["duplicate_outcome"])
        self.assertEqual(obs.begin_adjudication(self.pilot, unit_id)["state"],
                         "already_started")


class BriefTests(ObsCase):
    def selected(self):
        sel = obs.select_adjudication(self.pilot)[0]
        return sel["selected"]

    def test_brief_is_blind_and_sanitized(self):
        self.sealed_candidate(probs={"Q-REQ-1": 0.9})
        record = cap.load_capture(
            self.pilot.captures_dir,
            self.cand()["capture_id"])
        briefs = []
        for position, unit_id in enumerate(self.selected(), 1):
            built = obs.build_brief(self.pilot, "demo#1@1", unit_id,
                                    position)
            self.assertIn("brief", built, built)
            briefs.append(built["brief"])
        dump = json.dumps(briefs)
        for forbidden in ("sess-a", self.repo,
                          record["raw_content_fingerprint"],
                          record["base_raw_fingerprint"], "sha256"):
            self.assertNotIn(forbidden, dump)
        for unit in record["units"]:
            self.assertNotIn(unit["rank"], dump)
        for brief in briefs:
            self.assertEqual(set(brief["unit"]), {"kind", "statement"})
            self.assertEqual(brief["envelope"], {
                "max_tool_calls": 30, "max_minutes": 20,
                "max_total_tokens": 400000})
            self.assertEqual(brief["capture_ref"]["base_evidence"],
                             "present")
            self.assertNotRegex(json.dumps(obs._observer_authored(brief)),
                                r"(?i)alert")
        first, second = briefs
        self.assertEqual(set(first), set(second))
        self.assertEqual(set(first["capture_ref"]),
                         set(second["capture_ref"]))
        self.assertIn("app.py", first["capture_ref"]["file_texts"])

    def test_missing_base_is_explicit(self):
        self.sealed_candidate(with_base=False)
        unit_id = self.selected()[0]
        brief = obs.build_brief(self.pilot, "demo#1@1", unit_id, 1)["brief"]
        self.assertEqual(brief["capture_ref"]["base_evidence"], "missing")
        self.assertIn("origin unknown", brief["capture_ref"]["origin_note"])

    def test_tampered_capture_is_refused_and_excluded(self):
        self.sealed_candidate()
        unit_id = self.selected()[0]
        self.tamper(self.cand()["capture_id"])
        built = obs.build_brief(self.pilot, "demo#1@1", unit_id, 1)
        self.assertEqual(built["refused"], "capture_verification_failed")
        self.assertEqual(self.cand()["excluded"],
                         "capture_verification_failed")

    def test_scan_detects_leaks(self):
        self.sealed_candidate()
        unit_id = self.selected()[0]
        brief = obs.build_brief(self.pilot, "demo#1@1", unit_id, 1)["brief"]
        self.assertEqual(obs.scan_brief(brief, {"raw_fingerprint": ["zz"]},
                                        {"rank": ["zz"]}), [])
        brief["capture_ref"]["base_digest"] = "deadbeefcafe"
        self.assertEqual(obs.scan_brief(brief, {"raw_fingerprint": [
            "deadbeefcafe"]}, {}), ["raw_fingerprint"])
        brief["family_questions"].append("this unit raised an alert")
        self.assertIn("status_word", obs.scan_brief(brief, {}, {}))
        self.assertEqual(obs.scan_brief(brief, {}, {"ordinary_verdict": [
            "approve"]}).count("ordinary_verdict"), 0)


class DefectTests(ObsCase):
    def two_confirmed(self, **second):
        self.sealed_candidate(probs={"Q-REQ-1": 0.9})
        units = sorted(self.cand()["units"], key=obs._rank_key)
        self.adjudicate(units[0]["unit_id"])
        self.adjudicate(units[1]["unit_id"], **second)
        return units

    def test_same_key_counts_once_with_both_links(self):
        units = self.two_confirmed()
        records = obs.build_defect_records(self.pilot)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["defect_key"],
                         "demo#1@1|export_report|missing_behavior")
        self.assertEqual(record["unit_links"],
                         sorted(u["unit_id"] for u in units))
        self.assertTrue(record["detected_by_jev"])
        self.assertFalse(record["matched_ordinary"])
        self.assertTrue(record["additional_signal"])
        self.assertEqual(record["origin"], "introduced")

    def test_matched_ordinary_finding(self):
        self.two_confirmed()
        self.assertFalse(obs.key_ordinary_findings(
            self.pilot, "demo#1@1", [{"primary_symbol": "x",
                                      "defect_class": "bogus"}]))
        self.assertTrue(obs.key_ordinary_findings(
            self.pilot, "demo#1@1", [{"text": "t", "primary_symbol":
                                      "export_report",
                                      "defect_class": "missing_behavior"}]))
        self.assertFalse(obs.key_ordinary_findings(
            self.pilot, "demo#1@1", []))
        record = obs.build_defect_records(self.pilot)[0]
        self.assertTrue(record["matched_ordinary"])
        self.assertFalse(record["additional_signal"])

    def test_alias_merge_before_metrics(self):
        self.two_confirmed(cls="partial_state")
        self.assertEqual(len(obs.build_defect_records(self.pilot)), 2)
        with open(self.pilot.alias_path, "w") as fh:
            fh.write(json.dumps({
                "at": "2026-10-03T00:00:00Z", "kind": "defect",
                "from": "demo#1@1|export_report|partial_state",
                "to": "demo#1@1|export_report|missing_behavior",
                "reason": "same defect"}) + "\n")
        merged = obs.build_defect_records(self.pilot)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["unit_links"]), 2)

    def test_intervened_candidate_has_no_additional_signal(self):
        self.write_pilot()
        token = self.promote()
        self.run_jobs()
        obs.record_intervention(self.pilot, token["candidate_id"],
                                "defect communicated early")
        self.review(token)
        cand = self.cand()
        self.assertTrue(cand["intervened"])
        self.assertEqual(cand["intervention_reasons"],
                         ["defect communicated early"])
        unit_id = cand["units"][0]["unit_id"]
        self.assertEqual(obs.begin_adjudication(self.pilot, unit_id)[
            "reason"], "candidate_not_eligible")


class ConstClock:
    def __call__(self):
        return "2026-10-02T00:00:00Z"


class AuthorizationTests(ObsCase):
    def mutate_activation(self, mutate):
        self.write_pilot()
        path = os.path.join(self.root, "cohort_activation.json")
        with open(path) as fh:
            act = json.load(fh)
        mutate(act)
        with open(path, "w") as fh:
            json.dump(act, fh)

    def test_incomplete_or_mismatched_authorization_is_inert(self):
        reads = []
        obs.configure(credential_provider=lambda: reads.append(1) or "x")
        mutations = (
            lambda a: a["authorizations"].pop("budget_ref"),
            lambda a: a["authorizations"].update(vendor_retention_status=""),
            lambda a: a["authorizations"]["data_scope"].update(
                repositories=[]),
            lambda a: a["authorizations"].pop("data_scope"),
            lambda a: a["authorizations"].update(recipients=["TypeSafe"]),
            lambda a: a["authorizations"].update(
                recipients=["fake-provider"]),
            lambda a: a["adjudicator"].pop("provider"),
            lambda a: a["adjudicator"].update(brief_version="other"),
            lambda a: a["authorizations"]["docs_recheck"].update(
                price_usd_per_million_input="0.05"),
            lambda a: a["authorizations"]["docs_recheck"].update(
                checked_at="2026-10-05T00:00:00Z"),
            lambda a: a["authorizations"]["docs_recheck"].update(
                limits_recorded=False),
            lambda a: a["authorizations"].pop("docs_recheck"),
            lambda a: a.update(inclusion_list_digest="1" * 64),
            lambda a: a.pop("inclusion_list_digest"))
        for mutate in mutations:
            self.mutate_activation(mutate)
            self.assertIsNone(obs.load_pilot(self.root))
            self.assertIsNone(self.promote())
        self.assertEqual((self.transport.calls, reads, self.jobs), ([], [], []))

    def test_inclusion_list_edit_after_activation_is_rejected(self):
        self.write_pilot()
        path = os.path.join(self.root, "inclusion_list.json")
        with open(path) as fh:
            entries = json.load(fh)
        entries.append(dict(entries[0], session_id="sess-extra"))
        with open(path, "w") as fh:
            json.dump(entries, fh)
        self.assertIsNone(obs.load_pilot(self.root))
        self.assertIsNone(self.promote("sess-extra"))

    def test_session_must_match_authorized_repository_scope(self):
        other = os.path.join(self.tmp, "other-root")
        for entries in (
                entry_list({"sess-a": "Demo#1"}, self.repo, self.base,
                           repository="unlisted-repo"),
                entry_list({"sess-a": "Demo#1"}, other, self.base)):
            write_pilot_files(self.root, entries)
            self.pilot = obs.load_pilot(self.root)
            self.assertIsNotNone(self.pilot)
            self.assertIsNone(self.promote())
            state = self.records(obs.SCHEMA_STATE)[-1]
            self.assertEqual(state["exclusion_reason"],
                             "not_in_inclusion_list")
            self.assertEqual(state["problems"], ["repository_not_authorized"])
            os.remove(self.pilot.registry_path)
            shutil.rmtree(self.pilot.acceptance_dir, ignore_errors=True)
        self.assertEqual((self.transport.calls, self.jobs), ([], []))


class HaltTests(ObsCase):
    def test_guard_reason_reflects_halts(self):
        self.write_pilot()
        self.assertIsNone(obs.guard_reason(self.pilot))
        obs.suspend(self.pilot)
        self.assertEqual(obs.guard_reason(self.pilot), "suspended")
        obs.record_hard_stop(self.pilot, "budget_revocation")
        self.assertEqual(obs.guard_reason(self.pilot), "hard_stop")
        os.remove(os.path.join(self.root, "cohort_activation.json"))
        self.assertEqual(obs.guard_reason(self.pilot),
                         "authorization_invalid")

    def test_hard_stop_blocks_capture_and_queued_work(self):
        self.write_pilot({"sess-a": "Demo#1", "sess-b": "Demo#2"})
        token = self.promote("sess-a")
        obs.record_hard_stop(self.pilot, "data_scope_violation")
        self.run_jobs()
        self.assertEqual(self.transport.calls, [])
        self.assertIsNone(self.promote("sess-b"))
        self.assertEqual(self.records(obs.SCHEMA_STATE)[-1][
            "exclusion_reason"], "hard_stop")
        self.assertEqual(self.jobs, [])
        self.assertIsNotNone(token)

    def test_halt_blocks_adjudication_reservations(self):
        self.sealed_candidate(probs={"Q-REQ-1": 0.9})
        units = sorted(self.cand()["units"], key=obs._rank_key)
        first, second = units[0]["unit_id"], units[1]["unit_id"]
        self.assertEqual(obs.begin_adjudication(self.pilot, first)["state"],
                         "started")
        obs.suspend(self.pilot)
        self.assertEqual(obs.begin_adjudication(self.pilot, second),
                         {"state": "refused", "reason": "suspended"})
        os.remove(self.pilot.suspended_path)
        obs.record_hard_stop(self.pilot, "budget_revocation")
        self.assertEqual(obs.begin_adjudication(self.pilot, second),
                         {"state": "refused", "reason": "hard_stop"})
        self.assertEqual(len(self.records(obs.SCHEMA_ADJ_STARTED)), 1)
        # An attempt that already started settles truthfully.
        settled = obs.submit_adjudication(self.pilot,
                                          adjudication_record(first))
        self.assertTrue(settled["accepted"])

    def test_resume_never_clears_a_hard_stop(self):
        self.write_pilot({"sess-a": "Demo#1", "sess-b": "Demo#2"})
        obs.suspend(self.pilot)
        obs.record_hard_stop(self.pilot, "credential_exposure")
        result = obs.resume(self.pilot)
        self.assertTrue(result["hard_stop"])
        self.assertFalse(os.path.exists(self.pilot.suspended_path))
        self.assertEqual(obs.guard_reason(self.pilot), "hard_stop")
        self.assertIsNone(self.promote("sess-b"))
        self.assertEqual(self.transport.calls, [])


class BoundaryTests(ObsCase):
    def test_concurrent_sessions_near_the_limit_are_serialized(self):
        obs.configure(clock=ConstClock())
        sessions = {"sess-%s" % c: "Demo#%d" % i
                    for i, c in enumerate("abcd")}
        self.write_pilot(sessions, jev_caps={"candidates_per_cohort": 2})
        tokens = {}

        def work(sid):
            tokens[sid] = self.promote(sid)
        threads = [threading.Thread(target=work, args=(sid,))
                   for sid in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        winners = [s for s, t in tokens.items() if t is not None]
        self.assertEqual(len(winners), 2)
        self.assertEqual(len(self.jobs), 2)
        states = self.records(obs.SCHEMA_STATE)
        captured = [s for s in states if s["state"] == "captured"]
        outside = [s for s in states if s["state"] == "outside_cohort_window"]
        self.assertEqual((len(captured), len(outside)), (2, 2))
        self.assertEqual(sorted(s["acceptance_seq"] for s in captured),
                         [1, 2])
        stamps = self.records(obs.SCHEMA_STAMP)
        self.assertEqual(len(stamps), 1)
        self.assertEqual((stamps[0]["trigger"], stamps[0]["close_seq"]),
                         ("count", 2))
        self.assertEqual(sorted(s["acceptance_seq"] for s in outside),
                         [3, 4])

    def test_cap_close_uses_sequence_not_equal_timestamps(self):
        obs.configure(clock=ConstClock())
        self.write_pilot({"sess-a": "Demo#1", "sess-b": "Demo#2",
                          "sess-c": "Demo#3"},
                         jev_caps={"proposed_cap_usd": 0.005})
        for sid in ("sess-a", "sess-b", "sess-c"):
            self.assertIsNotNone(self.promote(sid))
        self.transport.gate = threading.Event()
        threads = [threading.Thread(target=job) for job in self.jobs]
        for thread in threads:
            thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not any(
                r["schema"] == jc.SCHEMA_CLOSED
                for r in obs._read_jsonl(self.pilot.client_registry)):
            time.sleep(0.01)
        self.transport.gate.set()
        for thread in threads:
            thread.join(30)
        stamp = self.records(obs.SCHEMA_STAMP)[0]
        self.assertEqual((stamp["trigger"], stamp["close_seq"]), ("cap", 3))
        self.assertEqual(len(self.records(obs.SCHEMA_STAMP)), 1)


class DocumentationTests(unittest.TestCase):
    def test_readme_documents_activation_and_suspension(self):
        path = os.path.join(os.path.dirname(SCRIPTS), "README.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(obs.PILOT_ENV, text)
        self.assertIn("SUSPENDED", text)
        self.assertIn("suspend", text.lower())
        self.assertIn("never re-send", text)


if __name__ == "__main__":
    unittest.main()
