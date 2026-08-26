#!/usr/bin/env python3
"""Cross-surface fact-equivalence tests (M4 Package D): interactive
(`cowork_ui.render_compact_activity`), headless
(`cowork_ui.render_headless_activity`), and the report leg
(`cowork_report._section_activity`, fed by `cowork_measure.build_record`'s
`record["activity"]`) all consume the SAME compact facts for the same
durable evidence -- proven end-to-end through the real Package B/D/E/D
functions, never a hand-rolled shape that could drift from what any of them
actually produce.

Run standalone:

    python3 -m unittest scripts.test_cowork_activity_cross_surface -v
"""

import io
import os
import shutil
import signal
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_activity as activity  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_report as report  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_ui as ui  # noqa: E402
import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402

WORK_ID = "99999999-8888-7777-6666-555555555555"


class FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal, so C's real Spinner (and
    the interactive renderer's own `is_tty()` check) treat it as one --
    the same fixture convention `test_cowork_ui_activity.py` already uses."""

    def isatty(self):
        return True


class CrossSurfaceEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._tmp = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._tmp
        self.session_uuid = "11112222-3333-4444-5555-666677778888"

    def tearDown(self):
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_durable_evidence(self, activity_class="productive_model_work",
                                source="claude", age_seconds=42.0,
                                provider_health="healthy"):
        state_store.append_activity_record(self.session_uuid, {
            "schema_version": 1, "record": "ActivityRecord",
            "work_id": WORK_ID, "time": "2026-01-01T00:00:00Z",
            "activity_class": activity_class, "source": source,
            "artifact_fingerprint": None, "artifact_delta": [],
            "provider_health": provider_health, "age_seconds": age_seconds,
        })
        state_store.write_scheduled_review(self.session_uuid, {
            "schema_version": 1, "record": "ScheduledReviewRecord",
            "work_id": WORK_ID, "next_inspection_at": "2026-01-01T00:05:00Z",
            "interval_seconds": 300, "last_inspection_result_ref": None,
        })

    def _interactive_and_headless_text(self):
        """The real interactive (off-TTY plain-text branch) and headless
        renderer output, built from the SAME durable evidence via the real
        `cowork_state.latest_activity`/`read_next_inspection` reads and a
        real `project_compact_state` projection -- exactly the pipeline
        `cowork.py`'s own activity-emission seam uses."""
        current = state_store.latest_activity(self.session_uuid, WORK_ID)
        schedule_record = state_store.read_next_inspection(
            self.session_uuid, WORK_ID)
        decision = activity.validate_watchdog_decision({
            "schema_version": 1, "record": "WatchdogDecision",
            "work_id": WORK_ID, "time": "2026-01-01T00:00:01Z",
            "verdict": "no_action", "durable_evidence_ref": None,
            "process_probe_ref": None,
        })
        compact_state = activity.project_compact_state(
            current["activity_record"], decision, schedule_record,
            current["reconciliation_record"])
        interactive_out = io.StringIO()
        ui.render_compact_activity(interactive_out, compact_state,
                                   enabled=False)
        headless_out = io.StringIO()
        ui.render_headless_activity(headless_out, compact_state)
        return interactive_out.getvalue(), headless_out.getvalue()

    def _report_text(self):
        record = measure.build_record(self.session_uuid)
        return "\n".join(report._section_activity(record)), record

    def test_all_three_surfaces_agree_on_activity_class(self):
        self._write_durable_evidence(activity_class="local_tool_work")
        interactive, headless = self._interactive_and_headless_text()
        report_text, record = self._report_text()
        # The renderers print a human-readable label for the SAME
        # underlying class the report prints verbatim.
        self.assertIn("local tool work", interactive)
        self.assertIn("local tool work", headless)
        self.assertIn("local_tool_work", report_text)
        self.assertEqual(record["activity"]["activity_class"],
                         "local_tool_work")

    def test_all_three_surfaces_agree_on_source_and_provider_health(self):
        self._write_durable_evidence(source="opencode",
                                     provider_health="degraded")
        interactive, headless = self._interactive_and_headless_text()
        report_text, record = self._report_text()
        for text in (interactive, headless, report_text):
            self.assertIn("opencode", text)
        self.assertIn("degraded", report_text)
        self.assertEqual(record["activity"]["source"], "opencode")
        self.assertEqual(record["activity"]["provider_health"], "degraded")

    def test_all_three_surfaces_agree_on_next_inspection_at(self):
        self._write_durable_evidence()
        interactive, headless = self._interactive_and_headless_text()
        report_text, record = self._report_text()
        for text in (interactive, headless, report_text):
            self.assertIn("2026-01-01T00:05:00Z", text)
        self.assertEqual(record["activity"]["next_inspection_at"],
                         "2026-01-01T00:05:00Z")

    def test_all_three_surfaces_agree_after_reconciliation(self):
        self._write_durable_evidence(activity_class="hung_descendant")
        state_store.reread_before_gate(
            self.session_uuid, WORK_ID, "2026-01-01T00:01:00Z",
            "productive_model_work", "a" * 64, "poll")
        interactive, headless = self._interactive_and_headless_text()
        report_text, record = self._report_text()
        # The RECONCILED class, never the superseded original, is what
        # every surface reports as the current fact.
        for text in (interactive, headless):
            self.assertIn("productive model work", text)
            self.assertIn("reconciled from hung descendant", text)
        self.assertIn("productive_model_work", report_text)
        self.assertEqual(record["activity"]["activity_class"],
                         "productive_model_work")
        self.assertEqual(record["activity"]["original_classification"],
                         "hung_descendant")
        self.assertTrue(record["activity"]["reconciled"])

    def test_no_durable_evidence_all_surfaces_agree_on_unknown(self):
        # No _write_durable_evidence() call: nothing was ever recorded.
        record = measure.build_record(self.session_uuid)
        report_text = "\n".join(report._section_activity(record))
        self.assertEqual(record["activity"], measure._ACTIVITY_UNKNOWN_SHAPE)
        # The fixed UNKNOWN shape is non-empty (every key present, valued
        # "unknown"), so `_section_activity` renders every fact line
        # honestly as `unknown` rather than the separate "nothing at all"
        # placeholder reserved for a genuinely absent/empty `activity` key.
        self.assertIn(measure.UNKNOWN, report_text)
        self.assertNotIn("no durable activity recorded", report_text)
        # The interactive/headless renderers, given no compact_state at
        # all (the real production seam never renders without one -- see
        # `cowork.py`'s own `_render_activity_snapshot` guard), simply
        # never fire; there is no fabricated "unknown" activity panel to
        # cross-check against on those two surfaces for an unrecorded work
        # engagement, matching this same fixed absence.

    def test_never_fabricates_productive_from_provider_wait_alone(self):
        # False-productive-attribution guard, proven across all three
        # surfaces at once: a provider_wait record with NO reconciliation
        # never renders as productive_model_work anywhere.
        self._write_durable_evidence(activity_class="provider_wait")
        interactive, headless = self._interactive_and_headless_text()
        report_text, record = self._report_text()
        self.assertIn("provider_wait", report_text)
        self.assertEqual(record["activity"]["activity_class"],
                         "provider_wait")
        for text in (interactive, headless):
            self.assertIn("waiting on provider", text)
            self.assertNotIn("productive model work", text)
        self.assertNotEqual(record["activity"]["activity_class"],
                            "productive_model_work")


# =========================================================================== #
# M4D-MAJ-01 correction: non-vacuous output arbitration -- a REAL             #
# `cowork_ui.Spinner` (Package C's own, unmocked) writes genuine `\r\033[K`   #
# CR-frames to a TTY-like sink while a real `cowork_bridge.CodexSession` turn #
# is in flight; D's retained renderer call fires only once that spinner has  #
# already closed, with zero interleave between the two.                     #
# =========================================================================== #

class _DelayedLineIter(object):
    """Yields `lines` one at a time, sleeping `delay` seconds before the
    FIRST line -- long enough for a real (0.1s-per-frame) `cowork_ui.
    Spinner` background thread to write at least one genuine CR-frame
    before any content arrives."""

    def __init__(self, lines, delay):
        self._lines = list(lines)
        self._delay = delay
        self._first = True

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            _sleep(self._delay)
        if not self._lines:
            raise StopIteration
        return self._lines.pop(0)


def _sleep(seconds):
    import time
    time.sleep(seconds)


class _DelayedScriptedProc(object):
    """A `subprocess.Popen`-shaped fake whose `.stdout` delays its first
    line just long enough for a real Spinner to spin, then completes
    normally -- never approaching the first-token deadline."""

    def __init__(self, lines, delay=0.35):
        self.stdout = _DelayedLineIter(lines, delay)
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


class SpinnerThenActivityRenderTest(unittest.TestCase):
    """Gate 12: real Spinner, TTY-like sink, a positive `\\r\\033[K` frame
    assertion, then zero interleave with the retained renderer."""

    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._tmp = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._tmp
        self._prior_interval = cowork._ACTIVITY_TICK_INTERVAL_SECONDS
        # Shrink the tick interval far below the spinner's ~0.35s window so
        # several ticks WOULD fire during it if they wrote anything at all
        # -- proving zero interleave is not merely "no tick had time to
        # run" but "a tick that DID run wrote nothing".
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = 0.05

    def tearDown(self):
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = self._prior_interval
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_real_spinner_frame_then_activity_render_zero_interleave(self):
        # `_role_loop`'s own status-repair/headless-nudge cascade may issue
        # MORE than one real send when (as here) a scripted proc never
        # writes a status artifact -- every one of them is a REAL send
        # through the SAME retained call site, so this test does not
        # assume exactly one turn; it counts the genuine sends and proves
        # the renderer fires exactly once per genuine send, never once
        # more from a tick.
        lines = [
            '{"type": "thread.started", "thread_id": "T1"}',
            '{"type": "item.completed", "item": {"type": "agent_message", '
            '"text": "done"}}',
        ]
        proc = _DelayedScriptedProc(lines, delay=0.35)
        out = FakeTTY()
        session_uuid = "77778888-0000-0000-0000-000000000001"
        work_id = "88887777-0000-0000-0000-000000000001"

        send_calls = []
        real_send = bridge.CodexSession.send

        def _counted_send(self, text, meta=None):
            send_calls.append(text)
            return real_send(self, text, meta=meta)

        with mock.patch.object(bridge.subprocess, "Popen", return_value=proc), \
                mock.patch.object(bridge.CodexSession, "send", _counted_send):
            session = bridge.CodexSession("implement", True, io_out=out,
                                          speaker="scout")
            cowork._role_loop(
                session, "seed", os.path.join(tempfile.mkdtemp(),
                                              "status.json"),
                context="", io_in=io.StringIO(""), io_out=out,
                headless=True, session_uuid=session_uuid,
                role_work_id=work_id, role="scout")

        full = out.getvalue()
        # Positive frame assertion: the real Spinner genuinely wrote at
        # least one periodic `\r\033[K<frame char>` CR-frame, not merely
        # its own final clear.
        self.assertRegex(full, r"\r\x1b\[K[|/\\-] ")

        # Zero interleave: the retained renderer fires EXACTLY once per
        # genuine turn-boundary send -- never once more from a tick, even
        # with a 0.05s tick interval active throughout the whole 0.35s
        # in-flight first turn above (proven non-vacuous: several tick
        # intervals genuinely elapsed during that wait).
        self.assertGreaterEqual(len(send_calls), 1)
        # "activity: " (with the trailing space `_activity_text_lines`
        # actually writes) is the renderer's own first fact line; plain
        # "activity:" alone also matches inside `durable_evidence_ref`'s
        # own "activity:<work_id>@<time>" reference string, so the space
        # is what disambiguates a genuine render from an evidence-ref
        # substring collision.
        self.assertEqual(full.count("activity: "), len(send_calls))

        # Ordering, scoped to the FIRST turn's own output (the one with
        # the real in-flight delay): its activity render appears strictly
        # AFTER that turn's own last periodic spin frame -- never before
        # or during it.
        first_chunk_end = full.index("artifact changes: none\n") + len(
            "artifact changes: none\n")
        first_chunk = full[:first_chunk_end]
        last_frame_idx = max(
            first_chunk.rfind("\r\x1b[K%s" % ch) for ch in "|/\\-")
        self.assertGreaterEqual(last_frame_idx, 0,
                                "no periodic spin frame found in the first "
                                "turn's own output")
        activity_idx = first_chunk.index("activity:")
        self.assertGreater(activity_idx, last_frame_idx,
                           "the activity render appeared before/during the "
                           "spinner's own last frame")


# =========================================================================== #
# M4D-MAJ-03 correction: `_ACTIVITY_SHUTDOWN_EVENT` is a process-global, but   #
# each `run_flow` invocation is its own production run -- a SIGTERM that      #
# landed during an EARLIER run_flow call in the same process (real, or as     #
# `test_cowork.py`'s own frozen `os.kill(os.getpid(), signal.SIGTERM)`        #
# regressions exercise via a genuine `_handle_external_kill`) must never      #
# suppress activity ticks/appends for a LATER, otherwise-healthy run_flow     #
# call. Both runs below go through the REAL `run_flow` entry point (never a   #
# reimplementation of its reset/handler-install ordering); only `run_scout`   #
# is swapped, exactly the injection seam `test_cowork.py`'s own              #
# `PhaseTruthExternalKillTest` already relies on for the identical SIGTERM    #
# mechanism.                                                                  #
# =========================================================================== #

class ShutdownEventRunBoundaryResetTest(unittest.TestCase):
    """A second `run_flow` call in the same process, after a first call left
    `_ACTIVITY_SHUTDOWN_EVENT` set via a real SIGTERM, still produces the
    real spinner CR-frame followed by the activity render with zero
    interleave -- proving the shared event is reset at the SECOND run's own
    entry, before its SIGTERM handler is installed and before its first
    tick, not merely left clear by test-local cleanup."""

    def setUp(self):
        self._prior_root = os.environ.get("COWORK_SESSIONS_ROOT")
        self._tmp = tempfile.mkdtemp()
        os.environ["COWORK_SESSIONS_ROOT"] = self._tmp
        self._prior_interval = cowork._ACTIVITY_TICK_INTERVAL_SECONDS
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = 0.05
        # A clean starting point for THIS test only -- the run_flow calls
        # below prove the reset happens in production code, not here.
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()

    def tearDown(self):
        cowork._ACTIVITY_TICK_INTERVAL_SECONDS = self._prior_interval
        cowork._ACTIVITY_SHUTDOWN_EVENT.clear()
        if self._prior_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._prior_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _headless_scout_args(controller):
        return cowork.build_parser().parse_args(
            ["--team", "scout", "--config", "scout=%s,yolo,plan" % controller,
             "--context", "hello", "--no-session", "--headless"])

    def test_second_run_flow_ignores_first_runs_leaked_sigterm_event(self):
        # First run: a real SIGTERM lands mid-run_flow (the same mechanism
        # `test_cowork.py`'s own `PhaseTruthExternalKillTest` exercises),
        # via the REAL `_handle_external_kill`, which sets the shared event
        # BEFORE this call raises SystemExit.
        def fake_scout_kill(config, context, selected, on_outcome=None,
                            on_session=None, resume_id=None,
                            session_uuid=None, **kw):
            os.kill(os.getpid(), signal.SIGTERM)
            return 0  # unreachable

        with self.assertRaises(SystemExit) as ctx:
            cowork.run_flow(
                self._headless_scout_args("claude"), io_out=io.StringIO(),
                which=lambda c: "/bin/" + c, run_scout_fn=fake_scout_kill)
        self.assertEqual(ctx.exception.code, 128 + signal.SIGTERM)
        self.assertTrue(
            cowork._ACTIVITY_SHUTDOWN_EVENT.is_set(),
            "the first run's real SIGTERM never set the shared event -- "
            "this fixture no longer exercises the leaked-state scenario")

        # Second run: a genuinely healthy run_flow call, in the SAME
        # process, with the leaked-set event from the first call still
        # standing at the moment this second `run_flow` is entered. Its own
        # `run_scout` override drives the REAL `cowork._role_loop` against a
        # REAL, unmocked `cowork_ui.Spinner` (via a real `CodexSession`
        # whose scripted subprocess delays its first line) -- identical
        # fixture discipline to `SpinnerThenActivityRenderTest` above.
        session_uuid = "77778888-0000-0000-0000-000000000002"
        work_id = "88887777-0000-0000-0000-000000000002"
        lines = [
            '{"type": "thread.started", "thread_id": "T1"}',
            '{"type": "item.completed", "item": {"type": "agent_message", '
            '"text": "done"}}',
        ]
        proc = _DelayedScriptedProc(lines, delay=0.35)
        out = FakeTTY()
        send_calls = []
        real_send = bridge.CodexSession.send

        def _counted_send(self, text, meta=None):
            send_calls.append(text)
            return real_send(self, text, meta=meta)

        def fake_scout_render(config, context, selected, io_in=None,
                              io_out=None, on_outcome=None, on_session=None,
                              resume_id=None, session_uuid=None, **kw):
            with mock.patch.object(bridge.subprocess, "Popen",
                                   return_value=proc), \
                    mock.patch.object(bridge.CodexSession, "send",
                                      _counted_send):
                session = bridge.CodexSession(
                    "implement", True, io_out=io_out, speaker="scout")
                cowork._role_loop(
                    session, "seed",
                    os.path.join(tempfile.mkdtemp(), "status.json"),
                    context="", io_in=io.StringIO(""), io_out=io_out,
                    headless=True, session_uuid=session_uuid,
                    role_work_id=work_id, role="scout")
            return 0

        rc = cowork.run_flow(
            self._headless_scout_args("codex"), io_out=out,
            which=lambda c: "/bin/" + c, run_scout_fn=fake_scout_render)
        self.assertEqual(rc, 0)

        full = out.getvalue()
        # Positive frame assertion: the real Spinner genuinely wrote at
        # least one periodic `\r\033[K<frame char>` CR-frame during the
        # second run.
        self.assertRegex(full, r"\r\x1b\[K[|/\\-] ")
        # Zero interleave AND zero suppression: every genuine turn-boundary
        # send in the second run rendered its own activity fact -- none
        # silently dropped by the first run's leaked shutdown event.
        self.assertGreaterEqual(len(send_calls), 1)
        self.assertEqual(full.count("activity: "), len(send_calls),
                         "the second run_flow call's activity render was "
                         "suppressed by the first call's leaked SIGTERM "
                         "event -- M4D-MAJ-03 regression")
        # Ordering, scoped to the first turn's own output: the activity
        # render appears strictly after that turn's own last spin frame.
        first_chunk_end = full.index("artifact changes: none\n") + len(
            "artifact changes: none\n")
        first_chunk = full[:first_chunk_end]
        last_frame_idx = max(
            first_chunk.rfind("\r\x1b[K%s" % ch) for ch in "|/\\-")
        self.assertGreaterEqual(last_frame_idx, 0,
                                "no periodic spin frame found in the second "
                                "run's own first-turn output")
        activity_idx = first_chunk.index("activity:")
        self.assertGreater(activity_idx, last_frame_idx,
                           "the activity render appeared before/during the "
                           "spinner's own last frame")


if __name__ == "__main__":
    unittest.main()
