#!/usr/bin/env python3
"""Tests for the cowork foundation + scout role.

Pure functions (flag assembly, framing, parsing, probe, flow) are tested with
fakes; no real claude/codex CLI is spawned. Run:

    python3 -m unittest scripts/test_cowork.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402


class FlagAssemblyTest(unittest.TestCase):
    def test_claude_mode_flags(self):
        self.assertEqual(bridge.claude_mode_flags("plan", True),
                         ["--permission-mode", "plan"])
        self.assertEqual(bridge.claude_mode_flags("plan", False),
                         ["--permission-mode", "plan"])
        self.assertEqual(bridge.claude_mode_flags("implement", True),
                         ["--dangerously-skip-permissions"])
        self.assertEqual(bridge.claude_mode_flags("implement", False),
                         ["--permission-mode", "acceptEdits"])

    def test_codex_mode_flags(self):
        # codex exec has no --ask-for-approval; approval is governed by sandbox.
        self.assertEqual(bridge.codex_mode_flags("plan", True),
                         ["--sandbox", "read-only"])
        self.assertEqual(bridge.codex_mode_flags("implement", True),
                         ["--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(bridge.codex_mode_flags("implement", False),
                         ["--sandbox", "workspace-write"])

    def test_build_claude_command_is_duplex(self):
        cmd = bridge.build_claude_command("roles/scout.md", "plan", True)
        for flag in ("-p", "--input-format", "stream-json", "--output-format",
                     "--verbose", "--replay-user-messages",
                     "--append-system-prompt-file"):
            self.assertIn(flag, cmd)
        self.assertIn("roles/scout.md", cmd)
        self.assertEqual(cmd[0], "claude")
        # interactive question tool is blocked (auto-"skipped" in headless -p)
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("AskUserQuestion", cmd)

    def test_build_codex_command(self):
        cmd = bridge.build_codex_command("PROMPT", "plan", True)
        self.assertEqual(cmd[:4], ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[-1], "PROMPT")

    def test_codex_resume_uses_explicit_id_never_last(self):
        cmd = bridge.build_codex_resume_command("thread-abc", "next")
        self.assertEqual(
            cmd,
            ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
             "thread-abc", "next"],
        )
        self.assertNotIn("--last", cmd)
        # resume rejects --sandbox (policy inherited from the original session).
        self.assertNotIn("--sandbox", cmd)


class PreflightTest(unittest.TestCase):
    def test_python_floor(self):
        ok, alert = preflight.check_python((3, 9, 6))
        self.assertTrue(ok)
        self.assertIsNone(alert)
        ok, alert = preflight.check_python((3, 8, 18))
        self.assertFalse(ok)
        self.assertIn("3.8.18", alert)

    def test_required_controllers_dedup(self):
        cfg = {
            "scout": {"controller": "claude"},
            "planner": {"controller": "claude"},
            "advisor": {"controller": "codex"},
        }
        self.assertEqual(preflight.required_controllers(cfg), ["claude", "codex"])

    def test_check_controllers_missing_and_present(self):
        ok, alerts = preflight.check_controllers(["claude"], which=lambda c: None)
        self.assertFalse(ok)
        self.assertIn("@anthropic-ai/claude-code", alerts[0])

        ok, alerts = preflight.check_controllers(["codex"], which=lambda c: None)
        self.assertIn("@openai/codex", alerts[0])

        ok, alerts = preflight.check_controllers(
            ["claude"], which=lambda c: "/usr/bin/" + c
        )
        self.assertTrue(ok)
        self.assertEqual(alerts, [])

    def test_preflight_aggregates_non_interactive(self):
        cfg = {"scout": {"controller": "codex"}}
        ok, alerts = preflight.preflight(
            cfg, version_info=(3, 8, 0), which=lambda c: None, interactive=False
        )
        self.assertFalse(ok)
        # python alert + codex alert (no gum required when non-interactive)
        self.assertEqual(len(alerts), 2)

    def test_preflight_requires_gum_only_when_interactive(self):
        cfg = {"scout": {"controller": "claude"}}
        present = lambda c: "/bin/" + c if c == "claude" else None
        ok, alerts = preflight.preflight(cfg, which=present, interactive=False)
        self.assertTrue(ok)
        ok, alerts = preflight.preflight(cfg, which=present, interactive=True)
        self.assertFalse(ok)
        self.assertTrue(any("gum" in a for a in alerts))


class GumSeamTest(unittest.TestCase):
    """The gum boundary is tested with a fake runner; gum itself is never run."""

    def _runner(self, script):
        """script: list of (returncode, stdout) returned in call order; also
        records the argv of each call in self.calls."""
        self.calls = []
        it = iter(script)

        def run(argv, input_text=None):
            self.calls.append(argv)
            return next(it)
        return run

    def test_gum_choose_builds_argv_and_parses(self):
        run = self._runner([(0, "scout\nadvisor\n")])
        rc, picks = cowork.gum_choose(
            ["scout", "advisor", "planner"], selected=["scout", "advisor"],
            header="Team", multi=True, run=run,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(picks, ["scout", "advisor"])
        argv = self.calls[0]
        self.assertEqual(argv[:2], ["gum", "choose"])
        self.assertIn("--no-limit", argv)
        self.assertIn("--selected=scout,advisor", argv)
        self.assertIn("--header=Team", argv)

    def test_gum_choose_one_returns_first_or_default(self):
        run = self._runner([(0, "codex\n")])
        self.assertEqual(
            cowork.gum_choose_one(["claude", "codex"], default="claude", run=run),
            "codex",
        )
        run = self._runner([(130, "")])  # cancelled
        self.assertEqual(
            cowork.gum_choose_one(["claude", "codex"], default="claude", run=run),
            "claude",
        )

    def test_select_team_interactive(self):
        run = self._runner([(0, "scout\nplanner\n")])
        self.assertEqual(cowork.select_team_interactive(run=run),
                         ["scout", "planner"])
        run = self._runner([(130, "")])  # cancel -> empty
        self.assertEqual(cowork.select_team_interactive(run=run), [])

    def test_configure_roles_interactive_accepts_defaults(self):
        # single gum choose; picking "use these defaults" -> keep defaults.
        run = self._runner([(0, "use these defaults\n")])
        cfg = cowork.configure_roles_interactive(["scout", "advisor"], run=run)
        self.assertEqual(cfg["scout"], cowork.DEFAULTS["scout"])
        self.assertEqual(len(self.calls), 1)
        # the defaults render as the gum menu header (so gum clears them on exit)
        header_arg = [a for a in self.calls[0] if a.startswith("--header=")][0]
        self.assertIn("Default tool config:", header_arg)
        self.assertIn("scout", header_arg)
        self.assertIn("implement", header_arg)

    def test_configure_roles_interactive_customizes(self):
        run = self._runner([
            (0, "customize\n"), (0, "scout\n"), (0, "codex\n"),
            (0, "no-yolo\n"), (0, "implement\n"),
        ])
        cfg = cowork.configure_roles_interactive(["scout"], run=run)
        self.assertEqual(cfg["scout"]["controller"], "codex")
        self.assertFalse(cfg["scout"]["yolo"])
        self.assertEqual(cfg["scout"]["mode"], "implement")

    def test_format_config_summary_aligned(self):
        cfg = cowork.default_config(["scout", "advisor", "builder"])
        text = cowork.format_config_summary(cfg)
        self.assertIn("scout", text)
        # column header row is present
        for label in ("role", "controller", "permissions", "mode"):
            self.assertIn(label, text)
        self.assertIn("no-yolo", cowork.format_config_summary(
            {"scout": {"controller": "claude", "yolo": False, "mode": "plan"}}))

    def test_gum_write(self):
        run = self._runner([(0, "ctx line one\nctx line two\n")])
        self.assertEqual(cowork.gum_write(header="Context", run=run),
                         "ctx line one\nctx line two")


class ConfigTest(unittest.TestCase):
    def test_default_config_matches_defaults(self):
        cfg = cowork.default_config(cowork.ROLES)
        # Roles default to implement mode (guardrailed by role spec, not plan).
        self.assertEqual(cfg["scout"],
                         {"controller": "claude", "yolo": True, "mode": "implement"})
        for role in cowork.ROLES:
            self.assertEqual(cfg[role]["mode"], "implement")

    def test_apply_config_override(self):
        cfg = cowork.default_config(["scout"])
        ok, err = cowork.apply_config_override(
            cfg, "scout", ["codex", "no-yolo", "implement"])
        self.assertTrue(ok)
        self.assertEqual(
            cfg["scout"], {"controller": "codex", "yolo": False, "mode": "implement"})
        ok, _ = cowork.apply_config_override(cfg, "ghost", ["claude"])
        self.assertFalse(ok)
        ok, _ = cowork.apply_config_override(cfg, "scout", ["bogus"])
        self.assertFalse(ok)


class ArgsPathTest(unittest.TestCase):
    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_parse_team(self):
        selected, err = cowork.parse_team("advisor,scout")
        self.assertIsNone(err)
        self.assertEqual(selected, ["scout", "advisor"])  # canonical order
        selected, err = cowork.parse_team("scout,ghost")
        self.assertIsNotNone(err)

    def test_apply_config_args(self):
        cfg = cowork.default_config(["scout", "advisor"])
        ok, err = cowork.apply_config_args(cfg, ["scout=codex,no-yolo,implement"])
        self.assertTrue(ok)
        self.assertEqual(
            cfg["scout"], {"controller": "codex", "yolo": False, "mode": "implement"})
        ok, err = cowork.apply_config_args(cfg, ["scoutcodex"])  # no '='
        self.assertFalse(ok)

    def test_resolve_context_text_and_file(self):
        args = self._args(["--context", "hello"])
        self.assertEqual(cowork.resolve_context(args), "hello")
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("from file")
            path = fh.name
        try:
            args = self._args(["--context-file", path])
            self.assertEqual(cowork.resolve_context(args), "from file")
        finally:
            os.unlink(path)

    def test_run_flow_non_interactive_reaches_scout(self):
        captured = {}

        def fake_run_scout(config, context, selected, io_in=None, io_out=None,
                           resume_id=None, on_session=None, intel_path=None):
            captured["config"] = config
            captured["context"] = context
            captured["selected"] = selected
            captured["intel_path"] = intel_path
            return 0

        args = self._args(
            ["--team", "scout,advisor",
             "--config", "scout=codex,no-yolo,implement",
             "--context", "do the thing", "--no-session"])
        out = io.StringIO()
        rc = cowork.run_flow(
            args, io_out=out,
            which=lambda c: "/bin/" + c,  # everything present
            run_scout_fn=fake_run_scout,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["selected"], ["scout", "advisor"])
        self.assertEqual(captured["context"], "do the thing")
        self.assertEqual(captured["config"]["scout"]["controller"], "codex")

    def test_run_flow_non_interactive_skips_gum_in_preflight(self):
        # claude present, gum absent: non-interactive must still pass preflight.
        args = self._args(["--team", "advisor", "--context", "x", "--no-session"])
        out = io.StringIO()
        rc = cowork.run_flow(
            args, io_out=out,
            which=lambda c: None if c == "gum" else "/bin/" + c,
        )
        # advisor (no scout) -> "not selected" note, rc 0, gum never required
        self.assertEqual(rc, 0)
        self.assertIn("scout not selected", out.getvalue())


class StateStoreTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_save_load_roundtrip_and_has_config(self):
        path = self._tmp()
        self.assertIsNone(state_store.load(path))
        cfg = cowork.default_config(["scout", "advisor"])
        state_store.save_config(path, ["scout", "advisor"], cfg)
        loaded = state_store.load(path)
        self.assertTrue(state_store.has_config(loaded))
        self.assertEqual(loaded["team"], ["scout", "advisor"])
        self.assertEqual(loaded["config"]["scout"]["controller"], "claude")

    def test_role_session_roundtrip_and_controller_match(self):
        path = self._tmp()
        state_store.save_config(path, ["scout"], cowork.default_config(["scout"]))
        state_store.save_role_session(path, "scout", "claude", "uuid-123")
        loaded = state_store.load(path)
        self.assertEqual(
            state_store.get_role_session(loaded, "scout", "claude"), "uuid-123")
        # controller mismatch -> no resume id
        self.assertIsNone(
            state_store.get_role_session(loaded, "scout", "codex"))

    def test_ensure_session_mints_and_persists_uuid_once(self):
        path = self._tmp()
        s1 = state_store.ensure_session(path, None, "fixed-uuid")
        self.assertEqual(state_store.get_session_uuid(s1), "fixed-uuid")
        self.assertEqual(
            state_store.get_session_uuid(state_store.load(path)), "fixed-uuid")
        # a second call with a different candidate must not overwrite it
        s2 = state_store.ensure_session(path, state_store.load(path), "other")
        self.assertEqual(state_store.get_session_uuid(s2), "fixed-uuid")

    def test_read_status(self):
        path = self._tmp()
        self.assertIsNone(state_store.read_status(path))  # missing
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write('{"session":"x","role":"scout","status":"needs_input","result":{}}')
        self.assertEqual(state_store.read_status(path), "needs_input")
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertIsNone(state_store.read_status(path))

    def test_load_rejects_incompatible_version(self):
        path = self._tmp()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write('{"version": 999, "team": ["scout"], "config": {}}')
        self.assertIsNone(state_store.load(path))


class SessionFlowTest(unittest.TestCase):
    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_config_saved_then_reused_and_session_resumed(self):
        spath = self._tmp_session()

        def fake_scout(config, context, selected, io_in=None, io_out=None,
                      resume_id=None, on_session=None, intel_path=None):
            fake_scout.last_resume = resume_id
            fake_scout.last_intel = intel_path
            if on_session and resume_id is None:
                on_session("claude", "sess-abc")  # simulate id capture
            return 0
        fake_scout.last_resume = "unset"
        fake_scout.last_intel = None

        # Run 1: choose config via args, scout saves its session id.
        rc = cowork.run_flow(
            self._args(["--team", "scout", "--config", "scout=claude,yolo,plan",
                        "--context", "first", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        self.assertIsNone(fake_scout.last_resume)
        saved = state_store.load(spath)
        self.assertTrue(state_store.has_config(saved))
        self.assertEqual(state_store.get_role_session(saved, "scout", "claude"),
                         "sess-abc")
        # a cowork session uuid is minted, persisted, and names the intel file
        suid = state_store.get_session_uuid(saved)
        self.assertTrue(suid)
        self.assertIn("scout.intel.%s.json" % suid, fake_scout.last_intel)

        # Run 2: only context + session file -> config reused, session resumed.
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--context", "second", "--session-file", spath]),
            io_out=out, which=lambda c: "/bin/" + c, run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        self.assertIn("using saved session config", out.getvalue())
        self.assertEqual(fake_scout.last_resume, "sess-abc")
        # session uuid is stable across runs
        self.assertEqual(state_store.get_session_uuid(state_store.load(spath)), suid)
        self.assertIn(suid, fake_scout.last_intel)

    def test_no_session_writes_nothing(self):
        spath = self._tmp_session()
        cowork.run_flow(
            self._args(["--team", "scout", "--context", "x",
                        "--session-file", spath, "--no-session"]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0)
        self.assertFalse(os.path.exists(spath))


class ProbeTest(unittest.TestCase):
    def test_probe_accepts_assistant(self):
        def spawn(cmd, stdin):
            return [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "pong"}]}}]
        ok, alert = bridge.probe_claude_stream_json(spawn)
        self.assertTrue(ok)
        self.assertIsNone(alert)

    def test_probe_rejects_unsupported(self):
        def spawn(cmd, stdin):
            return [{"type": "other"}]
        ok, alert = bridge.probe_claude_stream_json(spawn)
        self.assertFalse(ok)
        self.assertIn("stream-json", alert)

    def test_probe_spawn_failure(self):
        def spawn(cmd, stdin):
            raise OSError("claude: not found")
        ok, alert = bridge.probe_claude_stream_json(spawn)
        self.assertFalse(ok)
        self.assertIn("not found", alert)


class FramingTest(unittest.TestCase):
    def test_encode_user_message(self):
        import json
        line = bridge.encode_user_message("hello")
        self.assertTrue(line.endswith("\n"))
        obj = json.loads(line)
        self.assertEqual(obj["type"], "user")
        self.assertEqual(obj["message"]["role"], "user")
        self.assertEqual(obj["message"]["content"][0]["text"], "hello")

    def test_parse_claude_assistant_and_result(self):
        a = bridge.parse_claude_event(
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hi"}]}})
        self.assertEqual(a, {"kind": "assistant", "text": "hi"})
        r = bridge.parse_claude_event(
            {"type": "result", "subtype": "success", "result": "done"})
        self.assertEqual(r["kind"], "result")
        self.assertFalse(r["is_error"])

    def test_parse_claude_partial_text_delta(self):
        ev = {"type": "stream_event",
              "event": {"delta": {"type": "text_delta", "text": "hel"}}}
        self.assertEqual(bridge.parse_claude_event(ev),
                         {"kind": "partial", "text": "hel"})
        # non-text deltas are partials with no text
        ev2 = {"type": "stream_event", "event": {"delta": {"type": "input_json"}}}
        self.assertEqual(bridge.parse_claude_event(ev2)["kind"], "partial")

    def test_speaker_label(self):
        self.assertEqual(bridge.speaker_label("scout"), "scout › ")
        self.assertEqual(bridge.USER_LABEL, "you › ")

    def test_claude_command_streams_partials(self):
        cmd = bridge.build_claude_command("roles/scout.md", "implement", True)
        self.assertIn("--include-partial-messages", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)  # implement+yolo

    def test_parse_codex_events(self):
        ts = bridge.parse_codex_event({"type": "thread.started", "thread_id": "T1"})
        self.assertEqual(ts, {"kind": "thread_started", "thread_id": "T1"})
        msg = bridge.parse_codex_event(
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": "context map"}})
        self.assertEqual(msg, {"kind": "message", "text": "context map"})

    def test_capture_thread_id(self):
        events = [
            {"type": "turn.started"},
            {"type": "thread.started", "thread_id": "T-42"},
            {"type": "turn.completed"},
        ]
        self.assertEqual(bridge.capture_thread_id(events), "T-42")
        self.assertIsNone(bridge.capture_thread_id([{"type": "turn.started"}]))


class DenialTest(unittest.TestCase):
    def test_claude_permission_denied(self):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": [{"type": "text", "text": "Permission denied for Bash"}]}]}}
        self.assertEqual(bridge.parse_claude_event(ev)["kind"], "denied")

    def test_codex_rejected_item(self):
        ev = {"type": "item.completed",
              "item": {"type": "command_execution", "status": "rejected",
                       "text": "rm -rf"}}
        self.assertEqual(bridge.parse_codex_event(ev)["kind"], "denied")

    def test_codex_error(self):
        ev = {"type": "error", "message": "sandbox violation"}
        parsed = bridge.parse_codex_event(ev)
        self.assertEqual(parsed["kind"], "error")
        self.assertIn("sandbox", parsed["text"])


class FallthroughTest(unittest.TestCase):
    def test_brief_with_planner(self):
        brief = cowork.assemble_scout_brief(
            ["scout", "planner"], ".cowork/scout.intel.S.json")
        self.assertIn("do NOT produce a plan", brief)
        self.assertIn(".cowork/scout.intel.S.json", brief)
        self.assertIn("ONLY write target", brief)

    def test_brief_without_planner(self):
        brief = cowork.assemble_scout_brief(
            ["scout", "advisor"], ".cowork/scout.intel.S.json")
        self.assertIn("lightweight plan", brief)

    def test_brief_requires_json(self):
        brief = cowork.assemble_scout_brief(["scout"], "/tmp/x.json")
        self.assertIn("JSON", brief)

    def test_scout_intel_path(self):
        self.assertEqual(
            cowork.scout_intel_path(".cowork", "abc-123"),
            ".cowork/scout.intel.abc-123.json")

    def test_codex_prompt_includes_all_parts(self):
        prompt = cowork.assemble_codex_prompt("ROLE", "TEAM", "CTX")
        self.assertIn("ROLE", prompt)
        self.assertIn("TEAM", prompt)
        self.assertIn("CTX", prompt)


class RunScoutTest(unittest.TestCase):
    def test_run_scout_claude_probe_fail_aborts(self):
        config = {"scout": {"controller": "claude", "yolo": True, "mode": "plan"}}
        out = io.StringIO()

        def bad_spawn(cmd, stdin):
            return [{"type": "other"}]

        rc = cowork.run_scout(config, "ctx", ["scout", "planner"],
                              io_in=io.StringIO(""), io_out=out,
                              claude_spawn=bad_spawn)
        self.assertEqual(rc, 1)
        self.assertIn("cowork:", out.getvalue())


class InterruptTest(unittest.TestCase):
    def _main_with(self, exc):
        import contextlib
        orig = cowork.run_flow
        cowork.run_flow = lambda *a, **k: (_ for _ in ()).throw(exc)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cowork.main(["--team", "scout"])
            return rc, err.getvalue()
        finally:
            cowork.run_flow = orig

    def test_keyboard_interrupt_exits_130(self):
        rc, err = self._main_with(KeyboardInterrupt())
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", err)

    def test_eof_exits_130(self):
        rc, err = self._main_with(EOFError())
        self.assertEqual(rc, 130)

    def test_terminate_kills_live_proc(self):
        class FakeProc:
            def __init__(self):
                self.state = "running"
                self.terminated = False
            def poll(self):
                return None if self.state == "running" else 0
            def terminate(self):
                self.terminated = True
                self.state = "done"
            def wait(self, timeout=None):
                return 0
            def kill(self):
                self.state = "done"
        p = FakeProc()
        bridge._terminate(p)
        self.assertTrue(p.terminated)

    def test_terminate_noop_when_already_exited(self):
        class Dead:
            def poll(self):
                return 0
            def terminate(self):
                raise AssertionError("should not terminate an exited process")
        bridge._terminate(Dead())  # must not raise


class ScoutLoopTest(unittest.TestCase):
    """Drive _scout_loop with a fake session that writes intel statuses."""

    def _intel(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def _session(self, intel_path, statuses):
        test = self

        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                os.makedirs(os.path.dirname(intel_path), exist_ok=True)
                with open(intel_path, "w") as fh:
                    json.dump({"session": "X", "role": "scout",
                               "status": st, "result": {}}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def test_needs_input_then_review_then_approve(self):
        intel = self._intel()
        sess = self._session(intel, ["needs_input", "ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="ctx",
            io_in=io.StringIO("answer 1\n\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "answer 1"])
        self.assertTrue(sess.closed)
        text = out.getvalue()
        self.assertIn("scout needs your input", text)
        self.assertIn("ready for review", text)
        self.assertIn("scout finished", text)

    def test_review_revise_then_approve(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="",
            io_in=io.StringIO("more feedback\n\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "more feedback"])
        self.assertTrue(sess.closed)

    def test_blank_while_working_aborts(self):
        intel = self._intel()
        sess = self._session(intel, ["needs_input"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="", io_in=io.StringIO("\n"), io_out=out)
        self.assertEqual(sess.sent, ["seed"])  # aborted at the first prompt
        self.assertTrue(sess.closed)


class SessionClassTest(unittest.TestCase):
    def test_claude_session_streams_and_reports_session_id(self):
        import unittest.mock as mock

        class FakeStdin:
            def __init__(self):
                self.data = []

            def write(self, s):
                self.data.append(s)

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)
                self.stdin = FakeStdin()

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        lines = [
            json.dumps({"type": "stream_event",
                        "event": {"delta": {"type": "text_delta", "text": "hi"}}}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "hi", "session_id": "S1"}),
        ]
        got = {}
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)):
            out = io.StringIO()
            s = bridge.ClaudeSession(
                "roles/scout.md", "implement", True, io_out=out,
                on_session_id=lambda i: got.setdefault("id", i))
            s.send("hello")
        self.assertIn("scout › hi", out.getvalue())
        self.assertEqual(got.get("id"), "S1")
        self.assertEqual(s.proc.stdin.data[0],
                         bridge.encode_user_message("hello"))

    def test_claude_session_separates_text_blocks(self):
        import unittest.mock as mock

        class FakeStdin:
            def write(self, s):
                pass

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)
                self.stdin = FakeStdin()

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        lines = [
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_start", "content_block": {"type": "text"}}}),
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": "first."}}}),
            # tool use happens, then a new text block resumes narration
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_start", "content_block": {"type": "text"}}}),
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": "Second."}}}),
            json.dumps({"type": "result", "subtype": "success", "result": ""}),
        ]
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "implement", True, io_out=out)
            s.send("go")
        # blocks separated, not "first.Second."
        self.assertNotIn("first.Second.", out.getvalue())
        self.assertIn("first.", out.getvalue())
        self.assertIn("Second.", out.getvalue())

    def test_codex_session_first_then_resume(self):
        recorded = {"cmds": [], "tid": None}

        class FakeCodex(bridge.CodexSession):
            def _run(self, command):
                recorded["cmds"].append(command)
                return [{"type": "thread.started", "thread_id": "T1"}]

        s = FakeCodex("implement", True, io_out=io.StringIO(),
                      on_thread_id=lambda i: recorded.__setitem__("tid", i))
        s.send("first")
        s.send("second")
        self.assertEqual(recorded["cmds"][0][:4],
                         ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertEqual(recorded["cmds"][0][-1], "first")
        self.assertEqual(
            recorded["cmds"][1],
            ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
             "T1", "second"])
        self.assertEqual(recorded["tid"], "T1")


class AdditiveTest(unittest.TestCase):
    """cowork must stay additive: it must not import or reference the existing
    co-plan helper, and the existing files must still be present."""

    def test_cowork_does_not_import_co_plan_file(self):
        import ast
        for name in ("cowork.py", "cowork_bridge.py", "cowork_preflight.py",
                     "cowork_state.py"):
            with open(os.path.join(_HERE, name)) as fh:
                tree = ast.parse(fh.read(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("co_plan_file", alias.name,
                                         "%s must not import co_plan_file" % name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn("co_plan_file", node.module or "",
                                     "%s must not import co_plan_file" % name)

    def test_existing_skill_files_present(self):
        root = os.path.dirname(_HERE)
        for rel in ("SKILL.md", "scripts/co_plan_file.py"):
            self.assertTrue(os.path.exists(os.path.join(root, rel)))


# --------------------------------------------------------------------------- #
# Live integration tests against the real claude / codex CLIs.                #
#                                                                             #
# These exercise the actual stdin/stdout contracts (not fakes) so we catch    #
# CLI-version drift in the flags and event shapes. They cost real API calls   #
# and are slow, so they only run when COWORK_LIVE=1 is set AND the CLI is on  #
# PATH. Run them with:  COWORK_LIVE=1 python3 -m unittest scripts/test_cowork #
# --------------------------------------------------------------------------- #

LIVE = os.environ.get("COWORK_LIVE") == "1"
HAS_CLAUDE = shutil.which("claude") is not None
HAS_CODEX = shutil.which("codex") is not None
LIVE_TIMEOUT = int(os.environ.get("COWORK_LIVE_TIMEOUT", "240"))


def _run_cli(cmd, stdin_text=None, timeout=LIVE_TIMEOUT):
    """Run a real CLI command and return (returncode, [parsed json objs], stderr)."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise AssertionError("CLI timed out after %ss: %s" % (timeout, cmd[:3]))
    objs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return proc.returncode, objs, err


@unittest.skipUnless(LIVE and HAS_CLAUDE, "set COWORK_LIVE=1 with claude on PATH")
class LiveClaudeTest(unittest.TestCase):
    """Verify the real claude stream-json contract our bridge depends on."""

    def test_stdin_schema_accepted_and_assistant_result(self):
        cmd = bridge.build_claude_command(cowork.SCOUT_PROMPT_PATH, "plan", True)
        rc, objs, err = _run_cli(cmd, bridge.encode_user_message(
            "Reply with exactly the word: pong"))
        self.assertEqual(rc, 0, err[:300])
        parsed = [bridge.parse_claude_event(o) for o in objs]
        kinds = [p["kind"] for p in parsed]
        self.assertIn("assistant", kinds, "no assistant event: %s" % kinds)
        self.assertIn("result", kinds, "no result event: %s" % kinds)
        texts = " ".join(p.get("text", "") for p in parsed if p["kind"] == "assistant")
        self.assertIn("pong", texts.lower())
        result = [p for p in parsed if p["kind"] == "result"][0]
        self.assertFalse(result["is_error"])

    def test_probe_passes_against_real_claude(self):
        ok, alert = bridge.probe_claude_stream_json(
            lambda c, s: _run_cli(c, s)[1], mode="plan", yolo=True,
            role_prompt_file=cowork.SCOUT_PROMPT_PATH)
        self.assertTrue(ok, alert)


@unittest.skipUnless(LIVE and HAS_CODEX, "set COWORK_LIVE=1 with codex on PATH")
class LiveCodexTest(unittest.TestCase):
    """Verify the real codex exec --json + resume contract."""

    def test_exec_emits_thread_id_and_message(self):
        cmd = bridge.build_codex_command(
            "Reply with exactly the word: pong", "plan", True)
        rc, objs, err = _run_cli(cmd)
        self.assertEqual(rc, 0, err[:300])
        tid = bridge.capture_thread_id(objs)
        self.assertIsNotNone(tid, "no thread.started/thread_id: %s" %
                             [o.get("type") for o in objs])
        msgs = [bridge.parse_codex_event(o) for o in objs]
        texts = " ".join(m.get("text", "") for m in msgs if m["kind"] == "message")
        self.assertIn("pong", texts.lower())

    def test_resume_by_explicit_id_carries_session(self):
        rc, objs, err = _run_cli(bridge.build_codex_command(
            "Remember the number 7. Reply ok.", "plan", True))
        self.assertEqual(rc, 0, err[:300])
        tid = bridge.capture_thread_id(objs)
        self.assertIsNotNone(tid)
        rc2, objs2, err2 = _run_cli(bridge.build_codex_resume_command(
            tid, "What number did I ask you to remember? Reply with just the number."))
        self.assertEqual(rc2, 0, err2[:300])
        texts = " ".join(
            bridge.parse_codex_event(o).get("text", "")
            for o in objs2 if bridge.parse_codex_event(o)["kind"] == "message")
        self.assertIn("7", texts)


if __name__ == "__main__":
    unittest.main()
