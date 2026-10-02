import json
import multiprocessing
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from decimal import Decimal
from unittest import mock

import cowork
import cowork_jev_activation as activation
import cowork_jev_capture as cap
import cowork_jev_client as jc
import cowork_state as state_store


class FakeJevTransport:
    def __init__(self, failure=None, delay=0):
        self.failure = failure
        self.delay = delay
        self.calls = []
        self.peak = 0
        self._lock = threading.Lock()
        self._live = 0

    def __call__(self, url, headers, body, timeout):
        import time
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
            self.calls.append({"url": url, "headers": dict(headers),
                               "timeout": timeout})
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.failure:
                raise ConnectionError(self.failure)
            request = json.loads(body.decode("utf-8"))
            answers = {question_id: {"type": "noul", "noul": 0.1}
                       for question_id in request["questions"]}
            payload = {"model": jc.MODEL, "answers": answers,
                       "usage": {"input_tokens": 123,
                                 "output_tokens": 4}}
            return 200, json.dumps(payload).encode("utf-8")
        finally:
            with self._lock:
                self._live -= 1


def _process_failure_transport(url, headers, body, timeout):
    import time
    calls_path = os.environ["JEV_TEST_CALLS_PATH"]
    descriptor = os.open(calls_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                         0o600)
    try:
        os.write(descriptor, b"attempt\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    time.sleep(0.1)
    raise ConnectionError("synthetic offline failure")


def _automatic_budget_process(binding, repo, credential, calls_path,
                              reservation, ready, go):
    os.environ["JEV_TEST_CREDENTIAL"] = credential
    os.environ["JEV_TEST_CALLS_PATH"] = calls_path
    jc.RESERVATION_USD = Decimal(reservation)
    activation.configure(transport=_process_failure_transport)
    ready.set()
    if not go.wait(10):
        raise RuntimeError("test start signal timed out")
    activation.record_candidate_boundary(binding, repo)


class AutomaticActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jev-auto-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(activation.reset_overrides)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.identity = activation.repository_identity(self.repo)
        self.config = {
            "schema": activation.SCHEMA, "enabled": True,
            "mode": "observation_only", "effective_at": "2020-01-01T00:00:00Z",
            "repository_identity": self.identity,
            "pilot_dir": os.path.join(self.tmp, "pilot"),
            "shared_budget_usd": 5, "credential_env": "JEV_API_KEY",
        }
        self.config_path = os.path.join(self.tmp, "auto.json")
        self.config["_config_path"] = os.path.realpath(self.config_path)
        os.environ[activation.CONFIG_ENV] = self.config_path
        os.environ["COWORK_SESSIONS_ROOT"] = os.path.join(self.tmp, "sessions")
        self.addCleanup(os.environ.pop, activation.CONFIG_ENV, None)
        self.addCleanup(os.environ.pop, "COWORK_SESSIONS_ROOT", None)
        self._save_config()

    def _save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)

    def test_config_is_discovered_as_json_without_shell_sourcing(self):
        path = os.path.join(self.tmp, "auto.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)
        got, reason = activation.load_config(path, environ={})
        self.assertIsNone(reason)
        self.assertEqual(got["credential_env"], "JEV_API_KEY")

    def test_config_inside_authorized_repository_is_rejected(self):
        path = os.path.join(self.repo, "jev-config.json")
        self.config["pilot_dir"] = os.path.join(self.tmp, "pilot-outside")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)
        got, reason = activation.load_config(path, environ={})
        self.assertIsNone(reason)
        self.assertEqual(
            activation.match_config(got, self.repo),
            "configuration_or_artifact_inside_repository")

    def test_exact_repository_and_linked_worktree_identity(self):
        self.assertIsNone(activation.match_config(self.config, self.repo))
        linked = os.path.join(self.tmp, "linked")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-b",
                        "linked", linked], check=True, capture_output=True)
        self.assertIsNone(activation.match_config(self.config, linked))
        sibling = os.path.join(self.tmp, "repo-deceptive")
        os.makedirs(sibling)
        subprocess.run(["git", "init", "-q", sibling], check=True)
        self.assertEqual(activation.match_config(self.config, sibling),
                         "repository_not_authorized")
        alias = os.path.join(self.tmp, "repo-alias")
        os.symlink(sibling, alias)
        self.assertEqual(activation.match_config(self.config, alias),
                         "repository_not_authorized")

    def test_binding_is_stable_and_status_does_not_claim_accuracy(self):
        a = activation.session_binding(self.config, "session-a", self.repo,
                                       "Fix the behavior")
        b = activation.session_binding(self.config, "session-b", self.repo,
                                       "Fix another behavior")
        self.assertEqual(a["status"], "enrolled")
        self.assertNotEqual(a["provenance"], b["provenance"])
        self.assertEqual(a["accuracy_metrics"],
                         "pending_independent_ground_truth")
        receipt = activation.persist_session_binding(a)
        self.assertEqual(receipt["observation_status"], "not_applicable")
        self.assertEqual(receipt["unavailable_reason"],
                         "no_eligible_builder_candidate_yet")
        self.assertNotIn("secret", json.dumps(receipt).lower())

    def test_not_yet_effective_and_unconfigured_do_not_enroll(self):
        self.config["effective_at"] = "2999-01-01T00:00:00Z"
        binding = activation.session_binding(self.config, "old", self.repo,
                                             "objective")
        self.assertEqual(binding["reason"], "not_yet_effective")
        cfg, reason = activation.load_config(
            os.path.join(self.tmp, "missing.json"), environ={})
        self.assertIsNone(cfg)
        self.assertEqual(reason, "not_configured")

    def _write_source(self):
        source = os.path.join(self.repo, "app.py")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("def add(a, b):\n    return a + b\n")

    def _binding(self, session_id, objective=
                 "The service must preserve the submitted record."):
        self._write_source()
        subprocess.run(["git", "-C", self.repo, "add", "app.py"], check=True)
        if subprocess.run(["git", "-C", self.repo, "rev-parse", "--verify",
                           "HEAD"], capture_output=True).returncode:
            subprocess.run(["git", "-C", self.repo, "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit",
                            "-m", "baseline"], check=True,
                           capture_output=True)
        self._save_config()
        return activation.session_binding(self.config, session_id, self.repo,
                                          objective)

    def test_real_capture_and_fake_query_record_truthful_observation(self):
        secret = "fake-only-JEV-secret-do-not-persist"
        self.config["credential_env"] = "JEV_TEST_CREDENTIAL"
        binding = self._binding("query-session")
        with open(os.path.join(self.repo, "app.py"), "a",
                  encoding="utf-8") as handle:
            handle.write("\n# candidate change\n")
        transport = FakeJevTransport()
        activation.configure(transport=transport,
                             thread_starter=lambda work: work())
        with mock.patch.dict(os.environ, {"JEV_TEST_CREDENTIAL": secret}):
            started = activation.start_candidate_observation(binding,
                                                            self.repo)
        self.assertEqual(started["status"], "started")
        receipt_path = os.path.join(self.config["pilot_dir"], "automatic",
                                    "sessions", "query-session.json")
        with open(receipt_path, "r", encoding="utf-8") as handle:
            result = json.load(handle)
        self.assertEqual(result["observation_status"], "observed")
        self.assertEqual(result["accuracy_metrics"],
                         "pending_independent_ground_truth")
        self.assertEqual(result["independent_adjudication"],
                         "unavailable_not_authorized")
        self.assertGreaterEqual(result["usage"]["attempts"], 1)
        self.assertTrue(any(unit["queried"] for unit in result["signals"]))
        self.assertIn("The service must preserve the submitted record.",
                      result["requirements_text"])
        capture = cap.load_capture(os.path.join(self.config["pilot_dir"],
                                                "automatic", "captures"),
                                  result["capture_id"])
        by_path = {entry["rel_path"]: entry for entry in capture["files"]}
        self.assertEqual(by_path["app.py"]["status"], "modified")
        self.assertTrue(all(entry["status"] != "added" for entry in
                            capture["files"] if entry["rel_path"] != "app.py"))
        self.assertEqual(transport.calls[0]["headers"]["Authorization"],
                         "Bearer " + secret)
        for root, _dirs, files in os.walk(self.config["pilot_dir"]):
            for filename in files:
                with open(os.path.join(root, filename), "rb") as handle:
                    self.assertNotIn(secret.encode(), handle.read())

    def test_missing_key_and_suspension_do_not_call_transport(self):
        self.config["credential_env"] = "JEV_TEST_CREDENTIAL"
        self._save_config()
        transport = FakeJevTransport()
        activation.configure(transport=transport)
        first = self._binding("missing-key")
        self.assertIsNone(activation._binding_disabled_reason(first, self.repo),
                          repr((first, activation.load_config(
                              first["config_path"])[0],
                                activation.repository_identity(self.repo))))
        with mock.patch.dict(os.environ):
            os.environ.pop("JEV_TEST_CREDENTIAL", None)
            missing = activation.record_candidate_boundary(first, self.repo)
        self.assertEqual(missing["observation_status"], "unavailable")
        self.assertEqual(missing["unavailable_reason"], "no_credential")
        self.assertEqual(transport.calls, [])

        suspended = self._binding("suspended-session")
        marker = os.path.join(self.config["pilot_dir"], "automatic", "SUSPENDED")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("suspended for test\n")
        result = activation.record_candidate_boundary(suspended, self.repo)
        self.assertEqual(result["observation_status"], "excluded")
        self.assertEqual(result["unavailable_reason"], "suspended")
        self.assertEqual(transport.calls, [])

    def test_concurrent_sessions_share_unknown_reservation_and_budget_cap(self):
        self.config["credential_env"] = "JEV_TEST_CREDENTIAL"
        binding_a = self._binding("budget-a")
        binding_b = self._binding("budget-b")
        with open(os.path.join(self.repo, "app.py"), "a",
                  encoding="utf-8") as handle:
            handle.write("\n# shared candidate change\n")
        calls_path = os.path.join(self.tmp, "transport-calls.txt")
        context = multiprocessing.get_context("spawn")
        ready = [context.Event(), context.Event()]
        go = context.Event()
        processes = [context.Process(
            target=_automatic_budget_process,
            args=(binding, self.repo, "fake-test-key", calls_path, "3",
                  ready[index], go))
                     for index, binding in enumerate((binding_a, binding_b))]
        for process in processes:
            process.start()
        try:
            self.assertTrue(all(event.wait(10) for event in ready))
            go.set()
            for process in processes:
                process.join(20)
        finally:
            go.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        with open(calls_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read().splitlines(), ["attempt"])
        store = jc.FileStore(os.path.join(self.config["pilot_dir"],
                                          "automatic", "jev"))
        cohort = jc._Cohort(store.records(), "jev-auto-observation.v1")
        self.assertEqual(cohort.started_count, 1)
        self.assertEqual(cohort.committed, Decimal("3"))
        outcomes = [record for record in store.records()
                    if record.get("schema") == jc.SCHEMA_OUTCOME]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["cost"]["charge_status"], "unknown")
        self.assertEqual(cohort.closed, True)
        receipts = []
        for session_id in ("budget-a", "budget-b"):
            path = os.path.join(self.config["pilot_dir"], "automatic",
                                "sessions", session_id + ".json")
            with open(path, "r", encoding="utf-8") as handle:
                receipts.append(json.load(handle))
        self.assertCountEqual([rec["observation_status"] for rec in receipts],
                              ["observed", "unavailable"])
        loser = next(rec for rec in receipts
                     if rec["observation_status"] == "unavailable")
        self.assertEqual(loser["unavailable_reason"], "budget_exhausted")
        self.assertTrue(all(rec["accuracy_metrics"] ==
                            "pending_independent_ground_truth"
                            for rec in receipts))

    def test_linked_worktree_finds_persisted_session_receipt(self):
        self._write_source()
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "add", "app.py"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm",
                        "fixture"], check=True, capture_output=True)
        linked = os.path.join(self.tmp, "linked")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-b",
                        "linked", linked], check=True, capture_output=True)
        config_path = os.path.join(self.tmp, "auto.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)
        binding = activation.session_binding(self.config, "linked-session",
                                             self.repo, "An objective")
        activation.persist_session_binding(binding)
        with mock.patch.dict(os.environ,
                             {activation.CONFIG_ENV: config_path}):
            recovered = activation.session_binding_from_file("linked-session",
                                                             linked)
        self.assertEqual(recovered["session_id"], "linked-session")
        self.assertEqual(recovered["repository_identity"], self.identity)

    def test_linked_worktree_rejects_primary_root_artifacts(self):
        self._write_source()
        subprocess.run(["git", "-C", self.repo, "add", "app.py"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit",
                        "-m", "baseline"], check=True, capture_output=True)
        linked = os.path.join(self.tmp, "linked")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-b",
                        "linked", linked], check=True, capture_output=True)
        self.config["pilot_dir"] = os.path.join(self.repo, "pilot")
        self.assertEqual(activation.match_config(self.config, linked),
                         "configuration_or_artifact_inside_repository")

    def test_clean_baseline_and_unavailable_dirty_baseline(self):
        binding = self._binding("base-session")
        expected = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"],
                                  check=True, capture_output=True,
                                  text=True).stdout.strip()
        self.assertEqual(binding["base_ref"], expected)
        with open(os.path.join(self.repo, "app.py"), "a",
                  encoding="utf-8") as handle:
            handle.write("dirty before next session\n")
        dirty = activation.session_binding(self.config, "dirty-session",
                                           self.repo, "The code must work.")
        self.assertIsNone(dirty["base_ref"])
        result = activation.record_candidate_boundary(dirty, self.repo)
        self.assertEqual(result["unavailable_reason"],
                         "trusted_baseline_unavailable")

    def test_disable_blocks_anchor_and_preserves_binding(self):
        binding = self._binding("disabled-session")
        activation.persist_session_binding(binding)
        self.config["enabled"] = False
        self._save_config()
        linked = os.path.join(self.tmp, "disabled-linked")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-b",
                        "disabled-linked", linked], check=True,
                       capture_output=True)
        transport = FakeJevTransport()
        activation.configure(transport=transport)
        found = activation.session_binding_from_file("disabled-session", linked)
        self.assertEqual(found["session_id"], "disabled-session")
        result = activation.record_candidate_boundary(found, linked)
        self.assertEqual(result["unavailable_reason"],
                         "configuration_disabled")
        self.assertEqual(transport.calls, [])

    def test_queued_observation_recovers_without_promotion_and_no_resend(self):
        self.config["credential_env"] = "JEV_TEST_CREDENTIAL"
        binding = self._binding("recover-session")
        with open(os.path.join(self.repo, "app.py"), "a",
                  encoding="utf-8") as handle:
            handle.write("\n# candidate\n")
        prepared_result = activation.record_candidate_boundary(
            binding, self.repo, _capture_only=True)
        prepared = prepared_result["prepared"]
        prepared["repo"] = self.repo
        activation._write_work_item(binding, {
            "status": "queued", "capture_id": prepared["captured"][
                "capture_id"], "candidate_id": prepared["candidate_id"],
            "repo_path": self.repo, "capture_ms": 0})
        transport = FakeJevTransport()
        activation.configure(transport=transport,
                             thread_starter=lambda work: work())
        with mock.patch.dict(os.environ, {"JEV_TEST_CREDENTIAL": "fake-key"}):
            recovered = activation.recover_session_observation(binding,
                                                               self.repo)
        receipt_path = os.path.join(binding["pilot_dir"], "automatic",
                                    "sessions", binding["session_id"] + ".json")
        with open(receipt_path, "r", encoding="utf-8") as handle:
            receipt = json.load(handle)
        self.assertEqual(receipt["observation_status"], "observed",
                         repr(recovered))
        self.assertTrue(transport.calls)
        calls = len(transport.calls)
        with mock.patch.dict(os.environ, {"JEV_TEST_CREDENTIAL": "fake-key"}):
            activation.recover_session_observation(binding, self.repo)
        self.assertEqual(len(transport.calls), calls)

    def test_started_attempt_is_recorded_unknown_and_never_resent(self):
        self.config["credential_env"] = "JEV_TEST_CREDENTIAL"
        binding = self._binding("interrupted-session")
        with open(os.path.join(self.repo, "app.py"), "a",
                  encoding="utf-8") as handle:
            handle.write("\n# candidate\n")
        captured = activation.record_candidate_boundary(
            binding, self.repo, _capture_only=True)["prepared"]
        captured["repo"] = self.repo
        record = captured["record"]
        unit = next(unit for unit in record["units"]
                    if unit.get("applicable") is True)
        request = jc.build_request(unit)
        attempt_id = jc.attempt_id_for(unit["unit_id"],
                                       request["request_digest"])
        jev_dir = os.path.join(binding["pilot_dir"], "automatic", "jev")
        jc.FileStore(jev_dir).append({
            "schema": jc.SCHEMA_STARTED,
            "cohort_id": "jev-auto-observation.v1",
            "candidate_id": record["candidate_id"],
            "unit_id": unit["unit_id"], "attempt_id": attempt_id,
            "request_digest": request["request_digest"],
            "state_tokens_est": 100, "reserved_tokens": 100,
            "reserved_usd": str(jc.RESERVATION_USD),
            "started_at": "2026-10-02T00:00:00Z"})
        activation._write_work_item(binding, {
            "status": "started", "capture_id": captured["captured"][
                "capture_id"], "candidate_id": record["candidate_id"],
            "repo_path": self.repo, "capture_ms": 1})
        transport = FakeJevTransport()
        activation.configure(transport=transport,
                             thread_starter=lambda work: work())
        with mock.patch.dict(os.environ, {"JEV_TEST_CREDENTIAL": "fake-key"}):
            activation.recover_session_observation(binding, self.repo)
        with open(os.path.join(binding["pilot_dir"], "automatic", "sessions",
                               binding["session_id"] + ".json"), "r",
                  encoding="utf-8") as handle:
            receipt = json.load(handle)
        self.assertEqual(transport.calls, [])
        self.assertEqual(receipt["observation_status"], "observed")
        self.assertEqual(receipt["usage"]["unknown_charge_count"], 1)


class RunFlowEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jev-run-flow-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.identity = activation.repository_identity(self.repo)
        self.config_path = os.path.join(self.tmp, "auto.json")
        self.config = {
            "schema": activation.SCHEMA, "enabled": True,
            "mode": "observation_only",
            "effective_at": "2020-01-01T00:00:00Z",
            "repository_identity": self.identity,
            "pilot_dir": os.path.join(self.tmp, "pilot"),
            "shared_budget_usd": 5, "credential_env": "JEV_API_KEY",
        }
        old_cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, old_cwd)
        activation.reset_overrides()
        self.addCleanup(activation.reset_overrides)

    def _run_scout(self, *args, **kwargs):
        # End at the scout boundary; no provider is launched by this fixture.
        return 1

    def _run(self, argv):
        args = cowork.build_parser().parse_args(argv)
        result_box = {}
        rc = cowork.run_flow(
            args, io_out=__import__("io").StringIO(),
            which=lambda controller: "/bin/" + controller,
            run_scout_fn=self._run_scout,
            run_planner_fn=self._run_scout,
            run_builder_fn=self._run_scout,
            run_worktree_fn=self._run_scout,
            result_box=result_box)
        return rc, result_box

    def test_run_flow_enrolls_fresh_sessions_but_not_old_resume(self):
        env = {activation.CONFIG_ENV: self.config_path,
               "COWORK_SESSIONS_ROOT": os.path.join(self.tmp, "assets")}
        with mock.patch.dict(os.environ, env):
            os.environ.pop(cowork._JEV_PILOT_ENV, None)
            # A session created while no config exists stays unbound on resume.
            _old_rc, old = self._run(
                ["--team", "scout,scout-reviewer", "--context",
                 "Old objective must stay unchanged."])
            old_state = state_store.load(old["session_file"])
            self.assertNotIn("jev_observation", old_state)
            with open(self.config_path, "w", encoding="utf-8") as handle:
                json.dump(self.config, handle)
            _resume_rc, resumed = self._run(
                ["--session-file", old["session_file"]])
            self.assertEqual(resumed["session_uuid"], old["session_uuid"])
            self.assertNotIn("jev_observation",
                             state_store.load(old["session_file"]))
            old_receipt = os.path.join(
                self.config["pilot_dir"], "automatic", "sessions",
                old["session_uuid"] + ".json")
            self.assertFalse(os.path.exists(old_receipt))

            # Each fresh run enrolls without a manual inclusion list.
            fresh = []
            for objective in ("First objective must be measured.",
                              "Second objective should be observed."):
                _fresh_rc, box = self._run([
                    "--team", "scout,scout-reviewer", "--context", objective])
                fresh.append(box)
                state = state_store.load(box["session_file"])
                binding = state["jev_observation"]
                self.assertEqual(binding["status"], "enrolled")
                self.assertEqual(binding["objective_text"], objective)
                self.assertEqual(state["jev_observation_status"]["status"],
                                 "not_applicable")
                receipt_path = os.path.join(
                    self.config["pilot_dir"], "automatic", "sessions",
                    box["session_uuid"] + ".json")
                with open(receipt_path, "r", encoding="utf-8") as handle:
                    receipt = json.load(handle)
                self.assertEqual(receipt["observation_status"],
                                 "not_applicable")
            self.assertNotEqual(fresh[0]["session_uuid"],
                                fresh[1]["session_uuid"])
            self.assertFalse(os.path.exists(os.path.join(
                self.config["pilot_dir"], "inclusion_list.json")))

    def test_run_flow_resume_schedules_queued_query_without_promotion(self):
        source = os.path.join(self.repo, "app.py")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("def add(a, b):\n    return a + b\n")
        with open(os.path.join(self.repo, ".gitignore"), "w",
                  encoding="utf-8") as handle:
            handle.write(".cowork/\n")
        subprocess.run(["git", "-C", self.repo, "add", "app.py",
                        ".gitignore"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit",
                        "-m", "baseline"], check=True, capture_output=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle)
        sessions_root = os.path.join(self.tmp, "resume-assets")
        env = {activation.CONFIG_ENV: self.config_path,
               "COWORK_SESSIONS_ROOT": sessions_root}
        with mock.patch.dict(os.environ, env):
            os.environ.pop(cowork._JEV_PILOT_ENV, None)
            _rc, fresh = self._run([
                "--team", "scout,scout-reviewer", "--context",
                "The operation must retain its inputs."])
            state = state_store.load(fresh["session_file"])
            binding = state["jev_observation"]
            self.assertTrue(binding["base_ref"])
            with open(source, "a", encoding="utf-8") as handle:
                handle.write("\n# candidate change\n")
            prepared = activation.record_candidate_boundary(
                binding, self.repo, _capture_only=True)["prepared"]
            activation._write_work_item(binding, {
                "status": "queued", "capture_id": prepared["captured"][
                    "capture_id"], "candidate_id": prepared["candidate_id"],
                "repo_path": self.repo, "capture_ms": 0})
            scheduled = []
            transport = FakeJevTransport()
            activation.configure(transport=transport,
                                 thread_starter=scheduled.append)
            _resume_rc, resumed = self._run([
                "--session-file", fresh["session_file"]])
            self.assertEqual(resumed["session_uuid"], fresh["session_uuid"])
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(transport.calls, [])
            with mock.patch.dict(os.environ, {"JEV_API_KEY": "fake-key"}):
                scheduled[0]()
            self.assertTrue(transport.calls)
            receipt_path = os.path.join(self.config["pilot_dir"], "automatic",
                                        "sessions",
                                        fresh["session_uuid"] + ".json")
            with open(receipt_path, "r", encoding="utf-8") as handle:
                receipt = json.load(handle)
            self.assertEqual(receipt["observation_status"], "observed")


if __name__ == "__main__":
    unittest.main()
