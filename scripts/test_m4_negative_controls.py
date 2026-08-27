#!/usr/bin/env python3
"""M4 Package F: end-to-end negative-control suite.

Independent, fresh proof -- written without editing or importing any
existing test file's fixtures/doubles -- that every M4 negative control the
frozen brief and the corrected M4 v3 plan's own `required_negative_controls`
list names is refused by the REAL, fully-integrated (Package A-E) production
seams, never merely by an isolated pure function called in a vacuum.

Every test below drives one or more of: `cowork_bridge.classify_claude_
activity`/`classify_codex_activity`/`classify_opencode_activity` (Package C,
real), `cowork_bridge.classify_opencode_refusal` wired live through
`OpencodeSession.send()`'s real structured-output path (Package C), the
real bounded first-token deadline/reap embedded in `ClaudeSession._send_
turn`/`CodexSession.send`/`OpencodeSession.send` (Package C), `cowork_
activity.validate_watchdog_decision`/`project_compact_state` (Package A,
real), `cowork_watchdog.decide`/`process_probe` (Package D, real),
`cowork_state.append_activity_record`/`read_activity_history`/`latest_
activity`/`reread_before_gate`/`write_scheduled_review` (Package B, real),
`cowork.run_flow`/`cowork._role_loop`/`cowork._reconcile_before_
presentation`/`cowork._ACTIVITY_SHUTDOWN_EVENT` (Package D, real), and
`cowork_measure.build_record` (Package D, real) -- never a bypassed or
hand-rolled substitute for any of these.

Self-contained: this file defines its own fixtures/test-doubles rather than
importing any from `test_cowork_bridge_activity.py`, `test_cowork_
activity_contracts.py`, `test_cowork_state_m4.py`, `test_cowork_watchdog.py`,
or `test_cowork_activity_cross_surface.py`, so its proof stands
independently, matching M3 Package G's own self-containment discipline.

Organized by the frozen brief's own eight named items (each also mapped to
the corrected M4 v3 plan's five `required_negative_controls` entries):

    1. No passive event-tail certification
    2. No hard-timeout inference from silence ("silence-only rejection")
    3. No false productive attribution
    4. No stale artifact classification ("stale evidence")
    5. No controller-specific hidden state as the only truth
    6. Malformed/unknown activity evidence
    7. OpenCode refusal -> typed, real controller.error emission
    8. No-first-token deadline/reap (all three controllers)
    9. Late-write reconciliation (issue #5)
    10. Shutdown-event reuse across two `run_flow` calls in one process

Run standalone:

    python3 -m unittest scripts/test_m4_negative_controls.py -v
"""

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
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_activity as activity  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_report as report  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_ui as ui  # noqa: E402
import cowork_watchdog as watchdog  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal -- the repo's own established
    "real TTY shape" convention, reproduced independently here (matches
    `cowork_ui.is_tty`'s own docstring)."""

    def isatty(self):
        return True


class RecordingTrace:
    """Minimal Trace double: records every (event_name, fields) call, never
    imported from any other test file."""

    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))
        return len(self.events)

    def named(self, name):
        return [f for n, f in self.events if n == name]


class ScriptedProc:
    """A fast, one-shot child double: `.stdout` yields exactly `lines` then
    EOFs. Never approaches the first-token deadline."""

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
    """A one-shot child whose `.stdout` never yields a line on its own --
    the shape the bounded first-token deadline exists to catch."""

    class _NeverYields:
        def __iter__(self):
            return self

        def __next__(self):
            time.sleep(5)
            raise StopIteration

    def __init__(self):
        self.stdout = self._NeverYields()
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        if self.terminated:
            return -15
        return None

    def wait(self, timeout=None):
        if self.terminated or self.killed:
            return -15 if not self.killed else -9
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class PushableStream:
    """Thread-safe blocking iterator a test pushes lines into -- the shape a
    real OS pipe presents to a reader thread, in-process and deterministic."""

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
    """A persistent-duplex double with no `.terminate()`/`.kill()` at all --
    deliberate: a Claude first-token deadline must NEVER call either (its
    session-lifetime child is torn down only by `close()`); a fixture
    calling them fails loudly with AttributeError."""

    def __init__(self):
        self.stdout = PushableStream()
        self.stdin = io.StringIO()

    def poll(self):
        return None


class _SessionsRootMixin(object):
    """Isolated COWORK_SESSIONS_ROOT + a fast activity tick interval per
    test -- never the real home directory, never a shared tick cadence."""

    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._tmp = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._tmp
        self._prior_interval = cowork._ACTIVITY_TICK_INTERVAL_SECONDS
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = 0.05
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()

    def tearDown(self):
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = self._prior_interval
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._tmp, ignore_errors=True)


def _activity_record(work_id, activity_class="productive_model_work",
                     source="claude", time_="2026-01-01T00:00:00Z",
                     age_seconds=1.0, provider_health="healthy"):
    return {
        "schema_version": 1, "record": "ActivityRecord", "work_id": work_id,
        "time": time_, "activity_class": activity_class, "source": source,
        "artifact_fingerprint": None, "artifact_delta": [],
        "provider_health": provider_health, "age_seconds": age_seconds,
    }


def _schedule_record(work_id, next_at="2026-01-01T00:05:00Z",
                     interval=300):
    return {
        "schema_version": 1, "record": "ScheduledReviewRecord",
        "work_id": work_id, "next_inspection_at": next_at,
        "interval_seconds": interval, "last_inspection_result_ref": None,
    }


def _watchdog_decision(work_id, verdict="no_action",
                       durable_evidence_ref=None, process_probe_ref=None,
                       time_="2026-01-01T00:00:01Z"):
    return {
        "schema_version": 1, "record": "WatchdogDecision", "work_id": work_id,
        "time": time_, "verdict": verdict,
        "durable_evidence_ref": durable_evidence_ref,
        "process_probe_ref": process_probe_ref,
    }


# =============================================================================
# 1. No passive event-tail certification.
# =============================================================================

class NoPassiveEventTailCertificationTest(unittest.TestCase):
    """A fixture where SOME evidence (a real event) exists but carries no
    actual model output text must never classify as `productive_model_work`
    -- the defining property this negative control exists to defeat: a
    passive event-tail scan (event present => "the role did something
    productive") is exactly what these real classifiers must refuse."""

    def test_claude_text_carrying_event_with_blank_text_is_not_productive(self):
        for kind in ("assistant", "partial", "result"):
            with self.subTest(kind=kind):
                for blank in (None, "", "   ", 42, ["not", "a", "string"]):
                    evidence = {"kind": kind, "text": blank}
                    self.assertEqual(
                        bridge.classify_claude_activity(evidence),
                        "provider_wait",
                        "an event of the right shape but no real output "
                        "text was certified productive")

    def test_codex_and_opencode_message_event_with_blank_text_is_not_productive(self):
        for classify in (bridge.classify_codex_activity,
                         bridge.classify_opencode_activity):
            with self.subTest(classify=classify.__name__):
                self.assertEqual(
                    classify({"kind": "message", "text": ""}),
                    "provider_wait")

    def test_meta_bookkeeping_event_is_never_productive_but_is_not_silence(self):
        # A real, structurally recognized meta/bookkeeping event (session
        # lifecycle, tool-result echo) is genuine evidence the controller is
        # alive -- provider_wait, never the OTHER wrong answer of blank
        # silence either.
        for kind in ("system", "thread_started", "turn_started",
                     "child_usage", "other", "step_finish"):
            with self.subTest(kind=kind):
                result = bridge.classify_claude_activity({"kind": kind})
                self.assertEqual(result, "provider_wait")
                self.assertNotEqual(result, "productive_model_work")

    def test_turn_level_error_flag_is_never_productive_regardless_of_kind(self):
        # is_error=True on a "result" event (a REAL event, carrying REAL
        # text even) must never be certified productive merely because an
        # event with content exists.
        evidence = {"kind": "result", "text": "some partial output",
                   "is_error": True}
        self.assertEqual(bridge.classify_claude_activity(evidence),
                         "process_crash")

    def test_live_production_seam_never_records_productive_for_a_failed_send(self):
        """Drives the REAL `cowork._turn_boundary_activity_evidence` +
        `cowork._emit_activity_record` production seam (never a bypassed
        classifier call): a genuinely FAILED send_result (`ok=False`) --
        which still leaves a full `role.send.end` trace event behind, an
        event tail a passive scan would happily certify from -- is never
        durably recorded as productive."""
        session_uuid = _uuid()
        work_id = _uuid()
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        self.addCleanup(lambda: (
            os.environ.pop("COWORK_SESSIONS_ROOT", None) if prior_root is None
            else os.environ.__setitem__("COWORK_SESSIONS_ROOT", prior_root)))

        class _Session:
            controller = "claude"

        evidence = cowork._turn_boundary_activity_evidence(
            "claude", {"ok": False, "result": "error",
                      "error_type": "overloaded_error"})
        self.assertEqual(evidence, {"kind": "error", "is_error": True})
        record = cowork._emit_activity_record(
            session_uuid, work_id, _Session(), evidence, time.monotonic())
        self.assertIsNotNone(record)
        self.assertEqual(record["activity_class"], "process_crash")
        self.assertNotEqual(record["activity_class"], "productive_model_work")

    def test_denied_send_result_never_records_productive(self):
        session_uuid = _uuid()
        work_id = _uuid()
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        self.addCleanup(lambda: (
            os.environ.pop("COWORK_SESSIONS_ROOT", None) if prior_root is None
            else os.environ.__setitem__("COWORK_SESSIONS_ROOT", prior_root)))

        class _Session:
            controller = "codex"

        evidence = cowork._turn_boundary_activity_evidence(
            "codex", {"ok": False, "denied": True, "result": "denied"})
        self.assertEqual(evidence, {"kind": "denied"})
        record = cowork._emit_activity_record(
            session_uuid, work_id, _Session(), evidence, time.monotonic())
        self.assertIsNotNone(record)
        self.assertEqual(record["activity_class"], "policy_denial")


# =============================================================================
# 2. No hard-timeout inference from silence ("silence-only rejection").
# =============================================================================

class NoHardTimeoutFromSilenceTest(unittest.TestCase):
    """Package A's dual-evidence law, and Package D's `watchdog.decide`,
    both refuse to certify a terminal verdict from durable silence alone --
    two independently-tested rejection paths, never folded into one."""

    def test_durable_evidence_alone_rejected_at_schema_boundary(self):
        with self.assertRaises(ValueError):
            activity.validate_watchdog_decision(_watchdog_decision(
                _uuid(), verdict="hard_stall_eligible",
                durable_evidence_ref="activity:x@t", process_probe_ref=None))

    def test_process_probe_alone_rejected_at_schema_boundary(self):
        with self.assertRaises(ValueError):
            activity.validate_watchdog_decision(_watchdog_decision(
                _uuid(), verdict="hard_stall_eligible",
                durable_evidence_ref=None, process_probe_ref="pid:123"))

    def test_neither_rejected_at_schema_boundary(self):
        with self.assertRaises(ValueError):
            activity.validate_watchdog_decision(_watchdog_decision(
                _uuid(), verdict="soft_warning",
                durable_evidence_ref=None, process_probe_ref=None))

    def test_both_present_accepted_positive_control(self):
        record = activity.validate_watchdog_decision(_watchdog_decision(
            _uuid(), verdict="soft_warning",
            durable_evidence_ref="activity:x@t", process_probe_ref="pid:1"))
        self.assertEqual(record["verdict"], "soft_warning")

    def test_project_compact_state_reapplies_dual_evidence_law(self):
        """`project_compact_state` fully revalidates `health_record`
        through the exact validator -- a raw dict with the right KEYS but a
        one-legged terminal verdict is rejected at the projection boundary
        itself, not passed through verbatim."""
        work_id = _uuid()
        with self.assertRaises(ValueError):
            activity.project_compact_state(
                _activity_record(work_id, activity_class="no_evidence_silence"),
                _watchdog_decision(work_id, verdict="hard_stall_eligible",
                                   durable_evidence_ref="activity:x@t",
                                   process_probe_ref=None),
                _schedule_record(work_id))

    def test_decide_never_returns_terminal_while_probe_is_alive_no_matter_how_stale(self):
        """Package D's real `watchdog.decide`: an ALIVE probe always wins,
        even against a `no_evidence_silence` classification and a
        wildly-overdue scheduled review -- silence alone (elapsed time)
        never manufactures a hard-timeout while the controller is
        genuinely alive."""
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="no_evidence_silence",
            time_="2020-01-01T00:00:00Z", age_seconds=999999.0)
        schedule_record = _schedule_record(
            work_id, next_at="2020-01-01T00:05:00Z")  # ancient, overdue

        class _AliveProc:
            pid = 4242

            def poll(self):
                return None  # still alive

        class _AliveSession:
            controller = "claude"
            proc = _AliveProc()

        decision = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            schedule_record, session=_AliveSession())
        self.assertEqual(decision["verdict"], "no_action")

    def test_decide_never_certifies_hung_descendant_from_classification_alone(self):
        """Package C's own `hung_descendant` classification, with NO
        independent `ps` corroboration, degrades to `soft_warning` --
        never a silent promotion to `hard_stall_eligible` from one source
        of evidence alone."""
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="hung_descendant")

        decision = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            _schedule_record(work_id), session=None, hung_ps_evidence=None)
        self.assertEqual(decision["verdict"], "soft_warning")

    def test_decide_escalates_hung_descendant_only_with_independent_ps_evidence(self):
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="hung_descendant")
        decision = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            _schedule_record(work_id), session=None,
            hung_ps_evidence="ps:pid=1,ppid=1,stat=Z")
        self.assertEqual(decision["verdict"], "hard_stall_eligible")
        self.assertIn("ps:pid=1", decision["process_probe_ref"])


# =============================================================================
# 3. No false productive attribution.
# =============================================================================

class NoFalseProductiveAttributionTest(unittest.TestCase):

    def test_provider_wait_alone_never_projects_as_productive(self):
        work_id = _uuid()
        compact = activity.project_compact_state(
            _activity_record(work_id, activity_class="provider_wait"),
            _watchdog_decision(work_id), _schedule_record(work_id))
        self.assertEqual(compact["activity_class"], "provider_wait")
        self.assertNotEqual(compact["activity_class"], "productive_model_work")

    def test_reconciliation_required_to_ever_change_effective_class(self):
        """Live, through the real Package B store: an appended
        `provider_wait` ActivityRecord stays `provider_wait` in `latest_
        activity` until an EXPLICIT `reread_before_gate` reconciliation
        durably supersedes it -- never fabricated upward on its own."""
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        self.addCleanup(lambda: (
            os.environ.pop("COWORK_SESSIONS_ROOT", None) if prior_root is None
            else os.environ.__setitem__("COWORK_SESSIONS_ROOT", prior_root)))

        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(work_id,
                                           activity_class="provider_wait"))
        current = state_store.latest_activity(session_uuid, work_id)
        self.assertEqual(current["effective_classification"], "provider_wait")

        # A gate reread with an IDENTICAL fresh classification writes
        # nothing -- no phantom upgrade path exists.
        noop = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:01:00Z", "provider_wait",
            "a" * 64, "poll")
        self.assertIsNone(noop)
        self.assertEqual(
            state_store.latest_activity(session_uuid, work_id)
            ["effective_classification"], "provider_wait")

        # Only an EXPLICIT reconciliation, naming both the original AND the
        # reconciled classification, can ever change it.
        reconciled = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:02:00Z",
            "productive_model_work", "b" * 64, "poll")
        self.assertIsNotNone(reconciled)
        self.assertEqual(reconciled["original_classification"], "provider_wait")
        self.assertEqual(reconciled["reconciled_classification"],
                         "productive_model_work")
        current = state_store.latest_activity(session_uuid, work_id)
        self.assertEqual(current["effective_classification"],
                         "productive_model_work")

    def test_measurement_record_never_upgrades_provider_wait_to_productive(self):
        """`cowork_measure.build_record`'s unconditional `activity` field
        carries the durable effective classification through VERBATIM --
        it is never a second opportunity to reclassify upward."""
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        self.addCleanup(lambda: (
            os.environ.pop("COWORK_SESSIONS_ROOT", None) if prior_root is None
            else os.environ.__setitem__("COWORK_SESSIONS_ROOT", prior_root)))
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(work_id,
                                           activity_class="provider_wait"))
        record = measure.build_record(session_uuid)
        self.assertEqual(record["activity"]["activity_class"], "provider_wait")


# =============================================================================
# 4. No stale artifact classification ("stale evidence").
# =============================================================================

class NoStaleArtifactClassificationTest(unittest.TestCase):

    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._root = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._root

    def tearDown(self):
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._root, ignore_errors=True)

    def test_torn_activity_journal_tail_refused_never_silently_reported_latest(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(work_id))
        path = state_store.activity_history_path_for(session_uuid, work_id)
        with open(path, "ab") as fh:
            fh.write(b'{"schema_version": 1, "record": "ActivityRecord",'
                     b' "work_id": "' + work_id.encode() +
                     b'", "time": "2026-01-01T00:01:00Z", "activity_cla')
            # deliberately truncated -- no trailing newline, unparseable.
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.read_activity_history(session_uuid, work_id)
        with self.assertRaises(state_store.CorruptRecordError):
            state_store.latest_activity(session_uuid, work_id)

        # RESUME: a fresh, real append transparently repairs the torn tail
        # (append_jsonl_atomic's own repair-before-append) -- this is a
        # WRITE-time repair, never a read-time silent skip: the read
        # surfaces above must keep refusing until a genuine new write
        # actually lands.
        stored = state_store.append_activity_record(
            session_uuid,
            _activity_record(work_id, activity_class="local_tool_work",
                             time_="2026-01-01T00:02:00Z"))
        self.assertEqual(stored["activity_class"], "local_tool_work")
        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(history), 2)  # torn fragment discarded, not kept
        self.assertEqual(history[-1]["activity_class"], "local_tool_work")

    def test_stale_overdue_scheduled_review_never_silently_treated_as_reviewed(self):
        schedule = _schedule_record(_uuid(), next_at="2020-01-01T00:00:00Z")
        self.assertTrue(
            watchdog.review_due(schedule, "2026-01-01T00:00:00Z"),
            "an overdue schedule must explicitly report due=True")
        self.assertIsNone(
            watchdog.review_due(None, "2026-01-01T00:00:00Z"),
            "no schedule at all is genuinely UNKNOWN, never silently 'not due'")

    def test_identical_fresh_reread_writes_no_phantom_reconciliation_record(self):
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(work_id,
                                           activity_class="local_tool_work"))
        result = state_store.reread_before_gate(
            session_uuid, work_id, "2026-01-01T00:01:00Z", "local_tool_work",
            "a" * 64, "poll")
        self.assertIsNone(result)
        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(len(history), 1,
                         "an identical re-read must never append a phantom "
                         "reconciliation record")

    def test_ensure_scheduled_review_never_extends_an_already_terminal_schedule(self):
        """M4D-MAJ-02's own invariant, re-derived independently: once a
        prior schedule exists, a TERMINAL activity class must never push
        `next_inspection_at` further into the future -- that would make an
        already-overdue review structurally unreachable."""
        session_uuid = _uuid()
        work_id = _uuid()
        prior = state_store.write_scheduled_review(
            session_uuid, _schedule_record(
                work_id, next_at="2026-01-01T00:00:00Z"))
        refreshed = cowork._ensure_scheduled_review(
            session_uuid, work_id, "2026-01-01T00:10:00Z",
            activity_class="process_crash")
        self.assertEqual(refreshed["next_inspection_at"],
                         prior["next_inspection_at"],
                         "a terminal classification silently pushed the "
                         "next review further into the future")


# =============================================================================
# 5. No controller-specific hidden state as the only truth.
# =============================================================================

class NoControllerSpecificHiddenStateTest(unittest.TestCase):
    """`live_child_handle` (Package C) must be genuinely truthful and
    uniform for Claude, Codex, AND OpenCode -- never a Claude-only signal
    silently standing in for "process health" everywhere else."""

    def test_null_before_any_spawn_for_a_bare_session_shape(self):
        class _NeverSpawned:
            controller = "codex"
        self.assertIsNone(bridge.live_child_handle(_NeverSpawned()))

    def test_codex_and_opencode_handle_alive_during_run_null_after_reap(self):
        for controller_attr, session_cls in (
                ("codex", bridge.CodexSession),
                ("opencode", None)):
            with self.subTest(controller=controller_attr):
                lines = [json.dumps({"type": "thread.started",
                                     "thread_id": "T1"}),
                        json.dumps({"type": "item.completed",
                                   "item": {"type": "agent_message",
                                            "text": "hi"}})]
                proc = ScriptedProc(lines)
                with mock.patch.object(bridge.subprocess, "Popen",
                                       return_value=proc):
                    if controller_attr == "codex":
                        session = bridge.CodexSession(
                            "implement", True, io_out=io.StringIO())
                    else:
                        tmp = tempfile.mkdtemp()
                        self.addCleanup(
                            lambda: shutil.rmtree(tmp, ignore_errors=True))
                        rp = os.path.join(tmp, "role.md")
                        with open(rp, "w") as fh:
                            fh.write("ROLE")
                        session = bridge.OpencodeSession(
                            rp, "implement", True, agent_base_dir=tmp)
                    # No live child before the FIRST send.
                    self.assertIsNone(bridge.live_child_handle(session))
                    session.send("go")
                # After a clean, fully-reaped turn: null again -- never a
                # stale handle mistaken for continued liveness.
                self.assertIsNone(bridge.live_child_handle(session))

    def test_claude_handle_stays_non_null_across_inter_turn_interval(self):
        """Claude's is the ONE genuinely session-lifetime handle: it must
        stay non-null BETWEEN turns on the same open session -- unlike
        Codex/OpenCode's per-turn handle, which is truthfully null between
        turns. A watchdog probe that only ever checks Codex/OpenCode-shaped
        semantics would wrongly read a quiet-but-alive Claude session as
        dead between turns."""
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            session = bridge.ClaudeSession(
                "roles/scout.md", "plan", True, io_out=io.StringIO(),
                session_id="S1")
        self.assertIsNotNone(bridge.live_child_handle(session))
        # No turn is in flight right now (inter-turn interval) -- still
        # alive, because Claude's handle is session-lifetime, not turn-scoped.
        self.assertIsNotNone(bridge.live_child_handle(session))
        self.assertIs(bridge.live_child_handle(session), proc)

    def test_watchdog_process_probe_uses_the_SAME_accessor_for_every_controller(self):
        """Package D's `process_probe` never special-cases by controller
        name -- it is a thin, uniform wrapper over `live_child_handle`
        alone, so Codex/OpenCode are probed exactly as truthfully as
        Claude."""
        class _DeadCodex:
            controller = "codex"
            _live_proc = None

        class _DeadOpencode:
            controller = "opencode"
            _live_proc = None

        class _DeadClaude:
            controller = "claude"
            proc = None

        for session in (_DeadCodex(), _DeadOpencode(), _DeadClaude()):
            alive, ref = watchdog.process_probe(session)
            self.assertFalse(alive)
            self.assertEqual(ref, "dead:no_live_child")


# =============================================================================
# 6. Malformed / unknown activity evidence.
# =============================================================================

class MalformedUnknownActivityTest(unittest.TestCase):

    CLASSIFIERS = (bridge.classify_claude_activity,
                   bridge.classify_codex_activity,
                   bridge.classify_opencode_activity)

    def test_non_dict_evidence_is_no_evidence_silence_never_raises(self):
        for classify in self.CLASSIFIERS:
            for bad in (None, "a string", 42, [1, 2, 3], object()):
                with self.subTest(classify=classify.__name__, bad=bad):
                    self.assertEqual(classify(bad), "no_evidence_silence")

    def test_missing_or_empty_kind_is_no_evidence_silence(self):
        for classify in self.CLASSIFIERS:
            for bad_kind in (None, "", 42, [], {}):
                with self.subTest(classify=classify.__name__,
                                  bad_kind=bad_kind):
                    self.assertEqual(
                        classify({"kind": bad_kind}), "no_evidence_silence")

    def test_unrecognized_kind_degrades_to_provider_wait_never_raises(self):
        for classify in self.CLASSIFIERS:
            with self.subTest(classify=classify.__name__):
                self.assertEqual(
                    classify({"kind": "an_entirely_unknown_future_event"}),
                    "provider_wait")

    def test_validate_activity_record_rejects_unknown_activity_class(self):
        work_id = _uuid()
        bad = _activity_record(work_id, activity_class="not_a_real_class")
        with self.assertRaises(ValueError):
            activity.validate_activity_record(bad)

    def test_validate_activity_record_rejects_unknown_top_level_key(self):
        work_id = _uuid()
        bad = _activity_record(work_id)
        bad["totally_unexpected_extra_field"] = True
        with self.assertRaises(ValueError):
            activity.validate_activity_record(bad)

    def test_live_production_seam_swallows_a_malformed_send_result_never_raises(self):
        """`cowork._turn_boundary_activity_evidence` -> `_emit_activity_
        record`, driven with a send_result carrying no recognizable shape
        at all -- degrades honestly, never crashes the turn it observes."""
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        self.addCleanup(lambda: (
            os.environ.pop("COWORK_SESSIONS_ROOT", None) if prior_root is None
            else os.environ.__setitem__("COWORK_SESSIONS_ROOT", prior_root)))

        class _Session:
            controller = "claude"

        evidence = cowork._turn_boundary_activity_evidence(
            "claude", "not-even-a-dict")
        self.assertEqual(evidence, {"kind": None})
        record = cowork._emit_activity_record(
            _uuid(), _uuid(), _Session(), evidence, time.monotonic())
        self.assertIsNotNone(record)
        self.assertEqual(record["activity_class"], "no_evidence_silence")


# =============================================================================
# 7. OpenCode refusal -> typed, REAL controller.error emission.
# =============================================================================

class OpencodeRefusalRealEmissionTest(unittest.TestCase):
    """Issue #41 criterion 1, driven through the real `OpencodeSession.
    send()` structured-output path -- never `classify_opencode_refusal`
    called directly in isolation."""

    def _session(self, tmp, trace=None):
        rp = os.path.join(tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        return bridge.OpencodeSession(rp, "implement", True,
                                      agent_base_dir=tmp, trace=trace)

    def test_quota_refusal_emits_real_typed_controller_error_event(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({
            "type": "error", "sessionID": "ses_Q",
            "error": {"name": "quota_limited",
                     "data": {"message": "quota exceeded, upgrade required"}}})]
        proc = ScriptedProc(lines)
        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            session = self._session(tmp, trace=trace)
            result = session.send("go")
        self.assertFalse(result["ok"])
        errors = trace.named("controller.error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["outcome"], "refused")
        self.assertEqual(errors[0]["failure_class"], "quota")
        self.assertEqual(errors[0]["controller"], "opencode")

    def test_balance_depletion_refusal_typed_correctly_never_generic(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({
            "type": "error", "sessionID": "ses_B",
            "error": {"name": "insufficient_balance",
                     "data": {"message": "insufficient balance"}}})]
        proc = ScriptedProc(lines)
        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            session = self._session(tmp, trace=trace)
            session.send("go")
        errors = trace.named("controller.error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["failure_class"], "balance")

    def test_unrecognized_error_token_is_typed_unknown_never_auto_retried(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({
            "type": "error", "sessionID": "ses_U",
            "error": {"name": "a_token_nobody_recognizes",
                     "data": {"message": "something unrecognized happened"}}})]
        proc = ScriptedProc(lines)
        popen_calls = []

        def fake_popen(command, **kwargs):
            popen_calls.append(command)
            return proc

        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen",
                               side_effect=fake_popen):
            session = self._session(tmp, trace=trace)
            session.send("go")
        errors = trace.named("controller.error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["failure_class"], "unknown_provider_failure")
        # No auto-retry spawn for an unrecognized token.
        self.assertEqual(len(popen_calls), 1)

    def test_successful_turn_emits_no_controller_error_at_all(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        lines = [json.dumps({"type": "text", "sessionID": "ses_OK",
                             "part": {"text": "all good"}})]
        proc = ScriptedProc(lines)
        trace = RecordingTrace()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            session = self._session(tmp, trace=trace)
            result = session.send("go")
        self.assertTrue(result["ok"])
        self.assertEqual(trace.named("controller.error"), [])


# =============================================================================
# 8. No-first-token deadline/reap -- all three controllers, real seam.
# =============================================================================

class NoFirstTokenDeadlineReapTest(unittest.TestCase):

    def test_codex_hang_is_reaped_typed_and_leaves_no_orphan(self):
        hp = HangingProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            session = bridge.CodexSession(
                "implement", True, io_out=io.StringIO())
            session._first_token_deadline_seconds = 0.05
            result = session.send("go")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "no_first_token")
        self.assertEqual(
            result["controller_turn_outcome"],
            bridge.controller_turn_outcome_no_first_token())
        activity.validate_controller_turn_outcome(
            result["controller_turn_outcome"])
        self.assertTrue(hp.terminated)
        self.assertIsNone(bridge.live_child_handle(session),
                          "the reaped child must never remain an orphan")
        self.assertEqual(
            bridge.classify_codex_activity({"kind": "no_first_token"}),
            "hung_descendant")

    def test_opencode_hang_is_reaped_typed_and_leaves_no_orphan(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        rp = os.path.join(tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        hp = HangingProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=hp):
            session = bridge.OpencodeSession(
                rp, "implement", True, agent_base_dir=tmp)
            session._first_token_deadline_seconds = 0.05
            result = session.send("go")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "no_first_token")
        self.assertTrue(hp.terminated)
        self.assertIsNone(bridge.live_child_handle(session))
        self.assertEqual(
            bridge.classify_opencode_activity({"kind": "no_first_token"}),
            "hung_descendant")

    def test_claude_hang_yields_typed_outcome_without_tearing_down_session(self):
        """Claude's own bounded first-token deadline expiry must NEVER
        terminate the session-lifetime child -- only `close()` does; the
        deadline expiry is a typed, evidenced TURN outcome, not a process
        kill."""
        proc = ClaudeFakeProc()
        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
            session = bridge.ClaudeSession(
                "roles/scout.md", "plan", True, io_out=io.StringIO(),
                session_id="S1")
            session._first_token_deadline_seconds = 0.05
            result = session.send("first")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "no_first_token")
        self.assertEqual(
            result["controller_turn_outcome"],
            bridge.controller_turn_outcome_no_first_token())
        # No terminate()/kill() attribute exists on ClaudeFakeProc at all --
        # if the production code had called either, this test would already
        # have raised AttributeError above. The session-lifetime handle
        # remains genuinely alive.
        self.assertIsNotNone(bridge.live_child_handle(session))

    def test_no_first_token_never_classified_as_no_evidence_silence(self):
        for classify in (bridge.classify_claude_activity,
                         bridge.classify_codex_activity,
                         bridge.classify_opencode_activity):
            self.assertEqual(
                classify({"kind": "no_first_token"}), "hung_descendant")
            self.assertNotEqual(
                classify({"kind": "no_first_token"}), "no_evidence_silence")


# =============================================================================
# 9. Late-write reconciliation (issue #5), real cowork.py production seam.
# =============================================================================

class LateWriteReconciliationTest(unittest.TestCase):

    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._root = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._root

    def tearDown(self):
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._root, ignore_errors=True)

    def test_reconcile_before_presentation_retains_both_classifications(self):
        """Drives `cowork._reconcile_before_presentation` -- the REAL
        production call site `_role_loop` invokes immediately before any
        stall/retry/invalidation gate -- never `reread_before_gate`
        called directly."""
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(
                work_id, activity_class="hung_descendant"))
        trace = RecordingTrace()
        reconciliation = cowork._reconcile_before_presentation(
            session_uuid, work_id, "productive_model_work", trace=trace)
        self.assertIsNotNone(reconciliation)
        self.assertEqual(reconciliation["original_classification"],
                         "hung_descendant")
        self.assertEqual(reconciliation["reconciled_classification"],
                         "productive_model_work")
        events = trace.named("activity.reconciled")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["original"], "hung_descendant")
        self.assertEqual(events[0]["reconciled"], "productive_model_work")

        current = state_store.latest_activity(session_uuid, work_id)
        self.assertEqual(current["effective_classification"],
                         "productive_model_work")
        self.assertEqual(
            current["reconciliation_record"]["original_classification"],
            "hung_descendant",
            "the original classification must remain durably readable, "
            "never erased by the reconciliation")

    def test_late_write_after_a_presented_gate_never_retroactively_rewrites_it(self):
        """A LATE raw ActivityRecord arriving AFTER a gate was already
        reconciled must never retroactively alter that already-presented
        decision -- only the NEXT `reconcile_before_presentation` call
        reconciles against the fresh baseline."""
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(
                work_id, activity_class="hung_descendant",
                time_="2026-01-01T00:00:00Z"))
        first = cowork._reconcile_before_presentation(
            session_uuid, work_id, "productive_model_work")
        self.assertIsNotNone(first)

        # A LATE write lands (e.g. the controller genuinely produced a
        # fresh observation after the gate above was already shown).
        state_store.append_activity_record(
            session_uuid, _activity_record(
                work_id, activity_class="local_tool_work",
                time_="2026-01-01T00:01:00Z"))

        # `first`'s own already-returned/presented result is untouched --
        # this is proven by reading it back exactly as-is (append-only: no
        # in-place mutation exists anywhere in this store).
        history = state_store.read_activity_history(session_uuid, work_id)
        self.assertEqual(history[1]["record"], "ActivityReconciliationRecord")
        self.assertEqual(history[1]["reconciled_classification"],
                         "productive_model_work")

        # Only the NEXT reconciliation call reconciles again, now against
        # the late write's own fresh baseline.
        second = cowork._reconcile_before_presentation(
            session_uuid, work_id, "provider_wait")
        self.assertIsNotNone(second)
        self.assertEqual(second["original_classification"], "local_tool_work")
        self.assertEqual(second["reconciled_classification"], "provider_wait")


# =============================================================================
# 11. The frozen brief's seven required deterministic fixtures, each named
#     explicitly and independently re-derived here (several are ALSO proven
#     above through a different lens; this section is the single, clearly
#     citable place naming all seven by the brief's own vocabulary).
# =============================================================================

class SevenRequiredDeterministicFixturesTest(unittest.TestCase):

    def test_1_long_productive_silence_never_certified_a_stall(self):
        """A long-elapsed, wildly overdue-for-review turn whose controller
        process is STILL ALIVE is genuine "long productive silence" -- no
        event has landed in a long time, but the child is alive and
        working -- and must never be certified a stall."""
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="no_evidence_silence",
            time_="2020-01-01T00:00:00Z", age_seconds=7200.0)
        schedule_record = _schedule_record(
            work_id, next_at="2020-01-01T00:05:00Z")

        class _AliveProc:
            pid = 999

            def poll(self):
                return None

        class _AliveSession:
            controller = "codex"
            _live_proc = _AliveProc()

        decision = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            schedule_record, session=_AliveSession())
        self.assertEqual(decision["verdict"], "no_action")

    def test_2_no_event_process_death_escalates_when_review_overdue(self):
        """The genuine opposite of fixture 1: zero evidence AND a dead
        probe AND an overdue scheduled review together -- and ONLY
        together -- reach `hard_stall_eligible`."""
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="no_evidence_silence",
            time_="2026-01-01T00:00:00Z")
        schedule_record = _schedule_record(
            work_id, next_at="2026-01-01T00:01:00Z")  # already due

        class _DeadSession:
            controller = "opencode"
            _live_proc = None

        decision = watchdog.decide(
            work_id, "2026-01-01T00:10:00Z", activity_record, None,
            schedule_record, session=_DeadSession())
        self.assertEqual(decision["verdict"], "hard_stall_eligible")
        self.assertIsNotNone(decision["durable_evidence_ref"])
        self.assertIsNotNone(decision["process_probe_ref"])

    def test_3_provider_wait_never_a_stall_and_never_productive(self):
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="provider_wait")
        schedule_record = _schedule_record(
            work_id, next_at="2020-01-01T00:00:00Z")  # overdue, but non-terminal

        class _DeadSession:
            controller = "claude"
            proc = None

        decision = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            schedule_record, session=_DeadSession())
        # provider_wait is not in TERMINAL_ACTIVITY_CLASSES: never a stall,
        # regardless of an overdue review or a dead probe.
        self.assertEqual(decision["verdict"], "no_action")
        compact = activity.project_compact_state(
            activity_record, decision, schedule_record)
        self.assertEqual(compact["activity_class"], "provider_wait")

    def test_4_policy_denial_never_a_stall_and_never_reclassified(self):
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="policy_denial")
        schedule_record = _schedule_record(
            work_id, next_at="2020-01-01T00:00:00Z")

        class _DeadSession:
            controller = "codex"
            _live_proc = None

        decision = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            schedule_record, session=_DeadSession())
        self.assertEqual(decision["verdict"], "no_action")

    def test_5_late_writes_on_both_sides_of_controller_exit(self):
        """See `LateWriteBothSidesOfControllerExitTest` in
        `test_m4_crash_resume.py` for the full crash/resume-flavored
        derivation of this fixture; re-asserted here at the pure
        reconciliation-and-projection level for completeness in this
        file's own self-contained proof."""
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        self.addCleanup(lambda: (
            os.environ.pop("COWORK_SESSIONS_ROOT", None) if prior_root is None
            else os.environ.__setitem__("COWORK_SESSIONS_ROOT", prior_root)))
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(
            session_uuid, _activity_record(
                work_id, activity_class="provider_wait"))
        # BEFORE controller exit.
        before = cowork._reconcile_before_presentation(
            session_uuid, work_id, "local_tool_work")
        self.assertIsNotNone(before)
        # AFTER controller exit: a late write lands, then a second
        # reconciliation reflects it.
        state_store.append_activity_record(
            session_uuid, _activity_record(
                work_id, activity_class="hung_descendant",
                time_="2026-01-01T00:05:00Z"))
        after = cowork._reconcile_before_presentation(
            session_uuid, work_id, "process_crash")
        self.assertIsNotNone(after)
        self.assertEqual(after["original_classification"], "hung_descendant")

    def test_6_hung_descendants_soft_then_hard_with_independent_evidence(self):
        work_id = _uuid()
        activity_record = _activity_record(
            work_id, activity_class="hung_descendant")
        soft = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            _schedule_record(work_id), session=None, hung_ps_evidence=None)
        self.assertEqual(soft["verdict"], "soft_warning")
        hard = watchdog.decide(
            work_id, "2026-01-01T00:00:00Z", activity_record, None,
            _schedule_record(work_id), session=None,
            hung_ps_evidence="ps:pid=9,ppid=1,stat=Z")
        self.assertEqual(hard["verdict"], "hard_stall_eligible")

    def test_7_cross_surface_equivalence_interactive_headless_report(self):
        """Interactive (off-TTY plain text), headless, and report facts,
        built from the SAME durable evidence via the real Package B/A/D/E
        production functions -- an independent re-derivation of Package
        D's own three-way fixture (M4R-C01), fresh fixture data, no shared
        helper imported from `test_cowork_activity_cross_surface.py`."""
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        self.addCleanup(lambda: (
            os.environ.pop("COWORK_SESSIONS_ROOT", None) if prior_root is None
            else os.environ.__setitem__("COWORK_SESSIONS_ROOT", prior_root)))
        session_uuid = _uuid()
        work_id = _uuid()
        state_store.append_activity_record(session_uuid, _activity_record(
            work_id, activity_class="local_tool_work", source="opencode",
            provider_health="degraded"))
        state_store.write_scheduled_review(
            session_uuid, _schedule_record(
                work_id, next_at="2026-02-02T00:00:00Z"))

        current = state_store.latest_activity(session_uuid, work_id)
        schedule_record = state_store.read_next_inspection(
            session_uuid, work_id)
        decision = activity.validate_watchdog_decision(_watchdog_decision(
            work_id, verdict="no_action"))
        compact_state = activity.project_compact_state(
            current["activity_record"], decision, schedule_record,
            current["reconciliation_record"])

        interactive_out = io.StringIO()
        ui.render_compact_activity(interactive_out, compact_state,
                                   enabled=False)
        headless_out = io.StringIO()
        ui.render_headless_activity(headless_out, compact_state)

        record = measure.build_record(session_uuid)
        report_text = "\n".join(report._section_activity(record))

        for text in (interactive_out.getvalue(), headless_out.getvalue(),
                    report_text):
            self.assertIn("opencode", text)
        self.assertIn("degraded", report_text)
        self.assertIn("local tool work", interactive_out.getvalue())
        self.assertIn("local tool work", headless_out.getvalue())
        self.assertIn("local_tool_work", report_text)
        self.assertEqual(record["activity"]["source"], "opencode")
        self.assertEqual(record["activity"]["provider_health"], "degraded")
        self.assertEqual(record["activity"]["activity_class"],
                         "local_tool_work")

    def test_7b_ttyness_never_changes_headless_facts(self):
        """`render_headless_activity` must never branch on `isatty()` --
        the same compact_state renders identically whether `io_out` is a
        real-pty-shaped double or a plain StringIO."""
        work_id = _uuid()
        compact_state = activity.project_compact_state(
            _activity_record(work_id, activity_class="owned_verification"),
            _watchdog_decision(work_id), _schedule_record(work_id))
        plain_out = io.StringIO()
        ui.render_headless_activity(plain_out, compact_state)
        tty_out = FakeTTY()
        ui.render_headless_activity(tty_out, compact_state)
        self.assertEqual(plain_out.getvalue(), tty_out.getvalue())


# =============================================================================
# 12. Shutdown-event reuse across two `run_flow` calls in one process.
# =============================================================================

class ShutdownEventReuseAcrossRunsTest(unittest.TestCase):
    """M4D-MAJ-03, independently re-derived: `_ACTIVITY_SHUTDOWN_EVENT` is a
    process-global `threading.Event`, but each `run_flow` invocation is its
    own production run -- a real SIGTERM landing during an EARLIER call must
    never suppress activity appends for a LATER, healthy call in the SAME
    process."""

    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._root = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self._prior_interval = cowork._ACTIVITY_TICK_INTERVAL_SECONDS
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = 0.05
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()

    def tearDown(self):
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = self._prior_interval
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._root, ignore_errors=True)

    @staticmethod
    def _headless_args(controller="claude"):
        return cowork.build_parser().parse_args(
            ["--team", "scout", "--config", "scout=%s,yolo,plan" % controller,
             "--context", "hi", "--no-session", "--headless"])

    def test_leaked_shutdown_event_from_a_prior_run_never_suppresses_a_later_one(self):
        def _kill_mid_run(config, context, selected, on_outcome=None,
                          on_session=None, resume_id=None, session_uuid=None,
                          **kw):
            os.kill(os.getpid(), signal.SIGTERM)
            return 0  # unreachable

        with self.assertRaises(SystemExit) as ctx:
            cowork.run_flow(
                self._headless_args(), io_out=io.StringIO(),
                which=lambda c: "/bin/" + c, run_scout_fn=_kill_mid_run)
        self.assertEqual(ctx.exception.code, 128 + signal.SIGTERM)
        self.assertTrue(cowork._ACTIVITY_SHUTDOWN_EVENT.is_set())

        work_id = _uuid()
        captured = {}

        class _AcceptingSession:
            controller = "claude"

            def send(self, text, meta=None):
                return {"ok": True, "result": "ok"}

            def close(self):
                pass

        # `run_flow` mints and owns its own real session_uuid internally
        # (this run passes `--no-session`, so it generates a fresh one) --
        # this closure captures the ACTUAL value `run_flow` supplies, never
        # a pre-guessed/shadowed local, since a same-named kwarg here would
        # otherwise silently shadow an outer variable of the same name.
        def _second_run(config, context, selected, io_in=None, io_out=None,
                        on_outcome=None, on_session=None, resume_id=None,
                        session_uuid=None, **kw):
            captured["session_uuid"] = session_uuid
            cowork._role_loop(
                _AcceptingSession(), "seed",
                os.path.join(tempfile.mkdtemp(), "status.json"), context="",
                io_in=io.StringIO(""), io_out=io_out, headless=True,
                session_uuid=session_uuid, role_work_id=work_id, role="scout")
            return 0

        rc = cowork.run_flow(
            self._headless_args(), io_out=io.StringIO(),
            which=lambda c: "/bin/" + c, run_scout_fn=_second_run)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(captured.get("session_uuid"))
        # The second, genuinely healthy run must NOT have had its own
        # turn-boundary activity append suppressed by the first run's
        # leaked shutdown event.
        history = state_store.read_activity_history(
            captured["session_uuid"], work_id)
        self.assertGreater(
            len(history), 0,
            "the second run_flow call's activity append was silently "
            "suppressed by the first call's leaked SIGTERM event")

    def test_run_flow_entry_clears_the_shared_event_even_when_left_set_externally(self):
        """Independent of any SIGTERM at all: `run_flow`'s own entry
        unconditionally clears `_ACTIVITY_SHUTDOWN_EVENT` before installing
        its SIGTERM handler and before any tick can observe it -- proven by
        setting it externally first (never via a real signal), then
        confirming a real turn-boundary append still lands."""
        cowork._ACTIVITY_SHUTDOWN_EVENT.set()
        work_id = _uuid()
        captured = {}

        class _AcceptingSession:
            controller = "claude"

            def send(self, text, meta=None):
                return {"ok": True, "result": "ok"}

            def close(self):
                pass

        def _run_scout(config, context, selected, io_in=None, io_out=None,
                       on_outcome=None, on_session=None, resume_id=None,
                       session_uuid=None, **kw):
            captured["session_uuid"] = session_uuid
            cowork._role_loop(
                _AcceptingSession(), "seed",
                os.path.join(tempfile.mkdtemp(), "status.json"), context="",
                io_in=io.StringIO(""), io_out=io_out, headless=True,
                session_uuid=session_uuid, role_work_id=work_id, role="scout")
            return 0

        rc = cowork.run_flow(
            self._headless_args(), io_out=io.StringIO(),
            which=lambda c: "/bin/" + c, run_scout_fn=_run_scout)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(captured.get("session_uuid"))
        history = state_store.read_activity_history(
            captured["session_uuid"], work_id)
        self.assertGreater(len(history), 0)


if __name__ == "__main__":
    unittest.main()
