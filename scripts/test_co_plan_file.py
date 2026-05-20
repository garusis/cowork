#!/usr/bin/env python3
"""Tests for the co-plan helper script.

Exercises the CLI via subprocess to match how agents invoke it. Covers:
- init creates chat + sidecar
- ledger commands (add, answer, list)
- propose-consensus with plan-file hashing
- sign-off ordering, consensus gate, and plan-file integrity invariants
- legacy mode (no sidecar)
- concurrent add-question safety
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parent / "co_plan_file.py"


def run(
    *args: str,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the helper. Returns the completed process; doesn't raise by default."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def write_body(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


class CoPlanCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.chat = self.tmp / "chat.md"
        self.sidecar = self.tmp / "chat.md.state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def init_fresh(self, goal: str = "test goal") -> None:
        result = run("init", "--file", str(self.chat), "--goal", goal)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["created"])
        self.assertTrue(self.chat.exists())
        self.assertTrue(self.sidecar.exists())

    def inspect(self) -> dict:
        result = run("inspect", "--file", str(self.chat))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def status(self) -> dict:
        result = run("status", "--file", str(self.chat))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def post_turn(self, role: str, body: str) -> subprocess.CompletedProcess[str]:
        body_path = write_body(self.tmp / f"body-{role}-{id(body)}.md", body)
        return run("post-turn", "--file", str(self.chat), "--self", role, "--body-file", str(body_path))

    def turn(self, role: str, timeout: str = "0") -> subprocess.CompletedProcess[str]:
        return run(
            "turn",
            "--file",
            str(self.chat),
            "--self",
            role,
            "--timeout",
            timeout,
            "--poll-interval",
            "0.1",
        )

    def post(self, role: str, body: str, timeout: str = "0") -> subprocess.CompletedProcess[str]:
        body_path = write_body(self.tmp / f"high-post-{role}-{id(body)}.md", body)
        return run(
            "post",
            "--file",
            str(self.chat),
            "--self",
            role,
            "--body-file",
            str(body_path),
            "--timeout",
            timeout,
            "--poll-interval",
            "0.1",
        )

    def question(self, role: str, question: str) -> subprocess.CompletedProcess[str]:
        return run("question", "--file", str(self.chat), "--self", role, "--question", question)

    def resolve(self, decision: str) -> subprocess.CompletedProcess[str]:
        return run("resolve", "--file", str(self.chat), "--decision", decision)

    def post_signoff_recap(self, body: str = "Ready for sign-off. Ledger recap complete.") -> subprocess.CompletedProcess[str]:
        body_path = write_body(self.tmp / f"signoff-recap-{id(body)}.md", body)
        return run("post-signoff-recap", "--file", str(self.chat), "--body-file", str(body_path))

    def write_consensus(self) -> subprocess.CompletedProcess[str]:
        return run("write-consensus", "--file", str(self.chat))

    def valid_plan_body(self, detail: str = "Final plan body.") -> str:
        return (
            "# Test Plan\n\n"
            "## Summary\n\n"
            f"{detail}\n\n"
            "## Goal Coverage\n\n"
            "The goal is covered by the planned work.\n\n"
            "## Decisions\n\n"
            "All ledger decisions are answered or not applicable.\n\n"
            "## Approach\n\n"
            "Implement the current agreed approach.\n\n"
            "## Implementation Changes\n\n"
            "Make the required implementation changes.\n\n"
            "## Tests\n\n"
            "Run focused unit and integration checks.\n\n"
            "## Risks and Verification\n\n"
            "Residual risks are accepted.\n\n"
            "## Assumptions\n\n"
            "No extra assumptions.\n"
        )

    def write_plan(self, body: str | None = None) -> Path:
        plan_path = self.tmp / "chat.plan.md"
        if body is None:
            body = self.valid_plan_body()
        plan_path.write_text(body, encoding="utf-8")
        return plan_path

    def isolated_deps_env(self) -> dict[str, str]:
        env = os.environ.copy()
        bin_dir = self.tmp / "empty-bin"
        home_dir = self.tmp / "home"
        bin_dir.mkdir(exist_ok=True)
        home_dir.mkdir(exist_ok=True)
        env["PATH"] = str(bin_dir)
        env["HOME"] = str(home_dir)
        env.pop("COPLAN_CAVEMAN_PATHS", None)
        return env

    def make_executable(self, name: str) -> Path:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def receipt_body(self, note: str = "looks good") -> str:
        return (
            f"{note}\n\n"
            "## Plan Review Receipt\n\n"
            f"- Plan file: chat.plan.md\n"
            f"- Reviewed end-to-end: yes\n"
            "- Status by section:\n"
            "  - Summary: confirmed\n"
            "  - Goal Coverage: confirmed\n"
            "  - Decisions: confirmed\n"
            "  - Approach: confirmed\n"
            "  - Implementation Changes: confirmed\n"
            "  - Tests: confirmed\n"
            "  - Risks and Verification: confirmed\n"
            "  - Assumptions: confirmed\n\n"
            "--- proposing consensus ---\n"
        )

    def propose_plan(
        self,
        role: str,
        plan_path: Path,
        body: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            "propose-consensus",
            "--file",
            str(self.chat),
            "--self",
            role,
            "--plan-file",
            str(plan_path),
        ]
        if body is not None:
            body_path = write_body(self.tmp / f"proposal-{role}-{id(body)}.md", body)
            cmd.extend(["--body-file", str(body_path)])
        return run(*cmd)

    def propose_with_receipt(self, role: str, plan_path: Path, note: str = "looks good") -> subprocess.CompletedProcess[str]:
        return self.propose_plan(role, plan_path, self.receipt_body(note))

    def fully_consent(self, plan_body: str | None = None) -> Path:
        """Run the full propose-consensus dance for both roles against a plan file.

        Posts a chat message containing a `## Plan Review Receipt` for each role
        before running propose-consensus, per the new receipt-enforcement rule.
        """
        if plan_body is None:
            plan_body = self.valid_plan_body()
        plan_path = self.write_plan(plan_body)
        planner = self.propose_with_receipt("planner", plan_path, "planner ready")
        self.assertEqual(planner.returncode, 0, planner.stderr)
        advisor = self.propose_with_receipt("advisor", plan_path, "advisor concurs")
        self.assertEqual(advisor.returncode, 0, advisor.stderr)
        return plan_path

    def test_init_creates_chat_and_sidecar(self) -> None:
        self.init_fresh()
        sidecar = json.loads(self.sidecar.read_text(encoding="utf-8"))
        self.assertEqual(sidecar["version"], 2)
        self.assertEqual(sidecar["questions"], [])
        self.assertIsNone(sidecar["signoff"])
        self.assertIsNone(sidecar["proposed_plan"])
        self.assertEqual(sidecar["activity"]["planner"]["state"], "idle")
        self.assertEqual(sidecar["activity"]["advisor"]["state"], "idle")
        self.assertEqual(sidecar["derived"]["open_question_count"], 0)
        self.assertFalse(sidecar["derived"]["proposed_plan_hashes_match"])
        self.assertIsNone(sidecar["derived"]["proposed_plan_path"])

    def test_status_reports_activity_and_next_actions(self) -> None:
        self.init_fresh()
        snapshot = self.status()

        self.assertEqual(snapshot["activity"]["planner"]["state"], "idle")
        self.assertEqual(snapshot["activity"]["advisor"]["state"], "idle")
        self.assertEqual(snapshot["next_actions"]["planner"]["action"], "post")
        self.assertEqual(snapshot["next_actions"]["advisor"]["action"], "wait")

    def test_init_accepts_body_file(self) -> None:
        body = "Multi-paragraph goal.\n\nWith `backticks`, em-dashes — and \"quotes\".\n"
        body_path = write_body(self.tmp / "goal.md", body)
        result = run("init", "--file", str(self.chat), "--body-file", str(body_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["created"])
        chat_text = self.chat.read_text(encoding="utf-8")
        self.assertIn("Multi-paragraph goal.", chat_text)
        self.assertIn("With `backticks`, em-dashes — and \"quotes\".", chat_text)

    def test_init_body_file_normalizes_crlf(self) -> None:
        body_path = self.tmp / "goal.md"
        body_path.write_bytes(b"line one\r\n\r\nline two\r\n")
        result = run("init", "--file", str(self.chat), "--body-file", str(body_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        chat_text = self.chat.read_text(encoding="utf-8")
        self.assertNotIn("\r", chat_text)
        self.assertIn("line one\n\nline two", chat_text)

    def test_init_requires_goal_or_body_file(self) -> None:
        result = run("init", "--file", str(self.chat))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.chat.exists())

    def test_init_rejects_both_goal_and_body_file(self) -> None:
        body_path = write_body(self.tmp / "goal.md", "from file")
        result = run(
            "init",
            "--file",
            str(self.chat),
            "--goal",
            "from string",
            "--body-file",
            str(body_path),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.chat.exists())

    def test_init_empty_body_file_is_rejected(self) -> None:
        body_path = write_body(self.tmp / "goal.md", "   \n\n")
        result = run("init", "--file", str(self.chat), "--body-file", str(body_path))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.chat.exists())

    def test_deps_status_reports_absent_optional_tools(self) -> None:
        result = run("deps-status", env=self.isolated_deps_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertFalse(payload["rtk"]["available"])
        self.assertIsNone(payload["rtk"]["path"])
        self.assertFalse(payload["caveman"]["available"])
        self.assertEqual(payload["caveman"]["commands"], [])
        self.assertEqual(payload["caveman"]["paths"], [])
        self.assertFalse(payload["policy"]["auto_install"])
        self.assertFalse(payload["policy"]["missing_tools_block"])
        self.assertTrue(payload["policy"]["required_when_available"])

    def test_deps_status_detects_rtk_on_path(self) -> None:
        env = self.isolated_deps_env()
        rtk = self.make_executable("rtk")
        env["PATH"] = str(rtk.parent)

        result = run("deps-status", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertTrue(payload["rtk"]["available"])
        self.assertEqual(payload["rtk"]["path"], str(rtk))
        self.assertFalse(payload["caveman"]["available"])

    def test_deps_status_detects_caveman_skill_path(self) -> None:
        env = self.isolated_deps_env()
        skill = Path(env["HOME"]) / ".agents" / "skills" / "caveman" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: caveman\n---\n", encoding="utf-8")

        result = run("deps-status", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertFalse(payload["rtk"]["available"])
        self.assertTrue(payload["caveman"]["available"])
        self.assertEqual(payload["caveman"]["paths"], [str(skill)])

    def test_deps_status_detects_caveman_from_explicit_paths(self) -> None:
        env = self.isolated_deps_env()
        skill = self.tmp / "custom-caveman" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: caveman\n---\n", encoding="utf-8")
        env["COPLAN_CAVEMAN_PATHS"] = str(skill)

        result = run("deps-status", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertTrue(payload["caveman"]["available"])
        self.assertEqual(payload["caveman"]["paths"], [str(skill)])

    def test_turn_payload_includes_optional_dependency_status(self) -> None:
        self.init_fresh()
        result = run(
            "turn",
            "--file",
            str(self.chat),
            "--self",
            "planner",
            "--timeout",
            "0",
            "--poll-interval",
            "0.1",
            env=self.isolated_deps_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertEqual(payload["action"], "compose_initial_plan")
        self.assertIn("optional_dependencies", payload)
        self.assertFalse(payload["optional_dependencies"]["rtk"]["available"])
        self.assertFalse(payload["optional_dependencies"]["caveman"]["available"])

    def test_add_question_assigns_sequential_ids(self) -> None:
        self.init_fresh()
        first = run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "first?")
        second = run("add-question", "--file", str(self.chat), "--role", "advisor", "--question", "second?")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first.stdout)["id"], "Q1")
        self.assertEqual(json.loads(second.stdout)["id"], "Q2")

        listing = run("list-questions", "--file", str(self.chat))
        self.assertEqual(listing.returncode, 0)
        questions = json.loads(listing.stdout)["questions"]
        self.assertEqual([q["id"] for q in questions], ["Q1", "Q2"])
        self.assertEqual({q["state"] for q in questions}, {"open"})

    def test_answer_question_updates_ledger_and_chat(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "yes or no?")
        answer = run("answer-question", "--file", str(self.chat), "--id", "Q1", "--answer", "yes, do X")
        self.assertEqual(answer.returncode, 0, answer.stderr)
        payload = json.loads(answer.stdout)
        self.assertTrue(payload["answered"])
        self.assertFalse(payload["previously_answered"])

        snapshot = self.inspect()
        self.assertEqual(snapshot["open_question_count"], 0)
        self.assertEqual(snapshot["answered_question_count"], 1)
        self.assertEqual(snapshot["last_role"], "marcos")
        self.assertIn("**Q1**", snapshot["last_body"])
        self.assertIn("yes, do X", snapshot["last_body"])

    def test_answer_question_overwrites_latest_answer(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "?")
        run("answer-question", "--file", str(self.chat), "--id", "Q1", "--answer", "first")
        second = run("answer-question", "--file", str(self.chat), "--id", "Q1", "--answer", "second")
        self.assertEqual(second.returncode, 0)
        self.assertTrue(json.loads(second.stdout)["previously_answered"])

        sidecar = json.loads(self.sidecar.read_text(encoding="utf-8"))
        self.assertEqual(sidecar["questions"][0]["answer"], "second")
        # Both marcos blocks appear in the chat history.
        chat_text = self.chat.read_text(encoding="utf-8")
        self.assertEqual(chat_text.count("**Q1**"), 2)

    def test_resolve_appends_marcos_decision_and_clears_proposals(self) -> None:
        self.init_fresh()
        self.fully_consent()

        result = self.resolve("Reject sign-off until the retry scope is narrowed.")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["resolved"])
        self.assertTrue(payload["cleared_proposals"])

        snapshot = self.inspect()
        self.assertEqual(snapshot["last_role"], "marcos")
        self.assertIn("Reject sign-off", snapshot["last_body"])
        self.assertIsNone(snapshot["proposed_plan_path"])
        self.assertFalse(snapshot["proposed_plan_hashes_match"])

    def test_resolve_missing_chat_is_a_clear_error(self) -> None:
        result = run("resolve", "--file", str(self.tmp / "missing-chat.md"), "--decision", "go")
        self.assertEqual(result.returncode, 1)
        self.assertIn("chat file does not exist", result.stderr)

    def test_next_action_routes_open_questions_to_planner_escalation(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "advisor", "--question", "choose?")

        planner = run("next-action", "--file", str(self.chat), "--self", "planner")
        self.assertEqual(planner.returncode, 0, planner.stderr)
        planner_payload = json.loads(planner.stdout)
        self.assertEqual(planner_payload["action"], "escalate_open_questions")
        self.assertEqual(planner_payload["open_question_ids"], ["Q1"])
        self.assertFalse(planner_payload["should_poll"])

        advisor = run("next-action", "--file", str(self.chat), "--self", "advisor")
        self.assertEqual(advisor.returncode, 0, advisor.stderr)
        advisor_payload = json.loads(advisor.stdout)
        self.assertEqual(advisor_payload["action"], "wait")
        self.assertEqual(advisor_payload["reason"], "open_questions_wait_for_planner")
        self.assertTrue(advisor_payload["should_poll"])

    def test_poll_for_other_refuses_planner_with_open_questions(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "open?")
        result = run(
            "poll-for-other",
            "--file",
            str(self.chat),
            "--self",
            "planner",
            "--timeout",
            "0",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("next action for planner is escalate_open_questions", result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "escalate_open_questions")

    def test_poll_for_other_allows_advisor_waiting_with_open_questions(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "open?")
        result = run(
            "poll-for-other",
            "--file",
            str(self.chat),
            "--self",
            "advisor",
            "--timeout",
            "0",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('"timeout": true', result.stdout)
        self.assertEqual(result.stderr, "")

    def test_poll_for_other_refuses_when_role_should_post(self) -> None:
        self.init_fresh()
        result = run(
            "poll-for-other",
            "--file",
            str(self.chat),
            "--self",
            "planner",
            "--timeout",
            "0",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("next action for planner is post", result.stderr)

    def test_post_turn_allows_role_when_next_action_is_post(self) -> None:
        self.init_fresh()
        result = self.post_turn("planner", "Planner first turn.")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["posted"])
        self.assertEqual(payload["next_action"]["action"], "wait")
        self.assertEqual(self.inspect()["last_role"], "planner")

    def test_post_turn_refuses_when_role_should_wait(self) -> None:
        self.init_fresh()
        result = self.post_turn("advisor", "Advisor should wait.")
        self.assertEqual(result.returncode, 3)
        self.assertIn("next action for advisor is wait", result.stderr)

    def test_post_turn_refuses_proposal_marker(self) -> None:
        self.init_fresh()
        result = self.post_turn("planner", "Looks ready.\n\n--- proposing consensus ---")
        self.assertEqual(result.returncode, 3)
        self.assertIn("use propose-consensus", result.stderr)

    def test_turn_maps_goal_to_initial_plan_action(self) -> None:
        self.init_fresh()
        result = self.turn("planner")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["action"], "compose_initial_plan")
        self.assertTrue(payload["must_create_plan_file"])
        self.assertEqual(payload["plan_file"], str(self.tmp / "chat.plan.md"))
        self.assertEqual(self.status()["activity"]["planner"]["state"], "composing")

    def test_post_refuses_initial_planner_turn_without_plan_file(self) -> None:
        self.init_fresh()
        result = self.post("planner", "Initial plan summary.")
        self.assertEqual(result.returncode, 3)
        self.assertIn("must create plan file first", result.stderr)

    def test_post_initial_turn_appends_then_waits(self) -> None:
        self.init_fresh()
        self.write_plan()
        result = self.post("planner", "Initial plan created.")
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["posted"])
        self.assertEqual(payload["kind"], "turn")
        self.assertEqual(payload["turn"]["action"], "timeout")
        self.assertEqual(self.inspect()["last_role"], "planner")
        activity = self.status()["activity"]["planner"]
        self.assertEqual(activity["state"], "timed_out")
        self.assertEqual(activity["waiting_for"], "advisor")

    def test_question_alias_adds_open_question(self) -> None:
        self.init_fresh()
        result = self.question("advisor", "Should rollout be staged?")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["id"], "Q1")
        self.assertEqual(payload["raised_by"], "advisor")

    def test_post_initial_escalation_requires_plan_file(self) -> None:
        self.init_fresh()
        question = self.question("planner", "Should rollout be staged?")
        self.assertEqual(question.returncode, 0, question.stderr)
        result = self.post("planner", "Q1 blocks the plan until answered.")
        self.assertEqual(result.returncode, 3)
        self.assertIn("must create plan file first", result.stderr)

    def test_post_infers_proposal_and_returns_signoff_recap_action(self) -> None:
        self.init_fresh()
        plan = self.write_plan(self.valid_plan_body("planner proposes through post"))
        self.assertEqual(self.post_turn("planner", "Draft is ready for advisor.").returncode, 0)
        advisor = self.propose_with_receipt("advisor", plan, "advisor ready")
        self.assertEqual(advisor.returncode, 0, advisor.stderr)

        result = self.post("planner", self.receipt_body("planner concurs"))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kind"], "proposal")
        self.assertTrue(payload["hashes_match"])
        self.assertEqual(payload["turn"]["action"], "compose_signoff_recap")

    def test_turn_writes_consensus_after_signoff(self) -> None:
        self.init_fresh()
        self.fully_consent()
        self.assertEqual(self.post_signoff_recap().returncode, 0)
        signoff = run("signoff", "--file", str(self.chat))
        self.assertEqual(signoff.returncode, 0, signoff.stderr)

        result = self.turn("planner")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["action"], "consensus_written")
        self.assertTrue(self.inspect()["consensus_exists"])

    def test_escalate_open_questions_appends_planner_escalation(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "first?")
        run("add-question", "--file", str(self.chat), "--role", "advisor", "--question", "second?")
        body = write_body(
            self.tmp / "escalation.md",
            "Need answers before planning continues.\n\nQ1: first decision.\nQ2: second decision.",
        )
        result = run(
            "escalate-open-questions",
            "--file",
            str(self.chat),
            "--body-file",
            str(body),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["open_question_ids"], ["Q1", "Q2"])

        snapshot = self.inspect()
        self.assertEqual(snapshot["last_role"], "planner")
        self.assertIn("Q1", snapshot["last_body"])
        self.assertIn("Q2", snapshot["last_body"])
        self.assertIn("--- escalating to marcos ---", snapshot["last_body"])

    def test_escalate_open_questions_rejects_missing_qid(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "first?")
        run("add-question", "--file", str(self.chat), "--role", "advisor", "--question", "second?")
        body = write_body(self.tmp / "bad-escalation.md", "Q1 only")
        result = run(
            "escalate-open-questions",
            "--file",
            str(self.chat),
            "--body-file",
            str(body),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing: Q2", result.stderr)

    def test_escalate_open_questions_rejects_when_none_open(self) -> None:
        self.init_fresh()
        body = write_body(self.tmp / "no-open-escalation.md", "No questions.")
        result = run(
            "escalate-open-questions",
            "--file",
            str(self.chat),
            "--body-file",
            str(body),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("no open questions", result.stderr)

    def test_signoff_refused_with_open_questions(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "open?")
        result = run("signoff", "--file", str(self.chat))
        self.assertEqual(result.returncode, 3)
        self.assertIn("Q1", result.stderr)

    def test_signoff_refused_without_both_proposals(self) -> None:
        self.init_fresh()
        # No open questions, but no proposals yet.
        result = run("signoff", "--file", str(self.chat))
        self.assertEqual(result.returncode, 3)
        self.assertIn("proposing consensus", result.stderr)

    def test_signoff_succeeds_when_ready(self) -> None:
        self.init_fresh()
        self.fully_consent()
        result = run("signoff", "--file", str(self.chat))
        self.assertEqual(result.returncode, 0, result.stderr)

        snapshot = self.inspect()
        self.assertTrue(snapshot["signoff_present"])
        self.assertEqual(snapshot["last_role"], "marcos-signoff")
        self.assertTrue(snapshot["plan_file_intact"])

    def test_signoff_refused_with_only_one_role_proposing(self) -> None:
        self.init_fresh()
        plan = self.write_plan()
        self.assertEqual(self.propose_with_receipt("planner", plan, "planner ready").returncode, 0)
        result = run("signoff", "--file", str(self.chat))
        self.assertEqual(result.returncode, 3)
        self.assertIn("both planner and advisor must post", result.stderr)

    def test_signoff_refused_with_mismatched_plan_hashes(self) -> None:
        self.init_fresh()
        plan = self.write_plan(self.valid_plan_body("v1"))
        self.assertEqual(self.propose_with_receipt("planner", plan, "v1 planner").returncode, 0)
        plan.write_text(self.valid_plan_body("v2"), encoding="utf-8")
        self.assertEqual(self.propose_with_receipt("advisor", plan, "v2 advisor").returncode, 0)
        result = run("signoff", "--file", str(self.chat))
        self.assertEqual(result.returncode, 3)
        self.assertIn("different plan file contents", result.stderr)

    def test_signoff_refused_when_plan_file_modified_after_consent(self) -> None:
        self.init_fresh()
        plan = self.fully_consent(self.valid_plan_body("v1"))
        plan.write_text(self.valid_plan_body("v1 modified"), encoding="utf-8")
        result = run("signoff", "--file", str(self.chat))
        self.assertEqual(result.returncode, 3)
        self.assertIn("plan file has changed", result.stderr)

    def test_propose_consensus_new_path_clears_other_role_hash(self) -> None:
        self.init_fresh()
        plan_a = self.tmp / "plan-a.md"
        plan_a.write_text(self.valid_plan_body("plan A"), encoding="utf-8")
        plan_b = self.tmp / "plan-b.md"
        plan_b.write_text(self.valid_plan_body("plan B"), encoding="utf-8")

        self.assertEqual(self.propose_with_receipt("planner", plan_a, "planner against A").returncode, 0)
        result = self.propose_with_receipt("advisor", plan_b, "advisor against B")
        self.assertEqual(result.returncode, 0, result.stderr)

        sidecar = json.loads(self.sidecar.read_text(encoding="utf-8"))
        self.assertEqual(sidecar["proposed_plan"]["path"], str(plan_b.resolve()))
        self.assertIsNone(sidecar["proposed_plan"]["hashes"]["planner"])
        self.assertIsNotNone(sidecar["proposed_plan"]["hashes"]["advisor"])

    def test_propose_consensus_returns_hashes_match_when_aligned(self) -> None:
        self.init_fresh()
        plan = self.write_plan(self.valid_plan_body("agreed plan"))
        first = self.propose_with_receipt("planner", plan, "planner ready")
        self.assertEqual(first.returncode, 0)
        first_payload = json.loads(first.stdout)
        self.assertFalse(first_payload["hashes_match"])
        self.assertFalse(first_payload["both_proposed"])

        second = self.propose_with_receipt("advisor", plan, "advisor concurs")
        second_payload = json.loads(second.stdout)
        self.assertTrue(second_payload["hashes_match"])
        self.assertTrue(second_payload["both_proposed"])

    def test_next_action_allows_signoff_recap_when_planner_proposes_last(self) -> None:
        self.init_fresh()
        plan = self.write_plan(self.valid_plan_body("planner proposes last"))

        first_turn = self.post_turn("planner", "Draft is ready for advisor review.")
        self.assertEqual(first_turn.returncode, 0, first_turn.stderr)
        advisor = self.propose_with_receipt("advisor", plan, "advisor ready")
        self.assertEqual(advisor.returncode, 0, advisor.stderr)
        planner = self.propose_with_receipt("planner", plan, "planner concurs")
        self.assertEqual(planner.returncode, 0, planner.stderr)

        snapshot = self.inspect()
        self.assertEqual(snapshot["last_role"], "planner")
        self.assertTrue(snapshot["proposed_plan_hashes_match"])
        self.assertEqual(snapshot["open_question_count"], 0)

        planner_action = run("next-action", "--file", str(self.chat), "--self", "planner")
        self.assertEqual(planner_action.returncode, 0, planner_action.stderr)
        planner_payload = json.loads(planner_action.stdout)
        self.assertEqual(planner_payload["action"], "post_signoff_recap")
        self.assertFalse(planner_payload["should_poll"])

        advisor_action = run("next-action", "--file", str(self.chat), "--self", "advisor")
        self.assertEqual(advisor_action.returncode, 0, advisor_action.stderr)
        advisor_payload = json.loads(advisor_action.stdout)
        self.assertEqual(advisor_payload["action"], "wait")
        self.assertTrue(advisor_payload["should_poll"])

        recap = self.post_signoff_recap()
        self.assertEqual(recap.returncode, 0, recap.stderr)

    def test_next_action_rejects_signoff_recap_when_plan_file_changed(self) -> None:
        self.init_fresh()
        plan = self.fully_consent(self.valid_plan_body("v1"))
        plan.write_text(self.valid_plan_body("v2"), encoding="utf-8")

        snapshot = self.inspect()
        self.assertFalse(snapshot["proposed_plan_hashes_match"])
        self.assertFalse(snapshot["plan_file_intact"])
        self.assertNotEqual(
            snapshot["plan_file_current_hash"],
            snapshot["proposed_plan_hashes"]["planner"],
        )

        planner_action = run("next-action", "--file", str(self.chat), "--self", "planner")
        self.assertEqual(planner_action.returncode, 0, planner_action.stderr)
        planner_payload = json.loads(planner_action.stdout)
        self.assertEqual(planner_payload["action"], "post")
        self.assertNotEqual(planner_payload["action"], "post_signoff_recap")

    def test_post_refuses_to_label_regular_turn_as_signoff_recap(self) -> None:
        self.init_fresh()
        self.fully_consent()

        result = self.post("planner", "I reviewed the critique and updated the plan.")
        self.assertEqual(result.returncode, 3)
        self.assertIn("refusing to post sign-off recap", result.stderr)
        self.assertNotIn("I reviewed the critique", self.chat.read_text(encoding="utf-8"))

    def test_regular_turn_withdraws_existing_role_proposal(self) -> None:
        self.init_fresh()
        plan = self.write_plan(self.valid_plan_body("advisor proposed v1"))
        self.assertEqual(self.post_turn("planner", "Draft is ready for advisor.").returncode, 0)
        advisor = self.propose_with_receipt("advisor", plan, "advisor ready")
        self.assertEqual(advisor.returncode, 0, advisor.stderr)

        plan.write_text(self.valid_plan_body("planner rewrote v2"), encoding="utf-8")
        planner_turn = self.post("planner", "I rewrote the plan after advisor feedback.")
        self.assertEqual(planner_turn.returncode, 2, planner_turn.stderr)
        self.assertEqual(json.loads(planner_turn.stdout)["kind"], "turn")
        advisor_turn = self.post("advisor", "Blocking critique: the revised plan still misses scope.")
        self.assertEqual(advisor_turn.returncode, 2, advisor_turn.stderr)
        self.assertEqual(json.loads(advisor_turn.stdout)["kind"], "turn")

        sidecar = json.loads(self.sidecar.read_text(encoding="utf-8"))
        self.assertIsNone(sidecar["proposed_plan"])

    def test_propose_consensus_refused_without_plan_review_receipt(self) -> None:
        self.init_fresh()
        plan = self.write_plan()
        result = self.propose_plan("planner", plan, "plan looks good\n\n--- proposing consensus ---")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Plan Review Receipt", result.stderr)

    def test_propose_consensus_refused_when_receipt_missing_a_heading(self) -> None:
        self.init_fresh()
        plan_body = self.valid_plan_body()
        plan = self.write_plan(plan_body)
        # Receipt mentions every required heading except Assumptions.
        body = (
            "## Plan Review Receipt\n\n"
            "- Plan file: chat.plan.md\n"
            "- Reviewed end-to-end: yes\n"
            "- Status by section:\n"
            "  - Summary: confirmed\n"
            "  - Goal Coverage: confirmed\n"
            "  - Decisions: confirmed\n"
            "  - Approach: confirmed\n"
            "  - Implementation Changes: confirmed\n"
            "  - Tests: confirmed\n"
            "  - Risks and Verification: confirmed\n\n"
            "--- proposing consensus ---\n"
        )
        propose = self.propose_plan("planner", plan, body)
        self.assertEqual(propose.returncode, 3)
        self.assertIn("does not enumerate", propose.stderr)
        self.assertIn("assumptions", propose.stderr.lower())

    def test_propose_consensus_accepts_receipt_covering_all_headings(self) -> None:
        self.init_fresh()
        plan_body = self.valid_plan_body() + "\n### Phase 1 - Setup\n\nDetailed setup.\n"
        plan = self.write_plan(plan_body)
        body = (
            "Reviewed the full plan top to bottom.\n\n"
            "## Plan Review Receipt\n\n"
            "- Plan file: chat.plan.md\n"
            "- Reviewed end-to-end: yes\n"
            "- Status by section:\n"
            "  - Summary: confirmed\n"
            "  - Goal Coverage: confirmed\n"
            "  - Decisions: confirmed\n"
            "  - Approach: confirmed\n"
            "  - Implementation Changes: confirmed\n"
            "  - Tests: confirmed\n"
            "  - Risks and Verification: confirmed\n"
            "  - Assumptions: confirmed\n"
            "  - Phase 1 - Setup: confirmed\n\n"
            "--- proposing consensus ---\n"
        )
        propose = self.propose_plan("planner", plan, body)
        self.assertEqual(propose.returncode, 0, propose.stderr)

    def test_propose_consensus_normalizes_receipt_heading_mentions_symmetrically(self) -> None:
        self.init_fresh()
        plan_body = self.valid_plan_body() + "\n### @@USER_MESSAGE@@\n\nTemplate token.\n"
        plan = self.write_plan(plan_body)
        body = (
            "## Plan Review Receipt\n\n"
            "- Status by section:\n"
            "  - Summary: confirmed\n"
            "  - Goal Coverage: confirmed\n"
            "  - Decisions: confirmed\n"
            "  - Approach: confirmed\n"
            "  - Implementation Changes: confirmed\n"
            "  - Tests: confirmed\n"
            "  - Risks and Verification: confirmed\n"
            "  - Assumptions: confirmed\n"
            "  - @@user_message@@: confirmed\n\n"
            "--- proposing consensus ---\n"
        )
        propose = self.propose_plan("planner", plan, body)
        self.assertEqual(propose.returncode, 0, propose.stderr)

    def test_propose_consensus_ignores_headings_inside_code_blocks(self) -> None:
        self.init_fresh()
        plan_body = (
            self.valid_plan_body()
            + "\n"
            "```markdown\n"
            "## Not A Real Heading\n"
            "```\n"
        )
        plan = self.write_plan(plan_body)
        # Receipt mentions required headings but NOT the fenced one.
        body = (
            "## Plan Review Receipt\n\n"
            "- Status by section:\n"
            "  - Summary: confirmed\n"
            "  - Goal Coverage: confirmed\n"
            "  - Decisions: confirmed\n"
            "  - Approach: confirmed\n"
            "  - Implementation Changes: confirmed\n"
            "  - Tests: confirmed\n"
            "  - Risks and Verification: confirmed\n"
            "  - Assumptions: confirmed\n\n"
            "--- proposing consensus ---\n"
        )
        propose = self.propose_plan("planner", plan, body)
        self.assertEqual(propose.returncode, 0, propose.stderr)

    def test_validate_plan_accepts_valid_plan(self) -> None:
        self.init_fresh()
        plan = self.write_plan()
        result = run("validate-plan", "--file", str(self.chat), "--plan-file", str(plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_validate_plan_rejects_missing_required_heading(self) -> None:
        self.init_fresh()
        plan = self.write_plan(
            self.valid_plan_body().replace("\n## Assumptions\n\nNo extra assumptions.\n", "")
        )
        result = run("validate-plan", "--file", str(self.chat), "--plan-file", str(plan))
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing required plan headings", result.stderr)
        self.assertIn("assumptions", result.stderr)

    def test_validate_plan_rejects_stale_history_heading(self) -> None:
        self.init_fresh()
        plan = self.write_plan(self.valid_plan_body() + "\n## Previous Versions\n\nv1 was different.\n")
        result = run("validate-plan", "--file", str(self.chat), "--plan-file", str(plan))
        self.assertEqual(result.returncode, 3)
        self.assertIn("stale history", result.stderr)

    def test_validate_plan_rejects_placeholder_text(self) -> None:
        self.init_fresh()
        plan = self.write_plan(self.valid_plan_body("TODO: finish this plan."))
        result = run("validate-plan", "--file", str(self.chat), "--plan-file", str(plan))
        self.assertEqual(result.returncode, 3)
        self.assertIn("placeholder", result.stderr)

    def test_validate_plan_rejects_open_ledger_question_reference(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "choose?")
        plan = self.write_plan(self.valid_plan_body() + "\nQ1 controls rollout.\n")
        result = run("validate-plan", "--file", str(self.chat), "--plan-file", str(plan))
        self.assertEqual(result.returncode, 3)
        self.assertIn("open ledger questions remain: Q1", result.stderr)

    def test_propose_consensus_refuses_invalid_plan(self) -> None:
        self.init_fresh()
        plan = self.write_plan(
            self.valid_plan_body().replace("\n## Assumptions\n\nNo extra assumptions.\n", "")
        )
        body = (
            "## Plan Review Receipt\n\n"
            "- Status by section:\n"
            "  - Summary: confirmed\n"
            "  - Goal Coverage: confirmed\n"
            "  - Decisions: confirmed\n"
            "  - Approach: confirmed\n"
            "  - Implementation Changes: confirmed\n"
            "  - Tests: confirmed\n"
            "  - Risks and Verification: confirmed\n\n"
            "--- proposing consensus ---\n"
        )
        result = self.propose_plan("planner", plan, body)
        self.assertEqual(result.returncode, 3)
        self.assertIn("plan validation failed", result.stderr)

    def test_propose_consensus_refused_after_signoff(self) -> None:
        self.init_fresh()
        plan = self.fully_consent()
        run("signoff", "--file", str(self.chat))
        result = self.propose_plan("planner", plan)
        self.assertEqual(result.returncode, 3)
        self.assertIn("sign-off already recorded", result.stderr)

    def test_inspect_exposes_plan_file_state(self) -> None:
        self.init_fresh()
        plan = self.fully_consent()
        snapshot = self.inspect()
        self.assertEqual(snapshot["proposed_plan_path"], str(plan.resolve()))
        self.assertTrue(snapshot["proposed_plan_hashes_match"])
        self.assertTrue(snapshot["plan_file_intact"])
        self.assertEqual(
            snapshot["proposed_plan_hashes"]["planner"],
            snapshot["proposed_plan_hashes"]["advisor"],
        )

    def test_consensus_gate_blocks_without_signoff(self) -> None:
        self.init_fresh()
        self.fully_consent()
        self.assertEqual(self.post_signoff_recap().returncode, 0)
        result = self.write_consensus()
        self.assertEqual(result.returncode, 3)
        self.assertIn("next action for planner is wait", result.stderr)

    def test_consensus_gate_blocks_with_open_question(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "?")
        result = self.write_consensus()
        self.assertEqual(result.returncode, 3)
        self.assertIn("escalate_open_questions", result.stderr)

    def test_consensus_succeeds_after_full_protocol(self) -> None:
        self.init_fresh()
        run("add-question", "--file", str(self.chat), "--role", "planner", "--question", "?")
        run("answer-question", "--file", str(self.chat), "--id", "Q1", "--answer", "go")
        self.fully_consent()
        self.assertEqual(self.post_signoff_recap().returncode, 0)
        signoff = run("signoff", "--file", str(self.chat))
        self.assertEqual(signoff.returncode, 0, signoff.stderr)
        result = self.write_consensus()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.inspect()["consensus_exists"])

    def test_consensus_refused_when_plan_file_changed_after_signoff(self) -> None:
        self.init_fresh()
        plan = self.fully_consent(self.valid_plan_body("v1"))
        self.assertEqual(self.post_signoff_recap().returncode, 0)
        run("signoff", "--file", str(self.chat))
        plan.write_text("tampered\n", encoding="utf-8")
        result = self.write_consensus()
        self.assertEqual(result.returncode, 3)
        self.assertIn("plan file has changed", result.stderr)

    def test_missing_sidecar_is_a_hard_error(self) -> None:
        # Create a chat manually without a sidecar to simulate a corrupted state.
        self.chat.write_text(
            "# Co-plan chat\n\n---\n\n### [goal] 2026-05-12T00:00:00Z\n\norphan\n\n",
            encoding="utf-8",
        )
        self.assertFalse(self.sidecar.exists())

        # Every sidecar-touching command refuses with exit 1 and a clear message.
        for cmd in (
            ("inspect", "--file", str(self.chat)),
            ("next-action", "--file", str(self.chat), "--self", "planner"),
            ("post-turn", "--file", str(self.chat), "--self", "planner", "--body-file", str(self.tmp / "missing.md")),
            ("post-signoff-recap", "--file", str(self.chat), "--body-file", str(self.tmp / "missing.md")),
            ("add-question", "--file", str(self.chat), "--role", "planner", "--question", "?"),
            ("escalate-open-questions", "--file", str(self.chat), "--body-file", str(self.tmp / "missing.md")),
            ("answer-question", "--file", str(self.chat), "--id", "Q1", "--answer", "x"),
            ("resolve", "--file", str(self.chat), "--decision", "x"),
            ("list-questions", "--file", str(self.chat)),
            ("validate-plan", "--file", str(self.chat), "--plan-file", str(self.tmp / "chat.plan.md")),
            ("signoff", "--file", str(self.chat)),
            ("write-consensus", "--file", str(self.chat)),
        ):
            result = run(*cmd)
            self.assertEqual(result.returncode, 1, f"{cmd[0]} returned {result.returncode}")
            self.assertIn("sidecar missing", result.stderr)

    def test_add_question_after_signoff_refused(self) -> None:
        self.init_fresh()
        self.fully_consent()
        self.assertEqual(self.post_signoff_recap().returncode, 0)
        run("signoff", "--file", str(self.chat))
        result = run(
            "add-question",
            "--file",
            str(self.chat),
            "--role",
            "planner",
            "--question",
            "too late?",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("sign-off", result.stderr)

    def test_concurrent_add_question_assigns_unique_ids(self) -> None:
        self.init_fresh()
        errors: list[str] = []
        ids: list[str] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            result = run(
                "add-question",
                "--file",
                str(self.chat),
                "--role",
                "planner",
                "--question",
                f"q{index}",
            )
            if result.returncode != 0:
                with lock:
                    errors.append(result.stderr)
                return
            with lock:
                ids.append(json.loads(result.stdout)["id"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [], errors)
        self.assertEqual(sorted(ids), [f"Q{n}" for n in range(1, 9)])


if __name__ == "__main__":
    unittest.main()
