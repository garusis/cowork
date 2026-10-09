"""Neutral offline contracts for isolated Claude transport probes."""

import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import cowork_bridge as bridge
import cowork_probe_cache as cache
import cowork_action_policy as action_policy


class ProbeIsolationTest(unittest.TestCase):
    class Trace:
        session_uuid = "synthetic-probe"

        def __init__(self):
            self.events = []

        def event(self, name, **fields):
            self.events.append(dict(fields, event=name))

    def setUp(self):
        previous = bridge.set_nested_guard_active(False)
        self.addCleanup(bridge.set_nested_guard_active, previous)

    def assert_isolated(self, command):
        self.assertNotIn("--append-system-prompt-file", command)
        self.assertNotIn("/synthetic/engineering-role.md", command)
        self.assertEqual(command[command.index("--system-prompt") + 1],
                         bridge.CLAUDE_PROBE_PROMPT)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--disable-slash-commands", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--mcp-config") + 1],
                         bridge.CLAUDE_EMPTY_MCP_CONFIG_PATH)
        self.assertIn("Agent", command)
        self.assertIn("Task", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertEqual(command[command.index("--model") + 1], "model-pin")
        self.assertEqual(command[command.index("--effort") + 1], "high")

    def test_unguarded_probe_isolated_for_all_permission_shapes(self):
        for mode in ("plan", "implement"):
            for yolo in (False, True):
                with self.subTest(mode=mode, yolo=yolo):
                    captured = []

                    def spawn(command, stdin):
                        captured.append(command)
                        self.assertEqual(json.loads(stdin)["message"]["content"],
                                         [{"type": "text", "text": "ping"}])
                        return [{"type": "result", "subtype": "success"}]

                    ok, alert = bridge.probe_claude_stream_json(
                        spawn, mode=mode, yolo=yolo,
                        role_prompt_file="/synthetic/engineering-role.md",
                        model="model-pin", effort="high")
                    self.assertTrue(ok, alert)
                    self.assert_isolated(captured[0])

    def test_guarded_probe_retains_boundary_hooks_and_no_role_outputs(self):
        bridge.set_nested_guard_active(True)
        runtime = {
            "settings_path": "/synthetic/guard/settings.json",
            "delegation_allowed": False,
            "scope": action_policy.OwnedScope(),
            "env": {"TMPDIR": "/synthetic/guard/tmp"},
            "broker": None, "profile": None, "protected_paths": (),
        }
        captured = []

        def spawn(command, _stdin):
            captured.append(command)
            return [{"type": "result", "subtype": "success"}]

        with mock.patch.object(bridge, "_guard_runtime",
                               return_value=runtime) as guard, \
                mock.patch.object(bridge, "_require_controller_auth"), \
                mock.patch.object(bridge, "_stamp_guard_parent_work") as stamp, \
                mock.patch.object(bridge, "_close_guard_runtime") as close, \
                mock.patch.object(bridge, "kernel_write_boundary",
                                  side_effect=lambda scope, argv,
                                  **kw: {"available": True,
                                         "argv": ["sandbox-wrapper"] + argv}):
            ok, alert = bridge.probe_claude_stream_json(
                spawn, role_prompt_file="/synthetic/engineering-role.md",
                model="model-pin", effort="high")
        self.assertTrue(ok, alert)
        self.assert_isolated(captured[0])
        self.assertEqual(captured[0][0], "sandbox-wrapper")
        self.assertIn("TMPDIR=/synthetic/guard/tmp", captured[0])
        self.assertIn(runtime["settings_path"], captured[0])
        self.assertEqual(guard.call_args.kwargs["declared_outputs"], ())
        self.assertFalse(guard.call_args.kwargs["repo_writable"])
        stamp.assert_called_once()
        close.assert_called_once_with(runtime)

    def test_ordinary_role_command_unchanged(self):
        command = bridge.build_claude_command(
            "/synthetic/engineering-role.md", "plan", True)
        self.assertEqual(command[command.index("--append-system-prompt-file") + 1],
                         "/synthetic/engineering-role.md")
        for flag in ("--tools", "--system-prompt", "--disable-slash-commands",
                     "--setting-sources"):
            self.assertNotIn(flag, command)

    def test_native_usage_provenance_and_error_over_success(self):
        usage = {"input_tokens": 13, "output_tokens": 2,
                 "cache_read_input_tokens": 17,
                 "cache_creation_input_tokens": 5}
        for failure in (None, "server_error", "authentication_failed"):
            with self.subTest(failure=failure):
                trace = self.Trace()
                report = {}
                events = [{"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "pong"}]}}]
                if failure:
                    events.append({"type": "assistant",
                                   "isApiErrorMessage": True, "error": failure,
                                   "message": {"content": [
                                       {"type": "text", "text": "rejected"}]}})
                events.append({"type": "result", "subtype": "success",
                               "usage": usage})
                with mock.patch.object(cache, "resolve_claude_path",
                                       return_value="/synthetic/claude"), \
                        mock.patch.object(cache, "cache_hit", return_value=False), \
                        mock.patch.object(cache, "cache_store") as store:
                    ok, alert = bridge.probe_claude_stream_json(
                        lambda *_: events, trace=trace, report=report,
                        cache_enabled=True, version_fn=lambda _: "synthetic-version")
                self.assertEqual(ok, failure is None)
                self.assertEqual(report["live_auth_proven"], failure is None)
                self.assertEqual(store.call_count, int(failure is None))
                if failure == "authentication_failed":
                    self.assertEqual(report["controller_outcome"], failure)
                    self.assertIn("No role turn was launched", alert)
                start = next(e for e in trace.events
                             if e["event"] == "controller.probe.start")
                self.assertFalse(start["role_prompt_loaded"])
                self.assertFalse(start["tools_available"])
                self.assertEqual(start["system_prompt_sha256"], hashlib.sha256(
                    bridge.CLAUDE_PROBE_PROMPT.encode()).hexdigest())
                end = next(e for e in trace.events
                           if e["event"] == "controller.probe.end")
                self.assertEqual(end["usage_native"], usage)
                self.assertEqual(end["usage"], usage)
                self.assertEqual(end["usage_source"], "claude.result.usage")
                self.assertEqual(end["usage_interpretation"],
                                 "native_aggregate_not_fresh_generation")
                self.assertNotIn("fresh_generation_tokens", end)
                self.assertEqual(start["work_id"], end["work_id"])

    def test_cache_contract_and_pins_invalidate_legacy_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.json")
            common = ("/synthetic/claude", "version", "/synthetic/role.md",
                      "plan", True, False)
            old = cache.probe_cache_key(*common)
            contract = json.dumps(bridge.CLAUDE_PROBE_FLAGS)
            baseline = cache.probe_cache_key(*common, probe_contract=contract)
            self.assertNotEqual(old, baseline)
            for pins in ({"model": "other"}, {"effort": "high"},
                         {"guarded": True}, {"probe_contract": "changed"}):
                options = dict(probe_contract=contract)
                options.update(pins)
                self.assertNotEqual(baseline, cache.probe_cache_key(*common, **options))
            cache.cache_store(old, path=path)
            calls = []
            with mock.patch.object(cache, "resolve_claude_path", return_value=common[0]):
                for _ in range(2):
                    report = {}
                    trace = self.Trace()
                    ok, alert = bridge.probe_claude_stream_json(
                        lambda *args: calls.append(args) or [{"type": "result"}],
                        role_prompt_file=common[2], report=report, trace=trace,
                        cache_enabled=True, cache_path=path,
                        version_fn=lambda _: common[1])
                    self.assertTrue(ok, alert)
                self.assertEqual(len(calls), 1)
                self.assertTrue(report["cache_hit"])
                self.assertFalse(report["live_auth_proven"])
                event = next(e for e in trace.events
                             if e["event"] == "controller.probe.cache_hit")
                self.assertFalse(event["auth_revalidated"])
                self.assertFalse(event["live_auth_proven"])

    def test_missing_native_usage_stays_unknown(self):
        trace = self.Trace()
        ok, alert = bridge.probe_claude_stream_json(
            lambda *_: [{"type": "result", "subtype": "success"}], trace=trace)
        self.assertTrue(ok, alert)
        end = next(e for e in trace.events if e["event"] == "controller.probe.end")
        self.assertIsNone(end["usage"])
        self.assertIsNone(end["usage_native"])
        self.assertIsNone(end["usage_source"])

    def test_probe_call_cache_identity_tracks_pins_and_prompt_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.json")
            calls = []
            spawn = lambda *args: calls.append(args) or [{"type": "result"}]
            with mock.patch.object(cache, "resolve_claude_path",
                                   return_value="/synthetic/claude"):
                for pins in ({}, {"model": "other"}, {"effort": "high"}):
                    ok, alert = bridge.probe_claude_stream_json(
                        spawn, cache_enabled=True, cache_path=path,
                        version_fn=lambda _: "version", **pins)
                    self.assertTrue(ok, alert)
                with mock.patch.object(
                        bridge, "CLAUDE_PROBE_FLAGS",
                        bridge.CLAUDE_PROBE_FLAGS + ("--synthetic-contract",)):
                    ok, alert = bridge.probe_claude_stream_json(
                        spawn, cache_enabled=True, cache_path=path,
                        version_fn=lambda _: "version")
                    self.assertTrue(ok, alert)
            self.assertEqual(len(calls), 4)

    def test_error_result_cannot_be_overridden_by_assistant(self):
        report = {}
        ok, alert = bridge.probe_claude_stream_json(
            lambda *_: [
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "pong"}]}},
                {"type": "result", "is_error": True,
                 "subtype": "error_during_execution", "usage": {"output_tokens": 3}},
            ], report=report)
        self.assertFalse(ok)
        self.assertFalse(report["live_auth_proven"])
        self.assertIn("No role turn was launched", alert)

    def test_missing_guarded_login_metadata_does_not_spawn(self):
        bridge.set_nested_guard_active(True)
        spawn = mock.Mock()
        report = {}
        runtime = {"synthetic": True}
        with mock.patch.object(bridge, "_guard_runtime", return_value=runtime), \
                mock.patch.object(bridge, "_require_controller_auth",
                                  side_effect=RuntimeError("metadata absent")), \
                mock.patch.object(bridge, "_close_guard_runtime") as close:
            ok, alert = bridge.probe_claude_stream_json(spawn, report=report)
        self.assertFalse(ok)
        self.assertFalse(report["login_metadata_present"])
        self.assertFalse(report["live_auth_proven"])
        self.assertIn("No model turn was launched", alert)
        spawn.assert_not_called()
        close.assert_called_once_with(runtime)
