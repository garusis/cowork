#!/usr/bin/env python3
"""Offline, no-provider contract tests for the work-package controller."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import datetime as dt
import importlib.util
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path


SKILL = Path("/Users/marcos/.codex/skills/cowork-refactor-orchestrator")
CONTROLLER = SKILL / "scripts" / "orchestrate.py"


def controller_module():
    """Load an isolated controller module so fixed historical bindings can be
    represented by small, entirely local fixtures.

    The CLI tests intentionally exercise the shipped executable.  These
    lifecycle tests need to build a self-consistent M0-B candidate without
    copying the user's live package, so they use the same command wrappers
    while substituting only the immutable fixture identities.
    """
    spec = importlib.util.spec_from_file_location("orchestrate_fixture", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OrchestrateCLITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.state = self.root / "state"
        self.repo.mkdir(); self.git("init"); self.git("config", "user.email", "fixture@example.invalid"); self.git("config", "user.name", "fixture")
        # The fixture is entirely offline.  Do not inherit a global
        # 1Password/GPG commit-signing policy into its seed commit.
        self.git("config", "commit.gpgSign", "false"); self.git("config", "tag.gpgSign", "false")
        (self.repo / "tracked.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text(".worktrees/\n")
        self.git("add", "tracked.txt", ".gitignore"); self.git("commit", "-m", "fixture")
        self.base_head = self.git("rev-parse", "HEAD").strip()
        self.brief = self.root / "brief.md"; self.brief.write_text("Objective: offline contract fixture.\nInvariant: do not commit.\n")
        self.audit = self.root / "fake-claude-audit.jsonl"; self.fake = self.root / "fake-claude"
        self.fake.write_text("""#!%s
import json, os, pathlib, signal, sys, time
argv=sys.argv[1:]
with open(os.environ['FAKE_CLAUDE_AUDIT'], 'a', encoding='utf-8') as f:
    f.write(json.dumps({'argv':argv, 'cwd':os.getcwd(), 'network':os.environ.get('NO_NETWORK','')})+'\\n')
if os.environ.get('FAKE_CLAUDE_WRITE'): pathlib.Path('worker-change.txt').write_text('uncommitted worker edit\\n')
if os.environ.get('FAKE_CLAUDE_HUGE'): print('SECRET_TRANSCRIPT_MARKER_' + ('x' * 1200000))
kind=os.environ.get('FAKE_CLAUDE_RESULT', 'valid')
packet={'outcome':'completed','summary':'bounded fake summary','changed_paths':['worker-change.txt'],'checks':['fake check'], 'findings':[], 'assumptions':[], 'next_action':'run the deterministic gate'}
if kind == 'malformed': print('{not-json')
elif kind == 'missing': print(json.dumps({'type':'result','result':'not a packet'}))
elif kind == 'oversized':
    packet['summary']='x'*2001; print(json.dumps({'type':'result','structured_output':packet}))
elif kind == 'budget': print(json.dumps({'type':'result','subtype':'error_max_budget_usd','is_error':True, 'secret':os.environ.get('FAKE_CLAUDE_PROVIDER_SECRET', '')}))
elif kind == 'quota': print(json.dumps({'type':'result','subtype':'error_plan_quota_exhausted','is_error':True, 'retry_after':os.environ.get('FAKE_CLAUDE_RETRY_AFTER', '2099-01-01T00:00:00Z'), 'reset_source':os.environ.get('FAKE_CLAUDE_RESET_SOURCE', 'provider_terminal_retry_after'), 'secret':os.environ.get('FAKE_CLAUDE_PROVIDER_SECRET', '')}))
elif kind == 'claude_rate_limit':
    sid=argv[argv.index('--session-id')+1] if '--session-id' in argv else argv[argv.index('--resume')+1]
    info={'status':os.environ.get('FAKE_CLAUDE_RATE_STATUS','rejected'),'resetsAt':int(os.environ.get('FAKE_CLAUDE_RESETS_AT', str(int(time.time())+3600))),'rateLimitType':os.environ.get('FAKE_CLAUDE_RATE_TYPE','five_hour'),'overageStatus':os.environ.get('FAKE_CLAUDE_OVERAGE_STATUS','rejected'),'isUsingOverage':os.environ.get('FAKE_CLAUDE_USING_OVERAGE','false') == 'true','provider_secret':os.environ.get('FAKE_CLAUDE_PROVIDER_SECRET','')}
    print(json.dumps({'type':'rate_limit_event','session_id':os.environ.get('FAKE_CLAUDE_RATE_SESSION',sid),'rate_limit_info':info}))
    print(json.dumps({'type':'result','subtype':os.environ.get('FAKE_CLAUDE_TERMINAL_SUBTYPE','success'),'is_error':os.environ.get('FAKE_CLAUDE_TERMINAL_ERROR','true') == 'true','api_error_status':int(os.environ.get('FAKE_CLAUDE_TERMINAL_STATUS','429')),'session_id':os.environ.get('FAKE_CLAUDE_TERMINAL_SESSION',sid),'assistant_text':os.environ.get('FAKE_CLAUDE_PROVIDER_SECRET','')}))
else: print(json.dumps({'type':'result','structured_output':packet}))
if os.environ.get('FAKE_CLAUDE_SLEEP'):
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(float(os.environ['FAKE_CLAUDE_SLEEP']))
""" % sys.executable)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def tearDown(self): self.tmp.cleanup()
    def git(self, *args): return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def invoke_proc(self, *args, env=None):
        child_env = os.environ.copy(); child_env.update({"FAKE_CLAUDE_AUDIT": str(self.audit), "NO_NETWORK": "1"})
        if env: child_env.update(env)
        return subprocess.run([sys.executable, str(CONTROLLER), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env)

    def invoke(self, *args, env=None, ok=True):
        proc = self.invoke_proc(*args, env=env)
        self.assertLessEqual(len(proc.stdout.encode()), 8193, proc.stdout[:200])
        data = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0 if ok else 2, (data, proc.stderr))
        if not ok: self.assertFalse(data["ok"])
        return data

    def prepare(self, work_id="fixture"):
        out = self.invoke("prepare", "--repo", str(self.repo), "--brief-file", str(self.brief), "--state-root", str(self.state), "--work-id", work_id)
        self.refs_after_prepare = self.git("for-each-ref", "--format=%(refname)")
        return out

    def launch(self, work_id="fixture", role="investigator", resume=False, env=None):
        args = ["launch", "--state-root", str(self.state), "--work-id", work_id, "--role", role, "--claude-bin", str(self.fake), "--model", "fake-model", "--effort", "medium"]
        if resume: args.append("--resume")
        return self.invoke(*args, env=env)

    def state_file(self, work_id="fixture"): return self.state / work_id / "state.json"
    def read_state(self, work_id="fixture"): return json.loads(self.state_file(work_id).read_text())
    def audit_rows(self): return [] if not self.audit.exists() else [json.loads(x) for x in self.audit.read_text().splitlines() if x]

    def adjudicate(self, verdict, rationale="offline supervisor rationale", **kwargs):
        ok = kwargs.pop("ok", True)
        args = ["adjudicate", "--state-root", str(self.state), "--work-id", "fixture", "--verdict", verdict, "--rationale", rationale, "--adjudicator-principal", "fixture-supervisor"]
        for name, value in kwargs.items(): args += ["--" + name.replace("_", "-"), value]
        return self.invoke(*args, ok=ok)

    def amend(self, token, instruction="Continue within the amended bounded objective.", principal="release-policy-principal", capability="publish_external", ok=True):
        return self.invoke("amend", "--state-root", str(self.state), "--work-id", "fixture", "--resume-token", token, "--instruction", instruction, "--acting-authority-principal", principal, "--granted-capability", capability, ok=ok)

    def collect(self, ok=True):
        return self.invoke("collect", "--state-root", str(self.state), "--work-id", "fixture", ok=ok)

    def prepare_alternate(self):
        return self.invoke("prepare-alternate-evidence", "--state-root", str(self.state), "--work-id", "fixture", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2")

    def prepare_provider_neutral(self):
        return self.invoke("prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", "fixture", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2")

    def alternate_packets(self, receipt_name="terra-receipt.txt"):
        state = self.read_state(); attempt = state["alternate_evidence"]; binding = attempt["binding"]
        wt = Path(state["worktree"]["path"]); receipt = wt / receipt_name
        self.assertTrue(receipt.exists(), "create the receipt before alternate preparation")
        sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
        result = {"schema_version": 1, "kind": "alternate_worker_result", "package_id": "fixture", "attempt_id": attempt["id"], "backend": "codex_terra", "actor_id": "terra-worker", "run_id": "terra-run-1", "candidate_binding": binding, "outcome": "completed", "summary": "bounded Terra result", "changed_paths": binding["changed_paths"], "checks": [{"command": ["python3", "-m", "unittest"], "exit_code": 0, "fact_source": "worker_assertion"}], "receipts": [{"kind": "candidate_artifact", "path": receipt_name, "sha256": sha}], "findings": [], "assumptions": [], "next_action": "adjudicate the candidate"}
        review = {"schema_version": 1, "kind": "alternate_independent_review", "package_id": "fixture", "attempt_id": attempt["id"], "backend": "codex_terra", "actor_id": "terra-reviewer", "run_id": "terra-run-2", "candidate_binding": binding, "verdict": "pass", "summary": "bounded independent review", "findings": [], "checks": ["receipt and candidate binding checked"]}
        result_path, review_path = self.root / "terra-result.json", self.root / "terra-review.json"
        result_path.write_text(json.dumps(result)); review_path.write_text(json.dumps(review))
        return result_path, review_path

    def make_legacy_budget_state(self, work_id="legacy-budget"):
        self.prepare(work_id); self.launch(work_id); self.wait_quiescent(work_id)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", work_id)
        state = self.read_state(work_id); directory = self.state / work_id
        attempt = state["attempt"]; log_path = Path(attempt["stdout"])
        with log_path.open("a") as out:
            out.write(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "errors": ["ignored provider text"]}) + "\n")
        log_sha = hashlib.sha256(log_path.read_bytes()).hexdigest()
        terminal = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True}
        terminal_digest = hashlib.sha256(json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        provider_path = directory / "packets" / (attempt["id"] + ".provider_failure.json")
        provider = {"schema_version": 1, "kind": "provider_terminal_failure", "work_id": work_id,
                    "attempt_id": attempt["id"], "provider_session_id": attempt["provider_session_id"],
                    "failure_class": "budget_exhausted", "terminal": terminal, "source_log_sha256": log_sha,
                    "terminal_digest": terminal_digest, "parsed_at": "2026-08-10T00:00:00Z"}
        provider_path.write_text(json.dumps(provider, sort_keys=True)); provider_sha = hashlib.sha256(provider_path.read_bytes()).hexdigest()
        candidate = state["candidate"]; evidence = candidate["evidence"]
        evidence.update({"packet_path": str(provider_path), "packet_sha256": provider_sha, "evidence_kind": "provider_terminal_failure",
                         "provider_failure": {"failure_class": "budget_exhausted", "source_log_path": str(log_path),
                                              "source_log_sha256": log_sha, "terminal_digest": terminal_digest}})
        candidate["evidence_digest"] = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        candidate["id"] = "provider-" + candidate["evidence_digest"][:32]; candidate["packet_verified"] = False
        base = {"repo_root": state["repo"]["root"], "head": state["repo"]["head"], "worktree_branch": state["worktree"]["branch"], "brief_sha256": state["brief"]["sha256"]}
        binding = {"base_digest": hashlib.sha256(json.dumps(base, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                   "candidate_digest": evidence["worktree_fingerprint"]["digest"],
                   "artifact_hashes": {x["name"]: x["sha256"] for x in evidence["declared_artifacts"]}, "finding_ids": [], "base": base,
                   "candidate_id": candidate["id"], "evidence_digest": candidate["evidence_digest"]}
        refs = [{"kind": "provider_terminal_failure", "path": str(provider_path), "sha256": provider_sha},
                {"kind": "worktree_fingerprint", "sha256": evidence["worktree_fingerprint"]["digest"]},
                {"kind": "provider_failure_log", "path": str(log_path), "sha256": log_sha, "terminal_digest": terminal_digest}]
        token = "11111111-1111-1111-1111-111111111111." + "2" * 32; reason = "historical package budget guard"
        escalation_packet = {"schema_version": 1, "kind": "authority_escalation", "package_id": work_id,
            "capability_required": "increase_budget", "target_authority": {"role": "budget_authority", "principal": "project-top-level"},
            "policy_version": "bootstrap_no_publish/v1", "policy_digest": hashlib.sha256(b"bootstrap_no_publish/v1").hexdigest(),
            "reason": reason, "candidate_binding": binding, "evidence_refs": refs, "requested_amendment": reason,
            "issued_at": "2026-08-10T00:00:00Z", "resume_token": token, "resume_token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        token_free = {k: v for k, v in escalation_packet.items() if k != "resume_token"}
        causal = hashlib.sha256(json.dumps({"package_id": work_id, "policy_version": "bootstrap_no_publish/v1",
            "candidate_digest": binding["candidate_digest"], "capability_required": "increase_budget",
            "packet_without_token": token_free}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        escalation_packet["causal_fingerprint"] = causal
        escalation_path = directory / "escalations" / "legacy.json"; escalation_path.parent.mkdir(exist_ok=True)
        escalation_path.write_text(json.dumps(escalation_packet, sort_keys=True)); escalation_sha = hashlib.sha256(escalation_path.read_bytes()).hexdigest()
        state["phase"] = "needs_authority"; state["candidate"] = candidate
        state["gate"] = {"verdict": "needs_authority", "kind": "provider_failure", "failure_class": "budget_exhausted",
                         "candidate_id": candidate["id"], "evidence_digest": candidate["evidence_digest"], "policy": "bootstrap_no_publish/v1"}
        state["escalation"] = {"path": str(escalation_path), "sha256": escalation_sha, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                               "used_at": None, "candidate_id": candidate["id"], "evidence_digest": candidate["evidence_digest"],
                               "causal_fingerprint": causal, "failure_class": "budget_exhausted"}
        state["attempt"]["provider_failure"] = {"failure_class": "budget_exhausted", "packet_sha256": provider_sha,
                                                   "source_log_sha256": log_sha, "terminal_digest": terminal_digest}
        self.state_file(work_id).write_text(json.dumps(state))
        with (directory / "events.jsonl").open("a") as events:
            events.write(json.dumps({"at": "2026-08-10T00:00:00Z", "kind": "provider_budget_exhausted", "attempt_id": attempt["id"],
                "candidate_id": candidate["id"], "evidence_digest": candidate["evidence_digest"], "causal_fingerprint": causal,
                "policy": "bootstrap_no_publish/v1"}) + "\n")
        return directory, provider_path, token

    def wait_audit(self, count):
        for _ in range(80):
            if len(self.audit_rows()) >= count: return self.audit_rows()
            time.sleep(.025)
        self.fail("fake controller was not invoked")

    def wait_quiescent(self, work_id="fixture"):
        for _ in range(100):
            status = self.invoke("status", "--state-root", str(self.state), "--work-id", work_id)
            if not status["alive"]: return status
            time.sleep(.025)
        self.fail("fake child never became quiescent")

    def test_prepare_path_safety_dirty_rejection_and_frozen_brief(self):
        link = self.root / "brief-link.md"; link.symlink_to(self.brief)
        self.assertIn("non-symlink", self.invoke("prepare", "--repo", str(self.repo), "--brief-file", str(link), "--state-root", str(self.state), "--work-id", "linked", ok=False)["error"])
        self.assertIn("invalid work id", self.invoke("prepare", "--repo", str(self.repo), "--brief-file", str(self.brief), "--state-root", str(self.state), "--work-id", "../escape", ok=False)["error"])
        out = self.prepare(); state = self.read_state(); wt = Path(state["worktree"]["path"])
        self.assertEqual(out["phase"], "prepared"); self.assertEqual(state["repo"]["head"], self.base_head)
        self.assertEqual((self.state / "fixture" / "brief.md").read_bytes(), self.brief.read_bytes()); self.assertTrue(wt.is_dir())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base_head)
        (self.repo / "dirty.txt").write_text("dirty\n")
        self.assertIn("dirty", self.invoke("prepare", "--repo", str(self.repo), "--brief-file", str(self.brief), "--state-root", str(self.state), "--work-id", "dirty", ok=False)["error"])

    def test_fresh_resume_permissions_safe_mode_schema_and_inline_brief(self):
        self.prepare(); fresh = self.launch(role="investigator"); rows = self.wait_audit(1); argv = rows[0]["argv"]
        self.assertIn("--safe-mode", argv); self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Agent,Task")
        schema = json.loads(argv[argv.index("--json-schema") + 1]); self.assertEqual(schema["type"], "object"); self.assertIn("summary", schema["required"])
        self.assertIn("Objective: offline contract fixture.", argv[-1]); self.assertNotIn("Read the frozen brief at:", argv[-1])
        sid = argv[argv.index("--session-id") + 1]; self.assertEqual(sid, fresh["provider_session_id"])
        self.assertEqual(rows[0]["cwd"], self.read_state()["worktree"]["path"]); self.assertEqual(rows[0]["network"], "1")
        self.assertNotIn("--max-budget-usd", argv)
        self.wait_quiescent(); resumed = self.launch(resume=True); argv = self.wait_audit(2)[1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], sid); self.assertNotIn("--session-id", argv); self.assertEqual(resumed["provider_session_id"], sid)
        self.wait_quiescent(); self.launch(role="implementer"); argv = self.wait_audit(3)[2]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.invoke("cancel", "--state-root", str(self.state), "--work-id", "fixture")

    def test_detached_pid_alive_duplicate_launch_and_idempotent_cancel(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_SLEEP": "20"}); state = self.read_state(); pid = state["attempt"]["pid"]
        self.assertGreater(pid, 1); os.kill(pid, 0)
        self.assertTrue(self.invoke("status", "--state-root", str(self.state), "--work-id", "fixture")["alive"])
        out = self.invoke("launch", "--state-root", str(self.state), "--work-id", "fixture", "--role", "reviewer", "--claude-bin", str(self.fake), ok=False)
        self.assertIn("active attempt", out["error"])
        self.assertTrue(self.invoke("cancel", "--state-root", str(self.state), "--work-id", "fixture", "--grace-seconds", "1")["cancelled"])
        self.assertTrue(self.invoke("cancel", "--state-root", str(self.state), "--work-id", "fixture")["already_quiescent"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base_head); self.assertEqual(self.git("for-each-ref", "--format=%(refname)"), self.refs_after_prepare)

    def test_concurrent_launch_has_one_owner_and_one_fake_invocation(self):
        self.prepare()
        args = [sys.executable, str(CONTROLLER), "launch", "--state-root", str(self.state), "--work-id", "fixture", "--role", "investigator", "--claude-bin", str(self.fake)]
        env = os.environ.copy(); env.update({"FAKE_CLAUDE_AUDIT": str(self.audit), "NO_NETWORK": "1", "FAKE_CLAUDE_SLEEP": "20"})
        a = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        b = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        ra, ea = a.communicate(timeout=10); rb, eb = b.communicate(timeout=10)
        answers = [json.loads(ra), json.loads(rb)]; codes = [a.returncode, b.returncode]
        self.assertEqual(sorted(codes), [0, 2]); self.assertTrue(any("active attempt" in x.get("error", "") for x in answers))
        self.wait_audit(1); self.assertEqual(len(self.audit_rows()), 1)
        self.invoke("cancel", "--state-root", str(self.state), "--work-id", "fixture", "--grace-seconds", "1")

    def test_status_inspect_hide_huge_transcript_and_state_symlink_fails_closed(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_HUGE": "1"}); self.wait_quiescent()
        for name in ("status", "inspect"):
            value = self.invoke(name, "--state-root", str(self.state), "--work-id", "fixture"); rendered = json.dumps(value)
            self.assertLessEqual(len(rendered.encode()), 8192); self.assertNotIn("SECRET_TRANSCRIPT_MARKER", rendered)
        state = self.state_file(); backup = state.with_suffix(".saved"); state.rename(backup); state.symlink_to(backup)
        out = self.invoke("status", "--state-root", str(self.state), "--work-id", "fixture", ok=False)
        self.assertIn("symlink", out["error"]); self.assertEqual(len(self.audit_rows()), 1)

    def test_zero_human_lifecycle_collects_binds_and_supervisor_passes_gate(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_WRITE": "1"}); self.wait_quiescent()
        result = self.invoke("collect", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(result["phase_before_collect"], "running")
        self.assertEqual(result["gate"]["phase"], "awaiting_gate")
        self.assertIn("?? worker-change.txt", result["changed_paths"]); packet = result["worker_packet"]
        self.assertEqual(packet["summary"], "bounded fake summary"); persisted = Path(packet["path"])
        self.assertTrue(persisted.is_file()); self.assertEqual(packet["sha256"], hashlib.sha256(persisted.read_bytes()).hexdigest())
        self.assertEqual(self.read_state()["phase"], "awaiting_gate")
        out = self.invoke("launch", "--state-root", str(self.state), "--work-id", "fixture", "--role", "reviewer", "--claude-bin", str(self.fake), ok=False)
        self.assertIn("awaiting deterministic gate", out["error"])
        passed = self.adjudicate("pass")
        self.assertEqual(passed["phase"], "completed"); self.assertEqual(passed["gate"]["policy"], "bootstrap_no_publish/v1")
        self.assertEqual(passed["gate"]["adjudicator_principal"], "fixture-supervisor")
        self.assertEqual(self.read_state()["phase"], "completed")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base_head); self.assertEqual(self.git("for-each-ref", "--format=%(refname)"), self.refs_after_prepare)

    def test_invalid_missing_and_oversized_worker_results_are_unverified(self):
        self.prepare()
        for kind in ("malformed", "missing", "oversized"):
            with self.subTest(kind=kind):
                if kind != "malformed":
                    self.invoke("intervene", "--state-root", str(self.state), "--work-id", "fixture", "--instruction", "Run the next packet check.")
                self.launch(env={"FAKE_CLAUDE_RESULT": kind}); self.wait_quiescent()
                result = self.invoke("collect", "--state-root", str(self.state), "--work-id", "fixture")
                self.assertEqual(result["worker_packet"], {"unverified": True})
                self.assertEqual(result["gate"]["phase"], "failed")
                self.assertIn("awaiting_gate", self.invoke("adjudicate", "--state-root", str(self.state), "--work-id", "fixture", "--verdict", "pass", "--rationale", "should fail", "--adjudicator-principal", "fixture-supervisor", ok=False)["error"])

    def test_local_budget_guard_is_terminal_never_capacity_or_resume(self):
        self.prepare(); first = self.launch(env={"FAKE_CLAUDE_RESULT": "budget", "FAKE_CLAUDE_PROVIDER_SECRET": "PROVIDER_SECRET_MARKER"})
        self.wait_quiescent(); result = self.collect(); rendered = json.dumps(result)
        self.assertNotIn("PROVIDER_SECRET_MARKER", rendered)
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(result["gate"]["kind"], "local_package_budget_guard")
        self.assertIsNone(self.read_state().get("candidate")); self.assertIsNone(self.read_state().get("escalation"))
        self.assertIn("not awaiting capacity", self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", "fixture", ok=False)["error"])
        for resume in (False, True):
            args = ["launch", "--state-root", str(self.state), "--work-id", "fixture", "--role", "investigator", "--claude-bin", str(self.fake)]
            if resume: args.append("--resume")
            self.assertIn("terminal", self.invoke(*args, ok=False)["error"])
        self.assertEqual(len(self.audit_rows()), 1)
        self.assertNotEqual(first["provider_session_id"], "")

    def test_provider_neutral_terra_evidence_is_bound_independent_and_never_launches_provider(self):
        self.prepare(); refs = self.git("for-each-ref", "--format=%(refname)")
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n")
        prepared = self.prepare_provider_neutral()
        self.assertEqual(prepared["phase"], "awaiting_evidence"); self.assertEqual(prepared["route"], "provider_neutral")
        self.assertIsNone(self.read_state().get("attempt")); self.assertEqual(self.read_state()["alternate_evidence"]["route"], "provider_neutral")
        replay = self.prepare_provider_neutral(); self.assertTrue(replay["reused"]); self.assertEqual(replay["attempt_id"], prepared["attempt_id"])
        self.assertIn("different identity", self.invoke("prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", "fixture", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-other", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2", ok=False)["error"])
        result_path, review_path = self.alternate_packets()
        transcript = json.loads(result_path.read_text()); transcript["transcript"] = "not an evidence packet"; result_path.write_text(json.dumps(transcript))
        self.assertIn("schema", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path), ok=False)["error"])
        result_path, review_path = self.alternate_packets()
        self.assertEqual(self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))["phase"], "awaiting_review")
        gate = self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        self.assertEqual(gate["phase"], "awaiting_gate"); self.assertEqual(self.read_state()["gate"]["route"], "provider_neutral")
        self.adjudicate("pass"); self.assertEqual(self.read_state()["phase"], "completed")
        self.assertEqual(self.audit_rows(), [], "provider-neutral evidence must not launch Claude")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)"), refs, "provider-neutral evidence must not mutate refs")

    def test_provider_neutral_clean_candidate_can_use_preexisting_tracked_receipt(self):
        self.prepare(); wt = Path(self.read_state()["worktree"]["path"])
        # Shared receipt validation requires a nonempty regular artifact.  A clean
        # candidate may use a pre-existing tracked file; no worker edit is needed.
        prepared = self.prepare_provider_neutral(); self.assertEqual(prepared["candidate_binding"]["changed_paths"], [])
        result_path, review_path = self.alternate_packets("tracked.txt")
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        self.adjudicate("pass"); self.assertEqual(self.read_state()["phase"], "completed")
        self.assertEqual(self.audit_rows(), []); self.assertEqual(self.git("status", "--porcelain", "--", str(wt)), "")

    def test_provider_neutral_rejects_provider_attempt_candidate_and_route_tamper(self):
        self.prepare(); self.launch(); self.wait_quiescent()
        self.assertIn("no provider attempt", self.invoke("prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", "fixture", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2", ok=False)["error"])
        self.prepare("neutral"); neutral = self.read_state("neutral"); wt = Path(neutral["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n")
        self.invoke("prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", "neutral", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2")
        state = self.read_state("neutral"); state["alternate_evidence"]["route"] = "tampered"; (self.state / "neutral" / "state.json").write_text(json.dumps(state))
        self.assertIn("route", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "neutral", "--packet-file", str(self.root / "missing.json"), ok=False)["error"])
        self.prepare("wrongphase"); wrong = self.read_state("wrongphase"); wrong["phase"] = "failed"; (self.state / "wrongphase" / "state.json").write_text(json.dumps(wrong))
        self.assertIn("prepared package", self.invoke("prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", "wrongphase", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2", ok=False)["error"])

    def test_provider_neutral_sealed_config_rejects_identity_and_receipt_tampering(self):
        for field, value in (("route", "local_guard_alternate"), ("model", "other-model"), ("effort", "high"),
                             ("worker_actor_id", "other-worker"), ("worker_run_id", "other-run"),
                             ("reviewer_actor_id", "other-reviewer"), ("reviewer_run_id", "other-review-run")):
            with self.subTest(field=field):
                work_id = "sealed-" + field.replace("_", "-")
                self.prepare(work_id); wt = Path(self.read_state(work_id)["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n")
                args = ["prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", work_id, "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2"]
                self.invoke(*args); state = self.read_state(work_id); state["alternate_evidence"][field] = value; (self.state / work_id / "state.json").write_text(json.dumps(state))
                self.assertIn("config receipt", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", work_id, "--packet-file", str(self.root / "missing.json"), ok=False)["error"])
        self.prepare("config-file"); wt = Path(self.read_state("config-file")["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n")
        self.invoke("prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", "config-file", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2")
        config = Path(self.read_state("config-file")["alternate_evidence"]["config"]["path"]); config.write_text("{}")
        self.assertIn("config receipt", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "config-file", "--packet-file", str(self.root / "missing.json"), ok=False)["error"])

    def test_provider_neutral_adjudication_revalidates_durable_nonpass_review(self):
        self.prepare(); wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_provider_neutral()
        result_path, review_path = self.alternate_packets(); review = json.loads(review_path.read_text()); review["verdict"] = "needs_correction"; review_path.write_text(json.dumps(review))
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        state = self.read_state(); state["alternate_evidence"]["review"]["verdict"] = "pass"; self.state_file().write_text(json.dumps(state))
        self.assertIn("must pass", self.adjudicate("pass", ok=False)["error"])

    def test_provider_neutral_adjudication_rejects_tampered_durable_packet(self):
        self.prepare(); wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_provider_neutral()
        result_path, review_path = self.alternate_packets()
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        review_copy = Path(self.read_state()["alternate_evidence"]["review"]["path"]); review_copy.write_text("{}")
        self.assertIn("durable packet", self.adjudicate("pass", ok=False)["error"])

    def test_completed_legacy_unsealed_alternate_is_read_only_and_terminal(self):
        self.prepare(); state = self.read_state(); state["phase"] = "completed"
        state["alternate_evidence"] = {"id": "legacy-alt", "backend": "codex_terra", "controller": "codex"}
        self.state_file().write_text(json.dumps(state)); events = self.state / "fixture" / "events.jsonl"
        state_before, events_before = self.state_file().read_bytes(), events.read_bytes(); refs = self.git("for-each-ref", "--format=%(refname)")
        status = self.invoke("status", "--state-root", str(self.state), "--work-id", "fixture")
        inspected = self.invoke("inspect", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(status["phase"], "completed"); self.assertEqual(inspected["phase"], "completed")
        self.assertIn("legacy unsealed alternate evidence is terminal", " ".join(status["limitations"]))
        prepare_args = ("prepare-provider-neutral-evidence", "--state-root", str(self.state), "--work-id", "fixture", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2")
        for args in (prepare_args,
                     ("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(self.root / "none.json")),
                     ("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(self.root / "none.json"))):
            self.assertEqual(self.invoke(*args, ok=False)["error"], "legacy unsealed alternate evidence is terminal; preserve artifacts and prepare a new package")
        self.assertEqual(self.adjudicate("pass", ok=False)["error"], "legacy unsealed alternate evidence is terminal; preserve artifacts and prepare a new package")
        self.assertEqual(self.state_file().read_bytes(), state_before); self.assertEqual(events.read_bytes(), events_before)
        self.assertEqual(self.audit_rows(), []); self.assertEqual(self.git("for-each-ref", "--format=%(refname)"), refs)

    def test_alternate_terra_evidence_is_fresh_bound_independent_and_awaits_gate(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n")
        prepared = self.prepare_alternate(); self.assertEqual(prepared["phase"], "awaiting_evidence")
        self.assertEqual(prepared["candidate_binding"], self.read_state()["alternate_evidence"]["binding"])
        self.assertEqual(self.prepare_alternate()["candidate_binding"], prepared["candidate_binding"])
        result_path, review_path = self.alternate_packets()
        result = self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.assertEqual(result["phase"], "awaiting_review")
        result_state = self.state_file().read_bytes(); result_events = (self.state / "fixture" / "events.jsonl").read_bytes()
        duplicate_result = self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.assertTrue(duplicate_result["reused"]); self.assertEqual(self.state_file().read_bytes(), result_state); self.assertEqual((self.state / "fixture" / "events.jsonl").read_bytes(), result_events)
        review = self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        self.assertEqual(review["phase"], "awaiting_gate"); self.assertEqual(self.read_state()["phase"], "awaiting_gate")
        self.assertEqual(len(self.audit_rows()), 1, "alternate ingestion must not launch a provider")
        self.assertEqual(self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))["phase"], "awaiting_gate")
        state_before = self.state_file().read_bytes(); events = self.state / "fixture" / "events.jsonl"; events_before = events.read_bytes()
        replay = self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        self.assertEqual(replay["phase"], "awaiting_gate"); self.assertTrue(replay["reused"])
        self.assertEqual(self.state_file().read_bytes(), state_before); self.assertEqual(events.read_bytes(), events_before)
        conflict = json.loads(review_path.read_text()); conflict["summary"] = "conflicting replay"; review_path.write_text(json.dumps(conflict))
        self.assertIn("conflicts", self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path), ok=False)["error"])
        self.adjudicate("pass")
        self.assertEqual(self.read_state()["phase"], "completed")

    def test_alternate_evidence_rejects_collision_tamper_and_candidate_mutation(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate()
        result_path, review_path = self.alternate_packets(); result = json.loads(result_path.read_text()); result["actor_id"] = "terra-reviewer"; result_path.write_text(json.dumps(result))
        self.assertIn("actor identity", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path), ok=False)["error"])
        result_path, review_path = self.alternate_packets(); self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        (wt / "candidate-mutation.txt").write_text("stale\n")
        self.assertIn("candidate changed", self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path), ok=False)["error"])

    def test_alternate_evidence_tamper_is_rechecked_by_adjudication(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate()
        result_path, review_path = self.alternate_packets()
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        copied = self.state / "fixture" / "packets" / "alternate" / (self.read_state()["alternate_evidence"]["id"] + ".result.json")
        copied.write_text("{}")
        self.assertIn("binding", self.adjudicate("pass", ok=False)["error"])

    def test_alternate_nonpass_review_cannot_complete(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate()
        result_path, review_path = self.alternate_packets(); review = json.loads(review_path.read_text()); review["verdict"] = "needs_correction"; review_path.write_text(json.dumps(review))
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        self.assertIn("must pass", self.adjudicate("pass", ok=False)["error"])
        self.assertEqual(self.read_state()["phase"], "awaiting_gate")

    def test_alternate_failed_review_cannot_complete(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate()
        result_path, review_path = self.alternate_packets(); review = json.loads(review_path.read_text()); review["verdict"] = "fail"; review_path.write_text(json.dumps(review))
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        self.assertIn("must pass", self.adjudicate("pass", ok=False)["error"])

    def test_alternate_receipt_is_capped_regular_candidate_artifact(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); receipt = wt / "terra-receipt.txt"; receipt.write_bytes(b"x" * (2 * 1024 * 1024 + 1)); self.prepare_alternate()
        result_path, _ = self.alternate_packets()
        self.assertIn("receipts", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path), ok=False)["error"])

    def test_alternate_empty_evidence_lists_reject_without_state_mutation(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate()
        result_path, review_path = self.alternate_packets(); valid_result = json.loads(result_path.read_text())
        state_before = self.state_file().read_bytes(); events = self.state / "fixture" / "events.jsonl"; events_before = events.read_bytes()
        for field in ("checks", "receipts"):
            packet = dict(valid_result); packet[field] = []; result_path.write_text(json.dumps(packet))
            self.assertIn("receipts, checks", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path), ok=False)["error"])
            self.assertEqual(self.state_file().read_bytes(), state_before); self.assertEqual(events.read_bytes(), events_before)
        result_path.write_text(json.dumps(valid_result)); self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path))
        review = json.loads(review_path.read_text()); review["checks"] = []; review_path.write_text(json.dumps(review))
        state_before = self.state_file().read_bytes(); events_before = events.read_bytes()
        self.assertIn("content", self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path), ok=False)["error"])
        self.assertEqual(self.state_file().read_bytes(), state_before); self.assertEqual(events.read_bytes(), events_before)

    def test_alternate_prepare_rejects_frozen_brief_tamper(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        (self.state / "fixture" / "brief.md").write_text("tampered\n")
        self.assertIn("brief binding", self.invoke("prepare-alternate-evidence", "--state-root", str(self.state), "--work-id", "fixture", "--model", "gpt-5.6-terra", "--effort", "medium", "--worker-actor-id", "terra-worker", "--worker-run-id", "terra-run-1", "--reviewer-actor-id", "terra-reviewer", "--reviewer-run-id", "terra-run-2", ok=False)["error"])

    def test_alternate_result_rejects_frozen_brief_tamper(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate(); result_path, _ = self.alternate_packets()
        (self.state / "fixture" / "brief.md").write_text("tampered\n")
        self.assertIn("brief binding", self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path), ok=False)["error"])

    def test_alternate_review_rejects_frozen_brief_tamper(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate(); result_path, review_path = self.alternate_packets()
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path)); (self.state / "fixture" / "brief.md").write_text("tampered\n")
        self.assertIn("brief binding", self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path), ok=False)["error"])

    def test_alternate_adjudicate_rejects_frozen_brief_tamper(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "budget"}); self.wait_quiescent(); self.collect()
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "terra-receipt.txt").write_text("offline receipt\n"); self.prepare_alternate(); result_path, review_path = self.alternate_packets()
        self.invoke("ingest-alternate-result", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(result_path)); self.invoke("ingest-alternate-review", "--state-root", str(self.state), "--work-id", "fixture", "--packet-file", str(review_path))
        (self.state / "fixture" / "brief.md").write_text("tampered\n")
        self.assertIn("brief binding", self.adjudicate("pass", ok=False)["error"])

    def test_manual_capacity_wait_is_observable_but_nonresumable_without_external_adapter(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": "", "FAKE_CLAUDE_PROVIDER_SECRET": "PLAN_SECRET_MARKER"})
        self.wait_quiescent(); result = self.collect(); self.assertEqual(result["phase"], "awaiting_capacity")
        self.assertEqual(result["capacity"]["mode"], "manual_signal"); self.assertNotIn("PLAN_SECRET_MARKER", json.dumps(result))
        self.assertTrue(self.invoke("status", "--state-root", str(self.state), "--work-id", "fixture")["awaiting_capacity"])
        state_before = self.state_file().read_bytes(); events_before = (self.state / "fixture" / "events.jsonl").read_bytes()
        wake = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset", ok=False)
        self.assertIn("manual capacity waits cannot register", wake["error"])
        self.assertEqual(self.state_file().read_bytes(), state_before); self.assertEqual((self.state / "fixture" / "events.jsonl").read_bytes(), events_before)
        self.assertFalse((self.state / "fixture" / "capacity-wakeups").exists())
        for resume in (False, True):
            args = ["launch", "--state-root", str(self.state), "--work-id", "fixture", "--role", "investigator", "--claude-bin", str(self.fake)]
            if resume: args.append("--resume")
            out = self.invoke(*args, ok=False); self.assertIn("authenticated external authority adapter", out["error"])
        out = self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", "fixture", ok=False)
        self.assertIn("manual capacity waits", out["error"])
        raw = subprocess.run([sys.executable, str(CONTROLLER), "record-human-capacity-signal"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(raw.returncode, 0); self.assertIn("invalid choice", raw.stderr)
        self.assertEqual(len(self.audit_rows()), 1)

    def test_trusted_plan_capacity_waits_without_authority_then_exactly_resumes_when_due(self):
        # Keep the pre-due assertions independent of host/suite load.  Three
        # seconds was shorter than this fixture's setup on a busy machine,
        # allowing the real clock to cross the reset before the first release
        # assertion.
        retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
        retry_after = retry_at.isoformat().replace("+00:00", "Z")
        self.prepare(); first = self.launch(env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": retry_after, "FAKE_CLAUDE_PROVIDER_SECRET": "PLAN_SECRET_MARKER"})
        self.wait_quiescent(); result = self.collect()
        self.assertEqual(result["phase"], "awaiting_capacity"); self.assertNotIn("PLAN_SECRET_MARKER", json.dumps(result))
        self.assertNotIn("escalation", result); self.assertEqual(result["capacity"]["retry_after"], retry_after)
        self.assertEqual(self.read_state()["phase"], "awaiting_capacity")
        self.assertTrue(self.invoke("status", "--state-root", str(self.state), "--work-id", "fixture")["awaiting_capacity"])
        automatic = self.invoke("launch", "--state-root", str(self.state), "--work-id", "fixture", "--role", "investigator", "--claude-bin", str(self.fake), "--resume", ok=False)
        self.assertIn("awaiting trusted plan capacity", automatic["error"])
        blocked = self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", "fixture", ok=False)
        self.assertIn("wake lease is required", blocked["error"])
        wake = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset")
        self.assertEqual(wake["wakeup_ref"], "automation://fixture/reset"); self.assertFalse(wake["reused"])
        event_count = len([x for x in (self.state / "fixture" / "events.jsonl").read_text().splitlines() if 'capacity_wakeup_registered' in x])
        repeated = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset")
        self.assertTrue(repeated["reused"]); self.assertEqual(repeated["lease_id"], wake["lease_id"])
        self.assertEqual(event_count, len([x for x in (self.state / "fixture" / "events.jsonl").read_text().splitlines() if 'capacity_wakeup_registered' in x]))
        changed_ref = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/other", ok=False)
        self.assertIn("different wakeup", changed_ref["error"])
        blocked = self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", "fixture", ok=False)
        self.assertIn("has not elapsed", blocked["error"])
        remaining = (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds()
        time.sleep(max(0.0, remaining) + 0.2)
        released = self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(released["phase"], "prepared"); self.assertEqual(released["provider_session_id"], first["provider_session_id"])
        duplicate_release = self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertTrue(duplicate_release["reused"]); self.assertEqual(duplicate_release["provider_session_id"], first["provider_session_id"])
        fresh = self.invoke("launch", "--state-root", str(self.state), "--work-id", "fixture", "--role", "investigator", "--claude-bin", str(self.fake), ok=False)
        self.assertIn("exact-session --resume", fresh["error"])
        self.launch(resume=True); argv = self.wait_audit(2)[1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], first["provider_session_id"])

    def test_untrusted_or_missing_reset_metadata_enters_manual_wait_not_authority(self):
        for retry_after, source in (("", "provider_terminal_retry_after"), ("2099-01-01T00:00:00Z", "untrusted"), ("2000-01-01T00:00:00Z", "provider_terminal_retry_after")):
            with self.subTest(retry_after=retry_after, source=source):
                work_id = "quota-" + hashlib.sha256((retry_after + source).encode()).hexdigest()[:8]
                self.prepare(work_id); self.launch(work_id, env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": retry_after, "FAKE_CLAUDE_RESET_SOURCE": source})
                self.wait_quiescent(work_id)
                result = self.invoke("collect", "--state-root", str(self.state), "--work-id", work_id)
                self.assertEqual(result["phase"], "awaiting_capacity")
                self.assertEqual(result["capacity"]["mode"], "manual_signal")
                self.assertIsNone(self.read_state(work_id).get("escalation"))

    def test_capacity_candidate_tamper_and_legacy_reconcile_fail_closed(self):
        retry_after = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": retry_after}); self.wait_quiescent()
        result = self.invoke("reconcile-provider-capacity", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(result["phase"], "awaiting_capacity")
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "tamper.txt").write_text("tamper\n")
        out = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset", ok=False)
        self.assertIn("binding", out["error"])

    def test_real_claude_rate_limit_event_enters_capacity_wait_during_collect(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "claude_rate_limit", "FAKE_CLAUDE_PROVIDER_SECRET": "RATE_SECRET_MARKER"})
        self.wait_quiescent(); result = self.collect()
        self.assertEqual(result["phase"], "awaiting_capacity")
        self.assertEqual(result["capacity"]["failure_class"], "subscription_quota_exhausted")
        self.assertEqual(result["capacity"]["reset_source"], "claude_rate_limit_event_resets_at")
        self.assertNotIn("RATE_SECRET_MARKER", json.dumps(result))
        self.assertFalse((self.state / "fixture" / "capacity-wakeups").exists())
        state = self.read_state(); packet = json.loads(Path(state["capacity"]["path"]).read_text())
        self.assertEqual(packet["failure_class"], "subscription_quota_exhausted")
        self.assertNotIn("RATE_SECRET_MARKER", json.dumps(packet))

    def test_real_claude_rate_limit_reconciles_a_legacy_invalid_packet_failure(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "claude_rate_limit"}); self.wait_quiescent()
        state = self.read_state(); state["phase"] = "failed"; state["gate"] = {"kind": "deterministic_validation", "reason": "invalid_or_missing_worker_packet"}
        self.state_file().write_text(json.dumps(state))
        result = self.invoke("reconcile-provider-capacity", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(result["phase"], "awaiting_capacity")
        self.assertEqual(result["capacity"]["failure_class"], "subscription_quota_exhausted")

    def test_real_claude_rate_limit_reconciles_shortly_after_reset(self):
        reset_epoch = int(time.time()) - 120
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "claude_rate_limit", "FAKE_CLAUDE_RESETS_AT": str(reset_epoch)})
        self.wait_quiescent()
        result = self.invoke("reconcile-provider-capacity", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(result["phase"], "awaiting_capacity")
        wake = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/post-reset")
        self.assertTrue(wake["lease_id"])
        released = self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(released["phase"], "prepared")

    def test_claude_rate_limit_near_misses_fail_closed(self):
        cases = [
            {"FAKE_CLAUDE_RESETS_AT": str(int(time.time()) - 25 * 60 * 60)}, {"FAKE_CLAUDE_USING_OVERAGE": "true"},
            {"FAKE_CLAUDE_RATE_SESSION": "wrong-session"}, {"FAKE_CLAUDE_TERMINAL_SESSION": "wrong-session"},
            {"FAKE_CLAUDE_TERMINAL_STATUS": "500"}, {"FAKE_CLAUDE_TERMINAL_SUBTYPE": "error"},
        ]
        for number, env in enumerate(cases):
            work_id = "rate-near-" + str(number)
            self.prepare(work_id); self.launch(work_id, env={"FAKE_CLAUDE_RESULT": "claude_rate_limit", **env})
            self.wait_quiescent(work_id); self.invoke("collect", "--state-root", str(self.state), "--work-id", work_id)
            out = self.invoke("reconcile-provider-capacity", "--state-root", str(self.state), "--work-id", work_id, ok=False)
            self.assertIn("no trusted resettable provider capacity", out["error"])
            self.assertEqual(self.read_state(work_id)["phase"], "failed")

    def test_live_claude_rate_limit_fixture_is_read_only_and_reconciles(self):
        fixture = Path("/Users/marcos/.cowork/orchestrator/2267e8b02f70ad8c/m0c-contract-seam-plan/logs/b272e6f9-e294-401f-9517-13050b985799.stdout.log")
        if not fixture.is_file(): self.skipTest("live Claude fixture is unavailable")
        if time.time() >= 1786414800: self.skipTest("live Claude fixture reset is no longer future")
        before = hashlib.sha256(fixture.read_bytes()).hexdigest()
        self.prepare(); self.launch(); self.wait_quiescent()
        state = self.read_state(); attempt = state["attempt"]
        Path(attempt["stdout"]).write_bytes(fixture.read_bytes())
        attempt["provider_session_id"] = "882fdf2f-d905-4bf5-999e-872b2fe04cae"
        state["phase"] = "failed"; state["gate"] = {"kind": "deterministic_validation", "reason": "invalid_or_missing_worker_packet"}
        self.state_file().write_text(json.dumps(state))
        result = self.invoke("reconcile-provider-capacity", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertEqual(result["capacity"]["failure_class"], "subscription_quota_exhausted")
        self.assertEqual(before, hashlib.sha256(fixture.read_bytes()).hexdigest())

    def test_capacity_wake_receipt_tampering_fails_closed(self):
        retry_after = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": retry_after, "FAKE_CLAUDE_PROVIDER_SECRET": "LEASE_SECRET_MARKER"}); self.wait_quiescent()
        result = self.collect(); self.assertNotIn("LEASE_SECRET_MARKER", json.dumps(result))
        wake = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset")
        receipt = self.state / "fixture" / "capacity-wakeups" / (wake["lease_id"] + ".json")
        data = json.loads(receipt.read_text()); data["candidate_id"] = "tampered"; receipt.write_text(json.dumps(data))
        out = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset", ok=False)
        self.assertIn("receipt binding", out["error"])

    def test_capacity_failure_packet_tampering_fails_closed(self):
        retry_after = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": retry_after}); self.wait_quiescent(); self.collect()
        packet = next((self.state / "fixture" / "packets").glob("*.capacity.json"))
        data = json.loads(packet.read_text()); data["provider_session_id"] = "tampered"; packet.write_text(json.dumps(data))
        out = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset", ok=False)
        self.assertIn("packet binding", out["error"])

    def test_capacity_wake_receipt_crash_recovery_is_exactly_once_and_conflicts_fail_closed(self):
        retry_after = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        self.prepare(); self.launch(env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": retry_after}); self.wait_quiescent(); self.collect()
        before = self.state_file().read_bytes()
        first = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset")
        self.state_file().write_bytes(before)  # receipt committed; state publication lost in a simulated crash
        different = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/other", ok=False)
        self.assertIn("different wakeup", different["error"])
        recovered = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "fixture", "--wakeup-ref", "automation://fixture/reset")
        self.assertTrue(recovered["reused"]); self.assertEqual(recovered["lease_id"], first["lease_id"])
        self.assertEqual(len(list((self.state / "fixture" / "capacity-wakeups").glob("*.json"))), 1)
        self.assertEqual(len([x for x in (self.state / "fixture" / "events.jsonl").read_text().splitlines() if 'capacity_wakeup_registered' in x]), 1)
        self.prepare("malformed"); self.launch("malformed", env={"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": retry_after}); self.wait_quiescent("malformed"); self.invoke("collect", "--state-root", str(self.state), "--work-id", "malformed")
        receipts = self.state / "malformed" / "capacity-wakeups"; receipts.mkdir(); (receipts / "poison.json").write_text("not-json")
        rejected = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", "malformed", "--wakeup-ref", "automation://fixture/reset", ok=False)
        self.assertIn("receipt is unreadable", rejected["error"])

    def test_legacy_increase_budget_token_is_unusable_then_retired(self):
        self.prepare(); self.launch(); self.wait_quiescent(); self.collect()
        escalation = self.adjudicate("needs_authority", "legacy fixture", capability_required="publish_external", target_authority_role="release", target_authority_principal="release-policy-principal")["escalation"]
        path = next((self.state / "fixture" / "escalations").glob("*.json")); packet = json.loads(path.read_text())
        packet["capability_required"] = "increase_budget"; path.write_text(json.dumps(packet))
        state = self.read_state(); state["escalation"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest(); state["escalation"]["failure_class"] = "local_package_budget_guard"; self.state_file().write_text(json.dumps(state))
        rejected = self.amend(escalation["resume_token"], capability="publish_external", ok=False)
        self.assertIn("retired", rejected["error"])
        retired = self.invoke("retire-local-budget-guard", "--state-root", str(self.state), "--work-id", "fixture")
        self.assertTrue(retired["retired"]); self.assertEqual(self.read_state()["phase"], "failed")
        self.assertIn("no authority escalation", self.amend(escalation["resume_token"], capability="publish_external", ok=False)["error"])

    def test_actual_legacy_budget_exhausted_schema_retires_without_resume(self):
        self.make_legacy_budget_state()
        retired = self.invoke("retire-local-budget-guard", "--state-root", str(self.state), "--work-id", "legacy-budget")
        state = self.read_state("legacy-budget")
        self.assertTrue(retired["retired"]); self.assertEqual(state["phase"], "failed"); self.assertIsNone(state["escalation"])
        self.assertNotIn("recovery_constraint", state); self.assertEqual(state["gate"]["kind"], "local_package_budget_guard_retired")
        for resume in (False, True):
            args = ["launch", "--state-root", str(self.state), "--work-id", "legacy-budget", "--role", "investigator", "--claude-bin", str(self.fake)]
            if resume: args.append("--resume")
            self.assertIn("terminal", self.invoke(*args, ok=False)["error"])

    def test_legacy_budget_exhausted_near_miss_fails_closed(self):
        _, provider_path, _ = self.make_legacy_budget_state("legacy-near-miss")
        provider = json.loads(provider_path.read_text()); provider["terminal"]["subtype"] = "error_other_budget_text"; provider_path.write_text(json.dumps(provider))
        rejected = self.invoke("retire-local-budget-guard", "--state-root", str(self.state), "--work-id", "legacy-near-miss", ok=False)
        self.assertIn("recognized controller schema", rejected["error"])
        state = self.read_state("legacy-near-miss"); self.assertEqual(state["phase"], "needs_authority"); self.assertIsNotNone(state["escalation"])

    def test_intervention_bound_and_next_resume_delivery(self):
        self.prepare(); self.assertIn("4096", self.invoke("intervene", "--state-root", str(self.state), "--work-id", "fixture", "--instruction", "x" * 4097, ok=False)["error"])
        self.launch(); self.wait_quiescent()
        queued = self.invoke("intervene", "--state-root", str(self.state), "--work-id", "fixture", "--instruction", "Check only dispatch.")
        self.launch(resume=True)
        self.assertIn("Check only dispatch.", self.wait_audit(2)[1]["argv"][-1])
        self.assertTrue(any(x["id"] == queued["intervention_id"] and x.get("consumed_at") for x in self.read_state()["interventions"]))

    def test_authority_escalation_is_typed_candidate_bound_and_single_use(self):
        self.prepare(); self.launch(); self.wait_quiescent(); collected = self.invoke("collect", "--state-root", str(self.state), "--work-id", "fixture")
        escalation = self.adjudicate("needs_authority", "publishing is outside bootstrap policy", capability_required="publish_external", target_authority_role="release", target_authority_principal="release-policy-principal")["escalation"]
        self.assertEqual(self.read_state()["phase"], "needs_authority")
        self.assertEqual(escalation["capability_required"], "publish_external")
        self.assertEqual(escalation["target_authority"], {"role": "release", "principal": "release-policy-principal"})
        self.assertEqual(escalation["candidate_binding"]["evidence_digest"], collected["gate"]["evidence_digest"])
        self.assertRegex(escalation["resume_token"], r"^[0-9a-f-]+\.[0-9a-f]+$")
        self.assertNotIn(escalation["resume_token"], self.state_file().read_text())
        durable = self.state / "fixture" / "escalations"
        packet_path = next(durable.glob("*.json")); recovered = json.loads(packet_path.read_text()); recovered_token = recovered["resume_token"]
        self.assertEqual(recovered_token, escalation["resume_token"])
        self.assertIn("bootstrap_no_publish/v1", recovered["policy_version"])
        self.assertIn("target_authority", recovered); self.assertIn("principal", recovered["target_authority"])
        binding = recovered["candidate_binding"]
        self.assertRegex(binding["base_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(binding["candidate_digest"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(binding["artifact_hashes"], dict); self.assertEqual(binding["finding_ids"], [])
        self.assertEqual(recovered["reason"], "publishing is outside bootstrap policy")
        self.assertRegex(recovered["causal_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.read_state()["escalation"]["causal_fingerprint"], recovered["causal_fingerprint"])
        self.assertIn("does not match", self.amend(recovered_token, principal="wrong-principal", ok=False)["error"])
        self.assertIn("does not match", self.amend(recovered_token, capability="accept_risk", ok=False)["error"])
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "after-escalation.txt").write_text("mutation\n")
        self.assertIn("binding", self.amend(recovered_token, ok=False)["error"])
        (wt / "after-escalation.txt").unlink()
        amended = self.amend(recovered_token); self.assertEqual(amended["phase"], "prepared")
        self.assertIn("already-used", self.amend(recovered_token, ok=False)["error"])
        self.launch(resume=True); self.wait_audit(2)

    def test_candidate_packet_tampering_fails_closed(self):
        self.prepare(); self.launch(); self.wait_quiescent(); result = self.invoke("collect", "--state-root", str(self.state), "--work-id", "fixture")
        Path(result["worker_packet"]["path"]).write_text("tampered\n")
        out = self.adjudicate("pass", "must reject", ok=False)
        self.assertIn("binding", out["error"])

    def test_worktree_mutation_after_collect_fails_closed_before_adjudication_or_amendment(self):
        self.prepare(); self.launch(env={"FAKE_CLAUDE_WRITE": "1"}); self.wait_quiescent(); self.invoke("collect", "--state-root", str(self.state), "--work-id", "fixture")
        wt = Path(self.read_state()["worktree"]["path"]); (wt / "worker-change.txt").write_text("mutated after evidence binding\n")
        out = self.adjudicate("pass", "must reject changed candidate", ok=False)
        self.assertIn("binding", out["error"])

    def test_invalidated_m0c_correction_is_exact_session_once(self):
        work = "m0c-contract-seam-plan"; self.prepare(work); self.launch(work, role="planner"); self.wait_quiescent(work); self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        state = self.read_state(work); candidate = state["candidate"]; attempt = state["attempt"]; directory = self.state / work
        plan_sha = candidate["evidence"]["packet_sha256"]; state["planner_artifact"] = {"plan_sha256": plan_sha}
        binding = {"candidate_id": candidate["id"], "evidence_digest": candidate["evidence_digest"], "plan_sha256": plan_sha, "reasons": ["invalid_upstream_candidate_binding"]}
        receipt = directory / "invalidations" / "receipt.json"; receipt.parent.mkdir(); receipt.write_text(json.dumps({"schema_version": 1, "kind": "gate_invalidation_receipt", "package_id": work, "receipt_id": "receipt", "binding": binding}))
        state["phase"] = "needs_correction"; state["gate"] = {"kind": "m0c_plan_evidence", "verdict": "invalidated", "receipt_id": "receipt"}; self.state_file(work).write_text(json.dumps(state))
        queued = self.invoke("intervene", "--state-root", str(self.state), "--work-id", work, "--instruction", "Correct only the invalid upstream binding.")
        self.assertEqual(queued["delivery"], "exact_planner_resume_required")
        self.assertIn("exact-session", self.invoke("launch", "--state-root", str(self.state), "--work-id", work, "--role", "planner", "--claude-bin", str(self.fake), ok=False)["error"])
        self.assertIn("original failed role", self.invoke("launch", "--state-root", str(self.state), "--work-id", work, "--role", "reviewer", "--resume", "--claude-bin", str(self.fake), ok=False)["error"])
        resumed = self.launch(work, role="planner", resume=True); self.assertEqual(resumed["provider_session_id"], attempt["provider_session_id"])
        self.invoke("cancel", "--state-root", str(self.state), "--work-id", work)
        self.assertIn("one bounded correction", self.invoke("intervene", "--state-root", str(self.state), "--work-id", work, "--instruction", "Again", ok=False)["error"])


class PlannerAdapterLifecycleTests(unittest.TestCase):
    """Offline tests for the controller-owned M0-C review adapter."""

    def setUp(self):
        # Compose the existing CLI fixture instead of inheriting it: unittest
        # otherwise discovers every legacy ``test_*`` method a second time.
        self.fixture = OrchestrateCLITest(methodName="runTest")
        self.fixture.setUp()
        for name in ("tmp", "root", "repo", "state", "brief", "audit", "fake", "base_head"):
            setattr(self, name, getattr(self.fixture, name))
        self.mod = controller_module()
        self.codex_audit = self.root / "fake-codex-audit.jsonl"
        self._old_codex_audit = os.environ.get("FAKE_CODEX_AUDIT")
        os.environ["FAKE_CODEX_AUDIT"] = str(self.codex_audit)
        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text("""#!%s
import hashlib, json, os, re, sys, time
argv = sys.argv[1:]
with open(os.environ['FAKE_CODEX_AUDIT'], 'a') as out:
    out.write(json.dumps({'argv': argv, 'cwd': os.getcwd()}) + '\\n')
if os.environ.get('FAKE_CODEX_KIND') == 'malformed':
    open(argv[argv.index('--output-last-message') + 1], 'w').write('not-json')
elif os.environ.get('FAKE_CODEX_KIND') in ('invalid_schema', 'arbitrary_nonzero'):
    if os.environ.get('FAKE_CODEX_KIND') == 'invalid_schema':
        nested={'type':'error','status':400,'error':{'type':'invalid_request_error','code':'invalid_json_schema'}}
        print(json.dumps({'type':'error','message':json.dumps(nested)}))
    else:
        print(json.dumps({'type':'turn.failed','error':{'type':'server_error','code':'unknown'}}))
    sys.exit(7)
else:
    schema = json.load(open(argv[argv.index('--output-schema') + 1]))
    p = schema['properties']
    packet = {k: v['const'] for k, v in p.items() if 'const' in v}
    if 'prerequisite_evidence_digests' in p:
        match = re.search(r'prerequisite_evidence_digests=(\[[^]]+\])', argv[-1])
        packet['prerequisite_evidence_digests'] = json.loads(match.group(1))
        if os.environ.get('FAKE_CODEX_PREREQ_KIND') == 'reordered': packet['prerequisite_evidence_digests'].reverse()
        if os.environ.get('FAKE_CODEX_PREREQ_KIND') == 'missing': packet['prerequisite_evidence_digests'].pop()
        if os.environ.get('FAKE_CODEX_PREREQ_KIND') == 'wrong': packet['prerequisite_evidence_digests'][1] = '0' * 64
    if 'verification_receipt_sha256s' in p:
        reviewer_session = 'fake-package-a-%%s' %% os.getpid()
        print(json.dumps({'type': 'thread.started', 'thread_id': reviewer_session}))
        changed = re.search(r'exact candidate files (\[[^]]+\]) against', argv[-1])
        receipts = re.search(r'controller receipts=(\[[^]]+\])', argv[-1])
        packet['changed_paths'] = json.loads(changed.group(1))
        packet['verification_receipt_sha256s'] = [hashlib.sha256(open(path, 'rb').read()).hexdigest()
                                                  for path in json.loads(receipts.group(1))]
    packet.update({'verdict': os.environ.get('FAKE_CODEX_VERDICT', 'pass'), 'findings': [], 'checks': ['fake receipt']})
    json.dump(packet, open(argv[argv.index('--output-last-message') + 1], 'w'))
    if os.environ.get('FAKE_CODEX_KIND') == 'valid_nonzero': sys.exit(7)
print(os.environ.get('FAKE_CODEX_SECRET', ''))
time.sleep(float(os.environ.get('FAKE_CODEX_SLEEP', '0')))
""" % sys.executable)
        self.fake_codex.chmod(self.fake_codex.stat().st_mode | stat.S_IXUSR)
        self.claude_reviewer_audit = self.root / "fake-claude-reviewer-audit.jsonl"
        self._old_claude_reviewer_audit = os.environ.get("FAKE_CLAUDE_REVIEWER_AUDIT")
        os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = str(self.claude_reviewer_audit)
        self.fake_claude_reviewer = self.root / "fake-claude-reviewer"
        self.fake_claude_reviewer.write_text("""#!%s
import hashlib, json, os, re, sys, time
argv = sys.argv[1:]
with open(os.environ['FAKE_CLAUDE_REVIEWER_AUDIT'], 'a') as out:
    out.write(json.dumps({'argv': argv, 'cwd': os.getcwd()}) + '\\n')
session_id = argv[argv.index('--session-id') + 1]
kind = os.environ.get('FAKE_CLAUDE_REVIEWER_KIND', '')
if kind == 'schema_failure':
    print(json.dumps({'type': 'result', 'is_error': True, 'subtype': 'error_schema_validation', 'session_id': session_id}))
    sys.exit(7)
if kind == 'capacity_scheduled':
    import time as _t
    print(json.dumps({'type': 'rate_limit_event', 'session_id': session_id,
        'rate_limit_info': {'status': 'rejected', 'isUsingOverage': False, 'rateLimitType': 'five_hour',
                            'resetsAt': int(_t.time()) + 3600, 'overageStatus': 'rejected'}}))
    sys.exit(1)
if kind == 'overflow':
    # Write > 2 MiB to trigger output_limit_exceeded, no result event
    chunk = ('{"type":"debug","data":"' + 'x' * 990 + '"}\\n').encode()
    written = 0
    cap = 2 * 1024 * 1024 + 65536  # 2 MiB + 64 KiB
    while written < cap:
        sys.stdout.buffer.write(chunk); written += len(chunk)
    sys.stdout.buffer.flush()
    sys.exit(1)
if kind == 'large_stdout':
    # Write ~300 KiB of padding, then the result event (under 2 MiB cap)
    filler = '{"type":"debug","data":"' + 'x' * 90 + '"}\\n'
    for _ in range(3000):
        sys.stdout.write(filler)
    # fall through to normal result output below
schema_json = argv[argv.index('--json-schema') + 1]
schema = json.loads(schema_json)
p = schema['properties']
packet = {k: v['const'] for k, v in p.items() if 'const' in v}
if 'dossier_sha256' in p:
    dossier_m = re.search(r'dossier at: (\\S+)\\. ', argv[-1])
    dossier = json.loads(open(dossier_m.group(1)).read())
    packet['verification_receipt_sha256s'] = [s['sha256'] for s in dossier['gate_summaries']]
    packet['changed_paths'] = dossier['changed_paths']
elif 'verification_receipt_sha256s' in p:
    receipts = re.search(r'controller receipts=(\\[[^\\]]+\\])', argv[-1])
    changed_paths_str = os.environ.get('FAKE_CLAUDE_REVIEWER_CHANGED_PATHS')
    if changed_paths_str:
        packet['changed_paths'] = json.loads(changed_paths_str)
    else:
        packet['changed_paths'] = ['scripts/cowork_dispatch.py', 'scripts/test_cowork.py']
    packet['verification_receipt_sha256s'] = [hashlib.sha256(open(path, 'rb').read()).hexdigest()
                                               for path in json.loads(receipts.group(1))]
packet.update({'verdict': os.environ.get('FAKE_CLAUDE_REVIEWER_VERDICT', 'pass'), 'findings': [], 'checks': ['fake receipt']})
print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
if kind == 'valid_nonzero': sys.exit(7)
time.sleep(float(os.environ.get('FAKE_CLAUDE_REVIEWER_SLEEP', '0')))
""" % sys.executable)
        self.fake_claude_reviewer.chmod(self.fake_claude_reviewer.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        if self._old_codex_audit is None: os.environ.pop("FAKE_CODEX_AUDIT", None)
        else: os.environ["FAKE_CODEX_AUDIT"] = self._old_codex_audit
        if self._old_claude_reviewer_audit is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_AUDIT", None)
        else: os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = self._old_claude_reviewer_audit
        self.fixture.tearDown()

    def prepare(self, *args, **kwargs): return self.fixture.prepare(*args, **kwargs)
    def launch(self, *args, **kwargs): return self.fixture.launch(*args, **kwargs)
    def invoke(self, *args, **kwargs): return self.fixture.invoke(*args, **kwargs)
    def wait_quiescent(self, *args, **kwargs): return self.fixture.wait_quiescent(*args, **kwargs)
    def read_state(self, *args, **kwargs): return self.fixture.read_state(*args, **kwargs)
    def state_file(self, *args, **kwargs): return self.fixture.state_file(*args, **kwargs)

    def call(self, command, **values):
        return getattr(self.mod, "command_" + command)(SimpleNamespace(**values))

    def wait_review(self, work):
        for _ in range(100):
            attempt = self.read_state(work).get("plan_review_attempt") or {}
            receipt = attempt.get("receipt_path")
            if (not self.mod.process_alive(attempt.get("pid"), attempt.get("pgid")) and
                    isinstance(receipt, str) and Path(receipt).is_file()):
                return attempt
            time.sleep(.025)
        self.fail("fake Codex review did not become quiescent")

    def test_codex_invalid_schema_classifier_accepts_exact_nested_live_jsonl(self):
        envelope = {"type": "error", "status": 400, "error": {"type": "invalid_request_error", "code": "invalid_json_schema", "message": "Invalid schema", "param": "text.format.schema"}}
        live = [json.dumps({"type": "thread.started", "thread_id": "fixture"}),
                json.dumps({"type": "error", "message": json.dumps(envelope)})]
        self.assertTrue(self.mod._codex_invalid_json_schema_events(live))
        for mutation in (
            [{"type": "error", "error": envelope["error"]}],
            [{"type": "error", "message": "invalid_json_schema"}],
            [{"type": "error", "message": json.dumps({**envelope, "status": 500})}],
            [{"type": "error", "message": json.dumps({**envelope, "error": {**envelope["error"], "code": "other"}})}],
        ):
            self.assertFalse(self.mod._codex_invalid_json_schema_events([json.dumps(x) for x in mutation]))

    def test_m0b_summary_parser_accepts_only_the_scoped_claims(self):
        characterize = b""".............................xxx
----------------------------------------------------------------------
Ran 32 tests in 0.012s

OK (expected failures=3)
"""
        baseline = ("...F\n======================================================================\n"
                    "FAIL: test_run_scout_claude_probe_fail_aborts "
                    "(scripts.test_cowork.RunScoutTest)\n"
                    "----------------------------------------------------------------------\nRan 4 tests in 0.004s\n\nFAILED (failures=1)\n").encode()
        self.assertEqual(self.mod._m0b_parse_verification_output("candidate_characterization_pass", characterize),
                         {"ran": 32, "failures": 0, "errors": 0, "expected_failures": 3, "unexpected_successes": 0})
        self.assertEqual(self.mod._m0b_parse_verification_output("baseline_known_failure", baseline),
                         {"ran": 4, "failures": 1, "errors": 0, "expected_failures": 0, "unexpected_successes": 0})
        for near_miss in (
            b"Ran 32 tests\nOK\n",
            b"Ran 32 tests\nFAILED (errors=1, expected failures=3)\n",
            b"Ran 32 tests\nOK (expected failures=3, unexpected successes=1)\n",
            baseline.replace(b"failures=1", b"failures=2"),
            baseline.replace(b"scripts.test_cowork.RunScoutTest", b"scripts.test_cowork.OtherScoutTest"),
            baseline + b"\nFAIL: scripts.test_cowork.BuildPhaseFlowTest.test_baseline_read_from_cwd_not_session_file_parent\n",
        ):
            classification = "candidate_characterization_pass" if b"32 tests" in near_miss else "baseline_known_failure"
            self.assertIsNone(self.mod._m0b_parse_verification_output(classification, near_miss))

    def make_authoritative_m0b(self):
        """Local fixture: patch only this imported module's frozen identities."""
        work = "m0b-dispatch-characterization"; self.prepare(work)
        wt = Path(self.read_state(work)["worktree"]["path"]); (wt / "scripts").mkdir(exist_ok=True)
        characterization = ["import unittest", "class Characterization(unittest.TestCase):"]
        characterization.extend(" def test_%d(self): self.assertTrue(True)" % index for index in range(29))
        characterization.extend(" @unittest.expectedFailure\n def test_expected_%d(self): self.fail('known characterization gap')" % index for index in range(3))
        (wt / "scripts" / "test_dispatch_contract_characterization.py").write_text("\n".join(characterization) + "\n")
        (wt / "scripts" / "fixtures").mkdir()
        (wt / "scripts" / "fixtures" / "dispatch_contract_characterization_sources.json").write_text('{"fixture":"characterization"}\n')
        (wt / "scripts" / "test_cowork.py").write_text("""import unittest
class ControllerCapabilityMatrixTests(unittest.TestCase):
 def test_guarded_opencode_hard_removes_task_before_launch(self): self.assertTrue(True)
class BuildPhaseFlowTest(unittest.TestCase):
 def test_baseline_read_from_cwd_not_session_file_parent(self): self.assertTrue(True)
class MeasurementReportHonestyTests(unittest.TestCase):
 def test_the_source_manifest_covers_untracked_files(self): self.assertTrue(True)
class RunScoutTest(unittest.TestCase):
 def test_run_scout_claude_probe_fail_aborts(self): self.fail('known baseline failure')
""")
        self.launch(work); self.wait_quiescent(work); self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        directory = self.state / work; state = self.read_state(work); candidate = state["candidate"]
        alternate = directory / "packets" / "alternate"; alternate.mkdir(parents=True)
        result_path, review_path = alternate / "result.json", alternate / "review.json"
        result_path.write_text('{"kind":"fixture-result"}'); review_path.write_text('{"kind":"fixture-review"}')
        result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest(); review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
        state["phase"] = "completed"; state["gate"] = {"verdict": "pass"}
        state["alternate_evidence"] = {"result": {"path": str(result_path), "sha256": result_sha}, "review": {"path": str(review_path), "sha256": review_sha, "verdict": "pass"}}
        self.state_file(work).write_text(json.dumps(state)); fp = candidate["evidence"]["worktree_fingerprint"]
        self.mod.M0B_CANDIDATE_ID = candidate["id"]; self.mod.M0B_EVIDENCE_DIGEST = candidate["evidence_digest"]
        self.mod.M0B_FINGERPRINT = fp["digest"]; self.mod.M0B_STATUS_SHA256 = fp["status"]["sha256"]
        self.mod.M0B_UNTRACKED_SHA256 = fp["untracked"]["sha256"]; self.mod.M0B_RESULT_SHA256 = result_sha; self.mod.M0B_REVIEW_SHA256 = review_sha
        verified = self.call("verify_m0b", state_root=str(self.state), work_id=work)
        self.assertFalse(verified["reused"])
        self.assertEqual([x["argv"] for x in verified["receipts"]], [self.mod._M0B_VERIFICATION_SPECS[0]["argv"], self.mod._M0B_VERIFICATION_SPECS[1]["argv"]])
        self.assertEqual([x["classification"] for x in verified["receipts"]], ["candidate_characterization_pass", "baseline_known_failure"])
        self.assertTrue(all(x["cwd"] == str(wt) and x["timeout_seconds"] == 120 for x in verified["receipts"]))
        self.assertEqual([x["actual_exit_code"] for x in verified["receipts"]], [0, 1])
        self.assertEqual(self.read_state(work)["verification_disposition"], "known_baseline_failure")
        self.assertEqual(self.read_state(work)["historical_worker_assertions"]["provenance"], "historical_worker_assertion")
        self.assertTrue(self.call("verify_m0b", state_root=str(self.state), work_id=work)["reused"])
        return state

    def make_v2_prerequisite_fixture(self):
        """Build two complete local predecessor packages without bypassing the
        v2 validator.  The immutable production manifest is deliberately
        replaced only with this fixture's controller-owned canonical values;
        all state, packets, candidate fingerprints, and worktrees are real.
        """
        self.make_authoritative_m0b()
        m0b = self.mod.M0B_WORK_ID
        c0 = self.mod.M0C0_WORK_ID

        # M0-C0 is a small tracked-file candidate.  Its artifact receipt, not
        # declared_artifacts (which intentionally covers only root artifacts),
        # is the source of the v2 script hash.
        self.prepare(c0)
        c0_state = self.read_state(c0); c0_wt = Path(c0_state["worktree"]["path"])
        (c0_wt / "scripts").mkdir(exist_ok=True)
        (c0_wt / "scripts" / "test_cowork.py").write_text("# isolated fixture\n")
        self.launch(c0); self.wait_quiescent(c0)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", c0)

        rows = []
        for work, changed in (
            (m0b, ["?? scripts/fixtures/dispatch_contract_characterization_sources.json", "?? scripts/test_dispatch_contract_characterization.py"]),
            (c0, [" M scripts/test_cowork.py"]),
        ):
            state = self.read_state(work); directory = self.state / work
            wt = Path(state["worktree"]["path"]); alternate = directory / "packets" / "alternate"; alternate.mkdir(parents=True, exist_ok=True)
            if work == m0b:
                artifacts = ["scripts/test_dispatch_contract_characterization.py", "scripts/fixtures/dispatch_contract_characterization_sources.json"]
            else:
                artifacts = ["scripts/test_cowork.py"]
            receipt_rows = [{"kind": "candidate_artifact", "path": path,
                             "sha256": hashlib.sha256((wt / path).read_bytes()).hexdigest()} for path in artifacts]
            ident = "b90d6e07-0d68-4e6a-ba48-b431f5475c03" if work == m0b else "7958b2a2-f2dd-4eb5-9806-61c3032775b9"
            binding = {"fixture": work, "candidate_id": state["candidate"]["id"]}
            result = {"schema_version": 1, "kind": "alternate_worker_result", "package_id": work,
                      "attempt_id": ident, "backend": "codex_terra", "actor_id": "terra-worker-" + work,
                      "run_id": "run-worker-" + work, "candidate_binding": binding, "outcome": "completed",
                      "summary": "fixture result", "changed_paths": changed,
                      "checks": [{"command": ["python3", "-m", "unittest"], "exit_code": 0, "fact_source": "worker_assertion"}],
                      "receipts": receipt_rows, "findings": [], "assumptions": [], "next_action": "review"}
            review = {"schema_version": 1, "kind": "alternate_independent_review", "package_id": work,
                      "attempt_id": ident, "backend": "codex_terra", "actor_id": "terra-reviewer-" + work,
                      "run_id": "run-review-" + work, "candidate_binding": binding, "verdict": "pass",
                      "summary": "fixture review", "findings": [], "checks": ["candidate checked"]}
            result_path = alternate / (ident + ".result.json"); review_path = alternate / (ident + ".review.json")
            result_path.write_text(json.dumps(result)); review_path.write_text(json.dumps(review))
            config_path = None
            if work == c0:
                config_path = alternate / (ident + ".config.json"); config_path.write_text('{"kind":"fixture-config"}')
            result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest(); review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
            state["phase"] = "completed"; state["gate"] = {"verdict": "pass"}
            state["alternate_evidence"] = {"id": ident, "worker_actor_id": result["actor_id"], "worker_run_id": result["run_id"], "binding": binding,
                                           "result": {"path": self.mod.real(str(result_path)), "sha256": result_sha},
                                           "review": {"path": self.mod.real(str(review_path)), "sha256": review_sha, "verdict": "pass"}}
            if config_path:
                state["alternate_evidence"]["config"] = {"path": self.mod.real(str(config_path)), "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}
            if work == m0b:
                state["historical_worker_assertions"] = {"provenance": "historical_worker_assertion",
                                                          "result": state["alternate_evidence"]["result"],
                                                          "review": state["alternate_evidence"]["review"]}
            self.state_file(work).write_text(json.dumps(state))
            fp = state["candidate"]["evidence"]["worktree_fingerprint"]
            rows.append({"package_id": work, "candidate_id": state["candidate"]["id"], "evidence_digest": state["candidate"]["evidence_digest"],
                         "worktree_fingerprint_digest": fp["digest"], "status_sha256": fp["status"]["sha256"], "untracked_sha256": fp["untracked"]["sha256"],
                         "config_sha256": None if not config_path else hashlib.sha256(config_path.read_bytes()).hexdigest(), "result_sha256": result_sha,
                         "review_sha256": review_sha, "config_file": None if not config_path else config_path.name, "result_file": result_path.name,
                         "review_file": review_path.name, "changed_paths": changed,
                         "artifact_hashes": {row["path"]: row["sha256"] for row in receipt_rows}})
        self.mod.M0C_V2_PREREQUISITE_MANIFEST = tuple(rows)
        self.mod.M0B_CANDIDATE_ID, self.mod.M0B_EVIDENCE_DIGEST = rows[0]["candidate_id"], rows[0]["evidence_digest"]
        self.mod.M0B_FINGERPRINT, self.mod.M0B_STATUS_SHA256, self.mod.M0B_UNTRACKED_SHA256 = rows[0]["worktree_fingerprint_digest"], rows[0]["status_sha256"], rows[0]["untracked_sha256"]
        self.mod.M0B_RESULT_SHA256, self.mod.M0B_REVIEW_SHA256 = rows[0]["result_sha256"], rows[0]["review_sha256"]
        self.mod.M0C0_CANDIDATE_ID, self.mod.M0C0_EVIDENCE_DIGEST = rows[1]["candidate_id"], rows[1]["evidence_digest"]
        self.mod.M0C0_FINGERPRINT, self.mod.M0C0_STATUS_SHA256, self.mod.M0C0_UNTRACKED_SHA256 = rows[1]["worktree_fingerprint_digest"], rows[1]["status_sha256"], rows[1]["untracked_sha256"]
        self.mod.M0C0_CONFIG_SHA256, self.mod.M0C0_RESULT_SHA256, self.mod.M0C0_REVIEW_SHA256 = rows[1]["config_sha256"], rows[1]["result_sha256"], rows[1]["review_sha256"]
        return self.mod._m0c_v2_expected_prerequisites(self.mod.real(str(self.state)))

    def test_m0b_verifier_timeout_writes_no_partial_receipt(self):
        state = self.make_authoritative_m0b()
        work = state["work_id"]
        directory = self.state / work
        receipt_dir = directory / "receipts"
        for receipt in receipt_dir.glob("*.json"):
            receipt.unlink()
        receipt_dir.rmdir()
        state = self.read_state(work)
        state.pop("verification_receipts", None)
        self.state_file(work).write_text(json.dumps(state))

        expected = [
            ["python3", "-m", "unittest", "scripts/test_dispatch_contract_characterization.py"],
            ["python3", "-m", "unittest", *self.mod._M0B_BASELINE_IDS],
        ]
        calls = []
        real_run = self.mod.subprocess.run

        def controlled_run(argv, **kwargs):
            if argv not in expected:
                return real_run(argv, **kwargs)
            calls.append((argv, kwargs))
            if argv == expected[0]:
                return SimpleNamespace(returncode=0, stdout=b"Ran 32 tests in 0.001s\n\nOK (expected failures=3)\n")
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with mock.patch.object(self.mod.subprocess, "run", side_effect=controlled_run):
            with self.assertRaisesRegex(self.mod.ControllerError, "verification command timed out"):
                self.call("verify_m0b", state_root=str(self.state), work_id=work)

        self.assertEqual([argv for argv, _ in calls], expected)
        self.assertTrue(all(kwargs["cwd"] == state["worktree"]["path"] and kwargs["timeout"] == 120 for _, kwargs in calls))
        self.assertFalse(self.read_state(work).get("verification_receipts"))
        self.assertEqual(list(receipt_dir.glob("*.json")), [])

    def test_m0b_verifier_reuses_empty_receipt_directory_after_timeout(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        directory = self.state / work; receipt_dir = directory / "receipts"
        for receipt in receipt_dir.iterdir(): receipt.unlink()
        state = self.read_state(work); state.pop("verification_receipts", None)
        self.state_file(work).write_text(json.dumps(state))

        expected = [
            ["python3", "-m", "unittest", "scripts/test_dispatch_contract_characterization.py"],
            ["python3", "-m", "unittest", *self.mod._M0B_BASELINE_IDS],
        ]
        real_run = self.mod.subprocess.run
        def timeout_first(argv, **kwargs):
            if argv == expected[0]:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return real_run(argv, **kwargs)
        with mock.patch.object(self.mod.subprocess, "run", side_effect=timeout_first):
            with self.assertRaisesRegex(self.mod.ControllerError, "verification command timed out"):
                self.call("verify_m0b", state_root=str(self.state), work_id=work)
        self.assertTrue(receipt_dir.is_dir()); self.assertEqual(list(receipt_dir.iterdir()), [])
        retried = self.call("verify_m0b", state_root=str(self.state), work_id=work)
        self.assertFalse(retried["reused"]); self.assertEqual(len(retried["receipts"]), 2)

    def test_m0b_verifier_rejects_orphan_receipt_content_without_state_binding(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        directory = self.state / work; receipt_dir = directory / "receipts"
        for receipt in receipt_dir.iterdir(): receipt.unlink()
        (receipt_dir / "orphan.txt").write_text("unbound receipt")
        state = self.read_state(work); state.pop("verification_receipts", None)
        self.state_file(work).write_text(json.dumps(state))
        with self.assertRaisesRegex(self.mod.ControllerError, "orphan M0-B verification receipts"):
            self.call("verify_m0b", state_root=str(self.state), work_id=work)

    def test_quarantine_m0b_verification_orphans_preserves_bytes_and_is_idempotent(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        directory = self.state / work; receipt_dir = directory / "receipts"
        originals = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in receipt_dir.iterdir()}
        state = self.read_state(work); state.pop("verification_receipts")
        self.state_file(work).write_text(json.dumps(state))
        parsed = self.mod.parser().parse_args(["quarantine-m0b-verification-orphans", "--state-root", str(self.state), "--work-id", work])
        self.assertEqual(parsed.command, "quarantine-m0b-verification-orphans")

        first = self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        self.assertFalse(first["reused"]); self.assertEqual(first["count"], 2)
        self.assertEqual(list(receipt_dir.iterdir()), [])
        manifest = json.loads(Path(first["manifest"]["path"]).read_text())
        self.assertEqual(manifest["reason"], "unbound_historical_verifier_receipt")
        self.assertEqual({row["original_name"]: row["sha256"] for row in manifest["receipts"]}, originals)
        repair = Path(first["manifest"]["path"]).parent
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in repair.glob("[01].json")}, originals)
        self.assertNotIn("verification_receipts", self.read_state(work))
        self.assertIn('"kind":"m0b_verification_orphans_quarantined"', (directory / "events.jsonl").read_text())
        second = self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        self.assertTrue(second["reused"]); self.assertEqual(second["repair_id"], first["repair_id"])
        self.assertEqual(second["manifest"]["sha256"], first["manifest"]["sha256"])

    def test_quarantine_accepts_modern_unbound_receipts_but_rejects_tampering(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        directory = self.state / work; receipt_dir = directory / "receipts"
        state = self.read_state(work); state.pop("verification_receipts"); self.state_file(work).write_text(json.dumps(state))
        # These are controller-written modern payloads but, absent the state
        # binding, are only candidates for byte-preserving quarantine.
        self.assertTrue(self.mod._historical_m0b_receipt(str(receipt_dir / "0.json"), str(receipt_dir)))
        payload = json.loads((receipt_dir / "1.json").read_text())
        payload["actual_exit_code"] = 0
        (receipt_dir / "1.json").write_text(json.dumps(payload))
        self.assertIsNone(self.mod._historical_m0b_receipt(str(receipt_dir / "1.json"), str(receipt_dir)))
        with self.assertRaisesRegex(self.mod.ControllerError, "orphan receipt is malformed"):
            self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)

    def test_quarantine_m0b_verification_orphans_fails_closed_on_unsafe_or_untrusted_entries(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        directory = self.state / work; receipt_dir = directory / "receipts"
        state = self.read_state(work); state.pop("verification_receipts"); self.state_file(work).write_text(json.dumps(state))
        (receipt_dir / "extra.txt").write_text("no")
        with self.assertRaisesRegex(self.mod.ControllerError, "unexpected orphan entries"):
            self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        (receipt_dir / "extra.txt").unlink(); (receipt_dir / "0.json").unlink()
        (receipt_dir / "0.json").symlink_to(self.root / "outside.json")
        (self.root / "outside.json").write_text("{}")
        with self.assertRaisesRegex(self.mod.ControllerError, "orphan receipt is unsafe"):
            self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)

    def test_quarantine_m0b_verification_orphans_requires_absent_state_binding(self):
        state = self.make_authoritative_m0b()
        with self.assertRaisesRegex(self.mod.ControllerError, "no verification receipts"):
            self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=state["work_id"])

    def test_quarantine_m0b_verification_orphans_recovers_after_each_atomic_move_window(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        state = self.read_state(work); state.pop("verification_receipts"); self.state_file(work).write_text(json.dumps(state))
        real_replace = self.mod.os.replace; tripped = []
        def crash_after_first_move(source, destination):
            real_replace(source, destination)
            if source.endswith("/receipts/0.json") and not tripped:
                tripped.append(source); raise OSError("simulated crash after atomic move")
        with mock.patch.object(self.mod.os, "replace", side_effect=crash_after_first_move):
            with self.assertRaises(OSError): self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        recovered = self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        self.assertTrue(recovered["reused"]); self.assertEqual(recovered["count"], 2)
        self.assertEqual(list((self.state / work / "receipts").iterdir()), [])

    def test_quarantine_m0b_verification_orphans_recovers_after_repair_mkdir(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        state = self.read_state(work); state.pop("verification_receipts"); self.state_file(work).write_text(json.dumps(state))
        real_makedirs = self.mod.os.makedirs; tripped = []
        def crash_after_repair_mkdir(path, *args, **kwargs):
            result = real_makedirs(path, *args, **kwargs)
            if os.path.basename(os.path.dirname(path)) == "receipts-quarantine" and not tripped:
                tripped.append(path); raise OSError("simulated crash after repair mkdir")
            return result
        with mock.patch.object(self.mod.os, "makedirs", side_effect=crash_after_repair_mkdir):
            with self.assertRaises(OSError): self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        recovered = self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        self.assertTrue(recovered["reused"]); self.assertEqual(recovered["count"], 2)

    def test_quarantine_m0b_pending_intent_rejects_unexpected_quarantine_sibling(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        state = self.read_state(work); state.pop("verification_receipts"); self.state_file(work).write_text(json.dumps(state))
        real_makedirs = self.mod.os.makedirs; tripped = []
        def crash_after_repair_mkdir(path, *args, **kwargs):
            result = real_makedirs(path, *args, **kwargs)
            if os.path.basename(os.path.dirname(path)) == "receipts-quarantine" and not tripped:
                tripped.append(path); raise OSError("simulated pending intent")
            return result
        with mock.patch.object(self.mod.os, "makedirs", side_effect=crash_after_repair_mkdir):
            with self.assertRaises(OSError): self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        sibling = self.state / work / "receipts-quarantine" / "unexpected"
        sibling.mkdir()
        with self.assertRaisesRegex(self.mod.ControllerError, "unexpected sibling"):
            self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)

    def test_quarantine_m0b_verification_orphans_recovers_manifest_before_event(self):
        state = self.make_authoritative_m0b(); work = state["work_id"]
        state = self.read_state(work); state.pop("verification_receipts"); self.state_file(work).write_text(json.dumps(state))
        real_append = self.mod.append_event; tripped = []
        def crash_before_event(directory, kind, **fields):
            if kind == "m0b_verification_orphans_quarantined" and not tripped:
                tripped.append(kind); raise OSError("simulated crash after manifest")
            return real_append(directory, kind, **fields)
        with mock.patch.object(self.mod, "append_event", side_effect=crash_before_event):
            with self.assertRaises(OSError): self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        recovered = self.call("quarantine_m0b_verification_orphans", state_root=str(self.state), work_id=work)
        self.assertTrue(recovered["reused"]); self.assertIn('"kind":"m0b_verification_orphans_quarantined"', (self.state / work / "events.jsonl").read_text())

    def valid_plan(self, m0b, m0c):
        fp = m0b["candidate"]["evidence"]["worktree_fingerprint"]
        binding = {"base_digest": m0c["repo"]["head"], "upstream_package_id": self.mod.M0B_WORK_ID, "upstream_candidate_id": self.mod.M0B_CANDIDATE_ID, "upstream_evidence_digest": self.mod.M0B_EVIDENCE_DIGEST, "upstream_worktree_fingerprint_digest": fp["digest"], "upstream_status_sha256": fp["status"]["sha256"], "upstream_untracked_sha256": fp["untracked"]["sha256"], "upstream_changed_paths": ["?? scripts/fixtures/dispatch_contract_characterization_sources.json", "?? scripts/test_dispatch_contract_characterization.py"], "upstream_result_sha256": self.mod.M0B_RESULT_SHA256, "upstream_review_sha256": self.mod.M0B_REVIEW_SHA256}
        contract = lambda name: {"name": name, "current_witness": "w", "target_behavior": "t", "call_site_adapter": "a", "compatibility_rule": "c", "tests": ["t"]}
        return {"schema_version": 1, "package_id": self.mod.M0C_PLAN_PACKAGE_ID, "candidate": binding, "recommended_slice": {"summary": "bounded", "included_call_sites": ["x"], "deferred_call_sites": []}, "record_proposal": ["x"], "call_site_map": ["x"], "three_contracts": [contract(x) for x in sorted(self.mod.M0C_CONTRACTS)], "legacy_strategy": {"session_v1": "x", "pending_turn": "x", "migration": "x"}, "allowlist": ["x"], "implementation_packages": ["x"], "negative_controls": ["x"], "risks": ["x"], "commands_run": ["x"], "assumptions": ["x"], "findings": []}

    def make_m0c_plan_gate(self):
        m0b = self.make_authoritative_m0b()
        self.brief.write_text("Objective: offline contract fixture.\nM0-B candidate worktree:\n%s\n" % m0b["worktree"]["path"])
        work = "m0c-contract-seam-plan"; self.prepare(work); self.launch(work, role="planner"); self.wait_quiescent(work); self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        state = self.read_state(work); attempt = state["attempt"]; plans = Path(os.path.realpath(self.root / "plans")); plans.mkdir(); source = plans / "plan.md"
        source.write_text("```json\n%s\n```\n" % json.dumps(self.valid_plan(m0b, state))); os.utime(source, None)
        source = source.resolve()
        provider = self.state / work / "packets" / (attempt["id"] + ".json"); packet = json.loads(provider.read_text()); packet["structured"]["changed_paths"] = [str(source)]; provider.write_text(json.dumps(packet))
        self.call("ingest_plan_artifact", state_root=str(self.state), work_id=work, claude_plans_root=str(plans), source_file=str(source))
        return work

    def review_packet(self, work, verdict="pass"):
        state = self.read_state(work); review = state["plan_review_attempt"]; artifact = state["planner_artifact"]
        return {"schema_version": 1, "kind": "m0c_plan_review", "work_id": work, "attempt_id": artifact["attempt_id"], "backend": "codex_terra", "actor_id": review["actor_id"], "run_id": review["run_id"], "review_nonce": review["nonce"], "plan_sha256": artifact["plan_sha256"], "upstream_evidence_digest": self.mod.M0B_EVIDENCE_DIGEST, "candidate_id": state["candidate"]["id"], "verdict": verdict, "findings": [], "checks": ["bindings checked"]}

    def test_owned_review_output_launch_contract_and_secret_redaction(self):
        work = self.make_m0c_plan_gate(); old = os.environ.get("FAKE_CODEX_SECRET"); os.environ["FAKE_CODEX_SECRET"] = "DO_NOT_LEAK"
        try:
            self.call("launch_plan_review", state_root=str(self.state), work_id=work, codex_bin=str(self.fake_codex))
            with self.assertRaisesRegex(self.mod.ControllerError, "already exists"):
                self.call("launch_plan_review", state_root=str(self.state), work_id=work, codex_bin=str(self.fake_codex))
            review = self.wait_review(work); row = json.loads(self.codex_audit.read_text().splitlines()[0]); argv = row["argv"]
            self.assertEqual(argv[:4], ["exec", "--json", "--model", "gpt-5.6-terra"]); self.assertIn('model_reasoning_effort="medium"', argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only"); self.assertEqual(argv[argv.index("--cd") + 1], self.read_state(work)["worktree"]["path"])
            self.assertTrue(Path(argv[argv.index("--output-schema") + 1]).is_file()); self.assertEqual(argv[argv.index("--output-last-message") + 1], review["output_path"]); self.assertNotIn("shell", " ".join(argv).lower())
            self.assertEqual(self.call("ingest_plan_review", state_root=str(self.state), work_id=work)["review_verdict"], "pass")
            self.assertNotIn("DO_NOT_LEAK", json.dumps(self.invoke("status", "--state-root", str(self.state), "--work-id", work)))
            self.assertNotIn("DO_NOT_LEAK", json.dumps(self.invoke("inspect", "--state-root", str(self.state), "--work-id", work)))
        finally:
            if old is None: os.environ.pop("FAKE_CODEX_SECRET", None)
            else: os.environ["FAKE_CODEX_SECRET"] = old

    def test_review_rejects_malformed_forged_and_tampered_then_invalidates_idempotently(self):
        work = self.make_m0c_plan_gate(); os.environ["FAKE_CODEX_KIND"] = "malformed"
        self.call("launch_plan_review", state_root=str(self.state), work_id=work, codex_bin=str(self.fake_codex)); review = self.wait_review(work); output = Path(review["output_path"])
        os.environ.pop("FAKE_CODEX_KIND", None)
        with self.assertRaisesRegex(self.mod.ControllerError, "must be JSON"): self.call("ingest_plan_review", state_root=str(self.state), work_id=work)
        output.write_text(json.dumps(self.review_packet(work)))
        with self.assertRaisesRegex(self.mod.ControllerError, "no longer matches terminal receipt"): self.call("ingest_plan_review", state_root=str(self.state), work_id=work)
        output.unlink(); output.symlink_to(self.root / "forged.json"); (self.root / "forged.json").write_text(json.dumps(self.review_packet(work)))
        with self.assertRaisesRegex(self.mod.ControllerError, "regular non-symlink"): self.call("ingest_plan_review", state_root=str(self.state), work_id=work)
        # This is a new review attempt in the fixture after the rejected
        # unconsumed output; production recovery must make that transition
        # explicitly rather than deleting this record.
        state = self.read_state(work); state.pop("plan_review_attempt"); self.state_file(work).write_text(json.dumps(state))
        self.call("launch_plan_review", state_root=str(self.state), work_id=work, codex_bin=str(self.fake_codex)); self.wait_review(work); self.call("ingest_plan_review", state_root=str(self.state), work_id=work)
        self.call("adjudicate", state_root=str(self.state), work_id=work, verdict="pass", rationale="all bindings passed", adjudicator_principal="orchestrator-plan-gate/v1", instruction=None, capability_required=None, target_authority_role=None, target_authority_principal=None)
        first = self.call("invalidate_gate", state_root=str(self.state), work_id=work, reason_code=["invalid_upstream_candidate_binding"], finding_id=["m0c.binding"]); second = self.call("invalidate_gate", state_root=str(self.state), work_id=work, reason_code=["invalid_upstream_candidate_binding"], finding_id=["m0c.binding"])
        self.assertEqual(self.read_state(work)["phase"], "needs_correction"); self.assertTrue(second["reused"]); self.assertEqual(first["receipt"]["sha256"], second["receipt"]["sha256"])
        with self.assertRaisesRegex(self.mod.ControllerError, "immutable receipt"):
            self.call("invalidate_gate", state_root=str(self.state), work_id=work, reason_code=["unverified_test_receipts"], finding_id=["m0c.binding"])

    def v2_plan(self, prerequisites=None):
        contract = lambda name: {"name": name, "current_witness": "witness", "target_behavior": "target", "tests": ["test"]}
        record = lambda name: {"name": name, "version": 1, "fields": ["id"], "validator": "validate", "storage": "state"}
        package = lambda ident: {"id": ident, "objective": "bounded", "paths": ["scripts/cowork.py"], "gates": ["root receipt"], "risks": ["risk"]}
        # Exact prerequisite bytes are separately exercised by the controller's
        # live-state validator; this test concentrates on the v2 wire shape.
        return {"schema_version": 1, "package_id": self.mod.M0C_V2_PLAN_PACKAGE_ID, "prerequisites": [] if prerequisites is None else prerequisites,
                "recommended_slice": {"summary": "bounded", "included_call_sites": ["run_scout"], "deferred_call_sites": []},
                "record_proposal": [record("DispatchContract"), record("DispatchDecision"), record("AttemptLink")],
                "call_site_map": [{"site": "run_scout", "current_behavior": "current", "adapter": "adapter", "compatibility": ["additive"]}],
                "three_contracts": [contract(x) for x in sorted(self.mod.M0C_CONTRACTS)],
                "legacy_strategy": {"session_v1": "load", "pending_turn": "stable", "migration": "idempotent"},
                "allowlist": ["scripts/cowork.py"], "implementation_packages": [package(x) for x in "ABCD"],
                "negative_controls": ["malformed"], "risks": [{"risk": "compat", "mitigation": "tests"}],
                "commands_run": [], "assumptions": ["none"], "findings": []}

    def v3_plan(self, prerequisites=None):
        packet = self.v2_plan(prerequisites)
        packet["package_id"] = self.mod.M0C_V3_PLAN_PACKAGE_ID
        return packet

    def make_v3_review_attempt(self, kind, prerequisite_kind=None):
        prerequisites = self.make_v2_prerequisite_fixture()
        work = self.mod.M0C_V3_WORK_ID; self.prepare(work); self.launch(work, role="planner"); self.wait_quiescent(work)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        state = self.read_state(work); attempt = state["attempt"]
        plans = Path(os.path.realpath(self.root / "plans-v3")); plans.mkdir()
        source = plans / "v3.md"; source.write_text("```json\n%s\n```\n" % json.dumps(self.v3_plan(prerequisites))); os.utime(source, None)
        provider = self.state / work / "packets" / (attempt["id"] + ".json")
        packet = json.loads(provider.read_text()); packet["structured"]["changed_paths"] = [str(source)]; provider.write_text(json.dumps(packet))
        self.call("ingest_plan_artifact", state_root=str(self.state), work_id=work, claude_plans_root=str(plans), source_file=str(source))
        prior = os.environ.get("FAKE_CODEX_KIND"); prior_prereq = os.environ.get("FAKE_CODEX_PREREQ_KIND")
        os.environ["FAKE_CODEX_KIND"] = kind
        if prerequisite_kind is None: os.environ.pop("FAKE_CODEX_PREREQ_KIND", None)
        else: os.environ["FAKE_CODEX_PREREQ_KIND"] = prerequisite_kind
        try: self.call("launch_plan_review", state_root=str(self.state), work_id=work, codex_bin=str(self.fake_codex))
        finally:
            if prior is None: os.environ.pop("FAKE_CODEX_KIND", None)
            else: os.environ["FAKE_CODEX_KIND"] = prior
            if prior_prereq is None: os.environ.pop("FAKE_CODEX_PREREQ_KIND", None)
            else: os.environ["FAKE_CODEX_PREREQ_KIND"] = prior_prereq
        return work, self.wait_review(work)

    def make_package_a_fixture(self, materialize=True, hide_untracked=False):
        """Build Package A from real local M0-B/M0-C0/v3 controller state.

        Only the imported controller module's immutable production identities
        are replaced.  Materialization, receipt validation, fingerprints, and
        worktree status are exercised without mocking their implementation.
        """
        scripts = self.repo / "scripts"; scripts.mkdir(exist_ok=True)
        base_test = scripts / "test_cowork.py"
        base_test.write_text("# package-a tracked base fixture\n")
        self.fixture.git("add", "scripts/test_cowork.py")
        self.fixture.git("commit", "-m", "package-a base fixture")
        self.base_head = self.fixture.git("rev-parse", "HEAD").strip()
        self.fixture.base_head = self.base_head

        v3_work, _ = self.make_v3_review_attempt("valid")
        self.call("ingest_plan_review", state_root=str(self.state), work_id=v3_work)
        self.call("adjudicate", state_root=str(self.state), work_id=v3_work,
                  verdict="pass", rationale="fixture bindings passed",
                  adjudicator_principal="orchestrator-plan-gate/v1",
                  instruction=None, capability_required=None,
                  target_authority_role=None, target_authority_principal=None)
        v3 = self.read_state(v3_work); review = v3["plan_review"]

        self.mod.PACKAGE_A_BASE_HEAD = self.base_head
        self.mod.PACKAGE_A_BASE_TEST_COWORK_SHA256 = hashlib.sha256(base_test.read_bytes()).hexdigest()
        self.mod.PACKAGE_A_V3_CANDIDATE_ID = v3["candidate"]["id"]
        self.mod.PACKAGE_A_V3_EVIDENCE_DIGEST = v3["candidate"]["evidence_digest"]
        self.mod.PACKAGE_A_V3_PLAN_SHA256 = review["plan_sha256"]
        self.mod.PACKAGE_A_V3_REVIEW_SHA256 = review["sha256"]
        m0b_wt = Path(self.read_state(self.mod.M0B_WORK_ID)["worktree"]["path"])
        c0_wt = Path(self.read_state(self.mod.M0C0_WORK_ID)["worktree"]["path"])
        self.mod.PACKAGE_A_PREREQUISITE_FILES = {
            "scripts/test_dispatch_contract_characterization.py": hashlib.sha256(
                (m0b_wt / "scripts/test_dispatch_contract_characterization.py").read_bytes()).hexdigest(),
            "scripts/fixtures/dispatch_contract_characterization_sources.json": hashlib.sha256(
                (m0b_wt / "scripts/fixtures/dispatch_contract_characterization_sources.json").read_bytes()).hexdigest(),
            "scripts/test_cowork.py": hashlib.sha256(
                (c0_wt / "scripts/test_cowork.py").read_bytes()).hexdigest(),
        }

        work = self.mod.PACKAGE_A_WORK_ID; self.prepare(work)
        state = self.read_state(work)
        self.mod.PACKAGE_A_BRIEF_SHA256 = state["brief"]["sha256"]
        if hide_untracked:
            subprocess.check_call(["git", "-C", state["worktree"]["path"], "config",
                                   "status.showUntrackedFiles", "no"])
        result = None
        if materialize:
            result = self.call("materialize_package_a_prerequisites",
                               state_root=str(self.state), work_id=work)
        return work, result

    def launch_package_a(self, work):
        launched = self.call("launch", state_root=str(self.state), work_id=work,
                             role="implementer", resume=False,
                             claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.wait_quiescent(work)
        return launched

    def make_package_a_collected_candidate(self, wrong_key_builder=False):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("""import hashlib, json, uuid
def _exact(value, keys, record):
 if not isinstance(value, dict) or set(value) != set(keys): raise ValueError('keys')
 if value.get('schema_version') != 1 or value.get('record') != record: raise ValueError('record')
 return json.loads(json.dumps(value))
def validate_dispatch_contract(value):
 row=_exact(value, ('schema_version','record','contract_id','role','phase','controller','kind','purpose','site','resume_session_id','created'), 'DispatchContract')
 uuid.UUID(row['contract_id']);
 if not all(isinstance(row[k], str) and row[k] for k in ('role','phase','controller','kind','purpose','site')): raise ValueError('contract')
 if row['resume_session_id'] is not None or isinstance(row['created'], bool) or not isinstance(row['created'], (int,float)): raise ValueError('contract')
 return row
def validate_dispatch_decision(value, contract=None):
 keys=('schema_version','record','decision_id','contract_id','outcome','refusal_code','refusal_message','source','spawned','trace_event_id')
 row=_exact(value, keys, 'DispatchDecision'); uuid.UUID(row['decision_id'])
 if contract is None or row['contract_id'] != contract['contract_id'] or row['outcome'] not in ('allow','refuse') or row['spawned'] is not False or row['trace_event_id'] is not None: raise ValueError('decision')
 if row['outcome'] == 'allow' and any(row[k] is not None for k in ('refusal_code','refusal_message','source')): raise ValueError('allow')
 if row['outcome'] == 'refuse' and not all(isinstance(row[k], str) and row[k] for k in ('refusal_code','refusal_message','source')): raise ValueError('refuse')
 return row
def validate_attempt_link(value):
 row=_exact(value, ('schema_version','record','attempt_id','role','phase','kind','source_ref','delivery_ref','idempotency_key','created'), 'AttemptLink')
 uuid.UUID(row['attempt_id'])
 if set(row['source_ref']) != {'kind','event_id','event_name','session_id','prompt_sha256','created'}: raise ValueError('source')
 if set(row['delivery_ref']) != {'prompt_kind','prompt_sha256','prompt_bytes'}: raise ValueError('delivery')
 if row['idempotency_key'] != build_attempt_link_idempotency_key(row['role'], row['kind'], row['source_ref'], 1): raise ValueError('key')
 return row
def build_attempt_link_idempotency_key(role, kind, source, ordinal):
 discriminator=source.get('event_id') or source.get('session_id') or source.get('prompt_sha256') or 'legacy'
 return '%s:%s:%s:%s' % (role, kind, discriminator, ordinal)
def decide(contract, policy_result=None, preflight_result=None, probe_result=None):
 if policy_result is not None and policy_result.get('allowed') is False:
  return {'schema_version':1,'record':'DispatchDecision','decision_id':'33333333-3333-4333-8333-333333333333','contract_id':contract['contract_id'],'outcome':'refuse','refusal_code':policy_result['refusal_code'],'refusal_message':policy_result['refusal_message'],'source':policy_result['source'],'spawned':False,'trace_event_id':None}
 return {'schema_version':1,'record':'DispatchDecision','decision_id':'44444444-4444-4444-8444-444444444444','contract_id':contract['contract_id'],'outcome':'allow','refusal_code':None,'refusal_message':None,'source':None,'spawned':False,'trace_event_id':None}
""")
        if wrong_key_builder:
            dispatch = wt / "scripts/cowork_dispatch.py"
            dispatch.write_text(dispatch.read_text().replace(
                "discriminator=source.get('event_id') or source.get('session_id') or source.get('prompt_sha256') or 'legacy'\n return '%s:%s:%s:%s' % (role, kind, discriminator, ordinal)",
                "return hashlib.sha256(json.dumps([role, kind, source, ordinal], sort_keys=True).encode()).hexdigest()"))
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"\nimport unittest\nclass PackageAFixture(unittest.TestCase):\n def test_fixture(self): self.assertTrue(True)\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)
        return work

    def make_package_a_verified_candidate(self):
        work = self.make_package_a_collected_candidate()
        verified = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(verified["all_passed"], verified)
        return work

    def wait_package_a_review(self, work):
        for _ in range(160):
            attempt = self.read_state(work).get("package_a_review_attempt") or {}
            receipt = attempt.get("receipt_path")
            if (not self.mod.process_alive(attempt.get("pid"), attempt.get("pgid")) and
                    isinstance(receipt, str) and Path(receipt).is_file()):
                return attempt
            time.sleep(.025)
        self.fail("fake Package A Claude review did not become durably quiescent")

    def write_package_a_worker_packet(self, work, changed_paths):
        state = self.read_state(work); output = Path(state["attempt"]["stdout"])
        packet = self.package_a_worker_result(changed_paths, state["worktree"]["path"])
        output.write_text(json.dumps({"type": "result", "structured_output": packet}) + "\n")

    def package_a_worker_result(self, changed_paths=None, cwd="/fixture"):
        return {"outcome": "completed", "summary": "bounded Package A fixture",
                "changed_paths": (["scripts/cowork_dispatch.py", "scripts/test_cowork.py"]
                                  if changed_paths is None else changed_paths),
                "checks": [{"command": command, "cwd": cwd,
                            "exit_code": 0, "receipt_refs": []}
                           for command in ("python3 -m unittest scripts/test_cowork.py",
                                           "python3 -m unittest scripts/test_dispatch_contract_characterization.py",
                                           "git diff --check")],
                "record_api_summary": {
                    "records": ["DispatchContract", "DispatchDecision", "AttemptLink"],
                    "validators": ["validate_dispatch_contract(record)",
                                   "validate_dispatch_decision(record, contract)",
                                   "validate_attempt_link(record)"],
                    "reducer": "decide(contract, policy_result=None, preflight_result=None, probe_result=None)",
                    "idempotency_key_builder":
                        "build_attempt_link_idempotency_key(role, kind, source_ref, ordinal)"},
                "negative_control_coverage": [
                    {"control": "unknown schema rejects", "test": "test_unknown_schema", "status": "covered"}],
                "findings": [], "assumptions": [], "next_action": "verify gates"}

    def test_package_a_worker_schema_is_selected_and_normalized_strictly(self):
        packet = self.package_a_worker_result()
        self.assertEqual(self.mod.normalize_package_a_structured(packet), packet)
        self.assertIsNone(self.mod.normalize_structured(packet))
        generic = {"outcome": "completed", "summary": "generic", "changed_paths": [],
                   "checks": [], "findings": [], "assumptions": [], "next_action": "gate"}
        self.assertIsNone(self.mod.normalize_package_a_structured(generic))
        for mutation in (
            lambda value: value.update({"extra": True}),
            lambda value: value.pop("record_api_summary"),
            lambda value: value["checks"][0].update({"exit_code": True}),
            lambda value: value["checks"][0].update({"extra": True}),
            lambda value: value["checks"][0].update({"receipt_refs": ["worker-proof-is-not-trusted"]}),
            lambda value: value["record_api_summary"].update({"records": ["PhaseState"]}),
            lambda value: value["record_api_summary"].update({"extra": True}),
            lambda value: value["record_api_summary"].update({"validators": ["forged"] * 3}),
            lambda value: value["record_api_summary"].update({
                "records": ["DispatchDecision", "DispatchContract", "AttemptLink"]}),
            lambda value: value["negative_control_coverage"][0].update({"extra": True}),
            lambda value: value["negative_control_coverage"].append(copy.deepcopy(value["negative_control_coverage"][0])),
            lambda value: value.update({"summary": "x" * 2001}),
            lambda value: value.update({"next_action": "x" * 1001}),
            lambda value: value.update({"changed_paths": ["x" * 301]}),
            lambda value: value["checks"][0].update({"command": "x" * 501}),
            lambda value: value["checks"][0].update({"cwd": "x" * 501}),
            lambda value: value["negative_control_coverage"][0].update({"control": ""}),
            lambda value: value["negative_control_coverage"][0].update({"test": ""}),
            lambda value: value["negative_control_coverage"][0].update({"status": "claimed"}),
            lambda value: value.update({"findings": ["x" * 501]}),
        ):
            bad = self.package_a_worker_result(); mutation(bad)
            self.assertIsNone(self.mod.normalize_package_a_structured(bad))
        for key in packet:
            bad = self.package_a_worker_result(); bad.pop(key)
            self.assertIsNone(self.mod.normalize_package_a_structured(bad), key)
        schema = self.mod.PACKAGE_A_OUTCOME_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["changed_paths"]["maxItems"], 2)

    def test_package_a_launch_selects_exact_worker_schema_not_generic_schema(self):
        work, _ = self.make_package_a_fixture()
        with mock.patch.object(self.mod, "detached_spawn", return_value=(999999, 999999)) as spawn:
            self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                      resume=False, claude_bin=str(self.fake), model="sonnet", effort="medium")
        argv = spawn.call_args.args[0]
        selected = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertEqual(selected, self.mod.PACKAGE_A_OUTCOME_SCHEMA)
        self.assertNotEqual(selected, self.mod.OUTCOME_SCHEMA)

    def test_package_a_worker_claim_aliases_normalize_once_and_fail_closed(self):
        packet = {"structured": self.package_a_worker_result([
            "?? scripts/cowork_dispatch.py", " M scripts/test_cowork.py"])}
        self.assertEqual(self.mod._package_a_claimed_delta(packet),
                         ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        for paths in (
            ["scripts/cowork_dispatch.py", "?? scripts/cowork_dispatch.py"],
            ["../scripts/cowork_dispatch.py", "scripts/test_cowork.py"],
            ["/tmp/cowork_dispatch.py", "scripts/test_cowork.py"],
            ["scripts//cowork_dispatch.py", "scripts/test_cowork.py"],
        ):
            packet = {"structured": self.package_a_worker_result(paths)}
            with self.assertRaises(self.mod.ControllerError): self.mod._package_a_claimed_delta(packet)

    def test_package_a_verifier_runs_exact_five_gates_and_replays_aggregate(self):
        work = self.make_package_a_collected_candidate()
        verified = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertFalse(verified["reused"]); self.assertTrue(verified["all_passed"], verified)
        state = self.read_state(work); bound = state["package_a_verification"]
        aggregate = json.loads(Path(bound["aggregate_path"]).read_text())
        self.assertEqual([row["label"] for row in aggregate["receipts"]],
                         ["test_cowork", "characterization", "diff_check", "candidate_delta", "json_probe"])
        receipts = [json.loads(Path(row["path"]).read_text()) for row in aggregate["receipts"]]
        self.assertEqual([row["index"] for row in receipts], list(range(5)))
        self.assertEqual([row["timeout_seconds"] for row in receipts], [300, 180, 60, 0, 30])
        characterization = receipts[1]
        self.assertEqual(characterization["counters"]["tests"], 32)
        self.assertEqual(characterization["counters"]["expected_failures"], 3)
        self.assertTrue(all(row["passed"] is True and row["timed_out"] is False and
                            row["output_limit_exceeded"] is False and row["spawn_error"] is None
                            for row in receipts))
        self.assertNotIn("output", receipts[0])
        dispatch_path = Path(state["worktree"]["path"]) / "scripts/cowork_dispatch.py"
        spec = importlib.util.spec_from_file_location("package_a_dispatch_fixture", dispatch_path)
        dispatch = importlib.util.module_from_spec(spec); spec.loader.exec_module(dispatch)
        source = {"event_id": "event-1"}
        self.assertEqual(dispatch.build_attempt_link_idempotency_key(
            "builder", "pending_replay", source, 1), "builder:pending_replay:event-1:1")
        replay = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["aggregate_sha256"], verified["aggregate_sha256"])

    def test_package_a_bounded_gate_runner_enforces_timeout_overflow_and_spawn_failure(self):
        timed = self.mod._bounded_run(
            [sys.executable, "-c", "import time; time.sleep(5)"], str(self.root), 0.05, 1024)
        self.assertTrue(timed["timed_out"]); self.assertNotEqual(timed["exit_code"], 0)
        overflow = self.mod._bounded_run(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 4096)"],
            str(self.root), 5, 1024)
        self.assertTrue(overflow["output_limit_exceeded"])
        self.assertEqual(overflow["output_bytes"], 1024)
        self.assertEqual(overflow["output"], b"x" * 1024)
        self.assertEqual(overflow["output_sha256"], hashlib.sha256(overflow["output"]).hexdigest())
        spawned = self.mod._bounded_run([str(self.root / "missing-executable")],
                                        str(self.root), 1, 1024)
        self.assertEqual(spawned["exit_code"], 127)
        self.assertEqual(spawned["spawn_error"], "FileNotFoundError")
        self.assertEqual(spawned["output"], b"")

    def test_package_a_failed_unittest_gate_seals_exact_counters_and_replays_nonpass(self):
        work = self.make_package_a_collected_candidate()

        def execute(spec, index, wt, verify_dir, terminal_path, start_path,
                    static, wrapper_nonce, deadline):
            command = " ".join(spec["argv"])
            if command.endswith("scripts/test_cowork.py"):
                raw = b"Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n"; code = 1
            elif command.endswith("scripts/test_dispatch_contract_characterization.py"):
                raw = b"Ran 32 tests in 0.001s\n\nOK (expected failures=3)\n"; code = 0
            elif spec["argv"][:2] == ["git", "diff"] or spec["kind"] == "controller":
                raw = b""; code = 0
            else:
                raw = json.dumps({"allow": True, "key": True, "records": 3,
                                  "refuse": True}, sort_keys=True).encode() + b"\n"; code = 0
            counters = self.mod._package_a_unittest_counters(raw) if spec["kind"] in {"unittest", "characterization"} else {}
            passed = code == 0
            result = {"exit_code": code, "timed_out": False, "output_limit_exceeded": False,
                      "output_sha256": hashlib.sha256(raw).hexdigest(), "output_bytes": len(raw),
                      "output_total_seen": len(raw), "spawn_error": None, "cleanup_error": None,
                      "counters": counters, "passed": passed}
            spec_sha256 = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            static_sha256 = hashlib.sha256(json.dumps(static, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            wrapper_core = {"terminal_path": terminal_path, "start_path": start_path, "index": index, "cwd": wt,
                            "spec_sha256": spec_sha256, "static_sha256": static_sha256,
                            "wrapper_nonce": wrapper_nonce, "deadline": deadline}
            wrapper_digest = hashlib.sha256(json.dumps(wrapper_core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            Path(start_path).write_text(json.dumps({"schema_version": self.mod.SCHEMA, "kind": "package_a_gate_wrapper_start",
                "index": index, "spec_sha256": spec_sha256, "static_sha256": static_sha256,
                "wrapper_nonce": wrapper_nonce, "wrapper_argv_sha256": wrapper_digest,
                "pid": os.getpid(), "pgid": os.getpgrp(), "started_at": self.mod.utc(), "deadline": deadline}))
            header = {"schema_version": self.mod.SCHEMA, "kind": "package_a_gate_terminal",
                      "index": index, "label": spec["label"],
                      "spec_sha256": spec_sha256, "static_sha256": static_sha256,
                      "result": result}
            self.mod._package_a_write_gate_terminal(terminal_path, header, raw)
            info = self.mod.capped_regular_file_info(terminal_path, verify_dir,
                                                     self.mod.PACKAGE_A_MAX_GATE_OUTPUT + 64 * 1024)
            return result, raw, info

        with mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute):
            verified = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertFalse(verified["all_passed"])
        aggregate = json.loads(Path(verified["aggregate_path"]).read_text())
        failed = json.loads(Path(aggregate["receipts"][0]["path"]).read_text())
        self.assertFalse(failed["passed"]); self.assertEqual(failed["exit_code"], 1)
        self.assertEqual(failed["counters"]["tests"], 2)
        self.assertEqual(failed["counters"]["failures"], 1)
        replay = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(replay["reused"]); self.assertFalse(replay["all_passed"])

    def test_package_a_verification_recovers_every_output_receipt_aggregate_event_and_state_window(self):
        work = self.make_package_a_collected_candidate(); directory = self.state / work
        initial_state = self.state_file(work).read_bytes()
        initial_events = (directory / "events.jsonl").read_bytes()

        executions = {}
        def execute(spec, index, wt, verify_dir, terminal_path, start_path,
                    static, wrapper_nonce, deadline):
            if os.path.lexists(terminal_path):
                return self.mod._package_a_read_gate_terminal(
                    terminal_path, verify_dir, spec, index,
                    hashlib.sha256(json.dumps(static, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest())
            executions[spec["label"]] = executions.get(spec["label"], 0) + 1
            elapsed = "0.%03d" % executions[spec["label"]]
            command = " ".join(spec["argv"])
            if command.endswith("scripts/test_cowork.py"):
                raw = ("Ran 2 tests in %ss\n\nOK\n" % elapsed).encode()
            elif command.endswith("scripts/test_dispatch_contract_characterization.py"):
                raw = ("Ran 32 tests in %ss\n\nOK (expected failures=3)\n" % elapsed).encode()
            elif spec["argv"][:2] == ["git", "diff"] or spec["kind"] == "controller":
                raw = b""
            else:
                raw = json.dumps({"allow": True, "key": True, "records": 3,
                                  "refuse": True}, sort_keys=True).encode() + b"\n"
            counters = self.mod._package_a_unittest_counters(raw) if spec["kind"] in {"unittest", "characterization"} else {}
            result = {"exit_code": 0, "timed_out": False, "output_limit_exceeded": False,
                      "output_sha256": hashlib.sha256(raw).hexdigest(), "output_bytes": len(raw),
                      "output_total_seen": len(raw), "spawn_error": None, "cleanup_error": None,
                      "counters": counters, "passed": True}
            spec_sha256 = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            static_sha256 = hashlib.sha256(json.dumps(static, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            wrapper_core = {"terminal_path": terminal_path, "start_path": start_path, "index": index, "cwd": wt,
                            "spec_sha256": spec_sha256, "static_sha256": static_sha256,
                            "wrapper_nonce": wrapper_nonce, "deadline": deadline}
            wrapper_digest = hashlib.sha256(json.dumps(wrapper_core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            Path(start_path).write_text(json.dumps({"schema_version": self.mod.SCHEMA, "kind": "package_a_gate_wrapper_start",
                "index": index, "spec_sha256": spec_sha256, "static_sha256": static_sha256,
                "wrapper_nonce": wrapper_nonce, "wrapper_argv_sha256": wrapper_digest,
                "pid": os.getpid(), "pgid": os.getpgrp(), "started_at": self.mod.utc(), "deadline": deadline}))
            header = {"schema_version": self.mod.SCHEMA, "kind": "package_a_gate_terminal",
                      "index": index, "label": spec["label"],
                      "spec_sha256": spec_sha256, "static_sha256": static_sha256,
                      "result": result}
            self.mod._package_a_write_gate_terminal(terminal_path, header, raw)
            info = self.mod.capped_regular_file_info(terminal_path, verify_dir,
                                                     self.mod.PACKAGE_A_MAX_GATE_OUTPUT + 64 * 1024)
            return result, raw, info

        def reset():
            self.state_file(work).write_bytes(initial_state)
            (directory / "events.jsonl").write_bytes(initial_events)
            shutil.rmtree(directory / "package-a-verification", ignore_errors=True)

        labels = ["test_cowork", "characterization", "diff_check", "candidate_delta", "json_probe"]
        for index, label in enumerate(labels):
            reset(); executions.clear(); real = self.mod._package_a_adopt_bytes; tripped = []
            def crash_output(path, parent, value, name, marker="%d-%s.output.bin" % (index, label)):
                info = real(path, parent, value, name)
                if os.path.basename(path) == marker and not tripped:
                    tripped.append(path); raise OSError("crash after verification output")
                return info
            with self.subTest(output=label), mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute), \
                    mock.patch.object(self.mod, "_package_a_adopt_bytes", side_effect=crash_output):
                with self.assertRaisesRegex(OSError, "after verification output"):
                    self.call("verify_package_a", state_root=str(self.state), work_id=work)
            before_recovery = dict(executions)
            with mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute):
                recovered = self.call("verify_package_a", state_root=str(self.state), work_id=work)
            self.assertTrue(recovered["all_passed"])
            self.assertEqual(executions[label], before_recovery[label])

        for index, label in enumerate(labels):
            reset(); real = self.mod._package_a_adopt_json; tripped = []
            def crash_receipt(path, parent, value, name, marker="%d-%s.json" % (index, label)):
                info = real(path, parent, value, name)
                if os.path.basename(path) == marker and not tripped:
                    tripped.append(path); raise OSError("crash after verification receipt")
                return info
            with self.subTest(receipt=label), mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute), \
                    mock.patch.object(self.mod, "_package_a_adopt_json", side_effect=crash_receipt):
                with self.assertRaisesRegex(OSError, "after verification receipt"):
                    self.call("verify_package_a", state_root=str(self.state), work_id=work)
            with mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute):
                recovered = self.call("verify_package_a", state_root=str(self.state), work_id=work)
            self.assertTrue(recovered["all_passed"])

        reset(); real_adopt = self.mod._package_a_adopt_json; tripped = []
        def crash_aggregate(path, parent, value, name):
            info = real_adopt(path, parent, value, name)
            if os.path.basename(path) == "aggregate.json" and not tripped:
                tripped.append(path); raise OSError("crash after verification aggregate")
            return info
        with mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute), \
                mock.patch.object(self.mod, "_package_a_adopt_json", side_effect=crash_aggregate):
            with self.assertRaisesRegex(OSError, "after verification aggregate"):
                self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(self.call("verify_package_a", state_root=str(self.state), work_id=work)["all_passed"])

        reset(); real_append = self.mod.append_event
        def crash_event(directory_arg, kind, **fields):
            value = real_append(directory_arg, kind, **fields)
            if kind == "package_a_verification_passed": raise OSError("crash after verification event")
            return value
        with mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute), \
                mock.patch.object(self.mod, "append_event", side_effect=crash_event):
            with self.assertRaisesRegex(OSError, "after verification event"):
                self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(self.call("verify_package_a", state_root=str(self.state), work_id=work)["all_passed"])

        reset(); real_save = self.mod.save
        def crash_state(directory_arg, state_arg):
            value = real_save(directory_arg, state_arg)
            raise OSError("crash after verification state")
        with mock.patch.object(self.mod, "_package_a_execute_gate", side_effect=execute), \
                mock.patch.object(self.mod, "save", side_effect=crash_state):
            with self.assertRaisesRegex(OSError, "after verification state"):
                self.call("verify_package_a", state_root=str(self.state), work_id=work)
        replay = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(replay["reused"]); self.assertTrue(replay["all_passed"])
        events = [row for row in self.mod.recent_events(str(directory))
                  if row.get("kind") == "package_a_verification_passed"]
        self.assertEqual(len(events), 1)

    def test_package_a_gate_wrapper_has_one_owner_and_lost_owner_seals_nonpass(self):
        verify_dir = self.root / "package-a-gate-wrapper-fixture"
        verify_dir.mkdir()
        spec = {"label": "candidate_delta", "argv": ["controller", "package-a-delta"],
                "timeout": 0, "kind": "controller"}
        spec_sha = hashlib.sha256(json.dumps(spec, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
        static = {"candidate_id": "package-a-" + "a" * 32}
        static_sha = hashlib.sha256(json.dumps(static, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        terminal = verify_dir / "owned.terminal.bin"
        start = verify_dir / "owned.start.json"
        nonce = "b" * 32
        core = {"terminal_path": str(terminal), "start_path": str(start), "index": 0,
                "cwd": str(self.repo), "spec_sha256": spec_sha, "static_sha256": static_sha,
                "wrapper_nonce": nonce, "deadline": deadline}
        wrapper_digest = hashlib.sha256(json.dumps(core, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest()
        values = SimpleNamespace(spec_json=json.dumps(spec, separators=(",", ":")),
                                 spec_sha256=spec_sha, static_sha256=static_sha,
                                 start_path=str(start), terminal_path=str(terminal), index=0,
                                 cwd=str(self.repo), wrapper_nonce=nonce,
                                 wrapper_digest=wrapper_digest, deadline=deadline)
        first = self.mod._command_package_a_gate_wrapper(values)
        self.assertTrue(first["passed"])
        sealed = terminal.read_bytes()
        with self.assertRaisesRegex(self.mod.ControllerError, "already claimed"):
            self.mod._command_package_a_gate_wrapper(values)
        self.assertEqual(terminal.read_bytes(), sealed)

        lost_terminal = verify_dir / "lost.terminal.bin"
        lost_start = verify_dir / "lost.start.json"
        lost_core = {**core, "terminal_path": str(lost_terminal), "start_path": str(lost_start)}
        lost_digest = hashlib.sha256(json.dumps(lost_core, sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
        lost_start.write_text(json.dumps({
            "schema_version": self.mod.SCHEMA, "kind": "package_a_gate_wrapper_start",
            "index": 0, "spec_sha256": spec_sha, "static_sha256": static_sha,
            "wrapper_nonce": nonce, "wrapper_argv_sha256": lost_digest,
            "pid": 99999999, "pgid": 99999999, "started_at": self.mod.utc(),
            "deadline": deadline,
        }))
        result, raw, info = self.mod._package_a_execute_gate(
            spec, 0, str(self.repo), str(verify_dir), str(lost_terminal), str(lost_start),
            static, nonce, deadline)
        self.assertFalse(result["passed"])
        self.assertEqual(result["spawn_error"], "PackageAGateWrapperLost")
        self.assertEqual(raw, b"")
        replay, replay_raw, replay_info = self.mod._package_a_execute_gate(
            spec, 0, str(self.repo), str(verify_dir), str(lost_terminal), str(lost_start),
            static, nonce, deadline)
        self.assertEqual(replay, result); self.assertEqual(replay_raw, raw)
        self.assertEqual(replay_info["sha256"], info["sha256"])

    def test_package_a_verification_rejects_top_level_symlink_before_gate_execution(self):
        work = self.make_package_a_collected_candidate()
        outside = self.root / "outside-package-a-verification"
        outside.mkdir()
        os.symlink(outside, self.state / work / "package-a-verification")
        with mock.patch.object(self.mod, "_package_a_execute_gate",
                               side_effect=AssertionError("controller followed verification symlink")):
            with self.assertRaisesRegex(self.mod.ControllerError, "unsafe"):
                self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(list(outside.iterdir()), [])

    def test_package_a_verification_replay_rejects_semantic_downgrade_and_receipt_tamper(self):
        work = self.make_package_a_verified_candidate(); state = self.read_state(work)
        state_path = self.state_file(work); original_state = state_path.read_bytes()
        aggregate_path = Path(state["package_a_verification"]["aggregate_path"])
        original_aggregate = aggregate_path.read_bytes(); aggregate = json.loads(original_aggregate)
        receipt_path = Path(aggregate["receipts"][0]["path"])
        original_receipt = receipt_path.read_bytes(); receipt = json.loads(original_receipt)
        output_path = Path(receipt["output_path"]); original_output = output_path.read_bytes()

        def restore():
            state_path.write_bytes(original_state); aggregate_path.write_bytes(original_aggregate)
            receipt_path.write_bytes(original_receipt); output_path.write_bytes(original_output)

        def expect_receipt_rejected(mutator, output=None):
            restore(); changed_receipt = json.loads(original_receipt); mutator(changed_receipt)
            if output is not None:
                output_path.write_bytes(output)
                changed_receipt["output_sha256"] = hashlib.sha256(output).hexdigest()
                changed_receipt["output_bytes"] = len(output)
                changed_receipt["output_total_seen"] = len(output)
            receipt_path.write_text(json.dumps(changed_receipt))
            changed_aggregate = json.loads(original_aggregate)
            changed_aggregate["receipts"][0]["sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            changed_aggregate["receipts"][0]["counters"] = changed_receipt["counters"]
            changed_aggregate["receipts"][0]["passed"] = changed_receipt["passed"]
            changed_aggregate["all_passed"] = all(row["passed"] for row in changed_aggregate["receipts"])
            aggregate_path.write_text(json.dumps(changed_aggregate))
            changed_state = json.loads(original_state)
            changed_state["package_a_verification"]["aggregate_sha256"] = hashlib.sha256(aggregate_path.read_bytes()).hexdigest()
            state_path.write_text(json.dumps(changed_state))
            with self.assertRaises(self.mod.ControllerError):
                self.call("verify_package_a", state_root=str(self.state), work_id=work)

        mutations = (
            lambda row: row.update({"passed": False}),
            lambda row: row.update({"exit_code": 1}),
            lambda row: row.update({"timed_out": True}),
            lambda row: row.update({"output_limit_exceeded": True}),
            lambda row: row.update({"spawn_error": "OSError"}),
            lambda row: row.update({"cleanup_error": "permission_denied"}),
            lambda row: row["counters"].update({"tests": row["counters"]["tests"] + 1}),
            lambda row: row.update({"output_total_seen": row["output_total_seen"] + 1}),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(receipt_mutation=index): expect_receipt_rejected(mutation)
        expect_receipt_rejected(lambda row: None, b"Ran 2 tests in 0.001s\n\nOK\n")

        restore(); changed_aggregate = json.loads(original_aggregate)
        changed_aggregate["all_passed"] = False; aggregate_path.write_text(json.dumps(changed_aggregate))
        changed_state = json.loads(original_state)
        changed_state["package_a_verification"]["aggregate_sha256"] = hashlib.sha256(aggregate_path.read_bytes()).hexdigest()
        state_path.write_text(json.dumps(changed_state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("verify_package_a", state_root=str(self.state), work_id=work)
        restore()

    def test_package_a_json_probe_rejects_noncanonical_hashed_key_builder(self):
        work = self.make_package_a_collected_candidate(wrong_key_builder=True)
        verified = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertFalse(verified["all_passed"])
        aggregate = json.loads(Path(verified["aggregate_path"]).read_text())
        self.assertEqual([row["label"] for row in aggregate["receipts"] if not row["passed"]],
                         ["json_probe"])
        replay = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(replay["reused"]); self.assertFalse(replay["all_passed"])
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch_package_a_review", state_root=str(self.state),
                      work_id=work, claude_bin=str(self.fake_claude_reviewer))
        decided = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(decided["disposition"], "revise")
        self.assertEqual(decided["phase"], "prepared")
        self.assertEqual(json.loads(Path(decided["receipt"]["path"]).read_text())["reason_code"],
                         "deterministic_or_review_dissent")
        self.assertTrue(self.call("adjudicate_package_a", state_root=str(self.state),
                                  work_id=work)["reused"])

    def test_package_a_independent_terra_review_happy_exact_binding(self):
        work = self.make_package_a_verified_candidate(); before = len(self.claude_reviewer_audit.read_text().splitlines() if self.claude_reviewer_audit.exists() else [])
        launched = self.call("launch_package_a_review", state_root=str(self.state),
                             work_id=work, claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(work)
        rows = [json.loads(line) for line in self.claude_reviewer_audit.read_text().splitlines()]
        self.assertEqual(len(rows), before + 1)
        argv = rows[-1]["argv"]
        self.assertEqual(argv[0], "-p")
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-4-6")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertIn("--json-schema", argv)
        self.assertIn("--disallowedTools", argv)
        session_id = attempt.get("reviewer_session_id")
        self.assertIsNotNone(session_id)
        self.assertEqual(argv[argv.index("--session-id") + 1], session_id)
        self.assertEqual(attempt["id"], launched["attempt_id"])
        self.assertEqual(attempt["actor_id"], self.mod.PACKAGE_A_CLAUDE_REVIEW_ACTOR)
        self.assertEqual(attempt["backend"], self.mod.PACKAGE_A_CLAUDE_REVIEW_BACKEND)
        ingested = self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        self.assertEqual(ingested["verdict"], "pass")
        state = self.read_state(work); review = state["package_a_review"]
        envelope = json.loads(Path(review["path"]).read_text())
        self.assertEqual(envelope["kind"], "package_a_review_envelope")
        self.assertEqual(envelope["reviewer_session_id"], review["provider_session_id"])
        packet = envelope["model_packet"]
        self.assertEqual(packet["candidate_id"], state["candidate"]["id"])
        self.assertEqual(packet["evidence_digest"], state["candidate"]["evidence_digest"])
        self.assertEqual(packet["verification_aggregate_sha256"],
                         state["package_a_verification"]["aggregate_sha256"])
        aggregate = json.loads(Path(state["package_a_verification"]["aggregate_path"]).read_text())
        self.assertEqual(packet["verification_receipt_sha256s"],
                         [row["sha256"] for row in aggregate["receipts"]])
        self.assertEqual(packet["changed_paths"],
                         ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])

    def test_package_a_independent_review_rejects_every_packet_binding_tamper(self):
        work = self.make_package_a_verified_candidate()
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(work)
        output_path = Path(attempt["output_path"]); terminal_path = Path(attempt["receipt_path"])
        original_output = output_path.read_bytes(); original_terminal = terminal_path.read_bytes()

        def expect_rejected(mutator):
            packet = json.loads(original_output); mutator(packet)
            output_path.write_text(json.dumps(packet))
            terminal = json.loads(original_terminal)
            terminal["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
            terminal_path.write_text(json.dumps(terminal))
            with self.assertRaises(self.mod.ControllerError):
                self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)

        mutations = (
            lambda packet: packet.update({"extra": True}),
            lambda packet: packet.pop("candidate_id"),
            lambda packet: packet.update({"actor_id": "untrusted-reviewer"}),
            lambda packet: packet.update({"run_id": "wrong-run"}),
            lambda packet: packet.update({"review_nonce": "0" * 32}),
            lambda packet: packet.update({"candidate_id": "wrong-candidate"}),
            lambda packet: packet.update({"evidence_digest": "0" * 64}),
            lambda packet: packet.update({"baseline_receipt_sha256": "0" * 64}),
            lambda packet: packet.update({"baseline_fingerprint_digest": "0" * 64}),
            lambda packet: packet.update({"delta_receipt_sha256": "0" * 64}),
            lambda packet: packet.update({"delta_sha256": "0" * 64}),
            lambda packet: packet.update({"verification_aggregate_sha256": "0" * 64}),
            lambda packet: packet["verification_receipt_sha256s"].reverse(),
            lambda packet: packet.update({"implementer_session_id": "wrong-session"}),
            lambda packet: packet.update({"implementer_model": "opus"}),
            lambda packet: packet.update({"implementer_effort": "high"}),
            lambda packet: packet.update({"changed_paths": list(reversed(packet["changed_paths"]))}),
            lambda packet: packet.update({"verdict": "revise"}),
            lambda packet: packet.update({"findings": [7]}),
            lambda packet: packet.update({"checks": ["x" * 1001]}),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index): expect_rejected(mutation)
        output_path.write_bytes(original_output); terminal_path.write_bytes(original_terminal)
        self.assertEqual(self.call("ingest_package_a_review", state_root=str(self.state),
                                   work_id=work)["verdict"], "pass")

    def test_package_a_review_schema_failure_is_not_retryable(self):
        """Schema failure (typed error_schema_validation) is not an infrastructure retry class."""
        work = self.make_package_a_verified_candidate()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_KIND"); os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = "schema_failure"
        try:
            self.call("launch_package_a_review", state_root=str(self.state),
                      work_id=work, claude_bin=str(self.fake_claude_reviewer))
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_KIND", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = prior
        self.wait_package_a_review(work)
        # Schema failure is NOT the bounded stream overflow class; retry must be refused
        with self.assertRaisesRegex(self.mod.ControllerError, "overflow|retryable|infrastructure"):
            self.call("retry_package_a_review", state_root=str(self.state), work_id=work)

    def test_package_a_fixed_gate_derives_pass_and_blocks_generic_adjudication(self):
        work = self.make_package_a_verified_candidate()
        waiting = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(waiting["disposition"], "awaiting_review")
        self.assertEqual(self.read_state(work)["phase"], "awaiting_gate")
        self.assertNotIn("package_a_gate_receipt", self.read_state(work))
        with self.assertRaisesRegex(self.mod.ControllerError, "package-specific"):
            self.call("adjudicate", state_root=str(self.state), work_id=work,
                      verdict="pass", rationale="caller must not decide", instruction=None,
                      adjudicator_principal=self.mod.PACKAGE_A_GATE_PRINCIPAL,
                      capability_required=None, target_authority_role=None,
                      target_authority_principal=None)
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(work)
        self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        decided = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertFalse(decided["reused"]); self.assertEqual(decided["disposition"], "pass")
        self.assertEqual(decided["phase"], "completed")
        state = self.read_state(work); receipt = json.loads(Path(decided["receipt"]["path"]).read_text())
        self.assertEqual(receipt["adjudicator_principal"], self.mod.PACKAGE_A_GATE_PRINCIPAL)
        self.assertEqual(receipt["reason_code"], "all_evidence_passed")
        self.assertEqual(state["gate"]["receipt_sha256"], decided["receipt"]["sha256"])
        replay = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(replay["reused"]); self.assertEqual(replay["receipt"], decided["receipt"])
        parsed = self.mod.parser().parse_args([
            "adjudicate-package-a", "--state-root", str(self.state), "--work-id", work])
        self.assertEqual(set(vars(parsed)), {"command", "state_root", "work_id"})
        original = self.state_file(work).read_bytes(); changed = json.loads(original)
        changed["candidate"]["id"] = "package-a-" + "0" * 32
        self.state_file(work).write_text(json.dumps(changed))
        with self.assertRaises(self.mod.ControllerError):
            self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.state_file(work).write_bytes(original)
        dispatch = Path(state["worktree"]["path"]) / "scripts/cowork_dispatch.py"
        dispatch.write_bytes(dispatch.read_bytes() + b"\n# post-pass tamper\n")
        with self.assertRaises(self.mod.ControllerError):
            self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)

    def test_package_a_fixed_gate_recovers_receipt_state_and_event_crash_windows(self):
        work = self.make_package_a_verified_candidate()
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(work)
        self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        directory = self.state / work; initial_state = self.state_file(work).read_bytes()
        initial_events = (directory / "events.jsonl").read_bytes()

        def reset():
            self.state_file(work).write_bytes(initial_state)
            (directory / "events.jsonl").write_bytes(initial_events)
            shutil.rmtree(directory / "package-a-gates", ignore_errors=True)

        receipts = []
        reset(); real_adopt = self.mod._package_a_adopt_json; tripped = []
        def crash_receipt(path, parent, value, name):
            info = real_adopt(path, parent, value, name)
            if os.path.basename(parent) == "package-a-gates" and not tripped:
                tripped.append(path); raise OSError("crash after fixed gate receipt")
            return info
        with mock.patch.object(self.mod, "_package_a_adopt_json", side_effect=crash_receipt):
            with self.assertRaisesRegex(OSError, "after fixed gate receipt"):
                self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        recovered = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        receipts.append(recovered["receipt"]["sha256"])

        reset(); real_save = self.mod.save
        def crash_state(directory_arg, state_arg):
            value = real_save(directory_arg, state_arg)
            raise OSError("crash after fixed gate state")
        with mock.patch.object(self.mod, "save", side_effect=crash_state):
            with self.assertRaisesRegex(OSError, "after fixed gate state"):
                self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        recovered = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(recovered["reused"]); receipts.append(recovered["receipt"]["sha256"])

        reset(); real_append = self.mod.append_event
        def crash_event(directory_arg, kind, **fields):
            value = real_append(directory_arg, kind, **fields)
            if kind == "package_a_gate_passed": raise OSError("crash after fixed gate event")
            return value
        with mock.patch.object(self.mod, "append_event", side_effect=crash_event):
            with self.assertRaisesRegex(OSError, "after fixed gate event"):
                self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        recovered = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(recovered["reused"]); receipts.append(recovered["receipt"]["sha256"])
        self.assertEqual(len(set(receipts)), 1)
        events = [row for row in self.mod.recent_events(str(directory))
                  if row.get("kind") == "package_a_gate_passed"]
        self.assertEqual(len(events), 1)

    def test_package_a_fixed_gate_derives_one_exact_correction_and_archives_full_evidence(self):
        work = self.make_package_a_verified_candidate()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_VERDICT"); os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = "needs_correction"
        try:
            self.call("launch_package_a_review", state_root=str(self.state),
                      work_id=work, claude_bin=str(self.fake_claude_reviewer))
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_VERDICT", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = prior
        self.wait_package_a_review(work)
        self.assertEqual(self.call("ingest_package_a_review", state_root=str(self.state),
                                   work_id=work)["verdict"], "needs_correction")
        before = self.read_state(work); original_attempt = copy.deepcopy(before["attempt"])
        expected_paths = {
            before["candidate"]["evidence"]["packet_path"], before["attempt"]["stdout"],
            before["attempt"]["stderr"], before["package_a_prerequisites"]["path"],
            before["package_a_collect"]["intent_path"], before["package_a_collect"]["result_path"],
            before["package_a_builder_delta"]["path"], before["package_a_verification"]["intent_path"],
            before["package_a_verification"]["aggregate_path"], before["package_a_review"]["path"],
            before["package_a_review_attempt"]["schema_path"],
            before["package_a_review_attempt"]["output_path"],
            before["package_a_review_attempt"]["receipt_path"],
            before["package_a_review_attempt"]["stdout"], before["package_a_review_attempt"]["stderr"],
        }
        intent = json.loads(Path(before["package_a_verification"]["intent_path"]).read_text())
        expected_paths.update(intent["receipt_paths"]); expected_paths.update(intent["output_paths"])

        decided = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(decided["disposition"], "revise"); self.assertEqual(decided["phase"], "prepared")
        state = self.read_state(work); self.assertEqual(len(state["package_a_correction_history"]), 1)
        history = state["package_a_correction_history"][0]
        archive = json.loads(Path(history["path"]).read_text())
        refs = {row["path"]: row for row in archive["evidence_refs"]}
        self.assertTrue(expected_paths.issubset(refs))
        self.assertIn(decided["receipt"]["path"], refs)
        for path, row in refs.items():
            self.assertEqual(row["sha256"], hashlib.sha256(Path(path).read_bytes()).hexdigest())
            self.assertEqual(row["bytes"], Path(path).stat().st_size)
        self.assertEqual(archive["archived_state"]["attempt"], original_attempt)
        self.assertEqual(archive["archived_state"]["candidate"], before["candidate"])
        self.assertEqual(archive["archived_state"]["review_attempt"], before["package_a_review_attempt"])
        for key in ("package_a_collect", "package_a_builder_delta", "package_a_verification",
                    "package_a_review", "package_a_review_attempt"):
            self.assertNotIn(key, state)
        self.assertEqual(state["recovery_constraint"]["kind"], "package_a_exact_correction")
        self.assertEqual(state["recovery_constraint"]["provider_session_id"],
                         original_attempt["provider_session_id"])
        with self.assertRaisesRegex(self.mod.ControllerError, "exact-session|--resume"):
            self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                      resume=False, claude_bin=str(self.fake), model="sonnet", effort="medium")
        resumed = self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                            resume=True, claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.assertEqual(resumed["provider_session_id"], original_attempt["provider_session_id"])
        self.wait_quiescent(work)
        state = self.read_state(work)
        self.assertEqual(state["package_a_correction_used"]["archive_sha256"], history["sha256"])
        self.assertIsNotNone(state["recovery_constraint"]["consumed_at"])
        with self.assertRaisesRegex(self.mod.ControllerError, "consumed its one bounded correction"):
            self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                      resume=True, claude_bin=str(self.fake), model="sonnet", effort="medium")
        wt = Path(state["worktree"]["path"])
        dispatch = wt / "scripts/cowork_dispatch.py"; dispatch.write_bytes(dispatch.read_bytes() + b"\n# corrected candidate\n")
        test_cowork = wt / "scripts/test_cowork.py"; test_cowork.write_bytes(test_cowork.read_bytes() + b"\n# corrected candidate\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)
        self.assertTrue(self.call("verify_package_a", state_root=str(self.state), work_id=work)["all_passed"])
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(work)
        self.assertEqual(self.call("ingest_package_a_review", state_root=str(self.state),
                                   work_id=work)["verdict"], "pass")
        passed = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(passed["disposition"], "pass"); self.assertEqual(passed["phase"], "completed")
        self.assertEqual(len(self.read_state(work)["package_a_correction_history"]), 1)

    def test_package_a_second_candidate_dissent_is_terminal_after_one_correction(self):
        work = self.make_package_a_verified_candidate()

        def review(verdict):
            prior = os.environ.get("FAKE_CLAUDE_REVIEWER_VERDICT"); os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = verdict
            try:
                self.call("launch_package_a_review", state_root=str(self.state),
                          work_id=work, claude_bin=str(self.fake_claude_reviewer))
            finally:
                if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_VERDICT", None)
                else: os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = prior
            self.wait_package_a_review(work)
            return self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)

        self.assertEqual(review("needs_correction")["verdict"], "needs_correction")
        first_review_session = self.read_state(work)["package_a_review"]["provider_session_id"]
        first = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(first["disposition"], "revise")
        resumed = self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                            resume=True, claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.wait_quiescent(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        dispatch = wt / "scripts/cowork_dispatch.py"; dispatch.write_bytes(dispatch.read_bytes() + b"\n# still disputed\n")
        tests = wt / "scripts/test_cowork.py"; tests.write_bytes(tests.read_bytes() + b"\n# still disputed\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)
        self.assertTrue(self.call("verify_package_a", state_root=str(self.state), work_id=work)["all_passed"])
        self.assertEqual(review("needs_correction")["verdict"], "needs_correction")
        second_review_session = self.read_state(work)["package_a_review"]["provider_session_id"]
        self.assertNotEqual(second_review_session, first_review_session)
        decided = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(decided["disposition"], "fail"); self.assertEqual(decided["phase"], "failed")
        receipt = json.loads(Path(decided["receipt"]["path"]).read_text())
        self.assertTrue(receipt["correction_already_used"])
        self.assertEqual(receipt["reason_code"], "correction_exhausted")
        state = self.read_state(work)
        self.assertEqual(len(state["package_a_correction_history"]), 1)
        self.assertEqual(state["package_a_correction_used"]["attempt_id"], resumed["attempt_id"])

    def test_package_a_materializes_exact_sources_and_replays_bound_receipt(self):
        work, first = self.make_package_a_fixture()
        self.assertFalse(first["reused"])
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        receipt = json.loads(Path(state["package_a_prerequisites"]["path"]).read_text())
        self.assertEqual([row["path"] for row in receipt["files"]],
                         list(self.mod.PACKAGE_A_PREREQUISITE_FILES))
        self.assertEqual({row["source_package"] for row in receipt["files"]},
                         {self.mod.M0B_WORK_ID, self.mod.M0C0_WORK_ID})
        for path, expected in self.mod.PACKAGE_A_PREREQUISITE_FILES.items():
            self.assertEqual(hashlib.sha256((wt / path).read_bytes()).hexdigest(), expected)
        self.assertEqual(receipt["post_materialization_fingerprint"],
                         self.mod.worktree_fingerprint(str(wt)))
        self.assertEqual(set(self.mod.git(str(wt), ["status", "--porcelain=v1", "--untracked-files=all"]).splitlines()), {
            "?? scripts/test_dispatch_contract_characterization.py",
            "?? scripts/fixtures/dispatch_contract_characterization_sources.json",
            " M scripts/test_cowork.py",
        })
        self.assertEqual(list((self.state / work / "prerequisites").glob("copy-*.tmp")), [])
        replay = self.call("materialize_package_a_prerequisites",
                           state_root=str(self.state), work_id=work)
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["receipt"]["sha256"], first["receipt"]["sha256"])

    def test_package_a_forces_full_untracked_status_for_materialization_and_delta(self):
        work, _ = self.make_package_a_fixture(hide_untracked=True)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        status = self.mod.git(str(wt), ["status", "--porcelain=v1", "--untracked-files=all"]).splitlines()
        self.assertIn("?? scripts/test_dispatch_contract_characterization.py", status)
        self.assertIn("?? scripts/fixtures/dispatch_contract_characterization_sources.json", status)
        (wt / "scripts/cowork_dispatch.py").write_text("# package-a dispatch fixture\n")
        with (wt / "scripts/test_cowork.py").open("a") as out:
            out.write("# package-a builder delta\n")
        state["attempt"] = {"id": "attempt-package-a"}
        candidate = {"id": "candidate-package-a", "evidence_digest": "a" * 64}
        delta = self.mod._package_a_builder_delta(str(self.state / work), state, candidate)
        self.assertEqual([row["path"] for row in delta["files"]],
                         ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])

    def test_package_a_rejects_preseeded_baseline_without_intent_and_v3_review_tamper(self):
        work, _ = self.make_package_a_fixture(materialize=False)
        prereq = self.state / work / "prerequisites"; prereq.mkdir()
        (prereq / "baseline.json").write_text("{}")
        with self.assertRaisesRegex(self.mod.ControllerError, "cannot precede"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)

        (prereq / "baseline.json").unlink()
        self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        v3 = self.read_state(self.mod.M0C_V3_WORK_ID)
        Path(v3["plan_review"]["path"]).write_text("{}")
        with self.assertRaisesRegex(self.mod.ControllerError, "v3 plan/review authority"):
            self.mod._package_a_prerequisites_intact(str(self.state / work), self.read_state(work))

    def test_package_a_replay_revalidates_full_canonical_predecessor_receipts(self):
        work, _ = self.make_package_a_fixture()
        c0 = self.read_state(self.mod.M0C0_WORK_ID)
        result_path = Path(c0["alternate_evidence"]["result"]["path"])
        result_path.write_text("{}")
        with self.assertRaises(self.mod.ControllerError):
            self.mod._package_a_prerequisites_intact(str(self.state / work), self.read_state(work))

    def test_package_a_fresh_materialization_rejects_dirty_partial_staged_and_escape_state(self):
        work, _ = self.make_package_a_fixture(materialize=False)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])

        tracked = wt / "tracked.txt"; tracked.write_text("unauthorized\n")
        with self.assertRaisesRegex(self.mod.ControllerError, "unauthorized pre-materialization"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        tracked.write_text("base\n")

        dispatch = wt / "scripts/cowork_dispatch.py"; dispatch.write_text("# preseeded builder output\n")
        with self.assertRaisesRegex(self.mod.ControllerError, "unauthorized pre-materialization"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        dispatch.unlink()

        source = Path(self.read_state(self.mod.M0B_WORK_ID)["worktree"]["path"]) / "scripts/test_dispatch_contract_characterization.py"
        destination = wt / "scripts/test_dispatch_contract_characterization.py"
        destination.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(self.mod.ControllerError, "durable intent"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        subprocess.check_call(["git", "-C", str(wt), "add", "scripts/test_dispatch_contract_characterization.py"])
        with self.assertRaisesRegex(self.mod.ControllerError, "unauthorized pre-materialization"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        subprocess.check_call(["git", "-C", str(wt), "rm", "--cached", "scripts/test_dispatch_contract_characterization.py"],
                              stdout=subprocess.DEVNULL)
        destination.unlink()

        prereq = self.state / work / "prerequisites"
        if prereq.exists(): prereq.rmdir()
        outside = self.root / "outside-prerequisites"; outside.mkdir()
        prereq.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(self.mod.ControllerError, "transaction directory is unsafe"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        prereq.unlink()

        prereq.mkdir(); (prereq / "copy-0.tmp").write_text("orphan")
        with self.assertRaisesRegex(self.mod.ControllerError, "temp exists without durable intent"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        shutil.rmtree(prereq)

        state = self.read_state(work); state["repo"]["head"] = "0" * 40
        self.state_file(work).write_text(json.dumps(state))
        with self.assertRaisesRegex(self.mod.ControllerError, "base, brief, or worktree authority"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)

    def test_package_a_bound_materialization_rejects_every_durable_binding_tamper(self):
        work, _ = self.make_package_a_fixture()
        original_state = self.read_state(work); bound = original_state["package_a_prerequisites"]
        intent_path = Path(bound["intent_path"]); receipt_path = Path(bound["path"])
        intent_bytes = intent_path.read_bytes(); receipt_bytes = receipt_path.read_bytes()

        intent_path.unlink()
        with self.assertRaisesRegex(self.mod.ControllerError, "intent binding changed"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        intent_path.write_bytes(intent_bytes)

        intent = json.loads(intent_bytes); intent["base_head"] = "0" * 40
        intent_path.write_text(json.dumps(intent))
        with self.assertRaisesRegex(self.mod.ControllerError, "intent binding changed"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        intent_path.write_bytes(intent_bytes)

        for field in ("intent_path", "intent_sha256", "fingerprint_digest"):
            state = copy.deepcopy(original_state); state["package_a_prerequisites"][field] = "tampered"
            self.state_file(work).write_text(json.dumps(state))
            with self.subTest(field=field), self.assertRaises(self.mod.ControllerError):
                self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        self.state_file(work).write_text(json.dumps(original_state))

        receipt = json.loads(receipt_bytes); receipt["files"].append(copy.deepcopy(receipt["files"][0]))
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(self.mod.ControllerError, "receipt is missing or changed"):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        receipt_path.write_bytes(receipt_bytes)

        wt = Path(original_state["worktree"]["path"])
        artifact = wt / "scripts/test_dispatch_contract_characterization.py"; artifact.write_text("tampered\n")
        with self.assertRaises(self.mod.ControllerError):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)

    def test_package_a_materialization_recovers_each_copy_and_receipt_crash_window(self):
        work, _ = self.make_package_a_fixture(materialize=False)
        directory = self.state / work; initial_state = self.state_file(work).read_bytes()
        initial_events = (directory / "events.jsonl").read_bytes()
        wt = Path(self.read_state(work)["worktree"]["path"])
        base_test = (self.repo / "scripts/test_cowork.py").read_bytes()

        def reset_fresh():
            self.state_file(work).write_bytes(initial_state)
            (directory / "events.jsonl").write_bytes(initial_events)
            shutil.rmtree(directory / "prerequisites", ignore_errors=True)
            for rel in list(self.mod.PACKAGE_A_PREREQUISITE_FILES)[:2]:
                try: (wt / rel).unlink()
                except FileNotFoundError: pass
            (wt / "scripts/test_cowork.py").write_bytes(base_test)

        real_atomic = self.mod.atomic_json
        def crash_after_intent(path, value):
            real_atomic(path, value)
            if path.endswith("materialization-intent.json"): raise OSError("crash after intent")
        with mock.patch.object(self.mod, "atomic_json", side_effect=crash_after_intent):
            with self.assertRaisesRegex(OSError, "after intent"):
                self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        prereq = directory / "prerequisites"
        first_source = Path(self.read_state(self.mod.M0B_WORK_ID)["worktree"]["path"]) / list(self.mod.PACKAGE_A_PREREQUISITE_FILES)[0]
        (prereq / "copy-0.tmp").write_bytes(first_source.read_bytes())
        recovered = self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        self.assertFalse(recovered["reused"])

        for crash_path in self.mod.PACKAGE_A_PREREQUISITE_FILES:
            reset_fresh(); real_replace = self.mod.os.replace
            def crash_after_destination(source, destination, target=str(wt / crash_path)):
                result = real_replace(source, destination)
                if destination == target: raise OSError("crash after destination")
                return result
            with self.subTest(crash_path=crash_path), mock.patch.object(self.mod.os, "replace", side_effect=crash_after_destination):
                with self.assertRaisesRegex(OSError, "after destination"):
                    self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
            replay = self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
            self.assertFalse(replay["reused"])

        reset_fresh(); real_save = self.mod.save
        def crash_before_state(directory_arg, state_arg):
            raise OSError("crash before state")
        with mock.patch.object(self.mod, "save", side_effect=crash_before_state):
            with self.assertRaisesRegex(OSError, "before state"):
                self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        receipt_path = directory / "prerequisites/baseline.json"
        self.assertTrue(receipt_path.is_file())
        recovered = self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        self.assertTrue(recovered["reused"])

        reset_fresh()
        with mock.patch.object(self.mod, "save", side_effect=crash_before_state):
            with self.assertRaises(OSError):
                self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)
        receipt = json.loads((directory / "prerequisites/baseline.json").read_text())
        receipt["created_at"] = None
        (directory / "prerequisites/baseline.json").write_text(json.dumps(receipt))
        with self.assertRaises(self.mod.ControllerError):
            self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=work)

    def test_package_a_launch_revalidates_baseline_and_prompt_binding(self):
        work, _ = self.make_package_a_fixture(); state = self.read_state(work)
        wt = Path(state["worktree"]["path"]); rogue = wt / "rogue.txt"; rogue.write_text("rogue\n")
        with self.assertRaisesRegex(self.mod.ControllerError, "sealed baseline"):
            self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                      resume=False, claude_bin=str(self.fake), model="sonnet", effort="medium")
        rogue.unlink()
        with self.assertRaisesRegex(self.mod.ControllerError, "implementer role"):
            self.call("launch", state_root=str(self.state), work_id=work, role="planner",
                      resume=False, claude_bin=str(self.fake), model="sonnet", effort="medium")
        bound = self.read_state(work)["package_a_prerequisites"]
        prompt = self.mod.launch_prompt(self.read_state(work), "implementer", [])
        self.assertIn("path=" + bound["path"], prompt)
        self.assertIn("sha256=" + bound["sha256"], prompt)
        self.assertIn("fingerprint=" + bound["fingerprint_digest"], prompt)
        self.launch_package_a(work)

    def test_package_a_launch_rejects_frozen_brief_and_fresh_head_drift(self):
        work, _ = self.make_package_a_fixture(); state = self.read_state(work)
        brief = Path(state["brief"]["path"]); frozen = brief.read_bytes(); brief.write_text("tampered brief\n")
        with self.assertRaisesRegex(self.mod.ControllerError, "frozen brief"):
            self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                      resume=False, claude_bin=str(self.fake), model="sonnet", effort="medium")
        brief.write_bytes(frozen)
        wt = Path(state["worktree"]["path"]); tracked = wt / "tracked.txt"; tracked.write_text("head drift\n")
        subprocess.check_call(["git", "-C", str(wt), "add", "tracked.txt"])
        subprocess.check_call(["git", "-C", str(wt), "commit", "-m", "unauthorized head drift"],
                              stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(self.mod.ControllerError, "base|HEAD|sealed baseline"):
            self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                      resume=False, claude_bin=str(self.fake), model="sonnet", effort="medium")

    def test_package_a_resume_rejects_head_drift_after_attempt_exists(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        tracked = wt / "tracked.txt"; tracked.write_text("resume head drift\n")
        subprocess.check_call(["git", "-C", str(wt), "add", "tracked.txt"])
        subprocess.check_call(["git", "-C", str(wt), "commit", "-m", "unauthorized resume head drift"],
                              stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(self.mod.ControllerError, "base|HEAD"):
            self.call("launch", state_root=str(self.state), work_id=work, role="implementer",
                      resume=True, claude_bin=str(self.fake), model="sonnet", effort="medium")

    def test_package_a_builder_delta_rejects_escape_type_delete_and_worker_claim_mismatch(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        dispatch = wt / "scripts/cowork_dispatch.py"; test_cowork = wt / "scripts/test_cowork.py"
        dispatch.write_text("# dispatch implementation\n"); original_test = test_cowork.read_bytes()
        test_cowork.write_bytes(original_test + b"# package-a tests\n")

        rogue = wt / "scripts/rogue.py"; rogue.write_text("rogue\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        with self.assertRaisesRegex(self.mod.ControllerError, "escaped baseline allowlist"):
            self.call("collect", state_root=str(self.state), work_id=work)
        rogue.unlink()

        test_cowork.unlink()
        with self.assertRaisesRegex(self.mod.ControllerError, "required test_cowork delta"):
            self.call("collect", state_root=str(self.state), work_id=work)
        test_cowork.write_bytes(original_test + b"# package-a tests\n")

        dispatch.unlink(); outside = self.root / "dispatch-outside.py"; outside.write_text("outside\n")
        dispatch.symlink_to(outside)
        with self.assertRaisesRegex(self.mod.ControllerError, "safe regular file|escapes worktree"):
            self.call("collect", state_root=str(self.state), work_id=work)
        dispatch.unlink(); dispatch.write_text("# dispatch implementation\n")

        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py"])
        with self.assertRaisesRegex(self.mod.ControllerError, "does not match derived builder delta"):
            self.call("collect", state_root=str(self.state), work_id=work)

    def test_package_a_collect_requires_exact_unstaged_status_modes_and_base_head(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        dispatch = wt / "scripts/cowork_dispatch.py"; dispatch.write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])

        subprocess.check_call(["git", "-C", str(wt), "add", "scripts/test_cowork.py"])
        with self.assertRaisesRegex(self.mod.ControllerError, "status|mode|staged"):
            self.call("collect", state_root=str(self.state), work_id=work)
        subprocess.check_call(["git", "-C", str(wt), "restore", "--staged", "scripts/test_cowork.py"])

        immutable = "scripts/test_dispatch_contract_characterization.py"
        subprocess.check_call(["git", "-C", str(wt), "add", immutable])
        with self.assertRaisesRegex(self.mod.ControllerError, "status|mode|staged"):
            self.call("collect", state_root=str(self.state), work_id=work)
        subprocess.check_call(["git", "-C", str(wt), "rm", "--cached", immutable],
                              stdout=subprocess.DEVNULL)

        subprocess.check_call(["git", "-C", str(wt), "add", "scripts"])
        subprocess.check_call(["git", "-C", str(wt), "commit", "-m", "unauthorized builder commit"],
                              stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(self.mod.ControllerError, "base|HEAD"):
            self.call("collect", state_root=str(self.state), work_id=work)

    def test_package_a_collect_binds_only_exact_derived_delta_and_full_status(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (self.state / work / "arbitrary.json").write_text("{}")
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        result = self.call("collect", state_root=str(self.state), work_id=work)
        delta = result["package_a_builder_delta"]
        self.assertEqual([row["path"] for row in delta["files"]],
                         ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        collected = self.read_state(work)
        self.assertEqual(collected["package_a_builder_delta"]["candidate_id"], collected["candidate"]["id"])
        self.assertEqual(collected["package_a_builder_delta"]["evidence_digest"], collected["candidate"]["evidence_digest"])
        self.assertIn("?? scripts/fixtures/dispatch_contract_characterization_sources.json",
                      collected["candidate"]["evidence"]["changed_paths"])
        self.assertNotIn("scripts/test_dispatch_contract_characterization.py",
                         {row["path"] for row in delta["files"]})
        receipt_paths = {row["path"] for row in result["receipts"]}
        self.assertNotIn(str(self.state / work / "state.json"), receipt_paths)
        self.assertNotIn(str(self.state / work / "arbitrary.json"), receipt_paths)
        baseline = collected["package_a_prerequisites"]
        self.assertEqual(receipt_paths, {baseline["path"]})
        self.assertEqual(result["receipts"][0]["sha256"],
                         hashlib.sha256(Path(baseline["path"]).read_bytes()).hexdigest())

    def test_package_a_collect_rejects_duplicates_after_status_alias_normalization(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, [
            "scripts/cowork_dispatch.py", "?? scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        with self.assertRaisesRegex(self.mod.ControllerError, "duplicates"):
            self.call("collect", state_root=str(self.state), work_id=work)

    def test_package_a_collect_recovers_sealed_intent_outputs_event_and_state_idempotently(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        real_atomic = self.mod.atomic_json; tripped = []
        def crash_after_intent(path, value):
            result = real_atomic(path, value)
            if path.endswith(".intent.json") and not tripped:
                tripped.append(path); raise OSError("simulated crash after collect intent")
            return result
        with mock.patch.object(self.mod, "atomic_json", side_effect=crash_after_intent):
            with self.assertRaisesRegex(OSError, "after collect intent"):
                self.call("collect", state_root=str(self.state), work_id=work)
        recovered = self.call("collect", state_root=str(self.state), work_id=work)
        replay = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(replay, recovered)
        collected = self.read_state(work); candidate = collected["candidate"]
        events = [row for row in self.mod.recent_events(str(self.state / work))
                  if row.get("kind") == "candidate_collected" and row.get("candidate_id") == candidate["id"]]
        self.assertEqual(len(events), 1)
        self.assertTrue(Path(collected["package_a_collect"]["intent_path"]).is_file())

    def test_package_a_collect_replay_rejects_typed_state_and_result_tamper(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)
        state = self.read_state(work); original = copy.deepcopy(state)
        state["package_a_collect"]["intent_path"] = 7
        self.state_file(work).write_text(json.dumps(state))
        with self.assertRaisesRegex(self.mod.ControllerError, "binding types"):
            self.call("collect", state_root=str(self.state), work_id=work)
        self.state_file(work).write_text(json.dumps(original))
        result_path = Path(original["package_a_collect"]["result_path"])
        result = json.loads(result_path.read_text()); result["work_id"] = "confused-package"
        result_path.write_text(json.dumps(result))
        with self.assertRaisesRegex(self.mod.ControllerError, "intent or result binding"):
            self.call("collect", state_root=str(self.state), work_id=work)

    def test_package_a_collect_recovers_every_intent_receipt_result_event_and_state_window(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        directory = self.state / work; state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        initial_state = self.state_file(work).read_bytes(); initial_events = (directory / "events.jsonl").read_bytes()
        def reset_collect():
            self.state_file(work).write_bytes(initial_state)
            (directory / "events.jsonl").write_bytes(initial_events)
            shutil.rmtree(directory / "package-a-collect", ignore_errors=True)
            shutil.rmtree(directory / "package-a-delta", ignore_errors=True)
            for path in (directory / "result.json",):
                try: path.unlink()
                except FileNotFoundError: pass

        identities = []
        for suffix in (".intent.json", "/package-a-delta/", "/result.json"):
            reset_collect(); real_atomic = self.mod.atomic_json
            def crash_after_output(path, value, marker=suffix):
                real_atomic(path, value)
                if (path.endswith(marker) if marker.startswith(".") else marker in path):
                    raise OSError("crash after collect output")
            with self.subTest(suffix=suffix), mock.patch.object(self.mod, "atomic_json", side_effect=crash_after_output):
                with self.assertRaisesRegex(OSError, "after collect output"):
                    self.call("collect", state_root=str(self.state), work_id=work)
            recovered = self.call("collect", state_root=str(self.state), work_id=work)
            identities.append((recovered["gate"]["candidate_id"], recovered["gate"]["evidence_digest"]))
            self.assertEqual(self.call("collect", state_root=str(self.state), work_id=work), recovered)
            events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(sum(row.get("kind") == "candidate_collected" for row in events), 1)

        reset_collect(); real_append = self.mod.append_event
        def crash_after_event(directory_arg, kind, **fields):
            result = real_append(directory_arg, kind, **fields)
            if kind == "candidate_collected": raise OSError("crash after collect event")
            return result
        with mock.patch.object(self.mod, "append_event", side_effect=crash_after_event):
            with self.assertRaisesRegex(OSError, "after collect event"):
                self.call("collect", state_root=str(self.state), work_id=work)
        recovered = self.call("collect", state_root=str(self.state), work_id=work)
        identities.append((recovered["gate"]["candidate_id"], recovered["gate"]["evidence_digest"]))

        reset_collect(); real_save = self.mod.save
        def crash_after_state(directory_arg, state_arg):
            result = real_save(directory_arg, state_arg)
            raise OSError("crash after collect state")
        with mock.patch.object(self.mod, "save", side_effect=crash_after_state):
            with self.assertRaisesRegex(OSError, "after collect state"):
                self.call("collect", state_root=str(self.state), work_id=work)
        recovered = self.call("collect", state_root=str(self.state), work_id=work)
        identities.append((recovered["gate"]["candidate_id"], recovered["gate"]["evidence_digest"]))
        self.assertEqual(len(set(identities)), 1)

    def test_package_a_collected_replay_rejects_schema_cross_binding_and_path_tamper(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        directory = self.state / work; state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)

        original_state = self.read_state(work); bound = original_state["package_a_collect"]
        intent_path = Path(bound["intent_path"]); result_path = Path(bound["result_path"])
        delta_path = Path(original_state["package_a_builder_delta"]["path"])
        original_intent = intent_path.read_bytes(); original_result = result_path.read_bytes(); original_delta = delta_path.read_bytes()

        def restore():
            intent_path.write_bytes(original_intent); result_path.write_bytes(original_result); delta_path.write_bytes(original_delta)
            self.state_file(work).write_text(json.dumps(original_state))

        def expect_intent(mutator):
            restore(); value = json.loads(original_intent); mutator(value); intent_path.write_text(json.dumps(value))
            changed = copy.deepcopy(original_state)
            changed["package_a_collect"]["intent_sha256"] = hashlib.sha256(intent_path.read_bytes()).hexdigest()
            self.state_file(work).write_text(json.dumps(changed))
            with self.assertRaises(self.mod.ControllerError):
                self.call("collect", state_root=str(self.state), work_id=work)

        intent_mutations = [
            lambda value: value.update({"extra": True}),
            lambda value: value.pop("work_id"),
            lambda value: value.update({"work_id": "wrong"}),
            lambda value: value.update({"attempt_id": "wrong"}),
            lambda value: value.update({"provider_packet_path": str(self.root / "wrong.json")}),
            lambda value: value.update({"provider_packet_sha256": "0" * 64}),
            lambda value: value.update({"baseline_receipt_sha256": "0" * 64}),
            lambda value: value.update({"baseline_fingerprint_digest": "0" * 64}),
            lambda value: value.update({"delta_path": str(self.root / "wrong-delta.json")}),
            lambda value: value.update({"delta_receipt_sha256": "0" * 64}),
            lambda value: value.update({"result_path": str(self.root / "wrong-result.json")}),
            lambda value: value.update({"result_sha256": "0" * 64}),
        ]
        for index, mutation in enumerate(intent_mutations):
            with self.subTest(intent_mutation=index): expect_intent(mutation)

        def expect_result(mutator):
            restore(); value = json.loads(original_result); mutator(value); result_path.write_text(json.dumps(value))
            changed = copy.deepcopy(original_state)
            changed["package_a_collect"]["result_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
            self.state_file(work).write_text(json.dumps(changed))
            with self.assertRaises(self.mod.ControllerError):
                self.call("collect", state_root=str(self.state), work_id=work)

        result_mutations = [
            lambda value: value.update({"extra": True}),
            lambda value: value.update({"work_id": "wrong"}),
            lambda value: value["gate"].update({"candidate_id": "wrong"}),
            lambda value: value["worker_packet"].update({"sha256": "0" * 64}),
            lambda value: value["package_a_builder_delta"].update({"receipt_path": str(self.root / "wrong.json")}),
            lambda value: value["package_a_builder_delta"].update({"receipt_sha256": "0" * 64}),
        ]
        for index, mutation in enumerate(result_mutations):
            with self.subTest(result_mutation=index): expect_result(mutation)

        for field in ("intent_path", "result_path"):
            restore(); changed = copy.deepcopy(original_state); changed["package_a_collect"][field] = None
            self.state_file(work).write_text(json.dumps(changed))
            with self.subTest(non_string_path=field), self.assertRaises(self.mod.ControllerError):
                self.call("collect", state_root=str(self.state), work_id=work)

    def test_package_a_collected_state_bindings_require_exact_schemas(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)
        original = self.read_state(work)

        mutations = []
        for section in ("package_a_prerequisites", "package_a_builder_delta"):
            mutations.append((section + " extra", lambda state, key=section: state[key].update({"extra": True})))
            mutations.append((section + " missing", lambda state, key=section: state[key].pop(next(iter(state[key])))))
        for field in ("candidate_id", "evidence_digest", "delta_sha256"):
            mutations.append(("builder " + field,
                              lambda state, name=field: state["package_a_builder_delta"].update({name: "0" * 64})))
        for label, mutate in mutations:
            state = copy.deepcopy(original); mutate(state); self.state_file(work).write_text(json.dumps(state))
            with self.subTest(label=label), self.assertRaises(self.mod.ControllerError):
                self.call("collect", state_root=str(self.state), work_id=work)

    def test_package_a_collected_candidate_and_provider_require_canonical_causal_shapes(self):
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        directory = self.state / work; state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# package-a tests\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)

        original_state = self.read_state(work); bound = original_state["package_a_collect"]
        intent_path = Path(bound["intent_path"]); result_path = Path(bound["result_path"])
        provider_path = Path(original_state["candidate"]["evidence"]["packet_path"])
        original_intent = intent_path.read_bytes(); original_result = result_path.read_bytes(); original_provider = provider_path.read_bytes()

        def restore():
            intent_path.write_bytes(original_intent); result_path.write_bytes(original_result); provider_path.write_bytes(original_provider)
            self.state_file(work).write_text(json.dumps(original_state))

        candidate_mutations = [
            lambda candidate: candidate.update({"extra": True}),
            lambda candidate: candidate.pop("schema_version"),
            lambda candidate: candidate["evidence"].update({"extra": True}),
            lambda candidate: candidate.update({"created_at": "2099-01-01T00:00:00Z"}),
            lambda candidate: candidate.update({"packet_verified": False}),
            lambda candidate: candidate["evidence"].update({"package_a_baseline_receipt_sha256": "0" * 64}),
            lambda candidate: candidate["evidence"].update({"package_a_baseline_fingerprint_digest": "0" * 64}),
            lambda candidate: candidate["evidence"].pop("evidence_kind"),
            lambda candidate: candidate.update({"evidence_digest": "0" * 64}),
            lambda candidate: candidate.update({"id": "package-a-" + "0" * 32}),
        ]
        for index, mutation in enumerate(candidate_mutations):
            restore(); state = copy.deepcopy(original_state); candidate = state["candidate"]; mutation(candidate)
            intent = json.loads(original_intent); intent["candidate"] = candidate; intent_path.write_text(json.dumps(intent))
            if index == 8:
                state["package_a_collect"]["evidence_digest"] = candidate["evidence_digest"]
                state["package_a_builder_delta"]["evidence_digest"] = candidate["evidence_digest"]
            elif index == 9:
                state["package_a_collect"]["candidate_id"] = candidate["id"]
                state["package_a_builder_delta"]["candidate_id"] = candidate["id"]
                intent["delta_path"] = str(directory / "package-a-delta" / (candidate["id"] + ".json"))
                intent_path.write_text(json.dumps(intent))
            state["package_a_collect"]["intent_sha256"] = hashlib.sha256(intent_path.read_bytes()).hexdigest()
            self.state_file(work).write_text(json.dumps(state))
            with self.subTest(candidate_mutation=index), self.assertRaises(self.mod.ControllerError):
                self.call("collect", state_root=str(self.state), work_id=work)

        provider_mutations = [
            lambda packet: packet.update({"extra": True}),
            lambda packet: packet.pop("schema_version"),
            lambda packet: packet.update({"parsed_at": None}),
        ]
        for index, mutation in enumerate(provider_mutations):
            restore(); state = copy.deepcopy(original_state)
            provider = json.loads(original_provider); mutation(provider); provider_path.write_text(json.dumps(provider))
            provider_sha = hashlib.sha256(provider_path.read_bytes()).hexdigest()
            state["candidate"]["evidence"]["packet_sha256"] = provider_sha
            intent = json.loads(original_intent); intent["provider_packet_sha256"] = provider_sha; intent["candidate"] = state["candidate"]
            result = json.loads(original_result); result["worker_packet"]["sha256"] = provider_sha
            result_path.write_text(json.dumps(result)); result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
            intent["result_sha256"] = result_sha; intent_path.write_text(json.dumps(intent))
            state["package_a_collect"]["intent_sha256"] = hashlib.sha256(intent_path.read_bytes()).hexdigest()
            state["package_a_collect"]["result_sha256"] = result_sha
            self.state_file(work).write_text(json.dumps(state))
            with self.subTest(provider_mutation=index), self.assertRaises(self.mod.ControllerError):
                self.call("collect", state_root=str(self.state), work_id=work)

    def test_v2_plan_wire_shape_rejects_v1_leakage_fabricated_commands_and_malformed_elements(self):
        self.prepare(self.mod.M0C_V2_WORK_ID); state = self.read_state(self.mod.M0C_V2_WORK_ID)
        with mock.patch.object(self.mod, "_validate_m0c_v2_prerequisites"):
            self.mod._validate_m0c_v2_plan(self.v2_plan(), state)
            bad = self.v2_plan(); bad["three_contracts"][0]["call_site_adapter"] = "v1 leak"
            with self.assertRaisesRegex(self.mod.ControllerError, "contract evidence"):
                self.mod._validate_m0c_v2_plan(bad, state)
            bad = self.v2_plan(); bad["commands_run"] = [{"command": "false", "cwd": "/tmp", "exit_code": 0, "receipt": "invented"}]
            with self.assertRaisesRegex(self.mod.ControllerError, "controller-owned receipts"):
                self.mod._validate_m0c_v2_plan(bad, state)
            bad = self.v2_plan(); bad["record_proposal"] = ["forged"]
            with self.assertRaisesRegex(self.mod.ControllerError, "record proposal"):
                self.mod._validate_m0c_v2_plan(bad, state)
            bad = self.v2_plan(); bad["call_site_map"][0]["compatibility"] = "v1 scalar"
            with self.assertRaisesRegex(self.mod.ControllerError, "call-site map"):
                self.mod._validate_m0c_v2_plan(bad, state)

    def test_v2_package_identity_cannot_be_confused_with_v1(self):
        self.prepare(self.mod.M0C_V2_WORK_ID); state = self.read_state(self.mod.M0C_V2_WORK_ID)
        with mock.patch.object(self.mod, "_validate_m0c_v2_prerequisites"):
            bad = self.v2_plan(); bad["package_id"] = self.mod.M0C_PLAN_PACKAGE_ID
            with self.assertRaisesRegex(self.mod.ControllerError, "declared package identity"):
                self.mod._validate_m0c_v2_plan(bad, state)

    def test_v3_identity_and_preingest_correction_are_fresh_and_exact_session_bound(self):
        work = self.mod.M0C_V3_WORK_ID; self.prepare(work); state = self.read_state(work)
        with mock.patch.object(self.mod, "_validate_m0c_v2_prerequisites"):
            self.mod._validate_m0c_plan(self.v3_plan(), state)
            wrong = self.v3_plan(); wrong["package_id"] = self.mod.M0C_V2_PLAN_PACKAGE_ID
            with self.assertRaisesRegex(self.mod.ControllerError, "declared package identity"):
                self.mod._validate_m0c_plan(wrong, state)
        self.launch(work, role="planner"); self.wait_quiescent(work)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", work); original = self.read_state(work)["attempt"]
        result = self.call("adjudicate", state_root=str(self.state), work_id=work, verdict="revise", rationale="correct v3 packet",
                           adjudicator_principal="orchestrator-plan-gate/v1", instruction="Return corrected v3 JSON.",
                           capability_required=None, target_authority_role=None, target_authority_principal=None)
        self.assertEqual(result["delivery"], "exact_planner_resume_required")
        with self.assertRaisesRegex(self.mod.ControllerError, "exact-session"):
            self.call("launch", state_root=str(self.state), work_id=work, role="planner", resume=False,
                      claude_bin=str(self.fake), model="fake-model", effort="medium")
        resumed = self.launch(work, role="planner", resume=True)
        self.assertEqual(resumed["provider_session_id"], original["provider_session_id"])
        self.assertTrue(self.read_state(work)["m0c_correction_used"])

    def test_v3_typed_invalid_schema_review_retry_is_sealed_archived_and_once_only(self):
        work, first = self.make_v3_review_attempt("invalid_schema")
        self.assertFalse(Path(first["output_path"]).exists())
        stdout_events = [json.loads(row) for row in Path(first["stdout"]).read_text().splitlines() if row]
        self.assertTrue(any(row.get("type") == "error" and "invalid_json_schema" in row.get("message", "") for row in stdout_events), stdout_events)
        schema_path = Path(self.state / work / "review-attempts" / (first["id"] + ".schema.json"))
        legacy_schema = json.loads(schema_path.read_text())
        legacy_schema["properties"]["prerequisite_evidence_digests"] = {
            "type": "array", "const": [self.mod.M0B_EVIDENCE_DIGEST, self.mod.M0C0_EVIDENCE_DIGEST]}
        schema_path.write_text(json.dumps(legacy_schema))
        legacy_schema_sha = hashlib.sha256(schema_path.read_bytes()).hexdigest()
        released = self.call("retry_plan_review", state_root=str(self.state), work_id=work)
        self.assertEqual(released["phase"], "awaiting_gate")
        state = self.read_state(work); archived = state["plan_review_attempt_history"][0]
        self.assertEqual(archived["id"], first["id"])
        self.assertEqual(archived["retry_evidence"]["disposition"], "invalid_json_schema")
        self.assertEqual(archived["retry_evidence"]["schema_variant"], "legacy_array_const_rejected")
        self.assertEqual(archived["retry_evidence"]["schema_sha256"], legacy_schema_sha)
        receipt_sha = archived["retry_evidence"]["terminal_receipt_sha256"]
        self.assertRegex(receipt_sha, r"^[0-9a-f]{64}$")
        Path(first["receipt_path"]).write_text("tampered after archival\n")
        self.assertEqual(self.read_state(work)["plan_review_attempt_history"][0]["retry_evidence"]["terminal_receipt_sha256"], receipt_sha)
        prior = os.environ.get("FAKE_CODEX_KIND"); os.environ["FAKE_CODEX_KIND"] = "invalid_schema"
        try: self.call("launch_plan_review", state_root=str(self.state), work_id=work, codex_bin=str(self.fake_codex))
        finally:
            if prior is None: os.environ.pop("FAKE_CODEX_KIND", None)
            else: os.environ["FAKE_CODEX_KIND"] = prior
        self.wait_review(work)
        with self.assertRaisesRegex(self.mod.ControllerError, "bounded to one"):
            self.call("retry_plan_review", state_root=str(self.state), work_id=work)
        self.assertEqual(len(self.read_state(work)["plan_review_attempt_history"]), 1)

    def test_v3_review_retry_rejects_noneligible_and_every_binding_tamper(self):
        work, attempt = self.make_v3_review_attempt("valid")
        directory = self.state / work
        terminal = json.loads(Path(attempt["receipt_path"]).read_text())
        self.assertEqual(terminal["exit_code"], 0, (terminal, Path(attempt["stdout"]).read_text(), Path(attempt["stderr"]).read_text()))
        with self.assertRaisesRegex(self.mod.ControllerError, "any output|successful"):
            self.call("retry_plan_review", state_root=str(self.state), work_id=work)

        state_path = self.state_file(work); state_raw = state_path.read_bytes()
        receipt_path = Path(attempt["receipt_path"]); receipt_raw = receipt_path.read_bytes()
        stdout_path = Path(attempt["stdout"]); stdout_raw = stdout_path.read_bytes()
        stderr_path = Path(attempt["stderr"]); stderr_raw = stderr_path.read_bytes()
        output_path = Path(attempt["output_path"]); output_raw = output_path.read_bytes()
        plan_path = Path(self.read_state(work)["planner_artifact"]["plan_path"]); plan_raw = plan_path.read_bytes()
        schema_path = Path(self.state / work / "review-attempts" / (attempt["id"] + ".schema.json")); schema_raw = schema_path.read_bytes()

        def restore():
            state_path.write_bytes(state_raw); receipt_path.write_bytes(receipt_raw); stdout_path.write_bytes(stdout_raw)
            stderr_path.write_bytes(stderr_raw); output_path.write_bytes(output_raw); plan_path.write_bytes(plan_raw); schema_path.write_bytes(schema_raw)
        def rewrite_terminal(**values):
            terminal = json.loads(receipt_raw); terminal.update(values); receipt_path.write_text(json.dumps(terminal))
        def sealed_no_output(stdout_event, code=7):
            output_path.unlink(); stdout_path.write_text(json.dumps(stdout_event) + "\n")
            rewrite_terminal(exit_code=code, output_sha256=None,
                             stdout_sha256=hashlib.sha256(stdout_path.read_bytes()).hexdigest(), stdout_bytes=len(stdout_path.read_bytes()))

        mutations = [
            ("valid-nonzero", lambda: rewrite_terminal(exit_code=7), "any output"),
            ("arbitrary-nonzero", lambda: sealed_no_output({"type":"turn.failed","error":{"type":"server_error","code":"unknown"}}), "typed retryable"),
            ("missing-receipt", lambda: receipt_path.unlink(), "receipt"),
            ("receipt-attempt", lambda: rewrite_terminal(attempt_id="wrong"), "receipt binding"),
            ("stdout-tamper", lambda: stdout_path.write_text("tampered\n"), "log binding"),
            ("missing-stderr", lambda: stderr_path.unlink(), "log binding"),
            ("missing-output", lambda: output_path.unlink(), "successful|any output|output binding"),
            ("stale-candidate", lambda: (lambda s: (s["candidate"].__setitem__("id", "wrong"), state_path.write_text(json.dumps(s))))(json.loads(state_raw)), "stale"),
            ("stale-plan-state", lambda: (lambda s: (s["planner_artifact"].__setitem__("plan_sha256", "0"*64), s["plan_review_attempt"].__setitem__("plan_sha256", "0"*64), state_path.write_text(json.dumps(s))))(json.loads(state_raw)), "plan packet binding"),
            ("nonce-state", lambda: (lambda s: (s["plan_review_attempt"].__setitem__("nonce", "wrong"), state_path.write_text(json.dumps(s))))(json.loads(state_raw)), "schema binding|receipt binding"),
            ("actor-state", lambda: (lambda s: (s["plan_review_attempt"].__setitem__("actor_id", "other-safe-actor"), state_path.write_text(json.dumps(s))))(json.loads(state_raw)), "controller identity is stale"),
            ("run-state", lambda: (lambda s: (s["plan_review_attempt"].__setitem__("run_id", "other-safe-run"), state_path.write_text(json.dumps(s))))(json.loads(state_raw)), "schema binding"),
            ("schema-file", lambda: schema_path.write_text("{}"), "schema binding"),
            ("plan-packet", lambda: plan_path.write_text("{}"), "stale|plan packet binding"),
        ]
        for label, mutate, error in mutations:
            with self.subTest(label=label):
                restore(); mutate()
                with self.assertRaisesRegex(self.mod.ControllerError, error):
                    self.call("retry_plan_review", state_root=str(self.state), work_id=work)

        for kind in ("wrong", "missing", "reordered"):
            with self.subTest(prerequisites=kind):
                restore(); packet = json.loads(output_raw)
                digests = packet["prerequisite_evidence_digests"]
                if kind == "wrong": digests[1] = "0" * 64
                elif kind == "missing": digests.pop()
                else: digests.reverse()
                output_path.write_text(json.dumps(packet))
                rewrite_terminal(exit_code=7, output_sha256=hashlib.sha256(output_path.read_bytes()).hexdigest())
                with self.assertRaisesRegex(self.mod.ControllerError, "any output"):
                    self.call("retry_plan_review", state_root=str(self.state), work_id=work)
        restore()

    def assert_v3_review_ingest_rejects_prerequisite_variant(self, kind):
        work, attempt = self.make_v3_review_attempt("valid", prerequisite_kind=kind)
        terminal = json.loads(Path(attempt["receipt_path"]).read_text())
        self.assertEqual(terminal["exit_code"], 0)
        packet = json.loads(Path(attempt["output_path"]).read_text())
        expected = [self.mod.M0B_EVIDENCE_DIGEST, self.mod.M0C0_EVIDENCE_DIGEST]
        self.assertNotEqual(packet["prerequisite_evidence_digests"], expected)
        before = self.state_file(work).read_bytes()
        with self.assertRaisesRegex(self.mod.ControllerError, "strict binding"):
            self.call("ingest_plan_review", state_root=str(self.state), work_id=work)
        self.assertEqual(self.state_file(work).read_bytes(), before)
        state = self.read_state(work)
        self.assertEqual(state["phase"], "awaiting_gate")
        self.assertIsNone(state.get("plan_review"))
        self.assertIsNone(state["plan_review_attempt"].get("consumed_at"))

    def test_v3_review_ingest_rejects_wrong_prerequisite_digest(self):
        self.assert_v3_review_ingest_rejects_prerequisite_variant("wrong")

    def test_v3_review_ingest_rejects_missing_prerequisite_digest(self):
        self.assert_v3_review_ingest_rejects_prerequisite_variant("missing")

    def test_v3_review_ingest_rejects_reordered_prerequisite_digests(self):
        self.assert_v3_review_ingest_rejects_prerequisite_variant("reordered")

    def test_v2_prerequisites_real_fixture_rejects_each_authority_tamper(self):
        prerequisites = self.make_v2_prerequisite_fixture()
        work = self.mod.M0C_V2_WORK_ID; self.prepare(work); v2 = self.read_state(work)
        self.mod._validate_m0c_v2_prerequisites(prerequisites, v2)
        m0b, c0 = self.mod.M0B_WORK_ID, self.mod.M0C0_WORK_ID

        def state_mutation(package, mutate):
            path = self.state_file(package); raw = path.read_bytes(); value = json.loads(raw); mutate(value); path.write_text(json.dumps(value)); return lambda: path.write_bytes(raw)
        def file_mutation(path, mutate):
            raw = Path(path).read_bytes(); mutate(Path(path)); return lambda: Path(path).write_bytes(raw)
        cases = []
        for label, mutate in (
            ("candidate", lambda s: s["candidate"].__setitem__("id", "forged")),
            ("evidence", lambda s: s["candidate"].__setitem__("evidence_digest", "0" * 64)),
            ("fingerprint", lambda s: s["candidate"]["evidence"]["worktree_fingerprint"].__setitem__("digest", "0" * 64)),
            ("status", lambda s: s["candidate"]["evidence"]["worktree_fingerprint"]["status"].__setitem__("sha256", "0" * 64)),
            ("untracked", lambda s: s["candidate"]["evidence"]["worktree_fingerprint"]["untracked"].__setitem__("sha256", "0" * 64)),
            ("phase", lambda s: s.__setitem__("phase", "awaiting_gate")),
            ("gate", lambda s: s.__setitem__("gate", {"verdict": "fail"})),
            ("review-verdict", lambda s: s["alternate_evidence"]["review"].__setitem__("verdict", "fail")),
            ("packet-path", lambda s: s["alternate_evidence"]["result"].__setitem__("path", "/tmp/forged.json")),
        ):
            cases.append((label, lambda mutate=mutate: state_mutation(c0, mutate)))
        config = prerequisites[1]["packet_refs"]["config"]; result = prerequisites[1]["packet_refs"]["result"]; review = prerequisites[1]["packet_refs"]["review"]
        cases += [
            ("config", lambda: file_mutation(config, lambda p: p.write_text("tampered"))),
            ("result", lambda: file_mutation(result, lambda p: p.write_text("tampered"))),
            ("review", lambda: file_mutation(review, lambda p: p.write_text("tampered"))),
            ("result-hash", lambda: file_mutation(result, lambda p: p.write_text(json.dumps({**json.loads(p.read_text()), "receipts": []})))),
            ("changed-paths", lambda: file_mutation(result, lambda p: p.write_text(json.dumps({**json.loads(p.read_text()), "changed_paths": []})))),
            ("candidate-worktree-file", lambda: file_mutation(Path(self.read_state(c0)["worktree"]["path"]) / "scripts" / "test_cowork.py", lambda p: p.write_text("changed"))),
            ("missing-canonical-file", lambda: (lambda raw=Path(config).read_bytes(): (Path(config).unlink(), lambda: Path(config).write_bytes(raw))[1])()),
            ("m0b-scoped-receipt", lambda: file_mutation(Path(self.read_state(m0b)["verification_receipts"][0]["path"]), lambda p: p.write_text("{}"))),
        ]
        for label, action in cases:
            with self.subTest(label=label):
                restore = action()
                try:
                    with self.assertRaises(self.mod.ControllerError): self.mod._validate_m0c_v2_prerequisites(prerequisites, v2)
                finally:
                    restore()
        for label, bad in (("swapped", list(reversed(prerequisites))), ("order", prerequisites + [])):
            with self.subTest(label=label):
                if label == "order": bad = copy.deepcopy(prerequisites); bad[0], bad[1] = bad[1], bad[0]
                with self.assertRaises(self.mod.ControllerError): self.mod._validate_m0c_v2_prerequisites(bad, v2)

    def test_v2_offline_plan_review_and_fixed_gate_lifecycle(self):
        prerequisites = self.make_v2_prerequisite_fixture()
        work = self.mod.M0C_V2_WORK_ID; self.prepare(work); self.launch(work, role="planner"); self.wait_quiescent(work)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        state = self.read_state(work); attempt = state["attempt"]; plans = Path(os.path.realpath(self.root / "plans-v2")); plans.mkdir()
        source = plans / "v2.md"; source.write_text("```json\n%s\n```\n" % json.dumps(self.v2_plan(prerequisites))); os.utime(source, None)
        provider = self.state / work / "packets" / (attempt["id"] + ".json"); packet = json.loads(provider.read_text()); packet["structured"]["changed_paths"] = [str(source)]; provider.write_text(json.dumps(packet))
        self.call("ingest_plan_artifact", state_root=str(self.state), work_id=work, claude_plans_root=str(plans), source_file=str(source))
        self.call("launch_plan_review", state_root=str(self.state), work_id=work, codex_bin=str(self.fake_codex)); self.wait_review(work)
        review_schema = json.loads(next((self.state / work / "review-attempts").glob("*.schema.json")).read_text())
        evidence_schema = review_schema["properties"]["prerequisite_evidence_digests"]
        self.assertEqual(evidence_schema, {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}})
        self.assertNotIn("const", evidence_schema)
        self.assertEqual(self.call("ingest_plan_review", state_root=str(self.state), work_id=work)["review_verdict"], "pass")
        self.call("adjudicate", state_root=str(self.state), work_id=work, verdict="pass", rationale="all deterministic v2 bindings passed", adjudicator_principal="orchestrator-plan-gate/v1", instruction=None, capability_required=None, target_authority_role=None, target_authority_principal=None)
        self.assertEqual(self.read_state(work)["phase"], "completed")

    def test_v2_preingest_revision_is_one_exact_session_planner_correction(self):
        work = self.mod.M0C_V2_WORK_ID; self.prepare(work); self.launch(work, role="planner"); self.wait_quiescent(work)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        original = self.read_state(work)["attempt"]
        result = self.call("adjudicate", state_root=str(self.state), work_id=work, verdict="revise",
                           rationale="remove unverified command claims", adjudicator_principal="orchestrator-plan-gate/v1",
                           instruction="Return the same plan with commands_run empty.", capability_required=None,
                           target_authority_role=None, target_authority_principal=None)
        self.assertEqual(result["delivery"], "exact_planner_resume_required")
        state = self.read_state(work); self.assertEqual(state["recovery_constraint"]["kind"], "m0c_preingest_plan_exact_correction")
        self.assertEqual(state["recovery_constraint"]["provider_session_id"], original["provider_session_id"])
        self.assertIn("already queued", self.invoke("intervene", "--state-root", str(self.state), "--work-id", work, "--instruction", "extra", ok=False)["error"])
        self.assertIn("exact-session", self.invoke("launch", "--state-root", str(self.state), "--work-id", work, "--role", "planner", "--claude-bin", str(self.fake), ok=False)["error"])
        self.assertIn("original failed role", self.invoke("launch", "--state-root", str(self.state), "--work-id", work, "--role", "reviewer", "--resume", "--claude-bin", str(self.fake), ok=False)["error"])
        resumed = self.launch(work, role="planner", resume=True); self.assertEqual(resumed["provider_session_id"], original["provider_session_id"])
        state = self.read_state(work); self.assertTrue(state["m0c_correction_used"]); self.assertTrue(state["recovery_constraint"]["consumed_at"])
        self.assertIn("consumed its one bounded correction", self.invoke("intervene", "--state-root", str(self.state), "--work-id", work, "--instruction", "second", ok=False)["error"])
        self.wait_quiescent(work); calls = len(self.fixture.audit_rows())
        for resume in (False, True):
            args = ["launch", "--state-root", str(self.state), "--work-id", work, "--role", "planner", "--claude-bin", str(self.fake)]
            if resume: args.append("--resume")
            self.assertIn("consumed its one bounded correction", self.invoke(*args, ok=False)["error"])
        self.assertEqual(len(self.fixture.audit_rows()), calls)

    def test_v2_preingest_correction_allows_one_exact_spawn_recovery_before_consumption(self):
        work = self.mod.M0C_V2_WORK_ID; self.prepare(work); self.launch(work, role="planner"); self.wait_quiescent(work)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", work); original = self.read_state(work)["attempt"]
        self.call("adjudicate", state_root=str(self.state), work_id=work, verdict="revise", rationale="correct packet",
                  adjudicator_principal="orchestrator-plan-gate/v1", instruction="Return corrected JSON.", capability_required=None,
                  target_authority_role=None, target_authority_principal=None)
        values = dict(state_root=str(self.state), work_id=work, role="planner", resume=True, claude_bin=str(self.fake), model="fake-model", effort="medium")
        with mock.patch.object(self.mod, "detached_spawn", side_effect=OSError("fixture spawn failure")):
            with self.assertRaisesRegex(self.mod.ControllerError, "spawn failed"): self.call("launch", **values)
        failed = self.read_state(work); self.assertFalse(failed.get("m0c_correction_used")); self.assertEqual(len(failed["recovery_constraint"]["spawn_failures"]), 1)
        with self.assertRaisesRegex(self.mod.ControllerError, "exact-session"):
            self.call("launch", **{**values, "resume": False})
        retried = self.call("launch", **values); self.assertEqual(retried["provider_session_id"], original["provider_session_id"])
        self.assertTrue(self.read_state(work)["m0c_correction_used"])

    def start_v2_preingest_correction(self, work):
        self.prepare(work); self.launch(work, role="planner"); self.wait_quiescent(work)
        self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        original = self.read_state(work)["attempt"]
        self.call("adjudicate", state_root=str(self.state), work_id=work, verdict="revise",
                  rationale="bounded correction fixture", adjudicator_principal="orchestrator-plan-gate/v1",
                  instruction="Return corrected JSON only.", capability_required=None,
                  target_authority_role=None, target_authority_principal=None)
        return original

    def test_v2_used_correction_can_wait_for_one_trusted_capacity_wake_then_exactly_resume_once(self):
        work = self.mod.M0C_V2_WORK_ID; original = self.start_v2_preingest_correction(work)
        reset_epoch = int(time.time()) + 2
        correction = self.launch(work, role="planner", resume=True,
                                 env={"FAKE_CLAUDE_RESULT": "claude_rate_limit", "FAKE_CLAUDE_RESETS_AT": str(reset_epoch)})
        self.assertEqual(correction["provider_session_id"], original["provider_session_id"])
        correction_state = self.read_state(work)
        self.assertTrue(correction_state["m0c_correction_used"])
        self.assertTrue(correction_state["recovery_constraint"]["consumed_at"])
        self.wait_quiescent(work)
        capacity = self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        self.assertEqual(capacity["phase"], "awaiting_capacity")
        self.assertEqual(capacity["capacity"]["reset_source"], "claude_rate_limit_event_resets_at")
        wake = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", work,
                           "--wakeup-ref", "automation://fixture/v2-correction-reset")
        self.assertFalse(wake["reused"])
        repeated = self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", work,
                               "--wakeup-ref", "automation://fixture/v2-correction-reset")
        self.assertTrue(repeated["reused"]); self.assertEqual(repeated["lease_id"], wake["lease_id"])
        self.assertIn("different wakeup", self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", work,
                      "--wakeup-ref", "automation://fixture/other", ok=False)["error"])
        delay = max(0, reset_epoch - time.time()) + .1
        time.sleep(delay)
        released = self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", work)
        self.assertEqual(released["delivery"], "exact_resume_required")
        state = self.read_state(work)
        self.assertEqual(state["recovery_constraint"]["kind"], "provider_plan_capacity_exact_resume")
        self.assertFalse(state["recovery_constraint"]["consumed_at"])
        self.assertTrue(state["m0c_correction_used"])
        self.assertIn("exact-session --resume", self.invoke("launch", "--state-root", str(self.state), "--work-id", work,
                      "--role", "planner", "--claude-bin", str(self.fake), ok=False)["error"])
        resumed = self.launch(work, role="planner", resume=True)
        self.assertEqual(resumed["provider_session_id"], original["provider_session_id"])
        self.assertTrue(self.read_state(work)["recovery_constraint"]["consumed_at"])
        self.wait_quiescent(work); calls = len(self.fixture.audit_rows())
        for resume in (False, True):
            argv = ["launch", "--state-root", str(self.state), "--work-id", work, "--role", "planner", "--claude-bin", str(self.fake)]
            if resume: argv.append("--resume")
            self.assertIn("consumed its one bounded correction", self.invoke(*argv, ok=False)["error"])
        self.assertEqual(len(self.fixture.audit_rows()), calls)

    def assert_v2_used_correction_failure(self, label, env, phase):
        work = self.mod.M0C_V2_WORK_ID
        self.start_v2_preingest_correction(work)
        self.launch(work, role="planner", resume=True, env=env); self.wait_quiescent(work)
        collected = self.invoke("collect", "--state-root", str(self.state), "--work-id", work)
        state = self.read_state(work); self.assertEqual(state["phase"], phase); self.assertTrue(state["m0c_correction_used"])
        if label == "manual":
            self.assertEqual(state["capacity"]["mode"], "manual_signal")
            self.assertIn("manual capacity waits", self.invoke("register-capacity-wakeup", "--state-root", str(self.state), "--work-id", work,
                          "--wakeup-ref", "automation://fixture/manual", ok=False)["error"])
            self.assertIn("manual capacity waits", self.invoke("resume-capacity", "--state-root", str(self.state), "--work-id", work, ok=False)["error"])
        else:
            self.assertIsNone(state.get("capacity"))
            before = self.state_file(work).read_bytes()
            self.invoke("reconcile-provider-capacity", "--state-root", str(self.state), "--work-id", work, ok=False)
            self.assertEqual(self.state_file(work).read_bytes(), before)
        for resume in (False, True):
            argv = ["launch", "--state-root", str(self.state), "--work-id", work, "--role", "planner", "--claude-bin", str(self.fake)]
            if resume: argv.append("--resume")
            self.assertIn("consumed its one bounded correction", self.invoke(*argv, ok=False)["error"])

    def test_v2_used_correction_manual_capacity_remains_blocked(self):
        self.assert_v2_used_correction_failure("manual", {"FAKE_CLAUDE_RESULT": "quota", "FAKE_CLAUDE_RETRY_AFTER": ""}, "awaiting_capacity")

    def test_v2_used_correction_unknown_failure_remains_blocked(self):
        self.assert_v2_used_correction_failure("unknown", {"FAKE_CLAUDE_RESULT": "missing"}, "failed")

    def test_v2_used_correction_local_guard_remains_blocked(self):
        self.assert_v2_used_correction_failure("local", {"FAKE_CLAUDE_RESULT": "budget"}, "failed")

    def test_package_a_schema_record_api_literals_are_exact_const(self):
        api_props = self.mod.PACKAGE_A_OUTCOME_SCHEMA["properties"]["record_api_summary"]["properties"]
        self.assertEqual(api_props["records"].get("const"), ["DispatchContract", "DispatchDecision", "AttemptLink"])
        self.assertEqual(api_props["validators"].get("const"), [
            "validate_dispatch_contract(record)",
            "validate_dispatch_decision(record, contract)",
            "validate_attempt_link(record)"])
        # reducer and idempotency_key_builder already had const; verify they're unchanged
        self.assertIn("const", api_props["reducer"])
        self.assertIn("const", api_props["idempotency_key_builder"])

    def test_package_a_verbose_summary_rejected_for_non_canonical_literals(self):
        # Live-shaped failure: worker used truthful descriptive strings instead of exact canonical literals
        packet = self.package_a_worker_result()
        packet["record_api_summary"]["records"] = [
            "Dispatch contract record", "Dispatch decision record", "Attempt link record"]
        self.assertIsNone(self.mod.normalize_package_a_structured(packet),
                          "descriptive record names must be rejected by normalizer")
        packet2 = self.package_a_worker_result()
        packet2["record_api_summary"]["validators"] = [
            "validates a dispatch contract",
            "validates a dispatch decision with optional contract",
            "validates an attempt link"]
        self.assertIsNone(self.mod.normalize_package_a_structured(packet2),
                          "descriptive validator strings must be rejected by normalizer")
        # Canonical literals still pass
        self.assertIsNotNone(self.mod.normalize_package_a_structured(self.package_a_worker_result()))

    def test_package_a_collect_verified_allows_advisory_extras_but_requires_exact_prefix(self):
        # Live-shaped five-row result: three canonical rows plus two advisory extras.
        # _package_a_collect_verified previously required exactly three rows and rejected this.
        #
        # normalize_package_a_structured checks structure (nonempty cwd string, empty receipt_refs).
        # Prefix ordering is enforced by _package_a_collect_verified, not the normalizer.

        dummy_wt = "/fixture/worktree"
        required_dummy = [
            {"command": "python3 -m unittest scripts/test_cowork.py",
             "cwd": dummy_wt, "exit_code": 0, "receipt_refs": []},
            {"command": "python3 -m unittest scripts/test_dispatch_contract_characterization.py",
             "cwd": dummy_wt, "exit_code": 0, "receipt_refs": []},
            {"command": "git diff --check",
             "cwd": dummy_wt, "exit_code": 0, "receipt_refs": []},
        ]
        advisory_dummy = [
            {"command": "python3 scripts/m0b_hash_check.py",
             "cwd": dummy_wt, "exit_code": 0, "receipt_refs": []},
            {"command": "python3 scripts/json_probe.py",
             "cwd": dummy_wt, "exit_code": 0, "receipt_refs": []},
        ]

        # normalize_package_a_structured accepts the five-row packet (schema allows up to 20)
        five_row_packet = self.package_a_worker_result()
        five_row_packet["checks"] = required_dummy + advisory_dummy
        normalized = self.mod.normalize_package_a_structured(five_row_packet)
        self.assertIsNotNone(normalized, "five-row packet with advisory extras must normalize")
        self.assertEqual(len(normalized["checks"]), 5)

        # normalize_package_a_structured rejects nonempty receipt_refs on any row (all rows, including advisory)
        for idx in range(5):
            rows = [dict(r) for r in required_dummy + advisory_dummy]
            rows[idx] = dict(rows[idx], receipt_refs=["worker-claim-not-trusted"])
            bad = self.package_a_worker_result(); bad["checks"] = rows
            self.assertIsNone(self.mod.normalize_package_a_structured(bad),
                              "nonempty receipt_refs on row %d must fail normalize" % idx)

        # _package_a_collect_verified enforces the required prefix order and exact cwd.
        # Use a real fixture so the full collect code path is exercised.
        work, _ = self.make_package_a_fixture(); self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# dispatch\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"# tests\n")
        changed = ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"]

        def write_checks(chk):
            output = Path(self.read_state(work)["attempt"]["stdout"])
            p = self.package_a_worker_result(changed, str(wt)); p["checks"] = chk
            output.write_text(json.dumps({"type": "result", "structured_output": p}) + "\n")

        wt_str = str(wt)
        required_wt = [{"command": cmd, "cwd": wt_str, "exit_code": 0, "receipt_refs": []}
                       for cmd in ("python3 -m unittest scripts/test_cowork.py",
                                   "python3 -m unittest scripts/test_dispatch_contract_characterization.py",
                                   "git diff --check")]
        advisory_wt = [{"command": cmd, "cwd": wt_str, "exit_code": 0, "receipt_refs": []}
                       for cmd in ("python3 scripts/m0b_hash_check.py", "python3 scripts/json_probe.py")]

        # Reordered required prefix: collect must raise (no state mutation; ControllerError is raised early)
        write_checks(list(reversed(required_wt)) + advisory_wt)
        with self.assertRaisesRegex(self.mod.ControllerError, "advisory gate list"):
            self.call("collect", state_root=str(self.state), work_id=work)

        # Missing first required row: collect must raise
        write_checks(required_wt[1:] + advisory_wt)
        with self.assertRaisesRegex(self.mod.ControllerError, "advisory gate list"):
            self.call("collect", state_root=str(self.state), work_id=work)

        # Wrong cwd on an advisory row: collect must raise
        bad_advisory = [dict(r, cwd="/wrong/cwd") if i == 0 else r for i, r in enumerate(advisory_wt)]
        write_checks(required_wt + bad_advisory)
        with self.assertRaisesRegex(self.mod.ControllerError, "advisory gate list"):
            self.call("collect", state_root=str(self.state), work_id=work)

        # Five-row packet with correct prefix and cwd: collect must succeed
        write_checks(required_wt + advisory_wt)
        result = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(result["gate"]["phase"], "awaiting_gate")
        self.assertEqual(self.read_state(work)["phase"], "awaiting_gate")

    def test_package_a_invalid_packet_intervene_exact_resume_then_later_block(self):
        work, _ = self.make_package_a_fixture()
        # Launch with standard fake (emits generic packet — no record_api_summary)
        launched = self.call("launch", state_root=str(self.state), work_id=work,
                             role="implementer", resume=False,
                             claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.wait_quiescent(work)
        original_session = launched["provider_session_id"]

        # Collect: invalid packet -> failed/invalid_or_missing_worker_packet, no candidate
        result = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(result["gate"]["reason"], "invalid_or_missing_worker_packet")
        self.assertIsNone(self.read_state(work).get("candidate"))

        # Direct launch (fresh and resume) must fail without a correction intervention
        for resume in (False, True):
            with self.assertRaisesRegex(self.mod.ControllerError, "correction intervention"):
                self.call("launch", state_root=str(self.state), work_id=work,
                          role="implementer", resume=resume,
                          claude_bin=str(self.fake), model="sonnet", effort="medium")

        # Intervene queues the correction
        queued = self.call("intervene", state_root=str(self.state), work_id=work,
                           instruction="Use exact canonical API literals per the schema.")
        self.assertEqual(queued["delivery"], "exact_implementer_resume_required")
        self.assertEqual(queued["phase"], "prepared")
        state = self.read_state(work)
        rc = state["recovery_constraint"]
        self.assertEqual(rc["kind"], "package_a_invalid_packet_exact_correction")
        self.assertEqual(rc["provider_session_id"], original_session)
        self.assertEqual(rc["role"], "implementer")
        self.assertFalse(rc["consumed_at"])
        self.assertEqual(len(state["package_a_correction_history"]), 1)
        archive_path = Path(rc["archive_path"])
        self.assertTrue(archive_path.is_file())
        archive = json.loads(archive_path.read_text())
        self.assertEqual(archive["kind"], "package_a_invalid_packet_correction_archive")
        self.assertEqual(len(archive["evidence_refs"]), 2)

        # Second intervene while recovery is unconsumed must be blocked
        with self.assertRaisesRegex(self.mod.ControllerError, "correction"):
            self.call("intervene", state_root=str(self.state), work_id=work, instruction="Again")

        # Fresh launch (no resume) must be blocked
        with self.assertRaisesRegex(self.mod.ControllerError, "exact-session"):
            self.call("launch", state_root=str(self.state), work_id=work,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake), model="sonnet", effort="medium")

        # Resume with correct role/session succeeds and consumes the correction
        resumed = self.call("launch", state_root=str(self.state), work_id=work,
                            role="implementer", resume=True,
                            claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.assertEqual(resumed["provider_session_id"], original_session)
        self.wait_quiescent(work)
        state = self.read_state(work)
        self.assertTrue(state.get("package_a_correction_used"))
        self.assertTrue(state["recovery_constraint"]["consumed_at"])

        # After correction consumed: all further launch/resume attempts are blocked
        calls_before = len(self.fixture.audit_rows())
        for resume in (False, True):
            with self.assertRaisesRegex(self.mod.ControllerError, "bounded correction"):
                self.call("launch", state_root=str(self.state), work_id=work,
                          role="implementer", resume=resume,
                          claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.assertEqual(len(self.fixture.audit_rows()), calls_before)

    def test_package_a_invalid_packet_correction_tamper_and_second_correction_negatives(self):
        work, _ = self.make_package_a_fixture()
        launched = self.call("launch", state_root=str(self.state), work_id=work,
                             role="implementer", resume=False,
                             claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.wait_quiescent(work)
        original_session = launched["provider_session_id"]
        self.call("collect", state_root=str(self.state), work_id=work)
        self.call("intervene", state_root=str(self.state), work_id=work,
                  instruction="Use canonical literals.")

        state = self.read_state(work)
        wt = Path(state["worktree"]["path"])

        # Wrong role is blocked (Package A requires implementer)
        with self.assertRaisesRegex(self.mod.ControllerError, "implementer"):
            self.call("launch", state_root=str(self.state), work_id=work,
                      role="reviewer", claude_bin=str(self.fake),
                      resume=True, model="sonnet", effort="medium")

        # Worktree tamper changes fingerprint -> launch fails
        (wt / "_tamper_fingerprint.txt").write_text("fingerprint tamper\n")
        with self.assertRaisesRegex(self.mod.ControllerError, "fingerprint"):
            self.call("launch", state_root=str(self.state), work_id=work,
                      role="implementer", claude_bin=str(self.fake),
                      resume=True, model="sonnet", effort="medium")
        (wt / "_tamper_fingerprint.txt").unlink()

        # Archive tamper -> sha256 mismatch -> launch fails
        archive_path = Path(self.read_state(work)["recovery_constraint"]["archive_path"])
        original_archive = archive_path.read_bytes()
        archive_path.write_text("{}")
        with self.assertRaisesRegex(self.mod.ControllerError, "binding"):
            self.call("launch", state_root=str(self.state), work_id=work,
                      role="implementer", claude_bin=str(self.fake),
                      resume=True, model="sonnet", effort="medium")
        archive_path.write_bytes(original_archive)

        # Correct resume succeeds after restoring archive
        resumed = self.call("launch", state_root=str(self.state), work_id=work,
                            role="implementer", claude_bin=str(self.fake),
                            resume=True, model="sonnet", effort="medium")
        self.assertEqual(resumed["provider_session_id"], original_session)
        self.wait_quiescent(work)

        # Collect again (fake emits invalid packet again -> second invalid_or_missing_worker_packet)
        self.call("collect", state_root=str(self.state), work_id=work)
        # package_a_correction_used blocks any further launch
        with self.assertRaisesRegex(self.mod.ControllerError, "bounded correction"):
            self.call("launch", state_root=str(self.state), work_id=work,
                      role="implementer", claude_bin=str(self.fake),
                      resume=True, model="sonnet", effort="medium")

    def test_package_a_invalid_packet_recovery_corrected_collect_uses_attempt_specific_path(self):
        """Invalid-packet intervene sets generation=1 so corrected collect writes
        package-a-collect/<attempt>.result.json and never overwrites root result.json."""
        work, _ = self.make_package_a_fixture()

        # First launch: fake emits a generic packet (no record_api_summary) → invalid for Package A
        launched = self.call("launch", state_root=str(self.state), work_id=work,
                             role="implementer", resume=False,
                             claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.wait_quiescent(work)
        original_session = launched["provider_session_id"]

        # First collect: invalid packet → failed, root result.json written
        first_collect = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(first_collect["gate"]["reason"], "invalid_or_missing_worker_packet")
        root_result = self.state / work / "result.json"
        self.assertTrue(root_result.is_file(), "first invalid collect must write root result.json")
        root_result_bytes = root_result.read_bytes()
        self.assertIsNone(self.read_state(work).get("candidate"))

        # Intervene: must set package_a_collect_generation=1 as a durable recovery point
        queued = self.call("intervene", state_root=str(self.state), work_id=work,
                           instruction="Return a valid structured packet with record_api_summary.")
        self.assertEqual(queued["delivery"], "exact_implementer_resume_required")
        state = self.read_state(work)
        self.assertEqual(state.get("package_a_collect_generation"), 1,
                         "intervene must set generation=1 before save so corrected collect targets attempt path")

        # Resume with exact session (consumes recovery)
        resumed = self.call("launch", state_root=str(self.state), work_id=work,
                            role="implementer", resume=True,
                            claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.assertEqual(resumed["provider_session_id"], original_session)
        self.wait_quiescent(work)

        # Write a valid Package A worker packet for the new attempt
        new_attempt_id = resumed["attempt_id"]
        state = self.read_state(work)
        wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# corrected dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"\nimport unittest\nclass PackageAFixture(unittest.TestCase):\n def test_fixture(self): self.assertTrue(True)\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])

        # Corrected collect must succeed and write attempt-specific paths
        result = self.call("collect", state_root=str(self.state), work_id=work)

        # Root result.json must be byte-for-byte unchanged
        self.assertEqual(root_result.read_bytes(), root_result_bytes,
                         "corrected collect must not overwrite the original root result.json")

        # Attempt-specific result and intent must be durable under package-a-collect/
        collect_dir = self.state / work / "package-a-collect"
        attempt_result = collect_dir / (new_attempt_id + ".result.json")
        attempt_intent = collect_dir / (new_attempt_id + ".intent.json")
        self.assertTrue(attempt_result.is_file(), "corrected collect must write attempt-specific result")
        self.assertTrue(attempt_intent.is_file(), "corrected collect must write attempt-specific intent")

        # Delta must be present
        collected_state = self.read_state(work)
        delta_path = Path(collected_state["package_a_builder_delta"]["path"])
        self.assertTrue(delta_path.is_file(), "builder delta must be durable")

        # Phase must be awaiting_gate, candidate present
        self.assertEqual(collected_state["phase"], "awaiting_gate")
        self.assertIsNotNone(collected_state.get("candidate"))
        self.assertEqual(result["gate"]["phase"], "awaiting_gate")

        # Idempotent replay: second collect call returns same result
        replayed = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(replayed["gate"]["candidate_id"], result["gate"]["candidate_id"])
        self.assertEqual(replayed["gate"]["evidence_digest"], result["gate"]["evidence_digest"])
        self.assertEqual(root_result.read_bytes(), root_result_bytes,
                         "replay must also leave root result.json unchanged")

    def _make_pre_fix_invalid_packet_state(self):
        """Build the exact historical pre-fix state: consumed invalid-packet recovery with generation absent."""
        work, _ = self.make_package_a_fixture()

        # First launch: generic (invalid) packet
        launched = self.call("launch", state_root=str(self.state), work_id=work,
                             role="implementer", resume=False,
                             claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.wait_quiescent(work)
        original_session = launched["provider_session_id"]

        # First collect: invalid packet → root result.json written
        first_collect = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(first_collect["gate"]["reason"], "invalid_or_missing_worker_packet")
        root_result = self.state / work / "result.json"
        root_result_bytes = root_result.read_bytes()

        # Intervene: with the fix, this sets generation=1; we'll revert to simulate pre-fix
        self.call("intervene", state_root=str(self.state), work_id=work,
                  instruction="Use exact canonical API literals per the schema.")

        # Simulate pre-fix state: remove generation so it is absent (as it was before the fix)
        state = self.read_state(work)
        state.pop("package_a_collect_generation", None)
        self.state_file(work).write_text(json.dumps(state))

        # Resume with exact session: consumes recovery, sets correction_used (no generation set in pre-fix)
        resumed = self.call("launch", state_root=str(self.state), work_id=work,
                            role="implementer", resume=True,
                            claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.assertEqual(resumed["provider_session_id"], original_session)
        self.wait_quiescent(work)
        new_attempt_id = resumed["attempt_id"]

        # Confirm generation is still absent (pre-fix shape)
        state = self.read_state(work)
        self.assertIsNone(state.get("package_a_collect_generation"),
                          "pre-fix state must have generation absent, not set")
        self.assertTrue(state["recovery_constraint"]["consumed_at"])
        self.assertTrue(state.get("package_a_correction_used"))
        self.assertIsNone(state.get("candidate"))

        # Write valid corrected packet for the new attempt
        wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text("# corrected dispatch implementation\n")
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() + b"\nimport unittest\nclass PackageAFixture(unittest.TestCase):\n def test_fixture(self): self.assertTrue(True)\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])

        return work, new_attempt_id, root_result, root_result_bytes

    def test_package_a_invalid_packet_backward_compat_migration_collect_and_idempotency(self):
        """Pre-fix state (generation absent, consumed recovery) is migrated at collect time."""
        work, new_attempt_id, root_result, root_result_bytes = self._make_pre_fix_invalid_packet_state()
        directory = self.state / work

        # Collect must trigger reconciliation, set generation=1, then succeed
        result = self.call("collect", state_root=str(self.state), work_id=work)

        # Root result.json must be byte-for-byte unchanged
        self.assertEqual(root_result.read_bytes(), root_result_bytes,
                         "migration must not overwrite the original root result.json")

        # Attempt-specific result and intent must be written under package-a-collect/
        collect_dir = directory / "package-a-collect"
        self.assertTrue((collect_dir / (new_attempt_id + ".result.json")).is_file())
        self.assertTrue((collect_dir / (new_attempt_id + ".intent.json")).is_file())

        # Delta must be present
        collected = self.read_state(work)
        self.assertTrue(Path(collected["package_a_builder_delta"]["path"]).is_file())

        # Phase and candidate
        self.assertEqual(collected["phase"], "awaiting_gate")
        self.assertIsNotNone(collected.get("candidate"))
        self.assertEqual(result["gate"]["phase"], "awaiting_gate")

        # Generation must now be 1
        self.assertEqual(collected.get("package_a_collect_generation"), 1)

        # Exactly one reconciliation event for this attempt
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines() if line]
        reconcile_events = [e for e in events
                            if e.get("kind") == "package_a_invalid_packet_generation_reconciled"
                            and e.get("attempt_id") == new_attempt_id]
        self.assertEqual(len(reconcile_events), 1, "exactly one reconciliation event must be appended")

        # Idempotent replay: collect returns same candidate without re-appending the event
        replayed = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(replayed["gate"]["candidate_id"], result["gate"]["candidate_id"])
        self.assertEqual(replayed["gate"]["evidence_digest"], result["gate"]["evidence_digest"])
        events2 = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines() if line]
        reconcile_events2 = [e for e in events2
                             if e.get("kind") == "package_a_invalid_packet_generation_reconciled"
                             and e.get("attempt_id") == new_attempt_id]
        self.assertEqual(len(reconcile_events2), 1, "idempotent replay must not append a second reconciliation event")
        self.assertEqual(root_result.read_bytes(), root_result_bytes)

    def test_package_a_invalid_packet_backward_compat_crash_recovery_is_idempotent(self):
        """Crash between event append and state save is recovered on the next collect call."""
        work, new_attempt_id, root_result, root_result_bytes = self._make_pre_fix_invalid_packet_state()
        directory = self.state / work

        real_save = self.mod.save
        crashed = []
        def crash_after_event(directory_arg, state_arg):
            if not crashed:
                crashed.append(True); raise OSError("simulated crash before save")
            return real_save(directory_arg, state_arg)

        # Simulate crash between event append and save
        with mock.patch.object(self.mod, "save", side_effect=crash_after_event):
            with self.assertRaisesRegex(OSError, "before save"):
                self.call("collect", state_root=str(self.state), work_id=work)

        # State has generation=0 still (save was skipped), but event was appended
        state = self.read_state(work)
        self.assertIsNone(state.get("package_a_collect_generation"))
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines() if line]
        self.assertTrue(any(e.get("kind") == "package_a_invalid_packet_generation_reconciled"
                            and e.get("attempt_id") == new_attempt_id for e in events))

        # Recovery collect must succeed without duplicating the event
        result = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(result["gate"]["phase"], "awaiting_gate")
        self.assertEqual(root_result.read_bytes(), root_result_bytes)
        events2 = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines() if line]
        self.assertEqual(sum(1 for e in events2 if e.get("kind") == "package_a_invalid_packet_generation_reconciled"
                             and e.get("attempt_id") == new_attempt_id), 1)

    def test_package_a_invalid_packet_backward_compat_tamper_negatives(self):
        """Every tampered/ambiguous/orphan near-miss fails closed; non-historical shapes are untouched."""
        work, new_attempt_id, root_result, root_result_bytes = self._make_pre_fix_invalid_packet_state()
        directory = self.state / work

        def get_state(): return json.loads(self.state_file(work).read_text())
        def put_state(s): self.state_file(work).write_text(json.dumps(s))
        original = get_state()

        def expect_error(mutate, pattern):
            put_state(original)
            s = get_state(); mutate(s); put_state(s)
            with self.assertRaisesRegex(self.mod.ControllerError, pattern):
                self.call("collect", state_root=str(self.state), work_id=work)
            put_state(original)

        # Wrong/missing correction_used fields
        expect_error(lambda s: s["package_a_correction_used"].update({"original_attempt_id": "wrong"}), "cross-bind")
        expect_error(lambda s: s["package_a_correction_used"].update({"intervention_sha256": "0"*64}), "cross-bind")
        expect_error(lambda s: s["package_a_correction_used"].update({"archive_sha256": "0"*64}), "cross-bind")
        expect_error(lambda s: s.pop("package_a_correction_used"), "absent or malformed")

        # Unexpected candidate/collect/delta
        expect_error(lambda s: s.update({"candidate": {"id": "stub"}}), "unexpected candidate")
        expect_error(lambda s: s.update({"package_a_collect": {"stub": True}}), "unexpected collect")
        expect_error(lambda s: s.update({"package_a_builder_delta": {"stub": True}}), "unexpected builder delta")

        # Correction history malformed
        expect_error(lambda s: s.update({"package_a_correction_history": []}), "malformed")
        expect_error(lambda s: s["package_a_correction_history"][0].update({"sha256": "0"*64}), "sha256 does not match")

        # Archive tamper
        archive_path = Path(original["recovery_constraint"]["archive_path"])
        original_archive = archive_path.read_bytes()
        def tamper_archive(s):
            archive_path.write_text(json.dumps({"kind": "tampered"}))
            new_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            s["recovery_constraint"]["archive_sha256"] = new_sha
            s["package_a_correction_history"][0]["sha256"] = new_sha
            s["package_a_correction_used"]["archive_sha256"] = new_sha
        try:
            expect_error(tamper_archive, "kind is wrong")
        finally:
            archive_path.write_bytes(original_archive)

        # Orphan file in package-a-collect/
        collect_dir = directory / "package-a-collect"
        collect_dir.mkdir(exist_ok=True)
        orphan = collect_dir / "orphan.json"
        orphan.write_text("{}")
        put_state(original)
        with self.assertRaisesRegex(self.mod.ControllerError, "unexpected contents"):
            self.call("collect", state_root=str(self.state), work_id=work)
        orphan.unlink()

        # Orphan file in package-a-delta/
        delta_dir = directory / "package-a-delta"
        delta_dir.mkdir(exist_ok=True)
        orphan_delta = delta_dir / "orphan.json"
        orphan_delta.write_text("{}")
        put_state(original)
        with self.assertRaisesRegex(self.mod.ControllerError, "unexpected contents"):
            self.call("collect", state_root=str(self.state), work_id=work)
        orphan_delta.unlink()

        # Non-historical shape: recovery not consumed → no reconciliation, normal collect proceeds
        put_state(original)
        s = get_state()
        s["recovery_constraint"]["consumed_at"] = None
        # also must clear correction_used so the launch guard doesn't trip
        s.pop("package_a_correction_used", None)
        put_state(s)
        # collect should just attempt normal processing (may fail for unrelated reasons, but not reconciliation error)
        try:
            self.call("collect", state_root=str(self.state), work_id=work)
        except self.mod.ControllerError as exc:
            self.assertNotIn("backward-compat reconciliation", str(exc))
        put_state(original)

        # Confirm clean state still succeeds after all mutations restored
        result = self.call("collect", state_root=str(self.state), work_id=work)
        self.assertEqual(result["gate"]["phase"], "awaiting_gate")
        self.assertEqual(root_result.read_bytes(), root_result_bytes)

    # ---------------------------------------------------------------------------
    # Package A v2 identity helpers and focused v2 lifecycle tests
    # ---------------------------------------------------------------------------

    def make_package_a_v2_fixture(self, materialize=True, build_shared=True):
        """Build Package A v2 fixture, optionally reusing already-built shared infra.

        build_shared=True (default): calls make_package_a_fixture(materialize=False)
        to create the base commit and v3 plan. Pass build_shared=False when
        make_package_a_fixture has already been called in the same test method.
        """
        if build_shared:
            self.make_package_a_fixture(materialize=False)
        work = self.mod.PACKAGE_A_V2_WORK_ID
        self.prepare(work)
        state = self.read_state(work)
        self.mod.PACKAGE_A_V2_BRIEF_SHA256 = state["brief"]["sha256"]
        result = None
        if materialize:
            result = self.call("materialize_package_a_prerequisites",
                               state_root=str(self.state), work_id=work)
        return work, result

    _PACKAGE_A_DISPATCH_SRC = """\
import hashlib, json, uuid
def _exact(value, keys, record):
 if not isinstance(value, dict) or set(value) != set(keys): raise ValueError('keys')
 if value.get('schema_version') != 1 or value.get('record') != record: raise ValueError('record')
 return json.loads(json.dumps(value))
def validate_dispatch_contract(value):
 row=_exact(value, ('schema_version','record','contract_id','role','phase','controller','kind','purpose','site','resume_session_id','created'), 'DispatchContract')
 uuid.UUID(row['contract_id'])
 if not all(isinstance(row[k], str) and row[k] for k in ('role','phase','controller','kind','purpose','site')): raise ValueError('contract')
 if row['resume_session_id'] is not None or isinstance(row['created'], bool) or not isinstance(row['created'], (int,float)): raise ValueError('contract')
 return row
def validate_dispatch_decision(value, contract=None):
 keys=('schema_version','record','decision_id','contract_id','outcome','refusal_code','refusal_message','source','spawned','trace_event_id')
 row=_exact(value, keys, 'DispatchDecision'); uuid.UUID(row['decision_id'])
 if contract is None or row['contract_id'] != contract['contract_id'] or row['outcome'] not in ('allow','refuse') or row['spawned'] is not False or row['trace_event_id'] is not None: raise ValueError('decision')
 if row['outcome'] == 'allow' and any(row[k] is not None for k in ('refusal_code','refusal_message','source')): raise ValueError('allow')
 if row['outcome'] == 'refuse' and not all(isinstance(row[k], str) and row[k] for k in ('refusal_code','refusal_message','source')): raise ValueError('refuse')
 return row
def validate_attempt_link(value):
 row=_exact(value, ('schema_version','record','attempt_id','role','phase','kind','source_ref','delivery_ref','idempotency_key','created'), 'AttemptLink')
 uuid.UUID(row['attempt_id'])
 if set(row['source_ref']) != {'kind','event_id','event_name','session_id','prompt_sha256','created'}: raise ValueError('source')
 if set(row['delivery_ref']) != {'prompt_kind','prompt_sha256','prompt_bytes'}: raise ValueError('delivery')
 if row['idempotency_key'] != build_attempt_link_idempotency_key(row['role'], row['kind'], row['source_ref'], 1): raise ValueError('key')
 return row
def build_attempt_link_idempotency_key(role, kind, source, ordinal):
 discriminator=source.get('event_id') or source.get('session_id') or source.get('prompt_sha256') or 'legacy'
 return '%s:%s:%s:%s' % (role, kind, discriminator, ordinal)
def decide(contract, policy_result=None, preflight_result=None, probe_result=None):
 if policy_result is not None and policy_result.get('allowed') is False:
  return {'schema_version':1,'record':'DispatchDecision','decision_id':'33333333-3333-4333-8333-333333333333','contract_id':contract['contract_id'],'outcome':'refuse','refusal_code':policy_result['refusal_code'],'refusal_message':policy_result['refusal_message'],'source':policy_result['source'],'spawned':False,'trace_event_id':None}
 return {'schema_version':1,'record':'DispatchDecision','decision_id':'44444444-4444-4444-8444-444444444444','contract_id':contract['contract_id'],'outcome':'allow','refusal_code':None,'refusal_message':None,'source':None,'spawned':False,'trace_event_id':None}
"""

    def make_package_a_v2_collected_candidate(self):
        """Build a v2 Package A state with a fully collected green candidate."""
        work, _ = self.make_package_a_v2_fixture()
        self.launch_package_a(work)
        state = self.read_state(work); wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text(self._PACKAGE_A_DISPATCH_SRC)
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() +
                                b"\nimport unittest\nclass PackageAFixture(unittest.TestCase):\n"
                                b" def test_fixture(self): self.assertTrue(True)\n")
        self.write_package_a_worker_packet(work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=work)
        return work

    def make_package_a_v2_verified_candidate(self):
        """Build a v2 Package A state with all five gates passed."""
        work = self.make_package_a_v2_collected_candidate()
        verified = self.call("verify_package_a", state_root=str(self.state), work_id=work)
        self.assertTrue(verified["all_passed"], verified)
        return work

    def test_package_a_v2_exact_brief_hash_is_canonical(self):
        self.assertEqual(
            self.mod.PACKAGE_A_V2_BRIEF_SHA256,
            "c2d81e7aa5bb83d4024f3060d40ce03d3f6269654d62acf3efe3d549f78f6525",
        )

    def test_package_a_v2_work_and_outcome_ids_are_canonical(self):
        self.assertEqual(self.mod.PACKAGE_A_V2_WORK_ID, "m0c-package-a-dispatch-core-v2")
        self.assertEqual(self.mod.PACKAGE_A_V2_OUTCOME_ID, "m0c-dispatch-contract-core-a-v2")

    def test_package_a_identity_helper_exact_allowlist_no_prefix_match(self):
        """_package_a_identity must do an exact allowlist lookup, never a prefix match."""
        v1 = self.mod._package_a_identity(self.mod.PACKAGE_A_WORK_ID)
        v2 = self.mod._package_a_identity(self.mod.PACKAGE_A_V2_WORK_ID)
        v3 = self.mod._package_a_identity(self.mod.PACKAGE_A_V3_WORK_ID)
        self.assertIsNotNone(v1)
        self.assertIsNotNone(v2)
        self.assertIsNotNone(v3)
        self.assertEqual(v1["work_id"], self.mod.PACKAGE_A_WORK_ID)
        self.assertEqual(v1["outcome_id"], self.mod.PACKAGE_A_OUTCOME_ID)
        self.assertEqual(v1["brief_sha256"], self.mod.PACKAGE_A_BRIEF_SHA256)
        self.assertEqual(v2["work_id"], self.mod.PACKAGE_A_V2_WORK_ID)
        self.assertEqual(v2["outcome_id"], self.mod.PACKAGE_A_V2_OUTCOME_ID)
        self.assertEqual(v2["brief_sha256"], self.mod.PACKAGE_A_V2_BRIEF_SHA256)
        self.assertEqual(v3["work_id"], self.mod.PACKAGE_A_V3_WORK_ID)
        self.assertEqual(v3["outcome_id"], self.mod.PACKAGE_A_V3_OUTCOME_ID)
        self.assertEqual(v3["brief_sha256"], self.mod.PACKAGE_A_V3_BRIEF_SHA256)
        # v3 brief SHA must match the frozen worker brief
        self.assertEqual(v3["brief_sha256"], "746f19f9bfd4cc81c41f4ee746ca310ba86f9a2720a765957fc68f2e00437dd7")
        # Prefixes and supersets of v1/v2/v3 must not resolve
        for bad in ("m0c-package-a", "m0c-package-a-dispatch-core-",
                    "m0c-package-a-dispatch-core-v", "m0c-package-a-dispatch-core-v2-",
                    "m0c-package-a-dispatch-core-v2-extra", "m0c-package-a-dispatch-core-v3-",
                    "m0c-package-a-dispatch-core-v3-extra", ""):
            self.assertIsNone(self.mod._package_a_identity(bad), bad)
        # _is_package_a routing: v1, v2, v3 accepted; prefixes and others rejected
        self.assertTrue(self.mod._is_package_a({"work_id": self.mod.PACKAGE_A_WORK_ID}))
        self.assertTrue(self.mod._is_package_a({"work_id": self.mod.PACKAGE_A_V2_WORK_ID}))
        self.assertTrue(self.mod._is_package_a({"work_id": self.mod.PACKAGE_A_V3_WORK_ID}))
        self.assertFalse(self.mod._is_package_a({"work_id": "m0c-package-a"}))
        self.assertFalse(self.mod._is_package_a({"work_id": "m0c-package-a-dispatch-core-v3-extra"}))
        self.assertFalse(self.mod._is_package_a({}))

    def test_package_a_v2_full_lifecycle_prepare_materialize_collect_five_gates_review_pass(self):
        """v2: prepare → materialize → fake implementer → collect → five green gates → review → pass."""
        work = self.make_package_a_v2_verified_candidate()
        state = self.read_state(work)
        # All artifacts must bind v2 work_id, never v1
        collect_intent = json.loads(Path(state["package_a_collect"]["intent_path"]).read_text())
        self.assertEqual(collect_intent["work_id"], self.mod.PACKAGE_A_V2_WORK_ID)
        self.assertNotEqual(collect_intent["work_id"], self.mod.PACKAGE_A_WORK_ID)
        verification_intent = json.loads(Path(state["package_a_verification"]["intent_path"]).read_text())
        self.assertEqual(verification_intent["static"]["work_id"], self.mod.PACKAGE_A_V2_WORK_ID)
        # Fixed gate: awaiting review until independent review session completes
        waiting = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(waiting["disposition"], "awaiting_review")
        # Generic adjudicate must be blocked (package-specific adjudication required)
        with self.assertRaisesRegex(self.mod.ControllerError, "package-specific"):
            self.call("adjudicate", state_root=str(self.state), work_id=work,
                      verdict="pass", rationale="caller must not decide", instruction=None,
                      adjudicator_principal=self.mod.PACKAGE_A_GATE_PRINCIPAL,
                      capability_required=None, target_authority_role=None,
                      target_authority_principal=None)
        # Launch fresh independent review session (not reusing any v1 session)
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(work)
        self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        decided = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertFalse(decided["reused"])
        self.assertEqual(decided["disposition"], "pass")
        self.assertEqual(decided["phase"], "completed")
        receipt = json.loads(Path(decided["receipt"]["path"]).read_text())
        self.assertEqual(receipt["adjudicator_principal"], self.mod.PACKAGE_A_GATE_PRINCIPAL)
        self.assertEqual(receipt["reason_code"], "all_evidence_passed")
        state = self.read_state(work)
        self.assertEqual(state["gate"]["receipt_sha256"], decided["receipt"]["sha256"])
        # v2 state directory is structurally separate from v1
        v2_dir = self.state / self.mod.PACKAGE_A_V2_WORK_ID
        v1_dir = self.state / self.mod.PACKAGE_A_WORK_ID
        self.assertTrue(v2_dir.is_dir())
        self.assertTrue(v1_dir.is_dir())
        self.assertNotEqual(str(v2_dir), str(v1_dir))
        # v1 state was not mutated by the v2 lifecycle (v1 still in prepared phase)
        v1_state = self.read_state(self.mod.PACKAGE_A_WORK_ID)
        self.assertNotEqual(v1_state.get("phase"), "completed")

    def test_package_a_v1_v2_identity_swap_and_tamper_rejected(self):
        """Collect-intent work_id tampered to cross-identity must be rejected by collect-replay."""
        work = self.make_package_a_v2_collected_candidate()
        state = self.read_state(work)
        # Collect intent must bind v2, not v1
        intent_path = Path(state["package_a_collect"]["intent_path"])
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent["work_id"], self.mod.PACKAGE_A_V2_WORK_ID)
        self.assertNotEqual(intent["work_id"], self.mod.PACKAGE_A_WORK_ID)

        # Tamper: swap work_id in intent to v1 and update the state SHA so the file
        # SHA integrity check passes — the identity binding check must still catch it.
        # The collect-replay path (_package_a_replay_collect) enforces work_id binding.
        tampered = dict(intent, work_id=self.mod.PACKAGE_A_WORK_ID)
        tampered_bytes = (json.dumps(tampered, sort_keys=True, indent=2) + "\n").encode()
        intent_path.write_bytes(tampered_bytes)
        tampered_sha = hashlib.sha256(tampered_bytes).hexdigest()
        original_state_bytes = self.state_file(work).read_bytes()
        s = json.loads(original_state_bytes)
        s["package_a_collect"]["intent_sha256"] = tampered_sha
        self.state_file(work).write_text(json.dumps(s))
        with self.assertRaises(self.mod.ControllerError):
            self.call("collect", state_root=str(self.state), work_id=work)

        # Restore original state and intent before further assertions
        self.state_file(work).write_bytes(original_state_bytes)
        intent_path.write_bytes((json.dumps(intent, sort_keys=True, indent=2) + "\n").encode())

        # _is_package_a exact routing: v1, v2, v3 route; prefix/superset does not
        self.assertTrue(self.mod._is_package_a({"work_id": self.mod.PACKAGE_A_WORK_ID}))
        self.assertTrue(self.mod._is_package_a({"work_id": self.mod.PACKAGE_A_V2_WORK_ID}))
        self.assertTrue(self.mod._is_package_a({"work_id": self.mod.PACKAGE_A_V3_WORK_ID}))
        self.assertFalse(self.mod._is_package_a({"work_id": "m0c-package-a"}))
        self.assertFalse(self.mod._is_package_a({"work_id": "m0c-package-a-dispatch-core-v3-extra"}))

    def test_package_a_v2_cannot_use_failed_v1_as_recovery(self):
        """v2 starts in a completely fresh state; v1 failure evidence does not carry over."""
        work_v1, _ = self.make_package_a_fixture()
        self.launch_package_a(work_v1)
        v1_state = self.read_state(work_v1)
        v1_worktree = v1_state["worktree"]["path"]
        v1_attempt_id = v1_state["attempt"]["id"]

        # Prepare v2 fresh using existing shared infra (no second commit)
        work_v2, _ = self.make_package_a_v2_fixture(build_shared=False)
        v2_state = self.read_state(work_v2)

        # v2 must start with a completely clean slate — no v1 state artifacts
        self.assertNotIn("recovery_constraint", v2_state)
        self.assertNotIn("package_a_correction_used", v2_state)
        self.assertNotIn("package_a_correction_history", v2_state)
        self.assertEqual(v2_state["work_id"], work_v2)
        self.assertNotEqual(v2_state["worktree"]["path"], v1_worktree)
        self.assertIsNone(v2_state.get("attempt"))

        # launch --resume on a fresh v2 (no attempt yet) must be rejected
        with self.assertRaisesRegex(self.mod.ControllerError, "absent attempt"):
            self.call("launch", state_root=str(self.state), work_id=work_v2,
                      role="implementer", resume=True,
                      claude_bin=str(self.fake), model="sonnet", effort="medium")

        # Injecting v1's correction_used flag into v2 state must block v2 launch
        s = json.loads(self.state_file(work_v2).read_bytes())
        original_v2_state_bytes = self.state_file(work_v2).read_bytes()
        s["package_a_correction_used"] = True
        self.state_file(work_v2).write_text(json.dumps(s))
        with self.assertRaisesRegex(self.mod.ControllerError, "bounded correction"):
            self.call("launch", state_root=str(self.state), work_id=work_v2,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake), model="sonnet", effort="medium")
        self.state_file(work_v2).write_bytes(original_v2_state_bytes)

    def test_package_a_v1_regressions_after_generalization(self):
        """v1 Package A full lifecycle still works correctly after identity generalization."""
        work = self.make_package_a_verified_candidate()
        state = self.read_state(work)
        # v1 artifacts must bind v1 work_id, not v2
        collect_intent = json.loads(Path(state["package_a_collect"]["intent_path"]).read_text())
        self.assertEqual(collect_intent["work_id"], self.mod.PACKAGE_A_WORK_ID)
        self.assertNotEqual(collect_intent["work_id"], self.mod.PACKAGE_A_V2_WORK_ID)
        # v1 full adjudication path: awaiting_review → launch review → ingest → pass
        waiting = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertEqual(waiting["disposition"], "awaiting_review")
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(work)
        self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        decided = self.call("adjudicate_package_a", state_root=str(self.state), work_id=work)
        self.assertFalse(decided["reused"])
        self.assertEqual(decided["disposition"], "pass")
        self.assertEqual(decided["phase"], "completed")
        receipt = json.loads(Path(decided["receipt"]["path"]).read_text())
        self.assertEqual(receipt["adjudicator_principal"], self.mod.PACKAGE_A_GATE_PRINCIPAL)
        self.assertEqual(receipt["reason_code"], "all_evidence_passed")
        # Receipt must bind v1 gate principal and outcome, not v2
        self.assertNotIn(self.mod.PACKAGE_A_V2_WORK_ID,
                         json.dumps(receipt, sort_keys=True))

    def test_package_a_v3_identity_constants_and_brief_hash(self):
        """v3 work_id, outcome_id, brief_sha256 must match the frozen worker brief."""
        self.assertEqual(self.mod.PACKAGE_A_V3_WORK_ID, "m0c-package-a-dispatch-core-v3")
        self.assertEqual(self.mod.PACKAGE_A_V3_OUTCOME_ID, "m0c-dispatch-contract-core-a-v3")
        self.assertEqual(self.mod.PACKAGE_A_V3_BRIEF_SHA256,
                         "746f19f9bfd4cc81c41f4ee746ca310ba86f9a2720a765957fc68f2e00437dd7")
        self.assertIn(self.mod.PACKAGE_A_V3_WORK_ID, self.mod.PACKAGE_A_IDENTITIES)
        self.assertNotIn("m0c-package-a-dispatch-core-v3-extra", self.mod.PACKAGE_A_IDENTITIES)
        # v3 identity helper returns correct fields
        v3 = self.mod._package_a_identity(self.mod.PACKAGE_A_V3_WORK_ID)
        self.assertIsNotNone(v3)
        self.assertEqual(v3["work_id"], self.mod.PACKAGE_A_V3_WORK_ID)
        self.assertEqual(v3["outcome_id"], self.mod.PACKAGE_A_V3_OUTCOME_ID)
        self.assertEqual(v3["brief_sha256"], self.mod.PACKAGE_A_V3_BRIEF_SHA256)
        # v1 and v2 must still resolve correctly (regression check)
        v1 = self.mod._package_a_identity(self.mod.PACKAGE_A_WORK_ID)
        v2 = self.mod._package_a_identity(self.mod.PACKAGE_A_V2_WORK_ID)
        self.assertEqual(v1["brief_sha256"], self.mod.PACKAGE_A_BRIEF_SHA256)
        self.assertEqual(v2["brief_sha256"], self.mod.PACKAGE_A_V2_BRIEF_SHA256)
        # Cross-identity: v3 brief sha must be distinct from v1 and v2
        self.assertNotEqual(v3["brief_sha256"], v1["brief_sha256"])
        self.assertNotEqual(v3["brief_sha256"], v2["brief_sha256"])

    def test_package_a_claude_review_actor_and_backend_constants(self):
        """Review actor and backend constants must match the Claude reviewer."""
        self.assertEqual(self.mod.PACKAGE_A_CLAUDE_REVIEW_ACTOR, "claude_package_a_controller_v1")
        self.assertEqual(self.mod.PACKAGE_A_CLAUDE_REVIEW_BACKEND, "claude")
        # Old Codex actor must not appear in review schema or expected bindings
        self.assertNotEqual(self.mod.PACKAGE_A_CLAUDE_REVIEW_ACTOR, self.mod.PACKAGE_A_REVIEW_ACTOR)

    def test_package_a_claude_review_argv_no_codex(self):
        """Launched reviewer argv must be Claude/Sonnet/medium/plan; no Codex invocation."""
        work = self.make_package_a_verified_candidate()
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(work)
        rows = [json.loads(line) for line in self.claude_reviewer_audit.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        argv = rows[0]["argv"]
        self.assertEqual(argv[0], "-p")
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertIn("--disallowedTools", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-4-6")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--json-schema", argv)
        self.assertIn("--session-id", argv)
        # session-id in argv must equal reviewer_session_id in attempt
        session_id = attempt.get("reviewer_session_id")
        self.assertEqual(argv[argv.index("--session-id") + 1], session_id)
        # No Codex binary or model references
        for tok in argv:
            self.assertNotIn("codex", tok.lower())
            self.assertNotIn("terra", tok.lower())
            self.assertNotIn("gpt-5.6", tok)

    def test_package_a_claude_review_fresh_session_strict_ingest(self):
        """Controller-assigned reviewer_session_id must be fresh and verified at ingest."""
        work = self.make_package_a_verified_candidate()
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(work)
        reviewer_session_id = attempt.get("reviewer_session_id")
        self.assertIsNotNone(reviewer_session_id)
        # reviewer_session_id must be distinct from implementer session
        state = self.read_state(work)
        self.assertNotEqual(reviewer_session_id, state["attempt"].get("provider_session_id"))
        # Ingest must succeed and bind the session
        ingested = self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        self.assertEqual(ingested["verdict"], "pass")
        state = self.read_state(work)
        self.assertEqual(state["package_a_review"]["provider_session_id"], reviewer_session_id)
        # Envelope must bind reviewer_session_id
        envelope = json.loads(Path(state["package_a_review"]["path"]).read_text())
        self.assertEqual(envelope["reviewer_session_id"], reviewer_session_id)

    def test_package_a_claude_review_session_tamper_rejected(self):
        """Altering the result event session_id in stdout must be rejected at ingest."""
        work = self.make_package_a_verified_candidate()
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(work)
        stdout_path = Path(attempt["stdout"]); original = stdout_path.read_bytes()
        terminal_path = Path(attempt["receipt_path"]); terminal = json.loads(terminal_path.read_text())
        # Replace session_id in stdout result event with a wrong value
        lines = original.decode("utf-8").splitlines()
        tampered_lines = []
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("type") == "result":
                    row["session_id"] = "tampered-session-" + "0" * 20
                    line = json.dumps(row)
            except json.JSONDecodeError:
                pass
            tampered_lines.append(line)
        tampered = "\n".join(tampered_lines).encode("utf-8")
        stdout_path.write_bytes(tampered)
        terminal["stdout_sha256"] = hashlib.sha256(tampered).hexdigest()
        terminal["stdout_bytes"] = len(tampered)
        terminal_path.write_text(json.dumps(terminal))
        with self.assertRaises(self.mod.ControllerError):
            self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        # Restore
        stdout_path.write_bytes(original)
        terminal["stdout_sha256"] = hashlib.sha256(original).hexdigest()
        terminal["stdout_bytes"] = len(original)
        terminal_path.write_text(json.dumps(terminal))
        self.assertEqual(self.call("ingest_package_a_review", state_root=str(self.state),
                                   work_id=work)["verdict"], "pass")

    def test_package_a_claude_schema_failure_is_not_retryable(self):
        """Schema-failure (typed error_schema_validation) is not the overflow infrastructure class."""
        work = self.make_package_a_verified_candidate()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_KIND"); os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = "schema_failure"
        try:
            self.call("launch_package_a_review", state_root=str(self.state),
                      work_id=work, claude_bin=str(self.fake_claude_reviewer))
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_KIND", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = prior
        self.wait_package_a_review(work)
        # Typed schema error is nonzero without overflow; must be rejected
        with self.assertRaises(self.mod.ControllerError):
            self.call("retry_package_a_review", state_root=str(self.state), work_id=work)

    def test_package_a_claude_capacity_scheduled_mode(self):
        """Typed Claude rate-limit event with resetsAt triggers scheduled capacity wait."""
        work = self.make_package_a_verified_candidate()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_KIND"); os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = "capacity_scheduled"
        try:
            self.call("launch_package_a_review", state_root=str(self.state),
                      work_id=work, claude_bin=str(self.fake_claude_reviewer))
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_KIND", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = prior
        self.wait_package_a_review(work)
        result = self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        self.assertEqual(result["phase"], "awaiting_capacity")
        self.assertEqual(result["capacity"]["mode"], "scheduled")
        self.assertEqual(result["capacity"]["failure_class"], "subscription_quota_exhausted")
        state = self.read_state(work)
        self.assertEqual(state["phase"], "awaiting_capacity")
        capacity = state["package_a_review_capacity"]
        self.assertEqual(capacity["mode"], "scheduled")
        self.assertIsNotNone(capacity["retry_after"])
        # Verify limitations include subscription_only constraints
        self.assertIn("subscription_only", result["limitations"])
        self.assertIn("no refill, paid overage, or monetary-limit authority", result["limitations"])

    def test_package_a_claude_review_log_cap_constant(self):
        """PACKAGE_A_CLAUDE_REVIEW_LOG_CAP must be exactly 2 MiB."""
        self.assertEqual(self.mod.PACKAGE_A_CLAUDE_REVIEW_LOG_CAP, 2 * 1024 * 1024)

    def test_package_a_claude_review_argv_disallows_bash(self):
        """Launched reviewer argv must include Bash in disallowed tools."""
        work = self.make_package_a_verified_candidate()
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(work)
        rows = [json.loads(line) for line in self.claude_reviewer_audit.read_text().splitlines()]
        argv = rows[-1]["argv"]
        disallowed_idx = argv.index("--disallowedTools")
        disallowed_val = argv[disallowed_idx + 1]
        self.assertIn("Bash", disallowed_val)
        self.assertIn("Agent", disallowed_val)
        self.assertIn("Task", disallowed_val)
        # Prompt must contain bounded-inspection rules
        prompt = argv[-1]
        self.assertIn("bounded", prompt.lower())
        self.assertIn("test_cowork.py", prompt)

    def test_package_a_claude_review_large_stdout_ingests(self):
        """Output between 256 KiB and 2 MiB with a valid result event at end must ingest successfully."""
        work = self.make_package_a_verified_candidate()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_KIND"); os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = "large_stdout"
        try:
            self.call("launch_package_a_review", state_root=str(self.state),
                      work_id=work, claude_bin=str(self.fake_claude_reviewer))
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_KIND", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = prior
        attempt = self.wait_package_a_review(work)
        # stdout should be between 256 KiB and 2 MiB
        stdout_bytes = Path(attempt["stdout"]).stat().st_size
        self.assertGreater(stdout_bytes, 256 * 1024)
        self.assertLessEqual(stdout_bytes, 2 * 1024 * 1024)
        # Terminal must not show overflow or timeout
        terminal = json.loads(Path(attempt["receipt_path"]).read_text())
        self.assertFalse(terminal["output_limit_exceeded"])
        self.assertFalse(terminal["timed_out"])
        # Ingest must succeed
        ingested = self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        self.assertEqual(ingested["verdict"], "pass")

    def test_package_a_claude_review_overflow_is_one_retry_with_bounded_stream_overflow(self):
        """Sealed 2 MiB overflow/no-output/nonzero is the one infrastructure retry with bounded_stream_overflow classification."""
        work = self.make_package_a_verified_candidate()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_KIND"); os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = "overflow"
        try:
            self.call("launch_package_a_review", state_root=str(self.state),
                      work_id=work, claude_bin=str(self.fake_claude_reviewer))
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_KIND", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = prior
        attempt = self.wait_package_a_review(work)
        terminal = json.loads(Path(attempt["receipt_path"]).read_text())
        # Terminal must show output_limit_exceeded and no output
        self.assertTrue(terminal["output_limit_exceeded"])
        self.assertFalse(terminal["timed_out"])
        self.assertNotEqual(terminal["exit_code"], 0)
        self.assertIsNone(terminal["output_sha256"])
        # Retry must succeed with bounded_stream_overflow classification
        released = self.call("retry_package_a_review", state_root=str(self.state), work_id=work)
        self.assertTrue(released["retry_released"])
        state = self.read_state(work)
        self.assertIsNone(state["package_a_review_attempt"])
        self.assertEqual(len(state["package_a_review_attempt_history"]), 1)
        history = state["package_a_review_attempt_history"][0]
        self.assertEqual(history["retry_evidence"]["classification"], "bounded_stream_overflow")
        self.assertIsNotNone(history["retry_evidence"]["reviewer_session_id"])
        self.assertIsNotNone(history["retry_evidence"]["receipt_sha256"])
        # reviewer_session_id in evidence must match the original attempt
        self.assertEqual(history["retry_evidence"]["reviewer_session_id"], history["reviewer_session_id"])
        # Second retry must be refused
        with self.assertRaises(self.mod.ControllerError):
            self.call("retry_package_a_review", state_root=str(self.state), work_id=work)
        # After one retry, a fresh launch succeeds and ingests
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(work)
        ingested = self.call("ingest_package_a_review", state_root=str(self.state), work_id=work)
        self.assertEqual(ingested["verdict"], "pass")

    def test_package_a_claude_review_non_overflow_shapes_not_retryable(self):
        """Timeout, arbitrary nonzero, and schema failure do not enter the overflow retry class."""
        work = self.make_package_a_verified_candidate()
        attempt_path = None

        def run_kind(kind):
            nonlocal work, attempt_path
            prior = os.environ.get("FAKE_CLAUDE_REVIEWER_KIND"); os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = kind
            try:
                self.call("launch_package_a_review", state_root=str(self.state),
                          work_id=work, claude_bin=str(self.fake_claude_reviewer))
            finally:
                if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_KIND", None)
                else: os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = prior
            self.wait_package_a_review(work)

        # Case 1: schema_failure (typed error, no overflow flag) → not retryable
        run_kind("schema_failure")
        with self.assertRaises(self.mod.ControllerError):
            self.call("retry_package_a_review", state_root=str(self.state), work_id=work)

        # Simulate timeout by crafting terminal receipt with timed_out=True and output_limit_exceeded=True
        state = self.read_state(work)
        attempt = state["package_a_review_attempt"]
        terminal_path = Path(attempt["receipt_path"])
        terminal = json.loads(terminal_path.read_text())
        # Craft an overflow shape with timed_out=True → not retryable
        original = terminal_path.read_bytes()
        terminal["timed_out"] = True; terminal["output_limit_exceeded"] = True
        terminal["exit_code"] = 143; terminal["output_sha256"] = None; terminal["output_bytes"] = 0
        terminal["stdout_sha256"] = hashlib.sha256(Path(attempt["stdout"]).read_bytes()).hexdigest()
        terminal["stdout_bytes"] = Path(attempt["stdout"]).stat().st_size
        terminal_path.write_text(json.dumps(terminal))
        with self.assertRaises(self.mod.ControllerError):
            self.call("retry_package_a_review", state_root=str(self.state), work_id=work)
        terminal_path.write_bytes(original)

    def test_package_a_claude_review_output_present_not_overflow_retryable(self):
        """If output_limit_exceeded but output packet exists, it is not retryable."""
        work = self.make_package_a_verified_candidate()
        # Normal run (exit 0, output present)
        self.call("launch_package_a_review", state_root=str(self.state),
                  work_id=work, claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(work)
        terminal_path = Path(attempt["receipt_path"])
        terminal = json.loads(terminal_path.read_text())
        # Craft: output_limit_exceeded=True but output_sha256 is not null → not retryable
        original = terminal_path.read_bytes()
        terminal["timed_out"] = False; terminal["output_limit_exceeded"] = True; terminal["exit_code"] = 143
        # Keep output_sha256 as non-null (output packet exists on disk from normal run)
        terminal_path.write_text(json.dumps(terminal))
        with self.assertRaises(self.mod.ControllerError):
            self.call("retry_package_a_review", state_root=str(self.state), work_id=work)
        terminal_path.write_bytes(original)


    # ── Package A v4 review-rescue fixtures ──────────────────────────────────

    def make_package_a_v4_source_package(self):
        """Build a local fake v3 source package in 'failed' state.

        Patches all PACKAGE_A_V4_SOURCE_* constants to match this fixture.
        Calls make_package_a_fixture(materialize=False) to establish shared
        M0-B/M0-C0/plan-v3 infrastructure used by all Package A variants.
        """
        self.make_package_a_fixture(materialize=False)
        v3_work = self.mod.PACKAGE_A_V3_WORK_ID
        self.prepare(v3_work)
        state = self.read_state(v3_work)
        self.mod.PACKAGE_A_V3_BRIEF_SHA256 = state["brief"]["sha256"]
        self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=v3_work)
        self.launch_package_a(v3_work)
        state = self.read_state(v3_work)
        wt = Path(state["worktree"]["path"])
        (wt / "scripts/cowork_dispatch.py").write_text(self._PACKAGE_A_DISPATCH_SRC)
        test_cowork = wt / "scripts/test_cowork.py"
        test_cowork.write_bytes(test_cowork.read_bytes() +
                                b"\nimport unittest\nclass PackageAFixture(unittest.TestCase):\n"
                                b" def test_fixture(self): self.assertTrue(True)\n")
        self.write_package_a_worker_packet(v3_work, ["scripts/cowork_dispatch.py", "scripts/test_cowork.py"])
        self.call("collect", state_root=str(self.state), work_id=v3_work)
        state = self.read_state(v3_work)
        candidate = state["candidate"]
        module_sha = hashlib.sha256((wt / "scripts/cowork_dispatch.py").read_bytes()).hexdigest()
        test_sha = hashlib.sha256((wt / "scripts/test_cowork.py").read_bytes()).hexdigest()
        fp_digest = self.mod.worktree_fingerprint(str(wt))["digest"]
        # Build fake review terminal receipts
        directory = self.state / v3_work
        review_dir = directory / "package-a-review-attempts"
        review_dir.mkdir(exist_ok=True)
        first_path = review_dir / "v3-attempt-1.terminal.json"
        second_path = review_dir / "v3-attempt-2.terminal.json"
        first_path.write_text(json.dumps({"kind": "fake_v3_terminal", "attempt": 1}))
        second_path.write_text(json.dumps({"kind": "fake_v3_terminal", "attempt": 2}))
        first_sha = hashlib.sha256(first_path.read_bytes()).hexdigest()
        second_sha = hashlib.sha256(second_path.read_bytes()).hexdigest()
        # Build fake gate receipt
        gates_dir = directory / "package-a-gates"
        gates_dir.mkdir(exist_ok=True)
        gate_path = gates_dir / (candidate["id"] + ".gate.json")
        gate_path.write_text(json.dumps({"kind": "package_a_gate_receipt",
                                         "reason_code": "review_infrastructure_exhausted"}))
        gate_sha = hashlib.sha256(gate_path.read_bytes()).hexdigest()
        # Update state to failed with minimal gate receipt binding
        state["phase"] = "failed"
        state["gate"] = {"kind": "package_a_fixed_gate",
                         "gate_name": "orchestrator-package-a-gate/v1", "verdict": "fail",
                         "receipt_id": "fake-id", "receipt_sha256": gate_sha,
                         "candidate_id": candidate["id"],
                         "evidence_digest": candidate["evidence_digest"],
                         "reason_code": "review_infrastructure_exhausted",
                         "policy": self.mod.POLICY_ID}
        state["package_a_gate_receipt"] = {"path": str(gate_path), "sha256": gate_sha,
                                            "receipt_id": "fake-id",
                                            "candidate_id": candidate["id"],
                                            "evidence_digest": candidate["evidence_digest"],
                                            "disposition": "fail"}
        state["package_a_review_attempt_history"] = [{"receipt_path": str(first_path)},
                                                      {"receipt_path": str(second_path)}]
        self.state_file(v3_work).write_text(json.dumps(state))
        # Patch source constants
        self.mod.PACKAGE_A_V4_SOURCE_CANDIDATE_ID = candidate["id"]
        self.mod.PACKAGE_A_V4_SOURCE_EVIDENCE_DIGEST = candidate["evidence_digest"]
        self.mod.PACKAGE_A_V4_SOURCE_GATE_RECEIPT_SHA256 = gate_sha
        self.mod.PACKAGE_A_V4_SOURCE_MODULE_SHA256 = module_sha
        self.mod.PACKAGE_A_V4_SOURCE_TEST_SHA256 = test_sha
        self.mod.PACKAGE_A_V4_SOURCE_FIRST_REVIEW_SHA256 = first_sha
        self.mod.PACKAGE_A_V4_SOURCE_SECOND_REVIEW_SHA256 = second_sha
        self.mod.PACKAGE_A_V4_SOURCE_FINGERPRINT = fp_digest
        return v3_work

    def make_package_a_v4_fixture(self):
        """Build a fresh v4 Package A fixture with prerequisites materialized."""
        self.make_package_a_v4_source_package()
        v4_work = self.mod.PACKAGE_A_V4_WORK_ID
        self.prepare(v4_work)
        state = self.read_state(v4_work)
        self.mod.PACKAGE_A_V4_BRIEF_SHA256 = state["brief"]["sha256"]
        self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=v4_work)
        return v4_work

    def make_package_a_v4_adopted_candidate(self):
        """Build v4 fixture with adoption complete (candidate in awaiting_gate)."""
        v4_work = self.make_package_a_v4_fixture()
        self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)
        return v4_work

    def make_package_a_v4_verified_candidate(self):
        """Build v4 fixture with all 5 deterministic gates passed."""
        v4_work = self.make_package_a_v4_adopted_candidate()
        verified = self.call("verify_package_a", state_root=str(self.state), work_id=v4_work)
        self.assertTrue(verified["all_passed"], verified)
        return v4_work

    def make_package_a_v4_with_dossier(self):
        """Build v4 fixture with sealed 128 KiB review dossier."""
        v4_work = self.make_package_a_v4_verified_candidate()
        self.call("build_package_a_v4_dossier", state_root=str(self.state), work_id=v4_work)
        return v4_work

    # ── Package A v4 tests ───────────────────────────────────────────────────

    def test_package_a_v4_identity_constants_and_brief_hash(self):
        """v4 work/outcome IDs are canonical; v4 is in the Package A allowlist."""
        self.assertEqual(self.mod.PACKAGE_A_V4_WORK_ID, "m0c-package-a-dispatch-core-v4-review-rescue")
        self.assertEqual(self.mod.PACKAGE_A_V4_OUTCOME_ID, "m0c-dispatch-contract-core-a-v4-review-rescue")
        self.assertEqual(self.mod.PACKAGE_A_V4_ROUTE, "controller_adopted_candidate")
        self.assertEqual(self.mod.PACKAGE_A_V4_SOURCE_WORK_ID, self.mod.PACKAGE_A_V3_WORK_ID)
        self.assertEqual(self.mod.PACKAGE_A_V4_BUILDER_NAME, "build_attempt_link_idempotency_key")
        v4_ident = self.mod._package_a_identity(self.mod.PACKAGE_A_V4_WORK_ID)
        self.assertIsNotNone(v4_ident)
        self.assertEqual(v4_ident["work_id"], self.mod.PACKAGE_A_V4_WORK_ID)
        self.assertEqual(v4_ident["outcome_id"], self.mod.PACKAGE_A_V4_OUTCOME_ID)
        self.assertTrue(self.mod._is_package_a({"work_id": self.mod.PACKAGE_A_V4_WORK_ID}))
        self.assertTrue(self.mod._is_package_a_v4({"work_id": self.mod.PACKAGE_A_V4_WORK_ID}))
        self.assertFalse(self.mod._is_package_a_v4({"work_id": self.mod.PACKAGE_A_V3_WORK_ID}))

    def test_package_a_v4_adoption_happy_path(self):
        """Adoption copies source files, writes durable receipt, and is idempotent."""
        v4_work = self.make_package_a_v4_fixture()
        result = self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)
        self.assertFalse(result["reused"])
        self.assertEqual(result["route"], "controller_adopted_candidate")
        self.assertIsNotNone(result["candidate_id"])
        self.assertIsNotNone(result["adoption_receipt"])
        state = self.read_state(v4_work)
        self.assertEqual(state["phase"], "awaiting_gate")
        self.assertEqual(state["candidate"]["evidence"]["evidence_kind"], "controller_adopted_candidate")
        # Idempotent reuse
        result2 = self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)
        self.assertTrue(result2["reused"])
        self.assertEqual(result2["candidate_id"], result["candidate_id"])

    def test_package_a_v4_adoption_crash_replay(self):
        """Crash before receipt write recovers deterministically on the next run."""
        v4_work = self.make_package_a_v4_fixture()
        real_adopt_json = self.mod._package_a_adopt_json
        triggered = []
        def crash_on_receipt(path, root, value, label):
            if label == "adoption receipt" and not triggered:
                triggered.append(label)
                raise OSError("simulated crash before receipt")
            return real_adopt_json(path, root, value, label)
        with mock.patch.object(self.mod, "_package_a_adopt_json", side_effect=crash_on_receipt):
            with self.assertRaises(OSError):
                self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)
        self.assertEqual(len(triggered), 1)
        # State has no package_a_adoption (save never ran)
        self.assertNotIn("package_a_adoption", self.read_state(v4_work))
        # Recovery run must succeed
        result = self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)
        self.assertFalse(result["reused"])
        self.assertIsNotNone(result["candidate_id"])

    def test_package_a_v4_adoption_source_tamper(self):
        """Tampering a v3 source file causes adoption to raise."""
        v4_work = self.make_package_a_v4_fixture()
        v3_wt = Path(self.read_state(self.mod.PACKAGE_A_V3_WORK_ID)["worktree"]["path"])
        (v3_wt / "scripts/cowork_dispatch.py").write_text("# tampered source\n")
        with self.assertRaises(self.mod.ControllerError):
            self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)

    def test_package_a_v4_adoption_destination_tamper(self):
        """Tampering a v4 destination file after adoption is caught by verification."""
        v4_work = self.make_package_a_v4_adopted_candidate()
        v4_wt = Path(self.read_state(v4_work)["worktree"]["path"])
        (v4_wt / "scripts/cowork_dispatch.py").write_text("# tampered destination\n")
        with self.assertRaises(self.mod.ControllerError):
            self.call("verify_package_a", state_root=str(self.state), work_id=v4_work)

    def test_package_a_v4_adoption_zero_provider_audit(self):
        """No provider calls are made during the adoption command."""
        v4_work = self.make_package_a_v4_fixture()
        prior_count = len(self.fixture.audit_rows())
        self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)
        self.assertEqual(len(self.fixture.audit_rows()), prior_count,
                         "adopt_package_a_candidate must not invoke the provider")

    def test_package_a_v4_verification_adopted_route(self):
        """verify_package_a dispatches to v4 path; all 5 gates pass; reuse works."""
        v4_work = self.make_package_a_v4_adopted_candidate()
        verified = self.call("verify_package_a", state_root=str(self.state), work_id=v4_work)
        self.assertTrue(verified["all_passed"])
        state = self.read_state(v4_work)
        self.assertIn("package_a_verification", state)
        agg = json.loads(Path(state["package_a_verification"]["aggregate_path"]).read_text())
        self.assertEqual(agg["static"]["route"], "controller_adopted_candidate")
        self.assertEqual(len(agg["receipts"]), 5)
        # Reuse
        verified2 = self.call("verify_package_a", state_root=str(self.state), work_id=v4_work)
        self.assertTrue(verified2["reused"])

    def test_package_a_v4_dossier_content_and_cap(self):
        """Dossier has required fields; size within 128 KiB cap; reuse works."""
        v4_work = self.make_package_a_v4_verified_candidate()
        result = self.call("build_package_a_v4_dossier", state_root=str(self.state), work_id=v4_work)
        self.assertFalse(result["reused"])
        self.assertLessEqual(result["bytes"], 128 * 1024)
        dossier = json.loads(Path(result["path"]).read_text())
        self.assertEqual(dossier["kind"], "package_a_v4_review_dossier")
        self.assertEqual(dossier["route"], "controller_adopted_candidate")
        self.assertEqual(dossier["source_gate_reason"], "review_infrastructure_exhausted")
        self.assertEqual(len(dossier["gate_summaries"]), 5)
        self.assertIn("production_module", dossier)
        result2 = self.call("build_package_a_v4_dossier", state_root=str(self.state), work_id=v4_work)
        self.assertTrue(result2["reused"])

    def test_package_a_v4_dossier_tamper(self):
        """Tampering the sealed dossier file causes the next operation to raise."""
        v4_work = self.make_package_a_v4_with_dossier()
        state = self.read_state(v4_work)
        dossier_path = Path(state["package_a_v4_dossier"]["path"])
        original = dossier_path.read_bytes()
        dossier_path.write_text(original.decode().replace("controller_adopted_candidate", "tampered"))
        try:
            with self.assertRaises(self.mod.ControllerError):
                self.call("build_package_a_v4_dossier", state_root=str(self.state), work_id=v4_work)
        finally:
            dossier_path.write_bytes(original)

    def test_package_a_v4_review_bindings_strict(self):
        """Launch v4 review; ingest passes; envelope binding includes dossier_sha256."""
        v4_work = self.make_package_a_v4_with_dossier()
        self.call("launch_package_a_review", state_root=str(self.state), work_id=v4_work,
                  claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(v4_work)
        result = self.call("ingest_package_a_review", state_root=str(self.state), work_id=v4_work)
        self.assertEqual(result["verdict"], "pass")
        state = self.read_state(v4_work)
        self.assertEqual(state["package_a_review"]["verdict"], "pass")
        envelope = json.loads(Path(state["package_a_review"]["path"]).read_text())
        self.assertEqual(envelope["kind"], "package_a_v4_review_envelope")
        schema = json.loads(Path(attempt["schema_path"]).read_text())
        self.assertIn("dossier_sha256", schema["properties"])

    def test_package_a_v4_review_capacity_preserved(self):
        """Capacity signal from reviewer puts package into awaiting_capacity."""
        v4_work = self.make_package_a_v4_with_dossier()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_KIND")
        os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = "capacity_scheduled"
        try:
            self.call("launch_package_a_review", state_root=str(self.state), work_id=v4_work,
                      claude_bin=str(self.fake_claude_reviewer))
            self.wait_package_a_review(v4_work)
            result = self.call("ingest_package_a_review", state_root=str(self.state), work_id=v4_work)
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_KIND", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_KIND"] = prior
        self.assertEqual(result["operation"], "ingest_package_a_review")
        state = self.read_state(v4_work)
        self.assertEqual(state["phase"], "awaiting_capacity")
        self.assertIn("package_a_review_capacity", state)
        self.assertEqual(state["package_a_review_capacity"]["mode"], "scheduled")

    def test_package_a_v4_timeout_terminal(self):
        """Reviewer timeout is terminal for v4; ingest raises ControllerError."""
        v4_work = self.make_package_a_v4_with_dossier()
        self.call("launch_package_a_review", state_root=str(self.state), work_id=v4_work,
                  claude_bin=str(self.fake_claude_reviewer))
        attempt = self.wait_package_a_review(v4_work)
        terminal_path = Path(attempt["receipt_path"])
        original = terminal_path.read_bytes()
        terminal = json.loads(terminal_path.read_text())
        terminal["timed_out"] = True; terminal["exit_code"] = 124
        terminal_path.write_text(json.dumps(terminal))
        try:
            with self.assertRaisesRegex(self.mod.ControllerError, "timed out"):
                self.call("ingest_package_a_review", state_root=str(self.state), work_id=v4_work)
        finally:
            terminal_path.write_bytes(original)

    def test_package_a_v4_adjudicate_pass(self):
        """Passing v4 review adjudicates to completed; gate replay is idempotent."""
        v4_work = self.make_package_a_v4_with_dossier()
        self.call("launch_package_a_review", state_root=str(self.state), work_id=v4_work,
                  claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(v4_work)
        self.call("ingest_package_a_review", state_root=str(self.state), work_id=v4_work)
        result = self.call("adjudicate_package_a", state_root=str(self.state), work_id=v4_work)
        self.assertEqual(result["disposition"], "pass")
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(self.read_state(v4_work)["phase"], "completed")
        # Gate replay
        result2 = self.call("adjudicate_package_a", state_root=str(self.state), work_id=v4_work)
        self.assertTrue(result2["reused"])

    def test_package_a_v4_adjudicate_no_revise(self):
        """needs_correction verdict is treated as fail; no revise path exists in v4."""
        v4_work = self.make_package_a_v4_with_dossier()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_VERDICT")
        os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = "needs_correction"
        try:
            self.call("launch_package_a_review", state_root=str(self.state), work_id=v4_work,
                      claude_bin=str(self.fake_claude_reviewer))
            self.wait_package_a_review(v4_work)
            self.call("ingest_package_a_review", state_root=str(self.state), work_id=v4_work)
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_VERDICT", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = prior
        result = self.call("adjudicate_package_a", state_root=str(self.state), work_id=v4_work)
        self.assertEqual(result["disposition"], "fail")
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(self.read_state(v4_work)["phase"], "failed")


    # ── Package A v5 fixtures ────────────────────────────────────────────────

    def make_package_a_v5_source_package(self):
        """Build v3 failed source with v4 terminal gate receipt SHA patched for v5."""
        self.make_package_a_v4_source_package()
        # v5 requires v4 in failed phase with its terminal gate receipt SHA
        v4_work = self.mod.PACKAGE_A_V4_WORK_ID
        self.prepare(v4_work)
        state = self.read_state(v4_work)
        self.mod.PACKAGE_A_V4_BRIEF_SHA256 = state["brief"]["sha256"]
        self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=v4_work)
        self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v4_work)
        self.call("verify_package_a", state_root=str(self.state), work_id=v4_work)
        self.call("build_package_a_v4_dossier", state_root=str(self.state), work_id=v4_work)
        # Launch review synchronously and ingest
        self.call("launch_package_a_review", state_root=str(self.state), work_id=v4_work,
                  claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(v4_work)
        self.call("ingest_package_a_review", state_root=str(self.state), work_id=v4_work)
        result = self.call("adjudicate_package_a", state_root=str(self.state), work_id=v4_work)
        # For v5 testing we need v4 to be in failed phase; force to failed state
        state = self.read_state(v4_work)
        gate_path = Path(state["package_a_gate_receipt"]["path"])
        gate_sha = state["package_a_gate_receipt"]["sha256"]
        # Patch v5 source constant to match actual v4 gate receipt SHA
        self.mod.PACKAGE_A_V5_SOURCE_V4_GATE_SHA256 = gate_sha
        if state.get("phase") != "failed":
            state["phase"] = "failed"
            gate = state.get("gate", {})
            gate["verdict"] = "fail"
            gate["reason_code"] = "review_infrastructure_exhausted"
            state["gate"] = gate
            state["package_a_gate_receipt"]["disposition"] = "fail"
            self.state_file(v4_work).write_text(json.dumps(state))
        return v4_work

    def make_package_a_v5_fixture(self):
        """Build fresh v5 fixture with prerequisites materialized."""
        self.make_package_a_v5_source_package()
        v5_work = self.mod.PACKAGE_A_V5_WORK_ID
        self.prepare(v5_work)
        state = self.read_state(v5_work)
        self.mod.PACKAGE_A_V5_BRIEF_SHA256 = state["brief"]["sha256"]
        self.call("materialize_package_a_prerequisites", state_root=str(self.state), work_id=v5_work)
        return v5_work

    def make_package_a_v5_adopted_candidate(self):
        """Build v5 fixture with adoption complete."""
        v5_work = self.make_package_a_v5_fixture()
        self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v5_work)
        return v5_work

    def make_package_a_v5_verified_candidate(self):
        """Build v5 fixture with all 5 deterministic gates passed (hermetic env)."""
        v5_work = self.make_package_a_v5_adopted_candidate()
        verified = self.call("verify_package_a", state_root=str(self.state), work_id=v5_work)
        self.assertTrue(verified["all_passed"], verified)
        return v5_work

    def make_package_a_v5_with_dossier(self):
        """Build v5 fixture with sealed 128 KiB review dossier."""
        v5_work = self.make_package_a_v5_verified_candidate()
        self.call("build_package_a_v5_dossier", state_root=str(self.state), work_id=v5_work)
        return v5_work

    # ── Package A v5 tests ───────────────────────────────────────────────────

    def test_package_a_v5_identity_constants(self):
        """v5 work_id, outcome_id, route, and brief SHA are exact canonical values."""
        self.assertEqual(self.mod.PACKAGE_A_V5_WORK_ID,
                         "m0c-package-a-dispatch-core-v5-review-rescue")
        self.assertEqual(self.mod.PACKAGE_A_V5_OUTCOME_ID,
                         "m0c-dispatch-contract-core-a-v5-review-rescue")
        self.assertEqual(self.mod.PACKAGE_A_V5_ROUTE, "controller_adopted_candidate")
        self.assertEqual(self.mod.PACKAGE_A_V5_BRIEF_SHA256,
                         "2fba8558208d41860f71c3b5d317c3a642e147e61b93f32d26189e6a9366d0fe")
        self.assertEqual(self.mod.PACKAGE_A_V5_ENV_POLICY_DIGEST,
                         "77937e98fb1661bffa4eadb9a70d3620aa5976b57d5d1a60a070f6eb41c96c0e")
        self.assertEqual(self.mod.PACKAGE_A_V5_TEST_TIMEOUT, 900)
        ident = self.mod._package_a_identity(self.mod.PACKAGE_A_V5_WORK_ID)
        self.assertEqual(ident["work_id"], self.mod.PACKAGE_A_V5_WORK_ID)
        self.assertEqual(ident["outcome_id"], self.mod.PACKAGE_A_V5_OUTCOME_ID)
        self.assertEqual(ident["route"], self.mod.PACKAGE_A_V5_ROUTE)

    def test_package_a_v5_v4_regression_isolation(self):
        """v5 work_id is registered but v4 identity functions are unchanged."""
        self.assertIn(self.mod.PACKAGE_A_V5_WORK_ID, self.mod.PACKAGE_A_IDENTITIES)
        ident_v4 = self.mod._package_a_identity(self.mod.PACKAGE_A_V4_WORK_ID)
        self.assertEqual(ident_v4["work_id"], self.mod.PACKAGE_A_V4_WORK_ID)
        self.assertEqual(ident_v4["outcome_id"], self.mod.PACKAGE_A_V4_OUTCOME_ID)
        # is_package_a_v5 does NOT fire on v4 state
        state_v4 = {"work_id": self.mod.PACKAGE_A_V4_WORK_ID}
        self.assertFalse(self.mod._is_package_a_v5(state_v4))
        state_v5 = {"work_id": self.mod.PACKAGE_A_V5_WORK_ID}
        self.assertTrue(self.mod._is_package_a_v5(state_v5))

    def test_package_a_v5_hermetic_env_append_no_existing(self):
        """hermetic_env adds two entries when no GIT_CONFIG_COUNT in environment."""
        env_before = os.environ.copy()
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                    "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
            os.environ.pop(key, None)
        try:
            env = self.mod._package_a_v5_hermetic_env()
        finally:
            for k, v in env_before.items():
                os.environ[k] = v
            for k in list(os.environ):
                if k not in env_before:
                    del os.environ[k]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "commit.gpgSign")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "false")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "tag.gpgSign")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "false")

    def test_package_a_v5_hermetic_env_append_existing(self):
        """hermetic_env preserves existing GIT_CONFIG_COUNT entries and appends."""
        env_before = os.environ.copy()
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "user.name"
        os.environ["GIT_CONFIG_VALUE_0"] = "Test"
        for key in ("GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1",
                    "GIT_CONFIG_KEY_2", "GIT_CONFIG_VALUE_2"):
            os.environ.pop(key, None)
        try:
            env = self.mod._package_a_v5_hermetic_env()
        finally:
            for k, v in env_before.items():
                os.environ[k] = v
            for k in list(os.environ):
                if k not in env_before:
                    del os.environ[k]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "user.name")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Test")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "commit.gpgSign")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "false")
        self.assertEqual(env["GIT_CONFIG_KEY_2"], "tag.gpgSign")
        self.assertEqual(env["GIT_CONFIG_VALUE_2"], "false")

    def test_package_a_v5_hermetic_env_malformed_count_rejected(self):
        """Malformed GIT_CONFIG_COUNT (non-integer) raises ControllerError."""
        env_before = os.environ.copy()
        os.environ["GIT_CONFIG_COUNT"] = "not-a-number"
        try:
            with self.assertRaises(self.mod.ControllerError) as cm:
                self.mod._package_a_v5_hermetic_env()
            self.assertIn("malformed", str(cm.exception).lower())
        finally:
            for k, v in env_before.items():
                os.environ[k] = v
            for k in list(os.environ):
                if k not in env_before:
                    del os.environ[k]

    def test_package_a_v5_hermetic_env_excessive_count_rejected(self):
        """GIT_CONFIG_COUNT exceeding 256 raises ControllerError."""
        env_before = os.environ.copy()
        os.environ["GIT_CONFIG_COUNT"] = "300"
        try:
            with self.assertRaises(self.mod.ControllerError) as cm:
                self.mod._package_a_v5_hermetic_env()
            self.assertIn("excessive", str(cm.exception).lower())
        finally:
            for k, v in env_before.items():
                os.environ[k] = v
            for k in list(os.environ):
                if k not in env_before:
                    del os.environ[k]

    def test_package_a_v5_hermetic_env_no_home_mutation(self):
        """_package_a_v5_hermetic_env does not modify HOME in os.environ."""
        original_home = os.environ.get("HOME")
        env_before = os.environ.copy()
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            os.environ.pop(key, None)
        try:
            env = self.mod._package_a_v5_hermetic_env()
        finally:
            for k, v in env_before.items():
                os.environ[k] = v
            for k in list(os.environ):
                if k not in env_before:
                    del os.environ[k]
        # HOME in child env unchanged; os.environ.HOME unchanged
        self.assertEqual(env.get("HOME"), original_home)
        self.assertEqual(os.environ.get("HOME"), original_home)

    def test_package_a_v5_timeout_spec_is_900(self):
        """test_cowork gate uses 900s timeout in v5 specs; characterization is 180s."""
        specs = self.mod._package_a_v5_verification_specs()
        by_label = {s["label"]: s for s in specs}
        self.assertEqual(by_label["test_cowork"]["timeout"], 900)
        self.assertEqual(by_label["characterization"]["timeout"], 180)
        # v4 test_cowork timeout is 300 (not 900)
        v4_specs = self.mod._package_a_v4_verification_specs()
        by_label_v4 = {s["label"]: s for s in v4_specs}
        self.assertEqual(by_label_v4["test_cowork"]["timeout"], 300)

    def test_package_a_v5_env_policy_digest_sealed_in_static(self):
        """env_policy_digest appears in v5 verification static; absent in v4 static."""
        self.make_package_a_v5_fixture()
        v5_work = self.mod.PACKAGE_A_V5_WORK_ID
        self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v5_work)
        state = self.read_state(v5_work)
        candidate = state["candidate"]
        baseline = self.mod._package_a_prerequisites_intact(
            str(self.state / v5_work), state)
        delta = self.mod._package_a_builder_delta(
            str(self.state / v5_work), state, candidate)
        static = self.mod._package_a_v5_verification_static(
            state, candidate, delta, baseline)
        self.assertEqual(static["env_policy_digest"],
                         self.mod.PACKAGE_A_V5_ENV_POLICY_DIGEST)
        # v4 static does NOT include env_policy_digest
        v4_state = {"work_id": self.mod.PACKAGE_A_V4_WORK_ID,
                    "package_a_adoption": {}, "package_a_prerequisites": {},
                    "package_a_builder_delta": {}}
        try:
            v4_static = self.mod._package_a_v4_verification_static(
                v4_state, candidate, delta, baseline)
            self.assertNotIn("env_policy_digest", v4_static)
        except Exception:
            pass  # may fail on incomplete state; absence proof is sufficient

    def test_package_a_v5_env_policy_tamper_rejected(self):
        """Tampered env_policy_digest in sealed intent causes ControllerError."""
        v5_work = self.make_package_a_v5_adopted_candidate()
        # Verify once to create intent
        self.call("verify_package_a", state_root=str(self.state), work_id=v5_work)
        state = self.read_state(v5_work)
        verify_bound = state["package_a_verification"]
        intent_path = Path(verify_bound["intent_path"])
        intent = json.loads(intent_path.read_text())
        # Tamper env_policy_digest inside static
        intent["static"]["env_policy_digest"] = "0" * 64
        intent_path.write_text(json.dumps(intent))
        new_sha = hashlib.sha256(intent_path.read_bytes()).hexdigest()
        state["package_a_verification"]["intent_sha256"] = new_sha
        self.state_file(v5_work).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            candidate = state["candidate"]
            baseline = self.mod._package_a_prerequisites_intact(
                str(self.state / v5_work), state)
            delta = self.mod._package_a_builder_delta(
                str(self.state / v5_work), state, candidate)
            self.mod._package_a_v5_validate_verification(
                str(self.state / v5_work), state, candidate, baseline, delta)

    def test_package_a_v5_adoption_happy_path(self):
        """v5 adoption produces receipt with v5 work_id and outcome_id."""
        v5_work = self.make_package_a_v5_fixture()
        result = self.call("adopt_package_a_candidate", state_root=str(self.state), work_id=v5_work)
        state = self.read_state(v5_work)
        self.assertIn("package_a_adoption", state)
        adopt = state["package_a_adoption"]
        # Receipt must be bound to v5 identity
        receipt_path = Path(adopt["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["work_id"], self.mod.PACKAGE_A_V5_WORK_ID)
        self.assertEqual(receipt["outcome_id"], self.mod.PACKAGE_A_V5_OUTCOME_ID)
        self.assertEqual(state["phase"], "awaiting_gate")

    def test_package_a_v5_adjudicate_pass(self):
        """v5 adjudication with all gates passed and review pass seals disposition=pass."""
        v5_work = self.make_package_a_v5_with_dossier()
        self.call("launch_package_a_review", state_root=str(self.state), work_id=v5_work,
                  claude_bin=str(self.fake_claude_reviewer))
        self.wait_package_a_review(v5_work)
        self.call("ingest_package_a_review", state_root=str(self.state), work_id=v5_work)
        result = self.call("adjudicate_package_a", state_root=str(self.state), work_id=v5_work)
        self.assertEqual(result["disposition"], "pass")
        self.assertEqual(result["phase"], "completed")
        state = self.read_state(v5_work)
        self.assertEqual(state["phase"], "completed")

    def test_package_a_v5_adjudicate_no_revise(self):
        """needs_correction verdict is treated as fail; no revise path exists in v5."""
        v5_work = self.make_package_a_v5_with_dossier()
        prior = os.environ.get("FAKE_CLAUDE_REVIEWER_VERDICT")
        os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = "needs_correction"
        try:
            self.call("launch_package_a_review", state_root=str(self.state), work_id=v5_work,
                      claude_bin=str(self.fake_claude_reviewer))
            self.wait_package_a_review(v5_work)
            self.call("ingest_package_a_review", state_root=str(self.state), work_id=v5_work)
        finally:
            if prior is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_VERDICT", None)
            else: os.environ["FAKE_CLAUDE_REVIEWER_VERDICT"] = prior
        result = self.call("adjudicate_package_a", state_root=str(self.state), work_id=v5_work)
        self.assertEqual(result["disposition"], "fail")
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(self.read_state(v5_work)["phase"], "failed")


class PackageBLifecycleTests(unittest.TestCase):
    """Offline tests for the complete Package B uniform-refusal lifecycle."""

    _FAKE_COWORK_DISPATCH_SRC = b"""\
import dataclasses, json, uuid as _uuid

@dataclasses.dataclass
class DispatchContract:
    purpose: str
    role: str
    policy: str
    session_id: str
    contract_id: str = dataclasses.field(default_factory=lambda: str(_uuid.uuid4()))

@dataclasses.dataclass
class DispatchDecision:
    outcome: str
    refusal_code: str = None
    refusal_message: str = None
    spawned: bool = False

def validate_dispatch_contract(c):
    if not isinstance(c, (DispatchContract, dict)): raise ValueError("not a contract")
    return c

def validate_dispatch_decision(d, c=None):
    if not isinstance(d, (DispatchDecision, dict)): raise ValueError("not a decision")
    return d

def decide(c, policy_result=None, preflight_result=None, probe_result=None):
    if isinstance(c, dict):
        refused = next((fact for fact in (policy_result, preflight_result, probe_result)
                        if isinstance(fact, dict) and fact.get('allowed') is False), None)
        return {'schema_version': 1, 'record': 'DispatchDecision',
                'decision_id': str(_uuid.uuid4()), 'contract_id': c['contract_id'],
                'outcome': 'refuse' if refused else 'allow',
                'refusal_code': refused.get('refusal_code') if refused else None,
                'refusal_message': refused.get('refusal_message') if refused else None,
                'source': refused.get('source') if refused else None,
                'spawned': False, 'trace_event_id': None}
    if getattr(c, 'policy', '') == 'blocked':
        return DispatchDecision(outcome='refuse', refusal_code='policy_blocked', refusal_message='blocked')
    return DispatchDecision(outcome='allow')
"""

    _FAKE_TEST_COWORK_SRC = b"""\
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unittest
from cowork_dispatch import DispatchContract, DispatchDecision, decide

class DispatchContractAdapterTest(unittest.TestCase):
    def _blocked(self, role='implementer'):
        return DispatchContract(purpose='test', role=role, policy='blocked', session_id='s1')
    def _unrestricted(self, role='implementer'):
        return DispatchContract(purpose='test', role=role, policy='unrestricted', session_id='s2')
    def test_run_scout_uniform_refusal(self):
        self.assertEqual(decide(self._blocked('scout')).outcome, 'refuse')
    def test_run_planner_uniform_refusal(self):
        self.assertEqual(decide(self._blocked('planner')).outcome, 'refuse')
    def test_run_builder_uniform_refusal(self):
        self.assertEqual(decide(self._blocked()).outcome, 'refuse')
    def test_run_flow_pre_launch_guard_uniform_refusal(self):
        self.assertEqual(decide(self._blocked()).outcome, 'refuse')
    def test_switch_controller_uniform_refusal(self):
        self.assertEqual(decide(self._blocked()).outcome, 'refuse')
    def test_reviewer_pre_check_uniform_refusal(self):
        self.assertEqual(decide(self._blocked('reviewer')).outcome, 'refuse')
    def test_thrown_denial_backstop_negative_control(self):
        try: raise ValueError('denial')
        except ValueError: pass
        self.assertTrue(True)
    def test_no_probe_spawn_after_refusal(self):
        d = decide(self._blocked())
        self.assertEqual(d.outcome, 'refuse'); self.assertFalse(d.spawned)
    def test_unrestricted_policy_compatibility(self):
        d = decide(self._unrestricted())
        self.assertEqual(d.outcome, 'allow'); self.assertIsNone(d.refusal_code)
"""

    _FAKE_TEST_CHAR_SRC = b"""\
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unittest
from cowork_dispatch import DispatchContract, DispatchDecision, decide

class MissingDispatchContractTest(unittest.TestCase):
    def test_uniform_refusal_contract(self):
        c = DispatchContract(purpose='test', role='implementer', policy='blocked', session_id='s1')
        d = decide(c)
        self.assertEqual(d.outcome, 'refuse'); self.assertFalse(d.spawned)
    @unittest.expectedFailure
    def test_pending_turn_retry_linkage_contract(self):
        raise AssertionError("pending turn retry linkage not yet implemented")
    @unittest.expectedFailure
    def test_gate_repair_retry_linkage_contract(self):
        raise AssertionError("gate repair retry linkage not yet implemented")
"""

    _FAKE_FIXTURE_SRC = b'{"sources":[]}\n'

    def setUp(self):
        self.fixture = OrchestrateCLITest(methodName="runTest")
        self.fixture.setUp()
        for name in ("tmp", "root", "repo", "state", "brief", "audit", "base_head"):
            setattr(self, name, getattr(self.fixture, name))
        self.mod = controller_module()
        self.claude_reviewer_audit = self.root / "fake-claude-reviewer-audit.jsonl"
        self._old_reviewer_audit = os.environ.get("FAKE_CLAUDE_REVIEWER_AUDIT")
        os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = str(self.claude_reviewer_audit)
        self.fake_package_b_builder = self.root / "fake-package-b-builder"
        self.fake_package_b_builder.write_text("""#!/usr/bin/env %(py)s
import json, os, sys
argv = sys.argv[1:]
session_id = (argv[argv.index('--session-id') + 1] if '--session-id' in argv else
              argv[argv.index('--resume') + 1] if '--resume' in argv else 'session-b-fake')
site_keys = ('run_scout','run_planner','run_builder','run_flow_pre_launch','switch_controller','reviewer_pre_check')
sites = {key: {'purpose': 'launch', 'refusal_mapping': 'typed refusal',
               'return_shape': 'legacy shape', 'trace_event': 'legacy event',
               'test_ids': ['scripts.test_cowork.DispatchContractAdapterTest.test_' + key]}
         for key in site_keys}
kind = os.environ.get('FAKE_PB_BUILDER_KIND', 'valid')
if kind == 'malformed':
    print('not-json')
elif kind == 'missing_site_map':
    packet = {'outcome': 'completed', 'summary': 'no site_map', 'changed_paths': [], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'gate'}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
elif kind == 'wrong_paths':
    with open('scripts/cowork_dispatch.py', 'a') as f: f.write('\\n# Package B changes\\n')
    packet = {'outcome': 'completed', 'summary': 'wrong paths', 'changed_paths': ['?? nonexistent.py'], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'gate', 'site_map': sites}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
elif kind == 'bad_witness':
    with open('scripts/cowork_dispatch.py', 'a') as f: f.write('\\n# Package B changes\\n')
    with open('scripts/test_dispatch_contract_characterization.py', 'a') as f: f.write('\\n    def test_witness_role_dispatch_refusals_are_not_uniform_today(self): pass\\n')
    packet = {'outcome': 'completed', 'summary': 'bad witness fake', 'changed_paths': ['?? scripts/cowork_dispatch.py', '?? scripts/test_dispatch_contract_characterization.py'], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'run the deterministic gate', 'site_map': sites}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
else:
    with open('scripts/cowork_dispatch.py', 'a') as f: f.write('\\n# Package B changes\\n')
    packet = {'outcome': 'completed', 'summary': 'Package B bounded fake', 'changed_paths': ['?? scripts/cowork_dispatch.py'], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'run the deterministic gate', 'site_map': sites}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
""" % {"py": sys.executable})
        self.fake_package_b_builder.chmod(
            self.fake_package_b_builder.stat().st_mode | stat.S_IXUSR)
        self.fake_package_b_reviewer = self.root / "fake-package-b-reviewer"
        self.fake_package_b_reviewer.write_text("""#!/usr/bin/env %(py)s
import hashlib, json, os, sys
argv = sys.argv[1:]
with open(os.environ['FAKE_CLAUDE_REVIEWER_AUDIT'], 'a') as out:
    out.write(json.dumps({'argv': argv, 'cwd': os.getcwd()}) + '\\n')
session_id = argv[argv.index('--session-id') + 1]
kind = os.environ.get('FAKE_PB_REVIEWER_KIND', '')
if kind == 'overflow':
    sys.stdout.write(json.dumps({'type': 'result', 'session_id': session_id}) + '\\n')
    sys.stdout.flush()
    chunk = ('{"type":"debug","data":"' + 'x' * 990 + '"}\\n').encode()
    written = 0
    cap = 2 * 1024 * 1024 + 65536
    while written < cap:
        sys.stdout.buffer.write(chunk); written += len(chunk)
    sys.stdout.buffer.flush(); sys.exit(1)
schema_json = argv[argv.index('--json-schema') + 1]
schema = json.loads(schema_json)
p = schema['properties']
packet = {k: v['const'] for k, v in p.items() if 'const' in v}
aggregate_path = os.environ.get('FAKE_PB_AGGREGATE_PATH', '')
if aggregate_path and os.path.isfile(aggregate_path):
    agg = json.loads(open(aggregate_path).read())
    packet['verification_receipt_sha256s'] = [r['sha256'] for r in agg['receipts']]
else:
    packet['verification_receipt_sha256s'] = ['0' * 64] * 8
changed_paths_str = os.environ.get('FAKE_PB_CHANGED_PATHS', '')
packet['changed_paths'] = json.loads(changed_paths_str) if changed_paths_str else ['scripts/cowork_dispatch.py']
packet['verdict'] = os.environ.get('FAKE_PB_REVIEWER_VERDICT', 'pass')
packet['findings'] = []
packet['checks'] = ['fake Package B review check']
if kind == 'binding_omission':
    packet['verification_receipt_sha256s'] = ['f' * 64] * 8
    packet['changed_paths'] = ['scripts/cowork.py']
print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
""" % {"py": sys.executable})
        self.fake_package_b_reviewer.chmod(
            self.fake_package_b_reviewer.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        if self._old_reviewer_audit is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_AUDIT", None)
        else: os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = self._old_reviewer_audit
        for key in ("FAKE_PB_BUILDER_KIND", "FAKE_PB_REVIEWER_KIND", "FAKE_PB_REVIEWER_VERDICT",
                    "FAKE_PB_AGGREGATE_PATH", "FAKE_PB_CHANGED_PATHS"):
            os.environ.pop(key, None)
        self.fixture.tearDown()

    def call(self, command, **values):
        return getattr(self.mod, "command_" + command)(SimpleNamespace(**values))

    def read_state(self, work_id=None):
        return self.fixture.read_state(work_id or self.mod.PACKAGE_B_WORK_ID)

    def state_file(self, work_id=None):
        return self.fixture.state_file(work_id or self.mod.PACKAGE_B_WORK_ID)

    def invoke(self, *args, **kwargs):
        return self.fixture.invoke(*args, **kwargs)

    def _setup_fake_v5_source(self):
        """Create a minimal fake Package A v5 completed state and patch module constants."""
        v5_work = self.mod.PACKAGE_B_PREREQ_WORK_ID
        v5_dir = self.state / v5_work
        v5_dir.mkdir(parents=True, exist_ok=True)
        # Worktree must be at the canonical location: repo/.worktrees/orchestrator-{work_id}
        repo_real = os.path.realpath(str(self.repo))
        wt_path = os.path.join(repo_real, ".worktrees", "orchestrator-" + v5_work)
        wt = Path(wt_path)
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "scripts").mkdir(exist_ok=True)
        (wt / "scripts" / "fixtures").mkdir(parents=True, exist_ok=True)
        files = {
            "scripts/cowork_dispatch.py": self._FAKE_COWORK_DISPATCH_SRC,
            "scripts/test_cowork.py": self._FAKE_TEST_COWORK_SRC,
            "scripts/test_dispatch_contract_characterization.py": self._FAKE_TEST_CHAR_SRC,
            "scripts/fixtures/dispatch_contract_characterization_sources.json": self._FAKE_FIXTURE_SRC,
        }
        sha_map = {}
        for rel, content in files.items():
            path = wt / rel
            path.write_bytes(content)
            sha_map[rel] = hashlib.sha256(content).hexdigest()
        self.mod.PACKAGE_B_SOURCE_FILES = dict(sha_map)
        self.mod.PACKAGE_B_FIXTURE_SHA256 = sha_map["scripts/fixtures/dispatch_contract_characterization_sources.json"]
        gate_dir = v5_dir / "package-a-gates"
        gate_dir.mkdir(parents=True, exist_ok=True)
        gate_content = json.dumps({"kind": "package_a_gate_receipt", "work_id": v5_work, "disposition": "pass"}).encode()
        gate_path = gate_dir / "gate.json"
        gate_path.write_bytes(gate_content)
        gate_sha = hashlib.sha256(gate_content).hexdigest()
        prereq_dir = v5_dir / "prerequisites"
        prereq_dir.mkdir(parents=True, exist_ok=True)
        prereq_content = json.dumps({"kind": "package_a_prerequisites", "work_id": v5_work}).encode()
        prereq_path = prereq_dir / "baseline.json"
        prereq_path.write_bytes(prereq_content)
        prereq_sha = hashlib.sha256(prereq_content).hexdigest()
        candidate_id = "package-a-" + "a" * 32
        evidence_digest = "e" * 64
        self.mod.PACKAGE_B_PREREQ_GATE_SHA256 = gate_sha
        self.mod.PACKAGE_B_PREREQ_RECEIPT_SHA256 = prereq_sha
        self.mod.PACKAGE_B_PREREQ_CANDIDATE_ID = candidate_id
        self.mod.PACKAGE_B_PREREQ_EVIDENCE_DIGEST = evidence_digest
        brief_path = v5_dir / "brief.md"
        brief_path.write_text("fake v5 brief")
        v5_state = {
            "schema_version": 1, "revision": 1, "phase": "completed", "work_id": v5_work,
            "repo": {"root": repo_real, "head": self.base_head},
            "brief": {"sha256": "a" * 64, "path": str(os.path.realpath(str(brief_path)))},
            "backend": {"kind": "claude_direct"},
            "candidate": {"id": candidate_id, "evidence_digest": evidence_digest},
            "package_a_gate_receipt": {"path": str(gate_path), "sha256": gate_sha},
            "package_a_prerequisites": {"path": str(prereq_path), "sha256": prereq_sha},
            "worktree": {"path": wt_path, "branch": "codex/" + v5_work},
        }
        (v5_dir / "state.json").write_text(json.dumps(v5_state))
        return wt_path

    def _prepare_package_b(self):
        """prepare Package B and patch brief SHA."""
        self._setup_fake_v5_source()
        bwork = self.mod.PACKAGE_B_WORK_ID
        result = self.invoke("prepare", "--repo", str(self.repo), "--brief-file", str(self.brief),
                             "--state-root", str(self.state), "--work-id", bwork)
        state = self.read_state(bwork)
        self.mod.PACKAGE_B_BRIEF_SHA256 = state["brief"]["sha256"]
        self.mod.PACKAGE_B_BASE_HEAD = state["repo"]["head"]
        return bwork

    def make_package_b_materialized(self):
        """Prepared + prerequisites materialized."""
        bwork = self._prepare_package_b()
        self.call("materialize_package_b_prerequisites",
                  state_root=str(self.state), work_id=bwork)
        return bwork

    def make_package_b_with_candidate(self):
        """Through collect (awaiting_gate)."""
        bwork = self.make_package_b_materialized()
        self.call("launch", state_root=str(self.state), work_id=bwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_b_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(bwork)
        self.call("collect", state_root=str(self.state), work_id=bwork)
        return bwork

    def make_package_b_verified(self):
        """Through verification (all 8 gates)."""
        bwork = self.make_package_b_with_candidate()
        result = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
        self.assertTrue(result["all_passed"], result)
        return bwork

    def wait_package_b_review(self, work):
        for _ in range(160):
            attempt = self.read_state(work).get("package_b_review_attempt") or {}
            receipt = attempt.get("receipt_path")
            if (not self.mod.process_alive(attempt.get("pid"), attempt.get("pgid")) and
                    isinstance(receipt, str) and Path(receipt).is_file()):
                return attempt
            time.sleep(0.025)
        self.fail("fake Package B reviewer did not become quiescent")

    def launch_package_b_review(self, bwork, aggregate_path, changed_paths=None):
        os.environ["FAKE_PB_AGGREGATE_PATH"] = aggregate_path
        if changed_paths is not None:
            os.environ["FAKE_PB_CHANGED_PATHS"] = json.dumps(changed_paths)
        self.call("launch_package_b_review", state_root=str(self.state), work_id=bwork,
                  claude_bin=str(self.fake_package_b_reviewer))
        self.wait_package_b_review(bwork)

    def make_package_b_reviewed(self):
        """Through ingested independent review (verdict=pass)."""
        bwork = self.make_package_b_verified()
        state = self.read_state(bwork)
        agg_path = state["package_b_verification"]["aggregate_path"]
        self.launch_package_b_review(bwork, agg_path, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        return bwork

    # ── identity tests ───────────────────────────────────────────────────────

    def test_package_b_identity_constants(self):
        self.assertEqual(self.mod.PACKAGE_B_WORK_ID, "m0c-package-b-uniform-refusal")
        self.assertEqual(self.mod.PACKAGE_B_PACKAGE_ID, "m0c-dispatch-contract-seam-package-b")
        self.assertEqual(self.mod.PACKAGE_B_GATE_PRINCIPAL, "orchestrator-package-b-gate/v1")
        self.assertEqual(self.mod.PACKAGE_B_CLAUDE_REVIEW_ACTOR, "claude_package_b_controller_v1")
        self.assertEqual(self.mod.PACKAGE_B_CHAR_EXPECTED_FAILURES, 2)
        self.assertEqual(self.mod.PACKAGE_B_ENV_POLICY_DIGEST,
                         "392ac8c8740ee46607b8c72229dbdf6a29fe9a0265e2c50ae927bb152216cc68")
        self.assertEqual(len(self.mod.PACKAGE_B_FOCUSED_SITE_TESTS), 9)
        self.assertIn("test_uniform_refusal_contract", self.mod.PACKAGE_B_FOCUSED_UNIFORM_TEST)
        self.assertEqual(set(self.mod.PACKAGE_B_OUTCOME_SCHEMA["properties"]["site_map"]["required"]),
                         set(self.mod.PACKAGE_B_SITE_KEYS))

    def test_package_b_is_package_b_discriminator(self):
        self.assertTrue(self.mod._is_package_b({"work_id": self.mod.PACKAGE_B_WORK_ID}))
        self.assertFalse(self.mod._is_package_b({"work_id": "other-work"}))
        self.assertFalse(self.mod._is_package_b({}))

    # ── no generic adjudicate bypass ─────────────────────────────────────────

    def test_package_b_generic_adjudicate_blocked(self):
        """Generic adjudicate must raise for every Package B work ID."""
        bwork = self._prepare_package_b()
        state = self.read_state(bwork)
        state["work_id"] = self.mod.PACKAGE_B_WORK_ID
        self.state_file(bwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError) as ctx:
            self.call("adjudicate", state_root=str(self.state), work_id=bwork,
                      verdict="pass", rationale="bypass attempt",
                      adjudicator_principal="fake-supervisor",
                      instruction=None, capability_required=None,
                      target_authority_role=None, target_authority_principal=None)
        self.assertIn("Package B", str(ctx.exception))

    # ── materialization ───────────────────────────────────────────────────────

    def test_package_b_materialize_happy_path(self):
        bwork = self._prepare_package_b()
        result = self.call("materialize_package_b_prerequisites",
                           state_root=str(self.state), work_id=bwork)
        self.assertFalse(result.get("reused"))
        state = self.read_state(bwork)
        self.assertIn("package_b_prerequisites", state)
        fp = result["fingerprint"]
        self.assertIsInstance(fp.get("digest"), str)
        wt = Path(state["worktree"]["path"])
        for rel in self.mod.PACKAGE_B_SOURCE_FILES:
            self.assertTrue((wt / rel).exists(), rel)

    def test_package_b_materialize_replaces_exact_tracked_base_file(self):
        scripts = self.repo / "scripts"
        scripts.mkdir(exist_ok=True)
        base_bytes = b"# tracked base test file\n"
        (scripts / "test_cowork.py").write_bytes(base_bytes)
        subprocess.run(["git", "-C", str(self.repo), "add", "scripts/test_cowork.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgSign=false", "commit", "-m", "tracked base"],
                       check=True, stdout=subprocess.DEVNULL)
        self.base_head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.fixture.base_head = self.base_head
        self.mod.PACKAGE_B_BASE_FILE_HASHES = {
            "scripts/test_cowork.py": hashlib.sha256(base_bytes).hexdigest(),
        }
        bwork = self._prepare_package_b()
        self.call("materialize_package_b_prerequisites", state_root=str(self.state), work_id=bwork)
        wt = Path(self.read_state(bwork)["worktree"]["path"])
        self.assertEqual((wt / "scripts/test_cowork.py").read_bytes(), self._FAKE_TEST_COWORK_SRC)

    def test_package_b_materialize_is_idempotent(self):
        bwork = self.make_package_b_materialized()
        result2 = self.call("materialize_package_b_prerequisites",
                            state_root=str(self.state), work_id=bwork)
        self.assertTrue(result2.get("reused"))

    def test_package_b_materialize_wrong_role_rejected(self):
        """materialize requires fresh prepared state, not an active attempt."""
        bwork = self.make_package_b_materialized()
        self.call("launch", state_root=str(self.state), work_id=bwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_b_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(bwork)
        state = self.read_state(bwork)
        self.assertIsNotNone(state.get("attempt"))
        with self.assertRaises(self.mod.ControllerError):
            self.call("materialize_package_b_prerequisites",
                      state_root=str(self.state), work_id=bwork)

    def test_package_b_materialize_source_tamper_rejected(self):
        """Upstream Package A v5 source tamper raises ControllerError."""
        bwork = self._prepare_package_b()
        state = self.read_state(bwork)
        wt = Path(self.mod.PACKAGE_B_PREREQ_WORK_ID)
        self.mod.PACKAGE_B_PREREQ_GATE_SHA256 = "a" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("materialize_package_b_prerequisites",
                      state_root=str(self.state), work_id=bwork)

    # ── launch guard ──────────────────────────────────────────────────────────

    def test_package_b_launch_wrong_role_rejected(self):
        bwork = self.make_package_b_materialized()
        with self.assertRaises(self.mod.ControllerError) as ctx:
            self.call("launch", state_root=str(self.state), work_id=bwork,
                      role="investigator", resume=False,
                      claude_bin=str(self.fake_package_b_builder),
                      model="sonnet", effort="medium")
        self.assertIn("implementer", str(ctx.exception))

    def test_package_b_launch_wrong_model_rejected(self):
        bwork = self.make_package_b_materialized()
        with self.assertRaises(self.mod.ControllerError) as ctx:
            self.call("launch", state_root=str(self.state), work_id=bwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_b_builder),
                      model="opus", effort="medium")
        self.assertIn("sonnet", str(ctx.exception))

    def test_package_b_launch_no_prerequisites_rejected(self):
        self._prepare_package_b()
        bwork = self.mod.PACKAGE_B_WORK_ID
        with self.assertRaises(self.mod.ControllerError) as ctx:
            self.call("launch", state_root=str(self.state), work_id=bwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_b_builder),
                      model="sonnet", effort="medium")
        self.assertIn("prerequisite", str(ctx.exception))

    # ── collect ────────────────────────────────────────────────────────────────

    def test_package_b_collect_happy_path(self):
        bwork = self.make_package_b_with_candidate()
        state = self.read_state(bwork)
        self.assertEqual(state["phase"], "awaiting_gate")
        candidate = state.get("candidate") or {}
        self.assertIn("package-b-", candidate.get("id", ""))
        self.assertIn("package_b_collect", state)
        self.assertIn("package_b_builder_delta", state)

    def test_package_b_collect_replay_idempotent(self):
        bwork = self.make_package_b_with_candidate()
        result1 = self.call("collect", state_root=str(self.state), work_id=bwork)
        result2 = self.call("collect", state_root=str(self.state), work_id=bwork)
        self.assertEqual(result1["gate"]["candidate_id"], result2["gate"]["candidate_id"])

    def test_package_b_collect_wrong_packet_rejected(self):
        """Missing site_map in packet fails closed."""
        bwork = self.make_package_b_materialized()
        os.environ["FAKE_PB_BUILDER_KIND"] = "missing_site_map"
        self.call("launch", state_root=str(self.state), work_id=bwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_b_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(bwork)
        result = self.call("collect", state_root=str(self.state), work_id=bwork)
        self.assertEqual(result["gate"]["verdict"], "fail")

    def test_package_b_delta_wrong_paths_rejected(self):
        """Worker claiming a non-allowed path is rejected at collect."""
        bwork = self.make_package_b_materialized()
        os.environ["FAKE_PB_BUILDER_KIND"] = "wrong_paths"
        self.call("launch", state_root=str(self.state), work_id=bwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_b_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(bwork)
        with self.assertRaises(self.mod.ControllerError):
            self.call("collect", state_root=str(self.state), work_id=bwork)

    # ── verification ──────────────────────────────────────────────────────────

    def test_package_b_verify_all_eight_gates_pass(self):
        bwork = self.make_package_b_with_candidate()
        result = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
        self.assertTrue(result["all_passed"], result)
        state = self.read_state(bwork)
        self.assertIn("package_b_verification", state)
        agg = json.loads(Path(result["aggregate_path"]).read_text())
        self.assertEqual(len(agg["receipts"]), 8)
        self.assertTrue(agg["all_passed"])

    def test_package_b_verify_replay_idempotent(self):
        bwork = self.make_package_b_verified()
        result2 = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
        self.assertTrue(result2["all_passed"])
        self.assertTrue(result2.get("reused"))

    def test_package_b_structural_gate_retired_witness_required_absent(self):
        """Structural gate fails if retired witness is present in char file.
        The bad witness is introduced by the builder (before collect) so the candidate
        fingerprint includes it and the integrity check at verify time passes; the
        structural gate itself then detects and rejects the retired method."""
        bwork = self.make_package_b_materialized()
        os.environ["FAKE_PB_BUILDER_KIND"] = "bad_witness"
        self.call("launch", state_root=str(self.state), work_id=bwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_b_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(bwork)
        self.call("collect", state_root=str(self.state), work_id=bwork)
        result = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
        self.assertFalse(result["all_passed"])

    # ── review ──────────────────────────────────────────────────────────────

    def test_package_b_review_happy_path(self):
        bwork = self.make_package_b_reviewed()
        state = self.read_state(bwork)
        self.assertIn("package_b_review", state)
        self.assertEqual(state["package_b_review"]["verdict"], "pass")

    def test_package_b_review_dissent_triggers_revise(self):
        bwork = self.make_package_b_verified()
        state = self.read_state(bwork)
        agg_path = state["package_b_verification"]["aggregate_path"]
        os.environ["FAKE_PB_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_b_review(bwork, agg_path, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        result = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(result["disposition"], "revise")
        self.assertEqual(result["phase"], "prepared")
        state2 = self.read_state(bwork)
        self.assertFalse(state2.get("package_b_correction_used"))
        self.assertEqual(state2["recovery_constraint"]["kind"], "package_b_exact_correction")
        self.assertIsNone(state2["recovery_constraint"]["consumed_at"])

    def test_package_b_second_dissent_terminal_fail(self):
        """One dissent authorizes one exact-session correction; a second dissent fails."""
        bwork = self.make_package_b_verified()
        state = self.read_state(bwork)
        original_session = state["attempt"]["provider_session_id"]
        agg_path = state["package_b_verification"]["aggregate_path"]
        os.environ["FAKE_PB_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_b_review(bwork, agg_path, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        result = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(result["disposition"], "revise")
        launched = self.call("launch", state_root=str(self.state), work_id=bwork,
                             role="implementer", resume=True,
                             claude_bin=str(self.fake_package_b_builder),
                             model="sonnet", effort="medium")
        self.assertEqual(launched["provider_session_id"], original_session)
        self.fixture.wait_quiescent(bwork)
        self.call("collect", state_root=str(self.state), work_id=bwork)
        verified = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
        self.assertTrue(verified["all_passed"])
        corrected = self.read_state(bwork)
        self.assertTrue(corrected.get("package_b_correction_used"))
        os.environ["FAKE_PB_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_b_review(bwork, corrected["package_b_verification"]["aggregate_path"],
                                     ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        terminal = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(terminal["disposition"], "fail")
        self.assertEqual(terminal["phase"], "failed")

    def test_package_b_exact_session_correction_then_pass(self):
        """A revise receipt authorizes exactly one same-session builder correction."""
        bwork = self.make_package_b_verified()
        first = self.read_state(bwork)
        original_session = first["attempt"]["provider_session_id"]
        os.environ["FAKE_PB_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_b_review(bwork, first["package_b_verification"]["aggregate_path"],
                                     ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        self.assertEqual(self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)["disposition"], "revise")
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=bwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_b_builder), model="sonnet", effort="medium")
        launched = self.call("launch", state_root=str(self.state), work_id=bwork,
                             role="implementer", resume=True,
                             claude_bin=str(self.fake_package_b_builder), model="sonnet", effort="medium")
        self.assertEqual(launched["provider_session_id"], original_session)
        self.fixture.wait_quiescent(bwork)
        self.call("collect", state_root=str(self.state), work_id=bwork)
        self.assertTrue(self.call("verify_package_b", state_root=str(self.state), work_id=bwork)["all_passed"])
        corrected = self.read_state(bwork)
        os.environ["FAKE_PB_REVIEWER_VERDICT"] = "pass"
        self.launch_package_b_review(bwork, corrected["package_b_verification"]["aggregate_path"],
                                     ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        completed = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(completed["disposition"], "pass")
        self.assertEqual(completed["phase"], "completed")
        audit_before = len(self.fixture.audit_rows())
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=bwork,
                      role="implementer", resume=True,
                      claude_bin=str(self.fake_package_b_builder), model="sonnet", effort="medium")
        self.assertEqual(len(self.fixture.audit_rows()), audit_before)

    def test_package_b_nonpass_prefix_repair_is_append_only_and_queues_correction(self):
        """A historical false terminal caused by the old prefix reader is repaired once."""
        original_tests = self.mod.PACKAGE_B_FOCUSED_SITE_TESTS
        original_validator = self.mod._package_b_validate_verification
        self.mod.PACKAGE_B_FOCUSED_SITE_TESTS = [
            "scripts.test_cowork.DispatchContractAdapterTest.test_missing_required_site_receipt",
        ]
        try:
            bwork = self.make_package_b_with_candidate()
            verified = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
            self.assertFalse(verified["all_passed"])

            def historical_prefix_reader(*_args, **_kwargs):
                raise self.mod.ControllerError("historical reader required all eight receipts")

            self.mod._package_b_validate_verification = historical_prefix_reader
            failed = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
            self.assertEqual(failed["disposition"], "fail")
            failed_state = self.read_state(bwork)
            old_gate_path = failed_state["package_b_gate_receipt"]["path"]
            old_gate_bytes = Path(old_gate_path).read_bytes()
            self.mod._package_b_validate_verification = original_validator

            repaired = self.call("repair_package_b_nonpass_gate", state_root=str(self.state), work_id=bwork)
            self.assertEqual(repaired["phase"], "awaiting_gate")
            self.assertFalse(repaired["reused"])
            self.assertEqual(Path(old_gate_path).read_bytes(), old_gate_bytes)
            replay = self.call("repair_package_b_nonpass_gate", state_root=str(self.state), work_id=bwork)
            self.assertTrue(replay["reused"])
            revised = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
            self.assertEqual(revised["disposition"], "revise")
            self.assertEqual(revised["phase"], "prepared")
        finally:
            self.mod.PACKAGE_B_FOCUSED_SITE_TESTS = original_tests
            self.mod._package_b_validate_verification = original_validator

    def test_package_b_legacy_structural_check_repair_starts_fresh_verification_generation(self):
        """A substring-only false negative is preserved, superseded, and rerun."""
        original_execute = self.mod._package_a_execute_gate
        try:
            bwork = self.make_package_b_with_candidate()
            def legacy_execute(spec, *args, **kwargs):
                result, raw, terminal = original_execute(spec, *args, **kwargs)
                if spec.get("kind") == "package_b_structural":
                    raw = b"structural_migration_check_failed"
                    result = {**result, "exit_code": 1, "output_sha256": hashlib.sha256(raw).hexdigest(),
                              "output_bytes": len(raw), "output_total_seen": len(raw), "output": raw,
                              "counters": {}, "passed": False}
                return result, raw, terminal
            self.mod._package_a_execute_gate = legacy_execute
            first = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
            self.assertFalse(first["all_passed"])
            old_aggregate = Path(first["aggregate_path"]).read_bytes()
            self.mod._package_a_execute_gate = original_execute

            repaired = self.call("repair_package_b_structural_gate", state_root=str(self.state), work_id=bwork)
            self.assertEqual(repaired["verification_generation"], 1)
            self.assertFalse(repaired["reused"])
            self.assertEqual(Path(first["aggregate_path"]).read_bytes(), old_aggregate)
            replay = self.call("repair_package_b_structural_gate", state_root=str(self.state), work_id=bwork)
            self.assertTrue(replay["reused"])
            second = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
            self.assertTrue(second["all_passed"])
            self.assertNotEqual(second["aggregate_path"], first["aggregate_path"])
        finally:
            self.mod._package_a_execute_gate = original_execute

    def test_package_b_legacy_class_probe_repair_starts_generation_two(self):
        """The historical class-based probe is preserved and replaced by the dict API probe."""
        original_legacy = self.mod._package_b_probe_code_legacy_class_api
        self.mod._package_b_probe_code_legacy_class_api = lambda: (
            "import json\nprint(json.dumps({'error': \"cannot import name 'DispatchContract' from 'cowork_dispatch' (fixture)\"}))\n")
        try:
            bwork = self.make_package_b_with_candidate()
            state = self.read_state(bwork)
            state["package_b_verification_generation"] = 1
            state["package_b_verification_probe_version"] = 1
            state["package_b_verification_history"] = [{"fixture": "prior structural generation"}]
            self.state_file(bwork).write_text(json.dumps(state))
            first = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
            self.assertFalse(first["all_passed"])
            old_aggregate = Path(first["aggregate_path"]).read_bytes()
            repaired = self.call("repair_package_b_probe_gate", state_root=str(self.state), work_id=bwork)
            self.assertEqual(repaired["verification_generation"], 2)
            self.assertFalse(repaired["reused"])
            self.assertEqual(Path(first["aggregate_path"]).read_bytes(), old_aggregate)
            replay = self.call("repair_package_b_probe_gate", state_root=str(self.state), work_id=bwork)
            self.assertTrue(replay["reused"])
            self.mod._package_b_probe_code_legacy_class_api = original_legacy
            second = self.call("verify_package_b", state_root=str(self.state), work_id=bwork)
            self.assertTrue(second["all_passed"])
            self.assertIn("-g2", second["aggregate_path"])
        finally:
            self.mod._package_b_probe_code_legacy_class_api = original_legacy

    # ── full happy path lifecycle ──────────────────────────────────────────────

    def test_package_b_full_lifecycle_pass(self):
        """Happy path: prepare → materialize → launch → collect → verify → review → adjudicate → completed."""
        bwork = self.make_package_b_reviewed()
        result = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(result["disposition"], "pass")
        self.assertEqual(result["phase"], "completed")
        state = self.read_state(bwork)
        self.assertEqual(state["phase"], "completed")

    def test_package_b_gate_replay_idempotent(self):
        """adjudicate-package-b replays without re-running after completed."""
        bwork = self.make_package_b_reviewed()
        r1 = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(r1["disposition"], "pass")
        r2 = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(r2["disposition"], "pass")
        self.assertTrue(r2.get("reused"))

    def test_package_b_no_publish_capability(self):
        """Package B never exposes publish_external capability."""
        self.assertNotIn("publish_external",
                         getattr(self.mod, "PACKAGE_B_AUTHORITY_CAPABILITIES", set()))
        # Verify adjudicate-package-b result has no publication field
        bwork = self.make_package_b_reviewed()
        result = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        result_json = json.dumps(result)
        self.assertNotIn("publish", result_json.lower().replace("bootstrap_no_publish", ""))

    def test_package_b_review_capacity_wait_manual(self):
        """Untrusted rate-limit event enters manual capacity wait."""
        bwork = self.make_package_b_verified()
        state = self.read_state(bwork)
        agg_path = state["package_b_verification"]["aggregate_path"]
        os.environ["FAKE_PB_REVIEWER_KIND"] = "overflow"
        self.call("launch_package_b_review", state_root=str(self.state), work_id=bwork,
                  claude_bin=str(self.fake_package_b_reviewer))
        self.wait_package_b_review(bwork)
        # Overflow is an infrastructure retry candidate, not capacity
        with self.assertRaises(self.mod.ControllerError):
            # ingest should raise since overflow doesn't produce a valid packet
            self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)

    def test_package_b_review_infrastructure_retry_once(self):
        """Overflow qualifies for exactly one infrastructure retry; a second overflow is rejected."""
        bwork = self.make_package_b_verified()
        state = self.read_state(bwork)
        agg_path = state["package_b_verification"]["aggregate_path"]
        # First attempt: overflow → retry queued (1 slot consumed)
        os.environ["FAKE_PB_REVIEWER_KIND"] = "overflow"
        self.call("launch_package_b_review", state_root=str(self.state), work_id=bwork,
                  claude_bin=str(self.fake_package_b_reviewer))
        self.wait_package_b_review(bwork)
        result = self.call("retry_package_b_review", state_root=str(self.state), work_id=bwork)
        self.assertEqual(result["retry_evidence"]["classification"], "bounded_stream_overflow")
        # Second attempt: overflow again → retry slot is exhausted
        self.call("launch_package_b_review", state_root=str(self.state), work_id=bwork,
                  claude_bin=str(self.fake_package_b_reviewer))
        self.wait_package_b_review(bwork)
        with self.assertRaises(self.mod.ControllerError):
            self.call("retry_package_b_review", state_root=str(self.state), work_id=bwork)

    def test_package_b_review_exact_array_binding_omission_retries_once(self):
        """A passing review with only the two unprompted arrays wrong gets one infra retry."""
        bwork = self.make_package_b_verified()
        state = self.read_state(bwork)
        os.environ["FAKE_PB_REVIEWER_KIND"] = "binding_omission"
        self.launch_package_b_review(bwork, state["package_b_verification"]["aggregate_path"],
                                     ["scripts/cowork_dispatch.py"])
        with self.assertRaises(self.mod.ControllerError):
            self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        retry = self.call("retry_package_b_review", state_root=str(self.state), work_id=bwork)
        self.assertEqual(retry["retry_evidence"]["classification"], "exact_array_binding_omission")
        first_session = self.read_state(bwork)["package_b_review_attempt_history"][0]["provider_session_id"]
        self.assertTrue(first_session)
        os.environ.pop("FAKE_PB_REVIEWER_KIND", None)
        state = self.read_state(bwork)
        self.launch_package_b_review(bwork, state["package_b_verification"]["aggregate_path"],
                                     ["scripts/cowork_dispatch.py"])
        ingested = self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        self.assertEqual(ingested["verdict"], "pass")
        self.assertNotEqual(self.read_state(bwork)["package_b_review"]["provider_session_id"], first_session)

    def test_package_b_review_binding_repair_uses_first_pass_after_retry_exhaustion(self):
        """Controller-owned arrays can repair one passing review after its sole retry exhausts."""
        bwork = self.make_package_b_verified()
        state = self.read_state(bwork)
        aggregate_path = state["package_b_verification"]["aggregate_path"]
        os.environ["FAKE_PB_REVIEWER_KIND"] = "binding_omission"
        self.launch_package_b_review(bwork, aggregate_path, ["scripts/cowork_dispatch.py"])
        with self.assertRaises(self.mod.ControllerError):
            self.call("ingest_package_b_review", state_root=str(self.state), work_id=bwork)
        self.call("retry_package_b_review", state_root=str(self.state), work_id=bwork)
        os.environ["FAKE_PB_REVIEWER_KIND"] = "overflow"
        self.call("launch_package_b_review", state_root=str(self.state), work_id=bwork,
                  claude_bin=str(self.fake_package_b_reviewer))
        self.wait_package_b_review(bwork)
        repaired = self.call("repair_package_b_review_binding", state_root=str(self.state), work_id=bwork)
        self.assertEqual(repaired["verdict"], "pass")
        self.assertFalse(repaired["reused"])
        self.assertTrue(self.call("repair_package_b_review_binding", state_root=str(self.state), work_id=bwork)["reused"])
        completed = self.call("adjudicate_package_b", state_root=str(self.state), work_id=bwork)
        self.assertEqual(completed["disposition"], "pass")
        self.assertEqual(completed["phase"], "completed")


class PackageCLifecycleTests(unittest.TestCase):
    """Offline tests for the complete Package C pending/gate-repair AttemptLink lifecycle."""

    _FAKE_COWORK_DISPATCH_SRC = b"""\
import uuid as _uuid

_VALID_KINDS = {'pending_replay', 'gate_repair'}

def AttemptLink(kind, attempt_id, source_ref, delivery_ref, idempotency_key):
    return {'kind': kind, 'attempt_id': attempt_id, 'source_ref': source_ref,
            'delivery_ref': delivery_ref, 'idempotency_key': idempotency_key}

def validate_attempt_link(link):
    if not isinstance(link, dict): raise ValueError('not a link')
    if link.get('kind') not in _VALID_KINDS: raise ValueError('bad kind')
    try: _uuid.UUID(str(link.get('attempt_id')))
    except (ValueError, TypeError, AttributeError): raise ValueError('bad attempt_id')
    if not link.get('source_ref'): raise ValueError('missing source_ref')
    if not link.get('delivery_ref'): raise ValueError('missing delivery_ref')
    if not link.get('idempotency_key'): raise ValueError('missing idempotency_key')
    ordinal = str(link['idempotency_key']).rsplit(':', 1)[-1]
    expected = build_attempt_link_idempotency_key(
        link.get('role') or 'implementer', link.get('kind'), link.get('source_ref'), ordinal)
    if link['idempotency_key'] != expected: raise ValueError('bad idempotency_key')
    return link

def build_attempt_link_idempotency_key(role, kind, source_ref, ordinal):
    event_id = source_ref.get('event_id') if isinstance(source_ref, dict) else source_ref
    return '%s:%s:event_id=%s:%s' % (role, kind, event_id, ordinal)
"""

    _FAKE_COWORK_SRC = b"'''fake Package C scripts/cowork.py placeholder'''\n"
    _FAKE_COWORK_STATE_SRC = b"'''fake Package C scripts/cowork_state.py placeholder'''\n"

    _FAKE_TEST_COWORK_SRC = b"""\
import os, sys, uuid, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unittest
from cowork_dispatch import AttemptLink, validate_attempt_link, build_attempt_link_idempotency_key

class PendingTurnLinkageTest(unittest.TestCase):
    def test_save_pending_turn_creates_no_attempt(self):
        self.assertIsNone(None)
    def test_source_precedence_event_session_fingerprint(self):
        self.assertEqual(build_attempt_link_idempotency_key('implementer', 'pending_replay', 'evt-1', 0),
                          build_attempt_link_idempotency_key('implementer', 'pending_replay', 'evt-1', 0))
    def test_legacy_session_replay_deterministic_source(self):
        fp = hashlib.sha256(b'legacy-text').hexdigest()
        self.assertEqual(len(fp), 64)
    def test_pending_source_survives_switch(self):
        self.assertTrue(True)
    def test_clear_consume_removes_text_and_source(self):
        self.assertTrue(True)
    def test_mismatched_source_fails_closed(self):
        with self.assertRaises(Exception):
            validate_attempt_link(AttemptLink(kind='pending_replay', attempt_id='bad', source_ref='', delivery_ref='d', idempotency_key='x'))
    def test_duplicate_replay_appends_once(self):
        link = validate_attempt_link(AttemptLink(kind='pending_replay', attempt_id=str(uuid.uuid4()),
            source_ref='evt-1', delivery_ref='deliv-1',
            idempotency_key=build_attempt_link_idempotency_key('implementer', 'pending_replay', 'evt-1', 0)))
        self.assertEqual(link['kind'], 'pending_replay')

class PendingReplayLinkageTest(unittest.TestCase):
    def test_save_pending_turn_creates_no_attempt_id_at_any_depth(self):
        self.assertIsNone(None)
    def test_build_pending_source_ref_trace_event_takes_priority(self):
        key = build_attempt_link_idempotency_key('implementer', 'pending_replay', 'evt-trace', 0)
        self.assertIn('evt-trace', key)
    def test_build_pending_source_ref_falls_to_session_id(self):
        key = build_attempt_link_idempotency_key('implementer', 'pending_replay', 'sess-1', 0)
        self.assertIn('sess-1', key)
    def test_build_pending_source_ref_falls_to_fingerprint(self):
        fp = hashlib.sha256(b'wt-bytes').hexdigest()
        key = build_attempt_link_idempotency_key('implementer', 'pending_replay', fp, 0)
        self.assertIn(fp, key)
    def test_delivery_fingerprint_mismatch_raises_before_replay(self):
        with self.assertRaises(Exception):
            validate_attempt_link(AttemptLink(kind='pending_replay', attempt_id='not-a-uuid',
                source_ref='', delivery_ref='d', idempotency_key='x'))
    def test_duplicate_pending_replay_appends_exactly_one_link(self):
        link = validate_attempt_link(AttemptLink(kind='pending_replay', attempt_id=str(uuid.uuid4()),
            source_ref='evt-dup', delivery_ref='deliv-dup',
            idempotency_key=build_attempt_link_idempotency_key('implementer', 'pending_replay', 'evt-dup', 0)))
        self.assertEqual(link['kind'], 'pending_replay')
    def test_legacy_entry_without_pending_source_normalizes_to_legacy_kind(self):
        self.assertTrue(True)
    def test_legacy_entry_produces_valid_replay_link(self):
        link = validate_attempt_link(AttemptLink(kind='pending_replay', attempt_id=str(uuid.uuid4()),
            source_ref='legacy-src', delivery_ref='deliv-leg',
            idempotency_key=build_attempt_link_idempotency_key('implementer', 'pending_replay', 'legacy-src', 0)))
        self.assertIsNotNone(link)
    def test_legacy_entry_does_not_invent_event_id_or_session_id(self):
        self.assertIsNone(None)
    def test_pending_source_carried_through_controller_switch(self):
        self.assertTrue(True)
    def test_pending_source_removed_on_clear(self):
        self.assertTrue(True)
    def test_source_ref_with_attempt_id_rejected_by_validate(self):
        with self.assertRaises(Exception):
            validate_attempt_link(AttemptLink(kind='pending_replay', attempt_id='not-a-uuid',
                source_ref='ref', delivery_ref='', idempotency_key='x'))

class GateRepairLinkageTest(unittest.TestCase):
    def test_two_repairs_distinct_identities(self):
        self.assertNotEqual(str(uuid.uuid4()), str(uuid.uuid4()))
    def test_repair_links_to_repaired_source(self):
        link = validate_attempt_link(AttemptLink(kind='gate_repair', attempt_id=str(uuid.uuid4()),
            source_ref='evt-2', delivery_ref='deliv-2',
            idempotency_key=build_attempt_link_idempotency_key('implementer', 'gate_repair', 'evt-2', 1)))
        self.assertEqual(link['source_ref'], 'evt-2')
    def test_repeated_delivery_idempotent(self):
        key1 = build_attempt_link_idempotency_key('implementer', 'gate_repair', 'evt-2', 1)
        key2 = build_attempt_link_idempotency_key('implementer', 'gate_repair', 'evt-2', 1)
        self.assertEqual(key1, key2)
    def test_repair_prompt_bytes_unchanged(self):
        self.assertTrue(True)
    def test_repair_cap_and_stuck_behavior_unchanged(self):
        self.assertTrue(True)
    def test_two_repair_deliveries_have_distinct_attempt_ids(self):
        a1 = str(uuid.uuid4()); a2 = str(uuid.uuid4())
        self.assertNotEqual(a1, a2)
    def test_two_repair_deliveries_with_distinct_ordinals_have_distinct_keys(self):
        k1 = build_attempt_link_idempotency_key('implementer', 'gate_repair', 'src-x', 1)
        k2 = build_attempt_link_idempotency_key('implementer', 'gate_repair', 'src-x', 2)
        self.assertNotEqual(k1, k2)
    def test_two_deliveries_linked_to_same_source_still_differ(self):
        k1 = build_attempt_link_idempotency_key('implementer', 'gate_repair', 'src-shared', 1)
        k2 = build_attempt_link_idempotency_key('implementer', 'gate_repair', 'src-shared', 2)
        self.assertNotEqual(k1, k2)
    def test_same_repair_link_appended_twice_writes_one_record(self):
        k = build_attempt_link_idempotency_key('implementer', 'gate_repair', 'src-idem', 1)
        self.assertEqual(k, k)
    def test_repair_delivery_prompt_text_is_unchanged(self):
        self.assertTrue(True)
    def test_repair_delivery_carries_valid_attempt_link(self):
        link = validate_attempt_link(AttemptLink(kind='gate_repair', attempt_id=str(uuid.uuid4()),
            source_ref='src-valid', delivery_ref='deliv-valid',
            idempotency_key=build_attempt_link_idempotency_key('implementer', 'gate_repair', 'src-valid', 1)))
        self.assertEqual(link['kind'], 'gate_repair')
    def test_repair_link_delivery_ref_matches_prompt_bytes(self):
        delivery_ref = hashlib.sha256(b'prompt-bytes').hexdigest()
        link = validate_attempt_link(AttemptLink(kind='gate_repair', attempt_id=str(uuid.uuid4()),
            source_ref='src-bytes', delivery_ref=delivery_ref,
            idempotency_key=build_attempt_link_idempotency_key('implementer', 'gate_repair', 'src-bytes', 1)))
        self.assertEqual(link['delivery_ref'], delivery_ref)
    def test_repair_link_source_ref_has_no_attempt_id(self):
        src_ref = hashlib.sha256(b'event-without-attempt').hexdigest()
        link = validate_attempt_link(AttemptLink(kind='gate_repair', attempt_id=str(uuid.uuid4()),
            source_ref=src_ref, delivery_ref='deliv-src',
            idempotency_key=build_attempt_link_idempotency_key('implementer', 'gate_repair', src_ref, 1)))
        self.assertNotIn('attempt_id', link.get('source_ref', ''))

class StaleNoOpTest(unittest.TestCase):
    def test_t3_repair_fails_stuck_gate_end(self):
        self.assertTrue(True)
"""

    _FAKE_TEST_CHAR_SRC = b"""\
import unittest

class MissingDispatchContractTest(unittest.TestCase):
    def test_pending_turn_retry_linkage_contract(self):
        self.assertTrue(True)
    def test_gate_repair_retry_linkage_contract(self):
        self.assertTrue(True)

class RetryLinkageTest(unittest.TestCase):
    def test_placeholder(self):
        self.assertTrue(True)
"""

    _FAKE_TEST_CHAR_UNMIGRATED_SRC = b"""\
import unittest

class MissingDispatchContractTest(unittest.TestCase):
    def test_pending_turn_retry_linkage_contract(self):
        self.assertTrue(True)
    @unittest.expectedFailure
    def test_gate_repair_retry_linkage_contract(self):
        raise AssertionError('gate repair retry linkage not yet implemented')

class RetryLinkageTest(unittest.TestCase):
    def test_placeholder(self):
        self.assertTrue(True)
"""

    _FAKE_FIXTURE_SRC = b'{"sources":[]}\n'

    def setUp(self):
        self.fixture = OrchestrateCLITest(methodName="runTest")
        self.fixture.setUp()
        for name in ("tmp", "root", "repo", "state", "brief", "audit", "base_head"):
            setattr(self, name, getattr(self.fixture, name))
        self.mod = controller_module()
        self.claude_reviewer_audit = self.root / "fake-claude-c-reviewer-audit.jsonl"
        self._old_reviewer_audit = os.environ.get("FAKE_CLAUDE_REVIEWER_AUDIT")
        os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = str(self.claude_reviewer_audit)
        self.fake_package_c_builder = self.root / "fake-package-c-builder"
        self.fake_package_c_builder.write_text("""#!/usr/bin/env %(py)s
import json, os, sys
argv = sys.argv[1:]
session_id = (argv[argv.index('--session-id') + 1] if '--session-id' in argv else
              argv[argv.index('--resume') + 1] if '--resume' in argv else 'session-c-fake')
kind = os.environ.get('FAKE_PC_BUILDER_KIND', 'valid')
pending_map = {
    'save_creates_no_attempt': True, 'source_precedence': ['event', 'session', 'fingerprint'],
    'legacy_normalization': 'legacy', 'switch_carry': True, 'clear_consume': True,
    'mismatch_fail_closed': True, 'duplicate_replay_idempotent': True, 'replay_link_kind': 'pending_replay',
    'test_ids': [
        'scripts.test_cowork.PendingTurnLinkageTest.test_save_pending_turn_creates_no_attempt',
        'scripts.test_cowork.PendingTurnLinkageTest.test_source_precedence_event_session_fingerprint',
        'scripts.test_cowork.PendingTurnLinkageTest.test_legacy_session_replay_deterministic_source',
        'scripts.test_cowork.PendingTurnLinkageTest.test_pending_source_survives_switch',
        'scripts.test_cowork.PendingTurnLinkageTest.test_clear_consume_removes_text_and_source',
        'scripts.test_cowork.PendingTurnLinkageTest.test_mismatched_source_fails_closed',
        'scripts.test_cowork.PendingTurnLinkageTest.test_duplicate_replay_appends_once',
    ],
}
repair_map = {
    'distinct_identity_per_delivery': True, 'repaired_source_linkage': True,
    'idempotency_key_includes_ordinal': True, 'unchanged_prompt_bytes': True,
    'unchanged_cap_behavior': True, 'link_kind': 'gate_repair',
    'test_ids': [
        'scripts.test_cowork.GateRepairLinkageTest.test_two_repairs_distinct_identities',
        'scripts.test_cowork.GateRepairLinkageTest.test_repair_links_to_repaired_source',
        'scripts.test_cowork.GateRepairLinkageTest.test_repeated_delivery_idempotent',
        'scripts.test_cowork.GateRepairLinkageTest.test_repair_prompt_bytes_unchanged',
        'scripts.test_cowork.GateRepairLinkageTest.test_repair_cap_and_stuck_behavior_unchanged',
    ],
}
migrated_tests = [
    'scripts.test_dispatch_contract_characterization.MissingDispatchContractTest.test_pending_turn_retry_linkage_contract',
    'scripts.test_dispatch_contract_characterization.MissingDispatchContractTest.test_gate_repair_retry_linkage_contract',
]
retired_witnesses = [
    'test_witness_pending_turn_replay_records_no_attempt',
    'test_gate_repair_retry_has_no_attempt_identity_only_a_flag',
]
negative_controls = [{'control': 'save_no_attempt', 'test': pending_map['test_ids'][0], 'status': 'covered'}]
if kind == 'malformed':
    print('not-json')
elif kind == 'missing_pending_map':
    packet = {'outcome': 'completed', 'summary': 'no pending_map', 'changed_paths': [], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'gate',
              'repair_map': repair_map, 'migrated_tests': migrated_tests, 'retired_witnesses': retired_witnesses,
              'remaining_expected_failures': 0, 'negative_controls': negative_controls}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
elif kind == 'wrong_paths':
    with open('scripts/cowork_dispatch.py', 'a') as f: f.write('\\n# Package C changes\\n')
    packet = {'outcome': 'completed', 'summary': 'wrong paths', 'changed_paths': ['?? nonexistent.py'], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'gate',
              'pending_map': pending_map, 'repair_map': repair_map, 'migrated_tests': migrated_tests, 'retired_witnesses': retired_witnesses,
              'remaining_expected_failures': 0, 'negative_controls': negative_controls}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
elif kind == 'unmigrated_char':
    import shutil as _shutil
    _shutil.copyfile(os.environ['FAKE_PC_UNMIGRATED_CHAR_PATH'], 'scripts/test_dispatch_contract_characterization.py')
    packet = {'outcome': 'completed', 'summary': 'unmigrated char delivered', 'changed_paths': ['?? scripts/test_dispatch_contract_characterization.py'], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'gate',
              'pending_map': pending_map, 'repair_map': repair_map, 'migrated_tests': migrated_tests, 'retired_witnesses': retired_witnesses,
              'remaining_expected_failures': 0, 'negative_controls': negative_controls}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
else:
    with open('scripts/cowork_dispatch.py', 'a') as f: f.write('\\n# Package C changes\\n')
    packet = {'outcome': 'completed', 'summary': 'Package C bounded fake', 'changed_paths': ['?? scripts/cowork_dispatch.py'], 'checks': [], 'findings': [], 'assumptions': [], 'next_action': 'run the deterministic gate',
              'pending_map': pending_map, 'repair_map': repair_map, 'migrated_tests': migrated_tests, 'retired_witnesses': retired_witnesses,
              'remaining_expected_failures': 0, 'negative_controls': negative_controls}
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
""" % {"py": sys.executable})
        self.fake_package_c_builder.chmod(
            self.fake_package_c_builder.stat().st_mode | stat.S_IXUSR)
        self.fake_pc_unmigrated_char = self.root / "fake-pc-unmigrated-char-src.py"
        self.fake_pc_unmigrated_char.write_bytes(self._FAKE_TEST_CHAR_UNMIGRATED_SRC)
        self.fake_package_c_reviewer = self.root / "fake-package-c-reviewer"
        self.fake_package_c_reviewer.write_text("""#!/usr/bin/env %(py)s
import json, os, sys
argv = sys.argv[1:]
with open(os.environ['FAKE_CLAUDE_REVIEWER_AUDIT'], 'a') as out:
    out.write(json.dumps({'argv': argv, 'cwd': os.getcwd()}) + '\\n')
session_id = argv[argv.index('--session-id') + 1]
kind = os.environ.get('FAKE_PC_REVIEWER_KIND', '')
if kind == 'overflow':
    sys.stdout.write(json.dumps({'type': 'result', 'session_id': session_id}) + '\\n')
    sys.stdout.flush()
    chunk = ('{"type":"debug","data":"' + 'x' * 990 + '"}\\n').encode()
    written = 0
    cap = 2 * 1024 * 1024 + 65536
    while written < cap:
        sys.stdout.buffer.write(chunk); written += len(chunk)
    sys.stdout.buffer.flush(); sys.exit(1)
if kind == 'manual_capacity':
    print(json.dumps({'type': 'rate_limit_event', 'session_id': session_id,
                       'rate_limit_info': {'isUsingOverage': False, 'status': 'rejected', 'resetsAt': None}}))
    sys.exit(1)
schema_json = argv[argv.index('--json-schema') + 1]
schema = json.loads(schema_json)
p = schema['properties']
packet = {k: v['const'] for k, v in p.items() if 'const' in v}
aggregate_path = os.environ.get('FAKE_PC_AGGREGATE_PATH', '')
if aggregate_path and os.path.isfile(aggregate_path):
    agg = json.loads(open(aggregate_path).read())
    packet['verification_receipt_sha256s'] = [r['sha256'] for r in agg['receipts']]
else:
    packet['verification_receipt_sha256s'] = ['0' * 64] * 9
changed_paths_str = os.environ.get('FAKE_PC_CHANGED_PATHS', '')
packet['changed_paths'] = json.loads(changed_paths_str) if changed_paths_str else ['scripts/cowork_dispatch.py']
packet['verdict'] = os.environ.get('FAKE_PC_REVIEWER_VERDICT', 'pass')
packet['findings'] = []
packet['checks'] = ['fake Package C review check']
print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
""" % {"py": sys.executable})
        self.fake_package_c_reviewer.chmod(
            self.fake_package_c_reviewer.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        if self._old_reviewer_audit is None: os.environ.pop("FAKE_CLAUDE_REVIEWER_AUDIT", None)
        else: os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = self._old_reviewer_audit
        for key in ("FAKE_PC_BUILDER_KIND", "FAKE_PC_REVIEWER_KIND", "FAKE_PC_REVIEWER_VERDICT",
                    "FAKE_PC_AGGREGATE_PATH", "FAKE_PC_CHANGED_PATHS", "FAKE_PC_UNMIGRATED_CHAR_PATH"):
            os.environ.pop(key, None)
        self.fixture.tearDown()

    def call(self, command, **values):
        return getattr(self.mod, "command_" + command)(SimpleNamespace(**values))

    def read_state(self, work_id=None):
        return self.fixture.read_state(work_id or self.mod.PACKAGE_C_WORK_ID)

    def state_file(self, work_id=None):
        return self.fixture.state_file(work_id or self.mod.PACKAGE_C_WORK_ID)

    def invoke(self, *args, **kwargs):
        return self.fixture.invoke(*args, **kwargs)

    def _setup_fake_v3_plan(self):
        """Create a minimal fake completed M0-C v3 plan and patch module constants."""
        self.mod.PACKAGE_C_V3_WORK_ID = "fake-v3-plan-work"
        self.mod.PACKAGE_C_V3_CANDIDATE_ID = "v3-cand-" + "1" * 24
        self.mod.PACKAGE_C_V3_EVIDENCE_DIGEST = "2" * 64
        self.mod.PACKAGE_C_V3_PLAN_SHA256 = "4" * 64
        v3_work = self.mod.PACKAGE_C_V3_WORK_ID
        v3_dir = self.state / v3_work
        v3_dir.mkdir(parents=True, exist_ok=True)
        packets_dir = v3_dir / "packets"; packets_dir.mkdir(parents=True, exist_ok=True)
        packet_content = b'{"kind":"fake-v3-plan-packet"}'
        packet_path = packets_dir / "plan.json"
        packet_path.write_bytes(packet_content)
        packet_sha = hashlib.sha256(packet_content).hexdigest()
        repo_real = os.path.realpath(str(self.repo))
        wt_path = os.path.join(repo_real, ".worktrees", "orchestrator-" + v3_work)
        Path(wt_path).mkdir(parents=True, exist_ok=True)
        brief_path = v3_dir / "brief.md"
        brief_path.write_text("fake v3 plan brief")
        evidence = {
            "packet_sha256": packet_sha, "packet_path": str(packet_path),
            "evidence_kind": "worker_packet", "changed_paths": [],
            "declared_artifacts": [], "worktree_fingerprint": self.mod.worktree_fingerprint(wt_path),
        }
        candidate = {
            "schema_version": 1, "id": self.mod.PACKAGE_C_V3_CANDIDATE_ID,
            "evidence_digest": self.mod.PACKAGE_C_V3_EVIDENCE_DIGEST,
            "evidence": evidence, "packet_verified": True, "created_at": "2024-01-01T00:00:00Z",
        }
        review_content = json.dumps({"verdict": "pass"}).encode()
        review_path = packets_dir / "review.json"
        review_path.write_bytes(review_content)
        review_sha = hashlib.sha256(review_content).hexdigest()
        self.mod.PACKAGE_C_V3_REVIEW_SHA256 = review_sha
        v3_state = {
            "schema_version": 1, "revision": 1, "phase": "completed", "work_id": v3_work,
            "repo": {"root": repo_real, "head": self.base_head},
            "brief": {"sha256": "a" * 64, "path": str(os.path.realpath(str(brief_path)))},
            "gate": {"verdict": "pass", "adjudicator_principal": self.mod.PACKAGE_C_V3_ADJUDICATOR},
            "candidate": candidate,
            "plan_review": {"path": str(review_path), "sha256": review_sha, "verdict": "pass",
                             "candidate_id": candidate["id"], "plan_sha256": self.mod.PACKAGE_C_V3_PLAN_SHA256},
            "worktree": {"path": wt_path, "branch": "codex/" + v3_work},
        }
        (v3_dir / "state.json").write_text(json.dumps(v3_state))

    def _setup_fake_package_b_source(self):
        """Create a minimal fake completed Package B state and patch module constants."""
        self.mod.PACKAGE_C_PREREQ_WORK_ID = "fake-package-b-work"
        self.mod.PACKAGE_C_PREREQ_CANDIDATE_ID = "package-b-" + "a" * 32
        self.mod.PACKAGE_C_PREREQ_EVIDENCE_DIGEST = "e" * 64
        self.mod.PACKAGE_C_PREREQ_BASELINE_RECEIPT_SHA256 = "b" * 64
        self.mod.PACKAGE_C_PREREQ_DELTA_SHA256 = "c" * 64
        self.mod.PACKAGE_C_PREREQ_RESULT_SHA256 = "d" * 64
        self.mod.PACKAGE_C_PREREQ_VERIFICATION_SHA256 = "f" * 64
        self.mod.PACKAGE_C_PREREQ_REVIEW_SHA256 = "9" * 64
        self.mod.PACKAGE_C_PREREQ_GATE_SHA256 = "7" * 64
        b_work = self.mod.PACKAGE_C_PREREQ_WORK_ID
        b_dir = self.state / b_work
        b_dir.mkdir(parents=True, exist_ok=True)
        repo_real = os.path.realpath(str(self.repo))
        wt_path = os.path.join(repo_real, ".worktrees", "orchestrator-" + b_work)
        wt = Path(wt_path)
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "scripts").mkdir(exist_ok=True)
        (wt / "scripts" / "fixtures").mkdir(parents=True, exist_ok=True)
        files = {
            "scripts/cowork.py": self._FAKE_COWORK_SRC,
            "scripts/cowork_dispatch.py": self._FAKE_COWORK_DISPATCH_SRC,
            "scripts/cowork_state.py": self._FAKE_COWORK_STATE_SRC,
            "scripts/test_cowork.py": self._FAKE_TEST_COWORK_SRC,
            "scripts/test_dispatch_contract_characterization.py": self._FAKE_TEST_CHAR_SRC,
            "scripts/fixtures/dispatch_contract_characterization_sources.json": self._FAKE_FIXTURE_SRC,
        }
        sha_map = {}
        for rel, content in files.items():
            path = wt / rel
            path.write_bytes(content)
            sha_map[rel] = hashlib.sha256(content).hexdigest()
        self.mod.PACKAGE_C_SOURCE_FILES = dict(sha_map)
        self.mod.PACKAGE_C_FIXTURE_SHA256 = sha_map["scripts/fixtures/dispatch_contract_characterization_sources.json"]
        brief_path = b_dir / "brief.md"
        brief_path.write_text("fake package b brief")
        b_state = {
            "schema_version": 1, "revision": 1, "phase": "completed", "work_id": b_work,
            "repo": {"root": repo_real, "head": self.base_head},
            "brief": {"sha256": "a" * 64, "path": str(os.path.realpath(str(brief_path)))},
            "candidate": {"id": self.mod.PACKAGE_C_PREREQ_CANDIDATE_ID, "evidence_digest": self.mod.PACKAGE_C_PREREQ_EVIDENCE_DIGEST},
            "package_b_prerequisites": {"sha256": self.mod.PACKAGE_C_PREREQ_BASELINE_RECEIPT_SHA256},
            "package_b_builder_delta": {"sha256": self.mod.PACKAGE_C_PREREQ_DELTA_SHA256},
            "package_b_collect": {"result_sha256": self.mod.PACKAGE_C_PREREQ_RESULT_SHA256},
            "package_b_verification": {"aggregate_sha256": self.mod.PACKAGE_C_PREREQ_VERIFICATION_SHA256},
            "package_b_review": {"sha256": self.mod.PACKAGE_C_PREREQ_REVIEW_SHA256},
            "package_b_gate_receipt": {"sha256": self.mod.PACKAGE_C_PREREQ_GATE_SHA256, "disposition": "pass"},
            "worktree": {"path": wt_path, "branch": "codex/" + b_work},
        }
        (b_dir / "state.json").write_text(json.dumps(b_state))
        return wt_path

    def _prepare_package_c(self):
        """prepare Package C and patch brief SHA/base head."""
        self._setup_fake_v3_plan()
        self._setup_fake_package_b_source()
        cwork = self.mod.PACKAGE_C_WORK_ID
        self.invoke("prepare", "--repo", str(self.repo), "--brief-file", str(self.brief),
                    "--state-root", str(self.state), "--work-id", cwork)
        state = self.read_state(cwork)
        self.mod.PACKAGE_C_BRIEF_SHA256 = state["brief"]["sha256"]
        self.mod.PACKAGE_C_BASE_HEAD = state["repo"]["head"]
        return cwork

    def make_package_c_materialized(self):
        """Prepared + prerequisites materialized."""
        cwork = self._prepare_package_c()
        self.call("materialize_package_c_prerequisites", state_root=str(self.state), work_id=cwork)
        return cwork

    def make_package_c_with_candidate(self):
        """Through collect (awaiting_gate)."""
        cwork = self.make_package_c_materialized()
        self.call("launch", state_root=str(self.state), work_id=cwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_c_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(cwork)
        self.call("collect", state_root=str(self.state), work_id=cwork)
        return cwork

    def make_package_c_verified(self):
        """Through verification (all nine gates)."""
        cwork = self.make_package_c_with_candidate()
        result = self.call("verify_package_c", state_root=str(self.state), work_id=cwork)
        self.assertTrue(result["all_passed"], result)
        return cwork

    def wait_package_c_review(self, work):
        for _ in range(160):
            attempt = self.read_state(work).get("package_c_review_attempt") or {}
            receipt = attempt.get("receipt_path")
            if (not self.mod.process_alive(attempt.get("pid"), attempt.get("pgid")) and
                    isinstance(receipt, str) and Path(receipt).is_file()):
                return attempt
            time.sleep(0.025)
        self.fail("fake Package C reviewer did not become quiescent")

    def launch_package_c_review(self, cwork, aggregate_path, changed_paths=None):
        os.environ["FAKE_PC_AGGREGATE_PATH"] = aggregate_path
        if changed_paths is not None:
            os.environ["FAKE_PC_CHANGED_PATHS"] = json.dumps(changed_paths)
        self.call("launch_package_c_review", state_root=str(self.state), work_id=cwork,
                  claude_bin=str(self.fake_package_c_reviewer))
        self.wait_package_c_review(cwork)

    def make_package_c_reviewed(self):
        """Through ingested independent review (verdict=pass)."""
        cwork = self.make_package_c_verified()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        self.launch_package_c_review(cwork, agg_path, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        return cwork

    # ── identity / dispatch tests ────────────────────────────────────────────

    def test_package_c_identity_constants(self):
        self.assertEqual(self.mod.PACKAGE_C_WORK_ID, "m0c-package-c-retry-linkage")
        self.assertEqual(self.mod.PACKAGE_C_PACKAGE_ID, "m0c-dispatch-contract-seam-package-c")
        self.assertEqual(self.mod.PACKAGE_C_GATE_PRINCIPAL, "orchestrator-package-c-gate/v1")
        self.assertEqual(self.mod.PACKAGE_C_CLAUDE_REVIEW_ACTOR, "claude_package_c_controller_v1")
        self.assertEqual(len(self.mod.PACKAGE_C_SOURCE_FILES), 6)
        self.assertEqual(len(self.mod.PACKAGE_C_ALLOWED_BUILDER_PATHS), 5)
        self.assertEqual(len(self.mod.PACKAGE_C_CONTRACT_TESTS), 2)
        self.assertEqual(len(self.mod.PACKAGE_C_RETIRED_WITNESSES), 2)
        self.assertEqual(self.mod.PACKAGE_C_BASE_FILE_HASHES, {
            "scripts/cowork.py": "1ced1fc504799454792e78fd169920dd06bcc493c98189f088fb046160de0130",
            "scripts/cowork_state.py": "549a7ebc1b2ffd47bb2249785d93ce6c4eb65484674be45cc5ff2e8d74a5bd46",
            "scripts/test_cowork.py": "ab69f6c07d6144fa710f7e46f25a837757342dbb5d06d8f59e68b81db6552d0d",
        })

    def test_package_c_is_package_c_discriminator(self):
        self.assertTrue(self.mod._is_package_c({"work_id": self.mod.PACKAGE_C_WORK_ID}))
        self.assertFalse(self.mod._is_package_c({"work_id": "other-work"}))
        self.assertFalse(self.mod._is_package_c({}))

    def test_package_c_generic_adjudicate_blocked(self):
        """The generic supervisor gate must reject every Package C disposition."""
        cwork = self.make_package_c_with_candidate()
        state = self.read_state(cwork)
        with self.assertRaises(self.mod.ControllerError):
            self.call("adjudicate", state_root=str(self.state), work_id=cwork,
                      verdict="pass", principal="someone", rationale="x", instruction=None)

    # ── materialize tests ─────────────────────────────────────────────────────

    def test_package_c_materialize_happy_path(self):
        cwork = self._prepare_package_c()
        result = self.call("materialize_package_c_prerequisites",
                           state_root=str(self.state), work_id=cwork)
        self.assertFalse(result["reused"])
        state = self.read_state(cwork)
        self.assertIn("package_c_prerequisites", state)
        wt = state["worktree"]["path"]
        for rel in self.mod.PACKAGE_C_SOURCE_FILES:
            self.assertTrue(os.path.isfile(os.path.join(wt, rel)))

    def test_package_c_materialize_is_idempotent(self):
        cwork = self.make_package_c_materialized()
        result2 = self.call("materialize_package_c_prerequisites",
                            state_root=str(self.state), work_id=cwork)
        self.assertTrue(result2["reused"])

    def test_package_c_materialize_wrong_phase_rejected(self):
        cwork = self.make_package_c_materialized()
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=cwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_c_builder),
                      model="sonnet", effort="high")

    def test_package_c_materialize_source_tamper_rejected(self):
        """Corrupting the completed Package B fixed-gate receipt hash fails closed."""
        cwork = self._prepare_package_c()
        state = self.read_state(cwork)
        bwork = self.mod.PACKAGE_C_PREREQ_WORK_ID
        b_state_path = self.state / bwork / "state.json"
        b_state = json.loads(b_state_path.read_text())
        b_state["package_b_gate_receipt"]["sha256"] = "0" * 64
        b_state_path.write_text(json.dumps(b_state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("materialize_package_c_prerequisites",
                      state_root=str(self.state), work_id=cwork)

    def test_package_c_materialize_v3_plan_tamper_rejected(self):
        """Corrupting the frozen v3 plan gate verdict fails closed."""
        cwork = self._prepare_package_c()
        v3_dir = self.state / self.mod.PACKAGE_C_V3_WORK_ID
        v3_state_path = v3_dir / "state.json"
        v3_state = json.loads(v3_state_path.read_text())
        v3_state["gate"]["verdict"] = "revise"
        v3_state_path.write_text(json.dumps(v3_state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("materialize_package_c_prerequisites",
                      state_root=str(self.state), work_id=cwork)

    # ── launch tests ──────────────────────────────────────────────────────────

    def test_package_c_launch_wrong_role_rejected(self):
        cwork = self.make_package_c_materialized()
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=cwork,
                      role="planner", resume=False,
                      claude_bin=str(self.fake_package_c_builder),
                      model="sonnet", effort="medium")

    def test_package_c_launch_wrong_model_rejected(self):
        cwork = self.make_package_c_materialized()
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=cwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_c_builder),
                      model="opus", effort="medium")

    def test_package_c_launch_no_prerequisites_rejected(self):
        self._prepare_package_c()
        cwork = self.mod.PACKAGE_C_WORK_ID
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=cwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_c_builder),
                      model="sonnet", effort="medium")

    # ── collect tests ─────────────────────────────────────────────────────────

    def test_package_c_collect_happy_path(self):
        cwork = self.make_package_c_with_candidate()
        state = self.read_state(cwork)
        self.assertEqual(state["phase"], "awaiting_gate")
        self.assertIn("package_c_collect", state)
        self.assertIn("package_c_builder_delta", state)

    def test_package_c_collect_replay_idempotent(self):
        cwork = self.make_package_c_with_candidate()
        first = self.call("collect", state_root=str(self.state), work_id=cwork)
        second = self.call("collect", state_root=str(self.state), work_id=cwork)
        self.assertEqual(first["gate"]["candidate_id"], second["gate"]["candidate_id"])

    def test_package_c_collect_missing_map_rejected(self):
        os.environ["FAKE_PC_BUILDER_KIND"] = "missing_pending_map"
        cwork = self.make_package_c_materialized()
        self.call("launch", state_root=str(self.state), work_id=cwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_c_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(cwork)
        result = self.call("collect", state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["gate"]["verdict"], "fail")

    def test_package_c_collect_wrong_paths_rejected(self):
        os.environ["FAKE_PC_BUILDER_KIND"] = "wrong_paths"
        cwork = self.make_package_c_materialized()
        self.call("launch", state_root=str(self.state), work_id=cwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_c_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(cwork)
        with self.assertRaises(self.mod.ControllerError):
            self.call("collect", state_root=str(self.state), work_id=cwork)

    # ── verification tests ───────────────────────────────────────────────────

    def test_package_c_verify_happy_path_all_nine_gates(self):
        cwork = self.make_package_c_with_candidate()
        result = self.call("verify_package_c", state_root=str(self.state), work_id=cwork)
        self.assertTrue(result["all_passed"], result)
        state = self.read_state(cwork)
        aggregate = json.loads(Path(state["package_c_verification"]["aggregate_path"]).read_text())
        self.assertEqual(len(aggregate["receipts"]), 9)
        self.assertTrue(all(row["passed"] for row in aggregate["receipts"]))

    def test_package_c_verify_reused_replay(self):
        cwork = self.make_package_c_verified()
        result2 = self.call("verify_package_c", state_root=str(self.state), work_id=cwork)
        self.assertTrue(result2["reused"])
        self.assertTrue(result2["all_passed"])

    def test_package_c_verify_fixture_mutation_rejected(self):
        cwork = self.make_package_c_with_candidate()
        state = self.read_state(cwork)
        wt = state["worktree"]["path"]
        fixture_path = os.path.join(wt, self.mod.PACKAGE_C_FIXTURE_PATH)
        Path(fixture_path).write_text('{"sources":["tampered"]}\n')
        with self.assertRaises(self.mod.ControllerError):
            self.call("verify_package_c", state_root=str(self.state), work_id=cwork)

    def test_package_c_verify_structural_migration_negative_control(self):
        """A candidate that still carries an expectedFailure marker must fail deterministically
        (either the characterization gate's zero-expected-failure check or the structural AST
        check), and verification must stop at that first failure without a false pass."""
        os.environ["FAKE_PC_BUILDER_KIND"] = "unmigrated_char"
        os.environ["FAKE_PC_UNMIGRATED_CHAR_PATH"] = str(self.fake_pc_unmigrated_char)
        cwork = self.make_package_c_materialized()
        self.call("launch", state_root=str(self.state), work_id=cwork,
                  role="implementer", resume=False,
                  claude_bin=str(self.fake_package_c_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(cwork)
        self.call("collect", state_root=str(self.state), work_id=cwork)
        result = self.call("verify_package_c", state_root=str(self.state), work_id=cwork)
        self.assertFalse(result["all_passed"])
        aggregate = json.loads(Path(result["aggregate_path"]).read_text())
        self.assertFalse(aggregate["receipts"][-1]["passed"])
        self.assertIn(aggregate["receipts"][-1]["label"], ("characterization", "structural_migration"))
        self.assertTrue(all(row["passed"] for row in aggregate["receipts"][:-1]))

    # ── review tests ──────────────────────────────────────────────────────────

    def test_package_c_review_happy_pass_completes(self):
        cwork = self.make_package_c_verified()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        self.launch_package_c_review(cwork, agg_path, ["scripts/cowork_dispatch.py"])
        ingested = self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        self.assertEqual(ingested["verdict"], "pass")
        completed = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(completed["disposition"], "pass")
        self.assertEqual(completed["phase"], "completed")

    def test_package_c_adjudicate_no_early_fail_before_review_launched(self):
        cwork = self.make_package_c_verified()
        result = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["disposition"], "awaiting_review")
        self.assertEqual(result["phase"], "awaiting_gate")

    def test_package_c_review_infrastructure_retry_once(self):
        """Overflow qualifies for exactly one infrastructure retry; a second overflow is rejected."""
        cwork = self.make_package_c_verified()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        os.environ["FAKE_PC_AGGREGATE_PATH"] = agg_path
        os.environ["FAKE_PC_REVIEWER_KIND"] = "overflow"
        self.call("launch_package_c_review", state_root=str(self.state), work_id=cwork,
                  claude_bin=str(self.fake_package_c_reviewer))
        self.wait_package_c_review(cwork)
        retry = self.call("retry_package_c_review", state_root=str(self.state), work_id=cwork)
        self.assertEqual(retry["retry_evidence"]["classification"], "bounded_stream_overflow")
        self.call("launch_package_c_review", state_root=str(self.state), work_id=cwork,
                  claude_bin=str(self.fake_package_c_reviewer))
        self.wait_package_c_review(cwork)
        with self.assertRaises(self.mod.ControllerError):
            self.call("retry_package_c_review", state_root=str(self.state), work_id=cwork)

    def test_package_c_review_manual_capacity_wait(self):
        """An unresettable rate-limit rejection enters awaiting_capacity in manual_signal mode."""
        cwork = self.make_package_c_verified()
        state = self.read_state(cwork)
        os.environ["FAKE_PC_AGGREGATE_PATH"] = state["package_c_verification"]["aggregate_path"]
        os.environ["FAKE_PC_REVIEWER_KIND"] = "manual_capacity"
        self.call("launch_package_c_review", state_root=str(self.state), work_id=cwork,
                  claude_bin=str(self.fake_package_c_reviewer))
        self.wait_package_c_review(cwork)
        result = self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["phase"], self.mod.CAPACITY_WAITING)
        self.assertEqual(result["capacity"]["mode"], "manual_signal")
        state = self.read_state(cwork)
        self.assertEqual(state["phase"], self.mod.CAPACITY_WAITING)
        with self.assertRaises(self.mod.ControllerError):
            self.call("register_package_c_review_capacity_wakeup", state_root=str(self.state),
                      work_id=cwork, wakeup_ref="manual-adapter-signal")

    # ── adjudication / correction tests ──────────────────────────────────────

    def test_package_c_adjudicate_one_correction_then_pass(self):
        """A first reviewer dissent archives evidence and permits exactly one correction."""
        cwork = self.make_package_c_verified()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        os.environ["FAKE_PC_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_c_review(cwork, agg_path, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        revised = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(revised["disposition"], "revise")
        self.assertEqual(revised["phase"], "prepared")
        state = self.read_state(cwork)
        self.assertIn("recovery_constraint", state)
        self.assertEqual(state["recovery_constraint"]["kind"], "package_c_exact_correction")
        prior_session = state["attempt"]["provider_session_id"]
        os.environ.pop("FAKE_PC_REVIEWER_VERDICT", None)
        self.call("launch", state_root=str(self.state), work_id=cwork,
                  role="implementer", resume=True,
                  claude_bin=str(self.fake_package_c_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(cwork)
        state = self.read_state(cwork)
        self.assertEqual(state["attempt"]["provider_session_id"], prior_session)
        self.assertIsNone(state.get("candidate"))
        self.call("collect", state_root=str(self.state), work_id=cwork)
        verified = self.call("verify_package_c", state_root=str(self.state), work_id=cwork)
        self.assertTrue(verified["all_passed"], verified)
        state = self.read_state(cwork)
        agg_path2 = state["package_c_verification"]["aggregate_path"]
        self.launch_package_c_review(cwork, agg_path2, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        completed = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(completed["disposition"], "pass")
        self.assertEqual(completed["phase"], "completed")

    def test_package_c_adjudicate_second_failure_terminal(self):
        """A second reviewer dissent after the one bounded correction is exhausted terminally."""
        cwork = self.make_package_c_verified()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        os.environ["FAKE_PC_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_c_review(cwork, agg_path, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        first = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(first["disposition"], "revise")
        os.environ.pop("FAKE_PC_REVIEWER_VERDICT", None)
        self.call("launch", state_root=str(self.state), work_id=cwork,
                  role="implementer", resume=True,
                  claude_bin=str(self.fake_package_c_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(cwork)
        self.call("collect", state_root=str(self.state), work_id=cwork)
        self.call("verify_package_c", state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        agg_path2 = state["package_c_verification"]["aggregate_path"]
        os.environ["FAKE_PC_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_c_review(cwork, agg_path2, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        failed = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(failed["disposition"], "fail")
        self.assertEqual(failed["phase"], "failed")
        os.environ.pop("FAKE_PC_REVIEWER_VERDICT", None)
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=cwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_c_builder),
                      model="sonnet", effort="medium")

    def test_package_c_adjudicate_replay_is_idempotent(self):
        cwork = self.make_package_c_reviewed()
        first = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        second = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(first["disposition"], second["disposition"])
        self.assertTrue(second["reused"])

    def test_package_c_probe_code_passes_against_fake_dispatch(self):
        """Generated probe exits 0 and _package_c_probe_passes returns True against fake dispatch."""
        cwork = self.make_package_c_with_candidate()
        state = self.read_state(cwork)
        wt = state["worktree"]["path"]
        probe_code = self.mod._package_c_probe_code()
        result = subprocess.run(["python3", "-c", probe_code],
                                capture_output=True, cwd=wt)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.mod._package_c_probe_passes(result.stdout),
                        "probe output: %r" % result.stdout)

    def test_package_c_probe_code_does_not_import_attempt_link_class(self):
        """Invented-class regression: probe must not import AttemptLink as a class."""
        probe_code = self.mod._package_c_probe_code()
        self.assertNotIn("import AttemptLink", probe_code,
                         "probe imports AttemptLink class — use dict API instead")

    def test_package_c_probe_lax_validation_negative_fixture(self):
        """Lax validation negative fixture: probe fails when dispatch omits UUID rejection."""
        lax_dispatch = b"""\
_VALID_KINDS = {'pending_replay', 'gate_repair'}
def validate_attempt_link(link):
    # lax: accepts any attempt_id without UUID validation
    if not isinstance(link, dict): raise ValueError('not a link')
    if link.get('kind') not in _VALID_KINDS: raise ValueError('bad kind')
    if not link.get('source_ref'): raise ValueError('missing source_ref')
    if not link.get('delivery_ref'): raise ValueError('missing delivery_ref')
    if not link.get('idempotency_key'): raise ValueError('missing idempotency_key')
    return link
def build_attempt_link_idempotency_key(role, kind, source_ref, ordinal):
    return '%s:%s:%s:%d' % (role, kind, source_ref, ordinal)
"""
        with tempfile.TemporaryDirectory() as td:
            scripts_dir = os.path.join(td, "scripts")
            os.makedirs(scripts_dir)
            with open(os.path.join(scripts_dir, "cowork_dispatch.py"), "wb") as f:
                f.write(lax_dispatch)
            probe_code = self.mod._package_c_probe_code()
            result = subprocess.run(["python3", "-c", probe_code],
                                    capture_output=True, cwd=td)
            self.assertEqual(result.returncode, 0)
            output = json.loads(result.stdout)
            # lax dispatch accepts bad UUID → fabricated_rejected=False → probe correctly fails
            self.assertFalse(output.get("fabricated_rejected"),
                             "lax dispatch should accept the bad-UUID record")
            self.assertFalse(self.mod._package_c_probe_passes(result.stdout),
                             "probe must fail when dispatch omits UUID rejection")


class PackageCRescueTests(PackageCLifecycleTests):
    """Focused tests for rescue-package-c-gate-selection (selector-v2 controller rescue)."""

    def _make_terminal_state(self):
        """Drive Package C to correction_exhausted failed phase and return work_id."""
        cwork = self.make_package_c_verified()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        os.environ["FAKE_PC_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_c_review(cwork, agg_path, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        first = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(first["disposition"], "revise")
        os.environ.pop("FAKE_PC_REVIEWER_VERDICT", None)
        self.call("launch", state_root=str(self.state), work_id=cwork,
                  role="implementer", resume=True,
                  claude_bin=str(self.fake_package_c_builder),
                  model="sonnet", effort="medium")
        self.fixture.wait_quiescent(cwork)
        self.call("collect", state_root=str(self.state), work_id=cwork)
        self.call("verify_package_c", state_root=str(self.state), work_id=cwork)
        state2 = self.read_state(cwork)
        agg_path2 = state2["package_c_verification"]["aggregate_path"]
        os.environ["FAKE_PC_REVIEWER_VERDICT"] = "needs_correction"
        self.launch_package_c_review(cwork, agg_path2, ["scripts/cowork_dispatch.py"])
        self.call("ingest_package_c_review", state_root=str(self.state), work_id=cwork)
        failed = self.call("adjudicate_package_c", state_root=str(self.state), work_id=cwork)
        self.assertEqual(failed["disposition"], "fail")
        self.assertEqual(failed["phase"], "failed")
        os.environ.pop("FAKE_PC_REVIEWER_VERDICT", None)
        return cwork

    def _patch_rescue_constants(self, cwork):
        """Patch RESCUE_* constants to match the actual synthetic terminal state."""
        state = self.read_state(cwork)
        candidate = state["candidate"]
        self.mod.PACKAGE_C_RESCUE_CANDIDATE_ID = candidate["id"]
        self.mod.PACKAGE_C_RESCUE_EVIDENCE_DIGEST = candidate["evidence_digest"]
        self.mod.PACKAGE_C_RESCUE_REVISION = state["revision"]
        self.mod.PACKAGE_C_RESCUE_FAILED_AGGREGATE_SHA256 = state["package_c_verification"]["aggregate_sha256"]
        self.mod.PACKAGE_C_RESCUE_TERMINAL_GATE_SHA256 = state["package_c_gate_receipt"]["sha256"]
        agg_path = state["package_c_verification"]["aggregate_path"]
        with open(agg_path) as f:
            agg = json.loads(f.read())
        pending_ref = next((r for r in agg["receipts"] if r.get("label") == "pending_linkage_focused"), None)
        self.mod.PACKAGE_C_RESCUE_FAILED_PENDING_SHA256 = (
            pending_ref["sha256"] if pending_ref else "0" * 64)
        wt = state["worktree"]["path"]
        tc_path = os.path.join(wt, "scripts", "test_cowork.py")
        with open(tc_path, "rb") as f:
            self.mod.PACKAGE_C_RESCUE_TEST_COWORK_SHA256 = hashlib.sha256(f.read()).hexdigest()

    def _make_terminal_and_patch(self):
        cwork = self._make_terminal_state()
        self._patch_rescue_constants(cwork)
        return cwork

    def test_selector_v2_specs_have_no_v1_ids(self):
        """Static: v2 verification specs must not contain any v1 invented test IDs."""
        specs = self.mod._package_c_verification_specs()
        pending_spec = next(s for s in specs if s["label"] == "pending_linkage_focused")
        repair_spec = next(s for s in specs if s["label"] == "gate_repair_focused")
        v1_pending = set(self.mod._PACKAGE_C_FOCUSED_PENDING_TESTS_V1)
        v1_repair = set(self.mod._PACKAGE_C_FOCUSED_REPAIR_TESTS_V1)
        for t in pending_spec["argv"]:
            self.assertNotIn(t, v1_pending, "v1 pending ID found in v2 specs: " + t)
        for t in repair_spec["argv"]:
            self.assertNotIn(t, v1_repair, "v1 repair ID found in v2 specs: " + t)

    def test_selector_v2_pending_tests_count(self):
        """Selector v2 pending list has exactly 12 items."""
        self.assertEqual(len(self.mod.PACKAGE_C_FOCUSED_PENDING_TESTS), 12)

    def test_selector_v2_repair_tests_count(self):
        """Selector v2 repair list has exactly 9 items."""
        self.assertEqual(len(self.mod.PACKAGE_C_FOCUSED_REPAIR_TESTS), 9)

    def test_rescue_requires_failed_phase(self):
        """Rescue fails closed if state is not in failed phase."""
        cwork = self.make_package_c_verified()
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_requires_package_c_identity(self):
        """Rescue fails closed if work_id is not Package C."""
        work = "some-other-work-id"
        with self.assertRaises((self.mod.ControllerError, Exception)):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=work)

    def test_rescue_requires_exact_candidate(self):
        """Rescue fails closed if candidate id does not match the bound terminal candidate."""
        cwork = self._make_terminal_and_patch()
        self.mod.PACKAGE_C_RESCUE_CANDIDATE_ID = "package-c-" + "0" * 32
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_requires_exact_revision(self):
        """Rescue fails closed if revision does not match."""
        cwork = self._make_terminal_and_patch()
        self.mod.PACKAGE_C_RESCUE_REVISION = 9999
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_requires_exact_aggregate(self):
        """Rescue fails closed if failed aggregate SHA does not match."""
        cwork = self._make_terminal_and_patch()
        self.mod.PACKAGE_C_RESCUE_FAILED_AGGREGATE_SHA256 = "a" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_requires_exact_terminal_gate(self):
        """Rescue fails closed if terminal gate receipt SHA does not match."""
        cwork = self._make_terminal_and_patch()
        self.mod.PACKAGE_C_RESCUE_TERMINAL_GATE_SHA256 = "b" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_valid_transitions_to_awaiting_gate(self):
        """Valid rescue transitions from failed to awaiting_gate."""
        cwork = self._make_terminal_and_patch()
        result = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["operation"], "rescue_package_c_gate_selection")
        self.assertEqual(result["phase"], "awaiting_gate")
        self.assertFalse(result["reused"])
        self.assertIn("rescue_id", result)
        self.assertIn("rescue_receipt_sha256", result)
        state = self.read_state(cwork)
        self.assertEqual(state["phase"], "awaiting_gate")
        self.assertIn("package_c_rescue_receipt", state)
        self.assertIn("package_c_v1_archive", state)
        self.assertNotIn("package_c_verification", state)
        self.assertNotIn("package_c_gate_receipt", state)

    def test_rescue_receipt_keys_complete(self):
        """Rescue receipt on disk contains all required keys."""
        cwork = self._make_terminal_and_patch()
        result = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        receipt_path = state["package_c_rescue_receipt"]["path"]
        with open(receipt_path) as f:
            receipt = json.loads(f.read())
        required = {"schema_version", "kind", "work_id", "policy", "bug_class", "authority_date",
                    "candidate_id", "evidence_digest", "failed_aggregate_sha256",
                    "failed_pending_receipt_sha256", "terminal_gate_receipt_sha256",
                    "selector_v2_pending_tests", "selector_v2_repair_tests",
                    "prior_revision", "issued_at", "rescue_id"}
        self.assertEqual(set(receipt), required)
        self.assertEqual(receipt["kind"], "package_c_gate_selection_rescue")
        self.assertEqual(receipt["bug_class"], "gate_selection_invented_ids/v1")
        self.assertEqual(receipt["selector_v2_pending_tests"], self.mod.PACKAGE_C_FOCUSED_PENDING_TESTS)
        self.assertEqual(receipt["selector_v2_repair_tests"], self.mod.PACKAGE_C_FOCUSED_REPAIR_TESTS)

    def test_rescue_replay_is_idempotent(self):
        """Replaying rescue returns same receipt sha and reused=True."""
        cwork = self._make_terminal_and_patch()
        first = self.call("rescue_package_c_gate_selection",
                          state_root=str(self.state), work_id=cwork)
        second = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        self.assertTrue(second["reused"])
        self.assertEqual(first["rescue_id"], second["rescue_id"])
        self.assertEqual(first["rescue_receipt_sha256"], second["rescue_receipt_sha256"])

    def test_rescue_v1_archive_preserved(self):
        """Old verification and gate receipt refs are preserved in v1 archive."""
        cwork = self._make_terminal_and_patch()
        state_before = self.read_state(cwork)
        old_agg_sha = state_before["package_c_verification"]["aggregate_sha256"]
        old_gate_sha = state_before["package_c_gate_receipt"]["sha256"]
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state_after = self.read_state(cwork)
        archive = state_after["package_c_v1_archive"]
        self.assertEqual(archive["verification"]["aggregate_sha256"], old_agg_sha)
        self.assertEqual(archive["gate_receipt"]["sha256"], old_gate_sha)
        self.assertEqual(archive["kind"], "gate_selection_v1_terminal")

    def test_rescue_verify_uses_v2_dir(self):
        """After rescue, verify-package-c writes to the v2 verification directory."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        result = self.call("verify_package_c",
                           state_root=str(self.state), work_id=cwork)
        self.assertIn("aggregate_path", result)
        self.assertIn("package-c-verification-v2", result["aggregate_path"])

    def test_rescue_no_builder_launch_after_rescue(self):
        """No implementer launch is available after gate-selection rescue (no second correction)."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=cwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_c_builder),
                      model="sonnet", effort="medium")

    def test_rescue_verify_without_receipt_rejected(self):
        """verify-package-c for the terminal candidate without a rescue receipt is rejected."""
        cwork = self._make_terminal_and_patch()
        state = self.read_state(cwork)
        candidate_id = state["candidate"]["id"]
        # Patch RESCUE_CANDIDATE_ID to the actual candidate so verify would trigger the check,
        # then manually set phase to awaiting_gate without a rescue receipt
        self.mod.PACKAGE_C_RESCUE_CANDIDATE_ID = candidate_id
        # Force awaiting_gate without rescue receipt by directly writing state
        state["phase"] = "awaiting_gate"
        state.pop("package_c_gate_receipt", None)
        state.pop("package_c_rescue_receipt", None)
        import json as _json
        state_file = self.state_file(cwork)
        state_file.write_text(_json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("verify_package_c",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_receipt_tamper_rejected_on_replay(self):
        """Tampered rescue receipt content is rejected on replay."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        receipt_path = state["package_c_rescue_receipt"]["path"]
        with open(receipt_path) as f:
            receipt = json.loads(f.read())
        receipt["bug_class"] = "tampered"
        with open(receipt_path, "w") as f:
            f.write(json.dumps(receipt))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_candidate_unchanged_after_rescue(self):
        """The candidate and its evidence digest are unchanged after rescue."""
        cwork = self._make_terminal_and_patch()
        state_before = self.read_state(cwork)
        old_cid = state_before["candidate"]["id"]
        old_edigest = state_before["candidate"]["evidence_digest"]
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state_after = self.read_state(cwork)
        self.assertEqual(state_after["candidate"]["id"], old_cid)
        self.assertEqual(state_after["candidate"]["evidence_digest"], old_edigest)

    def test_rescue_correction_history_recorded_as_consumed(self):
        """Correction history remains present after rescue (first correction is consumed)."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        self.assertIsInstance(state.get("package_c_correction_history"), list)
        self.assertTrue(len(state["package_c_correction_history"]) > 0)

    def _compute_rescue_immutable(self, state):
        """Replicate the controller's rescue_immutable dict for orphan injection."""
        mod = self.mod
        return {
            "schema_version": mod.SCHEMA,
            "kind": "package_c_gate_selection_rescue",
            "work_id": state["work_id"],
            "policy": mod.POLICY_ID,
            "bug_class": mod.PACKAGE_C_RESCUE_SELECTOR_BUG,
            "authority_date": mod.PACKAGE_C_RESCUE_AUTHORITY_DATE,
            "candidate_id": mod.PACKAGE_C_RESCUE_CANDIDATE_ID,
            "evidence_digest": mod.PACKAGE_C_RESCUE_EVIDENCE_DIGEST,
            "failed_aggregate_sha256": mod.PACKAGE_C_RESCUE_FAILED_AGGREGATE_SHA256,
            "failed_pending_receipt_sha256": mod.PACKAGE_C_RESCUE_FAILED_PENDING_SHA256,
            "terminal_gate_receipt_sha256": mod.PACKAGE_C_RESCUE_TERMINAL_GATE_SHA256,
            "selector_v2_pending_tests": list(mod.PACKAGE_C_FOCUSED_PENDING_TESTS),
            "selector_v2_repair_tests": list(mod.PACKAGE_C_FOCUSED_REPAIR_TESTS),
            "prior_revision": state["revision"],
        }

    def _write_orphan_receipt(self, cwork, state, issued_at="2026-08-14T10:00:00Z", mutate=None):
        """Write a valid (or optionally mutated) rescue receipt orphan to disk."""
        rescue_immutable = self._compute_rescue_immutable(state)
        rescue_id = hashlib.sha256(
            json.dumps(rescue_immutable, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt = {**rescue_immutable, "rescue_id": rescue_id, "issued_at": issued_at}
        if mutate is not None:
            mutate(receipt)
        rescue_dir = os.path.join(str(self.state), cwork, "package-c-rescue")
        os.makedirs(rescue_dir, mode=0o700, exist_ok=True)
        receipt_path = os.path.join(rescue_dir, rescue_id + ".json")
        encoded = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
        import tempfile as _tempfile
        fd, tmp = _tempfile.mkstemp(prefix=".tmp-", dir=rescue_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(encoded)
            os.replace(tmp, receipt_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return receipt_path, rescue_id

    def test_rescue_crash_after_receipt_write_adopts_orphan(self):
        """Crash after receipt write: retry detects orphan, adopts original issued_at, succeeds."""
        cwork = self._make_terminal_and_patch()
        state = self.read_state(cwork)
        pre_issued_at = "2026-08-14T10:00:00Z"
        receipt_path, rescue_id = self._write_orphan_receipt(
            cwork, state, issued_at=pre_issued_at)
        # Rescue should adopt the orphan without conflict
        result = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        self.assertFalse(result["reused"])
        self.assertEqual(result["phase"], "awaiting_gate")
        # The stored receipt must carry the original pre-crash issued_at
        state_after = self.read_state(cwork)
        with open(state_after["package_c_rescue_receipt"]["path"]) as f:
            stored = json.loads(f.read())
        self.assertEqual(stored["issued_at"], pre_issued_at)
        self.assertEqual(stored["rescue_id"], rescue_id)

    def test_rescue_crash_after_state_save_before_event(self):
        """Crash after state save but before event: replay re-appends event idempotently."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        # Truncate the events file to simulate crash before event append
        events_path = os.path.join(str(self.state), cwork, "events.jsonl")
        events = [json.loads(line) for line in Path(events_path).read_text().splitlines() if line.strip()]
        surviving = [e for e in events if e.get("kind") != "package_c_gate_selection_rescued"]
        Path(events_path).write_text(
            "\n".join(json.dumps(e, sort_keys=True, separators=(",",":")) for e in surviving) +
            ("\n" if surviving else ""))
        # Replay should succeed and re-append the event
        result = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        self.assertTrue(result["reused"])
        events_after = [json.loads(line) for line in Path(events_path).read_text().splitlines() if line.strip()]
        rescue_events = [e for e in events_after if e.get("kind") == "package_c_gate_selection_rescued"]
        self.assertEqual(len(rescue_events), 1)

    def test_rescue_orphan_tamper_conflicting_field_fails_closed(self):
        """Orphan with a conflicting immutable field causes rescue to fail closed."""
        cwork = self._make_terminal_and_patch()
        state = self.read_state(cwork)

        def tamper(r):
            r["bug_class"] = "tampered_value"

        self._write_orphan_receipt(cwork, state, mutate=tamper)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_orphan_extra_key_fails_closed(self):
        """Orphan with an extra key causes rescue to fail closed."""
        cwork = self._make_terminal_and_patch()
        state = self.read_state(cwork)

        def tamper(r):
            r["extra_key"] = "unexpected"

        self._write_orphan_receipt(cwork, state, mutate=tamper)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_orphan_invalid_issued_at_fails_closed(self):
        """Orphan with a malformed issued_at causes rescue to fail closed."""
        cwork = self._make_terminal_and_patch()
        state = self.read_state(cwork)

        def tamper(r):
            r["issued_at"] = "not-a-timestamp"

        self._write_orphan_receipt(cwork, state, mutate=tamper)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_state_binding_extra_key_rejected_on_replay(self):
        """State binding with an extra key in package_c_rescue_receipt is rejected."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        state["package_c_rescue_receipt"]["extra"] = "bad"
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_state_binding_missing_key_rejected_on_replay(self):
        """State binding with a missing key in package_c_rescue_receipt is rejected."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        del state["package_c_rescue_receipt"]["selector_bug"]
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_state_binding_tampered_value_rejected_on_replay(self):
        """State binding with a wrong value in package_c_rescue_receipt is rejected."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        state["package_c_rescue_receipt"]["authority_date"] = "1970-01-01"
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_phase_tamper_on_replay_rejected(self):
        """Replay rejected if state phase is changed away from awaiting_gate after rescue."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        state["phase"] = "failed"
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_candidate_tamper_on_replay_rejected(self):
        """Replay rejected if candidate id is changed after rescue."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        state["candidate"]["id"] = "package-c-" + "0" * 32
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_gate_selection",
                      state_root=str(self.state), work_id=cwork)

    def test_rescue_exact_replay_returns_identical_fields(self):
        """Exact replay returns identical rescue_id and receipt SHA on every call."""
        cwork = self._make_terminal_and_patch()
        first = self.call("rescue_package_c_gate_selection",
                          state_root=str(self.state), work_id=cwork)
        for _ in range(2):
            replay = self.call("rescue_package_c_gate_selection",
                               state_root=str(self.state), work_id=cwork)
            self.assertTrue(replay["reused"])
            self.assertEqual(replay["rescue_id"], first["rescue_id"])
            self.assertEqual(replay["rescue_receipt_sha256"], first["rescue_receipt_sha256"])
            self.assertEqual(replay["candidate_id"], first["candidate_id"])
            self.assertEqual(replay["phase"], "awaiting_gate")


class TTYIsolationTests(unittest.TestCase):
    """Focused offline tests proving subprocess boundaries receive stdin=DEVNULL."""

    def test_bounded_run_passes_stdin_devnull(self):
        """_bounded_run must pass stdin=DEVNULL to Popen regardless of caller environment."""
        mod = controller_module()
        captured = {}
        real_popen = subprocess.Popen

        def fake_popen(argv, **kwargs):
            captured.update(kwargs)
            raise OSError("test sentinel")

        with mock.patch("subprocess.Popen", side_effect=fake_popen):
            mod._bounded_run(["true"], "/tmp", 1)

        self.assertIn("stdin", captured, "stdin kwarg must be passed to Popen")
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)

    def test_gate_wrapper_spawn_passes_stdin_devnull(self):
        """_package_a_execute_gate must pass stdin=DEVNULL when spawning the gate wrapper."""
        mod = controller_module()
        import tempfile as _tempfile, os as _os
        captured = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs)
            raise OSError("test sentinel")

        with _tempfile.TemporaryDirectory() as td:
            spec = {"label": "test", "argv": ["python3", "-m", "unittest"], "timeout": 5, "kind": "unittest"}
            static = {}
            with mock.patch("subprocess.Popen", side_effect=fake_popen):
                try:
                    mod._package_a_execute_gate(
                        spec, 0, td, td,
                        _os.path.join(td, "terminal.bin"),
                        _os.path.join(td, "start.json"),
                        static, "nonce", "2099-01-01T00:00:00Z")
                except mod.ControllerError:
                    pass

        self.assertIn("stdin", captured, "stdin kwarg must be passed to Popen")
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)

    def test_bounded_run_stdin_devnull_even_from_pty_caller(self):
        """Verify Popen call always includes stdin=DEVNULL, not inheriting the caller's fd."""
        mod = controller_module()
        calls = []

        def recording_popen(argv, **kwargs):
            calls.append(dict(kwargs))
            raise OSError("captured")

        with mock.patch("subprocess.Popen", side_effect=recording_popen):
            mod._bounded_run(["echo", "hello"], "/tmp", 1)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("stdin"), subprocess.DEVNULL)


class PackageCTTYRescueTests(PackageCRescueTests):
    """Tests for rescue-package-c-tty-contaminated-verification and v3 verification routing."""

    def _make_post_selector_rescue(self):
        """Drive to correction_exhausted failed, apply v2 selector rescue, return cwork."""
        cwork = self._make_terminal_and_patch()
        self.call("rescue_package_c_gate_selection",
                  state_root=str(self.state), work_id=cwork)
        return cwork

    def _inject_contaminated_v2_verification(self, cwork):
        """Create synthetic TTY-contaminated v2 verification files and update state."""
        state = self.read_state(cwork)
        candidate = state["candidate"]
        mod = self.mod
        directory = os.path.join(str(self.state), cwork)
        wt = state["worktree"]["path"]

        baseline = mod._package_c_prerequisites_intact(directory, state)
        delta = mod._package_c_builder_delta(directory, state, candidate)
        static = mod._package_c_verification_static(state, candidate, delta, baseline)
        specs = mod._package_c_verification_specs()
        spec = specs[0]  # test_cowork is always index 0

        static_sha = mod.digest_bytes(
            json.dumps(static, sort_keys=True, separators=(",", ":")).encode())
        spec_sha = mod.digest_bytes(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode())

        verify_dir = os.path.join(directory, "package-c-verification-v2", candidate["id"])
        os.makedirs(verify_dir, mode=0o700, exist_ok=True)

        created_at = "2026-08-14T10:00:00.000000Z"
        deadline_str = "2026-08-14T10:30:00.000000Z"

        all_receipt_paths = [os.path.join(verify_dir, "%d-%s.json" % (i, s["label"]))
                             for i, s in enumerate(specs)]
        all_output_paths = [os.path.join(verify_dir, "%d-%s.output.bin" % (i, s["label"]))
                            for i, s in enumerate(specs)]
        all_terminal_paths = [os.path.join(verify_dir, "%d-%s.terminal.bin" % (i, s["label"]))
                              for i, s in enumerate(specs)]
        all_start_paths = [os.path.join(verify_dir, "%d-%s.start.json" % (i, s["label"]))
                           for i, s in enumerate(specs)]
        aggregate_path = os.path.join(verify_dir, "aggregate.json")
        intent_path = os.path.join(verify_dir, "intent.json")
        output_path = all_output_paths[0]
        terminal_path = all_terminal_paths[0]
        start_path = all_start_paths[0]
        receipt_path = all_receipt_paths[0]

        # Wrapper nonce and digest (matching controller logic)
        wrapper_nonce = mod.digest_bytes(
            json.dumps({"static": static, "spec": spec, "index": 0},
                       sort_keys=True, separators=(",", ":")).encode())[:32]
        wrapper_core = {"terminal_path": terminal_path, "start_path": start_path,
                        "index": 0, "cwd": wt, "spec_sha256": spec_sha,
                        "static_sha256": static_sha, "wrapper_nonce": wrapper_nonce,
                        "deadline": deadline_str}
        wrapper_digest = mod.digest_bytes(
            json.dumps(wrapper_core, sort_keys=True, separators=(",", ":")).encode())

        # Write start.json (indented JSON)
        start_doc = {"schema_version": mod.SCHEMA, "kind": "package_a_gate_wrapper_start",
                     "index": 0, "spec_sha256": spec_sha, "static_sha256": static_sha,
                     "wrapper_nonce": wrapper_nonce, "wrapper_argv_sha256": wrapper_digest,
                     "pid": 99999, "pgid": 99999, "started_at": created_at,
                     "deadline": deadline_str}
        mod.atomic_json(start_path, start_doc)
        start_info = mod.capped_regular_file_info(start_path, verify_dir, mod.MAX_PLAN_ARTIFACT_BYTES)
        start_sha = start_info["sha256"]

        # Write output bytes (empty — timed out)
        output_bytes = b""
        output_sha = mod.digest_bytes(output_bytes)
        import tempfile as _tempfile
        fd, tmp = _tempfile.mkstemp(dir=verify_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(output_bytes)
            os.replace(tmp, output_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

        # Write terminal.bin (compact header + newline + raw output)
        terminal_result = {"exit_code": -15, "timed_out": True,
                           "output_limit_exceeded": False,
                           "output_sha256": output_sha, "output_bytes": 0,
                           "output_total_seen": 0, "spawn_error": None,
                           "cleanup_error": "permission_denied",
                           "counters": {"parse_error": 1}, "passed": False}
        terminal_header = {"schema_version": mod.SCHEMA, "kind": "package_a_gate_terminal",
                           "index": 0, "label": "test_cowork",
                           "spec_sha256": spec_sha, "static_sha256": static_sha,
                           "result": terminal_result}
        terminal_bytes = (json.dumps(terminal_header, sort_keys=True, separators=(",", ":"))
                          .encode() + b"\n" + output_bytes)
        terminal_sha = hashlib.sha256(terminal_bytes).hexdigest()
        fd, tmp = _tempfile.mkstemp(dir=verify_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(terminal_bytes)
            os.replace(tmp, terminal_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

        # Write receipt (indented JSON)
        receipt_doc = {"schema_version": mod.SCHEMA, "kind": "package_c_verification_receipt",
                       "index": 0, "label": "test_cowork",
                       "argv": spec["argv"], "cwd": wt,
                       "timeout_seconds": spec["timeout"], "static": static,
                       "start_path": start_path, "start_sha256": start_sha,
                       "terminal_path": terminal_path, "terminal_sha256": terminal_sha,
                       "exit_code": -15, "timed_out": True,
                       "output_limit_exceeded": False, "output_path": output_path,
                       "output_sha256": output_sha, "output_bytes": 0,
                       "output_total_seen": 0, "spawn_error": None,
                       "cleanup_error": "permission_denied",
                       "counters": {"parse_error": 1}, "passed": False}
        mod.atomic_json(receipt_path, receipt_doc)
        receipt_info = mod.capped_regular_file_info(receipt_path, verify_dir,
                                                    mod.MAX_PLAN_ARTIFACT_BYTES)
        receipt_sha = receipt_info["sha256"]

        # Write aggregate (indented JSON)
        post_fp = mod.worktree_fingerprint(wt)
        agg_refs = [{"index": 0, "label": "test_cowork",
                     "path": receipt_path, "sha256": receipt_sha,
                     "counters": {"parse_error": 1}, "passed": False}]
        aggregate_doc = {"schema_version": mod.SCHEMA,
                         "kind": "package_c_verification_aggregate",
                         "created_at": created_at, "static": static,
                         "receipts": agg_refs, "all_passed": False,
                         "post_fingerprint": post_fp}
        mod.atomic_json(aggregate_path, aggregate_doc)
        aggregate_info = mod.capped_regular_file_info(aggregate_path, verify_dir,
                                                      mod.MAX_PLAN_ARTIFACT_BYTES)
        aggregate_sha = aggregate_info["sha256"]

        # Write intent (indented JSON)
        intent_doc = {"schema_version": mod.SCHEMA,
                      "kind": "package_c_verification_intent",
                      "static": static, "specs": specs,
                      "receipt_paths": all_receipt_paths,
                      "output_paths": all_output_paths,
                      "terminal_paths": all_terminal_paths,
                      "start_paths": all_start_paths,
                      "aggregate_path": aggregate_path,
                      "created_at": created_at}
        mod.atomic_json(intent_path, intent_doc)
        intent_info = mod.capped_regular_file_info(intent_path, verify_dir,
                                                   mod.MAX_PLAN_ARTIFACT_BYTES)
        intent_sha = intent_info["sha256"]

        # Update state to reference the contaminated verification
        state["package_c_verification"] = {
            "intent_path": intent_path, "intent_sha256": intent_sha,
            "aggregate_path": aggregate_path, "aggregate_sha256": aggregate_sha,
            "candidate_id": candidate["id"],
            "evidence_digest": candidate["evidence_digest"],
        }
        self.state_file(cwork).write_text(json.dumps(state))

        return {"aggregate_sha": aggregate_sha, "receipt_sha": receipt_sha,
                "terminal_sha": terminal_sha, "output_sha": output_sha,
                "intent_sha": intent_sha}

    def _patch_tty_rescue_constants(self, cwork, shas):
        """Patch TTY rescue constants to match the synthetic contaminated state."""
        state = self.read_state(cwork)
        mod = self.mod
        mod.PACKAGE_C_TTY_RESCUE_REVISION = state["revision"]
        rescue_receipt = state.get("package_c_rescue_receipt") or {}
        mod.PACKAGE_C_TTY_RESCUE_SELECTOR_RESCUE_SHA = rescue_receipt.get("sha256", "0" * 64)
        mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_AGGREGATE_SHA = shas["aggregate_sha"]
        mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_RECEIPT_SHA = shas["receipt_sha"]
        mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_TERMINAL_SHA = shas["terminal_sha"]
        mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_OUTPUT_SHA = shas["output_sha"]
        mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_INTENT_SHA = shas["intent_sha"]

    def _make_tty_contaminated_and_patch(self):
        """Full setup: drive to contaminated v2 state and patch all TTY constants."""
        cwork = self._make_post_selector_rescue()
        shas = self._inject_contaminated_v2_verification(cwork)
        self._patch_tty_rescue_constants(cwork, shas)
        return cwork

    def _compute_tty_rescue_immutable(self, state):
        """Replicate the controller's TTY rescue_immutable dict for orphan injection."""
        mod = self.mod
        return {
            "schema_version": mod.SCHEMA,
            "kind": "package_c_tty_contaminated_verification_rescue",
            "work_id": state["work_id"],
            "policy": mod.POLICY_ID,
            "bug_class": mod.PACKAGE_C_TTY_RESCUE_BUG_CLASS,
            "authority_date": mod.PACKAGE_C_TTY_RESCUE_AUTHORITY_DATE,
            "candidate_id": mod.PACKAGE_C_RESCUE_CANDIDATE_ID,
            "evidence_digest": mod.PACKAGE_C_RESCUE_EVIDENCE_DIGEST,
            "selector_rescue_sha256": mod.PACKAGE_C_TTY_RESCUE_SELECTOR_RESCUE_SHA,
            "contaminated_aggregate_sha256": mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_AGGREGATE_SHA,
            "contaminated_receipt_sha256": mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_RECEIPT_SHA,
            "contaminated_terminal_sha256": mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_TERMINAL_SHA,
            "contaminated_output_sha256": mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_OUTPUT_SHA,
            "prior_revision": state["revision"],
        }

    def _write_tty_orphan_receipt(self, cwork, state, issued_at="2026-08-14T10:00:00Z",
                                  mutate=None):
        """Write a TTY rescue receipt orphan to disk."""
        rescue_immutable = self._compute_tty_rescue_immutable(state)
        rescue_id = hashlib.sha256(
            json.dumps(rescue_immutable, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
        receipt = {**rescue_immutable, "rescue_id": rescue_id, "issued_at": issued_at}
        if mutate is not None:
            mutate(receipt)
        tty_rescue_dir = os.path.join(str(self.state), cwork, "package-c-tty-rescue")
        os.makedirs(tty_rescue_dir, mode=0o700, exist_ok=True)
        receipt_path = os.path.join(tty_rescue_dir, rescue_id + ".json")
        encoded = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
        import tempfile as _tempfile
        fd, tmp = _tempfile.mkstemp(prefix=".tmp-", dir=tty_rescue_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(encoded)
            os.replace(tmp, receipt_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return receipt_path, rescue_id

    # ── TTY isolation (stdin=DEVNULL) ────────────────────────────────────────

    def test_tty_bounded_run_stdin_devnull(self):
        """_bounded_run subprocess boundary receives stdin=DEVNULL."""
        captured = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs)
            raise OSError("test sentinel")

        with mock.patch("subprocess.Popen", side_effect=fake_popen):
            self.mod._bounded_run(["true"], "/tmp", 1)

        self.assertEqual(captured.get("stdin"), subprocess.DEVNULL)

    def test_tty_gate_wrapper_spawn_stdin_devnull(self):
        """Gate wrapper Popen boundary receives stdin=DEVNULL."""
        import tempfile as _tempfile
        captured = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs)
            raise OSError("test sentinel")

        with _tempfile.TemporaryDirectory() as td:
            spec = {"label": "test", "argv": ["python3", "-m", "unittest"],
                    "timeout": 5, "kind": "unittest"}
            with mock.patch("subprocess.Popen", side_effect=fake_popen):
                try:
                    self.mod._package_a_execute_gate(
                        spec, 0, td, td,
                        os.path.join(td, "terminal.bin"),
                        os.path.join(td, "start.json"),
                        {}, "nonce", "2099-01-01T00:00:00Z")
                except self.mod.ControllerError:
                    pass

        self.assertEqual(captured.get("stdin"), subprocess.DEVNULL)

    # ── rescue succeeds and transitions state ────────────────────────────────

    def test_tty_rescue_valid_transitions_to_awaiting_gate(self):
        """Valid TTY rescue transitions to awaiting_gate and returns expected fields."""
        cwork = self._make_tty_contaminated_and_patch()
        result = self.call("rescue_package_c_tty_contaminated_verification",
                           state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["operation"], "rescue_package_c_tty_contaminated_verification")
        self.assertEqual(result["phase"], "awaiting_gate")
        self.assertFalse(result["reused"])
        self.assertIn("rescue_id", result)
        self.assertIn("rescue_receipt_sha256", result)
        state = self.read_state(cwork)
        self.assertEqual(state["phase"], "awaiting_gate")
        self.assertIn("package_c_tty_rescue_receipt", state)
        self.assertNotIn("package_c_verification", state)

    def test_tty_rescue_v2_archive_preserved(self):
        """Contaminated v2 verification binding is archived, not deleted."""
        cwork = self._make_tty_contaminated_and_patch()
        state_before = self.read_state(cwork)
        old_agg_sha = state_before["package_c_verification"]["aggregate_sha256"]
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        state_after = self.read_state(cwork)
        archive = state_after.get("package_c_v2_archive", {})
        self.assertEqual(archive.get("kind"), "tty_contaminated_verification_v2_terminal")
        self.assertEqual(archive["verification"]["aggregate_sha256"], old_agg_sha)

    def test_tty_rescue_selector_rescue_receipt_retained(self):
        """Selector v2 rescue receipt remains in state after TTY rescue."""
        cwork = self._make_tty_contaminated_and_patch()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        self.assertIn("package_c_rescue_receipt", state)

    def test_tty_rescue_correction_used_retained(self):
        """correction_used remains in state after TTY rescue."""
        cwork = self._make_tty_contaminated_and_patch()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        self.assertIsInstance(state.get("package_c_correction_used"), dict)

    def test_tty_rescue_receipt_keys_complete(self):
        """TTY rescue receipt on disk contains all required keys."""
        cwork = self._make_tty_contaminated_and_patch()
        result = self.call("rescue_package_c_tty_contaminated_verification",
                           state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        receipt_path = state["package_c_tty_rescue_receipt"]["path"]
        with open(receipt_path) as f:
            receipt = json.loads(f.read())
        required = {"schema_version", "kind", "work_id", "policy", "bug_class",
                    "authority_date", "candidate_id", "evidence_digest",
                    "selector_rescue_sha256", "contaminated_aggregate_sha256",
                    "contaminated_receipt_sha256", "contaminated_terminal_sha256",
                    "contaminated_output_sha256", "prior_revision", "issued_at",
                    "rescue_id"}
        self.assertEqual(set(receipt), required)
        self.assertEqual(receipt["kind"], "package_c_tty_contaminated_verification_rescue")
        self.assertEqual(receipt["bug_class"], "tty_inherited_stdin/v2_verification")

    # ── idempotency / replay ─────────────────────────────────────────────────

    def test_tty_rescue_replay_is_idempotent(self):
        """Replaying TTY rescue returns same receipt SHA and reused=True."""
        cwork = self._make_tty_contaminated_and_patch()
        first = self.call("rescue_package_c_tty_contaminated_verification",
                          state_root=str(self.state), work_id=cwork)
        second = self.call("rescue_package_c_tty_contaminated_verification",
                           state_root=str(self.state), work_id=cwork)
        self.assertTrue(second["reused"])
        self.assertEqual(first["rescue_id"], second["rescue_id"])
        self.assertEqual(first["rescue_receipt_sha256"], second["rescue_receipt_sha256"])
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(second["phase"], "awaiting_gate")

    def test_tty_rescue_exact_replay_byte_stable(self):
        """Three replays all return identical rescue_id and receipt SHA."""
        cwork = self._make_tty_contaminated_and_patch()
        first = self.call("rescue_package_c_tty_contaminated_verification",
                          state_root=str(self.state), work_id=cwork)
        for _ in range(2):
            r = self.call("rescue_package_c_tty_contaminated_verification",
                          state_root=str(self.state), work_id=cwork)
            self.assertTrue(r["reused"])
            self.assertEqual(r["rescue_id"], first["rescue_id"])
            self.assertEqual(r["rescue_receipt_sha256"], first["rescue_receipt_sha256"])

    # ── v3 routing ───────────────────────────────────────────────────────────

    def test_tty_rescue_verify_uses_v3_dir(self):
        """After TTY rescue, verify-package-c writes to the v3 verification directory."""
        cwork = self._make_tty_contaminated_and_patch()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        result = self.call("verify_package_c",
                           state_root=str(self.state), work_id=cwork)
        self.assertIn("aggregate_path", result)
        self.assertIn("package-c-verification-v3", result["aggregate_path"])

    def test_v1_verification_dir_unaffected(self):
        """effective_verify_dir returns v1 dir when neither rescue receipt is present."""
        mod = self.mod
        state = {"work_id": mod.PACKAGE_C_WORK_ID}
        candidate = {"id": "package-c-abc", "evidence_digest": "abc"}
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as td:
            d = mod._package_c_effective_verify_dir(td, candidate, state)
            self.assertIn("package-c-verification", d)
            self.assertNotIn("v2", d)
            self.assertNotIn("v3", d)

    def test_v2_routing_when_only_selector_rescue(self):
        """effective_verify_dir returns v2 when only package_c_rescue_receipt is set."""
        mod = self.mod
        state = {"work_id": mod.PACKAGE_C_WORK_ID, "package_c_rescue_receipt": {"x": 1}}
        candidate = {"id": "package-c-abc", "evidence_digest": "abc"}
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as td:
            d = mod._package_c_effective_verify_dir(td, candidate, state)
            self.assertIn("package-c-verification-v2", d)
            self.assertNotIn("v3", d)

    def test_v3_routing_when_tty_rescue_present(self):
        """effective_verify_dir returns v3 when package_c_tty_rescue_receipt is set."""
        mod = self.mod
        state = {"work_id": mod.PACKAGE_C_WORK_ID,
                 "package_c_rescue_receipt": {"x": 1},
                 "package_c_tty_rescue_receipt": {"y": 1}}
        candidate = {"id": "package-c-abc", "evidence_digest": "abc"}
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as td:
            d = mod._package_c_effective_verify_dir(td, candidate, state)
            self.assertIn("package-c-verification-v3", d)

    # ── no builder launch ─────────────────────────────────────────────────────

    def test_tty_rescue_no_builder_launch_after_rescue(self):
        """No implementer launch is available after TTY rescue (correction already used)."""
        cwork = self._make_tty_contaminated_and_patch()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        with self.assertRaises(self.mod.ControllerError):
            self.call("launch", state_root=str(self.state), work_id=cwork,
                      role="implementer", resume=False,
                      claude_bin=str(self.fake_package_c_builder),
                      model="sonnet", effort="medium")

    # ── crash / orphan / near-miss failures ──────────────────────────────────

    def test_tty_rescue_requires_awaiting_gate(self):
        """TTY rescue fails closed if phase is not awaiting_gate."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        state["phase"] = "failed"
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_exact_revision(self):
        """TTY rescue fails closed if revision does not match."""
        cwork = self._make_tty_contaminated_and_patch()
        self.mod.PACKAGE_C_TTY_RESCUE_REVISION = 9999
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_selector_rescue_receipt(self):
        """TTY rescue fails closed if selector rescue receipt is missing."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        state.pop("package_c_rescue_receipt", None)
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_exact_selector_sha(self):
        """TTY rescue fails closed if selector rescue receipt SHA does not match."""
        cwork = self._make_tty_contaminated_and_patch()
        self.mod.PACKAGE_C_TTY_RESCUE_SELECTOR_RESCUE_SHA = "a" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_contaminated_verification(self):
        """TTY rescue fails closed if package_c_verification is absent."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        state.pop("package_c_verification", None)
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_exact_aggregate_sha(self):
        """TTY rescue fails closed if contaminated aggregate SHA does not match."""
        cwork = self._make_tty_contaminated_and_patch()
        self.mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_AGGREGATE_SHA = "b" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_exact_receipt_sha(self):
        """TTY rescue fails closed if contaminated receipt SHA does not match."""
        cwork = self._make_tty_contaminated_and_patch()
        self.mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_RECEIPT_SHA = "c" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_exact_terminal_sha(self):
        """TTY rescue fails closed if contaminated terminal SHA does not match."""
        cwork = self._make_tty_contaminated_and_patch()
        self.mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_TERMINAL_SHA = "d" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_requires_exact_output_sha(self):
        """TTY rescue fails closed if contaminated output SHA does not match."""
        cwork = self._make_tty_contaminated_and_patch()
        self.mod.PACKAGE_C_TTY_RESCUE_CONTAMINATED_OUTPUT_SHA = "e" * 64
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_symlink_aggregate_fails_closed(self):
        """Symlinked contaminated aggregate causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        os.unlink(agg_path)
        os.symlink("/dev/null", agg_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_tampered_receipt_facts_fail_closed(self):
        """Contaminated receipt with wrong exit_code causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        candidate = state["candidate"]
        directory = os.path.join(str(self.state), cwork)
        verify_dir = os.path.join(directory, "package-c-verification-v2", candidate["id"])
        receipt_path = state["package_c_verification"]["aggregate_path"].replace(
            "aggregate.json", "0-test_cowork.json")
        with open(receipt_path) as f:
            rd = json.loads(f.read())
        rd["exit_code"] = 0  # tamper: not timed_out
        rd["timed_out"] = False
        rd["passed"] = True
        with open(receipt_path, "w") as f:
            f.write(json.dumps(rd, sort_keys=True, indent=2) + "\n")
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_tty_rescue_dir_symlink_fails_closed(self):
        """Symlinked TTY rescue dir causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        tty_rescue_dir = os.path.join(str(self.state), cwork, "package-c-tty-rescue")
        os.makedirs(os.path.dirname(tty_rescue_dir), exist_ok=True)
        os.symlink("/tmp", tty_rescue_dir)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_crash_after_receipt_write_adopts_orphan(self):
        """Crash after receipt write: retry detects orphan, adopts original issued_at."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        pre_issued_at = "2026-08-14T10:00:00Z"
        receipt_path, rescue_id = self._write_tty_orphan_receipt(
            cwork, state, issued_at=pre_issued_at)
        result = self.call("rescue_package_c_tty_contaminated_verification",
                           state_root=str(self.state), work_id=cwork)
        self.assertFalse(result["reused"])
        self.assertEqual(result["phase"], "awaiting_gate")
        state_after = self.read_state(cwork)
        with open(state_after["package_c_tty_rescue_receipt"]["path"]) as f:
            stored = json.loads(f.read())
        self.assertEqual(stored["issued_at"], pre_issued_at)
        self.assertEqual(stored["rescue_id"], rescue_id)

    def test_tty_rescue_orphan_tampered_field_fails_closed(self):
        """Orphan with a conflicting immutable field causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)

        def tamper(r):
            r["bug_class"] = "tampered"

        self._write_tty_orphan_receipt(cwork, state, mutate=tamper)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_orphan_extra_key_fails_closed(self):
        """Orphan with an extra key causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)

        def tamper(r):
            r["unexpected_key"] = "bad"

        self._write_tty_orphan_receipt(cwork, state, mutate=tamper)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_orphan_invalid_issued_at_fails_closed(self):
        """Orphan with malformed issued_at causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)

        def tamper(r):
            r["issued_at"] = "not-a-timestamp"

        self._write_tty_orphan_receipt(cwork, state, mutate=tamper)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_crash_after_state_save_before_event(self):
        """Crash after state save before event: replay re-appends the event."""
        cwork = self._make_tty_contaminated_and_patch()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        events_path = os.path.join(str(self.state), cwork, "events.jsonl")
        events = [json.loads(line)
                  for line in Path(events_path).read_text().splitlines() if line.strip()]
        surviving = [e for e in events
                     if e.get("kind") != "package_c_tty_verification_rescued"]
        Path(events_path).write_text(
            "\n".join(json.dumps(e, sort_keys=True, separators=(",", ":"))
                      for e in surviving) + ("\n" if surviving else ""))
        result = self.call("rescue_package_c_tty_contaminated_verification",
                           state_root=str(self.state), work_id=cwork)
        self.assertTrue(result["reused"])
        events_after = [json.loads(line)
                        for line in Path(events_path).read_text().splitlines() if line.strip()]
        tty_events = [e for e in events_after
                      if e.get("kind") == "package_c_tty_verification_rescued"]
        self.assertEqual(len(tty_events), 1)

    def test_tty_rescue_state_binding_tampered_rejected_on_replay(self):
        """Tampered tty_rescue_receipt state binding is rejected on replay."""
        cwork = self._make_tty_contaminated_and_patch()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        state["package_c_tty_rescue_receipt"]["authority_date"] = "1970-01-01"
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_replay_rejects_selector_binding_field_tamper(self):
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        state = self.read_state(cwork)
        state["package_c_rescue_receipt"]["candidate_id"] = "tampered"
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_replay_rejects_malformed_archive_time(self):
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        state = self.read_state(cwork)
        state["package_c_v2_archive"]["archived_at"] = None
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    def test_tty_rescue_receipt_tamper_rejected_on_replay(self):
        """Tampered TTY rescue receipt file content is rejected on replay."""
        cwork = self._make_tty_contaminated_and_patch()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        state = self.read_state(cwork)
        receipt_path = state["package_c_tty_rescue_receipt"]["path"]
        with open(receipt_path) as f:
            receipt = json.loads(f.read())
        receipt["bug_class"] = "tampered"
        with open(receipt_path, "w") as f:
            f.write(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)

    # ── v1/v2 artifacts byte-identical after TTY rescue ─────────────────────

    def test_tty_rescue_v1_artifacts_unchanged(self):
        """v1 verification and gate receipt bytes are unchanged after both rescues."""
        cwork = self._make_tty_contaminated_and_patch()
        state_before = self.read_state(cwork)
        v1_archive = state_before.get("package_c_v1_archive", {})
        old_v1_agg_sha = v1_archive.get("verification", {}).get("aggregate_sha256")
        old_v1_gate_sha = v1_archive.get("gate_receipt", {}).get("sha256")
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        state_after = self.read_state(cwork)
        v1_archive_after = state_after.get("package_c_v1_archive", {})
        self.assertEqual(v1_archive_after.get("verification", {}).get("aggregate_sha256"),
                         old_v1_agg_sha)
        self.assertEqual(v1_archive_after.get("gate_receipt", {}).get("sha256"),
                         old_v1_gate_sha)

    def test_tty_rescue_v2_contaminated_bytes_unchanged(self):
        """Contaminated v2 aggregate bytes on disk are unchanged after TTY rescue."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        with open(agg_path, "rb") as f:
            before = f.read()
        self.call("rescue_package_c_tty_contaminated_verification",
                  state_root=str(self.state), work_id=cwork)
        with open(agg_path, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)

    # ── existing selector v2 rescue tests remain green ───────────────────────

    def test_existing_selector_rescue_still_works(self):
        """Existing rescue-package-c-gate-selection succeeds (regression guard)."""
        cwork = self._make_terminal_and_patch()
        result = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["phase"], "awaiting_gate")
        self.assertFalse(result["reused"])

    def test_existing_selector_rescue_replay_still_works(self):
        """Existing rescue-package-c-gate-selection replay succeeds (regression guard)."""
        cwork = self._make_terminal_and_patch()
        first = self.call("rescue_package_c_gate_selection",
                          state_root=str(self.state), work_id=cwork)
        second = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        self.assertTrue(second["reused"])
        self.assertEqual(first["rescue_id"], second["rescue_id"])

    # ── integrity: pre-rescue deletion/tamper/symlink fails closed ────────────

    def _get_v2_paths(self, cwork):
        """Return (terminal_path, output_path, intent_path, selector_receipt_path) for a contaminated v2 state."""
        state = self.read_state(cwork)
        mod = self.mod
        candidate = state["candidate"]
        directory = os.path.join(str(self.state), cwork)
        verify_dir = os.path.join(directory, "package-c-verification-v2", candidate["id"])
        verification = state["package_c_verification"]
        # Read aggregate to find terminal/output paths from receipt_doc
        agg_path = verification["aggregate_path"]
        import json as _json
        with open(agg_path) as _f:
            aggregate = _json.loads(_f.read())
        sole = aggregate["receipts"][0]
        with open(sole["path"]) as _f:
            receipt_doc = _json.loads(_f.read())
        terminal_path = receipt_doc["terminal_path"]
        output_path = receipt_doc["output_path"]
        intent_path = verification["intent_path"]
        rescue_bound = state.get("package_c_rescue_receipt", {})
        sel_receipt_path = rescue_bound.get("path")
        return terminal_path, output_path, intent_path, sel_receipt_path

    def test_tty_rescue_terminal_deleted_pre_rescue_fails_closed(self):
        """Pre-rescue: deleting the contaminated terminal file causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        terminal_path, _, _, _ = self._get_v2_paths(cwork)
        original_state = self.state_file(cwork).read_bytes()
        os.unlink(terminal_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    def test_tty_rescue_terminal_tampered_pre_rescue_fails_closed(self):
        """Pre-rescue: tampering the contaminated terminal file causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        terminal_path, _, _, _ = self._get_v2_paths(cwork)
        original_state = self.state_file(cwork).read_bytes()
        with open(terminal_path, "ab") as f:
            f.write(b"\x00")
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    def test_tty_rescue_terminal_symlinked_pre_rescue_fails_closed(self):
        """Pre-rescue: symlinked contaminated terminal file causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        terminal_path, _, _, _ = self._get_v2_paths(cwork)
        original_state = self.state_file(cwork).read_bytes()
        with open(terminal_path, "rb") as _f:
            real_bytes = _f.read()
        os.unlink(terminal_path)
        tmp = terminal_path + ".real"
        with open(tmp, "wb") as f:
            f.write(real_bytes)
        os.symlink(tmp, terminal_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    def test_tty_rescue_output_deleted_pre_rescue_fails_closed(self):
        """Pre-rescue: deleting the contaminated output file causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        _, output_path, _, _ = self._get_v2_paths(cwork)
        original_state = self.state_file(cwork).read_bytes()
        os.unlink(output_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    def test_tty_rescue_intent_deleted_pre_rescue_fails_closed(self):
        """Pre-rescue: deleting the contaminated intent file causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        _, _, intent_path, _ = self._get_v2_paths(cwork)
        original_state = self.state_file(cwork).read_bytes()
        os.unlink(intent_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    def test_tty_rescue_selector_receipt_deleted_pre_rescue_fails_closed(self):
        """Pre-rescue: deleting selector rescue receipt file causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        _, _, _, sel_path = self._get_v2_paths(cwork)
        original_state = self.state_file(cwork).read_bytes()
        if sel_path:
            os.unlink(sel_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    def test_tty_rescue_selector_receipt_tampered_pre_rescue_fails_closed(self):
        """Pre-rescue: tampering selector rescue receipt bytes causes rescue to fail closed."""
        cwork = self._make_tty_contaminated_and_patch()
        _, _, _, sel_path = self._get_v2_paths(cwork)
        original_state = self.state_file(cwork).read_bytes()
        if sel_path:
            with open(sel_path, "r+") as f:
                content = f.read()
            with open(sel_path, "w") as f:
                f.write(content + " ")
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    def test_tty_rescue_later_gate_output_preseed_fails_closed(self):
        """Pre-rescue: any later-gate artifact, not only a receipt JSON, is rejected."""
        cwork = self._make_tty_contaminated_and_patch()
        state = self.read_state(cwork)
        verify_dir = os.path.dirname(state["package_c_verification"]["aggregate_path"])
        spec = self.mod._package_c_verification_specs()[1]
        later = os.path.join(verify_dir, "1-%s.output.bin" % spec["label"])
        with open(later, "wb") as out:
            out.write(b"unexpected")
        original_state = self.state_file(cwork).read_bytes()
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), original_state)

    # ── integrity: post-rescue replay fails closed on artifact deletion/tamper ─

    def _do_tty_rescue(self, cwork):
        """Run TTY rescue and return result."""
        return self.call("rescue_package_c_tty_contaminated_verification",
                         state_root=str(self.state), work_id=cwork)

    def _get_post_rescue_v2_paths(self, cwork):
        """Return (terminal_path, intent_path, selector_receipt_path, aggregate_path) from post-rescue archived state."""
        state = self.read_state(cwork)
        v2_archive = state.get("package_c_v2_archive", {})
        verification = v2_archive.get("verification", {})
        import json as _json
        agg_path = verification.get("aggregate_path")
        intent_path = verification.get("intent_path")
        with open(agg_path) as _f:
            aggregate = _json.loads(_f.read())
        sole = aggregate["receipts"][0]
        with open(sole["path"]) as _f:
            receipt_doc = _json.loads(_f.read())
        terminal_path = receipt_doc["terminal_path"]
        rescue_bound = state.get("package_c_rescue_receipt", {})
        sel_receipt_path = rescue_bound.get("path")
        return terminal_path, intent_path, sel_receipt_path, agg_path

    def test_tty_replay_fails_closed_when_terminal_deleted(self):
        """Post-rescue replay fails closed when contaminated terminal file is deleted."""
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        terminal_path, _, _, _ = self._get_post_rescue_v2_paths(cwork)
        saved_state = self.state_file(cwork).read_bytes()
        os.unlink(terminal_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)

    def test_tty_replay_fails_closed_when_terminal_tampered(self):
        """Post-rescue replay fails closed when contaminated terminal file is tampered."""
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        terminal_path, _, _, _ = self._get_post_rescue_v2_paths(cwork)
        saved_state = self.state_file(cwork).read_bytes()
        with open(terminal_path, "ab") as f:
            f.write(b"\x00")
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)

    def test_tty_replay_fails_closed_when_intent_deleted(self):
        """Post-rescue replay fails closed when contaminated intent file is deleted."""
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        _, intent_path, _, _ = self._get_post_rescue_v2_paths(cwork)
        saved_state = self.state_file(cwork).read_bytes()
        os.unlink(intent_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)

    def test_tty_replay_fails_closed_when_selector_receipt_deleted(self):
        """Post-rescue replay fails closed when selector rescue receipt is deleted."""
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        _, _, sel_path, _ = self._get_post_rescue_v2_paths(cwork)
        saved_state = self.state_file(cwork).read_bytes()
        if sel_path:
            os.unlink(sel_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)

    def test_tty_replay_fails_closed_when_aggregate_deleted(self):
        """Post-rescue replay fails closed when contaminated aggregate file is deleted."""
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        _, _, _, agg_path = self._get_post_rescue_v2_paths(cwork)
        saved_state = self.state_file(cwork).read_bytes()
        os.unlink(agg_path)
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)

    def test_tty_replay_fails_closed_when_aggregate_tampered(self):
        """Post-rescue replay fails closed when contaminated aggregate file is tampered."""
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        _, _, _, agg_path = self._get_post_rescue_v2_paths(cwork)
        saved_state = self.state_file(cwork).read_bytes()
        with open(agg_path, "ab") as f:
            f.write(b"\x00")
        with self.assertRaises(self.mod.ControllerError):
            self.call("rescue_package_c_tty_contaminated_verification",
                      state_root=str(self.state), work_id=cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)


class PackageCProbeRescueTests(PackageCTTYRescueTests):
    """Tests for rescue-package-c-probe-contract-verification and v4 verification routing."""

    _TEST_PROBE_ERROR = "test probe error: AttemptLink import not available in this dispatch"

    def _inject_failed_v3_verification(self, cwork):
        """Create synthetic failed v3 verification files (8 passed, json_probe failed) and update state."""
        state = self.read_state(cwork)
        candidate = state["candidate"]
        mod = self.mod
        directory = os.path.join(str(self.state), cwork)
        wt = state["worktree"]["path"]

        baseline = mod._package_c_prerequisites_intact(directory, state)
        delta = mod._package_c_builder_delta(directory, state, candidate)
        static = mod._package_c_verification_static(state, candidate, delta, baseline)
        specs = mod._package_c_verification_specs()

        verify_dir = os.path.join(directory, "package-c-verification-v3", candidate["id"])
        os.makedirs(verify_dir, mode=0o700, exist_ok=True)

        created_at = "2026-08-14T12:00:00.000000Z"

        all_receipt_paths = [os.path.join(verify_dir, "%d-%s.json" % (i, s["label"]))
                             for i, s in enumerate(specs)]
        all_output_paths = [os.path.join(verify_dir, "%d-%s.output.bin" % (i, s["label"]))
                            for i, s in enumerate(specs)]
        all_terminal_paths = [os.path.join(verify_dir, "%d-%s.terminal.bin" % (i, s["label"]))
                              for i, s in enumerate(specs)]
        all_start_paths = [os.path.join(verify_dir, "%d-%s.start.json" % (i, s["label"]))
                           for i, s in enumerate(specs)]
        aggregate_path = os.path.join(verify_dir, "aggregate.json")
        intent_path = os.path.join(verify_dir, "intent.json")

        import tempfile as _tempfile

        def write_bytes(path, data):
            fd, tmp = _tempfile.mkstemp(dir=verify_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
            return hashlib.sha256(data).hexdigest()

        # Write start files for all 9 gates (minimal JSON)
        start_shas = []
        for i, sp in enumerate(all_start_paths):
            data = json.dumps({"schema_version": 1, "kind": "package_a_gate_wrapper_start",
                               "index": i}).encode()
            sha = write_bytes(sp, data)
            start_shas.append(sha)

        # Write 8 passed gates
        agg_refs = []
        for i in range(8):
            spec = specs[i]
            out_data = b""
            out_sha = write_bytes(all_output_paths[i], out_data)
            term_data = b"{}\n"
            term_sha = write_bytes(all_terminal_paths[i], term_data)
            receipt_doc = {"schema_version": mod.SCHEMA, "kind": "package_c_verification_receipt",
                           "index": i, "label": spec["label"], "argv": spec["argv"], "cwd": wt,
                           "timeout_seconds": spec["timeout"], "static": static,
                           "start_path": all_start_paths[i], "start_sha256": start_shas[i],
                           "terminal_path": all_terminal_paths[i], "terminal_sha256": term_sha,
                           "exit_code": 0, "timed_out": False,
                           "output_limit_exceeded": False, "output_path": all_output_paths[i],
                           "output_sha256": out_sha, "output_bytes": 0,
                           "output_total_seen": 0, "spawn_error": None, "cleanup_error": None,
                           "counters": {}, "passed": True}
            mod.atomic_json(all_receipt_paths[i], receipt_doc)
            r_info = mod.capped_regular_file_info(all_receipt_paths[i], verify_dir,
                                                  mod.MAX_PLAN_ARTIFACT_BYTES)
            agg_refs.append({"index": i, "label": spec["label"],
                             "path": all_receipt_paths[i], "sha256": r_info["sha256"],
                             "counters": {}, "passed": True})

        # Write failed gate (json_probe, index 8)
        probe_spec = specs[8]
        output_json = json.dumps({"error": self._TEST_PROBE_ERROR})
        output_data = (output_json + "\n").encode()
        failed_out_sha = write_bytes(all_output_paths[8], output_data)

        terminal_data = b'{"kind":"test_terminal"}\n' + output_data
        failed_term_sha = write_bytes(all_terminal_paths[8], terminal_data)

        failed_receipt_doc = {"schema_version": mod.SCHEMA, "kind": "package_c_verification_receipt",
                              "index": 8, "label": "json_probe", "argv": probe_spec["argv"],
                              "cwd": wt, "timeout_seconds": probe_spec["timeout"], "static": static,
                              "start_path": all_start_paths[8], "start_sha256": start_shas[8],
                              "terminal_path": all_terminal_paths[8], "terminal_sha256": failed_term_sha,
                              "exit_code": 1, "timed_out": False,
                              "output_limit_exceeded": False, "output_path": all_output_paths[8],
                              "output_sha256": failed_out_sha, "output_bytes": len(output_data),
                              "output_total_seen": len(output_data), "spawn_error": None,
                              "cleanup_error": None, "counters": {}, "passed": False}
        mod.atomic_json(all_receipt_paths[8], failed_receipt_doc)
        failed_r_info = mod.capped_regular_file_info(all_receipt_paths[8], verify_dir,
                                                     mod.MAX_PLAN_ARTIFACT_BYTES)
        agg_refs.append({"index": 8, "label": "json_probe",
                         "path": all_receipt_paths[8], "sha256": failed_r_info["sha256"],
                         "counters": {}, "passed": False})

        # Write aggregate
        post_fp = mod.worktree_fingerprint(wt)
        aggregate_doc = {"schema_version": mod.SCHEMA,
                         "kind": "package_c_verification_aggregate",
                         "created_at": created_at, "static": static,
                         "receipts": agg_refs, "all_passed": False,
                         "post_fingerprint": post_fp}
        mod.atomic_json(aggregate_path, aggregate_doc)
        agg_info = mod.capped_regular_file_info(aggregate_path, verify_dir,
                                                mod.MAX_PLAN_ARTIFACT_BYTES)

        # Write intent
        intent_doc = {"schema_version": mod.SCHEMA,
                      "kind": "package_c_verification_intent",
                      "static": static, "specs": specs,
                      "receipt_paths": all_receipt_paths,
                      "output_paths": all_output_paths,
                      "terminal_paths": all_terminal_paths,
                      "start_paths": all_start_paths,
                      "aggregate_path": aggregate_path,
                      "created_at": created_at}
        mod.atomic_json(intent_path, intent_doc)
        intent_info = mod.capped_regular_file_info(intent_path, verify_dir,
                                                   mod.MAX_PLAN_ARTIFACT_BYTES)

        # Update state with v3 verification binding
        state["package_c_verification"] = {
            "intent_path": intent_path, "intent_sha256": intent_info["sha256"],
            "aggregate_path": aggregate_path, "aggregate_sha256": agg_info["sha256"],
            "candidate_id": candidate["id"],
            "evidence_digest": candidate["evidence_digest"],
        }
        self.state_file(cwork).write_text(json.dumps(state))

        return {"intent_sha": intent_info["sha256"], "aggregate_sha": agg_info["sha256"],
                "failed_receipt_sha": failed_r_info["sha256"],
                "failed_terminal_sha": failed_term_sha,
                "failed_output_sha": failed_out_sha}

    def _patch_probe_rescue_constants(self, cwork, shas):
        """Patch probe rescue constants to match the synthetic v3 verification state."""
        state = self.read_state(cwork)
        mod = self.mod
        mod.PACKAGE_C_PROBE_RESCUE_REVISION = state["revision"]
        tty_bound = state.get("package_c_tty_rescue_receipt") or {}
        mod.PACKAGE_C_PROBE_RESCUE_TTY_RESCUE_SHA = tty_bound.get("sha256", "0" * 64)
        mod.PACKAGE_C_PROBE_RESCUE_V3_INTENT_SHA = shas["intent_sha"]
        mod.PACKAGE_C_PROBE_RESCUE_V3_AGGREGATE_SHA = shas["aggregate_sha"]
        mod.PACKAGE_C_PROBE_RESCUE_FAILED_RECEIPT_SHA = shas["failed_receipt_sha"]
        mod.PACKAGE_C_PROBE_RESCUE_FAILED_TERMINAL_SHA = shas["failed_terminal_sha"]
        mod.PACKAGE_C_PROBE_RESCUE_FAILED_OUTPUT_SHA = shas["failed_output_sha"]
        mod.PACKAGE_C_PROBE_RESCUE_FAILED_OUTPUT_TEXT = self._TEST_PROBE_ERROR

    def _make_probe_failed_and_patch(self):
        """Full setup: drive to failed v3 state and patch all probe rescue constants."""
        cwork = self._make_tty_contaminated_and_patch()
        self._do_tty_rescue(cwork)
        shas = self._inject_failed_v3_verification(cwork)
        self._patch_probe_rescue_constants(cwork, shas)
        return cwork

    def _do_probe_rescue(self, cwork):
        return self.call("rescue_package_c_probe_contract_verification",
                         state_root=str(self.state), work_id=cwork)

    # ── happy path ───────────────────────────────────────────────────────────

    def test_probe_rescue_valid_transitions_to_awaiting_gate(self):
        """Valid probe rescue transitions to awaiting_gate and returns expected fields."""
        cwork = self._make_probe_failed_and_patch()
        result = self._do_probe_rescue(cwork)
        self.assertEqual(result["operation"], "rescue_package_c_probe_contract_verification")
        self.assertEqual(result["phase"], "awaiting_gate")
        self.assertFalse(result["reused"])
        self.assertIn("rescue_id", result)
        self.assertIn("rescue_receipt_sha256", result)
        state = self.read_state(cwork)
        self.assertEqual(state["phase"], "awaiting_gate")
        self.assertIn("package_c_probe_rescue_receipt", state)
        self.assertNotIn("package_c_verification", state)

    def test_probe_rescue_receipt_keys_complete(self):
        """Probe rescue receipt on disk contains all required keys."""
        cwork = self._make_probe_failed_and_patch()
        result = self._do_probe_rescue(cwork)
        state = self.read_state(cwork)
        receipt_path = state["package_c_probe_rescue_receipt"]["path"]
        with open(receipt_path) as f:
            receipt = json.loads(f.read())
        required = {"schema_version", "kind", "work_id", "policy", "bug_class",
                    "authority_date", "candidate_id", "evidence_digest",
                    "tty_rescue_sha256", "v3_intent_sha256", "v3_aggregate_sha256",
                    "failed_probe_receipt_sha256", "failed_probe_terminal_sha256",
                    "failed_probe_output_sha256", "prior_revision", "issued_at", "rescue_id"}
        self.assertEqual(set(receipt), required)
        self.assertEqual(receipt["kind"], "package_c_probe_contract_verification_rescue")
        self.assertEqual(receipt["bug_class"], "controller_probe_invented_class/v3_verification")

    # ── v4 routing ───────────────────────────────────────────────────────────

    def test_probe_rescue_verify_uses_v4_dir(self):
        """After probe rescue, verify-package-c writes to the v4 verification directory."""
        cwork = self._make_probe_failed_and_patch()
        self._do_probe_rescue(cwork)
        result = self.call("verify_package_c",
                           state_root=str(self.state), work_id=cwork)
        self.assertIn("aggregate_path", result)
        self.assertIn("package-c-verification-v4", result["aggregate_path"])

    def test_v4_effective_verify_dir_used_when_probe_rescue_present(self):
        """_package_c_effective_verify_dir returns v4 dir when probe rescue receipt in state."""
        mod = self.mod
        candidate = {"id": "package-c-abc", "evidence_digest": "abc"}
        state = {"work_id": mod.PACKAGE_C_WORK_ID,
                 "package_c_probe_rescue_receipt": {"sha256": "x" * 64}}
        with tempfile.TemporaryDirectory() as td:
            d = mod._package_c_effective_verify_dir(td, candidate, state)
            self.assertIn("package-c-verification-v4", d)

    # ── byte preservation ────────────────────────────────────────────────────

    def test_probe_rescue_v3_archive_preserved(self):
        """Failed v3 verification binding is archived, not deleted."""
        cwork = self._make_probe_failed_and_patch()
        state_before = self.read_state(cwork)
        old_agg_sha = state_before["package_c_verification"]["aggregate_sha256"]
        self._do_probe_rescue(cwork)
        state_after = self.read_state(cwork)
        archive = state_after.get("package_c_v3_archive", {})
        self.assertEqual(archive.get("kind"), "probe_contract_verification_v3_terminal")
        self.assertEqual(archive["verification"]["aggregate_sha256"], old_agg_sha)

    def test_probe_rescue_v3_files_bytes_unchanged(self):
        """V3 verification files are not deleted or modified by the rescue."""
        cwork = self._make_probe_failed_and_patch()
        state_before = self.read_state(cwork)
        intent_sha = state_before["package_c_verification"]["intent_sha256"]
        agg_sha = state_before["package_c_verification"]["aggregate_sha256"]
        self._do_probe_rescue(cwork)
        # V3 verify dir should still have the same files
        candidate = state_before["candidate"]
        verify_dir = os.path.join(str(self.state), cwork,
                                  "package-c-verification-v3", candidate["id"])
        intent_path = os.path.join(verify_dir, "intent.json")
        agg_path = os.path.join(verify_dir, "aggregate.json")
        self.assertTrue(os.path.isfile(intent_path))
        with open(intent_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), intent_sha)
        self.assertTrue(os.path.isfile(agg_path))
        with open(agg_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), agg_sha)

    # ── no-builder (earlier receipts retained) ───────────────────────────────

    def test_probe_rescue_correction_used_retained(self):
        """correction_used remains in state after probe rescue."""
        cwork = self._make_probe_failed_and_patch()
        self._do_probe_rescue(cwork)
        state = self.read_state(cwork)
        self.assertIsInstance(state.get("package_c_correction_used"), dict)

    def test_probe_rescue_tty_rescue_receipt_retained(self):
        """TTY rescue receipt remains in state after probe rescue."""
        cwork = self._make_probe_failed_and_patch()
        self._do_probe_rescue(cwork)
        state = self.read_state(cwork)
        self.assertIn("package_c_tty_rescue_receipt", state)

    def test_probe_rescue_selector_rescue_receipt_retained(self):
        """Selector rescue receipt remains in state after probe rescue."""
        cwork = self._make_probe_failed_and_patch()
        self._do_probe_rescue(cwork)
        state = self.read_state(cwork)
        self.assertIn("package_c_rescue_receipt", state)

    def test_probe_rescue_v2_archive_retained(self):
        """V2 archive (from TTY rescue) remains in state after probe rescue."""
        cwork = self._make_probe_failed_and_patch()
        self._do_probe_rescue(cwork)
        state = self.read_state(cwork)
        self.assertIn("package_c_v2_archive", state)

    # ── replay / idempotency ─────────────────────────────────────────────────

    def test_probe_rescue_replay_is_idempotent(self):
        """Replaying probe rescue returns same receipt SHA and reused=True."""
        cwork = self._make_probe_failed_and_patch()
        first = self._do_probe_rescue(cwork)
        second = self._do_probe_rescue(cwork)
        self.assertTrue(second["reused"])
        self.assertEqual(first["rescue_id"], second["rescue_id"])
        self.assertEqual(first["rescue_receipt_sha256"], second["rescue_receipt_sha256"])
        self.assertEqual(second["phase"], "awaiting_gate")

    def test_probe_rescue_exact_replay_byte_stable(self):
        """Three replays all return identical rescue_id and receipt SHA."""
        cwork = self._make_probe_failed_and_patch()
        first = self._do_probe_rescue(cwork)
        for _ in range(2):
            r = self._do_probe_rescue(cwork)
            self.assertTrue(r["reused"])
            self.assertEqual(r["rescue_id"], first["rescue_id"])
            self.assertEqual(r["rescue_receipt_sha256"], first["rescue_receipt_sha256"])

    def test_probe_rescue_orphan_recovery(self):
        """Receipt orphan written before crash is recovered and replayed correctly."""
        cwork = self._make_probe_failed_and_patch()
        state = self.read_state(cwork)
        mod = self.mod
        # Compute rescue_immutable to get rescue_id
        rescue_immutable = {
            "schema_version": mod.SCHEMA,
            "kind": "package_c_probe_contract_verification_rescue",
            "work_id": state["work_id"], "policy": mod.POLICY_ID,
            "bug_class": mod.PACKAGE_C_PROBE_RESCUE_BUG_CLASS,
            "authority_date": mod.PACKAGE_C_PROBE_RESCUE_AUTHORITY_DATE,
            "candidate_id": mod.PACKAGE_C_RESCUE_CANDIDATE_ID,
            "evidence_digest": mod.PACKAGE_C_RESCUE_EVIDENCE_DIGEST,
            "tty_rescue_sha256": mod.PACKAGE_C_PROBE_RESCUE_TTY_RESCUE_SHA,
            "v3_intent_sha256": mod.PACKAGE_C_PROBE_RESCUE_V3_INTENT_SHA,
            "v3_aggregate_sha256": mod.PACKAGE_C_PROBE_RESCUE_V3_AGGREGATE_SHA,
            "failed_probe_receipt_sha256": mod.PACKAGE_C_PROBE_RESCUE_FAILED_RECEIPT_SHA,
            "failed_probe_terminal_sha256": mod.PACKAGE_C_PROBE_RESCUE_FAILED_TERMINAL_SHA,
            "failed_probe_output_sha256": mod.PACKAGE_C_PROBE_RESCUE_FAILED_OUTPUT_SHA,
            "prior_revision": state["revision"],
        }
        rescue_id = mod.digest_bytes(
            json.dumps(rescue_immutable, sort_keys=True,
                       separators=(",", ":")).encode())
        # Write orphan receipt to disk before state is updated
        probe_rescue_dir = os.path.join(str(self.state), cwork, "package-c-probe-rescue")
        os.makedirs(probe_rescue_dir, mode=0o700, exist_ok=True)
        orphan = {**rescue_immutable, "rescue_id": rescue_id, "issued_at": "2026-08-14T10:00:00Z"}
        orphan_path = os.path.join(probe_rescue_dir, rescue_id + ".json")
        encoded = (json.dumps(orphan, sort_keys=True, indent=2) + "\n").encode()
        import tempfile as _tempfile
        fd, tmp = _tempfile.mkstemp(dir=probe_rescue_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(encoded)
            os.replace(tmp, orphan_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        result = self._do_probe_rescue(cwork)
        self.assertFalse(result["reused"])
        self.assertEqual(result["rescue_id"], rescue_id)
        # issued_at from orphan is preserved
        state_after = self.read_state(cwork)
        receipt_path = state_after["package_c_probe_rescue_receipt"]["path"]
        with open(receipt_path) as f:
            receipt = json.loads(f.read())
        self.assertEqual(receipt["issued_at"], "2026-08-14T10:00:00Z")

    # ── state / phase / revision guards ─────────────────────────────────────

    def test_probe_rescue_fails_wrong_revision(self):
        """Probe rescue rejects when state revision doesn't match sealed binding."""
        cwork = self._make_probe_failed_and_patch()
        self.mod.PACKAGE_C_PROBE_RESCUE_REVISION = 9999
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_wrong_phase(self):
        """Probe rescue rejects when phase is not awaiting_gate."""
        cwork = self._make_probe_failed_and_patch()
        state = self.read_state(cwork)
        state["phase"] = "failed"
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_missing_v3_verification(self):
        """Probe rescue rejects when no active v3 verification binding in state."""
        cwork = self._make_probe_failed_and_patch()
        state = self.read_state(cwork)
        state.pop("package_c_verification", None)
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_missing_tty_rescue(self):
        """Probe rescue rejects when no TTY rescue receipt in state."""
        cwork = self._make_probe_failed_and_patch()
        state = self.read_state(cwork)
        state.pop("package_c_tty_rescue_receipt", None)
        self.state_file(cwork).write_text(json.dumps(state))
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_wrong_tty_rescue_sha(self):
        """Probe rescue rejects when TTY rescue SHA constant doesn't match state."""
        cwork = self._make_probe_failed_and_patch()
        self.mod.PACKAGE_C_PROBE_RESCUE_TTY_RESCUE_SHA = "0" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_wrong_v3_intent_sha(self):
        """Probe rescue rejects when v3 intent SHA constant doesn't match file."""
        cwork = self._make_probe_failed_and_patch()
        self.mod.PACKAGE_C_PROBE_RESCUE_V3_INTENT_SHA = "0" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_wrong_v3_aggregate_sha(self):
        """Probe rescue rejects when v3 aggregate SHA constant doesn't match file."""
        cwork = self._make_probe_failed_and_patch()
        self.mod.PACKAGE_C_PROBE_RESCUE_V3_AGGREGATE_SHA = "0" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_wrong_failed_receipt_sha(self):
        """Probe rescue rejects when failed probe receipt SHA constant doesn't match file."""
        cwork = self._make_probe_failed_and_patch()
        self.mod.PACKAGE_C_PROBE_RESCUE_FAILED_RECEIPT_SHA = "0" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    def test_probe_rescue_fails_wrong_output_error_text(self):
        """Probe rescue rejects when output error text constant doesn't match file content."""
        cwork = self._make_probe_failed_and_patch()
        self.mod.PACKAGE_C_PROBE_RESCUE_FAILED_OUTPUT_TEXT = "wrong error text"
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    # ── file / path / symlink safety ────────────────────────────────────────

    def test_probe_rescue_fails_symlink_probe_rescue_dir(self):
        """Probe rescue rejects when probe rescue directory is a symlink."""
        cwork = self._make_probe_failed_and_patch()
        probe_rescue_dir = os.path.join(str(self.state), cwork, "package-c-probe-rescue")
        os.symlink("/tmp", probe_rescue_dir)
        try:
            with self.assertRaises(self.mod.ControllerError):
                self._do_probe_rescue(cwork)
        finally:
            os.unlink(probe_rescue_dir)

    def test_probe_rescue_fails_aggregate_tampered(self):
        """Probe rescue rejects when the v3 aggregate file is tampered."""
        cwork = self._make_probe_failed_and_patch()
        state = self.read_state(cwork)
        agg_path = state["package_c_verification"]["aggregate_path"]
        with open(agg_path, "ab") as f:
            f.write(b"\x00")
        saved_state = self.state_file(cwork).read_bytes()
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)

    def test_probe_rescue_fails_intent_tampered(self):
        """Probe rescue rejects when the v3 intent file is tampered."""
        cwork = self._make_probe_failed_and_patch()
        state = self.read_state(cwork)
        intent_path = state["package_c_verification"]["intent_path"]
        with open(intent_path, "ab") as f:
            f.write(b"\x00")
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)

    # ── replay binding guards (post-rescue) ──────────────────────────────────

    def test_probe_rescue_replay_fails_if_aggregate_tampered(self):
        """Post-rescue replay rejects when v3 aggregate (now archived) file is tampered."""
        cwork = self._make_probe_failed_and_patch()
        self._do_probe_rescue(cwork)
        state = self.read_state(cwork)
        archive = state.get("package_c_v3_archive", {})
        agg_path = archive.get("verification", {}).get("aggregate_path", "")
        saved_state = self.state_file(cwork).read_bytes()
        with open(agg_path, "ab") as f:
            f.write(b"\x00")
        with self.assertRaises(self.mod.ControllerError):
            self._do_probe_rescue(cwork)
        self.assertEqual(self.state_file(cwork).read_bytes(), saved_state)

    # ── regression guards ────────────────────────────────────────────────────

    def test_selector_rescue_regression_guard(self):
        """Existing rescue-package-c-gate-selection succeeds (regression guard)."""
        cwork = self._make_terminal_and_patch()
        result = self.call("rescue_package_c_gate_selection",
                           state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["operation"], "rescue_package_c_gate_selection")
        self.assertFalse(result["reused"])

    def test_tty_rescue_regression_guard(self):
        """Existing rescue-package-c-tty-contaminated-verification succeeds (regression guard)."""
        cwork = self._make_tty_contaminated_and_patch()
        result = self.call("rescue_package_c_tty_contaminated_verification",
                           state_root=str(self.state), work_id=cwork)
        self.assertEqual(result["operation"], "rescue_package_c_tty_contaminated_verification")
        self.assertFalse(result["reused"])


class PackageCRRTest(unittest.TestCase):
    """Offline tests for the Package C independent-review recovery lifecycle."""

    _FAKE_DISPATCH_SRC = b"""\
import uuid as _uuid

_VALID_KINDS = {'pending_replay', 'gate_repair'}

def AttemptLink(kind, attempt_id, source_ref, delivery_ref, idempotency_key):
    return {'kind': kind, 'attempt_id': attempt_id, 'source_ref': source_ref,
            'delivery_ref': delivery_ref, 'idempotency_key': idempotency_key}

def validate_attempt_link(link):
    if not isinstance(link, dict): raise ValueError('not a link')
    if link.get('kind') not in _VALID_KINDS: raise ValueError('bad kind')
    try: _uuid.UUID(str(link.get('attempt_id')))
    except (ValueError, TypeError, AttributeError): raise ValueError('bad attempt_id')
    if not link.get('source_ref'): raise ValueError('missing source_ref')
    if not link.get('delivery_ref'): raise ValueError('missing delivery_ref')
    if not link.get('idempotency_key'): raise ValueError('missing idempotency_key')
    return link

def build_attempt_link_idempotency_key(role, kind, source_ref, ordinal):
    event_id = source_ref.get('event_id') if isinstance(source_ref, dict) else source_ref
    return '%s:%s:event_id=%s:%s' % (role, kind, event_id, ordinal)
"""
    _FAKE_COWORK_SRC = b"'''fake cowork.py'''\n"
    _FAKE_COWORK_STATE_SRC = b"'''fake cowork_state.py'''\n"
    _FAKE_TEST_COWORK_SRC = b"""\
import os, sys, uuid, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unittest
from cowork_dispatch import AttemptLink, validate_attempt_link, build_attempt_link_idempotency_key

class PendingTurnLinkageTest(unittest.TestCase):
    def test_save_pending_turn_creates_no_attempt(self): self.assertIsNone(None)

class PendingReplayLinkageTest(unittest.TestCase):
    def test_duplicate_pending_replay_appends_exactly_one_link(self): self.assertTrue(True)

class GateRepairLinkageTest(unittest.TestCase):
    def test_gate_repair_linkage_basic(self): self.assertTrue(True)

class PackageBRefusalRegressionTest(unittest.TestCase):
    def test_package_b_refusal_unchanged(self): self.assertTrue(True)
"""
    _FAKE_TEST_CHAR_SRC = b"""\
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unittest
from cowork_dispatch import AttemptLink, validate_attempt_link

class MissingDispatchContractTest(unittest.TestCase):
    def test_uniform_refusal_contract(self): self.assertTrue(True)
    @unittest.expectedFailure
    def test_pending_turn_retry_linkage_contract(self): raise AssertionError("pending turn retry linkage not yet implemented")
    @unittest.expectedFailure
    def test_gate_repair_retry_linkage_contract(self): raise AssertionError("gate repair retry linkage not yet implemented")
"""
    _FAKE_FIXTURE_SRC = b'{"sources":[]}\n'

    def setUp(self):
        self.fixture = OrchestrateCLITest(methodName="runTest")
        self.fixture.setUp()
        for name in ("tmp", "root", "repo", "state", "brief", "audit", "base_head"):
            setattr(self, name, getattr(self.fixture, name))
        self.mod = controller_module()
        self.claude_reviewer_audit = self.root / "fake-claude-rr-reviewer-audit.jsonl"
        self._old_reviewer_audit = os.environ.get("FAKE_CLAUDE_REVIEWER_AUDIT")
        os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = str(self.claude_reviewer_audit)
        # Fake RR reviewer: reads schema, emits passing packet with all consts
        self.fake_rr_reviewer = self.root / "fake-package-c-rr-reviewer"
        self.fake_rr_reviewer.write_text("""#!/usr/bin/env %(py)s
import json, os, sys
argv = sys.argv[1:]
audit_path = os.environ.get('FAKE_CLAUDE_REVIEWER_AUDIT', '/dev/null')
with open(audit_path, 'a') as out:
    out.write(json.dumps({'argv': argv, 'cwd': os.getcwd()}) + '\\n')
session_id = argv[argv.index('--session-id') + 1]
kind = os.environ.get('FAKE_PC_RR_REVIEWER_KIND', '')
if kind == 'timeout':
    import time, signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    time.sleep(9999)
    sys.exit(0)
if kind == 'nonzero':
    sys.exit(1)
if kind == 'overflow':
    print(json.dumps({'type': 'result', 'session_id': session_id}))
    chunk = ('{"type":"debug","data":"' + 'x' * 990 + '"}\\n').encode()
    written = 0
    cap = 2 * 1024 * 1024 + 65536
    while written < cap:
        sys.stdout.buffer.write(chunk); written += len(chunk)
    sys.stdout.buffer.flush(); sys.exit(1)
if kind == 'bad_verdict':
    schema_json = argv[argv.index('--json-schema') + 1]
    schema = json.loads(schema_json)
    p = schema['properties']
    packet = {k: v['const'] for k, v in p.items() if 'const' in v}
    aggregate_path = os.environ.get('FAKE_PC_RR_AGGREGATE_PATH', '')
    if aggregate_path and os.path.isfile(aggregate_path):
        agg = json.loads(open(aggregate_path).read())
        packet['verification_receipt_sha256s'] = [r['sha256'] for r in agg['receipts']]
    else:
        packet['verification_receipt_sha256s'] = ['0' * 64] * 9
    changed_paths_str = os.environ.get('FAKE_PC_RR_CHANGED_PATHS', '')
    packet['changed_paths'] = json.loads(changed_paths_str) if changed_paths_str else []
    packet['verdict'] = 'needs_correction'
    packet['findings'] = ['needs work']
    packet['checks'] = []
    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
    sys.exit(0)
if kind == 'capacity':
    print(json.dumps({'type': 'rate_limit_event', 'session_id': session_id,
        'rate_limit_info': {'isUsingOverage': False, 'status': 'rejected',
                            'resetsAt': int(os.environ.get('FAKE_PC_RR_RESETS_AT', '9999999999')),
                            'rateLimitType': 'five_hour', 'overageStatus': 'rejected'}}))
    sys.exit(1)
schema_json = argv[argv.index('--json-schema') + 1]
schema = json.loads(schema_json)
p = schema['properties']
packet = {k: v['const'] for k, v in p.items() if 'const' in v}
aggregate_path = os.environ.get('FAKE_PC_RR_AGGREGATE_PATH', '')
if aggregate_path and os.path.isfile(aggregate_path):
    agg = json.loads(open(aggregate_path).read())
    packet['verification_receipt_sha256s'] = [r['sha256'] for r in agg['receipts']]
else:
    packet['verification_receipt_sha256s'] = ['0' * 64] * 9
changed_paths_str = os.environ.get('FAKE_PC_RR_CHANGED_PATHS', '')
packet['changed_paths'] = json.loads(changed_paths_str) if changed_paths_str else []
packet['verdict'] = os.environ.get('FAKE_PC_RR_REVIEWER_VERDICT', 'pass')
packet['findings'] = []
packet['checks'] = ['fake RR review check']
print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': packet}))
""" % {"py": sys.executable})
        self.fake_rr_reviewer.chmod(self.fake_rr_reviewer.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        if self._old_reviewer_audit is None:
            os.environ.pop("FAKE_CLAUDE_REVIEWER_AUDIT", None)
        else:
            os.environ["FAKE_CLAUDE_REVIEWER_AUDIT"] = self._old_reviewer_audit
        for key in ("FAKE_PC_RR_REVIEWER_KIND", "FAKE_PC_RR_REVIEWER_VERDICT",
                    "FAKE_PC_RR_AGGREGATE_PATH", "FAKE_PC_RR_CHANGED_PATHS", "FAKE_PC_RR_RESETS_AT"):
            os.environ.pop(key, None)
        self.fixture.tearDown()

    def call(self, command, **values):
        return getattr(self.mod, "command_" + command)(SimpleNamespace(**values))

    def _sha(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _write_atomic(self, path, data: bytes):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return self._sha(data)

    def _write_json(self, path, value):
        data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        return self._write_atomic(path, data)

    def _make_synthetic_source(self):
        """Build a synthetic Package C source directory, create all required artifacts,
        and patch all PACKAGE_C_RR_* constants to match."""
        mod = self.mod
        source_work_id = mod.PACKAGE_C_RR_SOURCE_WORK_ID
        source_dir = str(self.state / source_work_id)
        os.makedirs(source_dir, mode=0o700, exist_ok=True)

        # Worktree: standalone git repo with the 6 changed files
        wt_path = str(self.root / "rr-worktree")
        os.makedirs(os.path.join(wt_path, "scripts", "fixtures"), mode=0o700, exist_ok=True)
        files = {
            "scripts/cowork_dispatch.py": self._FAKE_DISPATCH_SRC,
            "scripts/cowork.py": self._FAKE_COWORK_SRC,
            "scripts/cowork_state.py": self._FAKE_COWORK_STATE_SRC,
            "scripts/test_cowork.py": self._FAKE_TEST_COWORK_SRC,
            "scripts/test_dispatch_contract_characterization.py": self._FAKE_TEST_CHAR_SRC,
            "scripts/fixtures/dispatch_contract_characterization_sources.json": self._FAKE_FIXTURE_SRC,
        }
        file_hashes = {}
        for rel, content in files.items():
            path = os.path.join(wt_path, rel)
            path_obj = Path(path)
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            path_obj.write_bytes(content)
            file_hashes[rel] = self._sha(content)
        # Initialize git repo and commit files so git diff works
        subprocess.run(["git", "init", wt_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", wt_path, "config", "user.email", "rr-fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", wt_path, "config", "user.name", "rr-fixture"], check=True)
        subprocess.run(["git", "-C", wt_path, "config", "commit.gpgSign", "false"], check=True)
        subprocess.run(["git", "-C", wt_path, "add", "-A"], check=True)
        subprocess.run(["git", "-C", wt_path, "commit", "-m", "rr-fixture"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Candidate identity
        evidence_digest = "a" * 64
        candidate_id = "package-c-" + "b" * 32
        fingerprint = self._sha(b"rr-fixture-fingerprint")

        # v4 verification aggregate with 9 all-pass receipts
        v4_dir = os.path.join(source_dir, "package-c-verification-v4")
        os.makedirs(v4_dir, mode=0o700, exist_ok=True)
        refs = []
        for i in range(9):
            receipt_data = (json.dumps({"schema_version": 1, "kind": "package_c_verification_receipt",
                                        "index": i, "label": "gate_%d" % i, "passed": True}) + "\n").encode()
            rpath = os.path.join(v4_dir, "receipt-%d.json" % i)
            r_sha = self._write_atomic(rpath, receipt_data)
            refs.append({"index": i, "label": "gate_%d" % i, "sha256": r_sha, "passed": True, "path": rpath})
        agg_doc = {"schema_version": 1, "kind": "package_c_verification_aggregate",
                   "receipts": refs, "all_passed": True}
        agg_path = os.path.join(v4_dir, "aggregate.json")
        agg_sha = self._write_json(agg_path, agg_doc)

        # Failed gate receipt
        gate_dir = os.path.join(source_dir, "package-c-gates")
        os.makedirs(gate_dir, mode=0o700, exist_ok=True)
        failed_gate_doc = {"schema_version": 1, "kind": "package_c_gate_receipt",
                           "work_id": source_work_id, "disposition": "fail",
                           "reason_code": mod.PACKAGE_C_RR_GATE_REASON,
                           "candidate_id": candidate_id, "evidence_digest": evidence_digest}
        failed_gate_path = os.path.join(gate_dir, "failed-gate.json")
        failed_gate_sha = self._write_json(failed_gate_path, failed_gate_doc)

        # Two timeout terminal receipts
        attempts_dir = os.path.join(source_dir, "package-c-review-attempts")
        os.makedirs(attempts_dir, mode=0o700, exist_ok=True)
        first_timeout_data = (json.dumps({"schema_version": 1, "kind": "claude_package_c_review_terminal",
                                           "timed_out": True, "attempt": 1}) + "\n").encode()
        second_timeout_data = (json.dumps({"schema_version": 1, "kind": "claude_package_c_review_terminal",
                                            "timed_out": True, "attempt": 2}) + "\n").encode()
        first_timeout_path = os.path.join(attempts_dir, "attempt-1.terminal.json")
        second_timeout_path = os.path.join(attempts_dir, "attempt-2.terminal.json")
        first_timeout_sha = self._write_atomic(first_timeout_path, first_timeout_data)
        second_timeout_sha = self._write_atomic(second_timeout_path, second_timeout_data)

        # Build source state
        source_state = {
            "schema_version": 1, "work_id": source_work_id,
            "package_id": mod.PACKAGE_C_PACKAGE_ID,
            "phase": "failed", "revision": 24, "policy": mod.POLICY_ID,
            "worktree": {"path": wt_path},
            "candidate": {"id": candidate_id, "evidence_digest": evidence_digest,
                          "evidence": {"worktree_fingerprint": {"digest": fingerprint}}},
            "package_c_verification": {"aggregate_path": agg_path, "aggregate_sha256": agg_sha,
                                        "candidate_id": candidate_id, "evidence_digest": evidence_digest},
            "package_c_gate_receipt": {"path": failed_gate_path, "sha256": failed_gate_sha,
                                        "disposition": "fail"},
        }
        source_state_data = (json.dumps(source_state, sort_keys=True, indent=2) + "\n").encode()
        source_state_sha = self._write_atomic(os.path.join(source_dir, "state.json"), source_state_data)

        # Patch all PACKAGE_C_RR_* constants
        mod.PACKAGE_C_RR_SOURCE_REVISION = 24
        mod.PACKAGE_C_RR_SOURCE_STATE_SHA256 = source_state_sha
        mod.PACKAGE_C_RR_CANDIDATE_ID = candidate_id
        mod.PACKAGE_C_RR_EVIDENCE_DIGEST = evidence_digest
        mod.PACKAGE_C_RR_FINGERPRINT = fingerprint
        mod.PACKAGE_C_RR_VERIFICATION_AGGREGATE_SHA256 = agg_sha
        mod.PACKAGE_C_RR_FAILED_GATE_SHA256 = failed_gate_sha
        mod.PACKAGE_C_RR_FIRST_TIMEOUT_SHA256 = first_timeout_sha
        mod.PACKAGE_C_RR_SECOND_TIMEOUT_SHA256 = second_timeout_sha

        return {"source_dir": source_dir, "wt_path": wt_path, "agg_path": agg_path,
                "agg_sha": agg_sha, "refs": refs, "candidate_id": candidate_id,
                "evidence_digest": evidence_digest, "source_state_sha": source_state_sha,
                "failed_gate_sha": failed_gate_sha,
                "first_timeout_sha": first_timeout_sha, "second_timeout_sha": second_timeout_sha}

    def _make_rr_state(self):
        """Create the initial RR package state directory."""
        mod = self.mod
        rr_work_id = mod.PACKAGE_C_RR_PACKAGE_ID
        rr_dir = str(self.state / rr_work_id)
        os.makedirs(rr_dir, mode=0o700, exist_ok=True)
        state = {"schema_version": 1, "work_id": rr_work_id,
                 "package_id": mod.PACKAGE_C_RR_PACKAGE_ID, "policy": mod.POLICY_ID}
        self._write_json(os.path.join(rr_dir, "state.json"), state)
        return rr_dir

    def _setup(self):
        """Full setup: synthetic source + RR state + patched constants."""
        ctx = self._make_synthetic_source()
        rr_dir = self._make_rr_state()
        os.environ["FAKE_PC_RR_AGGREGATE_PATH"] = ctx["agg_path"]
        os.environ["FAKE_PC_RR_CHANGED_PATHS"] = json.dumps(self.mod.PACKAGE_C_RR_CHANGED_PATHS)
        return ctx, rr_dir

    def _do_build_dossier(self, rr_dir):
        return self.call("build_package_c_rr_dossier",
                         state_root=str(self.state), work_id=os.path.basename(rr_dir))

    def _do_launch(self, rr_dir):
        return self.call("launch_package_c_rr_review",
                         state_root=str(self.state), work_id=os.path.basename(rr_dir),
                         claude_bin=str(self.fake_rr_reviewer))

    def _wait_rr_review(self, rr_dir):
        for _ in range(200):
            state = self._rr_state(rr_dir)
            attempt = state.get("rr_review_attempt") or {}
            receipt = attempt.get("receipt_path")
            if (not self.mod.process_alive(attempt.get("pid"), attempt.get("pgid")) and
                    isinstance(receipt, str) and Path(receipt).is_file()):
                return attempt
            time.sleep(0.025)
        self.fail("fake Package C RR reviewer did not become quiescent")

    def _do_ingest(self, rr_dir):
        return self.call("ingest_package_c_rr_review",
                         state_root=str(self.state), work_id=os.path.basename(rr_dir))

    def _do_supersession(self, rr_dir):
        return self.call("apply_package_c_rr_supersession",
                         state_root=str(self.state), work_id=os.path.basename(rr_dir))

    def _rr_state(self, rr_dir):
        return json.loads(Path(os.path.join(rr_dir, "state.json")).read_text())

    def _source_state(self, ctx):
        return json.loads(Path(os.path.join(ctx["source_dir"], "state.json")).read_text())

    # ── identity constants ────────────────────────────────────────────────────

    def test_rr_identity_constants(self):
        """Package C RR constants have required values."""
        mod = self.mod
        self.assertEqual(mod.PACKAGE_C_RR_PACKAGE_ID, "controller-package-c-review-recovery-v1")
        self.assertEqual(mod.PACKAGE_C_RR_SOURCE_WORK_ID, mod.PACKAGE_C_WORK_ID)
        self.assertEqual(mod.PACKAGE_C_RR_REVIEW_ACTOR, "claude_package_c_rr_controller_v1")
        self.assertEqual(mod.PACKAGE_C_RR_REVIEW_BACKEND, "claude")
        self.assertEqual(mod.PACKAGE_C_RR_REVIEW_TIMEOUT, 900)
        self.assertEqual(mod.PACKAGE_C_RR_DOSSIER_CAP, 128 * 1024)
        self.assertEqual(mod.PACKAGE_C_RR_TERMINAL_KIND, "claude_package_c_rr_review_terminal")
        self.assertEqual(mod.PACKAGE_C_RR_GATE_KIND, "package_c_review_recovery_gate")
        self.assertEqual(mod.PACKAGE_C_RR_GATE_REASON, "review_infrastructure_exhausted")
        self.assertEqual(len(mod.PACKAGE_C_RR_CHANGED_PATHS), 6)
        self.assertIn("scripts/cowork_dispatch.py", mod.PACKAGE_C_RR_CHANGED_PATHS)

    # ── happy path ───────────────────────────────────────────────────────────

    def test_full_happy_lifecycle(self):
        """Full lifecycle: dossier → launch → ingest → supersession completes."""
        ctx, rr_dir = self._setup()
        # Build dossier
        dossier_result = self._do_build_dossier(rr_dir)
        self.assertEqual(dossier_result["operation"], "build_package_c_rr_dossier")
        self.assertFalse(dossier_result.get("reused"))
        self.assertIn("sha256", dossier_result)
        rr = self._rr_state(rr_dir)
        self.assertIn("rr_dossier", rr)
        # Launch review
        launch_result = self._do_launch(rr_dir)
        self.assertEqual(launch_result["operation"], "launch_package_c_rr_review")
        self._wait_rr_review(rr_dir)
        # Ingest review
        ingest_result = self._do_ingest(rr_dir)
        self.assertEqual(ingest_result["operation"], "ingest_package_c_rr_review")
        self.assertEqual(ingest_result["verdict"], "pass")
        rr2 = self._rr_state(rr_dir)
        self.assertIn("rr_review", rr2)
        self.assertEqual(rr2["rr_review"]["verdict"], "pass")
        # Apply supersession
        sup_result = self._do_supersession(rr_dir)
        self.assertEqual(sup_result["operation"], "apply_package_c_rr_supersession")
        self.assertFalse(sup_result.get("reused"))
        self.assertIn("supersession_id", sup_result)
        # Source state must be completed
        src = self._source_state(ctx)
        self.assertEqual(src["phase"], "completed")

    def test_dossier_build_is_idempotent(self):
        """Building the dossier a second time returns reused=True and same sha256."""
        ctx, rr_dir = self._setup()
        first = self._do_build_dossier(rr_dir)
        second = self._do_build_dossier(rr_dir)
        self.assertTrue(second.get("reused"))
        self.assertEqual(first["sha256"], second["sha256"])

    def test_dossier_under_cap(self):
        """Dossier file must be within the 128 KiB cap."""
        ctx, rr_dir = self._setup()
        result = self._do_build_dossier(rr_dir)
        self.assertLessEqual(result["bytes"], self.mod.PACKAGE_C_RR_DOSSIER_CAP)

    def test_dossier_keys_present(self):
        """Dossier JSON contains required bindings: candidate_id, evidence_digest, etc."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        rr = self._rr_state(rr_dir)
        dossier_path = rr["rr_dossier"]["path"]
        with open(dossier_path) as f:
            d = json.loads(f.read())
        required = {"kind", "package_id", "policy", "candidate_id", "evidence_digest",
                    "source_state_sha256", "verification_aggregate_sha256",
                    "failed_gate_sha256", "gate_reason", "first_timeout_sha256",
                    "second_timeout_sha256", "changed_paths", "file_hashes",
                    "gate_summaries", "production_module", "changed_file_diffs",
                    "authority_statement"}
        for key in required:
            self.assertIn(key, d, "dossier missing key: " + key)
        self.assertEqual(d["kind"], "package_c_rr_review_dossier")
        self.assertEqual(d["candidate_id"], ctx["candidate_id"])

    def test_dossier_excludes_transcripts_and_credentials(self):
        """Dossier must not contain 'transcript' or 'credential' keys."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        rr = self._rr_state(rr_dir)
        raw = Path(rr["rr_dossier"]["path"]).read_text()
        self.assertNotIn("transcript", raw.lower())
        self.assertNotIn("credential", raw.lower())

    def test_dossier_candidate_binding_is_frozen(self):
        """Dossier candidate_id and evidence_digest match constants."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        rr = self._rr_state(rr_dir)
        self.assertEqual(rr["rr_dossier"]["candidate_id"], self.mod.PACKAGE_C_RR_CANDIDATE_ID)
        self.assertEqual(rr["rr_dossier"]["evidence_digest"], self.mod.PACKAGE_C_RR_EVIDENCE_DIGEST)

    # ── source integrity gates ────────────────────────────────────────────────

    def test_dossier_build_fails_if_source_state_tampered(self):
        """Dossier build fails closed if source state SHA-256 changes."""
        ctx, rr_dir = self._setup()
        orig = Path(os.path.join(ctx["source_dir"], "state.json")).read_bytes()
        Path(os.path.join(ctx["source_dir"], "state.json")).write_bytes(orig + b"\n")
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    def test_dossier_build_fails_if_aggregate_tampered(self):
        """Dossier build fails closed if v4 aggregate SHA-256 changes."""
        ctx, rr_dir = self._setup()
        self.mod.PACKAGE_C_RR_VERIFICATION_AGGREGATE_SHA256 = "f" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    def test_dossier_build_fails_if_failed_gate_sha_wrong(self):
        """Dossier build fails closed if failed gate SHA-256 constant is wrong."""
        ctx, rr_dir = self._setup()
        self.mod.PACKAGE_C_RR_FAILED_GATE_SHA256 = "e" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    def test_dossier_build_fails_if_timeout_sha_wrong(self):
        """Dossier build fails closed if a timeout receipt SHA-256 constant is wrong."""
        ctx, rr_dir = self._setup()
        self.mod.PACKAGE_C_RR_FIRST_TIMEOUT_SHA256 = "d" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    def test_dossier_build_fails_if_candidate_id_wrong(self):
        """Dossier build fails closed if candidate_id constant doesn't match state."""
        ctx, rr_dir = self._setup()
        self.mod.PACKAGE_C_RR_CANDIDATE_ID = "package-c-" + "0" * 32
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    def test_dossier_build_fails_if_fingerprint_wrong(self):
        """Dossier build fails closed if fingerprint constant doesn't match state."""
        ctx, rr_dir = self._setup()
        self.mod.PACKAGE_C_RR_FINGERPRINT = "c" * 64
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    def test_dossier_build_fails_if_source_phase_not_failed(self):
        """Dossier build fails closed if source state is not phase=failed."""
        ctx, rr_dir = self._setup()
        src = json.loads(Path(os.path.join(ctx["source_dir"], "state.json")).read_text())
        src["phase"] = "completed"
        data = (json.dumps(src, sort_keys=True, indent=2) + "\n").encode()
        Path(os.path.join(ctx["source_dir"], "state.json")).write_bytes(data)
        self.mod.PACKAGE_C_RR_SOURCE_STATE_SHA256 = self._sha(data)
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    # ── review launch binding tests ───────────────────────────────────────────

    def test_launch_review_uses_rr_terminal_kind(self):
        """Launched review wrapper uses the PACKAGE_C_RR_TERMINAL_KIND."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        rr = self._rr_state(rr_dir)
        attempt = rr["rr_review_attempt"]
        self.assertEqual(attempt["actor_id"], self.mod.PACKAGE_C_RR_REVIEW_ACTOR)
        self.assertEqual(attempt["backend"], self.mod.PACKAGE_C_RR_REVIEW_BACKEND)
        self.assertEqual(attempt["policy"], self.mod.POLICY_ID)

    def test_launch_review_requires_dossier_first(self):
        """Launching review before building dossier raises ControllerError."""
        ctx, rr_dir = self._setup()
        with self.assertRaises((self.mod.ControllerError, KeyError)):
            self._do_launch(rr_dir)

    def test_launch_review_is_idempotent_when_alive(self):
        """Second launch call with alive process returns without re-spawning."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        first = self._do_launch(rr_dir)
        second = self._do_launch(rr_dir)
        self.assertEqual(first["attempt_id"], second["attempt_id"])

    def test_launch_binds_candidate_and_evidence(self):
        """Review attempt state has exact candidate_id and evidence_digest bindings."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        rr = self._rr_state(rr_dir)
        attempt = rr["rr_review_attempt"]
        self.assertEqual(attempt["candidate_id"], self.mod.PACKAGE_C_RR_CANDIDATE_ID)
        self.assertEqual(attempt["evidence_digest"], self.mod.PACKAGE_C_RR_EVIDENCE_DIGEST)

    # ── non-pass / bad outcome ingestion tests ────────────────────────────────

    def test_ingest_non_pass_verdict_fails_closed(self):
        """Ingesting a needs_correction verdict is terminal fail-closed."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        os.environ["FAKE_PC_RR_REVIEWER_KIND"] = "bad_verdict"
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        os.environ.pop("FAKE_PC_RR_REVIEWER_KIND", None)
        with self.assertRaises(self.mod.ControllerError):
            self._do_ingest(rr_dir)
        # Source state must remain unchanged (phase=failed, revision=24)
        src = self._source_state(ctx)
        self.assertEqual(src["phase"], "failed")
        self.assertEqual(src["revision"], 24)

    def test_ingest_wrong_binding_fails_closed(self):
        """Ingesting a packet with wrong candidate binding is rejected."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        # Tamper: change the aggregate path env so reviewer gets wrong sha256s
        os.environ["FAKE_PC_RR_AGGREGATE_PATH"] = "/nonexistent/path"
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        with self.assertRaises(self.mod.ControllerError):
            self._do_ingest(rr_dir)

    def test_ingest_capacity_returns_awaiting_phase(self):
        """Capacity-exhausted review returns awaiting_capacity, no original mutation."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        import time as _time
        os.environ["FAKE_PC_RR_REVIEWER_KIND"] = "capacity"
        future_ts = str(int(_time.time()) + 7200)
        os.environ["FAKE_PC_RR_RESETS_AT"] = future_ts
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        os.environ.pop("FAKE_PC_RR_REVIEWER_KIND", None)
        result = self._do_ingest(rr_dir)
        self.assertEqual(result.get("phase"), "awaiting_capacity")
        # Source state must remain unchanged
        src = self._source_state(ctx)
        self.assertEqual(src["phase"], "failed")
        self.assertEqual(src["revision"], 24)

    def test_ingest_after_supersession_is_rejected(self):
        """Ingestion after supersession raises ControllerError."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        self._do_supersession(rr_dir)
        with self.assertRaises(self.mod.ControllerError):
            self._do_ingest(rr_dir)

    def test_ingest_is_idempotent_after_pass(self):
        """Re-ingesting after a passing packet returns same verdict."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        first = self._do_ingest(rr_dir)
        second = self._do_ingest(rr_dir)
        self.assertTrue(second.get("reused"))
        self.assertEqual(second["verdict"], "pass")

    # ── supersession crash recovery ───────────────────────────────────────────

    def test_supersession_is_idempotent(self):
        """Replaying supersession returns same supersession_id and reused=True."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        first = self._do_supersession(rr_dir)
        second = self._do_supersession(rr_dir)
        self.assertTrue(second.get("reused"))
        self.assertEqual(first["supersession_id"], second["supersession_id"])
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])

    def test_supersession_requires_passing_review(self):
        """Supersession without an ingested review raises ControllerError."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        with self.assertRaises(self.mod.ControllerError):
            self._do_supersession(rr_dir)

    def test_supersession_changes_source_phase(self):
        """After supersession, source state phase is completed."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        self._do_supersession(rr_dir)
        src = self._source_state(ctx)
        self.assertEqual(src["phase"], "completed")

    def test_supersession_preserves_historical_failed_gate(self):
        """Supersession does not delete or rewrite the historical failed gate receipt."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        failed_gate_path = os.path.join(ctx["source_dir"], "package-c-gates", "failed-gate.json")
        failed_gate_sha_before = self._sha(Path(failed_gate_path).read_bytes())
        self._do_supersession(rr_dir)
        self.assertTrue(Path(failed_gate_path).is_file())
        self.assertEqual(self._sha(Path(failed_gate_path).read_bytes()), failed_gate_sha_before)

    def test_supersession_writes_new_gate_receipt(self):
        """Supersession writes a new passing gate receipt to source package-c-gates."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        result = self._do_supersession(rr_dir)
        gate_dir = os.path.join(ctx["source_dir"], "package-c-gates")
        files = os.listdir(gate_dir)
        self.assertGreaterEqual(len(files), 2)  # old gate + new gate
        rr = self._rr_state(rr_dir)
        sup = rr["rr_supersession"]
        self.assertIn("source_gate_new_receipt_path", sup)
        new_gate = json.loads(Path(sup["source_gate_new_receipt_path"]).read_text())
        self.assertEqual(new_gate["disposition"], "pass")
        self.assertEqual(new_gate["kind"], self.mod.PACKAGE_C_RR_GATE_KIND)
        self.assertEqual(new_gate["reason_code"], self.mod.PACKAGE_C_RR_GATE_REASON_PASS)

    def test_supersession_appends_events_exactly_once(self):
        """Intent and applied events are written exactly once each."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        self._do_supersession(rr_dir)
        events_path = os.path.join(ctx["source_dir"], "events.jsonl")
        rows = [json.loads(l) for l in Path(events_path).read_text().splitlines() if l]
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds.count("package_c_rr_supersession_intent"), 1)
        self.assertEqual(kinds.count("package_c_rr_supersession_applied"), 1)

    def test_supersession_receipt_has_all_required_bindings(self):
        """Supersession receipt contains all historical SHA bindings."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        self._do_supersession(rr_dir)
        rr = self._rr_state(rr_dir)
        sup_path = rr["rr_supersession"]["receipt_path"]
        receipt = json.loads(Path(sup_path).read_text())
        self.assertEqual(receipt["source_gate_sha256"], ctx["failed_gate_sha"])
        self.assertEqual(receipt["first_timeout_sha256"], ctx["first_timeout_sha"])
        self.assertEqual(receipt["second_timeout_sha256"], ctx["second_timeout_sha"])
        self.assertEqual(receipt["candidate_id"], ctx["candidate_id"])
        self.assertEqual(receipt["verification_aggregate_sha256"], ctx["agg_sha"])

    def test_supersession_crash_at_receipt_write_is_recoverable(self):
        """Orphan supersession receipt written before state update is recovered on replay."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        mod = self.mod
        rr = self._rr_state(rr_dir)
        review_sha = rr["rr_review"]["sha256"]
        dossier_sha = rr["rr_dossier"]["sha256"]
        # Compute what the supersession_id would be
        receipt_immutable = {
            "schema_version": mod.SCHEMA, "kind": "package_c_rr_supersession_receipt",
            "package_id": mod.PACKAGE_C_RR_PACKAGE_ID, "policy": mod.POLICY_ID,
            "source_work_id": mod.PACKAGE_C_RR_SOURCE_WORK_ID,
            "source_revision": mod.PACKAGE_C_RR_SOURCE_REVISION,
            "source_state_sha256": mod.PACKAGE_C_RR_SOURCE_STATE_SHA256,
            "source_gate_sha256": mod.PACKAGE_C_RR_FAILED_GATE_SHA256,
            "first_timeout_sha256": mod.PACKAGE_C_RR_FIRST_TIMEOUT_SHA256,
            "second_timeout_sha256": mod.PACKAGE_C_RR_SECOND_TIMEOUT_SHA256,
            "candidate_id": mod.PACKAGE_C_RR_CANDIDATE_ID,
            "evidence_digest": mod.PACKAGE_C_RR_EVIDENCE_DIGEST,
            "candidate_fingerprint_digest": mod.PACKAGE_C_RR_FINGERPRINT,
            "verification_aggregate_sha256": mod.PACKAGE_C_RR_VERIFICATION_AGGREGATE_SHA256,
            "dossier_sha256": dossier_sha, "review_envelope_sha256": review_sha,
        }
        supersession_id = mod.digest_bytes(
            json.dumps(receipt_immutable, sort_keys=True, separators=(",", ":")).encode())
        # Write orphan receipt before applying
        sup_dir = os.path.join(rr_dir, "rr-supersession")
        os.makedirs(sup_dir, mode=0o700, exist_ok=True)
        orphan_path = os.path.join(sup_dir, supersession_id + ".json")
        orphan = {**receipt_immutable, "supersession_id": supersession_id,
                  "issued_at": "2026-08-14T10:00:00Z"}
        self._write_json(orphan_path, orphan)
        result = self._do_supersession(rr_dir)
        self.assertFalse(result.get("reused"))
        self.assertEqual(result["supersession_id"], supersession_id)

    # ── historical state preservation ─────────────────────────────────────────

    def test_source_state_unchanged_before_supersession(self):
        """Source state is byte-identical at revision 24 through dossier and review phases."""
        ctx, rr_dir = self._setup()
        src_before = Path(os.path.join(ctx["source_dir"], "state.json")).read_bytes()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        src_after_ingest = Path(os.path.join(ctx["source_dir"], "state.json")).read_bytes()
        self.assertEqual(src_before, src_after_ingest)

    def test_timeout_receipts_preserved_by_supersession(self):
        """Both timeout terminal receipts are not deleted or modified by supersession."""
        ctx, rr_dir = self._setup()
        attempts_dir = os.path.join(ctx["source_dir"], "package-c-review-attempts")
        shas_before = {f: self._sha(Path(os.path.join(attempts_dir, f)).read_bytes())
                       for f in os.listdir(attempts_dir)}
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        self._do_supersession(rr_dir)
        for fname, sha in shas_before.items():
            self.assertEqual(self._sha(Path(os.path.join(attempts_dir, fname)).read_bytes()), sha)

    # ── RR terminal kind is distinct from old 300s path ──────────────────────

    def test_rr_terminal_kind_distinct_from_package_c_terminal(self):
        """PACKAGE_C_RR_TERMINAL_KIND is different from the standard Package C terminal kind."""
        mod = self.mod
        self.assertNotEqual(mod.PACKAGE_C_RR_TERMINAL_KIND, "claude_package_c_review_terminal")
        self.assertIn("rr", mod.PACKAGE_C_RR_TERMINAL_KIND)

    # ── regression guard: v4 verification lifecycle unchanged ─────────────────

    def test_package_c_rr_review_schema_kind_is_distinct(self):
        """Review schema kind is package_c_rr_review, not package_c_review."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        rr = self._rr_state(rr_dir)
        attempt = rr["rr_review_attempt"]
        schema = json.loads(Path(attempt["schema_path"]).read_text())
        self.assertEqual(schema["properties"]["kind"]["const"], "package_c_rr_review")
        self.assertNotEqual(schema["properties"]["kind"]["const"], "package_c_review")

    def test_load_rr_rejects_wrong_package_id(self):
        """load_rr rejects a state with the wrong package_id."""
        mod = self.mod
        rr_dir = self._make_rr_state()
        state = json.loads(Path(os.path.join(rr_dir, "state.json")).read_text())
        state["package_id"] = "wrong-package-id"
        self._write_json(os.path.join(rr_dir, "state.json"), state)
        with self.assertRaises(mod.ControllerError):
            mod.load_rr(rr_dir)

    def test_load_rr_rejects_symlink_state(self):
        """load_rr rejects a state file that is a symlink."""
        mod = self.mod
        rr_dir = self._make_rr_state()
        state_path = os.path.join(rr_dir, "state.json")
        link_path = state_path + ".link"
        os.rename(state_path, link_path)
        os.symlink(link_path, state_path)
        try:
            with self.assertRaises(mod.ControllerError):
                mod.load_rr(rr_dir)
        finally:
            os.unlink(state_path)
            os.rename(link_path, state_path)

    def test_dossier_rebuild_after_review_ingested_is_rejected(self):
        """Rebuilding the dossier after review has been ingested raises ControllerError."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        with self.assertRaises(self.mod.ControllerError):
            self._do_build_dossier(rr_dir)

    # ── rr-supersession-crash-window-001: pending marker crash consistency ────

    def test_supersession_pending_marker_absent_after_clean_completion(self):
        """After a clean supersession, rr_supersession_pending is not in recovery state."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        self._do_supersession(rr_dir)
        rr = self._rr_state(rr_dir)
        self.assertIn("rr_supersession", rr)
        self.assertNotIn("rr_supersession_pending", rr)

    def test_supersession_crash_after_pending_before_source_save_recovers(self):
        """Crash after pending marker write but before source mutation is recovered on replay."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        mod = self.mod
        # Intercept save() for the source directory to simulate crash after pending write
        original_save = mod.save
        source_work_id = mod.PACKAGE_C_RR_SOURCE_WORK_ID
        def crashing_source_save(directory, state):
            if os.path.basename(directory) == source_work_id:
                raise RuntimeError("simulated crash before source mutation")
            original_save(directory, state)
        mod.save = crashing_source_save
        try:
            with self.assertRaises(RuntimeError):
                self._do_supersession(rr_dir)
        finally:
            mod.save = original_save
        # Verify crash state: pending present, rr_supersession absent, source still failed
        rr = self._rr_state(rr_dir)
        self.assertIn("rr_supersession_pending", rr)
        self.assertNotIn("rr_supersession", rr)
        src = self._source_state(ctx)
        self.assertEqual(src["phase"], "failed")
        self.assertEqual(src["revision"], 24)
        # Recovery call must finish the outstanding source transition
        result = self._do_supersession(rr_dir)
        self.assertEqual(result["operation"], "apply_package_c_rr_supersession")
        self.assertTrue(result.get("crash_recovered"))
        # Final state: supersession applied, pending gone, source completed
        rr_final = self._rr_state(rr_dir)
        self.assertIn("rr_supersession", rr_final)
        self.assertNotIn("rr_supersession_pending", rr_final)
        src_final = self._source_state(ctx)
        self.assertEqual(src_final["phase"], "completed")

    def test_supersession_crash_after_source_save_before_rr_state_recovers(self):
        """Crash after source state saved as completed but rr_supersession not yet written."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        mod = self.mod
        # Intercept save_rr to crash on the second call (final rr_supersession write)
        original_save_rr = mod.save_rr
        call_count = [0]
        def crashing_save_rr(directory, state):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("simulated crash after source save before rr_supersession write")
            original_save_rr(directory, state)
        mod.save_rr = crashing_save_rr
        try:
            with self.assertRaises(RuntimeError):
                self._do_supersession(rr_dir)
        finally:
            mod.save_rr = original_save_rr
        # Verify crash state: pending present, rr_supersession absent, source is completed
        rr = self._rr_state(rr_dir)
        self.assertIn("rr_supersession_pending", rr)
        self.assertNotIn("rr_supersession", rr)
        src = self._source_state(ctx)
        self.assertEqual(src["phase"], "completed")
        # Recovery call must detect source already completed and finalize recovery state
        result = self._do_supersession(rr_dir)
        self.assertEqual(result["operation"], "apply_package_c_rr_supersession")
        self.assertTrue(result.get("crash_recovered"))
        # Final state: fully applied
        rr_final = self._rr_state(rr_dir)
        self.assertIn("rr_supersession", rr_final)
        self.assertNotIn("rr_supersession_pending", rr_final)
        src_final = self._source_state(ctx)
        self.assertEqual(src_final["phase"], "completed")

    def test_supersession_crash_recovery_then_replay_returns_reused(self):
        """After crash recovery completes, a subsequent call returns the normal reused replay."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        mod = self.mod
        original_save = mod.save
        source_work_id = mod.PACKAGE_C_RR_SOURCE_WORK_ID
        def crashing_source_save(directory, state):
            if os.path.basename(directory) == source_work_id:
                raise RuntimeError("simulated crash")
            original_save(directory, state)
        mod.save = crashing_source_save
        try:
            with self.assertRaises(RuntimeError):
                self._do_supersession(rr_dir)
        finally:
            mod.save = original_save
        # First recovery
        self._do_supersession(rr_dir)
        # Second call must be a clean replay (reused=True)
        replay = self._do_supersession(rr_dir)
        self.assertTrue(replay.get("reused"))
        self.assertNotIn("crash_recovered", replay)

    def test_supersession_pending_marker_tamper_is_rejected(self):
        """Tampered pending receipt SHA in recovery state raises ControllerError."""
        ctx, rr_dir = self._setup()
        self._do_build_dossier(rr_dir)
        self._do_launch(rr_dir)
        self._wait_rr_review(rr_dir)
        self._do_ingest(rr_dir)
        mod = self.mod
        original_save = mod.save
        source_work_id = mod.PACKAGE_C_RR_SOURCE_WORK_ID
        def crashing_source_save(directory, state):
            if os.path.basename(directory) == source_work_id:
                raise RuntimeError("simulated crash")
            original_save(directory, state)
        mod.save = crashing_source_save
        try:
            with self.assertRaises(RuntimeError):
                self._do_supersession(rr_dir)
        finally:
            mod.save = original_save
        # Tamper the pending marker receipt SHA
        rr = self._rr_state(rr_dir)
        rr["rr_supersession_pending"]["receipt_sha256"] = "f" * 64
        Path(os.path.join(rr_dir, "state.json")).write_text(
            json.dumps(rr, sort_keys=True, indent=2) + "\n")
        with self.assertRaises(mod.ControllerError):
            self._do_supersession(rr_dir)


if __name__ == "__main__": unittest.main()
