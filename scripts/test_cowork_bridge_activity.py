#!/usr/bin/env python3
"""Fixture-driven tests for M4 Package C: real-evidence controller-adapter
activity classification, typed OpenCode refusal/error extraction, the
bounded first-token deadline (SIGTERM then SIGKILL/reap) mechanism, and
`live_child_handle` in cowork_bridge.py.

Covers the frozen brief's required gates:

1. `python3 -m unittest scripts.test_cowork_bridge_activity -v` (this file).
2. Runs alongside `scripts.test_cowork_bridge_capacity` with no collisions.
3. (retired: the M4 delivery diff proof against its signed base commit.)
4. `OpencodeRefusalExtractionTest` -- structured-output AND log-tail
   fixtures.
5. `*FirstTokenDeadlineTest` (one per controller) -- deadline/reap/no-orphan
   fixtures.
7. `*LiveChildHandleTest` (one per controller) -- truthful live-handle
   fixtures.
8. This suite never imports/patches cowork_state and performs no file
   writes of its own beyond the ordinary unittest temp-dir housekeeping.

Run standalone:

    python3 -m unittest scripts/test_cowork_bridge_activity.py -v
"""

import ast
import dis
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock as mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_activity as activity  # noqa: E402
import cowork_bridge as bridge  # noqa: E402

# ---------------------------------------------------------------------------
# Fake subprocess.Popen doubles
# ---------------------------------------------------------------------------

class ScriptedProc:
    """A fast, scripted one-shot child: `.stdout` yields exactly `lines`
    then EOFs. Used for CodexSession/OpencodeSession fixtures that never
    approach the first-token deadline."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if not (self.terminated or self.killed) else 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class HangingProc:
    """A one-shot child whose `.stdout` NEVER produces a line on its own --
    genuinely unresponsive, exactly the shape the bounded first-token
    deadline exists to catch. `.terminate()` alone does not stop the
    (simulated) hang unless `terminate_is_effective=True`; this lets a
    fixture force the SIGTERM-then-SIGKILL escalation path in `_terminate`
    (terminate() alone leaves `.wait(timeout=...)` raising
    `subprocess.TimeoutExpired` until `.kill()` is ALSO called)."""

    class _NeverYields:
        def __iter__(self):
            return self

        def __next__(self):
            time.sleep(5)
            raise StopIteration

    def __init__(self, terminate_is_effective=True):
        self.stdout = self._NeverYields()
        self.terminated = False
        self.killed = False
        self._terminate_is_effective = terminate_is_effective

    def poll(self):
        if self.killed:
            return -9
        if self.terminated and self._terminate_is_effective:
            return -15
        return None

    def wait(self, timeout=None):
        if self.terminated and (self._terminate_is_effective or self.killed):
            return -15 if not self.killed else -9
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class PushableStream:
    """A thread-safe, blocking iterator a test can `push()` lines into --
    the shape a real OS pipe presents to a reader thread, but fully
    in-process and deterministic. Used for ClaudeSession's persistent-duplex
    fixtures (a hang is simply "nothing pushed yet")."""

    def __init__(self):
        self._items = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def push(self, item):
        with self._cv:
            self._items.append(item)
            self._cv.notify_all()

    def __iter__(self):
        return self

    def __next__(self):
        with self._cv:
            while not self._items:
                self._cv.wait()
            return self._items.pop(0)


class ClaudeFakeProc:
    """A persistent-duplex double: `.stdout` is a `PushableStream` a test
    feeds by hand, `.stdin` is a plain StringIO sink. No `.terminate()`/
    `.kill()` at all -- deliberate: a Claude first-token deadline must NEVER
    call either (see live_child_handle's pinned semantics), so a fixture
    calling them would raise AttributeError and fail loudly."""

    def __init__(self):
        self.stdout = PushableStream()
        self.stdin = io.StringIO()

    def poll(self):
        return None


def _claude_text_lines(text, session_id="S1"):
    return [
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]}}),
        json.dumps({"type": "result", "subtype": "success",
                    "session_id": session_id}),
    ]


# ---------------------------------------------------------------------------
# Gate 1/pure classification: real-evidence ActivityClass mapping
# ---------------------------------------------------------------------------

class ActivityClassificationTest(unittest.TestCase):
    CLASSIFIERS = {
        "claude": bridge.classify_claude_activity,
        "codex": bridge.classify_codex_activity,
        "opencode": bridge.classify_opencode_activity,
    }

    # Every kind explicitly named in each controller's own kind map, with
    # its expected classification -- the closed decision table itself.
    EXPECTED_KIND_CLASS = {
        "claude": {
            "assistant": "productive_model_work",
            "partial": "productive_model_work",
            "result": "productive_model_work",
            "tool": "local_tool_work",
            "denied": "policy_denial",
            "error": "process_crash",
            "transport_error": "process_crash",
            "no_first_token": "hung_descendant",
        },
        "codex": {
            "message": "productive_model_work",
            "tool": "local_tool_work",
            "tool_done": "local_tool_work",
            "denied": "policy_denial",
            "error": "process_crash",
            "transport_error": "process_crash",
            "no_first_token": "hung_descendant",
        },
        "opencode": {
            "message": "productive_model_work",
            "tool": "local_tool_work",
            "tool_done": "local_tool_work",
            "denied": "policy_denial",
            "error": "process_crash",
            "transport_error": "process_crash",
            "no_first_token": "hung_descendant",
        },
    }

    # A meta/bookkeeping kind each parser really emits, distinct from the
    # kind map above -- real, observed evidence, never silence.
    META_KINDS = {
        "claude": ("system", "user_replay", "child_usage", "child_start",
                  "child_end", "other"),
        "codex": ("thread_started", "turn_started", "turn_completed",
                  "other"),
        "opencode": ("step_finish", "other"),
    }

    TEXT_CARRYING_KIND = {
        "claude": "assistant", "codex": "message", "opencode": "message",
    }

    # Every text-carrying kind per controller (claude alone has three:
    # "assistant", "partial" AND "result") -- distinct from
    # TEXT_CARRYING_KIND above, which names just one representative kind
    # per controller for the blank/nonblank-text fixtures below.
    ALL_TEXT_CARRYING_KINDS = {
        "claude": ("assistant", "partial", "result"),
        "codex": ("message",),
        "opencode": ("message",),
    }

    def test_every_named_kind_classifies_as_documented(self):
        for controller, table in self.EXPECTED_KIND_CLASS.items():
            classify = self.CLASSIFIERS[controller]
            for kind, expected in table.items():
                evidence = {"kind": kind}
                if kind in self.ALL_TEXT_CARRYING_KINDS[controller]:
                    evidence["text"] = "real output"
                with self.subTest(controller=controller, kind=kind):
                    self.assertEqual(classify(evidence), expected)

    def test_every_returned_value_is_in_the_closed_taxonomy(self):
        for controller, table in self.EXPECTED_KIND_CLASS.items():
            classify = self.CLASSIFIERS[controller]
            for kind in table:
                evidence = {"kind": kind, "text": "x"}
                with self.subTest(controller=controller, kind=kind):
                    self.assertIn(classify(evidence), activity.ACTIVITY_CLASS_SET)

    def test_meta_bookkeeping_kinds_are_provider_wait_not_silence(self):
        for controller, kinds in self.META_KINDS.items():
            classify = self.CLASSIFIERS[controller]
            for kind in kinds:
                with self.subTest(controller=controller, kind=kind):
                    self.assertEqual(classify({"kind": kind}), "provider_wait")

    def test_text_carrying_kind_with_blank_text_is_provider_wait(self):
        for controller, kind in self.TEXT_CARRYING_KIND.items():
            classify = self.CLASSIFIERS[controller]
            with self.subTest(controller=controller, case="missing"):
                self.assertEqual(classify({"kind": kind}), "provider_wait")
            with self.subTest(controller=controller, case="empty"):
                self.assertEqual(classify({"kind": kind, "text": ""}),
                                 "provider_wait")
            with self.subTest(controller=controller, case="whitespace"):
                self.assertEqual(classify({"kind": kind, "text": "   "}),
                                 "provider_wait")
            with self.subTest(controller=controller, case="non_string"):
                self.assertEqual(classify({"kind": kind, "text": 123}),
                                 "provider_wait")

    def test_text_carrying_kind_with_real_text_is_productive(self):
        for controller, kind in self.TEXT_CARRYING_KIND.items():
            classify = self.CLASSIFIERS[controller]
            with self.subTest(controller=controller):
                self.assertEqual(
                    classify({"kind": kind, "text": "hello"}),
                    "productive_model_work")

    def test_claude_result_with_error_max_turns_is_process_crash_not_productive(self):
        # C-MAJOR-01 negative control: a failed turn (real parse_claude_
        # event evidence, not a hand-built fixture) must never be
        # certified productive_model_work merely because a `result` event
        # exists.
        evidence = bridge.parse_claude_event(
            {"type": "result", "subtype": "error_max_turns"})
        self.assertTrue(evidence["is_error"])
        self.assertEqual(bridge.classify_claude_activity(evidence),
                         "process_crash")

    def test_claude_result_with_error_during_execution_is_process_crash(self):
        evidence = bridge.parse_claude_event(
            {"type": "result", "subtype": "error_during_execution"})
        self.assertTrue(evidence["is_error"])
        self.assertEqual(bridge.classify_claude_activity(evidence),
                         "process_crash")

    def test_claude_successful_textless_result_is_provider_wait(self):
        # A genuinely successful turn-terminal event with no accompanying
        # model text (the CLI's own `result` field empty/absent) is real
        # bookkeeping evidence, not model output -- never
        # productive_model_work.
        evidence = bridge.parse_claude_event(
            {"type": "result", "subtype": "success"})
        self.assertFalse(evidence["is_error"])
        self.assertEqual(evidence["text"], "")
        self.assertEqual(bridge.classify_claude_activity(evidence),
                         "provider_wait")

    def test_claude_successful_result_with_text_is_productive(self):
        evidence = bridge.parse_claude_event(
            {"type": "result", "subtype": "success", "result": "the answer"})
        self.assertEqual(bridge.classify_claude_activity(evidence),
                         "productive_model_work")

    def test_is_error_flag_overrides_kind_map_for_every_controller(self):
        # is_error is Claude-specific in practice (only parse_claude_event
        # ever sets it), but the classifier's own is_error check is a
        # general, closed rule applied uniformly -- proven here directly
        # against all three classifiers.
        for controller, classify in self.CLASSIFIERS.items():
            with self.subTest(controller=controller):
                self.assertEqual(
                    classify({"kind": "tool", "is_error": True}),
                    "process_crash")

    def test_missing_evidence_is_the_only_path_to_no_evidence_silence(self):
        for controller, classify in self.CLASSIFIERS.items():
            for bad in (None, {}, {"kind": None}, {"kind": ""},
                       {"kind": 42}, "not-a-dict", [], 0, False):
                with self.subTest(controller=controller, evidence=bad):
                    self.assertEqual(classify(bad), "no_evidence_silence")

    def test_first_token_timeout_is_never_silence(self):
        for controller, classify in self.CLASSIFIERS.items():
            with self.subTest(controller=controller):
                result = classify({"kind": "no_first_token"})
                self.assertEqual(result, "hung_descendant")
                self.assertNotEqual(result, "no_evidence_silence")

    def test_unrecognized_kind_is_live_evidence_not_silence(self):
        # A kind no parser actually emits is still SOME evidence (a
        # nonempty string kind was observed) -- never fabricated silence.
        for controller, classify in self.CLASSIFIERS.items():
            with self.subTest(controller=controller):
                self.assertEqual(
                    classify({"kind": "a_future_event_kind"}), "provider_wait")

    def test_functions_never_raise_on_garbage_input(self):
        garbage = [None, {}, [], "x", 1, 1.5, True, object(),
                  {"kind": object()}, {"kind": ["assistant"]}]
        for controller, classify in self.CLASSIFIERS.items():
            for item in garbage:
                with self.subTest(controller=controller, item=item):
                    self.assertIn(classify(item), activity.ACTIVITY_CLASS_SET)

    def test_deterministic_pure_transform(self):
        for controller, classify in self.CLASSIFIERS.items():
            evidence = {"kind": "tool"}
            with self.subTest(controller=controller):
                self.assertEqual(classify(dict(evidence)), classify(dict(evidence)))


# ---------------------------------------------------------------------------
# Gate 4: typed OpenCode refusal/error extraction
# ---------------------------------------------------------------------------

class OpencodeRefusalExtractionTest(unittest.TestCase):
    def test_structured_quota_event(self):
        result = bridge.classify_opencode_refusal(event={
            "type": "error",
            "error": {"name": "rate_limit_exceeded",
                     "data": {"message": "rate limited by upstream"}}})
        self.assertEqual(result, {"schema_version": 1,
                                  "record": "ControllerTurnOutcome",
                                  "outcome": "refused",
                                  "failure_class": "quota"})
        activity.validate_controller_turn_outcome(result)

    def test_structured_overload_event(self):
        result = bridge.classify_opencode_refusal(event={
            "type": "error",
            "error": {"name": "server_overloaded", "data": {}}})
        self.assertEqual(result["failure_class"], "overload")
        activity.validate_controller_turn_outcome(result)

    def test_structured_auth_event(self):
        result = bridge.classify_opencode_refusal(event={
            "type": "error",
            "error": {"name": None, "data": {"message": "unauthorized",
                                             "status": 401}}})
        self.assertEqual(result["failure_class"], "auth")
        activity.validate_controller_turn_outcome(result)

    def test_structured_transport_event(self):
        result = bridge.classify_opencode_refusal(event={
            "type": "error",
            "error": {"name": "connection_reset", "data": {}}})
        self.assertEqual(result["failure_class"], "transport")
        activity.validate_controller_turn_outcome(result)

    def test_structured_unknown_event(self):
        result = bridge.classify_opencode_refusal(event={
            "type": "error",
            "error": {"name": "brand_new_2027", "data": {"message": "?"}}})
        self.assertEqual(result["failure_class"], "unknown_provider_failure")
        activity.validate_controller_turn_outcome(result)

    def test_structured_balance_event_by_name(self):
        result = bridge.classify_opencode_refusal(event={
            "type": "error",
            "error": {"name": "insufficient_balance", "data": {}}})
        self.assertEqual(result["failure_class"], "balance")
        activity.validate_controller_turn_outcome(result)

    def test_structured_balance_event_by_message_text(self):
        result = bridge.classify_opencode_refusal(event={
            "type": "error",
            "error": {"name": None,
                     "data": {"message": "your credit balance exhausted"}}})
        self.assertEqual(result["failure_class"], "balance")

    def test_non_error_event_is_not_a_refusal(self):
        self.assertIsNone(bridge.classify_opencode_refusal(
            event={"type": "tool", "tool": "bash"}))
        self.assertIsNone(bridge.classify_opencode_refusal(
            event={"type": "text", "part": {"text": "ok"}}))

    def test_log_tail_leading_http_status(self):
        result = bridge.classify_opencode_refusal(
            log_tail="401 Unauthorized: token expired\nsome trailing detail")
        self.assertEqual(result["failure_class"], "auth")
        activity.validate_controller_turn_outcome(result)

    def test_log_tail_overload_status(self):
        result = bridge.classify_opencode_refusal(log_tail="529 Overloaded")
        self.assertEqual(result["failure_class"], "overload")

    def test_log_tail_quota_status(self):
        result = bridge.classify_opencode_refusal(log_tail="429 Too Many Requests")
        self.assertEqual(result["failure_class"], "quota")

    def test_log_tail_balance_phrase(self):
        # Human-readable log text embeds the token space-joined, exactly
        # like a provider message would ("insufficient balance"), never
        # with its machine-token underscore -- see
        # `_opencode_balance_depletion_token`'s docstring.
        result = bridge.classify_opencode_refusal(
            log_tail="fatal: insufficient balance for this account")
        self.assertEqual(result["failure_class"], "balance")
        activity.validate_controller_turn_outcome(result)

    def test_log_tail_without_recognizable_status_is_none(self):
        self.assertIsNone(bridge.classify_opencode_refusal(
            log_tail="opencode: something went sideways, no code here"))

    def test_log_tail_status_not_at_start_is_ignored(self):
        # The narrow closed grammar only matches a LEADING status code --
        # never a generic search anywhere in the text.
        self.assertIsNone(bridge.classify_opencode_refusal(
            log_tail="see error 401 further down the log"))

    def test_no_event_and_no_log_tail_is_none(self):
        self.assertIsNone(bridge.classify_opencode_refusal())
        self.assertIsNone(bridge.classify_opencode_refusal(event=None, log_tail=None))
        self.assertIsNone(bridge.classify_opencode_refusal(log_tail=""))
        self.assertIsNone(bridge.classify_opencode_refusal(log_tail="   "))

    def test_structured_event_takes_priority_over_log_tail(self):
        result = bridge.classify_opencode_refusal(
            event={"type": "error", "error": {"name": "rate_limit_exceeded",
                                              "data": {}}},
            log_tail="401 this text is a decoy")
        self.assertEqual(result["failure_class"], "quota")

    def test_log_tail_consulted_only_when_event_carries_nothing(self):
        result = bridge.classify_opencode_refusal(
            event={"type": "tool", "tool": "bash"},
            log_tail="401 fell back to the log tail")
        self.assertEqual(result["failure_class"], "auth")

    def test_malformed_event_shapes_never_raise(self):
        for bad_event in (None, "x", 5, {"type": "error"},
                          {"type": "error", "error": "not-a-dict"},
                          {"type": "error", "error": {"data": "not-a-dict"}}):
            with self.subTest(event=bad_event):
                bridge.classify_opencode_refusal(event=bad_event)  # must not raise


class ControllerTurnOutcomeNoFirstTokenTest(unittest.TestCase):
    def test_shape_and_validation(self):
        result = bridge.controller_turn_outcome_no_first_token()
        self.assertEqual(result, {"schema_version": 1,
                                  "record": "ControllerTurnOutcome",
                                  "outcome": "no_first_token",
                                  "failure_class": None})
        activity.validate_controller_turn_outcome(result)

    def test_deterministic(self):
        self.assertEqual(bridge.controller_turn_outcome_no_first_token(),
                         bridge.controller_turn_outcome_no_first_token())


# ---------------------------------------------------------------------------
# Gate 5: per-controller first-token deadline / SIGTERM-then-SIGKILL / reap
# / no-orphan fixtures
# ---------------------------------------------------------------------------

class CodexFirstTokenDeadlineTest(unittest.TestCase):
    def test_hang_yields_typed_no_first_token(self):
        hp = HangingProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            s._first_token_deadline_seconds = 0.05
            result = s.send("go")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "no_first_token")
        self.assertEqual(result["error_type"], "no_first_token")
        # C-MAJOR-04: the real Package A ControllerTurnOutcome record is
        # attached to the returned turn_result, not merely available as
        # untethered library code.
        self.assertEqual(result["controller_turn_outcome"],
                         bridge.controller_turn_outcome_no_first_token())
        activity.validate_controller_turn_outcome(
            result["controller_turn_outcome"])

    def test_hang_terminates_reaps_and_leaves_no_orphan(self):
        hp = HangingProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            s._first_token_deadline_seconds = 0.05
            s.send("go")
        self.assertTrue(hp.terminated)
        self.assertIsNone(bridge.live_child_handle(s))  # reaped: no orphan

    def test_sigterm_then_sigkill_escalation_when_terminate_ineffective(self):
        hp = HangingProc(terminate_is_effective=False)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            s._first_token_deadline_seconds = 0.05
            s.send("go")
        self.assertTrue(hp.terminated)  # SIGTERM was tried first
        self.assertTrue(hp.killed)      # then escalated to SIGKILL
        self.assertIsNone(bridge.live_child_handle(s))

    def test_fast_response_never_trips_the_deadline(self):
        lines = [json.dumps({"type": "thread.started", "thread_id": "T1"}),
                json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": "done"}})]
        proc = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            s._first_token_deadline_seconds = 5.0
            result = s.send("go")
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["result"], "no_first_token")
        self.assertFalse(proc.terminated)
        self.assertFalse(proc.killed)

    def test_next_send_after_a_first_ever_turns_timeout_is_missing_thread_id(self):
        # Codex is turn-based: a fresh turn that timed out before any event
        # (including thread.started) never captured a thread_id, so this
        # session can never resume -- this is the SAME pre-existing
        # missing_thread_id contract `send()` already enforces (mirroring
        # test_cowork.py's identical fixture for a manually-cleared
        # thread_id); the new no_first_token path does not bypass it, and
        # no orphan process is left behind trying to "recover" the turn.
        hp = HangingProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            s._first_token_deadline_seconds = 0.05
            first = s.send("go")
        self.assertEqual(first["result"], "no_first_token")
        self.assertIsNone(s.thread_id)
        second = s.send("next")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_type"], "missing_thread_id")

    def test_next_turn_after_a_mid_thread_timeout_reuses_the_thread(self):
        # A RESUMED thread already has a captured thread_id (from an
        # earlier successful turn), so a later turn's timeout does not
        # strand the session -- the next send() correctly resumes it.
        lines = [json.dumps({"type": "thread.started", "thread_id": "T1"}),
                json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": "hi"}})]
        proc1 = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc1):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            first = s.send("go")
        self.assertTrue(first["ok"])
        self.assertEqual(s.thread_id, "T1")

        hp = HangingProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            s._first_token_deadline_seconds = 0.05
            second = s.send("next")
        self.assertEqual(second["result"], "no_first_token")
        self.assertEqual(s.thread_id, "T1")  # never lost

        lines3 = [json.dumps({"type": "item.completed",
                              "item": {"type": "agent_message",
                                       "text": "third"}})]
        proc3 = ScriptedProc(lines3)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc3):
            s._first_token_deadline_seconds = 5.0
            third = s.send("third")
        self.assertTrue(third["ok"])


class OpencodeFirstTokenDeadlineTest(unittest.TestCase):
    def _session(self, tmp):
        rp = os.path.join(tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        return bridge.OpencodeSession(rp, "implement", True, agent_base_dir=tmp)

    def test_hang_yields_typed_no_first_token(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        hp = HangingProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            s = self._session(tmp)
            s._first_token_deadline_seconds = 0.05
            result = s.send("go")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "no_first_token")
        self.assertEqual(result["error_type"], "no_first_token")
        self.assertTrue(hp.terminated)
        self.assertIsNone(bridge.live_child_handle(s))
        # C-MAJOR-04
        self.assertEqual(result["controller_turn_outcome"],
                         bridge.controller_turn_outcome_no_first_token())
        activity.validate_controller_turn_outcome(
            result["controller_turn_outcome"])

    def test_timeout_never_triggers_the_orch052_delivery_fallback(self):
        # A zero-event timeout must never be confused with the ORCH-052
        # agent-delivery-failure retry path (a materially different failure
        # mode with its own typed handling).
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        hp = HangingProc()
        popen_calls = []

        def fake_popen(command, **kwargs):
            popen_calls.append(command)
            return hp

        with mock.patch.object(bridge.subprocess, "Popen", side_effect=fake_popen):
            s = self._session(tmp)
            s._first_token_deadline_seconds = 0.05
            s.send("go")
        self.assertEqual(len(popen_calls), 1)  # no retry spawn

    def test_sigterm_then_sigkill_escalation(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        hp = HangingProc(terminate_is_effective=False)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            s = self._session(tmp)
            s._first_token_deadline_seconds = 0.05
            s.send("go")
        self.assertTrue(hp.terminated)
        self.assertTrue(hp.killed)
        self.assertIsNone(bridge.live_child_handle(s))

    def test_fast_response_never_trips_the_deadline(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({"type": "text", "sessionID": "ses_X",
                            "part": {"text": "hello"}})]
        proc = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = self._session(tmp)
            s._first_token_deadline_seconds = 5.0
            result = s.send("go")
        self.assertTrue(result["ok"])
        self.assertFalse(proc.terminated)


class ClaudeFirstTokenDeadlineTest(unittest.TestCase):
    def test_hang_yields_typed_no_first_token_without_killing_the_process(self):
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 0.05
            result = s.send("first")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "no_first_token")
        self.assertEqual(result["error_type"], "no_first_token")
        # C-MAJOR-04
        self.assertEqual(result["controller_turn_outcome"],
                         bridge.controller_turn_outcome_no_first_token())
        activity.validate_controller_turn_outcome(
            result["controller_turn_outcome"])

    def test_timeout_never_tears_down_the_session_lifetime_child(self):
        # Pinned semantics: Claude's live handle is non-null across turns
        # until close() -- a per-turn deadline is NOT a SIGTERM/SIGKILL/reap
        # of this persistent duplex process.
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 0.05
            s.send("first")
            self.assertIs(bridge.live_child_handle(s), proc)

    def test_abandoned_turns_stale_output_never_leaks_into_the_next_turn(self):
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 0.05
            first = s.send("first")
            self.assertEqual(first["result"], "no_first_token")

            # The abandoned turn's late output finally arrives.
            proc.stdout.push(json.dumps(
                {"type": "result", "subtype": "success", "session_id": "S1"}))
            time.sleep(0.1)  # let it land in the shared queue

            def push_real_turn():
                time.sleep(0.05)
                for line in _claude_text_lines("hello turn2"):
                    proc.stdout.push(line)

            threading.Thread(target=push_real_turn, daemon=True).start()
            s._first_token_deadline_seconds = 5.0
            second = s.send("second")
        self.assertTrue(second["ok"])
        self.assertIn("hello turn2", out.getvalue())

    def test_fast_response_never_trips_the_deadline(self):
        proc = ClaudeFakeProc()
        for line in _claude_text_lines("hi"):
            proc.stdout.push(line)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 5.0
            result = s.send("hello")
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["result"], "no_first_token")

    def test_stale_output_arriving_after_next_send_has_already_begun_never_leaks(self):
        # C-BLOCK-01 adversarial fixture (the review's own reproduction):
        # unlike the "already queued before send()" fixture above, turn
        # 2's send() call starts reading BEFORE any of turn 1's late
        # output exists at all -- turn 1's stale answer is pushed WHILE
        # turn 2's own read loop is already blocked waiting. A time-
        # ordered (queue-drain-at-start) mitigation cannot catch this; the
        # provenance-based (result-event-keyed) drain must.
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 0.05
            first = s.send("first")
        self.assertEqual(first["result"], "no_first_token")

        def push_stale_then_real():
            # Turn 1's answer arrives 0.15s later -- AFTER turn 2's send()
            # below has already started blocking in its own read loop.
            time.sleep(0.15)
            proc.stdout.push(json.dumps(
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "ANSWER-TO-QUESTION-ONE"}]}}))
            proc.stdout.push(json.dumps(
                {"type": "result", "subtype": "success", "session_id": "S1"}))
            for line in _claude_text_lines("ANSWER-TO-QUESTION-TWO"):
                proc.stdout.push(line)

        threading.Thread(target=push_stale_then_real, daemon=True).start()
        s._first_token_deadline_seconds = 5.0
        second = s.send("second")  # begins reading immediately, no pre-sleep
        self.assertTrue(second["ok"])
        self.assertEqual(second["result"], "ok")
        self.assertNotIn("ANSWER-TO-QUESTION-ONE", out.getvalue())
        self.assertIn("ANSWER-TO-QUESTION-TWO", out.getvalue())

    def test_drain_gives_up_after_its_own_bound_and_never_hangs_forever(self):
        # Safety net: if the abandoned turn's terminal `result` event NEVER
        # arrives, the provenance-based drain does not block the NEXT turn
        # indefinitely -- it gives up after its own bounded wait (the
        # pending count is simply retried again on a later turn).
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 0.05
            first = s.send("first")
        self.assertEqual(first["result"], "no_first_token")
        self.assertEqual(s._pending_abandoned_results, 1)

        started = time.monotonic()
        s._first_token_deadline_seconds = 0.1
        second = s.send("second")  # turn 1's answer never arrives
        elapsed = time.monotonic() - started
        self.assertEqual(second["result"], "no_first_token")
        self.assertLess(elapsed, 2.0)
        # Still unresolved -- AND turn 2 itself also timed out, so a THIRD
        # turn would owe the session two unconsumed result events.
        self.assertEqual(s._pending_abandoned_results, 2)

    def test_multiple_consecutive_timeouts_all_drained_before_real_content(self):
        # Generalizes the single-timeout case: two turns in a row time out
        # (self._pending_abandoned_results reaches 2) before a third turn
        # finally gets real content -- the drain must consume BOTH stale
        # `result` events before accepting anything as the third turn's own.
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 0.05
            first = s.send("t1")
            second = s.send("t2")
        self.assertEqual(first["result"], "no_first_token")
        self.assertEqual(second["result"], "no_first_token")
        self.assertEqual(s._pending_abandoned_results, 2)

        def push_two_stale_then_real():
            time.sleep(0.1)
            proc.stdout.push(json.dumps(
                {"type": "result", "subtype": "success", "session_id": "S1"}))
            proc.stdout.push(json.dumps(
                {"type": "result", "subtype": "success", "session_id": "S1"}))
            for line in _claude_text_lines("REAL-THIRD-TURN"):
                proc.stdout.push(line)

        threading.Thread(target=push_two_stale_then_real, daemon=True).start()
        s._first_token_deadline_seconds = 5.0
        third = s.send("t3")
        self.assertTrue(third["ok"])
        self.assertEqual(s._pending_abandoned_results, 0)
        self.assertIn("REAL-THIRD-TURN", out.getvalue())

    def test_stale_result_arriving_after_the_drain_bound_but_within_the_next_turns_read_window_never_leaks(self):
        # C-BLOCK-03: the pre-send drain (run before stdin is even written
        # for the new turn) is itself bounded by the same deadline and can
        # give up with the abandoned turn's own `result` still not having
        # arrived. If that `result` (and any stale text ahead of it) then
        # lands DURING this turn's own main read loop -- after the drain
        # bound expired, but still inside the fresh first-token deadline
        # window the main loop opens next -- it must still be recognized by
        # its `result` provenance boundary and discarded there: never armed
        # as this turn's first token, never rendered, and never allowed to
        # attribute this turn's session/result.
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s._first_token_deadline_seconds = 0.05
            first = s.send("first")
        self.assertEqual(first["result"], "no_first_token")
        self.assertEqual(s._pending_abandoned_results, 1)

        def push_after_drain_bound_expires():
            # Turn 2's pre-send drain is bounded by its own 0.15s deadline
            # and gives up at ~0.15s with nothing yet in the queue. This
            # push lands at 0.22s -- strictly after that drain bound, but
            # still inside the main loop's own fresh ~0.15s first-token
            # window (opened right after the drain gives up, so it runs to
            # roughly 0.30s).
            time.sleep(0.22)
            proc.stdout.push(json.dumps(
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "STALE-ANSWER-ONE"}]}}))
            proc.stdout.push(json.dumps(
                {"type": "result", "subtype": "success",
                 "session_id": "STALE-SID"}))
            for line in _claude_text_lines("REAL-SECOND-TURN",
                                           session_id="S2"):
                proc.stdout.push(line)

        threading.Thread(target=push_after_drain_bound_expires,
                         daemon=True).start()
        s._first_token_deadline_seconds = 0.15
        second = s.send("second")
        self.assertTrue(second["ok"])
        self.assertEqual(second["result"], "ok")
        self.assertEqual(second["session_id"], "S2")  # never the stale one
        self.assertEqual(s._pending_abandoned_results, 0)
        self.assertNotIn("STALE-ANSWER-ONE", out.getvalue())
        self.assertIn("REAL-SECOND-TURN", out.getvalue())


# ---------------------------------------------------------------------------
# Gate 6: per-controller foreground-label state-machine fixtures
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Gate 7: per-controller truthful live_child_handle fixtures
# ---------------------------------------------------------------------------

class ClaudeLiveChildHandleTest(unittest.TestCase):
    def test_non_null_immediately_after_construction(self):
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=io.StringIO(), session_id="S1")
        self.assertIs(bridge.live_child_handle(s), proc)

    def test_non_null_across_multiple_successful_turns(self):
        proc = ClaudeFakeProc()
        for line in _claude_text_lines("t1"):
            proc.stdout.push(line)
        for line in _claude_text_lines("t2"):
            proc.stdout.push(line)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=io.StringIO(), session_id="S1")
            self.assertIsNotNone(bridge.live_child_handle(s))
            s.send("first")
            self.assertIsNotNone(bridge.live_child_handle(s))
            s.send("second")
            self.assertIsNotNone(bridge.live_child_handle(s))

    def test_null_only_after_close(self):
        proc = ClaudeFakeProc()
        # close() runs the bounded cleanup helper, which needs
        # poll/terminate/wait. Like the real claude, the fake exits on stdin
        # EOF (close()'s graceful request) or on terminate().
        proc.terminated = False

        def poll():
            return 0 if proc.terminated or proc.stdin.closed else None

        def terminate():
            proc.terminated = True

        def wait(timeout=None):
            return 0

        proc.poll = poll
        proc.terminate = terminate
        proc.wait = wait
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=io.StringIO(), session_id="S1")
            self.assertIsNotNone(bridge.live_child_handle(s))
            s.close()
        self.assertIsNone(bridge.live_child_handle(s))


class CodexLiveChildHandleTest(unittest.TestCase):
    def test_null_before_first_send(self):
        s = bridge.CodexSession("implement", True, io_out=io.StringIO())
        self.assertIsNone(bridge.live_child_handle(s))

    def test_non_null_while_a_turn_is_in_flight(self):
        gate = threading.Event()
        released = threading.Event()

        class PausingIter:
            def __iter__(self):
                return self

            def __next__(self):
                if not released.is_set():
                    gate.set()
                    released.wait(timeout=5)
                raise StopIteration

        proc = ScriptedProc([])
        proc.stdout = PausingIter()
        s = bridge.CodexSession("implement", True, io_out=io.StringIO())
        result = {}
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            def run_turn():
                result["r"] = s.send("go")

            t = threading.Thread(target=run_turn)
            t.start()
            self.assertTrue(gate.wait(timeout=5))
            self.assertIs(bridge.live_child_handle(s), proc)
            released.set()
            t.join(timeout=5)
        self.assertIsNone(bridge.live_child_handle(s))

    def test_null_again_after_reap(self):
        lines = [json.dumps({"type": "thread.started", "thread_id": "T1"}),
                json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": "done"}})]
        proc = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = bridge.CodexSession("implement", True, io_out=io.StringIO())
            s.send("go")
        self.assertIsNone(bridge.live_child_handle(s))


class OpencodeLiveChildHandleTest(unittest.TestCase):
    def _session(self, tmp):
        rp = os.path.join(tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        return bridge.OpencodeSession(rp, "implement", True, agent_base_dir=tmp)

    def test_null_before_first_send(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        s = self._session(tmp)
        self.assertIsNone(bridge.live_child_handle(s))

    def test_null_again_after_reap(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({"type": "text", "sessionID": "ses_X",
                            "part": {"text": "hello"}})]
        proc = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = self._session(tmp)
            s.send("go")
        self.assertIsNone(bridge.live_child_handle(s))


class LiveChildHandleGenericBehaviorTest(unittest.TestCase):
    def test_none_for_an_unrecognized_session_shape(self):
        class Bare:
            pass
        self.assertIsNone(bridge.live_child_handle(Bare()))

    def test_dead_process_is_null_via_poll(self):
        class DeadProc:
            def poll(self):
                return 1

        class Session:
            controller = "codex"
            _live_proc = DeadProc()

        self.assertIsNone(bridge.live_child_handle(Session()))


# ---------------------------------------------------------------------------
# C-MAJOR-02: OpenCode refusal detector wired to a REAL controller.error
# production emission.
# ---------------------------------------------------------------------------

class RecordingTrace:
    """Minimal Trace double: records every (event_name, fields) call."""

    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class OpencodeControllerErrorEmissionTest(unittest.TestCase):
    def _session(self, tmp, trace=None):
        rp = os.path.join(tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        return bridge.OpencodeSession(rp, "implement", True,
                                      agent_base_dir=tmp, trace=trace)

    def test_quota_refusal_emits_a_typed_controller_error(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        # Matches issue #41's quota-refusal transcript shape: a real
        # opencode structured error event naming a rate-limit token.
        lines = [json.dumps({
            "type": "error", "sessionID": "ses_X",
            "error": {"name": "rate_limit_exceeded",
                     "data": {"message": "rate limited by upstream provider"}}})]
        proc = ScriptedProc(lines)
        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = self._session(tmp, trace=trace)
            started = time.monotonic()
            result = s.send("go")
            elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        error_events = [e for e in trace.events if e[0] == "controller.error"]
        self.assertEqual(len(error_events), 1)
        _, fields = error_events[0]
        self.assertEqual(fields["controller"], "opencode")
        self.assertEqual(fields["outcome"], "refused")
        self.assertEqual(fields["failure_class"], "quota")
        # "emitted within the fixture's synthetic elapsed time" -- promptly,
        # not after some unrelated wait.
        self.assertLess(elapsed, 5.0)

    def test_auth_refusal_emits_the_matching_failure_class(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({
            "type": "error", "sessionID": "ses_X",
            "error": {"name": None,
                     "data": {"message": "unauthorized", "status": 401}}})]
        proc = ScriptedProc(lines)
        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = self._session(tmp, trace=trace)
            s.send("go")
        error_events = [e for e in trace.events if e[0] == "controller.error"]
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0][1]["failure_class"], "auth")

    def test_successful_turn_emits_no_controller_error(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({"type": "text", "sessionID": "ses_X",
                            "part": {"text": "hello"}})]
        proc = ScriptedProc(lines)
        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = self._session(tmp, trace=trace)
            result = s.send("go")
        self.assertTrue(result["ok"])
        self.assertFalse([e for e in trace.events if e[0] == "controller.error"])

    def test_denied_turn_emits_no_controller_error(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({
            "type": "tool_use", "sessionID": "ses_X",
            "part": {"tool": "bash",
                    "state": {"status": "error",
                             "error": "rejected permission for bash"}}})]
        proc = ScriptedProc(lines)
        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = self._session(tmp, trace=trace)
            result = s.send("go")
        self.assertEqual(result["result"], "denied")
        self.assertFalse([e for e in trace.events if e[0] == "controller.error"])

    def test_no_trace_configured_never_raises(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({
            "type": "error", "sessionID": "ses_X",
            "error": {"name": "rate_limit_exceeded", "data": {}}})]
        proc = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = self._session(tmp, trace=None)
            result = s.send("go")  # must not raise
        self.assertFalse(result["ok"])


# ---------------------------------------------------------------------------
# C-MAJOR-03: pure artifact fingerprint/delta computation
# ---------------------------------------------------------------------------

class ArtifactFingerprintDeltaTest(unittest.TestCase):
    def test_fingerprint_of_empty_input_is_none(self):
        self.assertIsNone(bridge.compute_artifact_fingerprint({}))
        self.assertIsNone(bridge.compute_artifact_fingerprint(None))
        self.assertIsNone(bridge.compute_artifact_fingerprint(()))

    def test_fingerprint_normalizes_to_an_independent_copy(self):
        src = {"a.txt": "0" * 64}
        out = bridge.compute_artifact_fingerprint(src)
        self.assertEqual(out, src)
        self.assertIsNot(out, src)
        out["a.txt"] = "mutated"
        self.assertEqual(src["a.txt"], "0" * 64)

    def test_fingerprint_rejects_non_dict_truthy_input(self):
        with self.assertRaises(ValueError):
            bridge.compute_artifact_fingerprint("not-a-dict")
        with self.assertRaises(ValueError):
            bridge.compute_artifact_fingerprint(["a.txt"])

    def test_delta_empty_when_current_is_falsy(self):
        self.assertEqual(bridge.compute_artifact_delta({"a.txt": "1" * 64}, None), ())
        self.assertEqual(bridge.compute_artifact_delta({"a.txt": "1" * 64}, {}), ())

    def test_delta_all_paths_new_when_previous_is_none(self):
        current = {"b.txt": "1" * 64, "a.txt": "0" * 64}
        self.assertEqual(bridge.compute_artifact_delta(None, current),
                         ("a.txt", "b.txt"))

    def test_delta_only_changed_or_new_paths(self):
        previous = {"a.txt": "0" * 64, "b.txt": "1" * 64}
        current = {"a.txt": "0" * 64, "b.txt": "2" * 64, "c.txt": "3" * 64}
        self.assertEqual(bridge.compute_artifact_delta(previous, current),
                         ("b.txt", "c.txt"))

    def test_delta_no_changes_is_empty(self):
        fingerprint = {"a.txt": "0" * 64}
        self.assertEqual(
            bridge.compute_artifact_delta(fingerprint, dict(fingerprint)), ())

    def test_delta_malformed_previous_treated_as_empty_never_raises(self):
        current = {"a.txt": "0" * 64}
        self.assertEqual(bridge.compute_artifact_delta("garbage", current),
                         ("a.txt",))
        self.assertEqual(bridge.compute_artifact_delta(123, current), ("a.txt",))

    def test_removed_paths_are_never_representable(self):
        # Package A's own schema constraint: artifact_delta is always a
        # subset of the CURRENT fingerprint's keys, so a path present only
        # in `previous` can never appear in the delta.
        previous = {"a.txt": "0" * 64, "removed.txt": "9" * 64}
        current = {"a.txt": "0" * 64}
        self.assertEqual(bridge.compute_artifact_delta(previous, current), ())

    def test_round_trips_through_activity_record_validation(self):
        fingerprint = bridge.compute_artifact_fingerprint(
            {"scout.intel.json": "a" * 64, "scout.intel.md": "b" * 64})
        delta = bridge.compute_artifact_delta(
            {"scout.intel.json": "a" * 64}, fingerprint)
        record = {
            "schema_version": 1, "record": "ActivityRecord",
            "work_id": "11111111-1111-1111-1111-111111111111",
            "time": "2026-08-25T12:00:00Z",
            "activity_class": "local_tool_work",
            "source": "controller_native_tool",
            "artifact_fingerprint": fingerprint, "artifact_delta": delta,
            "provider_health": None, "age_seconds": 0,
        }
        normalized = activity.validate_activity_record(record)
        self.assertEqual(set(normalized["artifact_delta"]), {"scout.intel.md"})

    def test_delta_is_a_pure_deterministic_transform(self):
        previous = {"a.txt": "0" * 64}
        current = {"a.txt": "1" * 64, "b.txt": "2" * 64}
        self.assertEqual(
            bridge.compute_artifact_delta(dict(previous), dict(current)),
            bridge.compute_artifact_delta(dict(previous), dict(current)))


# ---------------------------------------------------------------------------
# Purity/import-boundary of the new pure functions (mirrors
# test_cowork_bridge_capacity.py's PurityAndImportBoundaryTest)
# ---------------------------------------------------------------------------

class PurityAndImportBoundaryTest(unittest.TestCase):
    NEW_PURE_FUNCTION_NAMES = (
        "_classify_controller_activity",
        "classify_claude_activity",
        "classify_codex_activity",
        "classify_opencode_activity",
        "_opencode_balance_depletion_token",
        "_controller_turn_outcome",
        "classify_opencode_refusal",
        "controller_turn_outcome_no_first_token",
        "compute_artifact_fingerprint",
        "compute_artifact_delta",
    )

    _FORBIDDEN_GLOBAL_NAMES = frozenset({
        "state_store", "policy", "action_policy", "guard_broker",
        "controller_profiles", "trace_store", "transcript", "probe_cache",
        "open", "subprocess", "os", "threading",
    })

    _FORBIDDEN_CALL_NAMES = frozenset({
        "open", "write", "writelines", "remove", "unlink", "rename",
        "replace", "rmtree", "copy", "copyfile", "move", "system", "popen",
        "Popen", "socket", "chmod", "mkdir", "makedirs", "truncate",
    })

    def test_new_functions_reference_no_forbidden_module_or_io(self):
        for name in self.NEW_PURE_FUNCTION_NAMES:
            func = getattr(bridge, name)
            with self.subTest(function=name):
                used_names = {
                    instr.argval for instr in dis.get_instructions(func)
                    if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_DEREF")
                }
                hit = used_names & self._FORBIDDEN_GLOBAL_NAMES
                self.assertFalse(hit, "function %s references forbidden name(s): %s"
                                 % (name, sorted(hit)))

    def test_new_function_source_contains_no_io_calls(self):
        module_path = os.path.join(_HERE, "cowork_bridge.py")
        with open(module_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=module_path)
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.NEW_PURE_FUNCTION_NAMES:
                found[node.name] = node
        missing = set(self.NEW_PURE_FUNCTION_NAMES) - set(found)
        self.assertFalse(missing, "functions not found in module AST: %s" % sorted(missing))
        for name, func_node in found.items():
            with self.subTest(function=name):
                for node in ast.walk(func_node):
                    if isinstance(node, ast.Call):
                        callee = node.func
                        call_name = (
                            callee.id if isinstance(callee, ast.Name)
                            else callee.attr if isinstance(callee, ast.Attribute)
                            else None)
                        self.assertNotIn(
                            call_name, self._FORBIDDEN_CALL_NAMES,
                            "function %s makes a forbidden I/O-shaped call: %s"
                            % (name, call_name))

    def test_functions_perform_no_file_writes(self):
        before = set(os.listdir(_HERE))
        bridge.classify_claude_activity({"kind": "assistant", "text": "x"})
        bridge.classify_codex_activity({"kind": "tool"})
        bridge.classify_opencode_activity({"kind": "no_first_token"})
        bridge.classify_opencode_refusal(
            event={"type": "error", "error": {"name": "rate_limit_exceeded"}})
        bridge.controller_turn_outcome_no_first_token()
        bridge.compute_artifact_fingerprint({"a.txt": "0" * 64})
        bridge.compute_artifact_delta({"a.txt": "0" * 64}, {"a.txt": "1" * 64})
        after = set(os.listdir(_HERE))
        self.assertEqual(before, after)

    def test_never_persists_or_mutates_input(self):
        evidence = {"kind": "assistant", "text": "hi"}
        snapshot = dict(evidence)
        bridge.classify_claude_activity(evidence)
        self.assertEqual(evidence, snapshot)


# ---------------------------------------------------------------------------
# Issue #89: bounded, confirmed controller cleanup with real fake processes.
#
# Every fake is a harmless `sys.executable -c` program driven by a JSON spec.
# It records its pid (and any grandchild pid) in a pid file, so the test can
# reap exactly the processes it caused. Reaping is per pid: a whole group is
# killed only when the fake leads it and it is not this runner's own group,
# which keeps these tests safe on a base where fakes share a group.
# ---------------------------------------------------------------------------

_FAKE_CONTROLLER = r'''
import json, os, signal, subprocess, sys, time
spec = json.loads(sys.argv[1])


def record(kind, pid):
    with open(spec["pidfile"], "a") as fh:
        fh.write("%s %d\n" % (kind, pid))


if spec.get("grandchild"):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    record("child", child.pid)
term = spec.get("term", "default")
if term == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif term == "delay":
    def _late_exit(signum, frame):
        time.sleep(spec.get("term_delay", 0.2))
        os._exit(0)
    signal.signal(signal.SIGTERM, _late_exit)
record("leader", os.getpid())


def emit(lines):
    for line in lines:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


emit(spec.get("lines", []))
after = spec.get("after", "exit")
if after == "stdin":
    for _ in sys.stdin:
        emit(spec.get("reply", []))
    if spec.get("on_eof", "exit") == "sleep":
        time.sleep(60)
    time.sleep(spec.get("eof_delay", 0))
    if spec.get("marker"):
        with open(spec["marker"], "w") as fh:
            fh.write("clean_exit")
elif after == "sleep":
    time.sleep(60)
elif after == "close_stdout_sleep":
    os.close(1)
    time.sleep(60)
elif after == "close_stdout_then_exit":
    os.close(1)
    time.sleep(spec.get("exit_delay", 0.3))
elif after == "write_forever":
    while True:
        emit(["{}"])
        time.sleep(0.05)
'''

_FAST_BOUNDS = {"stdin_eof": 0.5, "natural_exit": 0.5, "term_grace": 0.5,
                "kill_confirm": 0.5}
_SLACK = 1.0

_CODEX_MESSAGE = json.dumps({"type": "item.completed",
                             "item": {"type": "agent_message", "text": "hi"}})
_OPENCODE_MESSAGE = json.dumps({"type": "text", "sessionID": "ses_X",
                                "part": {"text": "hi"}})


def _pid_gone(pid):
    """True once `pid` no longer exists. A zombie child of this process is
    reaped first so it does not read as alive."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_gone(pid, timeout):
    deadline = time.monotonic() + timeout
    while True:
        if _pid_gone(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _reap(pid):
    """Kill one recorded fake pid. Its whole group only when the fake leads
    it and it is not this runner's own group."""
    if _pid_gone(pid):
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    try:
        if pgid == pid and pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _wait_gone(pid, 2.0)


class _RaisingOut(io.StringIO):
    """An io_out whose first write raises, so the turn unwinds mid-stream."""

    def __init__(self, make_exc):
        super().__init__()
        self._make_exc = make_exc

    def write(self, text):
        raise self._make_exc()


class _SlowOut(io.StringIO):
    """An io_out whose first write stalls, so the controller has already
    exited by the time the turn reads EOF."""

    def __init__(self, delay):
        super().__init__()
        self._delay = delay

    def write(self, text):
        if self._delay:
            time.sleep(self._delay)
            self._delay = 0
        return super().write(text)


class _OwnedFakeTestBase(unittest.TestCase):
    """Shared scaffolding: a temp dir, fake argv, and per-pid reaping."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._tracked = set()
        self._pidfiles = 0

    def _new_pidfile(self):
        self._pidfiles += 1
        return os.path.join(self.tmp, "fake-%d.pids" % self._pidfiles)

    def _fake_argv(self, spec):
        return [sys.executable, "-c", _FAKE_CONTROLLER, json.dumps(spec)]

    def _spec(self, **spec):
        spec.setdefault("pidfile", self._new_pidfile())
        return spec

    def _track(self, pid):
        if isinstance(pid, int) and pid not in self._tracked:
            self._tracked.add(pid)
            self.addCleanup(_reap, pid)

    def _pids(self, spec, timeout=10.0):
        """Wait for the fake to record its leader pid; track every pid."""
        deadline = time.monotonic() + timeout
        while True:
            pids = {}
            try:
                with open(spec["pidfile"]) as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) == 2:
                            pids[parts[0]] = int(parts[1])
            except OSError:
                pass
            if "leader" in pids:
                for pid in pids.values():
                    self._track(pid)
                return pids
            if time.monotonic() >= deadline:
                self.fail("the fake never recorded its pid")
            time.sleep(0.02)

    def _assert_gone(self, pid, bound=0.5):
        self.assertTrue(_wait_gone(pid, bound + _SLACK),
                        "pid %d is still alive" % pid)

    def _claude(self, spec, bounds=None, **kw):
        kw.setdefault("io_out", io.StringIO())
        with mock.patch.object(bridge, "build_claude_command",
                               return_value=self._fake_argv(spec)):
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     session_id="S1", **kw)
        self._track(s.proc.pid)
        s._cleanup_bounds = dict(bounds or _FAST_BOUNDS)
        return s

    def _codex(self, io_out=None, bounds=None, **kw):
        s = bridge.CodexSession("implement", True,
                                io_out=io_out or io.StringIO(), **kw)
        s._cleanup_bounds = dict(bounds or _FAST_BOUNDS)
        return s

    def _opencode(self, io_out=None, bounds=None, **kw):
        rp = os.path.join(self.tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        s = bridge.OpencodeSession(rp, "implement", True,
                                   io_out=io_out or io.StringIO(),
                                   agent_base_dir=self.tmp, **kw)
        s._cleanup_bounds = dict(bounds or _FAST_BOUNDS)
        return s


class ControllerCleanupHelperTests(_OwnedFakeTestBase):
    """`_close_owned_process` outcome/confirmed semantics, including the
    degraded leader-only path for pid-less test doubles."""

    def test_already_exited_leader_sends_no_signal(self):
        spec = self._spec(after="exit")
        proc = subprocess.Popen(self._fake_argv(spec),
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self._track(proc.pid)
        pgid = bridge._spawn_pgid(proc)
        proc.wait(timeout=10)
        evidence = bridge._close_owned_process(
            proc, pgid, "natural_exit", _FAST_BOUNDS, "codex", "turn_end")
        self.assertEqual(evidence["outcome"], "already_exited")
        self.assertTrue(evidence["confirmed"])
        self.assertFalse(evidence["term_sent"])
        self.assertFalse(evidence["kill_sent"])
        self.assertEqual(evidence["controller"], "codex")
        self.assertEqual(evidence["trigger"], "turn_end")

    def test_pidless_double_that_exits_is_graceful_without_terminate(self):
        proc = ScriptedProc([])
        evidence = bridge._close_owned_process(
            proc, bridge._spawn_pgid(proc), "natural_exit", _FAST_BOUNDS)
        self.assertEqual(evidence["outcome"], "graceful")
        self.assertTrue(evidence["confirmed"])
        self.assertFalse(evidence["group_verified"])
        self.assertFalse(proc.terminated)
        self.assertFalse(proc.killed)

    def test_pidless_hanging_double_escalates_within_bounds(self):
        proc = HangingProc(terminate_is_effective=False)
        bounds = {"term_grace": 0.2, "kill_confirm": 0.2}
        started = time.monotonic()
        evidence = bridge._close_owned_process(proc, None, "sigterm", bounds)
        elapsed = time.monotonic() - started
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertEqual(evidence["outcome"], "killed")
        self.assertTrue(evidence["confirmed"])
        self.assertFalse(evidence["group_verified"])
        # The TERM grace was really waited (no busy-spin through it), and
        # the whole escalation stayed inside its bounds.
        self.assertGreaterEqual(elapsed, 0.19)
        self.assertLess(elapsed, 0.2 + 0.2 + 0.5)

    def test_terminate_raising_never_raises(self):
        class Broken:
            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

            def terminate(self):
                raise OSError("terminate failed")

            def kill(self):
                raise OSError("kill failed")

        evidence = bridge._close_owned_process(
            Broken(), None, "sigterm", {"term_grace": 0.1,
                                        "kill_confirm": 0.1})
        self.assertEqual(evidence["outcome"], "failed")
        self.assertFalse(evidence["confirmed"])

    def test_unkillable_double_is_reported_failed(self):
        class Unkillable(HangingProc):
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

        proc = Unkillable(terminate_is_effective=False)
        evidence = bridge._close_owned_process(
            proc, None, "stdin_eof", {"stdin_eof": 0.1, "term_grace": 0.1,
                                      "kill_confirm": 0.1})
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertEqual(evidence["outcome"], "failed")
        self.assertFalse(evidence["confirmed"])
        self.assertTrue(evidence["term_sent"])
        self.assertTrue(evidence["kill_sent"])

    def test_spawn_pgid_is_none_without_an_owned_group(self):
        for pid in (None, mock.MagicMock(), True, False, 0, -1, "12"):
            with self.subTest(pid=pid):
                self.assertIsNone(bridge._spawn_pgid(
                    types.SimpleNamespace(pid=pid)))
        self.assertIsNone(bridge._spawn_pgid(object()))
        # This runner's own pid is never an owned controller group.
        self.assertIsNone(bridge._spawn_pgid(
            types.SimpleNamespace(pid=os.getpid())))


class ClaudeCloseCleanupMatrixTests(_OwnedFakeTestBase):
    """ClaudeSession.close(): stdin EOF, bounded wait, TERM, KILL, confirm."""

    def test_already_exited(self):
        spec = self._spec(after="exit")
        s = self._claude(spec)
        self._pids(spec)
        deadline = time.monotonic() + 10
        while s.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        s.close()
        self.assertEqual(s.last_cleanup["outcome"], "already_exited")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertFalse(s.last_cleanup["term_sent"])

    def test_prompt_exit_on_stdin_eof(self):
        spec = self._spec(after="stdin")
        s = self._claude(spec)
        leader = self._pids(spec)["leader"]
        s.close()
        self.assertEqual(s.last_cleanup["outcome"], "graceful")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertTrue(s.last_cleanup["group_verified"])
        self.assertFalse(s.last_cleanup["term_sent"])
        self.assertEqual(s.last_cleanup["trigger"], "close")
        self._assert_gone(leader)

    def test_delayed_exit_within_graceful(self):
        spec = self._spec(after="stdin", eof_delay=0.5)
        s = self._claude(spec, bounds=dict(_FAST_BOUNDS, stdin_eof=3.0))
        leader = self._pids(spec)["leader"]
        s.close()
        self.assertEqual(s.last_cleanup["outcome"], "graceful")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertFalse(s.last_cleanup["term_sent"])
        self.assertGreaterEqual(s.last_cleanup["duration_ms"], 400)
        self._assert_gone(leader)

    def test_eof_ignored_then_term(self):
        spec = self._spec(after="stdin", on_eof="sleep")
        s = self._claude(spec)
        leader = self._pids(spec)["leader"]
        s.close()
        self.assertEqual(s.last_cleanup["outcome"], "terminated")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertTrue(s.last_cleanup["term_sent"])
        self.assertFalse(s.last_cleanup["kill_sent"])
        self._assert_gone(leader)

    def test_term_ignored_then_kill(self):
        spec = self._spec(after="stdin", on_eof="sleep", term="ignore")
        s = self._claude(spec)
        leader = self._pids(spec)["leader"]
        s.close()
        self.assertEqual(s.last_cleanup["outcome"], "killed")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertTrue(s.last_cleanup["kill_sent"])
        self._assert_gone(leader)

    def test_owned_grandchild_outlives_parent(self):
        spec = self._spec(after="stdin", grandchild=True)
        s = self._claude(spec)
        pids = self._pids(spec)
        s.close()
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertIn(s.last_cleanup["outcome"], ("terminated", "killed"))
        self._assert_gone(pids["leader"])
        self._assert_gone(pids["child"])


class _RunCleanupMatrixMixin(object):
    """Codex/OpenCode `_run` rows, shared by both per-turn controllers."""

    MESSAGE = None

    def _session(self, io_out=None, bounds=None):
        raise NotImplementedError

    def _drive(self, spec, io_out=None, bounds=None):
        s = self._session(io_out=io_out, bounds=bounds)
        try:
            events = s._run(self._fake_argv(spec))
        finally:
            self._pids(spec)
        return s, events

    def test_already_exited_at_eof(self):
        spec = self._spec(lines=[self.MESSAGE], after="exit")
        s, events = self._drive(spec, io_out=_SlowOut(0.5))
        self.assertEqual(len(events), 1)
        self.assertEqual(s.last_cleanup["outcome"], "already_exited")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertFalse(s.last_cleanup["term_sent"])
        self.assertEqual(s.last_cleanup["trigger"], "turn_end")
        self._assert_gone(self._pids(spec)["leader"])

    def test_natural_exit_within_bound(self):
        spec = self._spec(lines=[self.MESSAGE],
                          after="close_stdout_then_exit", exit_delay=0.3)
        s, _ = self._drive(spec, bounds=dict(_FAST_BOUNDS, natural_exit=3.0))
        self.assertEqual(s.last_cleanup["outcome"], "graceful")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertTrue(s.last_cleanup["group_verified"])
        self.assertFalse(s.last_cleanup["term_sent"])
        self._assert_gone(self._pids(spec)["leader"])

    def test_abnormal_sigterm_prompt(self):
        spec = self._spec(lines=[self.MESSAGE], after="sleep")
        with self.assertRaises(RuntimeError):
            self._drive(spec, io_out=_RaisingOut(lambda: RuntimeError("x")))
        s = self._last_session
        self.assertEqual(s.last_cleanup["outcome"], "terminated")
        self.assertEqual(s.last_cleanup["trigger"], "abnormal")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertIsNone(bridge.live_child_handle(s))
        self._assert_gone(self._pids(spec)["leader"])

    def test_abnormal_sigterm_delayed(self):
        spec = self._spec(lines=[self.MESSAGE], after="sleep", term="delay",
                          term_delay=0.2)
        with self.assertRaises(RuntimeError):
            self._drive(spec, io_out=_RaisingOut(lambda: RuntimeError("x")),
                        bounds=dict(_FAST_BOUNDS, term_grace=3.0))
        s = self._last_session
        self.assertEqual(s.last_cleanup["outcome"], "terminated")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertFalse(s.last_cleanup["kill_sent"])
        self._assert_gone(self._pids(spec)["leader"])

    def test_abnormal_sigterm_ignored_then_kill(self):
        spec = self._spec(lines=[self.MESSAGE], after="sleep", term="ignore")
        with self.assertRaises(RuntimeError):
            self._drive(spec, io_out=_RaisingOut(lambda: RuntimeError("x")))
        s = self._last_session
        self.assertEqual(s.last_cleanup["outcome"], "killed")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertTrue(s.last_cleanup["kill_sent"])
        self._assert_gone(self._pids(spec)["leader"])

    def test_owned_grandchild_outlives_parent(self):
        spec = self._spec(lines=[self.MESSAGE], after="exit", grandchild=True)
        s, _ = self._drive(spec)
        pids = self._pids(spec)
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertIn(s.last_cleanup["outcome"], ("terminated", "killed"))
        self._assert_gone(pids["leader"])
        self._assert_gone(pids["child"])

    def test_lingering_leader_after_eof(self):
        spec = self._spec(lines=[self.MESSAGE], after="close_stdout_sleep")
        started = time.monotonic()
        s, events = self._drive(spec)
        self.assertLess(time.monotonic() - started, 0.5 * 4 + _SLACK + 5.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(s.last_cleanup["outcome"], "terminated")
        self.assertEqual(s.last_cleanup["trigger"], "turn_end")
        self.assertTrue(s.last_cleanup["confirmed"])
        self._assert_gone(self._pids(spec)["leader"])


class CodexRunCleanupMatrixTests(_RunCleanupMatrixMixin, _OwnedFakeTestBase):
    MESSAGE = _CODEX_MESSAGE

    def _session(self, io_out=None, bounds=None):
        self._last_session = self._codex(io_out=io_out, bounds=bounds)
        return self._last_session


class OpencodeRunCleanupMatrixTests(_RunCleanupMatrixMixin,
                                   _OwnedFakeTestBase):
    MESSAGE = _OPENCODE_MESSAGE

    def _session(self, io_out=None, bounds=None):
        self._last_session = self._opencode(io_out=io_out, bounds=bounds)
        return self._last_session


class ControllerCleanupNegativeTests(_OwnedFakeTestBase):
    """Cleanup never touches a process it does not own, nor Claude's
    persistent between-turn process."""

    def _bystander(self, new_session):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=new_session)
        self._track(proc.pid)
        return proc

    def _exercise_every_cleanup(self):
        spec = self._spec(after="stdin", grandchild=True)
        s = self._claude(spec)
        self._pids(spec)
        s.close()
        self.assertTrue(s.last_cleanup["confirmed"])

        spec = self._spec(lines=[_CODEX_MESSAGE], after="sleep")
        codex = self._codex(io_out=_RaisingOut(lambda: RuntimeError("x")))
        with self.assertRaises(RuntimeError):
            codex._run(self._fake_argv(spec))
        self._pids(spec)
        self.assertTrue(codex.last_cleanup["confirmed"])

        spec = self._spec(lines=[_CODEX_MESSAGE], after="exit",
                          grandchild=True)
        codex = self._codex()
        codex._run(self._fake_argv(spec))
        self._pids(spec)
        self.assertTrue(codex.last_cleanup["confirmed"])

    def test_unrelated_process_untouched(self):
        bystander = self._bystander(new_session=False)
        self._exercise_every_cleanup()
        self.assertIsNone(bystander.poll())
        os.kill(bystander.pid, 0)

    def test_verification_style_new_session_untouched(self):
        # Deferred verification work runs in its own session, never as a
        # controller descendant; controller cleanup must not reach it.
        bystander = self._bystander(new_session=True)
        self._exercise_every_cleanup()
        self.assertIsNone(bystander.poll())
        os.kill(bystander.pid, 0)

    def test_claude_persistent_after_normal_turn_alive(self):
        spec = self._spec(after="stdin", reply=_claude_text_lines("hello"))
        s = self._claude(spec)
        self.addCleanup(s.close)
        leader = self._pids(spec)["leader"]
        s._first_token_deadline_seconds = 10.0
        result = s.send("hi")
        self.assertTrue(result["ok"])
        time.sleep(0.2)
        self.assertIsNone(s.proc.poll())
        os.kill(leader, 0)
        self.assertIsNone(s.last_cleanup)
        self.assertIs(bridge.live_child_handle(s), s.proc)

    def test_claude_persistent_after_no_first_token_alive(self):
        spec = self._spec(after="stdin")
        s = self._claude(spec)
        self.addCleanup(s.close)
        leader = self._pids(spec)["leader"]
        s._first_token_deadline_seconds = 0.3
        result = s.send("hi")
        self.assertEqual(result["result"], "no_first_token")
        time.sleep(0.2)
        self.assertIsNone(s.proc.poll())
        os.kill(leader, 0)
        self.assertIsNone(s.last_cleanup)


class ControllerLifecyclePathTests(_OwnedFakeTestBase):
    """Every lifecycle path that can own a live controller process ends with
    that process confirmed gone (or, for Claude between turns, kept)."""

    def _per_turn(self, controller, io_out=None, bounds=None):
        if controller == "codex":
            return self._codex(io_out=io_out, bounds=bounds)
        return self._opencode(io_out=io_out, bounds=bounds)

    def _message(self, controller):
        return _CODEX_MESSAGE if controller == "codex" else _OPENCODE_MESSAGE

    def _unwind(self, controller, exc_type):
        spec = self._spec(lines=[self._message(controller)], after="sleep")
        s = self._per_turn(controller,
                           io_out=_RaisingOut(lambda: exc_type("x")))
        with self.assertRaises(exc_type):
            s._run(self._fake_argv(spec))
        self.assertEqual(s.last_cleanup["trigger"], "abnormal")
        self.assertTrue(s.last_cleanup["confirmed"])
        self.assertIsNone(bridge.live_child_handle(s))
        self._assert_gone(self._pids(spec)["leader"])

    def test_per_turn_normal_close(self):
        for controller in ("codex", "opencode"):
            with self.subTest(controller=controller):
                spec = self._spec(lines=[self._message(controller)],
                                  after="exit")
                s = self._per_turn(controller)
                events = s._run(self._fake_argv(spec))
                self.assertEqual(len(events), 1)
                self.assertEqual(s.last_cleanup["trigger"], "turn_end")
                self.assertTrue(s.last_cleanup["confirmed"])
                self._assert_gone(self._pids(spec)["leader"])

    def test_per_turn_exception_close(self):
        for controller in ("codex", "opencode"):
            with self.subTest(controller=controller):
                self._unwind(controller, RuntimeError)

    def test_per_turn_systemexit_close(self):
        for controller in ("codex", "opencode"):
            with self.subTest(controller=controller):
                self._unwind(controller, SystemExit)

    def test_per_turn_keyboard_interrupt(self):
        for controller in ("codex", "opencode"):
            with self.subTest(controller=controller):
                self._unwind(controller, KeyboardInterrupt)

    def test_per_turn_controller_error(self):
        error_lines = {
            "codex": [json.dumps({"type": "thread.started",
                                  "thread_id": "T1"}),
                      json.dumps({"type": "error", "message": "boom"})],
            "opencode": [json.dumps({
                "type": "error", "sessionID": "ses_E",
                "error": {"name": "APIError",
                          "data": {"message": "boom"}}})],
        }
        builders = {"codex": "build_codex_command",
                    "opencode": "build_opencode_command"}
        for controller in ("codex", "opencode"):
            with self.subTest(controller=controller):
                spec = self._spec(lines=error_lines[controller],
                                  after="close_stdout_then_exit",
                                  exit_delay=0.3)
                s = self._per_turn(
                    controller, bounds=dict(_FAST_BOUNDS, natural_exit=3.0))
                with mock.patch.object(bridge, builders[controller],
                                       return_value=self._fake_argv(spec)):
                    result = s.send("go")
                self.assertFalse(result["ok"])
                self.assertEqual(result["result"], "error")
                self.assertEqual(s.last_cleanup["outcome"], "graceful")
                self.assertTrue(s.last_cleanup["confirmed"])
                self._assert_gone(self._pids(spec)["leader"])

    def test_per_turn_no_first_token(self):
        for controller in ("codex", "opencode"):
            with self.subTest(controller=controller):
                spec = self._spec(after="sleep")
                s = self._per_turn(controller)
                s._first_token_deadline_seconds = 0.3
                s._run(self._fake_argv(spec))
                self.assertTrue(s._last_turn_no_first_token)
                self.assertEqual(s.last_cleanup["outcome"], "terminated")
                self.assertEqual(s.last_cleanup["trigger"], "abnormal")
                self.assertTrue(s.last_cleanup["confirmed"])
                self._assert_gone(self._pids(spec)["leader"])

    def test_claude_normal_close(self):
        spec = self._spec(after="stdin", reply=_claude_text_lines("hello"))
        s = self._claude(spec)
        leader = self._pids(spec)["leader"]
        s._first_token_deadline_seconds = 10.0
        self.assertTrue(s.send("hi")["ok"])
        s.close()
        self.assertEqual(s.last_cleanup["outcome"], "graceful")
        self.assertTrue(s.last_cleanup["confirmed"])
        self._assert_gone(leader)

    def test_claude_controller_error_then_close(self):
        spec = self._spec(after="stdin", reply=[json.dumps(
            {"type": "result", "subtype": "error_during_execution",
             "session_id": "S1"})])
        s = self._claude(spec)
        leader = self._pids(spec)["leader"]
        s._first_token_deadline_seconds = 10.0
        result = s.send("hi")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "error")
        # A controller error is not closure: the process is kept.
        self.assertIsNone(s.proc.poll())
        s.close()
        self.assertTrue(s.last_cleanup["confirmed"])
        self._assert_gone(leader)

    def test_claude_close_after_keyboard_interrupt_mid_send(self):
        def interrupting_region(io_out, label):
            raise KeyboardInterrupt()

        spec = self._spec(after="stdin", reply=_claude_text_lines("hello"))
        s = self._claude(spec, region_factory=interrupting_region)
        leader = self._pids(spec)["leader"]
        s._first_token_deadline_seconds = 10.0
        with self.assertRaises(KeyboardInterrupt):
            s.send("hi")
        s.close()
        self.assertTrue(s.last_cleanup["confirmed"])
        self._assert_gone(leader)

    def test_probe_exception_mid_read_cleans_up(self):
        spec = self._spec(lines=["{}"], after="sleep")
        with mock.patch.dict(bridge.CONTROLLER_CLEANUP_BOUNDS, _FAST_BOUNDS), \
                mock.patch.object(bridge, "json", _RaisingJson):
            with self.assertRaises(RuntimeError):
                bridge._real_claude_spawn(self._fake_argv(spec), "")
        self._assert_gone(self._pids(spec)["leader"])

    def test_probe_eof_bounded(self):
        spec = self._spec(lines=["{}"], after="close_stdout_sleep")
        started = time.monotonic()
        with mock.patch.dict(bridge.CONTROLLER_CLEANUP_BOUNDS, _FAST_BOUNDS):
            events = bridge._real_claude_spawn(self._fake_argv(spec), "")
        self.assertEqual(events, [{}])
        self.assertLess(time.monotonic() - started, 0.5 * 3 + _SLACK + 5.0)
        self._assert_gone(self._pids(spec)["leader"])


class _RaisingJson(object):
    """Stands in for the bridge's `json` module: parsing always fails."""

    JSONDecodeError = json.JSONDecodeError
    dumps = staticmethod(json.dumps)

    @staticmethod
    def loads(text):
        raise RuntimeError("parse failure")


class GapCharacterizationTests(_OwnedFakeTestBase):
    """Behavioral gap tests that use only base APIs (setting
    `_cleanup_bounds` is ignored on base). Each fails on base 5774020 and
    passes on the candidate; the 7 s bounds cover the candidate's defaults."""

    def test_claude_close_waits_for_graceful_eof(self):
        marker = os.path.join(self.tmp, "clean_exit")
        spec = self._spec(after="stdin", eof_delay=0.3, marker=marker)
        s = self._claude(spec, bounds=dict(_FAST_BOUNDS, stdin_eof=3.0))
        self._pids(spec)
        s.close()
        self.assertTrue(os.path.exists(marker))

    def test_claude_close_grandchild_gone(self):
        spec = self._spec(after="stdin", grandchild=True)
        s = self._claude(spec)
        pids = self._pids(spec)
        s.close()
        self.assertTrue(_wait_gone(pids["child"], 7.0))

    def _codex_unwind(self, session, exc_type):
        spec = self._spec(lines=[_CODEX_MESSAGE if isinstance(
            session, bridge.CodexSession) else _OPENCODE_MESSAGE],
            after="sleep")
        with self.assertRaises(exc_type):
            session._run(self._fake_argv(spec))
        self.assertTrue(_wait_gone(self._pids(spec)["leader"], 7.0))

    def test_codex_run_exception_unwind_no_live_process(self):
        self._codex_unwind(
            self._codex(io_out=_RaisingOut(lambda: RuntimeError("x"))),
            RuntimeError)

    def test_codex_run_systemexit_unwind_no_live_process(self):
        self._codex_unwind(
            self._codex(io_out=_RaisingOut(lambda: SystemExit(143))),
            SystemExit)

    def test_opencode_run_exception_unwind_no_live_process(self):
        self._codex_unwind(
            self._opencode(io_out=_RaisingOut(lambda: RuntimeError("x"))),
            RuntimeError)

    def test_codex_run_lingering_leader_bounded(self):
        spec = self._spec(lines=[_CODEX_MESSAGE], after="close_stdout_sleep")
        s = self._codex()
        worker = threading.Thread(
            target=s._run, args=(self._fake_argv(spec),), daemon=True)
        worker.start()
        self._pids(spec)
        worker.join(timeout=7.0)
        self.assertFalse(worker.is_alive())

    def test_codex_run_grandchild_after_eof_gone(self):
        spec = self._spec(lines=[_CODEX_MESSAGE], after="exit",
                          grandchild=True)
        s = self._codex()
        s._run(self._fake_argv(spec))
        self.assertTrue(_wait_gone(self._pids(spec)["child"], 7.0))

    def test_probe_exception_mid_read_no_live_process(self):
        spec = self._spec(lines=["{}"], after="sleep")
        with mock.patch.object(bridge, "json", _RaisingJson):
            with self.assertRaises(RuntimeError):
                bridge._real_claude_spawn(self._fake_argv(spec), "")
        self.assertTrue(_wait_gone(self._pids(spec)["leader"], 7.0))


class ControllerCleanupEvidenceTests(_OwnedFakeTestBase):
    """Cleanup evidence tells success from failure and never touches the
    turn's own result."""

    def test_success_event_and_attr(self):
        trace = RecordingTrace()
        spec = self._spec(lines=[_CODEX_MESSAGE],
                          after="close_stdout_then_exit", exit_delay=0.3)
        s = self._codex(trace=trace,
                        bounds=dict(_FAST_BOUNDS, natural_exit=3.0))
        s._run(self._fake_argv(spec))
        self._pids(spec)
        events = [f for name, f in trace.events if name == "controller.cleanup"]
        self.assertEqual(len(events), 1)
        fields = dict(events[0])
        self.assertEqual(fields.pop("role"), "scout")
        self.assertEqual(fields, s.last_cleanup)
        self.assertEqual(fields["outcome"], "graceful")
        self.assertIs(fields["confirmed"], True)

    def test_forced_failure_confirmed_false(self):
        trace = RecordingTrace()
        spec = self._spec(lines=[_CODEX_MESSAGE], after="exit")
        s = self._codex(trace=trace)
        with mock.patch.object(bridge, "_pgid_alive", return_value=True):
            s._run(self._fake_argv(spec))
        self._pids(spec)
        self.assertEqual(s.last_cleanup["outcome"], "failed")
        self.assertIs(s.last_cleanup["confirmed"], False)
        events = [f for name, f in trace.events if name == "controller.cleanup"]
        self.assertEqual(events[0]["outcome"], "failed")

    def test_turn_result_identical_with_and_without_failure(self):
        lines = [json.dumps({"type": "thread.started", "thread_id": "T1"}),
                 _CODEX_MESSAGE]
        results = []
        for fault in (False, True):
            spec = self._spec(lines=lines, after="exit")
            s = self._codex()
            with mock.patch.object(bridge, "build_codex_command",
                                   return_value=self._fake_argv(spec)):
                if fault:
                    with mock.patch.object(bridge, "_pgid_alive",
                                           return_value=True):
                        result = s.send("go")
                else:
                    result = s.send("go")
            self._pids(spec)
            results.append((dict(result), s.last_cleanup["confirmed"]))
        (clean, clean_ok), (faulted, faulted_ok) = results
        self.assertTrue(clean_ok)
        self.assertFalse(faulted_ok)
        # The wall-clock duration differs by the cleanup bound; every other
        # field of the primary result is identical.
        clean.pop("duration_ms", None)
        faulted.pop("duration_ms", None)
        self.assertEqual(clean, faulted)
        self.assertTrue(clean["ok"])

    def test_events_identical_with_and_without_failure(self):
        lines = [json.dumps({"type": "thread.started", "thread_id": "T1"}),
                 _CODEX_MESSAGE]
        spec = self._spec(lines=lines, after="exit")
        clean = self._codex()._run(self._fake_argv(spec))
        spec = self._spec(lines=lines, after="exit")
        with mock.patch.object(bridge, "_pgid_alive", return_value=True):
            faulted = self._codex()._run(self._fake_argv(spec))
        self._pids(spec)
        self.assertEqual(clean, faulted)

    def test_claude_close_failure_does_not_raise(self):
        proc = ClaudeFakeProc()  # no wait/terminate/kill at all
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=io.StringIO(), session_id="S1")
        s.close()
        self.assertEqual(s.last_cleanup["outcome"], "failed")
        self.assertIs(s.last_cleanup["confirmed"], False)


_OWNER_PROGRAM = r'''
import io, json, os, sys, threading, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["scripts"])
import cowork_bridge as bridge


def argv(behavior):
    return [sys.executable, "-c", spec["fake"], json.dumps(behavior)]


def wait_leader(path):
    while True:
        try:
            with open(path) as fh:
                if "leader" in fh.read():
                    return
        except OSError:
            pass
        time.sleep(0.02)


# (a) a Claude-shaped persistent process, through the real ClaudeSession.
bridge.build_claude_command = lambda *a, **k: argv(spec["a"])
claude = bridge.ClaudeSession("roles/scout.md", "plan", True,
                              io_out=io.StringIO(), session_id="S1")
wait_leader(spec["a"]["pidfile"])
# (b) a writing and (c) a silent Codex-shaped turn, through CodexSession.send.
for key in ("b", "c"):
    bridge.build_codex_command = (lambda behavior: (
        lambda *a, **k: argv(behavior)))(spec[key])
    session = bridge.CodexSession("implement", True, io_out=io.StringIO())
    session._first_token_deadline_seconds = 600
    threading.Thread(target=session.send, args=("go",), daemon=True).start()
    wait_leader(spec[key]["pidfile"])
sys.stdout.write("READY\n")
sys.stdout.flush()
time.sleep(120)
'''


class OwnerDeathCharacterizationTests(_OwnedFakeTestBase):
    """What happens to controller processes when their owner dies hard.

    Documented residual (PD-10): each controller leads its own group, so a
    silent Codex/OpenCode turn (c) outlives an owner that is SIGKILLed, by
    pid or with its whole group. A Claude process (a) ends on stdin EOF and a
    writing turn (b) ends on a broken pipe."""

    def _run_owner(self, kill):
        specs = {"a": self._spec(after="stdin"),
                 "b": self._spec(after="write_forever"),
                 "c": self._spec(after="sleep")}
        program = dict(specs, scripts=_HERE, fake=_FAKE_CONTROLLER)
        owner = subprocess.Popen(
            [sys.executable, "-c", _OWNER_PROGRAM, json.dumps(program)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self._track(owner.pid)
        self.addCleanup(owner.stdout.close)
        ready = []
        reader = threading.Thread(
            target=lambda: ready.append(owner.stdout.readline()),
            daemon=True)
        reader.start()
        reader.join(timeout=30)
        self.assertEqual(ready and ready[0].strip(), "READY")
        pids = {key: self._pids(spec)["leader"]
                for key, spec in specs.items()}
        kill(owner)
        owner.wait(timeout=10)
        return pids

    def _assert_residual(self, pids):
        self.assertTrue(_wait_gone(pids["a"], 5.0), "a survived its owner")
        self.assertTrue(_wait_gone(pids["b"], 5.0), "b survived its owner")
        # The accepted residual: nothing ends a silent turn in its own group.
        self.assertFalse(_pid_gone(pids["c"]))

    def test_pid_directed_sigkill(self):
        pids = self._run_owner(
            lambda owner: os.kill(owner.pid, signal.SIGKILL))
        self._assert_residual(pids)

    def test_group_directed_sigkill(self):
        pids = self._run_owner(
            lambda owner: os.killpg(owner.pid, signal.SIGKILL))
        self._assert_residual(pids)


if __name__ == "__main__":
    unittest.main()
