#!/usr/bin/env python3
"""Focused tests for M3 Package F's macOS launchd wake adapter
(`cowork_wake_macos.py`), built against Package D's frozen wake-trigger
contract (`cowork_capacity_scheduler.py`).

No real launchd registration or real subprocess ever happens in this suite
-- every `launchctl_runner`/`resume_runner`/`wake_trigger`/`now_provider` is
a fake this file supplies. Fake clock throughout: every `now` reaching
Package D is an explicit RFC3339 literal, never the real wall clock.

Run standalone:

    python3 -m unittest scripts/test_cowork_wake_macos.py -v
"""

import ast
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

import cowork_capacity as capacity  # noqa: E402
import cowork_capacity_scheduler as scheduler  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_wake_macos as wake_macos  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


def _forbidden_real_call(*_args, **_kwargs):
    raise AssertionError(
        "a real launchctl/subprocess runner was invoked -- every test in "
        "this suite must inject its own fake runner")


def setUpModule():
    """D-N10-style non-vacuous guard for 'no real launchd registration in
    ordinary tests': for the ENTIRE duration of this test module, Package
    F's real launchctl/subprocess runners raise if ever actually called,
    rather than merely trusting that every test remembered to pass a fake
    one. `CliFireTest` explicitly overrides `_real_subprocess_runner` with
    its own fake via `mock.patch.object` (save/restore), which layers over
    -- and is fully restored after -- this module-wide guard."""
    global _real_launchctl_patch, _real_subprocess_patch, _real_now_patch
    _real_launchctl_patch = mock.patch.object(
        wake_macos, "_real_launchctl_runner", side_effect=_forbidden_real_call)
    _real_subprocess_patch = mock.patch.object(
        wake_macos, "_real_subprocess_runner", side_effect=_forbidden_real_call)
    _real_now_patch = mock.patch.object(
        wake_macos, "_real_now_provider", side_effect=_forbidden_real_call)
    _real_launchctl_patch.start()
    _real_subprocess_patch.start()
    _real_now_patch.start()


def tearDownModule():
    _real_launchctl_patch.stop()
    _real_subprocess_patch.stop()
    _real_now_patch.stop()


class _WakeMacosEnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test (mirrors test_cowork_capacity_
    scheduler.py's _SchedEnvMixin), so nothing ever touches the real home
    dir, and no real launchd/subprocess call is reachable from a bare
    `setUp`/`tearDown` -- every test explicitly injects its own fakes."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)
        self._plist_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._plist_dir, ignore_errors=True))

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root


# --------------------------------------------------------------------------- #
# Fixture builders (mirror test_cowork_capacity_scheduler.py's conventions). #
# --------------------------------------------------------------------------- #


def _binding(**overrides):
    b = dict(role="builder", provider_session_id="sess-1",
            controller_policy_digest="a" * 64, candidate_digest="b" * 64,
            artifact_hashes={"artifact.txt": "c" * 64})
    b.update(overrides)
    return b


def _make_pause_lease(lease_id=None, failed_wake_attempts=0,
                      consumption_state="unclaimed", **overrides):
    lease = dict(schema_version=1, package_id="pkg-1",
                lease_id=lease_id or _uuid(),
                resume_mode="scheduled", not_before="2024-01-01T00:10:00Z",
                automation_ref="auto-1",
                consumption_state=consumption_state,
                failed_wake_attempts=failed_wake_attempts,
                issued_at="2024-01-01T00:00:00Z")
    lease.update(_binding())
    lease.update(overrides)
    return lease


def _create(session_id, **overrides):
    lease = _make_pause_lease(**overrides)
    return state_store.create_pause_lease(session_id, lease)


class _FakeLaunchctl:
    """Records every invocation; never touches a real launchd. `calls` is a
    list of the exact argv lists passed."""

    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, argv):
        self.calls.append(list(argv))
        return {"returncode": self.returncode, "stdout": "", "stderr": ""}


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeResumeRunner:
    """Records every invocation and its own explicit timeout; never spawns
    a real subprocess. `outcomes` lets a test script a sequence of
    per-call results (returncode or a raised exception)."""

    def __init__(self, returncode=0, raise_exc=None):
        self.calls = []
        self.returncode = returncode
        self.raise_exc = raise_exc

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeCompletedProcess(returncode=self.returncode)


# --------------------------------------------------------------------------- #
# Launchd registration: plist shape, install/uninstall/status via fakes.     #
# --------------------------------------------------------------------------- #


class LaunchdPlistShapeTest(_WakeMacosEnvMixin, unittest.TestCase):
    def test_label_is_exact_automation_ref(self):
        plist = wake_macos.build_launchd_plist(
            "com.cowork.wake.example", ["/usr/bin/python3", "fire"])
        self.assertEqual(plist["Label"], "com.cowork.wake.example")

    def test_program_arguments_passed_through_unmodified(self):
        args = ["/usr/bin/python3", "cowork_wake_macos.py", "fire", "--x", "y"]
        plist = wake_macos.build_launchd_plist("auto-1", args)
        self.assertEqual(plist["ProgramArguments"], args)

    def test_start_interval_seconds_included_when_given(self):
        plist = wake_macos.build_launchd_plist(
            "auto-1", ["/bin/true"], start_interval_seconds=120)
        self.assertEqual(plist["StartInterval"], 120)

    def test_start_interval_seconds_omitted_by_default(self):
        plist = wake_macos.build_launchd_plist("auto-1", ["/bin/true"])
        self.assertNotIn("StartInterval", plist)

    def test_rejects_unsafe_automation_ref(self):
        for bad in ("../escape", "has/slash", "", "has space", "a" * 300):
            with self.assertRaises(ValueError):
                wake_macos.build_launchd_plist(bad, ["/bin/true"])

    def test_rejects_empty_program_arguments(self):
        with self.assertRaises(ValueError):
            wake_macos.build_launchd_plist("auto-1", [])

    def test_rejects_non_string_program_arguments(self):
        with self.assertRaises(ValueError):
            wake_macos.build_launchd_plist("auto-1", ["/bin/true", 5])

    def test_rejects_non_positive_start_interval(self):
        with self.assertRaises(ValueError):
            wake_macos.build_launchd_plist(
                "auto-1", ["/bin/true"], start_interval_seconds=0)


class PlistPathTest(_WakeMacosEnvMixin, unittest.TestCase):
    def test_path_uses_automation_ref_as_filename(self):
        path = wake_macos.plist_path_for("auto-1", base_dir=self._plist_dir)
        self.assertEqual(path, os.path.join(self._plist_dir, "auto-1.plist"))

    def test_rejects_unsafe_automation_ref_before_any_path_use(self):
        with self.assertRaises(ValueError):
            wake_macos.plist_path_for("../escape", base_dir=self._plist_dir)


class InstallUninstallStatusTest(_WakeMacosEnvMixin, unittest.TestCase):
    def test_install_writes_plist_and_calls_fake_launchctl_load(self):
        fake = _FakeLaunchctl()
        result = wake_macos.install(
            "auto-1", ["/usr/bin/python3", "cowork_wake_macos.py", "fire"],
            start_interval_seconds=60, base_dir=self._plist_dir,
            launchctl_runner=fake)
        self.assertTrue(os.path.exists(result["plist_path"]))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][:2], ["launchctl", "load"])
        self.assertIn(result["plist_path"], fake.calls[0])
        self.assertTrue(result["ok"])

    def test_installed_plist_label_matches_automation_ref_exactly(self):
        import plistlib
        fake = _FakeLaunchctl()
        result = wake_macos.install(
            "com.cowork.wake.exact", ["/bin/true"], base_dir=self._plist_dir,
            launchctl_runner=fake)
        with open(result["plist_path"], "rb") as fh:
            written = plistlib.load(fh)
        self.assertEqual(written["Label"], "com.cowork.wake.exact")

    def test_uninstall_calls_fake_launchctl_unload_and_removes_file(self):
        install_fake = _FakeLaunchctl()
        result = wake_macos.install(
            "auto-1", ["/bin/true"], base_dir=self._plist_dir,
            launchctl_runner=install_fake)
        self.assertTrue(os.path.exists(result["plist_path"]))
        uninstall_fake = _FakeLaunchctl()
        uninstall_result = wake_macos.uninstall(
            "auto-1", base_dir=self._plist_dir, launchctl_runner=uninstall_fake)
        self.assertFalse(os.path.exists(result["plist_path"]))
        self.assertEqual(uninstall_fake.calls[0][:2], ["launchctl", "unload"])
        self.assertTrue(uninstall_result["ok"])

    def test_uninstall_of_never_installed_job_does_not_raise(self):
        fake = _FakeLaunchctl()
        result = wake_macos.uninstall("auto-1", base_dir=self._plist_dir,
                                      launchctl_runner=fake)
        self.assertFalse(result["launchctl_result"] is None)

    def test_status_reports_plist_presence(self):
        fake = _FakeLaunchctl()
        self.assertFalse(
            wake_macos.status("auto-1", base_dir=self._plist_dir,
                              launchctl_runner=fake)["plist_present"])
        wake_macos.install("auto-1", ["/bin/true"], base_dir=self._plist_dir,
                           launchctl_runner=fake)
        self.assertTrue(
            wake_macos.status("auto-1", base_dir=self._plist_dir,
                              launchctl_runner=fake)["plist_present"])

    # ----------------------------------------------------------------- #
    # F-MJ-03: non-vacuous fake-launchctl FAILURE tests.                #
    # ----------------------------------------------------------------- #

    def test_failed_launchctl_load_reports_not_ok(self):
        fake = _FakeLaunchctl(returncode=1)
        result = wake_macos.install(
            "auto-1", ["/bin/true"], base_dir=self._plist_dir,
            launchctl_runner=fake)
        self.assertFalse(result["ok"])

    def test_failed_launchctl_unload_does_not_delete_plist(self):
        """F-MJ-03: a FAILED unload must never delete the plist file --
        launchd may still consider the job loaded."""
        install_fake = _FakeLaunchctl(returncode=0)
        installed = wake_macos.install(
            "auto-1", ["/bin/true"], base_dir=self._plist_dir,
            launchctl_runner=install_fake)
        self.assertTrue(os.path.exists(installed["plist_path"]))
        failing_unload = _FakeLaunchctl(returncode=1)
        result = wake_macos.uninstall(
            "auto-1", base_dir=self._plist_dir, launchctl_runner=failing_unload)
        self.assertFalse(result["ok"])
        self.assertTrue(os.path.exists(installed["plist_path"]),
                        "plist must survive a failed unload")

    def test_successful_launchctl_unload_after_failed_one_still_deletes(self):
        install_fake = _FakeLaunchctl(returncode=0)
        installed = wake_macos.install(
            "auto-1", ["/bin/true"], base_dir=self._plist_dir,
            launchctl_runner=install_fake)
        failing_unload = _FakeLaunchctl(returncode=1)
        wake_macos.uninstall("auto-1", base_dir=self._plist_dir,
                             launchctl_runner=failing_unload)
        self.assertTrue(os.path.exists(installed["plist_path"]))
        succeeding_unload = _FakeLaunchctl(returncode=0)
        result = wake_macos.uninstall(
            "auto-1", base_dir=self._plist_dir, launchctl_runner=succeeding_unload)
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(installed["plist_path"]))

    def test_failed_launchctl_status_reports_not_ok(self):
        fake = _FakeLaunchctl(returncode=1)
        result = wake_macos.status("auto-1", base_dir=self._plist_dir,
                                   launchctl_runner=fake)
        self.assertFalse(result["ok"])

    def test_launchctl_result_missing_returncode_key_is_treated_as_failure(self):
        """A malformed/buggy runner (e.g. a broken fake) that omits its own
        `returncode` must never be silently trusted as success."""
        def broken_runner(argv):
            return {"stdout": "", "stderr": ""}
        result = wake_macos.status("auto-1", base_dir=self._plist_dir,
                                   launchctl_runner=broken_runner)
        self.assertFalse(result["ok"])


class CliLaunchctlExitCodeTest(_WakeMacosEnvMixin, unittest.TestCase):
    """F-MJ-03: failed launchctl load/unload/status must produce a nonzero
    CLI process status, not merely a nonzero-looking JSON payload. Every
    test here passes `--base-dir` pointing at an isolated temp dir -- never
    the real `~/Library/LaunchAgents` -- on top of patching out the real
    launchctl runner, so no real launchd registration or home-directory
    write is ever reachable from this suite."""

    def test_cli_install_nonzero_exit_on_failed_load(self):
        fake = _FakeLaunchctl(returncode=1)
        lines = []
        with mock.patch.object(wake_macos, "_real_launchctl_runner", fake):
            code = wake_macos.main(
                ["install", "--automation-ref", "auto-1",
                "--program-argument", "/bin/true",
                "--base-dir", self._plist_dir], output=lines.append)
        self.assertEqual(code, wake_macos.LAUNCHCTL_EXIT_CODES["failed"])
        self.assertNotEqual(code, 0)

    def test_cli_install_zero_exit_on_successful_load(self):
        fake = _FakeLaunchctl(returncode=0)
        lines = []
        with mock.patch.object(wake_macos, "_real_launchctl_runner", fake):
            code = wake_macos.main(
                ["install", "--automation-ref", "auto-1",
                "--program-argument", "/bin/true",
                "--base-dir", self._plist_dir], output=lines.append)
        self.assertEqual(code, wake_macos.LAUNCHCTL_EXIT_CODES["success"])

    def test_cli_uninstall_nonzero_exit_on_failed_unload(self):
        fake = _FakeLaunchctl(returncode=1)
        lines = []
        with mock.patch.object(wake_macos, "_real_launchctl_runner", fake):
            code = wake_macos.main(
                ["uninstall", "--automation-ref", "auto-1",
                "--base-dir", self._plist_dir], output=lines.append)
        self.assertEqual(code, wake_macos.LAUNCHCTL_EXIT_CODES["failed"])

    def test_cli_status_nonzero_exit_on_failed_query(self):
        fake = _FakeLaunchctl(returncode=1)
        lines = []
        with mock.patch.object(wake_macos, "_real_launchctl_runner", fake):
            code = wake_macos.main(
                ["status", "--automation-ref", "auto-1",
                "--base-dir", self._plist_dir], output=lines.append)
        self.assertEqual(code, wake_macos.LAUNCHCTL_EXIT_CODES["failed"])


# --------------------------------------------------------------------------- #
# Package D delegation: exact argument shape, versioned exit codes.          #
# --------------------------------------------------------------------------- #


class DWakeTriggerArgvShapeTest(unittest.TestCase):
    def test_argv_round_trips_through_ds_own_parser(self):
        argv = wake_macos.build_d_wake_trigger_argv(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z")
        parsed = scheduler.build_wake_trigger_arg_parser().parse_args(argv)
        self.assertEqual(parsed.session_uuid, "sess-1")
        self.assertEqual(parsed.lease_id, "lease-1")
        self.assertEqual(parsed.claimant_ref, "claim-1")
        self.assertEqual(parsed.automation_ref, "auto-1")
        self.assertEqual(parsed.now, "2024-01-01T00:00:00Z")
        self.assertIsNone(parsed.reference_now)

    def test_argv_exact_flag_order(self):
        argv = wake_macos.build_d_wake_trigger_argv(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z")
        self.assertEqual(argv, [
            "--session-uuid", "sess-1", "--lease-id", "lease-1",
            "--claimant-ref", "claim-1", "--automation-ref", "auto-1",
            "--now", "2024-01-01T00:00:00Z"])

    def test_argv_includes_optional_flags_only_when_given(self):
        argv = wake_macos.build_d_wake_trigger_argv(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z",
            reference_now="2024-01-01T00:00:01Z", max_clock_skew_seconds=5.0,
            max_jitter_seconds=10.0)
        parsed = scheduler.build_wake_trigger_arg_parser().parse_args(argv)
        self.assertEqual(parsed.reference_now, "2024-01-01T00:00:01Z")
        self.assertEqual(parsed.max_clock_skew_seconds, 5.0)
        self.assertEqual(parsed.max_jitter_seconds, 10.0)

    def test_omitted_optional_flags_use_ds_own_defaults(self):
        argv = wake_macos.build_d_wake_trigger_argv(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z")
        parsed = scheduler.build_wake_trigger_arg_parser().parse_args(argv)
        self.assertEqual(parsed.max_clock_skew_seconds,
                         scheduler.DEFAULT_MAX_CLOCK_SKEW_SECONDS)
        self.assertEqual(parsed.max_jitter_seconds,
                         scheduler.DEFAULT_MAX_JITTER_SECONDS)


class RunDWakeDecisionTest(_WakeMacosEnvMixin, unittest.TestCase):
    def test_success_outcome_and_exit_code_pass_through(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        exit_code, payload = wake_macos.run_d_wake_decision(
            session_id, "lease-1", "claim-1", "auto-1", "2024-06-01T00:00:00Z")
        self.assertEqual(exit_code, scheduler.WAKE_TRIGGER_EXIT_CODES["success"])
        self.assertEqual(payload["outcome"], "claimed")

    def test_not_due_outcome_and_exit_code_pass_through(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        exit_code, payload = wake_macos.run_d_wake_decision(
            session_id, "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z")
        self.assertEqual(exit_code, scheduler.WAKE_TRIGGER_EXIT_CODES["not_due"])
        self.assertEqual(payload["reason"], "early_refusal")

    def test_delegates_to_injected_wake_trigger_never_ds_real_one_when_faked(self):
        calls = []

        def fake_wake_trigger(argv, output=None):
            calls.append(argv)
            output(json.dumps({"outcome": "claimed", "lease_id": "lease-1",
                               "clock_skew_detected": False}) + "\n")
            return scheduler.WAKE_TRIGGER_EXIT_CODES["success"]

        exit_code, payload = wake_macos.run_d_wake_decision(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z",
            wake_trigger=fake_wake_trigger)
        self.assertEqual(len(calls), 1)
        self.assertEqual(exit_code, scheduler.WAKE_TRIGGER_EXIT_CODES["success"])
        self.assertEqual(payload["outcome"], "claimed")

    # ----------------------------------------------------------------- #
    # F-N03: total malformed/multiline D output handling -- never       #
    # raises, and never lets bad output data override the exit-code-    #
    # driven eligibility decision.                                       #
    # ----------------------------------------------------------------- #

    def test_zero_output_lines_never_raises(self):
        def silent_wake_trigger(argv, output=None):
            return scheduler.WAKE_TRIGGER_EXIT_CODES["success"]

        exit_code, payload = wake_macos.run_d_wake_decision(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z",
            wake_trigger=silent_wake_trigger)
        self.assertEqual(exit_code, scheduler.WAKE_TRIGGER_EXIT_CODES["success"])
        self.assertEqual(payload["d_output_anomaly"], "no_output_line")
        self.assertNotIn(payload["outcome"], scheduler.WAKE_TRIGGER_EXIT_CODES)

    def test_malformed_json_line_never_raises(self):
        def garbled_wake_trigger(argv, output=None):
            output("not valid json{{{\n")
            return scheduler.WAKE_TRIGGER_EXIT_CODES["success"]

        exit_code, payload = wake_macos.run_d_wake_decision(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z",
            wake_trigger=garbled_wake_trigger)
        self.assertEqual(exit_code, scheduler.WAKE_TRIGGER_EXIT_CODES["success"])
        self.assertEqual(payload["d_output_anomaly"], "malformed_json")
        self.assertEqual(payload["raw"], "not valid json{{{\n")

    def test_non_object_json_line_never_raises(self):
        def array_wake_trigger(argv, output=None):
            output(json.dumps(["not", "an", "object"]) + "\n")
            return scheduler.WAKE_TRIGGER_EXIT_CODES["success"]

        exit_code, payload = wake_macos.run_d_wake_decision(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z",
            wake_trigger=array_wake_trigger)
        self.assertEqual(payload["d_output_anomaly"], "not_an_object")

    def test_multiline_output_flagged_but_first_line_still_used(self):
        def multiline_wake_trigger(argv, output=None):
            output(json.dumps({"outcome": "claimed", "lease_id": "lease-1",
                               "clock_skew_detected": False}) + "\n")
            output(json.dumps({"outcome": "unexpected_extra"}) + "\n")
            return scheduler.WAKE_TRIGGER_EXIT_CODES["success"]

        exit_code, payload = wake_macos.run_d_wake_decision(
            "sess-1", "lease-1", "claim-1", "auto-1", "2024-01-01T00:00:00Z",
            wake_trigger=multiline_wake_trigger)
        self.assertEqual(payload["outcome"], "claimed")
        self.assertEqual(payload["d_output_anomaly"], "multiple_lines")

    def test_malformed_output_never_overrides_exit_code_driven_eligibility(self):
        """The eligibility decision is ALWAYS the exit code Package D's
        callable returned directly -- malformed body data can never make
        `fire` invoke (or skip) the resume-trigger incorrectly."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")

        def garbled_but_successful(argv, output=None):
            output("{{{not json\n")
            return scheduler.WAKE_TRIGGER_EXIT_CODES["success"]

        resume_fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake,
            wake_trigger=garbled_but_successful)
        self.assertEqual(result["outcome"], "resumed")
        self.assertEqual(len(resume_fake.calls), 1)


# --------------------------------------------------------------------------- #
# Package E delegation: external subprocess ONLY, never an import.           #
# --------------------------------------------------------------------------- #


class ResumeTriggerSubprocessTest(unittest.TestCase):
    def test_argv_appends_identifying_flags(self):
        argv = wake_macos.build_resume_trigger_argv(
            ["/usr/bin/python3", "e_resume_trigger.py"],
            "sess-1", "lease-1", "claim-1", "auto-1")
        self.assertEqual(argv, [
            "/usr/bin/python3", "e_resume_trigger.py",
            "--session-uuid", "sess-1", "--lease-id", "lease-1",
            "--claimant-ref", "claim-1", "--automation-ref", "auto-1"])

    def test_rejects_empty_resume_trigger_cmd(self):
        with self.assertRaises(ValueError):
            wake_macos.build_resume_trigger_argv(
                [], "sess-1", "lease-1", "claim-1", "auto-1")

    def test_invoke_success_uses_fake_runner_never_real_subprocess(self):
        fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.invoke_resume_trigger(
            ["/bin/e"], "sess-1", "lease-1", "claim-1", "auto-1", runner=fake)
        self.assertEqual(result["outcome"], "resume_trigger_invoked")
        self.assertEqual(len(fake.calls), 1)
        argv, timeout = fake.calls[0]
        self.assertEqual(timeout, wake_macos.RESUME_TRIGGER_TIMEOUT_SECONDS)
        self.assertIn("--session-uuid", argv)

    def test_invoke_nonzero_exit_reports_failure_not_exception(self):
        fake = _FakeResumeRunner(returncode=17)
        result = wake_macos.invoke_resume_trigger(
            ["/bin/e"], "sess-1", "lease-1", "claim-1", "auto-1", runner=fake)
        self.assertEqual(result["outcome"], "resume_trigger_failed")
        self.assertEqual(result["returncode"], 17)

    def test_invoke_crash_oserror_reports_failure_not_exception(self):
        fake = _FakeResumeRunner(raise_exc=OSError("simulated crash"))
        result = wake_macos.invoke_resume_trigger(
            ["/bin/e"], "sess-1", "lease-1", "claim-1", "auto-1", runner=fake)
        self.assertEqual(result["outcome"], "resume_trigger_failed")
        self.assertIn("simulated crash", result["detail"])

    def test_invoke_timeout_reports_failure_not_exception(self):
        fake = _FakeResumeRunner(
            raise_exc=subprocess.TimeoutExpired(cmd=["/bin/e"], timeout=60))
        result = wake_macos.invoke_resume_trigger(
            ["/bin/e"], "sess-1", "lease-1", "claim-1", "auto-1", runner=fake)
        self.assertEqual(result["outcome"], "resume_trigger_failed")


# --------------------------------------------------------------------------- #
# fire: end-to-end dispatch, duplicate-fire idempotency, crash safety.       #
# --------------------------------------------------------------------------- #


class FireTest(_WakeMacosEnvMixin, unittest.TestCase):
    def _fake_now_provider(self, value="2024-06-01T00:00:00Z"):
        return lambda: value

    def test_success_claim_invokes_resume_trigger_and_reports_resumed(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake)
        self.assertEqual(result["outcome"], "resumed")
        self.assertEqual(result["exit_code"], wake_macos.FIRE_EXIT_SUCCESS)
        self.assertEqual(len(resume_fake.calls), 1)
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "claimed")

    def test_claimant_ref_defaults_to_automation_ref(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=0)
        wake_macos.fire(session_id, "lease-1", "auto-1", ["/bin/e"],
                        now="2024-06-01T00:00:00Z", resume_runner=resume_fake)
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["claimant_ref"], "auto-1")

    def test_not_due_never_invokes_resume_trigger(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        resume_fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-01-01T00:00:00Z", resume_runner=resume_fake)
        self.assertEqual(result["outcome"], "wake_not_actionable")
        self.assertEqual(result["exit_code"],
                         scheduler.WAKE_TRIGGER_EXIT_CODES["not_due"])
        self.assertEqual(resume_fake.calls, [])

    def test_conflict_never_invokes_resume_trigger(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "someone-else",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        resume_fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            claimant_ref="auto-1", now="2024-06-01T00:00:00Z",
            resume_runner=resume_fake)
        self.assertEqual(result["outcome"], "wake_not_actionable")
        self.assertEqual(result["exit_code"],
                         scheduler.WAKE_TRIGGER_EXIT_CODES["conflict"])
        self.assertEqual(resume_fake.calls, [])

    def test_attempts_exhausted_never_invokes_resume_trigger(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        resume_fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake)
        self.assertEqual(result["outcome"], "wake_not_actionable")
        self.assertEqual(result["exit_code"],
                         scheduler.WAKE_TRIGGER_EXIT_CODES["attempts_exhausted"])
        self.assertEqual(resume_fake.calls, [])

    def test_duplicate_fires_yield_exactly_one_d_claim(self):
        """Two separate `fire` invocations for the same automation_ref (the
        real-world duplicate-launchd-fire case) each independently call
        Package D; Package D's own same-owner idempotency means the lease
        is claimed exactly once, never conflicting."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake_1 = _FakeResumeRunner(returncode=0)
        resume_fake_2 = _FakeResumeRunner(returncode=0)
        result_1 = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake_1)
        result_2 = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:05Z", resume_runner=resume_fake_2)
        self.assertEqual(result_1["d_payload"]["outcome"], "claimed")
        self.assertEqual(result_2["d_payload"]["outcome"], "already_claimed")
        self.assertEqual(result_1["outcome"], "resumed")
        self.assertEqual(result_2["outcome"], "resumed")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "claimed")

    def test_failed_resume_trigger_never_reports_success_and_lease_stays_claimed(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=1)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake)
        self.assertEqual(result["outcome"], "resume_trigger_failed")
        self.assertEqual(result["exit_code"],
                         wake_macos.FIRE_EXIT_RESUME_TRIGGER_FAILED)
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "claimed")

    def test_simulated_resume_trigger_crash_preserves_truthful_lease_state(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(raise_exc=OSError("simulated crash"))
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake)
        self.assertEqual(result["outcome"], "resume_trigger_failed")
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "claimed")
        self.assertNotEqual(result["outcome"], "resumed")

    def test_failed_d_claim_never_invokes_resume_trigger_and_lease_stays_truthful(self):
        """A failed CLAIM (as opposed to a failed resume-trigger subprocess)
        -- simulated via a lock/I/O failure inside Package D -- never
        reaches the resume-trigger step, and never claims success."""
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=0)
        with mock.patch.object(state_store, "claim_pause_lease",
                               side_effect=OSError("simulated lock timeout")):
            result = wake_macos.fire(
                session_id, "lease-1", "auto-1", ["/bin/e"],
                now="2024-06-01T00:00:00Z", resume_runner=resume_fake)
        self.assertEqual(result["outcome"], "wake_not_actionable")
        self.assertEqual(result["exit_code"],
                         scheduler.WAKE_TRIGGER_EXIT_CODES["internal_error"])
        self.assertEqual(resume_fake.calls, [])
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "unclaimed")

    def test_now_defaults_to_injected_now_provider_never_real_clock(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            resume_runner=resume_fake,
            now_provider=self._fake_now_provider("2024-06-01T00:00:00Z"))
        self.assertEqual(result["outcome"], "resumed")

    def test_explicit_now_overrides_now_provider(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=0)

        def exploding_now_provider():
            raise AssertionError("now_provider must not be called when --now given")

        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake,
            now_provider=exploding_now_provider)
        self.assertEqual(result["outcome"], "resumed")

    # ----------------------------------------------------------------- #
    # F-N01: explicit, NAMED disposition pass-through from Package D.   #
    # ----------------------------------------------------------------- #

    def test_wake_not_actionable_carries_explicit_d_disposition(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")  # not_before 00:10:00Z
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-01-01T00:00:00Z")
        self.assertEqual(result["d_disposition"], "not_due")

    def test_conflict_disposition_is_named_conflict(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        scheduler.claim(session_id, "lease-1", "someone-else",
                        now="2024-06-01T00:00:00Z", automation_ref="auto-1")
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            claimant_ref="auto-1", now="2024-06-01T00:00:00Z")
        self.assertEqual(result["d_disposition"], "conflict")

    def test_attempts_exhausted_disposition(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            state_store.record_pause_lease_failed_wake_attempt(session_id, "lease-1")
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"], now="2024-06-01T00:00:00Z")
        self.assertEqual(result["d_disposition"], "attempts_exhausted")

    def test_resumed_result_carries_no_d_disposition_key(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=0)
        result = wake_macos.fire(
            session_id, "lease-1", "auto-1", ["/bin/e"],
            now="2024-06-01T00:00:00Z", resume_runner=resume_fake)
        self.assertNotIn("d_disposition", result)


class PostClaimResumeTriggerCeilingSeamTest(_WakeMacosEnvMixin, unittest.TestCase):
    """F-MJ-02 non-vacuous pin: a lease that is successfully CLAIMED, but
    whose resume-trigger subprocess keeps failing on every subsequent fire,
    is retried on EVERY fire with no durable ceiling ever applying -- see
    the F-MJ-02 documentation block directly above `fire` in
    `cowork_wake_macos.py`. This test exists to make that exact,
    intentionally-unbounded-for-now behavior an explicit, checked fact
    rather than an undocumented assumption."""

    def test_repeated_post_claim_resume_trigger_failures_are_never_ceilinged(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        resume_fake = _FakeResumeRunner(returncode=1)

        first = wake_macos.fire(session_id, "lease-1", "auto-1", ["/bin/e"],
                                now="2024-06-01T00:00:00Z",
                                resume_runner=resume_fake)
        self.assertEqual(first["outcome"], "resume_trigger_failed")
        self.assertIn("durable_ceiling_note", first)

        attempt_count = 2 * capacity.FAILED_WAKE_ATTEMPT_CEILING
        for i in range(attempt_count):
            result = wake_macos.fire(
                session_id, "lease-1", "auto-1", ["/bin/e"],
                now="2024-06-01T00:%02d:00Z" % (i % 60),
                resume_runner=resume_fake)
            self.assertEqual(result["outcome"], "resume_trigger_failed",
                             "attempt %d unexpectedly stopped retrying" % i)
            self.assertIn("durable_ceiling_note", result)

        self.assertEqual(len(resume_fake.calls), attempt_count + 1)
        stored = state_store.read_pause_lease(session_id, "lease-1")
        self.assertEqual(stored["consumption_state"], "claimed",
                         "the claim itself must remain truthfully durable "
                         "throughout -- never expired, never reset")
        self.assertEqual(stored["failed_wake_attempts"], 0,
                         "Package D's own pre-claim counter is untouched by "
                         "post-claim resume-trigger failures -- this is "
                         "exactly the F-MJ-02 seam this test pins")


# --------------------------------------------------------------------------- #
# CLI end-to-end (still fully faked -- via injected argv only, no real       #
# subprocess/launchctl; `main`'s D delegation is real, in-process).          #
# --------------------------------------------------------------------------- #


class CliFireTest(_WakeMacosEnvMixin, unittest.TestCase):
    def test_fire_subcommand_success_via_cli(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        argv = ["fire", "--session-uuid", session_id, "--lease-id", "lease-1",
               "--automation-ref", "auto-1", "--now", "2024-06-01T00:00:00Z",
               "--resume-trigger-cmd", json.dumps(["/bin/true"])]
        lines = []
        with mock.patch.object(
                wake_macos, "_real_subprocess_runner",
                return_value=_FakeCompletedProcess(returncode=0)):
            code = wake_macos.main(argv, output=lines.append)
        self.assertEqual(code, wake_macos.FIRE_EXIT_SUCCESS)
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "resumed")

    def test_fire_subcommand_malformed_resume_trigger_cmd_json(self):
        session_id = _uuid()
        _create(session_id, lease_id="lease-1")
        argv = ["fire", "--session-uuid", session_id, "--lease-id", "lease-1",
               "--automation-ref", "auto-1", "--now", "2024-06-01T00:00:00Z",
               "--resume-trigger-cmd", "not-json"]
        lines = []
        code = wake_macos.main(argv, output=lines.append)
        self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["invalid_arguments"])

    def test_install_subcommand_via_cli_uses_real_launchctl_by_default(self):
        """Documents (rather than exercises) that the bare CLI `install`
        path uses the real launchctl runner -- this suite never actually
        invokes it; see InstallUninstallStatusTest for the fake-runner
        exercised path."""
        parser = wake_macos.build_arg_parser()
        args = parser.parse_args(["install", "--automation-ref", "auto-1",
                                  "--program-argument", "/bin/true"])
        self.assertEqual(args.command, "install")
        self.assertEqual(args.program_arguments, ["/bin/true"])


# --------------------------------------------------------------------------- #
# Structural gates: adapter-absence safety, no eligibility/lock              #
# reimplementation, no Package E import, exact four-path allowlist.          #
# --------------------------------------------------------------------------- #


class StructuralGatesTest(unittest.TestCase):
    def _module_path(self):
        return os.path.join(_HERE, "cowork_wake_macos.py")

    def _tree(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=self._module_path())

    def _top_level_imports(self):
        names = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_imports_only_expected_modules(self):
        imported = self._top_level_imports()
        self.assertEqual(
            imported,
            {"argparse", "json", "os", "plistlib", "re", "subprocess", "sys",
            "datetime", "cowork_capacity_scheduler"})

    def test_never_imports_cowork_state_directly(self):
        """No eligibility/locking/storage reimplementation: this module
        talks ONLY to Package D, never directly to Package B."""
        self.assertNotIn("cowork_state", self._top_level_imports())

    def test_never_imports_a_control_plane_module(self):
        self.assertNotIn("cowork_control_plane", self._top_level_imports())

    def test_source_contains_no_lock_primitive(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn(".flock(", source)
        self.assertNotIn("import fcntl", source)

    def test_f_local_exit_codes_disjoint_from_d_exit_codes(self):
        """F-N02: every F-local exit-code value (`FIRE_EXIT_CODES`,
        `LAUNCHCTL_EXIT_CODES`) must never collide with a Package D
        `WAKE_TRIGGER_EXIT_CODES` value -- EXCEPT the universally shared
        `0 == success` convention every one of these mappings uses. A
        collision on any of D's OTHER values (1-5) would let an F-local
        failure be misread as a specific Package D disposition it never
        actually reported."""
        d_values = set(scheduler.WAKE_TRIGGER_EXIT_CODES.values())
        f_values = (set(wake_macos.FIRE_EXIT_CODES.values())
                   | set(wake_macos.LAUNCHCTL_EXIT_CODES.values()))
        overlap = d_values & f_values
        self.assertEqual(overlap, {0},
                         "F-local exit codes must only ever share the "
                         "universal 0-means-success value with Package D's "
                         "own mapping, got overlap: %r" % overlap)


class AdapterAbsenceIsSafeTest(unittest.TestCase):
    """D-N-style absence proof: neither Package D nor Package B imports
    this module (or its manual-adapter sibling), so durable recovery is
    fully reachable with zero involvement from either Package F module."""

    def _imports(self, filename):
        path = os.path.join(_HERE, filename)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_package_d_does_not_import_package_f(self):
        imported = self._imports("cowork_capacity_scheduler.py")
        self.assertNotIn("cowork_wake_macos", imported)
        self.assertNotIn("cowork_wake_manual", imported)

    def test_package_b_does_not_import_package_f(self):
        imported = self._imports("cowork_state.py")
        self.assertNotIn("cowork_wake_macos", imported)
        self.assertNotIn("cowork_wake_manual", imported)

    def test_package_d_wake_decision_functions_fully_without_package_f(self):
        """A concrete demonstration, not merely an import check: Package
        D's own wake-trigger contract works end to end with this module
        never imported into the call path at all (only Package D/A/B are
        touched)."""
        root = tempfile.mkdtemp()
        try:
            old_root = os.environ.get("COWORK_SESSIONS_ROOT")
            os.environ["COWORK_SESSIONS_ROOT"] = root
            try:
                session_id = _uuid()
                _create(session_id, lease_id="lease-1")
                lines = []
                code = scheduler.run_wake_trigger(
                    ["--session-uuid", session_id, "--lease-id", "lease-1",
                    "--claimant-ref", "worker-a", "--automation-ref", "auto-1",
                    "--now", "2024-06-01T00:00:00Z"], output=lines.append)
                self.assertEqual(code, scheduler.WAKE_TRIGGER_EXIT_CODES["success"])
            finally:
                if old_root is None:
                    os.environ.pop("COWORK_SESSIONS_ROOT", None)
                else:
                    os.environ["COWORK_SESSIONS_ROOT"] = old_root
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
