#!/usr/bin/env python3
"""Fixture-driven tests for M4 Package C: real-evidence controller-adapter
activity classification, typed OpenCode refusal/error extraction, the
bounded first-token deadline (SIGTERM then SIGKILL/reap) mechanism, truthful
foreground spinner labels, and `live_child_handle` in cowork_bridge.py.

Covers the frozen brief's required gates:

1. `python3 -m unittest scripts.test_cowork_bridge_activity -v` (this file).
2. Runs alongside `scripts.test_cowork_bridge_capacity` with no collisions.
3. `NamedRegionDiffProofTest` -- a mechanical, AST-level diff proof against
   the signed base commit that ONLY the six named method regions in
   cowork_bridge.py changed and every other function/method body (and every
   other file outside the allowlist) is byte-identical to base.
4. `OpencodeRefusalExtractionTest` -- structured-output AND log-tail
   fixtures.
5. `*FirstTokenDeadlineTest` (one per controller) -- deadline/reap/no-orphan
   fixtures.
6. `*ForegroundLabelStateMachineTest` (one per controller) -- using a real
   TTY shape (`FakeTTY`) where the session only constructs a spinner on a
   TTY (ClaudeSession).
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
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock as mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_REPO_ROOT = os.path.dirname(_HERE)

import cowork_activity as activity  # noqa: E402
import cowork_bridge as bridge  # noqa: E402

BASE_SHA = "cdef8067fea3b9b1f4fe1401c9c70ba3082fb9dc"

# The exact six method regions the frozen brief authorizes edits inside.
# Keyed as "ClassName.method_name" (see NamedRegionDiffProofTest for how
# this maps onto the AST scan); ClaudeSession's nested `_feed` is inside
# `_send_turn`'s own source text, so a change to `_feed` alone still shows
# up as a change to `ClaudeSession._send_turn` here.
ALLOWED_CHANGED_REGIONS = frozenset({
    "ClaudeSession._send_turn",
    "CodexSession._run",
    "CodexSession.send",
    "OpencodeSession._run",
    "OpencodeSession.send",
})

ALLOWED_CHANGED_PATHS = frozenset({
    "scripts/cowork_bridge.py",
    "scripts/test_cowork_bridge_activity.py",
})


class FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal, so `ui.is_tty()` returns
    True -- the repo's own established "real TTY shape" test convention
    (mirrors `test_cowork.py`'s identically-named helper); gate 6 requires
    it because `ClaudeSession._send_turn` only constructs a spinner at all
    when `ui.is_tty(self.io_out)` is True."""

    def isatty(self):
        return True


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

class RecSpinner:
    """Records the exact sequence of label changes/stops -- shared shape
    with test_cowork.py's own RecSpinner fixtures."""
    insts = []

    def __init__(self, out, label="working"):
        self.labels = [label]
        self.stop_count = 0
        RecSpinner.insts.append(self)

    def start(self):
        return self

    def __enter__(self):
        return self

    def set_label(self, text):
        self.labels.append(text)

    def stop(self):
        self.stop_count += 1

    def __exit__(self, *exc):
        self.stop()


class CodexForegroundLabelStateMachineTest(unittest.TestCase):
    def test_tool_activity_then_permanent_stop_at_first_real_output(self):
        RecSpinner.insts.clear()
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "item.started",
                       "item": {"type": "command_execution"}}),
            json.dumps({"type": "item.completed",
                       "item": {"type": "command_execution"}}),
            json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "done"}}),
            # Post-first-token tool activity: must NEVER touch the spinner
            # again -- "never invent a post-first-token spinner state".
            json.dumps({"type": "item.started",
                       "item": {"type": "command_execution"}}),
        ]
        proc = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc), \
                mock.patch.object(bridge, "_Spinner", RecSpinner):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            s.send("go")
        spin = RecSpinner.insts[0]
        self.assertEqual(spin.labels, ["scout working",
                                       "scout running a command",
                                       "scout working"])
        self.assertGreaterEqual(spin.stop_count, 1)


class OpencodeForegroundLabelStateMachineTest(unittest.TestCase):
    def _session(self, tmp):
        rp = os.path.join(tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        return bridge.OpencodeSession(rp, "implement", True, agent_base_dir=tmp)

    def test_tool_activity_then_permanent_stop_at_first_real_output(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        RecSpinner.insts.clear()
        lines = [
            json.dumps({"type": "tool", "sessionID": "ses_X",
                       "part": {"tool": "bash"}}),
            json.dumps({"type": "tool_use", "sessionID": "ses_X",
                       "part": {"tool": "bash", "state": {"status": "ok"}}}),
            json.dumps({"type": "text", "sessionID": "ses_X",
                       "part": {"text": "done"}}),
            # Post-first-token tool activity: never touches the spinner again.
            json.dumps({"type": "tool", "sessionID": "ses_X",
                       "part": {"tool": "bash"}}),
        ]
        proc = ScriptedProc(lines)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc), \
                mock.patch.object(bridge, "_Spinner", RecSpinner):
            s = self._session(tmp)
            s.send("go")
        spin = RecSpinner.insts[0]
        # "using bash" (pre-first-token tool activity) then back to
        # "working" once that tool call completes (tool_done, still
        # pre-first-token) -- then PERMANENTLY unchanged once the real
        # message arrives: the post-first-token tool event in the fixture
        # never appends a fourth label.
        self.assertEqual(spin.labels, ["scout working", "scout using bash",
                                       "scout working"])
        self.assertGreaterEqual(spin.stop_count, 1)


class ClaudeForegroundLabelStateMachineTest(unittest.TestCase):
    """Requires a real TTY shape (gate 6): ClaudeSession only constructs a
    spinner at all when `ui.is_tty(self.io_out)` is True."""

    def test_pre_first_token_idle_then_tool_then_permanent_stop(self):
        proc = ClaudeFakeProc()
        events = [
            json.dumps({"type": "stream_event",
                       "event": {"type": "content_block_start",
                                "content_block": {"type": "tool_use",
                                                  "name": "Bash"}}}),
            json.dumps({"type": "user"}),  # tool_result echo -> back to idle
            json.dumps({"type": "assistant",
                       "message": {"content": [
                           {"type": "text", "text": "hello"}]}}),
            # Post-first-token tool activity: the spinner is already gone
            # (a region owns status display now); it must never restart.
            json.dumps({"type": "stream_event",
                       "event": {"type": "content_block_start",
                                "content_block": {"type": "tool_use",
                                                  "name": "Bash"}}}),
            json.dumps({"type": "result", "subtype": "success",
                       "session_id": "S1"}),
        ]
        for line in events:
            proc.stdout.push(line)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc), \
                mock.patch.object(bridge.ui, "Spinner", RecSpinner):
            RecSpinner.insts.clear()
            out = FakeTTY()
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            result = s.send("hi")
        self.assertTrue(result["ok"])
        spin = RecSpinner.insts[0]
        self.assertEqual(spin.labels, ["scout working", "scout using Bash",
                                       "scout working"])
        # The label sequence above is the truthful record: it never grows
        # a fourth "using Bash" entry for the post-first-token tool_use
        # block -- the spinner's OWN stop() may still be called again by
        # the turn's final result-handling (pre-existing, unrelated to
        # this deadline mechanism), so only the label sequence is asserted
        # as "permanent" here, not the raw stop() call count.
        self.assertGreaterEqual(spin.stop_count, 1)

    def test_off_tty_no_spinner_is_ever_constructed(self):
        proc = ClaudeFakeProc()
        for line in _claude_text_lines("hi"):
            proc.stdout.push(line)
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc), \
                mock.patch.object(bridge.ui, "Spinner", RecSpinner):
            RecSpinner.insts.clear()
            out = io.StringIO()  # NOT a TTY
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=out, session_id="S1")
            s.send("hi")
        self.assertEqual(RecSpinner.insts, [])


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
        # close() calls _terminate(), which needs poll/terminate/wait.
        proc.terminated = False

        def poll():
            return 0 if proc.terminated else None

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
        "controller_profiles", "trace_store", "ui", "probe_cache",
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

    def test_new_functions_import_nothing_new(self):
        # The additive grant covers new FUNCTIONS only, never new imports:
        # every new pure function's own instruction stream must reference
        # zero module-level names beyond ordinary builtins/locals (already
        # proven module-by-module by `_FORBIDDEN_GLOBAL_NAMES` above, which
        # includes cowork_state's local alias "state_store" itself). This
        # test additionally confirms the module's TOP-LEVEL import list is
        # unchanged from base -- no new `import` statement was added
        # anywhere in cowork_bridge.py for this package.
        base_source = subprocess.run(
            ["git", "show", "%s:scripts/cowork_bridge.py" % BASE_SHA],
            cwd=_REPO_ROOT, capture_output=True, text=True, check=True).stdout
        module_path = os.path.join(_HERE, "cowork_bridge.py")
        with open(module_path, "r", encoding="utf-8") as fh:
            new_source = fh.read()

        def top_level_imports(source, filename):
            tree = ast.parse(source, filename=filename)
            names = set()
            for node in tree.body:  # top-level only -- a local `import`
                                     # inside a method body doesn't count.
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        names.add(alias.asname or alias.name)
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        names.add(alias.asname or alias.name)
            return names

        old_imports = top_level_imports(base_source, "base:cowork_bridge.py")
        new_imports = top_level_imports(new_source, "cowork_bridge.py")
        self.assertEqual(old_imports, new_imports)

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
# Gate 3: named-method/additive diff proof and exact path allowlist
# ---------------------------------------------------------------------------

def _qualified_defs(source, filename):
    """Map "func_name" (module-level) / "ClassName.method_name" (a class's
    direct-child methods) -> exact source text, for one module's AST. Does
    NOT recurse into nested closures -- a change anywhere inside a method's
    body (including its nested helpers) is attributed to that one method."""
    tree = ast.parse(source, filename=filename)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = ast.get_source_segment(source, node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    out["%s.%s" % (node.name, child.name)] = (
                        ast.get_source_segment(source, child))
    return out


class NamedRegionDiffProofTest(unittest.TestCase):
    """A mechanical, AST-level proof (not a human claim) that:

    (1) every changed repository path is on the frozen brief's narrow
        allowlist, and
    (2) inside cowork_bridge.py, every function/method whose source text
        differs from the signed base commit is one of the six named
        regions -- and no function/method was REMOVED, and every newly
        ADDED name is a plain module-level function (never a new method,
        never a new class).
    """

    @classmethod
    def setUpClass(cls):
        try:
            cls.base_source = subprocess.run(
                ["git", "show", "%s:scripts/cowork_bridge.py" % BASE_SHA],
                cwd=_REPO_ROOT, capture_output=True, text=True,
                check=True).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise unittest.SkipTest(
                "base commit %s unavailable in this checkout: %s"
                % (BASE_SHA, exc))

    def test_only_allowlisted_paths_changed_since_base(self):
        # HERMETIC against an integration worktree that already carries
        # prior packages' commits (post-review fix for C-BLOCK-02): diffing
        # against the raw signed base SHA is only hermetic in a worktree
        # checked out EXACTLY at that commit -- never in the integration
        # worktree this gate is meant to protect, once ANY other package
        # (e.g. Package B's durable-activity-persistence commit) has
        # landed on top of base. Package C never commits anything, so
        # whatever is uncommitted right now -- `git diff HEAD` (tracked,
        # modified) plus untracked files -- IS exactly C's own changeset,
        # regardless of how many already-integrated packages' commits sit
        # between the signed base and the current HEAD. See
        # `test_head_descends_from_the_signed_base` for the separate
        # binding check that HEAD is still base or a real descendant of it.
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "."],
            cwd=_REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=_REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
        untracked = [line[3:] for line in status if line.startswith("?? ")]
        changed = {p.strip() for p in (tracked + untracked) if p.strip()}
        offenders = changed - ALLOWED_CHANGED_PATHS
        self.assertFalse(
            offenders,
            "paths changed outside the frozen brief's allowlist: %s"
            % sorted(offenders))

    def test_head_descends_from_the_signed_base(self):
        # The binding check the HEAD-relative gate above deliberately does
        # NOT perform on its own: HEAD must still BE the signed base, or a
        # real descendant of it (base plus zero or more already-
        # integrated, independently-reviewed packages) -- never a foreign
        # or rewritten history.
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", BASE_SHA, "HEAD"],
            cwd=_REPO_ROOT)
        self.assertEqual(
            result.returncode, 0,
            "HEAD is not a descendant of (or equal to) the signed base %s"
            % BASE_SHA)

    def test_no_function_or_method_was_removed(self):
        with open(os.path.join(_HERE, "cowork_bridge.py"),
                 "r", encoding="utf-8") as fh:
            new_source = fh.read()
        old_defs = _qualified_defs(self.base_source, "base:cowork_bridge.py")
        new_defs = _qualified_defs(new_source, "cowork_bridge.py")
        removed = set(old_defs) - set(new_defs)
        self.assertFalse(removed, "functions/methods removed vs base: %s"
                         % sorted(removed))

    def test_every_added_name_is_a_plain_new_module_level_function(self):
        with open(os.path.join(_HERE, "cowork_bridge.py"),
                 "r", encoding="utf-8") as fh:
            new_source = fh.read()
        old_defs = _qualified_defs(self.base_source, "base:cowork_bridge.py")
        new_defs = _qualified_defs(new_source, "cowork_bridge.py")
        added = set(new_defs) - set(old_defs)
        offenders = {name for name in added if "." in name}
        self.assertFalse(
            offenders,
            "new names include a class/method addition (only new "
            "module-level functions are additive): %s" % sorted(offenders))
        self.assertTrue(added, "expected at least the new M4 Package C "
                              "functions to be additive")

    def test_only_the_six_named_regions_changed(self):
        with open(os.path.join(_HERE, "cowork_bridge.py"),
                 "r", encoding="utf-8") as fh:
            new_source = fh.read()
        old_defs = _qualified_defs(self.base_source, "base:cowork_bridge.py")
        new_defs = _qualified_defs(new_source, "cowork_bridge.py")
        common = set(old_defs) & set(new_defs)
        changed = {name for name in common if old_defs[name] != new_defs[name]}
        offenders = changed - ALLOWED_CHANGED_REGIONS
        self.assertFalse(
            offenders,
            "function/method bodies changed outside the six named "
            "regions: %s" % sorted(offenders))

    def test_base_sha_matches_the_frozen_brief(self):
        self.assertEqual(BASE_SHA, "cdef8067fea3b9b1f4fe1401c9c70ba3082fb9dc")


if __name__ == "__main__":
    unittest.main()
