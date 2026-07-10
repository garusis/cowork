#!/usr/bin/env python3
"""Tests for the cowork foundation + scout role.

Pure functions (flag assembly, framing, parsing, probe, flow) are tested with
fakes; no real claude/codex CLI is spawned. Run:

    python3 -m unittest scripts/test_cowork.py
"""

import contextlib
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
import cowork_trace as trace_store  # noqa: E402
import cowork_ui as ui  # noqa: E402

# The rich UX stack is optional at import time (lazy-imported in cowork_ui). Tests
# that exercise the real libraries skip when they are absent — same pattern as the
# COWORK_LIVE integration tests below.
try:
    import rich  # noqa: F401
    import prompt_toolkit  # noqa: F401
    import questionary  # noqa: F401
    HAS_UI_DEPS = True
except ImportError:
    HAS_UI_DEPS = False


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
        cmd = bridge.build_codex_resume_command(
            "thread-abc", "next", "plan", True)
        self.assertEqual(cmd[:5],
                         ["codex", "exec", "resume", "--json",
                          "--skip-git-repo-check"])
        # explicit id present, prompt strictly last, never --last.
        self.assertIn("thread-abc", cmd)
        self.assertEqual(cmd[-1], "next")
        self.assertNotIn("--last", cmd)
        # resume rejects --sandbox/--add-dir; permissions re-applied via -c.
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("--add-dir", cmd)

    def _add_dir_pair(self, cmd):
        # Return the (flag, value) pair following --add-dir, or None.
        if "--add-dir" not in cmd:
            return None
        i = cmd.index("--add-dir")
        return (cmd[i], cmd[i + 1])

    def test_claude_command_extra_writable_dir(self):
        # Granted on BOTH the fresh (session_id) and resume (resume_id) forms.
        fresh = bridge.build_claude_command(
            "roles/scout.md", "implement", False, session_id="sid",
            extra_writable_dir="/home/u/.cowork/sessions/S")
        self.assertEqual(self._add_dir_pair(fresh),
                         ("--add-dir", "/home/u/.cowork/sessions/S"))
        resume = bridge.build_claude_command(
            "roles/scout.md", "implement", False, resume_id="rid",
            extra_writable_dir="/home/u/.cowork/sessions/S")
        self.assertEqual(self._add_dir_pair(resume),
                         ("--add-dir", "/home/u/.cowork/sessions/S"))
        # Byte-identical to today when the param is omitted/None.
        self.assertEqual(
            bridge.build_claude_command("roles/scout.md", "implement", False,
                                        session_id="sid"),
            bridge.build_claude_command("roles/scout.md", "implement", False,
                                        session_id="sid",
                                        extra_writable_dir=None))
        self.assertNotIn(
            "--add-dir",
            bridge.build_claude_command("roles/scout.md", "implement", False,
                                        session_id="sid"))

    def test_codex_command_extra_writable_dir(self):
        cmd = bridge.build_codex_command(
            "PROMPT", "implement", False,
            extra_writable_dir="/home/u/.cowork/sessions/S")
        self.assertEqual(self._add_dir_pair(cmd),
                         ("--add-dir", "/home/u/.cowork/sessions/S"))
        self.assertEqual(cmd[-1], "PROMPT")  # prompt stays last
        # Byte-identical to today when the param is omitted/None.
        self.assertEqual(
            bridge.build_codex_command("PROMPT", "implement", False),
            bridge.build_codex_command("PROMPT", "implement", False,
                                       extra_writable_dir=None))
        self.assertNotIn(
            "--add-dir",
            bridge.build_codex_command("PROMPT", "implement", False))

    def _c_values(self, cmd):
        # Return the list of values following each `-c` flag.
        return [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-c"]

    def test_codex_resume_mode_args_branches(self):
        # plan -> read-only via -c.
        self.assertEqual(
            bridge.codex_resume_mode_args("plan", True),
            ["-c", 'sandbox_mode="read-only"'])
        self.assertEqual(
            bridge.codex_resume_mode_args("plan", False),
            ["-c", 'sandbox_mode="read-only"'])
        # implement + yolo -> bypass flag, no sandbox/add-dir.
        self.assertEqual(
            bridge.codex_resume_mode_args("implement", True),
            ["--dangerously-bypass-approvals-and-sandbox"])
        # implement + no-yolo, no dir -> workspace-write only.
        self.assertEqual(
            bridge.codex_resume_mode_args("implement", False),
            ["-c", 'sandbox_mode="workspace-write"'])
        # implement + no-yolo + dir -> workspace-write + json-encoded root.
        self.assertEqual(
            bridge.codex_resume_mode_args(
                "implement", False,
                extra_writable_dir="/home/u/.cowork/sessions/S"),
            ["-c", 'sandbox_mode="workspace-write"',
             "-c",
             'sandbox_workspace_write.writable_roots='
             '["/home/u/.cowork/sessions/S"]'])

    def test_codex_resume_implement_yolo(self):
        cmd = bridge.build_codex_resume_command(
            "thread-abc", "next", "implement", True)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("--add-dir", cmd)
        self.assertIn("thread-abc", cmd)
        self.assertEqual(cmd[-1], "next")
        self.assertNotIn("--last", cmd)

    def test_codex_resume_implement_no_yolo_regrants_root(self):
        cmd = bridge.build_codex_resume_command(
            "thread-abc", "next", "implement", False,
            extra_writable_dir="/home/u/.cowork/sessions/S")
        cvals = self._c_values(cmd)
        self.assertIn('sandbox_mode="workspace-write"', cvals)
        self.assertIn(
            'sandbox_workspace_write.writable_roots='
            '["/home/u/.cowork/sessions/S"]', cvals)
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("--add-dir", cmd)
        self.assertEqual(cmd[-1], "next")  # prompt stays last
        self.assertNotIn("--last", cmd)

    def test_codex_resume_plan_read_only(self):
        cmd = bridge.build_codex_resume_command(
            "thread-abc", "next", "plan", False)
        self.assertIn('sandbox_mode="read-only"', self._c_values(cmd))
        self.assertEqual(cmd[-1], "next")


class ModelEffortFlagTest(unittest.TestCase):
    """Per-role model + thinking-effort pins on the assembled CLI commands."""

    def _c_values(self, cmd):
        return [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-c"]

    def test_claude_command_model_effort(self):
        cmd = bridge.build_claude_command(
            "roles/scout.md", "implement", True, session_id="sid",
            model="opus", effort="high")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        # Unset -> byte-identical to today (the CLI's own defaults apply).
        self.assertEqual(
            bridge.build_claude_command("roles/scout.md", "implement", True,
                                        session_id="sid"),
            bridge.build_claude_command("roles/scout.md", "implement", True,
                                        session_id="sid", model=None,
                                        effort=None))
        self.assertNotIn(
            "--model", bridge.build_claude_command(
                "roles/scout.md", "implement", True, session_id="sid"))

    def test_codex_model_args_shared_by_fresh_and_resume(self):
        self.assertEqual(bridge.codex_model_args(), [])
        # -c (not -m) so fresh and resume take the identical spelling: resume
        # rejects -m but accepts -c.
        self.assertEqual(
            bridge.codex_model_args("gpt-5.4-codex", "high"),
            ["-c", 'model="gpt-5.4-codex"',
             "-c", 'model_reasoning_effort="high"'])
        fresh = bridge.build_codex_command(
            "P", "implement", True, model="gpt-5.4-codex", effort="high")
        resume = bridge.build_codex_resume_command(
            "thread-1", "P", "implement", True, model="gpt-5.4-codex",
            effort="high")
        for cmd in (fresh, resume):
            self.assertIn('model="gpt-5.4-codex"', self._c_values(cmd))
            self.assertIn('model_reasoning_effort="high"',
                          self._c_values(cmd))
            self.assertEqual(cmd[-1], "P")  # prompt stays last
        self.assertNotIn("-m", fresh)
        # Unset -> byte-identical to today.
        self.assertEqual(
            bridge.build_codex_command("P", "implement", True),
            bridge.build_codex_command("P", "implement", True,
                                       model=None, effort=None))


class OpencodeBridgeTest(unittest.TestCase):
    """opencode agent-file generation, command assembly, and event parsing."""

    def test_permission_lines_per_mode(self):
        # yolo implement -> no block (--auto rides on the command instead).
        self.assertEqual(
            bridge.opencode_permission_lines("implement", True), [])
        plan = bridge.opencode_permission_lines("plan", True)
        self.assertIn("  edit: deny", plan)
        self.assertIn("  bash: ask", plan)  # ask auto-rejects headless
        safe = bridge.opencode_permission_lines("implement", False,
                                                external_dir=True)
        self.assertIn("  edit: allow", safe)
        self.assertIn("  external_directory: allow", safe)
        # plan never grants the external dir (read-only stays read-only).
        self.assertNotIn(
            "  external_directory: allow",
            bridge.opencode_permission_lines("plan", True, external_dir=True))

    def test_agent_markdown_frontmatter_and_body(self):
        md = bridge.opencode_agent_markdown(
            "ROLE PROMPT", "plan", True, description="cowork scout role")
        self.assertTrue(md.startswith("---\n"))
        self.assertIn("description: cowork scout role", md)
        self.assertIn("mode: primary", md)
        self.assertIn("edit: deny", md)
        self.assertTrue(md.rstrip().endswith("ROLE PROMPT"))
        yolo_md = bridge.opencode_agent_markdown(
            "R", "implement", True, description="d")
        self.assertNotIn("permission:", yolo_md)

    def test_ensure_opencode_agent_writes_and_regenerates(self):
        import tempfile
        base = tempfile.mkdtemp()
        rp = os.path.join(base, "role.md")
        with open(rp, "w") as fh:
            fh.write("BE THE SCOUT")
        name = bridge.ensure_opencode_agent(rp, "scout", "implement", False,
                                            base_dir=base)
        self.assertEqual(name, "cowork-scout")
        path = os.path.join(base, ".opencode", "agents", "cowork-scout.md")
        with open(path) as fh:
            content = fh.read()
        self.assertIn("BE THE SCOUT", content)
        self.assertIn("edit: allow", content)
        # A config change regenerates the file with the new permissions.
        bridge.ensure_opencode_agent(rp, "scout", "plan", True, base_dir=base)
        with open(path) as fh:
            self.assertIn("edit: deny", fh.read())

    def test_build_opencode_command(self):
        cmd = bridge.build_opencode_command(
            "cowork-scout", "PROMPT", "implement", True,
            model="anthropic/claude-sonnet-4-5", effort="max")
        self.assertEqual(cmd[:4], ["opencode", "run", "--format", "json"])
        self.assertEqual(cmd[cmd.index("--agent") + 1], "cowork-scout")
        self.assertEqual(cmd[cmd.index("--model") + 1],
                         "anthropic/claude-sonnet-4-5")
        self.assertEqual(cmd[cmd.index("--variant") + 1], "max")
        self.assertIn("--auto", cmd)
        self.assertEqual(cmd[-1], "PROMPT")
        # no-yolo and plan runs rely on agent permissions, never --auto.
        self.assertNotIn("--auto", bridge.build_opencode_command(
            "a", "P", "implement", False))
        self.assertNotIn("--auto", bridge.build_opencode_command(
            "a", "P", "plan", True))
        # model/effort omitted -> flags omitted (CLI defaults apply).
        bare = bridge.build_opencode_command("a", "P", "implement", True)
        self.assertNotIn("--model", bare)
        self.assertNotIn("--variant", bare)
        res = bridge.build_opencode_command(
            "a", "P", "implement", True, resume_session_id="ses_1")
        self.assertEqual(res[res.index("--session") + 1], "ses_1")
        self.assertEqual(res[-1], "P")

    def test_parse_opencode_events(self):
        self.assertEqual(
            bridge.parse_opencode_event(
                {"type": "text", "part": {"text": "hi"}}),
            {"kind": "message", "text": "hi"})
        self.assertEqual(
            bridge.parse_opencode_event(
                {"type": "tool", "part": {"tool": "bash"}}),
            {"kind": "tool", "label": "using bash"})
        done = bridge.parse_opencode_event(
            {"type": "tool_use",
             "part": {"tool": "bash", "state": {"status": "completed"}}})
        self.assertEqual(done["kind"], "tool_done")
        denied = bridge.parse_opencode_event(
            {"type": "tool_use",
             "part": {"state": {"status": "error",
                                "error": "The user rejected permission to use "
                                         "this specific tool call."}}})
        self.assertEqual(denied["kind"], "denied")
        err = bridge.parse_opencode_event(
            {"type": "error",
             "error": {"name": "APIError", "data": {"message": "boom"}}})
        self.assertEqual(err, {"kind": "error", "text": "boom"})
        fin = bridge.parse_opencode_event(
            {"type": "step_finish",
             "part": {"reason": "stop", "tokens": {"input": 5}}})
        self.assertEqual(fin["kind"], "step_finish")
        self.assertEqual(fin["reason"], "stop")
        other = bridge.parse_opencode_event({"type": "step_start", "part": {}})
        self.assertEqual(other["kind"], "other")

    def test_capture_session_id_and_usage(self):
        events = [
            {"type": "step_start", "sessionID": "ses_A", "part": {}},
            {"type": "step_finish", "sessionID": "ses_A",
             "part": {"reason": "tool-calls",
                      "tokens": {"input": 10, "output": 2, "reasoning": 3,
                                 "cache": {"read": 7, "write": 1}}}},
            {"type": "step_finish", "sessionID": "ses_A",
             "part": {"reason": "stop", "tokens": {"input": 4, "output": 5}}},
        ]
        self.assertEqual(bridge.capture_opencode_session_id(events), "ses_A")
        self.assertEqual(bridge.opencode_usage(events), {
            "input_tokens": 14, "output_tokens": 10,
            "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1})
        self.assertIsNone(bridge.opencode_usage([{"type": "text", "part": {}}]))
        self.assertIsNone(bridge.capture_opencode_session_id(
            [{"type": "text", "part": {}}]))


class OpencodeSessionTest(unittest.TestCase):
    """OpencodeSession turn behavior via a fake subprocess (no real CLI)."""

    class FakeProc:
        def __init__(self, lines):
            self.stdout = iter(lines)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    def _session(self, tmp, **kw):
        rp = os.path.join(tmp, "role.md")
        with open(rp, "w") as fh:
            fh.write("ROLE")
        return bridge.OpencodeSession(rp, "implement", True,
                                      agent_base_dir=tmp, **kw)

    def test_fresh_send_captures_session_and_resumes(self):
        import tempfile
        import unittest.mock as mock
        tmp = tempfile.mkdtemp()
        lines = [
            json.dumps({"type": "text", "sessionID": "ses_X",
                        "part": {"text": "hello there"}}),
            json.dumps({"type": "step_finish", "sessionID": "ses_X",
                        "part": {"reason": "stop",
                                 "tokens": {"input": 1, "output": 2}}}),
        ]
        got = {}
        cmds = []

        def fake_popen(command, **kwargs):
            cmds.append(command)
            return self.FakeProc(lines)

        out = io.StringIO()
        with mock.patch.object(bridge.subprocess, "Popen",
                               side_effect=fake_popen):
            s = self._session(tmp, io_out=out,
                              on_session_id=lambda i: got.setdefault("id", i))
            r1 = s.send("first")
            r2 = s.send("second")
        self.assertEqual(s.controller, "opencode")
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])
        self.assertEqual(got.get("id"), "ses_X")
        self.assertEqual(r1["session_id"], "ses_X")
        # fresh turn has no --session; the follow-up resumes the captured id.
        self.assertNotIn("--session", cmds[0])
        self.assertEqual(cmds[1][cmds[1].index("--session") + 1], "ses_X")
        # both turns target the generated agent.
        for cmd in cmds:
            self.assertEqual(cmd[cmd.index("--agent") + 1], "cowork-scout")
        self.assertIn("hello there", out.getvalue())

    def test_constructor_resume_uses_session_flag(self):
        import tempfile
        import unittest.mock as mock
        tmp = tempfile.mkdtemp()
        lines = [json.dumps({"type": "text", "sessionID": "ses_R",
                             "part": {"text": "resumed"}})]
        cmds = []

        def fake_popen(command, **kwargs):
            cmds.append(command)
            return self.FakeProc(lines)

        with mock.patch.object(bridge.subprocess, "Popen",
                               side_effect=fake_popen):
            s = self._session(tmp, io_out=io.StringIO(),
                              resume_session_id="ses_R")
            r = s.send("continue")
        self.assertTrue(r["ok"])
        self.assertEqual(cmds[0][cmds[0].index("--session") + 1], "ses_R")

    def test_denied_error_and_empty_stream(self):
        import tempfile
        import unittest.mock as mock
        tmp = tempfile.mkdtemp()

        def run_with(lines):
            with mock.patch.object(bridge.subprocess, "Popen",
                                   return_value=self.FakeProc(lines)):
                out = io.StringIO()
                s = self._session(tmp, io_out=out)
                return s.send("go"), out.getvalue()

        denied, text = run_with([json.dumps(
            {"type": "tool_use", "sessionID": "ses_D",
             "part": {"state": {"status": "error",
                                "error": "The user rejected permission to use "
                                         "this specific tool call."}}})])
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["result"], "denied")
        self.assertIn("denied", text)

        err, text = run_with([json.dumps(
            {"type": "error", "sessionID": "ses_E",
             "error": {"name": "APIError", "data": {"message": "boom"}}})])
        self.assertFalse(err["ok"])
        self.assertEqual(err["result"], "error")
        self.assertIn("boom", text)

        empty, _ = run_with([])
        self.assertFalse(empty["ok"])
        self.assertEqual(empty.get("error_type"), "no_events")


class StatusSpinnerTest(unittest.TestCase):
    def test_returns_fn_value_and_runs_it(self):
        seen = {}

        def fn():
            seen["ran"] = True
            return "verdict"

        out = io.StringIO()  # not a TTY
        result = cowork._with_status_spinner(out, "reviewing", fn)
        self.assertEqual(result, "verdict")
        self.assertTrue(seen["ran"])

    def test_noop_off_tty_writes_nothing(self):
        # ui.Spinner is TTY-gated, so off a TTY the helper must add zero bytes —
        # the scripted/test path stays byte-identical.
        out = io.StringIO()
        cowork._with_status_spinner(out, "reading repo state", lambda: None)
        self.assertEqual(out.getvalue(), "")

    def test_stops_spinner_even_when_fn_raises(self):
        # The spinner is torn down in a finally, so an exception still leaves a
        # clean stream (and off a TTY, nothing was written).
        out = io.StringIO()
        with self.assertRaises(ValueError):
            cowork._with_status_spinner(
                out, "starting scout",
                lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertEqual(out.getvalue(), "")


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
            "planning-advisor": {"controller": "codex"},
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

    def test_preflight_requires_packages_only_when_interactive(self):
        cfg = {"scout": {"controller": "claude"}}
        present = lambda c: "/bin/" + c if c == "claude" else None
        have = lambda name: object()   # all packages importable
        missing = lambda name: None    # none importable
        # Non-interactive: pip packages are not required.
        ok, _ = preflight.preflight(cfg, which=present, interactive=False,
                                    find_spec=missing)
        self.assertTrue(ok)
        # Interactive + packages missing -> fail with a package alert.
        ok, alerts = preflight.preflight(cfg, which=present, interactive=True,
                                         find_spec=missing)
        self.assertFalse(ok)
        self.assertTrue(any("prompt_toolkit" in a or "rich" in a
                            or "questionary" in a for a in alerts))
        # Interactive + packages present -> ok.
        ok, _ = preflight.preflight(cfg, which=present, interactive=True,
                                    find_spec=have)
        self.assertTrue(ok)

    def test_check_python_packages(self):
        ok, alerts = preflight.check_python_packages(
            ["rich", "questionary"], find_spec=lambda n: None)
        self.assertFalse(ok)
        self.assertEqual(len(alerts), 2)
        self.assertIn("pip install", alerts[0])
        ok, alerts = preflight.check_python_packages(
            ["rich"], find_spec=lambda n: object())
        self.assertTrue(ok)
        self.assertEqual(alerts, [])


class MenuTest(unittest.TestCase):
    """Interactive menus driven by injected ask-callables; questionary never runs."""

    def test_select_team_interactive(self):
        self.assertEqual(
            cowork.select_team_interactive(
                checkbox_fn=lambda msg, opts, checked=None: ["planner", "scout"]),
            ["scout", "planner"])  # re-ordered by canonical ROLES

    def test_select_team_cancel_returns_empty(self):
        self.assertEqual(
            cowork.select_team_interactive(checkbox_fn=lambda *a, **k: None), [])
        self.assertEqual(
            cowork.select_team_interactive(checkbox_fn=lambda *a, **k: []), [])

    def test_configure_roles_accepts_defaults(self):
        # One Enter: the start entry is the menu default, so returning the
        # default accepts the whole config untouched.
        cfg = cowork.configure_roles_interactive(
            ["scout", "planning-advisor"],
            select_fn=lambda opts, default=None, message="": default)
        self.assertEqual(cfg["scout"], cowork.DEFAULTS["scout"])

    def test_configure_roles_customizes(self):
        picks = {"n": 0}

        def select_fn(opts, default=None, message=""):
            if cowork.START_CHOICE in opts:  # the table screen
                picks["n"] += 1
                return "scout" if picks["n"] == 1 else cowork.START_CHOICE
            if message.endswith("controller"):
                return "codex"
            if message.endswith("access"):
                return "safe (edits only, other commands denied)"
            return default  # model + effort selects keep the default
        cfg = cowork.configure_roles_interactive(
            ["scout"], select_fn=select_fn,
            text_fn=lambda message, default="": default)
        self.assertEqual(cfg["scout"]["controller"], "codex")
        self.assertFalse(cfg["scout"]["yolo"])
        self.assertEqual(cfg["scout"]["mode"], "implement")
        self.assertIsNone(cfg["scout"]["model"])
        self.assertIsNone(cfg["scout"]["effort"])

    def test_configure_roles_opencode_provider_model_effort(self):
        picks = {"n": 0}

        def select_fn(opts, default=None, message=""):
            if cowork.START_CHOICE in opts:
                picks["n"] += 1
                return "builder" if picks["n"] == 1 else cowork.START_CHOICE
            if message.endswith("controller"):
                return "opencode"
            if message.endswith("provider (opencode)"):
                self.assertIn("anthropic", opts)
                return "anthropic"
            if message.endswith("model (anthropic)"):
                return "anthropic/claude-sonnet-4-5"
            if message.endswith("thinking effort (opencode)"):
                self.assertIn("max", opts)  # provider-tailored levels
                return "max"
            return default
        cfg = cowork.configure_roles_interactive(
            ["builder"], select_fn=select_fn,
            text_fn=lambda message, default="": default,
            opencode_models_fn=lambda: {
                "anthropic": ["anthropic/claude-sonnet-4-5",
                              "anthropic/claude-opus-4-5"]})
        self.assertEqual(cfg["builder"]["controller"], "opencode")
        self.assertEqual(cfg["builder"]["model"], "anthropic/claude-sonnet-4-5")
        self.assertEqual(cfg["builder"]["effort"], "max")

    def test_configure_roles_switching_controller_resets_model_effort(self):
        picks = {"n": 0}

        def select_fn(opts, default=None, message=""):
            if cowork.START_CHOICE in opts:
                picks["n"] += 1
                if picks["n"] == 1:
                    return "scout"      # first edit: claude + opus + high
                if picks["n"] == 2:
                    return "scout"      # second edit: switch to codex
                return cowork.START_CHOICE
            if message.endswith("controller"):
                return "claude" if picks["n"] == 1 else "codex"
            if picks["n"] == 1 and message.endswith("model (claude)"):
                return "opus"
            if picks["n"] == 1 and message.endswith("thinking effort (claude)"):
                return "high"
            return default
        cfg = cowork.configure_roles_interactive(
            ["scout"], select_fn=select_fn,
            text_fn=lambda message, default="": default)
        # The claude model/effort never leak into the codex config.
        self.assertEqual(cfg["scout"]["controller"], "codex")
        self.assertIsNone(cfg["scout"]["model"])
        self.assertIsNone(cfg["scout"]["effort"])

    def test_list_opencode_models_parses_and_tolerates_failure(self):
        parsed = cowork.list_opencode_models(
            runner=lambda: "anthropic/claude-sonnet-4-5\nopenai/gpt-5.4\n"
                           "openai/gpt-5.4-mini\nnot a model line\n")
        self.assertEqual(parsed, {
            "anthropic": ["anthropic/claude-sonnet-4-5"],
            "openai": ["openai/gpt-5.4", "openai/gpt-5.4-mini"]})
        self.assertEqual(cowork.list_opencode_models(runner=lambda: ""), {})

    def test_gather_context_eof_is_empty(self):
        self.assertEqual(
            cowork.gather_context_interactive(prompt_fn=lambda: ui.EOF), "")
        self.assertEqual(
            cowork.gather_context_interactive(prompt_fn=lambda: "the brief"),
            "the brief")

    def test_format_config_summary_aligned(self):
        cfg = cowork.default_config(["scout", "planning-advisor", "builder"])
        text = cowork.format_config_summary(cfg)
        self.assertIn("scout", text)
        for label in ("role", "controller", "permissions", "mode"):
            self.assertIn(label, text)
        self.assertIn("no-yolo", cowork.format_config_summary(
            {"scout": {"controller": "claude", "yolo": False, "mode": "plan"}}))


class ConfigTest(unittest.TestCase):
    def test_default_config_matches_defaults(self):
        cfg = cowork.default_config(cowork.ROLES)
        # Roles default to implement mode (guardrailed by role spec, not plan)
        # and to the controller CLI's own model/effort (None).
        self.assertEqual(cfg["scout"],
                         {"controller": "claude", "model": None, "effort": None,
                          "yolo": True, "mode": "implement"})
        for role in cowork.ROLES:
            self.assertEqual(cfg[role]["mode"], "implement")
            self.assertIsNone(cfg[role]["model"])
            self.assertIsNone(cfg[role]["effort"])

    def test_apply_config_override(self):
        cfg = cowork.default_config(["scout"])
        ok, err = cowork.apply_config_override(
            cfg, "scout", ["codex", "no-yolo", "implement"])
        self.assertTrue(ok)
        self.assertEqual(
            cfg["scout"], {"controller": "codex", "model": None, "effort": None,
                           "yolo": False, "mode": "implement"})
        ok, _ = cowork.apply_config_override(cfg, "ghost", ["claude"])
        self.assertFalse(ok)
        ok, _ = cowork.apply_config_override(cfg, "scout", ["bogus"])
        self.assertFalse(ok)

    def test_apply_config_override_opencode_model_effort(self):
        cfg = cowork.default_config(["scout"])
        ok, err = cowork.apply_config_override(
            cfg, "scout", ["opencode", "model=openai/gpt-5.4", "effort=high"])
        self.assertTrue(ok)
        self.assertEqual(cfg["scout"]["controller"], "opencode")
        self.assertEqual(cfg["scout"]["model"], "openai/gpt-5.4")
        self.assertEqual(cfg["scout"]["effort"], "high")
        # model=default / effort=default reset to the CLI's own setting.
        ok, _ = cowork.apply_config_override(
            cfg, "scout", ["model=default", "effort=default"])
        self.assertTrue(ok)
        self.assertIsNone(cfg["scout"]["model"])
        self.assertIsNone(cfg["scout"]["effort"])
        ok, _ = cowork.apply_config_override(cfg, "scout", ["speed=fast"])
        self.assertFalse(ok)

    def test_normalize_role_config_backfills_model_effort(self):
        old = {"controller": "codex", "yolo": True, "mode": "implement"}
        normalized = cowork.normalize_role_config(old)
        self.assertEqual(normalized,
                         {"controller": "codex", "model": None, "effort": None,
                          "yolo": True, "mode": "implement"})
        self.assertNotIn("model", old)  # input never mutated


class ArgsPathTest(unittest.TestCase):
    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_parse_team(self):
        selected, err = cowork.parse_team("planning-advisor,scout")
        self.assertIsNone(err)
        self.assertEqual(selected, ["scout", "planning-advisor"])  # canonical order
        selected, err = cowork.parse_team("scout,ghost")
        self.assertIsNotNone(err)
        # the reserved name was renamed: plain `advisor` is no longer a role
        selected, err = cowork.parse_team("scout,advisor")
        self.assertIsNotNone(err)

    def test_apply_config_args(self):
        cfg = cowork.default_config(["scout", "planning-advisor"])
        ok, err = cowork.apply_config_args(cfg, ["scout=codex,no-yolo,implement"])
        self.assertTrue(ok)
        self.assertEqual(
            cfg["scout"], {"controller": "codex", "model": None, "effort": None,
                           "yolo": False, "mode": "implement"})
        ok, err = cowork.apply_config_args(cfg, ["scoutcodex"])  # no '='
        self.assertFalse(ok)
        # key=value options ride in the same comma list; ROLE= splits on the
        # FIRST '=' only, so model=/effort= survive intact.
        ok, err = cowork.apply_config_args(
            cfg, ["scout=opencode,model=anthropic/claude-sonnet-4-5,"
                  "effort=max,yolo"])
        self.assertTrue(ok)
        self.assertEqual(
            cfg["scout"],
            {"controller": "opencode", "model": "anthropic/claude-sonnet-4-5",
             "effort": "max", "yolo": True, "mode": "implement"})

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

    def test_resolve_context_resuming_skips_prompt(self):
        # Resuming + interactive => skip the goal prompt, return "" so run_scout
        # auto-continues ("Continue the session.") without ever prompting.
        import unittest.mock as mock
        with mock.patch.object(cowork, "gather_context_interactive",
                               side_effect=AssertionError("prompted on resume")):
            self.assertEqual(cowork.resolve_context(self._args([]), resuming=True), "")
        # An explicit --context still wins on resume (redirect a resumed session).
        self.assertEqual(
            cowork.resolve_context(self._args(["--context", "new goal"]),
                                   resuming=True),
            "new goal")

    def test_run_flow_non_interactive_reaches_scout(self):
        captured = {}

        def fake_run_scout(config, context, selected, io_in=None, io_out=None,
                           resume_id=None, on_session=None, intel_path=None,
                           review_path=None, **kwargs):
            captured["config"] = config
            captured["context"] = context
            captured["selected"] = selected
            captured["intel_path"] = intel_path
            captured["review_path"] = review_path
            return 0

        args = self._args(
            ["--team", "scout,planning-advisor",
             "--config", "scout=codex,no-yolo,implement",
             "--context", "do the thing", "--no-session"])
        out = io.StringIO()
        rc = cowork.run_flow(
            args, io_out=out,
            which=lambda c: "/bin/" + c,  # everything present
            run_scout_fn=fake_run_scout,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["selected"], ["scout", "planning-advisor"])
        # The fresh scout's seed carries the goal AND the repo-discovery note.
        self.assertIn("do the thing", captured["context"])
        self.assertIn("Repository discovery", captured["context"])
        self.assertEqual(captured["config"]["scout"]["controller"], "codex")

    def test_run_flow_non_interactive_skips_gum_in_preflight(self):
        # claude present, gum absent: non-interactive must still pass preflight.
        # A reserved-reviewer-only team (no user-facing role) starts in the
        # default scouting phase and falls through the scout-not-selected
        # branch, exactly as the old --team revisor case did.
        args = self._args(
            ["--team", "build-reviewer", "--context", "x", "--no-session"])
        out = io.StringIO()
        rc = cowork.run_flow(
            args, io_out=out,
            which=lambda c: None if c == "gum" else "/bin/" + c,
        )
        # build-reviewer (no scout) -> "not selected" note, rc 0, gum never req'd
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
        cfg = cowork.default_config(["scout", "planning-advisor"])
        state_store.save_config(path, ["scout", "planning-advisor"], cfg)
        loaded = state_store.load(path)
        self.assertTrue(state_store.has_config(loaded))
        self.assertEqual(loaded["team"], ["scout", "planning-advisor"])
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

    def test_switch_role_controller_clears_active_id_and_preserves_bookkeeping(self):
        path = self._tmp()
        state = state_store.save_config(
            path, ["planner"], cowork.default_config(["planner"]))
        state = state_store.save_role_session(
            path, "planner", "claude", "old-claude", prior=state)
        state["sessions"]["planner"]["last_context_revision_seen"] = 7
        state["sessions"]["planner"]["last_approved_baseline"] = {"sha": "abc"}
        state_store.save(path, state)

        switched = state_store.switch_role_controller(
            path, "planner", "codex", prior=state, reason="limit",
            source="gate", created=123.0)

        self.assertEqual(switched["config"]["planner"]["controller"], "codex")
        sess = switched["sessions"]["planner"]
        self.assertEqual(sess["controller"], "codex")
        self.assertNotIn("id", sess)
        self.assertEqual(sess["last_context_revision_seen"], 7)
        self.assertEqual(sess["last_approved_baseline"], {"sha": "abc"})
        self.assertIsNone(
            state_store.get_role_session(switched, "planner", "codex"))
        pending = state_store.read_pending_switch(switched, "planner")
        self.assertEqual(pending["from_controller"], "claude")
        self.assertEqual(pending["to_controller"], "codex")
        self.assertEqual(pending["source"], "gate")

        saved = state_store.save_role_session(
            path, "planner", "codex", "new-thread", prior=switched)
        self.assertEqual(
            state_store.get_role_session(saved, "planner", "codex"),
            "new-thread")
        cleared = state_store.clear_pending_switch(path, "planner", prior=saved)
        self.assertIsNone(state_store.read_pending_switch(cleared, "planner"))

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

    def test_invalidate_ready_status(self):
        path = self._tmp()
        self.assertFalse(state_store.invalidate_ready_status(path))  # missing
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"summary": "keep me"}}, fh)
        self.assertTrue(state_store.invalidate_ready_status(path))
        with open(path, "r") as fh:
            data = json.load(fh)
        self.assertEqual(data["status"], "needs_input")
        self.assertEqual(data["result"], {"summary": "keep me"})
        self.assertFalse(state_store.invalidate_ready_status(path))

    def test_load_rejects_incompatible_version(self):
        path = self._tmp()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write('{"version": 999, "team": ["scout"], "config": {}}')
        self.assertIsNone(state_store.load(path))

    def test_fingerprint_tolerance_missing(self):
        # T6: a missing/unreadable file yields the all-None/exists:False shape
        # and never raises.
        self.assertEqual(
            state_store.fingerprint_status(None),
            {"exists": False, "status": None, "sha256": None,
             "size": None, "mtime_ns": None})
        path = self._tmp()
        fp = state_store.fingerprint_status(path)  # parent dir does not exist
        self.assertFalse(fp["exists"])
        self.assertIsNone(fp["status"])
        self.assertIsNone(fp["sha256"])
        self.assertIsNone(fp["size"])

    def test_fingerprint_malformed_readable(self):
        # T6b: a present-but-malformed artifact never raises and returns
        # exists:True, status:None, AND a real sha256/size from the raw bytes.
        path = self._tmp()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("this is not json {")
        fp = state_store.fingerprint_status(path)
        self.assertTrue(fp["exists"])
        self.assertIsNone(fp["status"])     # unparseable -> no status
        self.assertIsNotNone(fp["sha256"])  # but a real raw-byte hash
        self.assertEqual(fp["size"], len(b"this is not json {"))
        # Two malformed writes with DIFFERENT bytes produce DIFFERENT sha256,
        # so a malformed-but-changed turn reads as progress, not a no-op.
        with open(path, "w") as fh:
            fh.write("still not json }")
        fp2 = state_store.fingerprint_status(path)
        self.assertTrue(fp2["exists"])
        self.assertIsNone(fp2["status"])
        self.assertNotEqual(fp["sha256"], fp2["sha256"])

    def test_fingerprint_wellformed_reports_status(self):
        path = self._tmp()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        fp = state_store.fingerprint_status(path)
        self.assertTrue(fp["exists"])
        self.assertEqual(fp["status"], "ready_for_review")
        self.assertIsNotNone(fp["sha256"])
        self.assertGreater(fp["size"], 0)


class MultiSessionStoreTest(unittest.TestCase):
    def _dir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def _write_session(self, cwd, suid=None, legacy=False, context=None,
                       phase=None, created=None, mtime=None):
        if legacy:
            path = state_store.session_path(cwd)
        else:
            path = state_store.new_session_path(cwd, suid)
        state = {"team": [], "config": {}, "sessions": {}}
        if suid:
            state["session_uuid"] = suid
        if context is not None:
            state["context"] = {"text": context, "revision": 1}
        if phase:
            state["phase"] = phase
        if created is not None:
            state["created"] = created
        state_store.save(path, state)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_new_session_path_shape(self):
        cwd = self._dir()
        p = state_store.new_session_path(cwd, "abc-123")
        self.assertEqual(os.path.basename(p), "session.abc-123.json")
        self.assertEqual(os.path.dirname(p), state_store.session_dir(cwd))

    def test_derive_summary_first_line_trim_and_none(self):
        self.assertEqual(
            state_store.derive_summary({"context": {"text": "\n\n  build a    thing  \nmore"}}),
            "build a thing")
        self.assertIsNone(state_store.derive_summary({"context": {"text": "   \n  "}}))
        self.assertIsNone(state_store.derive_summary({}))
        long = "x" * 200
        out = state_store.derive_summary({"context": {"text": long}}, max_len=10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))
        # legacy plain-string context form is tolerated
        self.assertEqual(
            state_store.derive_summary({"context": "legacy goal text"}),
            "legacy goal text")

    def test_fallback_label_short_id_and_time(self):
        self.assertTrue(
            state_store.fallback_label("abcdef123456").startswith("session abcdef12"))
        labeled = state_store.fallback_label("abcdef123456", 0)  # falsy -> no time
        self.assertEqual(labeled, "session abcdef12")
        labeled2 = state_store.fallback_label("abcdef123456", 1_700_000_000)
        self.assertIn("session abcdef12", labeled2)
        self.assertIn("·", labeled2)

    def test_ensure_session_sets_created(self):
        cwd = self._dir()
        path = state_store.new_session_path(cwd, "u1")
        state = state_store.ensure_session(path, None, "u1")
        self.assertIn("created", state)
        self.assertGreater(state["created"], 0)
        # existing file (no created) is left alone
        path2 = self._write_session(cwd, suid="u2")  # no created field
        state2 = state_store.ensure_session(
            path2, state_store.load(path2), "ignored")
        self.assertNotIn("created", state2)
        self.assertEqual(state_store.get_session_uuid(state2), "u2")

    def test_new_session_uuid_identity(self):
        # A New session's filename uuid == persisted internal session_uuid
        # (== the ~/.cowork/sessions/<uuid>/ assets-dir key).
        cwd = self._dir()
        u = "11111111-2222-3333-4444-555555555555"
        path = state_store.new_session_path(cwd, u)
        state_store.ensure_session(path, None, u)
        loaded = state_store.load(path)
        self.assertEqual(
            os.path.basename(path),
            "session.%s.json" % state_store.get_session_uuid(loaded))

    def test_list_sessions_discovery_and_ordering(self):
        cwd = self._dir()
        # Three sessions: two new + one legacy, with distinct mtimes.
        self._write_session(cwd, suid="old", context="old goal",
                            phase="planning", created=100, mtime=100)
        self._write_session(cwd, suid="mid", context="mid goal",
                            phase="building", created=200, mtime=200)
        self._write_session(cwd, legacy=True, suid="leg", context="legacy goal",
                            mtime=300)  # no created -> sorts by mtime
        rows = state_store.list_sessions(cwd)
        self.assertEqual([r["id"] for r in rows], ["leg", "mid", "old"])
        self.assertEqual(rows[1]["summary"], "mid goal")
        self.assertEqual(rows[1]["phase"], "building")
        # legacy file appears with a derived summary and default phase
        leg = rows[0]
        self.assertEqual(leg["summary"], "legacy goal")
        self.assertEqual(leg["phase"], "scouting")
        self.assertIsNone(leg["created"])

    def test_list_sessions_skips_unreadable_and_idless(self):
        cwd = self._dir()
        good = self._write_session(cwd, suid="good", context="g")
        # a corrupt session.<uuid>.json -> load() returns None -> skipped
        bad = state_store.new_session_path(cwd, "bad")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "w") as fh:
            fh.write("not json {")
        # a legacy file with neither session_uuid nor a filename uuid -> skipped
        legacy = state_store.session_path(cwd)
        state_store.save(legacy, {"team": [], "config": {}, "sessions": {}})
        rows = state_store.list_sessions(cwd)
        self.assertEqual([r["id"] for r in rows], ["good"])

    def test_format_relative_time_is_relative_for_all_ages(self):
        now = 1_000_000_000
        self.assertEqual(ui.format_relative_time(0, now), "unknown")
        self.assertEqual(ui.format_relative_time(now, now), "just now")
        self.assertEqual(ui.format_relative_time(now - 5 * 60, now), "5m ago")
        self.assertEqual(ui.format_relative_time(now - 3 * 3600, now), "3h ago")
        self.assertEqual(ui.format_relative_time(now - 2 * 86400, now), "2d ago")
        # Older than a week must STAY relative — never an absolute date.
        for days, suffix in [(10, "w ago"), (40, "mo ago"), (800, "y ago")]:
            label = ui.format_relative_time(now - days * 86400, now)
            self.assertTrue(label.endswith(suffix), label)
            self.assertTrue(label.endswith("ago"), label)
            self.assertNotIn("-", label)  # no YYYY-MM-DD leak
        # Explicit large-age examples.
        self.assertEqual(ui.format_relative_time(now - 8 * 86400, now), "1w ago")
        self.assertEqual(ui.format_relative_time(now - 60 * 86400, now), "2mo ago")
        self.assertEqual(ui.format_relative_time(now - 400 * 86400, now), "1y ago")

    def test_list_sessions_id_falls_back_to_filename(self):
        cwd = self._dir()
        # A new-style file whose state lacks session_uuid still lists via the
        # uuid parsed from its filename.
        path = state_store.new_session_path(cwd, "fromname")
        state_store.save(path, {"team": [], "config": {}, "sessions": {}})
        rows = state_store.list_sessions(cwd)
        self.assertEqual([r["id"] for r in rows], ["fromname"])


class SelectSessionTest(unittest.TestCase):
    """select_session decision tree. TTY runs use FakeTTY streams + an injected
    select_fn; non-TTY runs use plain StringIO."""

    def _dir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    @contextlib.contextmanager
    def _chdir(self, d):
        prev = os.getcwd()
        os.chdir(d)
        try:
            yield
        finally:
            os.chdir(prev)

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _seed(self, cwd, suid, context="goal", mtime=None):
        path = state_store.new_session_path(cwd, suid)
        state = {"session_uuid": suid, "team": [], "config": {}, "sessions": {},
                 "context": {"text": context, "revision": 1}, "created": 1.0}
        state_store.save(path, state)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def _select(self, argv, cwd, select_fn=None, tty=True):
        io_cls = FakeTTY if tty else io.StringIO
        with self._chdir(cwd):
            return cowork.select_session(
                self._args(argv), io_cls(), io_cls(), select_fn=select_fn)

    # -- conflict checks fire first, even with --session-file present -------- #
    def test_new_and_resume_conflict(self):
        c = self._select(["--new", "--resume"], self._dir())
        self.assertIsNotNone(c.error)

    def test_no_session_and_resume_conflict(self):
        c = self._select(["--no-session", "--resume"], self._dir())
        self.assertIsNotNone(c.error)

    def test_conflict_beats_session_file(self):
        # --session-file must NOT bypass the conflict checks.
        c = self._select(
            ["--session-file", "/tmp/x.json", "--new", "--resume"], self._dir())
        self.assertIsNotNone(c.error)

    # -- --no-session runs the flow (not cancelled, not error) -------------- #
    def test_no_session_runs_flow(self):
        c = self._select(["--no-session"], self._dir())
        self.assertFalse(c.cancelled)
        self.assertIsNone(c.error)
        self.assertIsNotNone(c.path)

    # -- --session-file is single-session, no picker ------------------------ #
    def test_session_file_single_session(self):
        called = []
        c = self._select(["--session-file", "/tmp/x.json"], self._dir(),
                         select_fn=lambda *a: called.append(a))
        self.assertEqual(c.path, "/tmp/x.json")
        self.assertEqual(called, [])  # picker never shown

    # -- zero sessions -> silent fresh, no prompt --------------------------- #
    def test_zero_sessions_mints_fresh(self):
        cwd = self._dir()
        called = []
        c = self._select([], cwd, select_fn=lambda *a: called.append(a) or "new")
        self.assertIsNotNone(c.new_uuid)
        self.assertIn(c.new_uuid, c.path)
        self.assertEqual(called, [])  # no menu when nothing to resume

    # -- >=1 session + TTY -> resume-or-new menu ---------------------------- #
    def test_menu_resume_opens_picker(self):
        cwd = self._dir()
        p1 = self._seed(cwd, "s1", "first", mtime=100)
        p2 = self._seed(cwd, "s2", "second", mtime=200)
        answers = iter(["resume", p2])  # menu says resume, picker picks s2

        def fake_select(prompt, choices):
            return next(answers)
        c = self._select([], cwd, select_fn=fake_select)
        self.assertEqual(c.path, p2)
        self.assertFalse(c.cancelled)

    def test_menu_new_mints_fresh(self):
        cwd = self._dir()
        self._seed(cwd, "s1", "first")
        c = self._select([], cwd, select_fn=lambda *a: "new")
        self.assertIsNotNone(c.new_uuid)

    def test_menu_dismiss_is_cancelled(self):
        cwd = self._dir()
        self._seed(cwd, "s1", "first")
        c = self._select([], cwd, select_fn=lambda *a: None)
        self.assertTrue(c.cancelled)

    def test_picker_cancel_is_cancelled(self):
        cwd = self._dir()
        self._seed(cwd, "s1", "first")
        answers = iter(["resume", None])  # resume, then dismiss the picker
        c = self._select([], cwd, select_fn=lambda *a: next(answers))
        self.assertTrue(c.cancelled)

    # -- --new skips the prompt --------------------------------------------- #
    def test_new_flag_skips_prompt(self):
        cwd = self._dir()
        self._seed(cwd, "s1", "first")
        called = []
        c = self._select(["--new"], cwd,
                        select_fn=lambda *a: called.append(a) or "x")
        self.assertIsNotNone(c.new_uuid)
        self.assertEqual(called, [])

    # -- --resume opens the picker on a TTY --------------------------------- #
    def test_resume_flag_opens_picker(self):
        cwd = self._dir()
        p1 = self._seed(cwd, "s1", "first", mtime=100)
        p2 = self._seed(cwd, "s2", "second", mtime=200)
        c = self._select(["--resume"], cwd, select_fn=lambda prompt, ch: p1)
        self.assertEqual(c.path, p1)

    def test_resume_newest_first_order(self):
        cwd = self._dir()
        self._seed(cwd, "s1", "first", mtime=100)
        self._seed(cwd, "s2", "second", mtime=200)
        seen = {}

        def fake_select(prompt, choices):
            seen["choices"] = choices
            return choices[0][0]
        self._select(["--resume"], cwd, select_fn=fake_select)
        # newest (s2) is first in the picker
        self.assertIn("s2", seen["choices"][0][0])

    def test_resume_zero_sessions_errors(self):
        c = self._select(["--resume"], self._dir(),
                        select_fn=lambda *a: None)
        self.assertIsNotNone(c.error)

    def test_resume_non_tty_errors(self):
        cwd = self._dir()
        self._seed(cwd, "s1", "first")
        c = self._select(["--resume"], cwd, tty=False)
        self.assertIsNotNone(c.error)

    # -- non-TTY plain run -> most-recent, no crash ------------------------- #
    def test_non_tty_plain_continues_most_recent(self):
        cwd = self._dir()
        self._seed(cwd, "s1", "first", mtime=100)
        p2 = self._seed(cwd, "s2", "second", mtime=200)
        c = self._select([], cwd, tty=False)
        self.assertEqual(os.path.basename(c.path),
                         os.path.basename(p2))  # newest
        self.assertFalse(c.cancelled)
        self.assertIsNone(c.error)


class MultiSessionFlowTest(unittest.TestCase):
    """End-to-end run_flow over multiple resumable sessions in one directory."""

    def setUp(self):
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore)
        self.root = root
        self.cwd = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.cwd, ignore_errors=True))

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _all_trace_events(self):
        events = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                if name == "trace.jsonl":
                    with open(os.path.join(dirpath, name), "r") as fh:
                        events.extend(json.loads(l) for l in fh if l.strip())
        return events

    @contextlib.contextmanager
    def _chdir(self):
        prev = os.getcwd()
        os.chdir(self.cwd)
        try:
            yield
        finally:
            os.chdir(prev)

    def _scout(self):
        def fake_scout(config, context, selected, io_in=None, io_out=None,
                      resume_id=None, on_session=None, intel_path=None,
                      review_path=None, **kwargs):
            fake_scout.last_resume = resume_id
            fake_scout.last_intel = intel_path
            if on_session and resume_id is None:
                # The uuid now lives in the per-session FOLDER, not the intel
                # filename — derive the fake per-session id from the folder so it
                # stays unique across sessions.
                on_session("claude", "sess-" + os.path.basename(
                    os.path.dirname(intel_path or "x")))
            return 0
        fake_scout.last_resume = "unset"
        fake_scout.last_intel = None
        return fake_scout

    def test_two_new_runs_create_two_sessions_then_resume(self):
        scout = self._scout()
        with self._chdir():
            # Two --new runs -> two distinct resumable session files.
            cowork.run_flow(
                self._args(["--new", "--team", "scout",
                            "--config", "scout=claude,yolo,plan",
                            "--context", "alpha goal"]),
                io_out=io.StringIO(), which=lambda c: "/bin/" + c,
                run_scout_fn=scout)
            cowork.run_flow(
                self._args(["--new", "--team", "scout",
                            "--config", "scout=claude,yolo,plan",
                            "--context", "beta goal"]),
                io_out=io.StringIO(), which=lambda c: "/bin/" + c,
                run_scout_fn=scout)
            rows = state_store.list_sessions(self.cwd)
            self.assertEqual(len(rows), 2)
            summaries = sorted(r["summary"] for r in rows)
            self.assertEqual(summaries, ["alpha goal", "beta goal"])
            # Resume the alpha session via the picker; its scout session resumes.
            alpha = [r for r in rows if r["summary"] == "alpha goal"][0]
            import unittest.mock as mock
            with mock.patch.object(cowork.ui, "select",
                                   return_value=alpha["path"]), \
                    mock.patch.object(cowork.preflight, "preflight",
                                      return_value=(True, [])):
                rc = cowork.run_flow(
                    self._args(["--resume"]),
                    io_in=FakeTTY(), io_out=FakeTTY(),
                    which=lambda c: "/bin/" + c, run_scout_fn=scout)
            self.assertEqual(rc, 0)
        # The resumed run reused alpha's saved scout session id (recorded on the
        # first run, keyed by alpha's uuid).
        self.assertTrue(scout.last_resume.startswith("sess-"))
        self.assertIn(alpha["id"], scout.last_resume)

    def test_legacy_session_lists_and_resumes(self):
        scout = self._scout()
        with self._chdir():
            # Seed a legacy single-session file (no `created`), as written by an
            # older cowork.
            legacy = state_store.session_path(self.cwd)
            state_store.save(legacy, {
                "session_uuid": "legacy-uuid", "team": ["scout"],
                "config": {"scout": {"controller": "claude", "yolo": True,
                                     "mode": "plan"}},
                "sessions": {"scout": {"controller": "claude",
                                       "id": "legacy-sess"}},
                "context": {"text": "legacy goal", "revision": 1}})
            rows = state_store.list_sessions(self.cwd)
            self.assertEqual([r["id"] for r in rows], ["legacy-uuid"])
            self.assertEqual(rows[0]["summary"], "legacy goal")
            self.assertIsNone(rows[0]["created"])
            # Resume it via the picker -> the legacy scout session resumes.
            import unittest.mock as mock
            with mock.patch.object(cowork.ui, "select", return_value=legacy), \
                    mock.patch.object(cowork.preflight, "preflight",
                                      return_value=(True, [])):
                rc = cowork.run_flow(
                    self._args(["--resume"]),
                    io_in=FakeTTY(), io_out=FakeTTY(),
                    which=lambda c: "/bin/" + c, run_scout_fn=scout)
            self.assertEqual(rc, 0)
        self.assertEqual(scout.last_resume, "legacy-sess")

    def test_no_session_runs_full_flow(self):
        scout = self._scout()
        with self._chdir():
            rc = cowork.run_flow(
                self._args(["--no-session", "--team", "scout",
                            "--config", "scout=claude,yolo,plan",
                            "--context", "ephemeral goal"]),
                io_out=io.StringIO(), which=lambda c: "/bin/" + c,
                run_scout_fn=scout)
            self.assertEqual(rc, 0)
            # nothing persisted
            self.assertEqual(state_store.list_sessions(self.cwd), [])
        self.assertEqual(scout.last_resume, None)  # reached the scout fresh

    def test_session_select_error_traces_run_end(self):
        # A conflicting flag combo exits rc 2 AND records a run.end trace event.
        out = io.StringIO()
        with self._chdir():
            rc = cowork.run_flow(
                self._args(["--new", "--resume"]),
                io_out=out, which=lambda c: "/bin/" + c,
                run_scout_fn=self._scout())
        self.assertEqual(rc, 2)
        self.assertIn("--new and --resume", out.getvalue())
        ends = [e for e in self._all_trace_events()
                if e["event"] == "run.end" and e.get("rc") == 2
                and e.get("reason") == "session_select_error"]
        self.assertEqual(len(ends), 1)

    def test_session_select_cancel_traces_run_end(self):
        # A dismissed resume/new menu exits rc 0 (benign) AND traces run.end.
        scout = self._scout()
        out = FakeTTY()  # both streams must be TTY for the menu to show
        with self._chdir():
            # Seed a session so the resume-or-new menu is shown.
            state_store.ensure_session(
                state_store.new_session_path(self.cwd, "seed"), None, "seed")
            import unittest.mock as mock
            with mock.patch.object(cowork.ui, "select", return_value=None):
                rc = cowork.run_flow(
                    self._args([]), io_in=FakeTTY(), io_out=out,
                    which=lambda c: "/bin/" + c, run_scout_fn=scout)
        self.assertEqual(rc, 0)
        self.assertIn("cancelled; nothing to do", out.getvalue())
        ends = [e for e in self._all_trace_events()
                if e["event"] == "run.end" and e.get("rc") == 0
                and e.get("reason") == "session_select_cancelled"]
        self.assertEqual(len(ends), 1)


class TraceTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "trace.X.jsonl")

    def _events(self, path):
        with open(path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_trace_append_is_jsonl_and_does_not_touch_stdout(self):
        import contextlib
        path = self._tmp()
        trace = trace_store.Trace(path, session_uuid="X", run_id="R")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trace.event("status.read", role="scout", status="needs_input")
        self.assertEqual(out.getvalue(), "")
        events = self._events(path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "status.read")
        self.assertEqual(events[0]["session_uuid"], "X")
        self.assertEqual(events[0]["run_id"], "R")
        self.assertEqual(events[0]["status"], "needs_input")
        self.assertIn("ts", events[0])

    def test_command_meta_redacts_prompt_but_keeps_hash_and_length(self):
        secret = "please do not duplicate this prompt"
        meta = trace_store.command_meta(["codex", "exec", secret],
                                        prompt_text=secret)
        self.assertEqual(meta["argv"], ["codex", "exec", "<prompt>"])
        self.assertEqual(meta["prompt_bytes"], len(secret.encode("utf-8")))
        self.assertEqual(meta["prompt_sha256"],
                         trace_store.prompt_meta(secret)["prompt_sha256"])
        self.assertNotIn(secret, json.dumps(meta))

    def test_codex_session_traces_redacted_controller_metadata(self):
        import unittest.mock as mock

        class Proc:
            def __init__(self):
                self.stdout = iter([
                    json.dumps({"type": "thread.started", "thread_id": "T1"}),
                    json.dumps({"type": "turn.completed"}),
                ])

            def wait(self):
                return 0

        path = self._tmp()
        trace = trace_store.Trace(path, session_uuid="X", run_id="R")
        with mock.patch.object(bridge.subprocess, "Popen", return_value=Proc()):
            sess = bridge.CodexSession("implement", True, io_out=io.StringIO(),
                                      trace=trace)
            sess.send("secret prompt body")
        events = self._events(path)
        start = [e for e in events if e["event"] == "controller.turn.start"][0]
        self.assertEqual(start["controller"], "codex")
        self.assertEqual(start["argv"][-1], "<prompt>")
        self.assertIn("prompt_sha256", start)
        self.assertNotIn("secret prompt body", json.dumps(events))
        self.assertTrue(any(e["event"] == "controller.thread_id"
                            and e["thread_id"] == "T1" for e in events))


class SessionFlowTest(unittest.TestCase):
    def setUp(self):
        # Traces now live under ~/.cowork/sessions; pin the root to a tmp dir
        # so run_flow never writes to the real home dir.
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore)

    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _trace_events(self, session_path):
        saved = state_store.load(session_path)
        tpath = trace_store.trace_path_for(state_store.get_session_uuid(saved))
        with open(tpath, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_config_saved_then_reused_and_session_resumed(self):
        spath = self._tmp_session()

        def fake_scout(config, context, selected, io_in=None, io_out=None,
                      resume_id=None, on_session=None, intel_path=None,
                      review_path=None, **kwargs):
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
        # a cowork session uuid is minted and persisted; it isolates the intel
        # file via the per-session FOLDER, while the filename itself is uuid-free.
        suid = state_store.get_session_uuid(saved)
        self.assertTrue(suid)
        self.assertIn("scout.intel.json", fake_scout.last_intel)
        self.assertIn(suid, fake_scout.last_intel)  # uuid in the folder, not name

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

    def test_run_flow_traces_context_and_saved_session(self):
        spath = self._tmp_session()

        def fake_scout(config, context, selected, io_in=None, io_out=None,
                      resume_id=None, on_session=None, **kwargs):
            if on_session and resume_id is None:
                on_session("claude", "scout-trace-id")
            return 0

        rc = cowork.run_flow(
            self._args(["--team", "scout", "--context", "trace goal",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        events = self._trace_events(spath)
        self.assertTrue(any(e["event"] == "run.start" for e in events))
        self.assertTrue(any(e["event"] == "context.saved" for e in events))
        self.assertTrue(any(e["event"] == "role.session_saved"
                            and e["role"] == "scout"
                            and e["session_id"] == "scout-trace-id"
                            for e in events))
        self.assertTrue(any(e["event"] == "context.ack"
                            and e["role"] == "scout"
                            and e["revision"] == 1 for e in events))
        self.assertTrue(any(e["event"] == "run.end" and e["rc"] == 0
                            for e in events))

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

    def test_parse_claude_tool_use_block(self):
        ev = {"type": "stream_event", "event": {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "Bash"}}}
        self.assertEqual(bridge.parse_claude_event(ev),
                         {"kind": "tool", "name": "Bash"})
        # a missing tool name falls back to 'tool' (never 'using …')
        ev2 = {"type": "stream_event", "event": {
            "type": "content_block_start", "content_block": {"type": "tool_use"}}}
        self.assertEqual(bridge.parse_claude_event(ev2)["name"], "tool")
        # a text block start stays a no-text partial (spacing logic relies on it)
        ev3 = {"type": "stream_event", "event": {
            "type": "content_block_start", "content_block": {"type": "text"}}}
        self.assertEqual(bridge.parse_claude_event(ev3),
                         {"kind": "partial", "text": ""})

    def test_speaker_label(self):
        self.assertEqual(bridge.speaker_label("scout"), "scout › ")
        self.assertEqual(bridge.USER_LABEL, "you › ")

    def test_claude_command_streams_partials(self):
        cmd = bridge.build_claude_command("roles/scout.md", "implement", True)
        self.assertIn("--include-partial-messages", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)  # implement+yolo

    def test_parse_codex_events(self):
        ts = bridge.parse_codex_event({"type": "thread.started", "thread_id": "T1"})
        self.assertEqual(ts, {"kind": "thread_started", "thread_id": "T1",
                              "model": None})
        ts_model = bridge.parse_codex_event(
            {"type": "thread.started", "thread_id": "T1", "model": "gpt-5.3"})
        self.assertEqual(ts_model["model"], "gpt-5.3")
        msg = bridge.parse_codex_event(
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": "context map"}})
        self.assertEqual(msg, {"kind": "message", "text": "context map"})

    def test_parse_codex_tool_items(self):
        started = bridge.parse_codex_event(
            {"type": "item.started", "item": {"type": "command_execution",
                                              "command": "ls"}})
        self.assertEqual(started["kind"], "tool")
        self.assertEqual(started["label"], "running a command")
        mcp = bridge.parse_codex_event(
            {"type": "item.started", "item": {"type": "mcp_tool_call",
                                              "tool": "search"}})
        self.assertEqual(mcp["label"], "calling search")
        done = bridge.parse_codex_event(
            {"type": "item.completed", "item": {"type": "command_execution"}})
        self.assertEqual(done["kind"], "tool_done")
        # unknown item types stay 'other' — future codex events must not be
        # able to flip the activity label
        for etype in ("item.started", "item.completed"):
            other = bridge.parse_codex_event(
                {"type": etype, "item": {"type": "reasoning"}})
            self.assertEqual(other["kind"], "other")
        # rejected status still wins over the tool classification
        rej = bridge.parse_codex_event(
            {"type": "item.started", "item": {"type": "command_execution",
                                              "status": "rejected"}})
        self.assertEqual(rej["kind"], "denied")

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
            ["scout", "planner"],
            "/home/u/.cowork/sessions/S/scout.intel.S.json")
        self.assertIn("do NOT produce a plan", brief)
        self.assertIn("/home/u/.cowork/sessions/S/scout.intel.S.json", brief)
        self.assertIn("ONLY write target", brief)

    def test_brief_without_planner(self):
        brief = cowork.assemble_scout_brief(
            ["scout", "planning-advisor"],
            "/home/u/.cowork/sessions/S/scout.intel.S.json")
        self.assertIn("lightweight plan", brief)

    def test_brief_requires_json(self):
        brief = cowork.assemble_scout_brief(["scout"], "/tmp/x.json")
        self.assertIn("JSON", brief)

    def test_scout_intel_path(self):
        # No-uuid filename: the per-session folder isolates it; the session_uuid
        # arg is kept for call-site stability but unused.
        self.assertEqual(
            cowork.scout_intel_path(".cowork", "abc-123"),
            ".cowork/scout.intel.json")

    def test_codex_prompt_includes_all_parts(self):
        prompt = cowork.assemble_codex_prompt("ROLE", "TEAM", "CTX")
        self.assertIn("ROLE", prompt)
        self.assertIn("TEAM", prompt)
        self.assertIn("CTX", prompt)


class RunScoutTest(unittest.TestCase):
    def test_run_scout_opencode_seeds_brief_plus_context_only(self):
        import tempfile
        config = {"scout": {"controller": "opencode", "model": None,
                            "effort": None, "yolo": True, "mode": "implement"}}
        intel = os.path.join(tempfile.mkdtemp(), "scout.intel.json")
        seen = {}
        prompts = []

        def factory(controller, resume_session_id=None, on_session_id=None):
            seen["controller"] = controller
            seen["resume_session_id"] = resume_session_id

            class FakeScout:
                def send(self, text, meta=None):
                    prompts.append(text)
                    return {"ok": True, "result": "ok"}

                def close(self):
                    pass
            return FakeScout()

        out = io.StringIO()
        rc = cowork.run_scout(config, "build the thing", ["scout"],
                              io_in=io.StringIO(""), io_out=out,
                              intel_path=intel, session_factory=factory)
        self.assertEqual(rc, 0)
        self.assertEqual(seen["controller"], "opencode")
        self.assertIsNone(seen["resume_session_id"])
        self.assertIn("build the thing", prompts[0])
        # The role prompt is delivered via the generated agent file (system
        # prompt), never inlined into the seed — unlike the codex path.
        role_text = cowork.read_scout_prompt().strip()
        self.assertNotIn(role_text[:80], prompts[0])

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

    def _trace(self, intel_path):
        return trace_store.Trace(
            os.path.join(os.path.dirname(intel_path), "trace.X.jsonl"),
            session_uuid="X", run_id="R")

    def _trace_events(self, intel_path):
        path = os.path.join(os.path.dirname(intel_path), "trace.X.jsonl")
        with open(path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

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
        trace = self._trace(intel)
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="ctx",
            io_in=io.StringIO("answer 1\n\n"), io_out=out, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "answer 1"])
        self.assertTrue(sess.closed)
        text = out.getvalue()
        self.assertIn("scout needs your input", text)
        self.assertIn("ready for review", text)
        self.assertIn("scout finished", text)
        events = self._trace_events(intel)
        self.assertTrue(any(e["event"] == "status.read"
                            and e["status"] == "needs_input" for e in events))
        self.assertTrue(any(e["event"] == "gate.show"
                            and e["gate"] == "needs_input" for e in events))
        self.assertTrue(any(e["event"] == "user.action"
                            and e["action"] == "answer" for e in events))
        self.assertTrue(any(e["event"] == "gate.show"
                            and e["gate"] == "ready_for_review" for e in events))
        self.assertTrue(any(e["event"] == "user.action"
                            and e["action"] == "approve" for e in events))

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

    def test_blank_reprompts_then_eof_ends(self):
        # A blank line no longer aborts (#10): it re-prompts. Here the blank is
        # followed by EOF, which legitimately ends the loop — so still only the
        # seed was sent. The re-prompt means send() was NOT called again.
        intel = self._intel()
        sess = self._session(intel, ["needs_input"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="", io_in=io.StringIO("\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])  # blank re-prompted, EOF then ended
        self.assertTrue(sess.closed)

    def test_blank_reprompts_then_answers(self):
        # Prove a blank line re-prompts rather than ending: a blank followed by a
        # real answer must still deliver that answer as the next turn.
        intel = self._intel()
        sess = self._session(intel, ["needs_input", "ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="",
            io_in=io.StringIO("\nreal answer\n\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "real answer"])
        self.assertTrue(sess.closed)

    def test_slash_quit_ends_loop(self):
        intel = self._intel()
        sess = self._session(intel, ["needs_input"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="",
            io_in=io.StringIO("/quit\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])  # /quit ended before another send
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
        self.assertEqual(s.controller, "claude")
        self.assertIn("scout › hi", out.getvalue())
        self.assertEqual(got.get("id"), "S1")
        self.assertEqual(s.proc.stdin.data[0],
                         bridge.encode_user_message("hello"))

    def test_claude_session_result_error_is_structured_failure(self):
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
            json.dumps({"type": "result", "subtype": "error_during_execution",
                        "result": "limit reached", "session_id": "SERR"}),
        ]
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "implement", True,
                                     io_out=out)
            result = s.send("go")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "error")
        self.assertEqual(result["subtype"], "error_during_execution")
        self.assertIn("limit reached", out.getvalue())

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

    def test_claude_session_non_tty_ignores_tool_events(self):
        # Tool/user events interleaved between tokens must leave the non-TTY
        # output byte-identical — exact assertion, not just same-as-other-run.
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
                "delta": {"type": "text_delta", "text": "hi"}}}),
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Bash"}}}),
            json.dumps({"type": "user", "message": {"content": []}}),
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": " there"}}}),
            json.dumps({"type": "result", "subtype": "success", "result": ""}),
        ]
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "implement", True, io_out=out)
            s.send("go")
        self.assertEqual(out.getvalue(), "\nscout › hi there\n")

    def test_codex_session_first_then_resume(self):
        recorded = {"cmds": [], "tid": None}

        class FakeCodex(bridge.CodexSession):
            def _run(self, command):
                recorded["cmds"].append(command)
                return [{"type": "thread.started", "thread_id": "T1"}]

        s = FakeCodex("implement", True, io_out=io.StringIO(),
                      on_thread_id=lambda i: recorded.__setitem__("tid", i))
        self.assertEqual(s.controller, "codex")
        s.send("first")
        s.send("second")
        self.assertEqual(recorded["cmds"][0][:4],
                         ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertEqual(recorded["cmds"][0][-1], "first")
        # implement + yolo: resume re-applies the bypass flag (see
        # codex_resume_mode_args) and addresses the thread by explicit id.
        self.assertEqual(
            recorded["cmds"][1],
            ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox", "T1", "second"])
        self.assertEqual(recorded["tid"], "T1")

    def test_codex_session_error_denied_and_missing_thread_are_failures(self):
        class ErrorCodex(bridge.CodexSession):
            def _run(self, command):
                return [{"type": "thread.started", "thread_id": "T1"},
                        {"type": "error", "message": "boom"}]

        err = ErrorCodex("implement", True, io_out=io.StringIO()).send("x")
        self.assertFalse(err["ok"])
        self.assertEqual(err["result"], "error")

        class DeniedCodex(bridge.CodexSession):
            def _run(self, command):
                return [{"type": "thread.started", "thread_id": "T1"},
                        {"type": "item.completed",
                         "item": {"type": "file_change",
                                  "status": "denied", "text": "no"}}]

        denied = DeniedCodex("implement", True, io_out=io.StringIO()).send("x")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["result"], "denied")
        self.assertTrue(denied["denied"])

        missing = bridge.CodexSession(
            "implement", True, io_out=io.StringIO(),
            resume_thread_id="old")
        missing.thread_id = None
        missing._started = True
        result = missing.send("next")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "missing_thread_id")

    def test_codex_tool_activity_retitles_spinner(self):
        import unittest.mock as mock

        class RecSpinner:
            insts = []

            def __init__(self, out, label="working"):
                self.labels = [label]
                self.stops = 0
                RecSpinner.insts.append(self)

            def __enter__(self):
                return self

            def set_label(self, text):
                self.labels.append(text)

            def stop(self):
                self.stops += 1

            def __exit__(self, *exc):
                self.stop()

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        lines = [
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "item.started",
                        "item": {"type": "command_execution"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "command_execution"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"}}),
        ]
        RecSpinner.insts.clear()
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)), \
                mock.patch.object(bridge, "_Spinner", RecSpinner):
            out = io.StringIO()
            s = bridge.CodexSession("implement", True, io_out=out)
            s.send("go")
        spin = RecSpinner.insts[0]
        self.assertEqual(spin.labels, ["scout working",
                                       "scout running a command",
                                       "scout working"])
        self.assertGreaterEqual(spin.stops, 1)  # stopped on the emitted message
        self.assertIn("scout › done", out.getvalue())


class ScoutReviewerRegistrationTest(unittest.TestCase):
    def test_role_registered_with_codex_yolo_implement(self):
        self.assertIn("scout-reviewer", cowork.ROLES)
        # placed right after scout (paired reviewer)
        self.assertEqual(cowork.ROLES.index("scout-reviewer"), 1)
        self.assertNotIn("revisor", cowork.ROLES)  # reserved slot dropped
        self.assertEqual(
            cowork.DEFAULTS["scout-reviewer"],
            {"controller": "codex", "model": None, "effort": None,
             "yolo": True, "mode": "implement"})

    def test_role_prompt_file_exists(self):
        self.assertTrue(os.path.exists(cowork.SCOUT_REVIEWER_PROMPT_PATH))


class ReadReviewTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout-review.X.json")

    def _write(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(obj, fh)

    def test_missing_returns_none(self):
        self.assertIsNone(state_store.read_review(self._tmp()))
        self.assertIsNone(state_store.read_review(""))

    def test_valid_verdicts_preserved(self):
        path = self._tmp()
        self._write(path, {"verdict": "approve", "findings": []})
        self.assertEqual(state_store.read_review(path)["verdict"], "approve")
        self._write(path, {"verdict": "needs_user",
                           "user_question": "per-device or per-account?"})
        got = state_store.read_review(path)
        self.assertEqual(got["verdict"], "needs_user")
        self.assertEqual(got["user_question"], "per-device or per-account?")

    def test_malformed_degrades_to_safe_revise(self):
        path = self._tmp()
        # present but no/invalid verdict -> safe non-approving default
        self._write(path, {"role": "scout-reviewer", "findings": ["x"]})
        got = state_store.read_review(path)
        self.assertEqual(got["verdict"], "revise")
        self.assertTrue(got["malformed"])
        self._write(path, {"verdict": "maybe"})
        self.assertEqual(state_store.read_review(path)["verdict"], "revise")

    def test_needs_user_without_question_degrades_to_revise(self):
        path = self._tmp()
        self._write(path, {"verdict": "needs_user"})            # no user_question
        got = state_store.read_review(path)
        self.assertEqual(got["verdict"], "revise")
        self.assertTrue(got["malformed"])
        self._write(path, {"verdict": "needs_user", "user_question": "   "})
        self.assertEqual(state_store.read_review(path)["verdict"], "revise")

    def test_non_json_returns_none(self):
        path = self._tmp()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertIsNone(state_store.read_review(path))

    def test_review_path_for(self):
        # The uuid lives in the per-session folder, not the filename; the
        # session_uuid param is kept for call-site stability but unused.
        self.assertEqual(
            state_store.review_path_for(".cowork", "abc-123"),
            ".cowork/scout-review.json")


class ReviewerContextTest(unittest.TestCase):
    """B1 guard: the reviewer shares the user context + intel, NOT the scout's
    write-target brief / first payload."""

    def _intel(self, obj):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = os.path.join(d, ".cowork", "scout.intel.X.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    def test_context_has_user_context_and_intel_not_scout_brief(self):
        intel = self._intel({"status": "ready_for_review",
                             "result": {"objective": "dark mode toggle"}})
        selected = ["scout", "scout-reviewer"]
        ctx = cowork.assemble_reviewer_context(
            "add a dark-mode toggle", selected, intel)
        self.assertIn("add a dark-mode toggle", ctx)      # shared user context
        self.assertIn("dark mode toggle", ctx)            # intel JSON embedded
        # B1: the scout's write-target brief must NOT leak into the reviewer.
        scout_brief = cowork.assemble_scout_brief(selected, intel)
        self.assertNotIn(scout_brief, ctx)
        self.assertNotIn("do NOT produce a plan", ctx)

    def test_reviewer_brief_targets_review_file_only(self):
        brief = cowork.assemble_reviewer_brief(".cowork/scout-review.X.json")
        self.assertIn(".cowork/scout-review.X.json", brief)
        self.assertIn("ONLY write target", brief)
        self.assertIn("Do NOT edit the scout intel", brief)


class ReviewerHandoffTest(unittest.TestCase):
    """Faithful-relay handoff template (pure string templating, no model call)."""

    def test_needs_user_carries_full_question_and_relay_instruction(self):
        out = cowork.assemble_reviewer_handoff(
            "needs_user",
            {"user_question": "Persist per-device, or per-account when logged in?"})
        self.assertIn("[reviewer handoff]", out)
        self.assertIn("Persist per-device, or per-account when logged in?", out)
        self.assertIn("NOT change its meaning", out)
        self.assertIn("needs_input", out)

    def test_revise_lists_findings(self):
        out = cowork.assemble_reviewer_handoff(
            "revise", {"findings": ["cited file is wrong", "tighten assumption Y"]})
        self.assertIn("[reviewer handoff]", out)
        self.assertIn("cited file is wrong", out)
        self.assertIn("tighten assumption Y", out)
        self.assertIn("ready_for_review", out)

    def test_approve_is_empty(self):
        self.assertEqual(cowork.assemble_reviewer_handoff("approve", {}), "")


class ScoutLoopReviewTest(unittest.TestCase):
    """Drive _scout_loop with an injected review_fn (topology D)."""

    def _intel(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def _trace(self, intel_path):
        return trace_store.Trace(
            os.path.join(os.path.dirname(intel_path), "trace.X.jsonl"),
            session_uuid="X", run_id="R")

    def _trace_events(self, intel_path):
        path = os.path.join(os.path.dirname(intel_path), "trace.X.jsonl")
        with open(path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _session(self, intel_path, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                os.makedirs(os.path.dirname(intel_path), exist_ok=True)
                with open(intel_path, "w") as fh:
                    json.dump({"status": st}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def _review_fn(self, verdicts):
        calls = {"n": 0}

        def review_fn(intel_path, round_index):
            calls["n"] += 1
            return verdicts.pop(0) if verdicts else {"verdict": "approve"}
        review_fn.calls = calls
        return review_fn

    def test_revise_injected_then_approve_runs_user_gate(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        rfn = self._review_fn([
            {"verdict": "revise", "findings": ["fix the cited path"]},
            {"verdict": "approve"},
        ])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""),  # "" at the gate => approve
                                io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        # the reviewer's revise was injected as the scout's next turn...
        self.assertEqual(len(sess.sent), 2)
        self.assertEqual(sess.sent[0], "seed")
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertIn("fix the cited path", sess.sent[1])
        # ...and the reviewer ran twice; the user only saw the 'reviewed' marker.
        self.assertEqual(rfn.calls["n"], 2)
        text = out.getvalue()
        self.assertIn("reviewed: changes requested", text)
        self.assertIn("reviewed: approved", text)
        # single-voice: reviewer finding text never reached the user channel.
        self.assertNotIn("fix the cited path", text)

    def test_round_cap_falls_through_to_user_with_dissent(self):
        intel = self._intel()
        # Fixture sizes derive from the constant so cap changes don't break it:
        # cap revise verdicts, the last with a distinctive finding, plus one
        # sentinel that must never be consumed.
        cap = cowork.REVIEW_ROUND_CAP
        sess = self._session(intel, ["ready_for_review"] * (cap + 1))
        rfn = self._review_fn(
            [{"verdict": "revise", "findings": ["concern %d" % i]}
             for i in range(1, cap)]
            + [{"verdict": "revise", "findings": ["still not aligned"]},
               {"verdict": "revise", "findings": ["should not be reached"]}])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        # reviewer called at most cap times, then user gate with dissent
        self.assertEqual(rfn.calls["n"], cowork.REVIEW_ROUND_CAP)
        text = out.getvalue()
        self.assertIn("review cap reached (%d rounds)" % cap, text)
        self.assertIn("reviewer's unresolved notes", text)
        self.assertIn("still not aligned", text)
        # the badge counter shows budget progress up to the cap
        self.assertIn("reviewed: changes requested (round 1/%d)" % cap, text)
        self.assertIn("reviewed: changes requested (round %d/%d)" % (cap, cap),
                      text)

    def test_needs_user_drives_scout_back_to_user_question(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "needs_input"])
        rfn = self._review_fn([
            {"verdict": "needs_user",
             "user_question": "per-device or per-account?"},
        ])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""),  # EOF at the needs_input turn
                                io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sess.sent), 2)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertIn("per-device or per-account?", sess.sent[1])
        self.assertIn("needs_input", sess.sent[1])

    def test_missing_review_surfaces_failure_gate_not_silent_approve(self):
        # review_fn returns None (missing/unreadable verdict) every time: a
        # no-usable-verdict failure. After REVIEW_FAIL_CAP consecutive failures
        # the user sees the retry/skip-review/end gate — never a silent approval
        # and never an endless bounce through the role.
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        calls = {"n": 0}

        def review_fn(intel_path, round_index):
            calls["n"] += 1
            return None

        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO("end\n"), io_out=out,
                                review_fn=review_fn)
        self.assertEqual(rc, 0)
        # one silent auto-retry, then the gate: review_fn ran exactly FAIL_CAP times
        self.assertEqual(calls["n"], cowork.REVIEW_FAIL_CAP)
        self.assertIn("could not return a usable verdict", out.getvalue())
        # the role was never bounced — only the seed was ever sent
        self.assertEqual(sess.sent, ["seed"])

    def test_unknown_verdict_single_then_recovers_no_gate(self):
        # A single bad verdict (unknown value) is tolerated by ONE silent
        # auto-retry; the reviewer recovers on the retry, so no gate is shown and
        # the role is never bounced.
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        rfn = self._review_fn([{"verdict": "lgtm"}, {"verdict": "approve"}])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        self.assertEqual(rfn.calls["n"], 2)          # bad verdict + silent retry
        self.assertNotIn("could not return a usable verdict", out.getvalue())
        self.assertNotIn("[reviewer handoff]", "".join(sess.sent))  # no bounce
        self.assertIn("scout finished", out.getvalue())             # approved

    def test_needs_user_without_question_is_failure_not_empty_relay(self):
        # needs_user with a blank question can't be relayed faithfully -> a
        # failure, NOT an empty needs_user relay. One silent retry recovers here.
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        rfn = self._review_fn([{"verdict": "needs_user", "user_question": ""},
                               {"verdict": "approve"}])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        self.assertEqual(rfn.calls["n"], 2)
        joined = "".join(sess.sent)
        self.assertNotIn("Question:", joined)        # never an empty relay
        self.assertNotIn("[reviewer handoff]", joined)  # recovered -> no bounce

    def test_no_review_fn_keeps_legacy_user_gate(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out)  # no review_fn
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])
        self.assertNotIn("reviewed", out.getvalue())

    def test_user_revision_invalidates_stale_ready_before_next_turn(self):
        intel = self._intel()

        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                os.makedirs(os.path.dirname(intel), exist_ok=True)
                if len(self.sent) == 1:
                    with open(intel, "w") as fh:
                        json.dump({"status": "ready_for_review",
                                   "result": {"summary": "old ready"}}, fh)
                else:
                    # Turn 2: the role genuinely rewrites the artifact (a new
                    # needs_input question), so it is real progress — not the
                    # stale-no-op the detector targets.
                    with open(intel, "w") as fh:
                        json.dump({"status": "needs_input",
                                   "result": {"question": "what about X?"}}, fh)

            def close(self):
                self.closed = True

        sess = FakeSession()
        rfn = self._review_fn([{"verdict": "approve"}])
        trace = self._trace(intel)
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO("new concern\n"),
                                io_out=out, review_fn=rfn, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "new concern"])
        self.assertTrue(sess.closed)
        self.assertEqual(rfn.calls["n"], 1)
        text = out.getvalue()
        self.assertIn("scout needs your input", text)
        self.assertNotIn("scout finished", text)
        with open(intel, "r") as fh:
            self.assertEqual(json.load(fh)["status"], "needs_input")
        events = self._trace_events(intel)
        self.assertTrue(any(e["event"] == "review.verdict"
                            and e["verdict"] == "approve" for e in events))
        self.assertTrue(any(e["event"] == "user.action"
                            and e["action"] == "revise" for e in events))
        self.assertTrue(any(e["event"] == "status.invalidated"
                            and e["changed"] for e in events))
        self.assertTrue(any(e["event"] == "gate.show"
                            and e["gate"] == "needs_input" for e in events))


class StaleNoOpTest(unittest.TestCase):
    """Stale-no-op detection / one-shot repair / visible stuck gate, driven
    directly against the shared `_role_loop` so all three user-facing roles
    inherit the behavior. A `ScriptedSession` controls exactly what (if
    anything) each turn writes to the status file, so a byte-identical (or
    absent) write models the deadlock the detector targets."""

    def _path(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def _trace(self, path):
        return trace_store.Trace(
            os.path.join(os.path.dirname(path), "trace.X.jsonl"),
            session_uuid="X", run_id="R")

    def _events(self, path):
        tpath = os.path.join(os.path.dirname(path), "trace.X.jsonl")
        with open(tpath, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _session(self, path, writes):
        """`writes` is a per-send list. Each entry is either a dict (written as
        the status artifact) or None (the turn writes nothing — a no-op)."""
        class ScriptedSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                w = writes.pop(0) if writes else None
                if w is not None:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as fh:
                        json.dump(w, fh)

            def close(self):
                self.closed = True
        return ScriptedSession()

    def _review_fn(self, verdicts):
        calls = {"n": 0}

        def review_fn(status_path, round_index):
            calls["n"] += 1
            return verdicts.pop(0) if verdicts else {"verdict": "approve"}
        review_fn.calls = calls
        return review_fn

    _READY = {"status": "ready_for_review", "result": {}}

    def test_t1_detect_repair_fires(self):
        path = self._path()
        # turn1 ready -> user revise -> turn2 NO-OP -> repair -> turn3 progress.
        sess = self._session(path, [
            dict(self._READY),
            None,
            {"status": "needs_input", "result": {"q": "more?"}}])
        trace = self._trace(path)
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\n"), io_out=io.StringIO(), trace=trace)
        self.assertEqual(rc, 0)
        # The repair prompt was sent on the turn after the no-op.
        self.assertEqual(len(sess.sent), 3)
        self.assertIn("byte-identical", sess.sent[2])
        events = self._events(path)
        sn = [e for e in events if e["event"] == "stale_noop"]
        self.assertEqual(len(sn), 1)
        self.assertEqual(sn[0]["reopen_reason"], "user_revise")
        self.assertTrue(sn[0]["repair_attempted"])
        self.assertFalse(any(e["event"] == "stale_noop.unresolved"
                             for e in events))

    def test_t2_repair_succeeds_reviewer_runs(self):
        path = self._path()
        # turn1 ready -> reviewer approve -> user revise -> turn2 no-op ->
        # repair -> turn3 ready again -> reviewer runs again -> approve.
        sess = self._session(path, [dict(self._READY), None, dict(self._READY)])
        rfn = self._review_fn([{"verdict": "approve"}, {"verdict": "approve"}])
        trace = self._trace(path)
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\n"), io_out=out,
            review_fn=rfn, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "approved")
        # Reviewer ran on the first ready AND on the repaired ready.
        self.assertEqual(rfn.calls["n"], 2)
        events = self._events(path)
        self.assertTrue(any(e["event"] == "stale_noop" for e in events))
        self.assertFalse(any(e["event"] == "stale_noop.unresolved"
                             for e in events))
        self.assertNotIn("appears stuck", out.getvalue())

    def test_t3_repair_fails_stuck_gate_end(self):
        path = self._path()
        # turn1 ready -> revise -> turn2 no-op -> repair -> turn3 no-op ->
        # stuck gate -> 'end'.
        sess = self._session(path, [dict(self._READY), None, None])
        trace = self._trace(path)
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\nend\n"), io_out=out, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "ended")
        self.assertIn("appears stuck", out.getvalue())
        events = self._events(path)
        sn = [e for e in events if e["event"] == "stale_noop"]
        unr = [e for e in events if e["event"] == "stale_noop.unresolved"]
        self.assertEqual(len(sn), 1)
        self.assertEqual(len(unr), 1)
        # The unresolved event carries the SAME reopen_reason as the first.
        self.assertEqual(unr[0]["reopen_reason"], sn[0]["reopen_reason"])
        self.assertEqual(unr[0]["reopen_reason"], "user_revise")
        self.assertTrue(any(e["event"] == "user.action"
                            and e["action"] == "stuck_end" for e in events))

    def test_t3b_stuck_gate_retry_progress(self):
        path = self._path()
        # ... -> stuck gate -> 'retry' -> role writes ready -> proceeds, no
        # second gate.
        sess = self._session(
            path, [dict(self._READY), None, None, dict(self._READY)])
        trace = self._trace(path)
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\nretry\n"), io_out=out, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "approved")
        events = self._events(path)
        self.assertTrue(any(e["event"] == "user.action"
                            and e["action"] == "stuck_retry" for e in events))
        # The retry re-ran the role with the repair prompt (the 4th send).
        self.assertEqual(len(sess.sent), 4)
        self.assertIn("byte-identical", sess.sent[3])
        # Gate shown exactly once (progress after retry -> not re-shown).
        self.assertEqual(out.getvalue().count("appears stuck"), 1)

    def test_t3b_stuck_gate_retry_then_noop_reshows(self):
        path = self._path()
        # ... -> stuck gate -> 'retry' -> role no-ops AGAIN -> gate re-shown.
        sess = self._session(path, [dict(self._READY), None, None, None])
        trace = self._trace(path)
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\nretry\nend\n"), io_out=out, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "ended")
        # Gate shown twice: once after auto-repair, once after the retry no-op.
        self.assertEqual(out.getvalue().count("appears stuck"), 2)

    def test_t3c_stuck_gate_inspect_is_read_only(self):
        path = self._path()
        # A distinctive marker in the artifact's result survives the
        # ready->needs_input invalidation (invalidate preserves result), so we
        # can prove the RAW file content — not just the path/status labels — was
        # emitted by inspect.
        marker = "INSPECT_MARKER_9F3A"
        first = {"status": "ready_for_review", "result": {"marker": marker}}
        sess = self._session(path, [first, None, None])
        trace = self._trace(path)
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\ninspect\nend\n"), io_out=out,
            trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "ended")
        text = out.getvalue()
        # Inspect emitted the artifact path + on-disk status + raw content.
        self.assertIn("status file:", text)
        self.assertIn("on-disk status:", text)
        # The raw artifact body itself was printed (the marker only appears in
        # the file content dump, never in the labels).
        self.assertIn(marker, text)
        # Inspect ran NO role turn: only seed, revise, and the single repair
        # send happened — inspect added no 4th send.
        self.assertEqual(len(sess.sent), 3)
        events = self._events(path)
        self.assertTrue(any(e["event"] == "user.action"
                            and e["action"] == "stuck_inspect" for e in events))
        # Gate re-shown after inspect (so two banners total).
        self.assertEqual(text.count("appears stuck"), 2)

    def test_t4_legit_new_question_no_false_positive(self):
        path = self._path()
        # turn1 ready -> revise -> turn2 rewrites a NEW needs_input question
        # (different bytes) -> progress, NOT a stale no-op.
        sess = self._session(path, [
            dict(self._READY),
            {"status": "needs_input", "result": {"q": "brand new question"}}])
        trace = self._trace(path)
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\n"), io_out=io.StringIO(), trace=trace)
        self.assertEqual(rc, 0)
        events = self._events(path)
        self.assertFalse(any(e["event"] == "stale_noop" for e in events))
        self.assertEqual(sess.sent, ["seed", "fix it"])

    def test_t5_invalidation_trace_actual_status(self):
        path = self._path()
        sess = self._session(path, [
            dict(self._READY),
            {"status": "needs_input", "result": {"q": "next?"}}])
        trace = self._trace(path)
        cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("fix it\n"), io_out=io.StringIO(), trace=trace)
        events = self._events(path)
        inv = [e for e in events if e["event"] == "status.invalidated"
               and e.get("reason") == "work_reopened"]
        self.assertTrue(inv)
        # The previously-ambiguous changed:false case is now self-explanatory:
        # the event records the REAL on-disk status before and after.
        self.assertEqual(inv[0]["before_status"], "ready_for_review")
        self.assertEqual(inv[0]["after_status"], "needs_input")

    # A genuinely-new artifact written on the reopened turn (different raw
    # bytes) — used for the content-changing no-false-positive half of T7.
    _DIFF = {"status": "needs_input", "result": {"q": "genuinely new question"}}

    def test_t7_general_invariant_reopen_sources(self):
        # The general invariant (D1/D9): for EVERY work-reopening source, assert
        # BOTH halves — a byte-identical role turn triggers exactly one repair
        # (stale_noop with the matching reopen_reason), AND a content-changing
        # turn does NOT (no false positive). user_iterate is exercised the same
        # way in test_t7_user_iterate_source (it needs the TTY dissent gate to
        # emit _ITERATE); handoff_declined in test_t7_handoff_declined_source.
        # review_fn is single-use (pops), so each scenario gets a fresh one via
        # the factory.
        cases = [
            # (reason, review_fn_factory, noop_io, content_io)
            ("user_revise", lambda: None, "feedback\nend\n", "feedback\n"),
            ("reviewer_revise",
             lambda: self._review_fn([{"verdict": "revise", "findings": ["x"]}]),
             "end\n", ""),
            ("reviewer_needs_user",
             lambda: self._review_fn(
                 [{"verdict": "needs_user", "user_question": "which?"}]),
             "end\n", ""),
        ]
        for reason, rfn_factory, noop_io, content_io in cases:
            with self.subTest(reason=reason, mode="noop"):
                path = self._path()
                sess = self._session(path, [dict(self._READY), None, None])
                trace = self._trace(path)
                cowork._role_loop(
                    sess, "seed", path, context="",
                    io_in=io.StringIO(noop_io), io_out=io.StringIO(),
                    review_fn=rfn_factory(), trace=trace)
                events = self._events(path)
                sn = [e for e in events if e["event"] == "stale_noop"]
                self.assertEqual(len(sn), 1, "exactly one repair for %s" % reason)
                self.assertEqual(sn[0]["reopen_reason"], reason)
            with self.subTest(reason=reason, mode="content"):
                path = self._path()
                sess = self._session(path, [dict(self._READY), dict(self._DIFF)])
                trace = self._trace(path)
                cowork._role_loop(
                    sess, "seed", path, context="",
                    io_in=io.StringIO(content_io), io_out=io.StringIO(),
                    review_fn=rfn_factory(), trace=trace)
                events = self._events(path)
                self.assertFalse(
                    any(e["event"] == "stale_noop" for e in events),
                    "no false positive for content-changing %s" % reason)

    def test_t7_handoff_declined_source(self):
        path = self._path()
        # handoff_back -> declined -> inline invalidate -> next turn no-op.
        # The declined branch sets reason WITHOUT pending_reopens_work, so this
        # proves detection keys off the reason, not the boolean.
        sess = self._session(path, [
            {"status": "handoff_back", "handoff": "re-plan auth"},
            None, None])
        trace = self._trace(path)
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            handoff_enabled=True,
            handoff_confirm=lambda io_in, io_out: False,  # decline
            trace=trace)
        events = self._events(path)
        sn = [e for e in events if e["event"] == "stale_noop"]
        self.assertEqual(len(sn), 1)
        self.assertEqual(sn[0]["reopen_reason"], "handoff_declined")

    def test_t7_handoff_declined_content_change_no_false_positive(self):
        path = self._path()
        # Declined hand-back, then the role genuinely rewrites the artifact
        # (different bytes) -> progress, NOT a stale no-op.
        sess = self._session(path, [
            {"status": "handoff_back", "handoff": "re-plan auth"},
            dict(self._DIFF)])
        trace = self._trace(path)
        cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO(""), io_out=io.StringIO(),
            handoff_enabled=True,
            handoff_confirm=lambda io_in, io_out: False,  # decline
            trace=trace)
        events = self._events(path)
        self.assertFalse(any(e["event"] == "stale_noop" for e in events))

    def test_t7_user_iterate_source(self):
        # user_iterate reaches the reopen seam only via the TTY dissent gate
        # (_read_review_dissent -> _ITERATE). Force the dissent path with a
        # one-round review cap and patch the dissent reader to iterate once.
        import unittest.mock as mock
        path = self._path()
        sess = self._session(path, [dict(self._READY), None, None])
        rfn = self._review_fn([{"verdict": "revise", "findings": ["y"]}])
        trace = self._trace(path)
        with mock.patch.object(cowork, "REVIEW_ROUND_CAP", 1), \
                mock.patch.object(cowork, "_read_review_dissent",
                                  return_value=cowork._ITERATE):
            cowork._role_loop(
                sess, "seed", path, context="",
                io_in=io.StringIO("end\n"), io_out=io.StringIO(),
                review_fn=rfn, trace=trace)
        events = self._events(path)
        sn = [e for e in events if e["event"] == "stale_noop"]
        self.assertEqual(len(sn), 1)
        self.assertEqual(sn[0]["reopen_reason"], "user_iterate")

        # Content-changing half: an iterate that genuinely rewrites the artifact
        # is progress, NOT a stale no-op.
        path2 = self._path()
        sess2 = self._session(path2, [dict(self._READY), dict(self._DIFF)])
        rfn2 = self._review_fn([{"verdict": "revise", "findings": ["y"]}])
        trace2 = self._trace(path2)
        with mock.patch.object(cowork, "REVIEW_ROUND_CAP", 1), \
                mock.patch.object(cowork, "_read_review_dissent",
                                  return_value=cowork._ITERATE):
            cowork._role_loop(
                sess2, "seed", path2, context="",
                io_in=io.StringIO(""), io_out=io.StringIO(),
                review_fn=rfn2, trace=trace2)
        events2 = self._events(path2)
        self.assertFalse(any(e["event"] == "stale_noop" for e in events2))

    def test_t7_user_answer_source(self):
        # The ORIGINALLY-reported deadlock path: the role is at the needs_input
        # gate, the user answers, and the role consumes the answer but leaves
        # the artifact byte-identical. This is the source the whole feature was
        # motivated by, so it gets explicit both-halves coverage.
        first = {"status": "needs_input", "result": {"q": "first"}}

        # No-op half: a byte-identical answer turn -> exactly one repair tagged
        # user_answer.
        path = self._path()
        sess = self._session(path, [dict(first), None, None])
        trace = self._trace(path)
        cowork._role_loop(
            sess, "seed", path, context="",
            io_in=io.StringIO("my answer\nend\n"), io_out=io.StringIO(),
            trace=trace)
        events = self._events(path)
        sn = [e for e in events if e["event"] == "stale_noop"]
        self.assertEqual(len(sn), 1)
        self.assertEqual(sn[0]["reopen_reason"], "user_answer")

        # Content-changing half: an answer turn that rewrites the artifact with
        # new bytes is progress, NOT a stale no-op.
        path2 = self._path()
        sess2 = self._session(path2, [dict(first), dict(self._DIFF)])
        trace2 = self._trace(path2)
        cowork._role_loop(
            sess2, "seed", path2, context="",
            io_in=io.StringIO("my answer\n"), io_out=io.StringIO(),
            trace=trace2)
        events2 = self._events(path2)
        self.assertFalse(any(e["event"] == "stale_noop" for e in events2))


class ControllerSwitchLoopTest(unittest.TestCase):
    def _path(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "status.json")

    def test_send_failure_without_artifact_progress_returns_switch_outcome(self):
        path = self._path()

        class FailingSession:
            controller = "claude"

            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                return {"ok": False, "result": "error",
                        "error_type": "rate_limit"}

            def close(self):
                self.closed = True

        sess = FailingSession()
        out = io.StringIO()
        rc, outcome, payload = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO("switch\n"),
            io_out=out, role="planner")
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "switch_controller")
        self.assertEqual(payload["role"], "planner")
        self.assertEqual(payload["reason"], "send_failed")
        self.assertEqual(payload["pending"], "seed")
        self.assertTrue(sess.closed)
        self.assertIn("switch-controller", out.getvalue())
        self.assertIn("the claude controller for planner", out.getvalue())

    def test_later_turn_failures_preserve_failed_pending_turn(self):
        cases = []

        def user_answer(path):
            return {
                "writes": [{"status": "needs_input", "result": {"q": "q?"}},
                           {"fail": "rate_limit"}],
                "io": "answer text\nswitch\n",
                "review_fn": None,
                "expect": "answer text",
            }

        def reviewer_handoff(path):
            def review_fn(_status_path, _round_index):
                return {"verdict": "revise", "findings": ["fix risk"]}
            return {
                "writes": [{"status": "ready_for_review", "result": {}},
                           {"fail": "rate_limit"}],
                "io": "switch\n",
                "review_fn": review_fn,
                "expect": "fix risk",
            }

        def repair(path):
            return {
                "writes": [{"status": "ready_for_review", "result": {}},
                           None,
                           {"fail": "rate_limit"}],
                "io": "revise it\nswitch\n",
                "review_fn": None,
                "expect": "byte-identical",
            }

        cases.extend([
            ("user_answer", user_answer),
            ("reviewer_handoff", reviewer_handoff),
            ("repair", repair),
        ])
        for name, factory in cases:
            with self.subTest(name=name):
                path = self._path()
                cfg = factory(path)

                class Session:
                    controller = "claude"

                    def __init__(self):
                        self.sent = []

                    def send(self, text):
                        self.sent.append(text)
                        item = cfg["writes"].pop(0)
                        if isinstance(item, dict) and item.get("fail"):
                            return {"ok": False, "result": "error",
                                    "error_type": item["fail"]}
                        if item is not None:
                            os.makedirs(os.path.dirname(path), exist_ok=True)
                            with open(path, "w") as fh:
                                json.dump(item, fh)

                    def close(self):
                        pass

                rc, outcome, payload = cowork._role_loop(
                    Session(), "seed", path, context="",
                    io_in=io.StringIO(cfg["io"]), io_out=io.StringIO(),
                    role="planner", review_fn=cfg["review_fn"])
                self.assertEqual(rc, 0)
                self.assertEqual(outcome, "switch_controller")
                self.assertIn(cfg["expect"], payload["pending"])

    def test_stuck_gate_switch_returns_switch_outcome(self):
        path = self._path()

        class Session:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                if len(self.sent) == 1:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as fh:
                        json.dump({"status": "ready_for_review",
                                   "result": {}}, fh)
                # Later sends intentionally write nothing: stale/no-op.

            def close(self):
                pass

        rc, outcome, payload = cowork._role_loop(
            Session(), "seed", path, context="",
            io_in=io.StringIO("fix it\nswitch\n"), io_out=io.StringIO(),
            role="planner")
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "switch_controller")
        self.assertEqual(payload["reason"], "stuck")
        self.assertIn("byte-identical", payload["pending"])

    def test_reviewer_failure_switch_retries_same_round_without_lead_bounce(self):
        path = self._path()

        class LeadSession:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    json.dump({"status": "ready_for_review", "result": {}}, fh)

            def close(self):
                pass

        class ReviewFn:
            def __init__(self):
                self.calls = 0
                self.switched = False

            def __call__(self, status_path, round_index,
                         force_full_reread=False):
                self.calls += 1
                if not self.switched:
                    return {}
                self.force_full_reread = force_full_reread
                return {"verdict": "approve"}

            def switch_controller(self, reason="reviewer_failure"):
                self.switched = True
                self.reason = reason
                return True

        lead = LeadSession()
        review_fn = ReviewFn()
        rc, outcome, _ = cowork._role_loop(
            lead, "seed", path, context="",
            io_in=io.StringIO("switch\n\n"), io_out=io.StringIO(),
            role="planner", reviewer_role=cowork.PLANNING_ADVISOR,
            review_fn=review_fn)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "approved")
        self.assertEqual(lead.sent, ["seed"])
        self.assertEqual(review_fn.calls, 3)  # fail, silent retry, switched pass
        self.assertTrue(review_fn.switched)
        self.assertEqual(review_fn.reason, "reviewer_failure")
        self.assertTrue(review_fn.force_full_reread)

    def test_reviewer_preflight_failure_routes_to_switch_gate(self):
        status_path = self._path()
        review_path = status_path + ".review"
        config = cowork.default_config(["planner", cowork.PLANNING_ADVISOR])
        calls = {"runner": [], "checks": 0, "switches": []}

        class LeadSession:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                os.makedirs(os.path.dirname(status_path), exist_ok=True)
                with open(status_path, "w") as fh:
                    json.dump({"status": "ready_for_review", "result": {}}, fh)

            def close(self):
                pass

        def check(role):
            calls["checks"] += 1
            if config[role]["controller"] == "codex":
                return [
                    "Required tool 'codex' not found on PATH.\n"
                    "    Install it with: npm install -g @openai/codex"
                ]
            return None

        def switch(role, reason=None, source=None):
            calls["switches"].append((role, reason, source))
            config[role]["controller"] = "claude"
            return True

        def runner(config, context, selected, artifact_path, review_path, **kw):
            calls["runner"].append((context, kw.get("force_full_reread")))
            return {"verdict": "approve"}

        review_fn = cowork.make_review_fn(
            config, "shared context", ["planner", cowork.PLANNING_ADVISOR],
            review_path, reviewer_runner=runner,
            reviewer_role=cowork.PLANNING_ADVISOR,
            switch_controller_fn=switch,
            switch_note_fn=lambda role: "fresh reviewer switch note",
            reviewer_controller_check_fn=check)
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            LeadSession(), "seed", status_path, context="",
            io_in=io.StringIO("switch\n\n"), io_out=out,
            role="planner", reviewer_role=cowork.PLANNING_ADVISOR,
            review_fn=review_fn)

        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "approved")
        self.assertEqual(
            calls["switches"],
            [(cowork.PLANNING_ADVISOR, "reviewer_failure", "gate")])
        self.assertEqual(len(calls["runner"]), 1)
        self.assertIn("fresh reviewer switch note", calls["runner"][0][0])
        self.assertIn("Required tool 'codex' not found", out.getvalue())


class MakeReviewFnTest(unittest.TestCase):
    def test_none_when_reviewer_not_selected(self):
        self.assertIsNone(cowork.make_review_fn(
            cowork.default_config(["scout"]), "ctx", ["scout"],
            ".cowork/scout-review.X.json"))

    def test_none_without_review_path(self):
        self.assertIsNone(cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], None))

    def test_builds_callable_that_invokes_runner(self):
        seen = {}

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen["intel"] = intel_path
            seen["review"] = review_path
            return {"verdict": "approve"}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner)
        self.assertIsNotNone(fn)
        verdict = fn(".cowork/scout.intel.X.json", 1)
        self.assertEqual(verdict["verdict"], "approve")
        self.assertEqual(seen["intel"], ".cowork/scout.intel.X.json")
        self.assertEqual(seen["review"], ".cowork/scout-review.X.json")

    def test_persistent_reviewer_id_reused_and_persisted(self):
        # First pass creates the reviewer session (id captured + persisted);
        # the second pass resumes it (gets the first pass's id).
        seen = []
        persisted = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen.append(resume_id)
            if resume_id is None and on_session:
                on_session("codex", "rev-thread-1")  # capture a fresh id
            return {"verdict": "revise", "findings": ["x"]}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner,
            on_reviewer_session=lambda c, i: persisted.append((c, i)))
        fn(".cowork/scout.intel.X.json", 1)
        fn(".cowork/scout.intel.X.json", 2)
        self.assertEqual(seen, [None, "rev-thread-1"])      # 2nd pass resumes
        self.assertEqual(persisted, [("codex", "rev-thread-1")])

    def test_seeded_resume_id_used_on_first_pass(self):
        # On a cowork resume the stored reviewer id seeds make_review_fn and is
        # used from the very first pass.
        seen = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen.append(resume_id)
            return {"verdict": "approve"}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner, reviewer_resume_id="stored-rev-id")
        fn(".cowork/scout.intel.X.json", 1)
        self.assertEqual(seen, ["stored-rev-id"])


class ContextRevisionStoreTest(unittest.TestCase):
    """The versioned shared session context + per-role acknowledgment."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_save_context_bumps_revision_only_on_change(self):
        path = self._tmp()
        s = state_store.save_context(path, "goal v1")
        self.assertEqual(state_store.get_context(s), "goal v1")
        self.assertEqual(state_store.get_context_revision(s), 1)
        self.assertTrue(s["context"]["hash"])
        self.assertEqual(s["context"]["source"], "--context")
        # identical text -> no-op, same revision
        s = state_store.save_context(path, "goal v1", prior=s)
        self.assertEqual(state_store.get_context_revision(s), 1)
        # changed text -> revision bump
        s = state_store.save_context(path, "goal v2", prior=s)
        self.assertEqual(state_store.get_context_revision(s), 2)
        self.assertEqual(state_store.get_context(s), "goal v2")

    def test_legacy_plain_string_context_tolerated(self):
        state = {"context": "old plain context"}
        self.assertEqual(state_store.get_context(state), "old plain context")
        self.assertEqual(state_store.get_context_revision(state), 1)

    def test_seen_revision_and_gap(self):
        path = self._tmp()
        s = state_store.save_context(path, "goal")
        self.assertEqual(state_store.get_seen_revision(s, "scout-reviewer"), 0)
        self.assertEqual(state_store.role_context_gap(s, "scout-reviewer"), "goal")
        s = state_store.mark_context_seen(path, "scout-reviewer", 1, prior=s)
        self.assertEqual(state_store.get_seen_revision(s, "scout-reviewer"), 1)
        self.assertIsNone(state_store.role_context_gap(s, "scout-reviewer"))
        # a new revision reopens the gap
        s = state_store.save_context(path, "redirected goal", prior=s)
        self.assertEqual(state_store.role_context_gap(s, "scout-reviewer"),
                         "redirected goal")

    def test_save_role_session_preserves_seen_revision(self):
        path = self._tmp()
        s = state_store.save_context(path, "goal")
        s = state_store.mark_context_seen(path, "scout", 1, prior=s)
        # refreshing the session id must not clobber the acknowledgment
        s = state_store.save_role_session(path, "scout", "claude", "id-2", prior=s)
        self.assertEqual(state_store.get_seen_revision(s, "scout"), 1)
        self.assertEqual(state_store.get_role_session(s, "scout", "claude"), "id-2")


class ContextUpdateBlockTest(unittest.TestCase):
    def test_block_framing(self):
        block = cowork.context_update_block("the new goal")
        self.assertIn("New user context was provided", block)
        self.assertIn("<context>\nthe new goal\n</context>", block)
        self.assertIn("Keep prior session knowledge", block)

    def test_resumed_reviewer_prompt_includes_update_block(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        intel = os.path.join(d, "scout.intel.X.json")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review"}, fh)
        ctx = cowork.assemble_reviewer_resume_context(
            intel, context_update="redirected goal")
        self.assertIn("<context>\nredirected goal\n</context>", ctx)
        self.assertIn("ready_for_review", ctx)   # intel still included
        # without an update there is no block
        self.assertNotIn("<context>",
                         cowork.assemble_reviewer_resume_context(intel))


class ReviewFnContextAckTest(unittest.TestCase):
    """make_review_fn delivers the update block once and acks once."""

    def test_update_delivered_once_then_acked(self):
        seen_updates = []
        acks = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen_updates.append(context_update)
            return {"verdict": "revise", "findings": ["x"]}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "current goal",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner, reviewer_resume_id="rev-1",
            context_update="current goal",
            on_context_ack=lambda: acks.append(True))
        fn(".cowork/scout.intel.X.json", 1)
        fn(".cowork/scout.intel.X.json", 2)
        # block on the first pass only; ack exactly once
        self.assertEqual(seen_updates, ["current goal", None])
        self.assertEqual(acks, [True])

    def test_failed_pass_does_not_ack(self):
        acks = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            return None  # reviewer never produced a verdict

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "goal",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner, reviewer_resume_id="rev-1",
            context_update="goal", on_context_ack=lambda: acks.append(True))
        fn(".cowork/scout.intel.X.json", 1)
        self.assertEqual(acks, [])  # no verdict -> revision not acknowledged


class ReviewerSessionFlowTest(unittest.TestCase):
    """run_flow persists the reviewer session id + original context, and on a
    resume hands the reviewer its stored id and the original context."""

    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_reviewer_id_and_context_persist_across_resume(self):
        spath = self._tmp_session()
        rec = []

        def fake(config, context, selected, io_in=None, io_out=None,
                 resume_id=None, on_session=None, intel_path=None,
                 review_path=None, reviewer_resume_id=None,
                 on_reviewer_session=None, reviewer_context=None,
                 reviewer_context_update=None, on_reviewer_context_ack=None,
                 **kw):
            rec.append({"resume_id": resume_id,
                        "reviewer_resume_id": reviewer_resume_id,
                        "reviewer_context": reviewer_context,
                        "reviewer_context_update": reviewer_context_update,
                        "context": context})
            if on_session and resume_id is None:
                on_session(config["scout"]["controller"], "scout-1")
            if on_reviewer_session and reviewer_resume_id is None:
                on_reviewer_session("codex", "rev-1")
            if on_reviewer_context_ack:
                on_reviewer_context_ack()  # reviewer ran successfully
            return 0

        # Run 1: fresh, with a real goal. Persists scout + reviewer ids + context.
        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--context", "the original goal",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        self.assertIsNone(rec[0]["reviewer_resume_id"])
        self.assertEqual(rec[0]["reviewer_context"], "the original goal")
        saved = state_store.load(spath)
        self.assertEqual(state_store.get_context(saved), "the original goal")
        self.assertEqual(
            state_store.get_role_session(saved, "scout-reviewer", "codex"),
            "rev-1")

        # Run 2: resume (team set so it's non-interactive; no --context => empty).
        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        # plain resume auto-continues: the seed carries the standing discovery
        # note (every cycle) but NO re-injected user goal.
        self.assertIn("Repository discovery", rec[1]["context"])
        self.assertNotIn("original goal", rec[1]["context"])
        self.assertEqual(rec[1]["reviewer_resume_id"], "rev-1")  # stored id reused
        self.assertEqual(rec[1]["reviewer_context"], "the original goal")  # from store
        # both roles already acknowledged revision 1 -> no wake block
        self.assertIsNone(rec[1]["reviewer_context_update"])

        # Run 3: resume WITH a new --context (a redirect). Revision bumps; the
        # resumed reviewer must get the new context as a wake block; the scout
        # gets it naturally as its prompt.
        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--context", "redirected goal",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        # scout gets the redirect wrapped in the wake block (same semantics the
        # reviewer gets: current context, keep prior memory only if compatible)
        self.assertIn("New user context was provided", rec[2]["context"])
        self.assertIn("<context>\nredirected goal\n</context>", rec[2]["context"])
        self.assertEqual(rec[2]["reviewer_context"], "redirected goal")
        self.assertEqual(rec[2]["reviewer_context_update"], "redirected goal")
        saved = state_store.load(spath)
        self.assertEqual(state_store.get_context_revision(saved), 2)
        # both roles acknowledged revision 2 (fake ran the ack + rc==0)
        self.assertEqual(state_store.get_seen_revision(saved, "scout"), 2)
        self.assertEqual(state_store.get_seen_revision(saved, "scout-reviewer"), 2)

    def test_resumed_scout_gets_wake_block_when_unacknowledged(self):
        # A crash before the scout acked revision 1: the next resume must deliver
        # the stored context as an explicit wake block, not "Continue the session.".
        spath = self._tmp_session()
        state = state_store.save_config(
            spath, ["scout", "scout-reviewer"],
            cowork.default_config(["scout", "scout-reviewer"]))
        state = state_store.save_context(spath, "unseen goal", prior=state)
        state = state_store.save_role_session(
            spath, "scout", "claude", "scout-1", prior=state)  # resumable, no ack
        rec = []

        def fake(config, context, selected, **kw):
            rec.append(context)
            return 0

        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        self.assertIn("New user context was provided", rec[0])
        self.assertIn("<context>\nunseen goal\n</context>", rec[0])
        # delivered + successful run -> acknowledged now
        self.assertEqual(state_store.get_seen_revision(
            state_store.load(spath), "scout"), 1)


class RunReviewerOnceTest(unittest.TestCase):
    """The reviewer spawn path: quiet sink (single-voice) + review-file readback."""

    def _paths(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        intel = os.path.join(d, ".cowork", "scout.intel.X.json")
        review = os.path.join(d, ".cowork", "scout-review.X.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        return intel, review

    def test_codex_reviewer_writes_and_is_read_back_via_quiet_sink(self):
        intel, review = self._paths()
        seen = {}

        def factory(controller, io_out):
            seen["controller"] = controller
            seen["io_out"] = io_out

            class FakeRevSession:
                def send(self, text):
                    seen["prompt"] = text
                    with open(review, "w") as fh:
                        json.dump({"verdict": "needs_user",
                                   "user_question": "scope?"}, fh)

                def close(self):
                    seen["closed"] = True
            return FakeRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "the goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory)
        self.assertEqual(verdict["verdict"], "needs_user")
        self.assertEqual(verdict["user_question"], "scope?")
        self.assertEqual(seen["controller"], "codex")     # default controller
        self.assertTrue(seen["closed"])
        # single-voice: the reviewer's io_out is a quiet sink, not a real terminal.
        self.assertFalse(ui.is_tty(seen["io_out"]))
        self.assertIsInstance(seen["io_out"], cowork._QuietSink)
        # reviewer prompt carries the shared context + intel, not the scout brief.
        self.assertIn("the goal", seen["prompt"])
        self.assertNotIn("do NOT produce a plan", seen["prompt"])

    def test_stale_verdict_cleared_before_each_pass(self):
        # An old `approve` on disk must not be read back as THIS pass's verdict
        # when the reviewer fails to write a new one — that would both falsely
        # approve and falsely ack a context revision.
        intel, review = self._paths()
        with open(review, "w") as fh:
            json.dump({"verdict": "approve"}, fh)   # stale prior-round verdict

        def factory(controller, io_out):
            class DeadRevSession:
                def send(self, text):
                    pass                            # never writes the review file

                def close(self):
                    pass
            return DeadRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory)
        self.assertIsNone(verdict)                  # caller treats as safe revise
        self.assertFalse(os.path.exists(review))    # stale file was cleared

    def test_structured_send_failure_returns_controller_failure_verdict(self):
        intel, review = self._paths()
        trace_path = os.path.join(os.path.dirname(intel), "trace.jsonl")
        trace = trace_store.Trace(trace_path, session_uuid="X", run_id="R")

        def factory(controller, io_out):
            class FailingRevSession:
                def send(self, text):
                    return {"ok": False, "result": "error",
                            "error_type": "rate_limit"}

                def close(self):
                    pass
            return FailingRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory, trace=trace)
        self.assertTrue(verdict["controller_failure"])
        self.assertTrue(verdict["malformed"])
        self.assertEqual(
            verdict["controller_failure_result"]["error_type"], "rate_limit")
        with open(trace_path, "r") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(any(e["event"] == "review.run.end"
                            and e["result"] == "controller_failed"
                            and e["error_type"] == "rate_limit"
                            for e in events))


# --------------------------------------------------------------------------- #
# UX layer (cowork_ui). Pure/fallback paths run anywhere; the rich-library paths #
# use a FakeTTY stream + injected seams, and skip when the deps are absent.      #
# --------------------------------------------------------------------------- #


class FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal, so is_tty() returns True."""

    def isatty(self):
        return True


class UiBasicsTest(unittest.TestCase):
    def test_is_tty(self):
        self.assertTrue(ui.is_tty(FakeTTY()))
        self.assertFalse(ui.is_tty(io.StringIO()))

    def test_colorize_gated(self):
        self.assertEqual(ui.colorize("x", ui.RED, False), "x")
        self.assertEqual(ui.colorize("x", ui.RED, True),
                         ui.RED + "x" + ui.RESET)

    def test_label_plain_and_colored(self):
        # Plain forms must match the historical labels exactly.
        self.assertEqual(ui.label("you", False), "you › ")
        self.assertEqual(ui.label("scout", False), "scout › ")
        self.assertEqual(ui.label("you", True), ui.CYAN + "you › " + ui.RESET)
        self.assertEqual(ui.label("scout", True), ui.GREEN + "scout › " + ui.RESET)

    def test_shorten_path(self):
        cwd = "/tmp/work"
        self.assertEqual(
            ui.shorten_path("/tmp/work/.cowork/scout.intel.X.json", cwd=cwd),
            ".cowork/scout.intel.X.json")
        self.assertEqual(
            ui.shorten_path("/var/data/scout.intel.Y.json", cwd=cwd),
            "…/scout.intel.Y.json")

    def test_turn_separator_tty_only(self):
        out = io.StringIO()
        ui.turn_separator(out)              # non-TTY -> nothing
        self.assertEqual(out.getvalue(), "")
        tout = FakeTTY()
        ui.turn_separator(tout)             # TTY -> a dim rule
        self.assertIn("─", tout.getvalue())

    def test_spinner_noop_off_tty(self):
        s = ui.Spinner(io.StringIO())
        s.start()
        self.assertIsNone(s._thread)        # never spawns a thread off a TTY
        s.stop()

    def test_spinner_set_label(self):
        out = io.StringIO()
        s = ui.Spinner(out, "scout working")
        s.start()
        s.set_label("scout using Bash")
        s.stop()
        self.assertEqual(s.label, "scout using Bash")
        self.assertEqual(out.getvalue(), "")  # off-TTY: zero bytes, ever


class PromptUserFallbackTest(unittest.TestCase):
    """The non-TTY readline fallback — unchanged from before, runs without deps."""

    def test_readline_returns_line(self):
        self.assertEqual(
            ui.prompt_user(io.StringIO("hello\n"), io.StringIO()), "hello")

    def test_readline_eof_returns_sentinel(self):
        self.assertIs(ui.prompt_user(io.StringIO(""), io.StringIO()), ui.EOF)

    def test_readline_blank_line_is_empty_not_eof(self):
        # A blank line ("\n") is distinct from EOF: it yields "" (re-prompt).
        self.assertEqual(ui.prompt_user(io.StringIO("\n"), io.StringIO()), "")


@unittest.skipUnless(HAS_UI_DEPS, "prompt_toolkit not installed")
class PromptUserTtyTest(unittest.TestCase):
    """The TTY editor path, driven through an injected prompt_toolkit session.
    (Still needs prompt_toolkit: prompt_user builds real key bindings.)"""

    def _session_factory(self, behaviour):
        class FakeSession:
            def prompt(self, message, **kw):
                self.message = message
                self.kw = kw
                return behaviour()
        self._sess = FakeSession()
        return lambda: self._sess

    def test_returns_stripped_text(self):
        got = ui.prompt_user(FakeTTY(), FakeTTY(), header="your answer",
                             session_factory=self._session_factory(
                                 lambda: "multi\nline\n"))
        self.assertEqual(got, "multi\nline")
        # multiline enabled + the header and inline submit hint are in the prompt.
        self.assertTrue(self._sess.kw.get("multiline"))
        msg = getattr(self._sess.message, "value", self._sess.message)
        self.assertIn("your answer", msg)
        self.assertIn("Enter to send", msg)        # submit key is discoverable

    def test_eof_returns_sentinel(self):
        def boom():
            raise EOFError
        self.assertIs(
            ui.prompt_user(FakeTTY(), FakeTTY(),
                           session_factory=self._session_factory(boom)),
            ui.EOF)

    def test_keyboard_interrupt_propagates(self):
        def boom():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            ui.prompt_user(FakeTTY(), FakeTTY(),
                           session_factory=self._session_factory(boom))


@unittest.skipUnless(HAS_UI_DEPS, "prompt_toolkit not installed")
class KeyBindingsTest(unittest.TestCase):
    """Drive a real PromptSession headlessly: Enter submits, Ctrl+J newlines."""

    def test_enter_submits_ctrl_j_newlines(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            # "ab", Ctrl+J (\n -> newline), "cd", Enter (\r -> submit).
            inp.send_text("ab\ncd\r")
            session = PromptSession(
                input=inp, output=DummyOutput(), multiline=True,
                key_bindings=ui.build_key_bindings())
            result = session.prompt("> ")
        self.assertEqual(result, "ab\ncd")


class RenderMarkdownTest(unittest.TestCase):
    def test_non_tty_writes_raw(self):
        out = io.StringIO()
        ui.render_markdown(out, "# hi\nbody", enabled=False)
        self.assertEqual(out.getvalue(), "# hi\nbody\n")

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_renders_via_rich(self):
        out = FakeTTY()
        ui.render_markdown(out, "**bold**", enabled=True)
        text = out.getvalue()
        self.assertNotIn("**bold**", text)   # markers rendered away
        self.assertIn("bold", text)


class StreamingMarkdownTest(unittest.TestCase):
    class RecTrace:
        def __init__(self):
            self.events = []

        def event(self, name, **fields):
            self.events.append(dict({"event": name}, **fields))

    def test_non_tty_streams_raw_with_label(self):
        out = io.StringIO()
        region = ui.StreamingMarkdown(out, "scout › ")
        region.__enter__()
        region.feed("hello ")
        region.feed("world")
        region.__exit__(None, None, None)
        self.assertEqual(out.getvalue(), "\nscout › hello world\n")

    def test_non_tty_status_is_silent(self):
        # set_status/clear_status interleaved with feed must not change one
        # byte of the non-TTY stream — THE contract the test suite relies on.
        out = io.StringIO()
        region = ui.StreamingMarkdown(out, "scout › ")
        region.__enter__()
        region.feed("hello ")
        region.set_status("scout using Bash…")
        region.feed("world")
        region.clear_status()
        region.__exit__(None, None, None)
        self.assertEqual(out.getvalue(), "\nscout › hello world\n")

    def test_non_tty_traces_render_metadata_without_content(self):
        trace = self.RecTrace()
        out = io.StringIO()
        secret = "secret output text"
        region = ui.StreamingMarkdown(
            out, "scout › ", trace=trace,
            trace_fields={"controller": "claude", "role": "scout"})
        region.__enter__()
        region.feed(secret)
        region.__exit__(None, None, None)

        names = [e["event"] for e in trace.events]
        self.assertEqual(names, ["ui.markdown.start", "ui.markdown.end"])
        start, end = trace.events
        self.assertEqual(start["renderer"], "raw")
        self.assertFalse(start["tty"])
        self.assertEqual(start["controller"], "claude")
        self.assertEqual(start["role"], "scout")
        self.assertEqual(end["chunks"], 1)
        self.assertEqual(end["chars"], len(secret))
        self.assertNotIn(secret, json.dumps(trace.events))

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_renders_buffer(self):
        out = FakeTTY()
        with ui.StreamingMarkdown(out, "scout › ") as region:
            region.feed("**bold**")
        text = out.getvalue()
        self.assertIn("scout › ", text)
        self.assertIn("bold", text)

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_status_row_renders_and_clears(self):
        import unittest.mock as mock
        out = FakeTTY()
        # Test runners often set TERM=dumb; Rich intentionally suppresses live
        # frames there. This test is for the TTY render path, so pin a capable
        # terminal name around the Live console construction.
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            region = ui.StreamingMarkdown(out, "scout › ")
            region.__enter__()
            try:
                region.feed("hello")
                region.set_status("scout using Bash")
                region._live.refresh()  # force a frame (auto-refresh is time-based)
                self.assertIn("using Bash", out.getvalue())
                region.clear_status()
            finally:
                region.__exit__(None, None, None)
        self.assertIsNone(region._status)  # final render carries no status row

    def test_safe_commit_point_paragraph_and_fence(self):
        # commits greedily through every finalized paragraph, leaving the
        # still-growing tail in the live region...
        t = "para one\n\npara two\n\ntail"
        self.assertEqual(ui._safe_commit_point(t, 0), len("para one\n\npara two\n\n"))
        # ...but never inside an open ``` fence (fences may contain blank lines).
        t2 = "intro\n\n```\ncode\n\nmore\n```\n\nafter"
        self.assertEqual(t2[:ui._safe_commit_point(t2, 0)], "intro\n\n```\ncode\n\nmore\n```\n\n")
        # nothing finalized yet -> nothing to commit.
        self.assertIsNone(ui._safe_commit_point("partial line", 0))

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_long_reply_does_not_replay_lines(self):
        # Regression: a multi-paragraph reply streamed in chunks must print each
        # finalized paragraph exactly once. The old whole-buffer Live re-render
        # replayed lines that scrolled past the viewport.
        import unittest.mock as mock
        out = FakeTTY()
        trace = self.RecTrace()
        paras = [f"Paragraph number {i} with a unique marker word zzz{i}." for i in range(8)]
        text = "\n\n".join(paras) + "\n"
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with ui.StreamingMarkdown(out, "scout › ", trace=trace) as region:
                for ch in text:  # feed char-by-char, the worst case for replay
                    region.feed(ch)
        rendered = out.getvalue()
        for i in range(8):
            self.assertEqual(rendered.count(f"zzz{i}"), 1, f"marker zzz{i} replayed")
        self.assertTrue(any(e["event"] == "ui.markdown.commit"
                            for e in trace.events))
        self.assertNotIn("zzz0", json.dumps(trace.events))


class BannerTest(unittest.TestCase):
    def test_non_tty_plain_keeps_substrings(self):
        for text in ("scout needs your input",
                     "scout intel ready for review — x",
                     "scout finished — intel → x"):
            out = io.StringIO()
            ui.banner(out, text, "info")
            self.assertIn(text, out.getvalue())

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_renders_panel(self):
        out = FakeTTY()
        ui.banner(out, "ready for review", "review")
        text = out.getvalue()
        self.assertIn("ready for review", text)
        # Rich Panel draws a box border.
        self.assertTrue(any(ch in text for ch in "─│╭╰╮╯┌└"))


class ConfirmTest(unittest.TestCase):
    def test_injected_ask_fn(self):
        self.assertTrue(ui.confirm("ok?", ask_fn=lambda: True))
        self.assertFalse(ui.confirm("ok?", ask_fn=lambda: False))
        self.assertFalse(ui.confirm("ok?", ask_fn=lambda: None))  # cancel -> False


class SelectTest(unittest.TestCase):
    def test_injected_ask_fn(self):
        choices = [("a", "Option A"), ("b", "Option B")]
        self.assertEqual(ui.select("pick", choices, ask_fn=lambda: "b"), "b")
        # dismissed -> None passes through; callers pick their safe fallback
        self.assertIsNone(ui.select("pick", choices, ask_fn=lambda: None))


class ScoutLoopTtyTest(unittest.TestCase):
    """The review gate uses an explicit 3-way select on a TTY (#8): Approve &
    finish / Ask a question / Request changes. ui.select / ui.prompt_user /
    ui.banner are patched so no real prompt/library is needed."""

    def _intel(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def _session(self, intel_path, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                os.makedirs(os.path.dirname(intel_path), exist_ok=True)
                with open(intel_path, "w") as fh:
                    json.dump({"status": st}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def test_review_select_approve(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "select",
                                  return_value="approve") as sel:
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY())
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])
        sel.assert_called_once()

    def test_review_select_changes_then_approve(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "select",
                                  side_effect=["changes", "approve"]), \
                mock.patch.object(cowork.ui, "prompt_user",
                                  return_value="please tweak X"):
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY())
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "please tweak X"])

    def test_review_select_ask_then_approve(self):
        # "Ask a question" at the normal gate: answered in chat, then approve.
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "select",
                                  side_effect=["ask", "approve"]), \
                mock.patch.object(cowork.ui, "prompt_user",
                                  return_value="why this approach?"):
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY())
        self.assertEqual(rc, 0)
        # The question rode to the role as an ordinary turn (not a revise), and
        # the intel was never reopened.
        self.assertEqual(len(sess.sent), 2)
        self.assertIn("[user question", sess.sent[-1])

    def _always_revise_fn(self, finding="still not aligned"):
        """Revise for exactly REVIEW_ROUND_CAP rounds (hitting the dissent
        gate), then approve."""
        calls = {"n": 0}

        def review_fn(intel_path, round_index):
            calls["n"] += 1
            if calls["n"] <= cowork.REVIEW_ROUND_CAP:
                return {"verdict": "revise", "findings": [finding]}
            return {"verdict": "approve"}
        review_fn.calls = calls
        return review_fn

    def test_dissent_gate_iterate_hands_findings_back(self):
        import unittest.mock as mock
        intel = self._intel()
        cap = cowork.REVIEW_ROUND_CAP
        sess = self._session(intel, [])  # always ready_for_review
        rfn = self._always_revise_fn()
        banners = []
        with mock.patch.object(cowork.ui, "banner",
                               side_effect=lambda _io, text, kind="info",
                               **kw: banners.append((text, kind))), \
                mock.patch.object(cowork.ui, "select",
                                  return_value="iterate") as sel, \
                mock.patch.object(cowork, "_read_review",
                                  return_value=cowork._END):
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY(),
                                    review_fn=rfn)
        self.assertEqual(rc, 0)
        sel.assert_called_once()
        # iterate injected the reviewer's unresolved findings as the next turn
        self.assertIn("[reviewer handoff]", sess.sent[-1])
        self.assertIn("still not aligned", sess.sent[-1])
        # fresh budget after iterate: reviewer ran once more and approved
        self.assertEqual(rfn.calls["n"], cap + 1)
        texts = [t for t, _k in banners]
        # dissent banner used the dissent kind and the cap-reached header
        self.assertTrue(any(k == "dissent" and "review cap reached" in t
                            for t, k in banners))
        # the badge counter climbed to the cap, then visibly reset to 1
        self.assertIn("reviewed: changes requested (round %d/%d)" % (cap, cap),
                      texts)
        self.assertIn("reviewed: approved (round 1/%d)" % cap, texts)

    def test_dissent_gate_tell_blank_falls_back_to_iterate(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, [])
        rfn = self._always_revise_fn()
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "select",
                                  return_value="tell"), \
                mock.patch.object(cowork.ui, "prompt_user",
                                  return_value="") as pu, \
                mock.patch.object(cowork, "_read_review",
                                  return_value=cowork._END):
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY(),
                                    review_fn=rfn)
        self.assertEqual(rc, 0)
        pu.assert_called_once()
        # blank instructions never approve: the reviewer handoff was injected
        self.assertIn("[reviewer handoff]", sess.sent[-1])
        self.assertEqual(rfn.calls["n"], cowork.REVIEW_ROUND_CAP + 1)

    def test_dissent_gate_tell_sends_custom_instructions(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, [])
        rfn = self._always_revise_fn()
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "select", return_value="tell"), \
                mock.patch.object(cowork.ui, "prompt_user",
                                  return_value="focus on the schema"), \
                mock.patch.object(cowork, "_read_review",
                                  return_value=cowork._END):
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY(),
                                    review_fn=rfn)
        self.assertEqual(rc, 0)
        self.assertIn("focus on the schema", sess.sent)

    def test_dissent_gate_approve_finishes(self):
        import unittest.mock as mock
        intel = self._intel()
        cap = cowork.REVIEW_ROUND_CAP
        sess = self._session(intel, [])
        rfn = self._always_revise_fn()
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "select",
                                  return_value="approve"), \
                mock.patch.object(cowork, "_read_review") as rr:
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY(),
                                    review_fn=rfn)
        self.assertEqual(rc, 0)
        # approved straight from the dissent gate: no extra reviewer rounds,
        # no plain approve gate
        self.assertEqual(rfn.calls["n"], cap)
        self.assertEqual(len(sess.sent), cap)  # seed + (cap-1) revise handoffs
        rr.assert_not_called()


class ClaudeSessionTtyTest(unittest.TestCase):
    """On a TTY the claude reply streams into a render region (#5). The region is
    injected so this test needs no real terminal or Rich."""

    class _Stdin:
        def write(self, s):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class _Proc:
        def __init__(self, lines):
            self.stdout = iter(lines)
            self.stdin = ClaudeSessionTtyTest._Stdin()

        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def test_tokens_stream_into_region(self):
        import unittest.mock as mock

        class FakeRegion:
            log = []

            def __init__(self, io_out, label):
                self.io_out = io_out
                self.label = label
                self.buf = []
                self.entered = self.exited = False

            def __enter__(self):
                self.entered = True
                FakeRegion.log.append(self)
                return self

            def feed(self, chunk):
                self.buf.append(chunk)

            def __exit__(self, *exc):
                self.exited = True

        lines = [
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": "**hi**"}}}),
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": " there"}}}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "x", "session_id": "S1"}),
        ]
        got = {}
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=self._Proc(lines)):
            s = bridge.ClaudeSession(
                "roles/scout.md", "implement", True, io_out=io.StringIO(),
                region_factory=FakeRegion,
                on_session_id=lambda i: got.setdefault("id", i))
            s.send("hello")
        region = FakeRegion.log[0]
        self.assertTrue(region.entered and region.exited)
        self.assertEqual("".join(region.buf), "**hi** there")  # streamed in order
        self.assertEqual(region.label, "scout › ")             # plain label
        self.assertEqual(got.get("id"), "S1")

    def test_default_streaming_region_traces_metadata_without_content(self):
        import unittest.mock as mock

        class RecTrace:
            def __init__(self):
                self.events = []

            def event(self, name, **fields):
                self.events.append(dict({"event": name}, **fields))

        secret = "secret streamed reply"
        lines = [
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": secret}}}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "done"}),
        ]
        trace = RecTrace()
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=self._Proc(lines)):
            s = bridge.ClaudeSession(
                "roles/scout.md", "implement", True,
                io_out=io.StringIO(), trace=trace)
            s.send("secret prompt")

        ui_events = [e for e in trace.events
                     if e["event"].startswith("ui.markdown.")]
        self.assertEqual([e["event"] for e in ui_events],
                         ["ui.markdown.start", "ui.markdown.end"])
        self.assertEqual(ui_events[0]["controller"], "claude")
        self.assertEqual(ui_events[0]["role"], "scout")
        self.assertEqual(ui_events[0]["renderer"], "raw")
        self.assertEqual(ui_events[1]["chunks"], 1)
        self.assertEqual(ui_events[1]["chars"], len(secret))
        dumped = json.dumps(trace.events)
        self.assertNotIn(secret, dumped)
        self.assertNotIn("secret prompt", dumped)

    def test_tool_activity_drives_spinner_and_status(self):
        # The loading-state contract: tool calls before the first token retitle
        # the spinner; tool calls mid-stream show a status row in the region;
        # text resuming clears it.
        import unittest.mock as mock

        class RecSpinner:
            insts = []

            def __init__(self, out, label="working"):
                self.labels = [label]
                self.stopped = False
                RecSpinner.insts.append(self)

            def start(self):
                return self

            def set_label(self, text):
                self.labels.append(text)

            def stop(self):
                self.stopped = True

        class FakeRegion:
            log = []

            def __init__(self, io_out, label):
                self.buf = []
                self.status_calls = []
                self.clears = 0
                FakeRegion.log.append(self)

            def __enter__(self):
                return self

            def feed(self, chunk):
                self.buf.append(chunk)

            def set_status(self, text):
                self.status_calls.append(text)

            def clear_status(self):
                self.clears += 1

            def __exit__(self, *exc):
                pass

        def tool_use(name):
            return json.dumps({"type": "stream_event", "event": {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": name}}})

        def token(text):
            return json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": text}}})

        user = json.dumps({"type": "user", "message": {"content": []}})
        lines = [
            tool_use("Bash"),   # pre-token: spinner label flips
            user,               # tool done: spinner back to working
            token("hi"),        # region opens, spinner stops
            tool_use("Grep"),   # mid-stream: status row
            user,               # tool done: status back to working
            token(" there"),    # text resumes: status cleared
            json.dumps({"type": "result", "subtype": "success", "result": ""}),
        ]
        FakeRegion.log.clear()
        RecSpinner.insts.clear()
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=self._Proc(lines)), \
                mock.patch.object(bridge.ui, "Spinner", RecSpinner):
            s = bridge.ClaudeSession(
                "roles/scout.md", "implement", True, io_out=FakeTTY(),
                region_factory=FakeRegion)
            s.send("go")
        spin = RecSpinner.insts[0]
        self.assertEqual(spin.labels, ["scout working", "scout using Bash",
                                       "scout working"])
        self.assertTrue(spin.stopped)
        region = FakeRegion.log[0]
        self.assertEqual("".join(region.buf), "hi there")
        self.assertEqual(region.status_calls,
                         ["scout using Grep…", "scout working…"])
        self.assertEqual(region.clears, 1)  # cleared when text resumed


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
            tid, "What number did I ask you to remember? Reply with just the number.",
            "plan", True))
        self.assertEqual(rc2, 0, err2[:300])
        texts = " ".join(
            bridge.parse_codex_event(o).get("text", "")
            for o in objs2 if bridge.parse_codex_event(o)["kind"] == "message")
        self.assertIn("7", texts)


class PhaseStateTest(unittest.TestCase):
    """Phase persistence + the hand-back payload reader in cowork_state."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_phase_defaults_to_scouting(self):
        self.assertEqual(state_store.get_phase(None), "scouting")
        self.assertEqual(state_store.get_phase({}), "scouting")
        # unknown values are treated as scouting (back-compat / corruption)
        self.assertEqual(state_store.get_phase({"phase": "done"}), "scouting")

    def test_save_phase_roundtrip_preserves_state(self):
        path = self._tmp()
        state = state_store.save_config(
            path, ["scout", "planner"],
            cowork.default_config(["scout", "planner"]))
        state = state_store.save_phase(path, "planning", prior=state)
        loaded = state_store.load(path)
        self.assertEqual(state_store.get_phase(loaded), "planning")
        self.assertEqual(loaded["team"], ["scout", "planner"])  # preserved

    def test_planner_path_helpers(self):
        # No-uuid filenames: the per-session folder already isolates them; the
        # session_uuid arg is kept for call-site stability but unused.
        self.assertEqual(
            state_store.planner_plan_json_path_for(".cowork", "abc"),
            ".cowork/planner.plan.json")
        self.assertEqual(
            state_store.planner_plan_md_path_for(".cowork", "abc"),
            ".cowork/planner.plan.md")
        self.assertEqual(
            state_store.planner_review_path_for(".cowork", "abc"),
            ".cowork/planner-review.json")

    def test_read_handoff(self):
        path = self._tmp()  # reuse tmp dir; file path below
        plan = os.path.join(os.path.dirname(path), "planner.plan.X.json")
        self.assertIsNone(state_store.read_handoff(plan))     # missing
        os.makedirs(os.path.dirname(plan), exist_ok=True)
        with open(plan, "w") as fh:
            fh.write("not json")
        self.assertIsNone(state_store.read_handoff(plan))     # malformed
        with open(plan, "w") as fh:
            json.dump({"status": "needs_input", "handoff": "x"}, fh)
        self.assertIsNone(state_store.read_handoff(plan))     # wrong status
        with open(plan, "w") as fh:
            json.dump({"status": "handoff_back", "handoff": "  "}, fh)
        self.assertIsNone(state_store.read_handoff(plan))     # empty payload
        with open(plan, "w") as fh:
            json.dump({"status": "handoff_back",
                       "handoff": "re-check scope"}, fh)
        self.assertEqual(state_store.read_handoff(plan), "re-check scope")

    def test_invalidate_handoff_back(self):
        path = self._tmp()
        plan = os.path.join(os.path.dirname(path), "planner.plan.X.json")
        os.makedirs(os.path.dirname(plan), exist_ok=True)
        with open(plan, "w") as fh:
            json.dump({"status": "handoff_back", "handoff": "n"}, fh)
        # default from_status leaves a handoff_back untouched
        self.assertFalse(state_store.invalidate_ready_status(plan))
        self.assertTrue(state_store.invalidate_ready_status(
            plan, from_status="handoff_back"))
        with open(plan, "r") as fh:
            self.assertEqual(json.load(fh)["status"], "needs_input")


class PlanningAdvisorRegistrationTest(unittest.TestCase):
    def test_role_renamed_and_registered(self):
        self.assertIn("planning-advisor", cowork.ROLES)
        self.assertNotIn("advisor", cowork.ROLES)   # reserved name renamed
        # paired with the planner: placed right after it
        self.assertEqual(cowork.ROLES.index("planning-advisor"),
                         cowork.ROLES.index("planner") + 1)
        # inherits the old advisor defaults
        self.assertEqual(
            cowork.DEFAULTS["planning-advisor"],
            {"controller": "codex", "model": None, "effort": None,
             "yolo": True, "mode": "implement"})

    def test_role_prompt_files_exist(self):
        self.assertTrue(os.path.exists(cowork.PLANNER_PROMPT_PATH))
        self.assertTrue(os.path.exists(cowork.PLANNING_ADVISOR_PROMPT_PATH))

    def test_handback_contract_wires_planner_and_builder(self):
        self.assertEqual(cowork.HANDBACK_PREPROCESSOR,
                         {"planner": "scout", "builder": "planner"})


class PlannerLoopTest(unittest.TestCase):
    """Drive run_planner/_role_loop with a fake session writing plan statuses."""

    def _paths(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return (os.path.join(d, ".cowork", "planner.plan.X.json"),
                os.path.join(d, ".cowork", "planner.plan.X.md"))

    def _session(self, plan_json, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                entry = statuses.pop(0) if statuses else {"status": "ready_for_review"}
                os.makedirs(os.path.dirname(plan_json), exist_ok=True)
                with open(plan_json, "w") as fh:
                    json.dump(dict({"session": "X", "role": "planner",
                                    "result": {}}, **entry), fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def _run(self, plan_json, plan_md, sess, io_in, reviewer_runner=None,
             handoff_confirm=None, selected=None):
        out = io.StringIO()
        outcomes = []
        config = cowork.default_config(
            selected or ["scout", "planner", "planning-advisor"])
        config["planner"]["controller"] = "codex"
        rc = cowork.run_planner(
            config, "seed", selected or ["scout", "planner", "planning-advisor"],
            io_in=io_in, io_out=out,
            plan_json_path=plan_json, plan_md_path=plan_md,
            review_path=os.path.join(os.path.dirname(plan_json),
                                     "planner-review.X.json"),
            session_factory=lambda *a, **k: sess,
            reviewer_runner=reviewer_runner,
            handoff_confirm=handoff_confirm,
            on_outcome=lambda o, p: outcomes.append((o, p)))
        return rc, out.getvalue(), outcomes

    def test_needs_input_then_ready_then_approve(self):
        plan_json, plan_md = self._paths()
        sess = self._session(plan_json, [{"status": "needs_input"},
                                         {"status": "ready_for_review"}])
        rfn_calls = []

        def runner(config, context, selected, p, review_path, **kw):
            rfn_calls.append(p)
            return {"verdict": "approve"}

        rc, text, outcomes = self._run(
            plan_json, plan_md, sess, io.StringIO("answer\n\n"),
            reviewer_runner=runner)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent[1], "answer")
        self.assertIn("planner needs your input", text)
        self.assertIn("plan ready for review", text)
        self.assertIn("planner finished", text)
        self.assertEqual(rfn_calls, [plan_json])     # advisor saw the plan JSON
        self.assertEqual(outcomes, [("approved", None)])

    def test_advisor_revise_loops_then_user_gate(self):
        plan_json, plan_md = self._paths()
        sess = self._session(plan_json, [{"status": "ready_for_review"},
                                         {"status": "ready_for_review"}])
        verdicts = [{"verdict": "revise", "findings": ["cover migration risk"]},
                    {"verdict": "approve"}]

        def runner(config, context, selected, p, review_path, **kw):
            return verdicts.pop(0)

        rc, text, outcomes = self._run(
            plan_json, plan_md, sess, io.StringIO(""), reviewer_runner=runner)
        self.assertEqual(rc, 0)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertIn("cover migration risk", sess.sent[1])
        # the planner is told about its PLAN, not the scout's intel
        self.assertIn("update your plan", sess.sent[1])
        self.assertNotIn("intel", sess.sent[1])
        # single-voice: advisor findings never reach the user channel
        self.assertNotIn("cover migration risk", text)
        self.assertIn("reviewed: changes requested", text)
        self.assertEqual(outcomes, [("approved", None)])

    def test_handoff_confirmed_returns_payload(self):
        plan_json, plan_md = self._paths()
        sess = self._session(
            plan_json, [{"status": "handoff_back", "handoff": "re-scout auth"}])
        rc, text, outcomes = self._run(
            plan_json, plan_md, sess, io.StringIO(""),
            handoff_confirm=lambda io_in, io_out: True)
        self.assertEqual(rc, 0)
        self.assertIn("hand the work back to the scout", text)
        self.assertIn("re-scout auth", text)        # payload shown at the gate
        self.assertEqual(outcomes, [("handoff", "re-scout auth")])
        self.assertTrue(sess.closed)

    def test_handoff_declined_continues_planning(self):
        plan_json, plan_md = self._paths()
        sess = self._session(
            plan_json, [{"status": "handoff_back", "handoff": "re-scout auth"},
                        {"status": "ready_for_review"}])

        def runner(config, context, selected, p, review_path, **kw):
            return {"verdict": "approve"}

        rc, text, outcomes = self._run(
            plan_json, plan_md, sess, io.StringIO(""),
            reviewer_runner=runner,
            handoff_confirm=lambda io_in, io_out: False)
        self.assertEqual(rc, 0)
        # the declined note was injected as the planner's next turn...
        self.assertIn("DECLINED", sess.sent[1])
        # ...the stale handoff_back was downgraded before that turn ran
        self.assertEqual(outcomes, [("approved", None)])

    def test_handoff_without_payload_degrades_to_needs_input(self):
        plan_json, plan_md = self._paths()
        sess = self._session(plan_json, [{"status": "handoff_back"}])
        gates = []
        rc, text, outcomes = self._run(
            plan_json, plan_md, sess, io.StringIO(""),  # EOF ends after gate
            handoff_confirm=lambda io_in, io_out: gates.append(True) or True)
        self.assertEqual(rc, 0)
        self.assertEqual(gates, [])                     # gate never shown
        self.assertIn("planner needs your input", text)
        self.assertEqual(outcomes, [("ended", None)])

    def test_no_advisor_on_team_skips_review(self):
        plan_json, plan_md = self._paths()
        sess = self._session(plan_json, [{"status": "ready_for_review"}])
        rc, text, outcomes = self._run(
            plan_json, plan_md, sess, io.StringIO(""),
            selected=["scout", "planner"])
        self.assertEqual(rc, 0)
        self.assertNotIn("reviewed", text)
        self.assertEqual(outcomes, [("approved", None)])


class PlannerSeedTest(unittest.TestCase):
    def _intel(self, content):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = os.path.join(d, "scout.intel.X.json")
        with open(path, "w") as fh:
            fh.write(content)
        return path

    def test_planner_seed_carries_intel_and_context(self):
        intel = self._intel('{"status": "ready_for_review", "result": {"k": 1}}')
        seed = cowork.assemble_planner_seed(intel, "the goal")
        self.assertIn("APPROVED", seed)
        # #1: path-first — the intel path + a read-from-disk instruction, NOT
        # the embedded JSON body.
        self.assertIn(intel, seed)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, seed)
        self.assertNotIn('"k": 1', seed)
        self.assertIn("the goal", seed)

    def test_intel_updated_block_carries_intel(self):
        intel = self._intel('{"result": {"new": true}}')
        block = cowork.intel_updated_block(intel)
        self.assertIn("intel changed", block)
        # #1: path-first wake — path + read instruction, not the body.
        self.assertIn(intel, block)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, block)
        self.assertNotIn('"new": true', block)

    def test_handoff_wake_block_carries_payload(self):
        block = cowork.handoff_wake_block("narrow the scope to X")
        self.assertIn("<handoff>\nnarrow the scope to X\n</handoff>", block)
        self.assertIn("ready_for_review", block)

    def test_planner_brief_names_both_artifacts(self):
        brief = cowork.assemble_planner_brief("a.json", "a.md")
        self.assertIn("a.json", brief)
        self.assertIn("a.md", brief)
        self.assertIn("ONLY write targets", brief)

    def test_advisor_context_carries_both_artifacts(self):
        intel = self._intel('{"plan": "J"}')
        md = intel + ".md"
        with open(md, "w") as fh:
            fh.write("# MD PLAN")
        ctx = cowork.assemble_advisor_context("goal", ["planner"], intel, md)
        self.assertIn('"plan": "J"', ctx)
        self.assertIn("# MD PLAN", ctx)
        self.assertIn("goal", ctx)
        resumed = cowork.assemble_advisor_resume_context(
            intel, md, context_update="new goal")
        self.assertIn("<context>\nnew goal\n</context>", resumed)
        self.assertIn("# MD PLAN", resumed)


class SwitchControllerFlowTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore)

    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _saved_planning_session(self):
        spath = self._tmp_session()
        state = state_store.ensure_session(spath, None, "SWITCH-S")
        cfg = cowork.default_config(
            ["scout", "planner", cowork.PLANNING_ADVISOR])
        state = state_store.save_config(
            spath, ["scout", "planner", cowork.PLANNING_ADVISOR],
            cfg, prior=state)
        state = state_store.save_phase(spath, "planning", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "old-claude", prior=state)
        intel = os.path.join(state_store.session_assets_dir("SWITCH-S"),
                             "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "keep"}}, fh)
        return spath

    def test_switch_controller_parser_rejects_invalid_role_and_controller(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._args(["--switch-controller", "unknown=codex"])
            with self.assertRaises(SystemExit):
                self._args(["--switch-controller", "planner=vim"])
            with self.assertRaises(SystemExit):
                self._args(["--switch-controller", "planner"])

    def test_cli_switch_planner_to_codex_continues_planning_with_handoff(self):
        spath = self._saved_planning_session()
        calls = []

        def fake_planner(config, context, selected, on_session=None,
                         on_outcome=None, resume_id=None, **kw):
            calls.append({"config": config, "context": context,
                          "resume_id": resume_id})
            if kw.get("on_first_send_accepted"):
                kw["on_first_send_accepted"]()
            if on_session:
                on_session("codex", "new-codex-thread")
            if on_outcome:
                on_outcome("approved", None)
            return 0

        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--session-file", spath,
                        "--switch-controller", "planner=codex"]),
            io_in=io.StringIO(), io_out=out,
            which=lambda c: "/bin/" + c, run_planner_fn=fake_planner)

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["resume_id"])
        self.assertEqual(calls[0]["config"]["planner"]["controller"], "codex")
        self.assertIn("[controller switch handoff]", calls[0]["context"])
        self.assertIn("fresh codex provider conversation", calls[0]["context"])
        self.assertIn('"finding": "keep"', calls[0]["context"])
        saved = state_store.load(spath)
        self.assertEqual(saved["config"]["planner"]["controller"], "codex")
        self.assertEqual(
            state_store.get_role_session(saved, "planner", "codex"),
            "new-codex-thread")
        self.assertIsNone(state_store.read_pending_switch(saved, "planner"))
        self.assertIn("switched planner controller claude -> codex",
                      out.getvalue())
        saved = state_store.load(spath)
        trace_path = trace_store.trace_path_for(
            state_store.get_session_uuid(saved))
        with open(trace_path, "r") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(any(e["event"] == "controller.switch.request"
                            and e["role"] == "planner"
                            and e["from_controller"] == "claude"
                            and e["to_controller"] == "codex"
                            for e in events))
        self.assertTrue(any(e["event"] == "controller.switch.commit"
                            and e["role"] == "planner"
                            and e["source"] == "cli"
                            for e in events))
        self.assertTrue(any(e["event"] == "role.session_saved"
                            and e["role"] == "planner"
                            and e["controller"] == "codex"
                            and e["session_id"] == "new-codex-thread"
                            for e in events))

    def test_cli_switch_rejects_off_phase_role_without_mutating_state(self):
        spath = self._saved_planning_session()
        before = state_store.load(spath)
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--session-file", spath,
                        "--switch-controller", "scout=codex"]),
            io_in=io.StringIO(), io_out=out, which=lambda c: "/bin/" + c,
            run_planner_fn=lambda *a, **k: 0)
        self.assertEqual(rc, 2)
        self.assertIn("not switchable in the current planning phase",
                      out.getvalue())
        self.assertEqual(state_store.load(spath)["config"], before["config"])
        self.assertEqual(state_store.load(spath)["sessions"], before["sessions"])

    def test_missing_active_controller_reaches_switch_gate_before_launch(self):
        spath = self._tmp_session()
        state = state_store.ensure_session(spath, None, "SWITCH-MISSING")
        cfg = cowork.default_config(["scout"])
        state = state_store.save_config(spath, ["scout"], cfg, prior=state)
        state_store.save_phase(spath, "scouting", prior=state)
        calls = []

        def fake_scout(config, context, selected, on_session=None,
                       on_outcome=None, **kw):
            calls.append({"config": config, "context": context})
            if kw.get("on_first_send_accepted"):
                kw["on_first_send_accepted"]()
            if on_session:
                on_session("codex", "scout-codex-thread")
            if on_outcome:
                on_outcome("ended")
            return 0

        def which(cmd):
            return None if cmd == "claude" else "/bin/" + cmd

        rc = cowork.run_flow(
            self._args(["--session-file", spath]),
            io_in=io.StringIO("switch\n"), io_out=io.StringIO(),
            which=which, run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["config"]["scout"]["controller"], "codex")
        self.assertIn("[controller switch handoff]", calls[0]["context"])
        saved = state_store.load(spath)
        self.assertEqual(saved["config"]["scout"]["controller"], "codex")
        self.assertEqual(
            state_store.get_role_session(saved, "scout", "codex"),
            "scout-codex-thread")

    def test_cli_switch_rejects_incompatible_flags_without_mutation(self):
        spath = self._saved_planning_session()
        before = state_store.load(spath)
        cases = [
            ["--session-file", spath, "--switch-controller", "planner=codex",
             "--no-session"],
            ["--session-file", spath, "--switch-controller", "planner=codex",
             "--new"],
            ["--session-file", spath, "--switch-controller", "planner=codex",
             "--team", "planner"],
            ["--session-file", spath, "--switch-controller", "planner=codex",
             "--config", "planner=codex"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                out = io.StringIO()
                rc = cowork.run_flow(
                    self._args(argv), io_in=io.StringIO(), io_out=out,
                    which=lambda c: "/bin/" + c,
                    run_planner_fn=lambda *a, **k: 0)
                self.assertEqual(rc, 2)
                self.assertIn("cannot be combined", out.getvalue())
                self.assertEqual(state_store.load(spath)["config"],
                                 before["config"])
                self.assertEqual(state_store.load(spath)["sessions"],
                                 before["sessions"])

    def test_cli_switch_rejects_check_and_report(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cowork.main(["--switch-controller", "planner=codex", "--check"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot be combined with --check", err.getvalue())

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cowork.main(["--switch-controller", "planner=codex", "--report"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot be combined with --report", err.getvalue())

    def test_cli_switch_rejects_missing_config_and_role_not_on_team(self):
        spath = self._tmp_session()
        state_store.ensure_session(spath, None, "NO-CONFIG")
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--session-file", spath,
                        "--switch-controller", "planner=codex"]),
            io_in=io.StringIO(), io_out=out, which=lambda c: "/bin/" + c)
        self.assertEqual(rc, 2)
        self.assertIn("requires a saved session", out.getvalue())

        spath = self._tmp_session()
        state = state_store.ensure_session(spath, None, "NO-ROLE")
        cfg = cowork.default_config(["scout", "planner"])
        state = state_store.save_config(
            spath, ["scout", "planner"], cfg, prior=state)
        state_store.save_phase(spath, "planning", prior=state)
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--session-file", spath,
                        "--switch-controller", "planning-advisor=codex"]),
            io_in=io.StringIO(), io_out=out, which=lambda c: "/bin/" + c,
            run_planner_fn=lambda *a, **k: 0)
        self.assertEqual(rc, 2)
        self.assertIn("not on the saved team", out.getvalue())

    def test_cli_switch_rejects_unloadable_session_file_without_mutation(self):
        spath = self._tmp_session()
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        raw = "{not valid json"
        with open(spath, "w") as fh:
            fh.write(raw)

        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--session-file", spath,
                        "--switch-controller", "planner=codex"]),
            io_in=io.StringIO(), io_out=out, which=lambda c: "/bin/" + c)
        self.assertEqual(rc, 2)
        self.assertIn("not a loadable cowork session", out.getvalue())
        with open(spath, "r") as fh:
            self.assertEqual(fh.read(), raw)

    def test_cli_switch_session_discovery_errors(self):
        import tempfile
        cwd = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(cwd, ignore_errors=True))
        prev = os.getcwd()
        os.chdir(cwd)
        try:
            out = io.StringIO()
            rc = cowork.run_flow(
                self._args(["--switch-controller", "planner=codex"]),
                io_in=io.StringIO(), io_out=out, which=lambda c: "/bin/" + c)
            self.assertEqual(rc, 2)
            self.assertIn("no saved sessions", out.getvalue())

            for sid in ("S1", "S2"):
                state_store.ensure_session(
                    state_store.new_session_path(cwd, sid), None, sid)
            out = io.StringIO()
            rc = cowork.run_flow(
                self._args(["--switch-controller", "planner=codex"]),
                io_in=io.StringIO(), io_out=out, which=lambda c: "/bin/" + c)
            self.assertEqual(rc, 2)
            self.assertIn("multiple saved sessions", out.getvalue())
        finally:
            os.chdir(prev)

    def test_cli_switch_target_preflight_failure_leaves_state_unchanged(self):
        spath = self._saved_planning_session()
        before = state_store.load(spath)

        def which(cmd):
            return None if cmd == "codex" else "/bin/" + cmd

        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--session-file", spath,
                        "--switch-controller", "planner=codex"]),
            io_in=io.StringIO(), io_out=out, which=which,
            run_planner_fn=lambda *a, **k: 0)
        self.assertEqual(rc, 1)
        self.assertIn("cannot switch planner to codex", out.getvalue())
        saved = state_store.load(spath)
        self.assertEqual(saved["config"], before["config"])
        self.assertEqual(saved["sessions"], before["sessions"])
        self.assertIsNone(state_store.read_pending_switch(saved, "planner"))

    def test_lead_startup_failure_can_switch_and_relaunch_same_phase(self):
        spath = self._saved_planning_session()
        calls = []

        def fake_planner(config, context, selected, on_session=None,
                         on_outcome=None, **kw):
            calls.append({"controller": config["planner"]["controller"],
                          "context": context})
            if len(calls) == 1:
                return 1  # models startup/probe failure in the active controller
            if kw.get("on_first_send_accepted"):
                kw["on_first_send_accepted"]()
            if on_session:
                on_session("codex", "after-startup-switch")
            if on_outcome:
                on_outcome("approved", None)
            return 0

        rc = cowork.run_flow(
            self._args(["--session-file", spath]),
            io_in=io.StringIO("switch\n"), io_out=io.StringIO(),
            which=lambda c: "/bin/" + c, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        self.assertEqual([c["controller"] for c in calls], ["claude", "codex"])
        self.assertIn("[controller switch handoff]", calls[1]["context"])
        self.assertEqual(
            state_store.get_role_session(
                state_store.load(spath), "planner", "codex"),
            "after-startup-switch")

    def test_cli_switch_resume_picker_targets_selected_session(self):
        import tempfile
        import unittest.mock as mock
        cwd = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(cwd, ignore_errors=True))
        prev = os.getcwd()
        os.chdir(cwd)
        try:
            paths = []
            for sid in ("PICK-1", "PICK-2"):
                spath = state_store.new_session_path(cwd, sid)
                paths.append(spath)
                state = state_store.ensure_session(spath, None, sid)
                cfg = cowork.default_config(
                    ["scout", "planner", cowork.PLANNING_ADVISOR])
                state = state_store.save_config(
                    spath, ["scout", "planner", cowork.PLANNING_ADVISOR],
                    cfg, prior=state)
                state = state_store.save_phase(spath, "planning", prior=state)
                state_store.save_role_session(
                    spath, "planner", "claude", "old-" + sid, prior=state)
                intel = os.path.join(state_store.session_assets_dir(sid),
                                     "scout.intel.json")
                os.makedirs(os.path.dirname(intel), exist_ok=True)
                with open(intel, "w") as fh:
                    json.dump({"status": "ready_for_review",
                               "result": {"session": sid}}, fh)

            calls = []

            def fake_planner(config, context, selected, on_session=None,
                             on_outcome=None, **kw):
                calls.append(context)
                if kw.get("on_first_send_accepted"):
                    kw["on_first_send_accepted"]()
                if on_session:
                    on_session("codex", "picked-thread")
                if on_outcome:
                    on_outcome("approved", None)
                return 0

            with mock.patch.object(cowork.ui, "select", return_value=paths[1]):
                rc = cowork.run_flow(
                    self._args(["--resume", "--switch-controller",
                                "planner=codex"]),
                    io_in=FakeTTY(), io_out=FakeTTY(),
                    which=lambda c: "/bin/" + c,
                    run_planner_fn=fake_planner)
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertIn('"session": "PICK-2"', calls[0])
            self.assertEqual(
                state_store.get_role_session(
                    state_store.load(paths[1]), "planner", "codex"),
                "picked-thread")
            self.assertEqual(
                state_store.get_role_session(
                    state_store.load(paths[0]), "planner", "claude"),
                "old-PICK-1")
        finally:
            os.chdir(prev)


class PhaseChainFlowTest(unittest.TestCase):
    """run_flow phase loop: chaining, hand-back round trip, resume, refusal."""

    def setUp(self):
        # Traces now live under ~/.cowork/sessions; pin the root to a tmp dir
        # so run_flow never writes to the real home dir.
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore)

    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _fakes(self, scout_outcomes, planner_outcomes):
        """Build fake run_scout/run_planner that replay scripted outcomes."""
        calls = {"scout": [], "planner": []}

        def fake_scout(config, context, selected, on_outcome=None,
                       on_session=None, resume_id=None, **kw):
            calls["scout"].append({"context": context, "resume_id": resume_id,
                                   "intel_path": kw.get("intel_path")})
            if on_session and resume_id is None:
                on_session("claude", "scout-%d" % len(calls["scout"]))
            if on_outcome:
                on_outcome(scout_outcomes.pop(0))
            return 0

        def fake_planner(config, context, selected, on_outcome=None,
                         on_session=None, resume_id=None, **kw):
            calls["planner"].append({
                "context": context, "resume_id": resume_id,
                "plan_json_path": kw.get("plan_json_path"),
                "plan_md_path": kw.get("plan_md_path"),
                "review_path": kw.get("review_path"),
                "planning_epoch": kw.get("planning_epoch")})
            if on_session and resume_id is None:
                on_session("claude", "planner-%d" % len(calls["planner"]))
            if on_outcome:
                on_outcome(*planner_outcomes.pop(0))
            return 0

        return calls, fake_scout, fake_planner

    def _write_intel(self, spath):
        saved = state_store.load(spath)
        suid = state_store.get_session_uuid(saved)
        # Produced artifacts now live under the session-assets home, not the
        # project-local .cowork dir (which keeps only session.json).
        intel = os.path.join(state_store.session_assets_dir(suid),
                             "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "F1"}}, fh)
        return intel

    def test_scout_approval_chains_into_planning_same_run(self):
        spath = self._tmp_session()
        calls, fake_scout, fake_planner = self._fakes(
            ["approved"], [("approved", None)])

        # Pre-create the session so the intel file exists when the seed is built.
        state_store.ensure_session(spath, None, "S")
        intel = os.path.join(state_store.session_assets_dir("S"),
                             "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "F1"}}, fh)

        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer,planner,planning-advisor",
                        "--context", "build the thing", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["scout"]), 1)
        self.assertEqual(len(calls["planner"]), 1)
        # fresh planner is seeded with the approved intel + shared context
        seed = calls["planner"][0]["context"]
        self.assertIn("APPROVED", seed)
        # #1: the fresh planner is seeded path-first (read instruction), not the
        # embedded intel body.
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, seed)
        self.assertNotIn('"finding": "F1"', seed)
        self.assertIn("build the thing", seed)
        # planner artifacts carry uuid-free names; the session FOLDER isolates them
        self.assertIn("planner.plan.json", calls["planner"][0]["plan_json_path"])
        self.assertIn("planner.plan.md", calls["planner"][0]["plan_md_path"])
        self.assertIn("planner-review.json", calls["planner"][0]["review_path"])
        self.assertIn("/S/", calls["planner"][0]["plan_json_path"])  # uuid folder
        # plan approval is terminal: the run ended with phase still `planning`
        saved = state_store.load(spath)
        self.assertEqual(state_store.get_phase(saved), "planning")
        # both role sessions persisted
        self.assertEqual(
            state_store.get_role_session(saved, "planner", "claude"),
            "planner-1")

    def test_scout_eof_does_not_chain(self):
        spath = self._tmp_session()
        calls, fake_scout, fake_planner = self._fakes(
            ["ended"], [("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner",
                        "--context", "x", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["planner"]), 0)
        self.assertEqual(state_store.get_phase(state_store.load(spath)),
                         "scouting")

    def test_scout_discovery_note_anchors_to_run_cwd_not_session_dir(self):
        # --session-file lives OUTSIDE the launch folder's repo. The discovery
        # note (and thus the build baseline that shares _plan_repo_set) must
        # anchor to run_cwd = os.getcwd(), never to the session-file/intel dir.
        spath = self._tmp_session()
        calls, fake_scout, _ = self._fakes(["ended"], [])
        rc = cowork.run_flow(
            self._args(["--team", "scout", "--context", "do x",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        seed = calls["scout"][0]["context"]
        self.assertIn("Repository discovery", seed)
        self.assertIn(os.getcwd(), seed)               # base is the launch folder
        self.assertNotIn(os.path.dirname(spath), seed)  # NOT the session-file dir

    def test_plain_resumed_scout_seed_carries_discovery_note(self):
        # A plain auto-continue resume (no new --context) must STILL carry the
        # discovery note so the every-cycle subset responsibility holds.
        spath = self._tmp_session()
        calls, fake_scout, _ = self._fakes(["ended", "ended"], [])
        rc = cowork.run_flow(
            self._args(["--team", "scout", "--context", "x",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        rc = cowork.run_flow(  # resume, no --context
            self._args(["--team", "scout", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        self.assertEqual(calls["scout"][1]["resume_id"], "scout-1")
        self.assertIn("Repository discovery", calls["scout"][1]["context"])

    def test_build_baseline_is_per_repo_over_selected_set(self):
        # Drive a full scout -> planner -> builder chain. The planner writes a
        # plan with a 3-root selected set (one clean, one dirty, one no-HEAD);
        # build_baseline must snapshot EACH root once, flag has_head per root,
        # warn per dirty root, and enumerate every root in the note.
        spath = self._tmp_session()
        state_store.ensure_session(spath, None, "S")
        intel = os.path.join(state_store.session_assets_dir("S"),
                             "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)

        repoA, repoB, repoC = "/x/repoA", "/x/repoB", "/x/repoC"
        scripted = {repoA: ("a" * 40, False), repoB: ("b" * 40, True),
                    repoC: (None, None)}
        base_calls = []
        orig = cowork._git_build_baseline

        def fake_baseline(path):
            base_calls.append(path)
            return scripted.get(path, (None, None))
        cowork._git_build_baseline = fake_baseline
        self.addCleanup(lambda: setattr(cowork, "_git_build_baseline", orig))

        captured = {}

        def fake_scout(config, context, selected, on_outcome=None,
                       on_session=None, resume_id=None, **kw):
            if on_session and resume_id is None:
                on_session("claude", "scout-1")
            if on_outcome:
                on_outcome("approved")
            return 0

        def fake_planner(config, context, selected, on_outcome=None,
                         on_session=None, resume_id=None, **kw):
            with open(kw["plan_json_path"], "w") as fh:
                json.dump({"result": {"repos": [
                    {"path": repoA, "selected": True},
                    {"path": repoB, "selected": True},
                    {"path": repoC, "selected": True}]}}, fh)
            with open(kw["plan_md_path"], "w") as fh:
                fh.write("# PLAN")
            if on_session and resume_id is None:
                on_session("claude", "planner-1")
            if on_outcome:
                on_outcome("approved", None)
            return 0

        def fake_builder(config, context, selected, on_outcome=None,
                         on_session=None, resume_id=None, **kw):
            captured["baseline_note"] = kw.get("baseline_note")
            captured["baseline_repos"] = kw.get("baseline_repos")
            if on_outcome:
                on_outcome("ended", None)
            return 0

        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner,builder",
                        "--context", "x", "--session-file", spath]),
            io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner,
            run_builder_fn=fake_builder)
        self.assertEqual(rc, 0)
        # one snapshot per selected root, in plan order
        self.assertEqual(base_calls, [repoA, repoB, repoC])
        # per-root has_head flag threaded to the reviewer
        self.assertEqual(captured["baseline_repos"], [
            {"path": repoA, "has_head": True},
            {"path": repoB, "has_head": True},
            {"path": repoC, "has_head": False}])
        # note enumerates each root; dirty warning on B; no-commit line for C
        note = captured["baseline_note"]
        self.assertIn(repoA, note)
        self.assertIn("dirty", note)
        self.assertIn("%s (no commit baseline)" % repoC, note)
        # per-repo dirty warning to the user names ONLY the dirty root
        self.assertIn("dirty worktree in %s" % repoB, out.getvalue())
        self.assertNotIn("dirty worktree in %s" % repoA, out.getvalue())

    def test_handoff_round_trip_resumes_scout_then_planner(self):
        spath = self._tmp_session()
        calls, fake_scout, fake_planner = self._fakes(
            ["approved", "approved"],
            [("handoff", "narrow scope to auth"), ("approved", None)])
        state_store.ensure_session(spath, None, "S")
        intel = os.path.join(state_store.session_assets_dir("S"),
                             "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)

        rc = cowork.run_flow(
            self._args(["--team", "scout,planner",
                        "--context", "x", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        # scout ran twice: fresh, then resumed with the handoff wake block
        self.assertEqual(len(calls["scout"]), 2)
        self.assertEqual(calls["scout"][1]["resume_id"], "scout-1")
        self.assertIn("<handoff>\nnarrow scope to auth\n</handoff>",
                      calls["scout"][1]["context"])
        # the discovery responsibility rides BOTH the fresh seed and the
        # hand-back re-run seed, so the every-cycle subset confirmation holds.
        self.assertIn("Repository discovery", calls["scout"][0]["context"])
        self.assertIn("Repository discovery", calls["scout"][1]["context"])
        # planner ran twice: fresh seed, then resumed with the digest block
        self.assertEqual(len(calls["planner"]), 2)
        self.assertEqual(calls["planner"][1]["resume_id"], "planner-1")
        self.assertIn("intel changed", calls["planner"][1]["context"])
        self.assertEqual(state_store.get_phase(state_store.load(spath)),
                         "planning")
        # each scouting -> planning transition is a NEW planning phase: the
        # epoch bumps and is handed to the planner (the ->scout consumed-intel
        # evals re-run for the new phase, even on byte-identical intel)
        self.assertEqual(calls["planner"][0]["planning_epoch"], 1)
        self.assertEqual(calls["planner"][1]["planning_epoch"], 2)
        self.assertEqual(state_store.get_planning_epoch(
            state_store.load(spath)), 2)

    def test_resume_into_planning_skips_scout(self):
        spath = self._tmp_session()
        state = state_store.save_config(
            spath, ["scout", "planner"],
            cowork.default_config(["scout", "planner"]))
        state = state_store.save_phase(spath, "planning", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "planner-9", prior=state)
        calls, fake_scout, fake_planner = self._fakes([], [("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["scout"]), 0)     # no scout run, no prompt
        self.assertEqual(len(calls["planner"]), 1)
        self.assertEqual(calls["planner"][0]["resume_id"], "planner-9")
        self.assertEqual(calls["planner"][0]["context"], "")  # auto-continue

    def test_fresh_planner_without_scout_is_refused(self):
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--team", "planner,planning-advisor",
                        "--context", "x", "--no-session"]),
            io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0,
            run_planner_fn=lambda *a, **k: 0)
        self.assertEqual(rc, 0)
        self.assertIn("scout not selected", out.getvalue())
        self.assertIn("Planning requires approved scout intel", out.getvalue())

    def test_team_without_planner_keeps_terminal_scout(self):
        spath = self._tmp_session()
        calls, fake_scout, fake_planner = self._fakes(
            ["approved"], [("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team", "scout", "--context", "x",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["planner"]), 0)

    def test_handback_delivers_unseen_context_revision_to_scout(self):
        # A run resumes into planning with a NEW --context (revision bumps; the
        # scout has only acked the old one). When the planner hands back, the
        # resumed scout must be woken with the context block AND the handoff —
        # never have the revision marked seen without delivery.
        spath = self._tmp_session()
        state = state_store.save_config(
            spath, ["scout", "planner"],
            cowork.default_config(["scout", "planner"]))
        state = state_store.save_context(spath, "old goal", prior=state)
        state = state_store.save_phase(spath, "planning", prior=state)
        state = state_store.save_role_session(
            spath, "scout", "claude", "scout-1", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "planner-1", prior=state)
        state = state_store.mark_context_seen(spath, "scout", 1, prior=state)
        state = state_store.mark_context_seen(spath, "planner", 1, prior=state)
        calls, fake_scout, fake_planner = self._fakes(
            ["ended"], [("handoff", "re-scope auth")])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner",
                        "--context", "new direction", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        # planner (resumed, unacked rev 2) got the new context as a wake block
        self.assertIn("<context>\nnew direction\n</context>",
                      calls["planner"][0]["context"])
        # scout got BOTH the unseen revision and the handoff payload
        scout_ctx = calls["scout"][0]["context"]
        self.assertIn("New user context was provided", scout_ctx)
        self.assertIn("<context>\nnew direction\n</context>", scout_ctx)
        self.assertIn("<handoff>\nre-scope auth\n</handoff>", scout_ctx)
        # and the delivery is what justifies the ack
        self.assertEqual(state_store.get_seen_revision(
            state_store.load(spath), "scout"), 2)

    def test_resume_into_planning_without_planner_id_seeds_from_intel(self):
        # Killed between save_phase("planning") and the planner id save: the
        # next run must start a FRESH planner from the approved intel, not from
        # a bare context.
        spath = self._tmp_session()
        state = state_store.ensure_session(spath, None, "S")
        state = state_store.save_config(
            spath, ["scout", "planner"],
            cowork.default_config(["scout", "planner"]), prior=state)
        state = state_store.save_phase(spath, "planning", prior=state)
        intel = os.path.join(state_store.session_assets_dir("S"),
                             "scout.intel.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "F1"}}, fh)
        calls, fake_scout, fake_planner = self._fakes([], [("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["scout"]), 0)
        self.assertIsNone(calls["planner"][0]["resume_id"])  # fresh session
        seed = calls["planner"][0]["context"]
        self.assertIn("APPROVED", seed)
        # #1: path-first seed (read instruction), not the embedded intel body.
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, seed)
        self.assertNotIn('"finding": "F1"', seed)

    def test_phase_change_traced(self):
        spath = self._tmp_session()
        calls, fake_scout, fake_planner = self._fakes(
            ["approved"], [("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner", "--context", "x",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout, run_planner_fn=fake_planner)
        self.assertEqual(rc, 0)
        saved = state_store.load(spath)
        tpath = trace_store.trace_path_for(state_store.get_session_uuid(saved))
        with open(tpath, "r") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(any(e["event"] == "phase.change"
                            and e["from"] == "scouting"
                            and e["to"] == "planning" for e in events))


# --------------------------------------------------------------------------- #
# Peer evaluations: scratch/aggregate state helpers, the private eval prompt,   #
# output muting, the role-side and reviewer-side eval turns, and the            #
# observational invariant (verdict handling unchanged by eval).                 #
# --------------------------------------------------------------------------- #


class _EvalEnvMixin:
    """Shared fixtures: a temp .cowork dir + a temp COWORK_SESSIONS_ROOT so
    tests never touch the real home dir."""

    def _tmpdir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def _cowork_dir(self):
        d = os.path.join(self._tmpdir(), ".cowork")
        os.makedirs(d, exist_ok=True)
        return d

    def _scores_root(self):
        root = self._tmpdir()
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore)
        return root

    def _trace(self, cowork_dir):
        return trace_store.Trace(os.path.join(cowork_dir, "trace.X.jsonl"),
                                 session_uuid="X", run_id="R")

    def _trace_events(self, cowork_dir):
        path = os.path.join(cowork_dir, "trace.X.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _scores(self, session_uuid):
        path = state_store.scores_path_for(session_uuid)
        if not os.path.exists(path):
            return None
        with open(path, "r") as fh:
            return json.load(fh)


class EvalStateStoreTest(_EvalEnvMixin, unittest.TestCase):
    def test_eval_scratch_path_shape(self):
        # `role` stays in the name (distinguishes the two evaluators); the
        # session_uuid is dropped — the per-session folder carries it.
        self.assertEqual(
            state_store.eval_scratch_path_for("/tmp/.cowork", "scout", "S1"),
            "/tmp/.cowork/eval.scout.json")

    def test_scores_path_honors_env_root(self):
        root = self._scores_root()
        self.assertEqual(state_store.scores_path_for("S1"),
                         os.path.join(root, "S1", "scores.json"))

    def test_scores_path_defaults_to_home(self):
        old = os.environ.pop("COWORK_SESSIONS_ROOT", None)
        if old is not None:
            self.addCleanup(
                lambda: os.environ.__setitem__("COWORK_SESSIONS_ROOT", old))
        path = state_store.scores_path_for("S1")
        self.assertEqual(path, os.path.join(
            os.path.expanduser(os.path.join("~", ".cowork", "sessions")),
            "S1", "scores.json"))

    def test_session_assets_dir_honors_env_root(self):
        root = self._scores_root()
        self.assertEqual(state_store.session_assets_dir("S1"),
                         os.path.join(root, "S1"))
        # scores live inside the assets dir.
        self.assertEqual(
            os.path.dirname(state_store.scores_path_for("S1")),
            state_store.session_assets_dir("S1"))

    def test_session_assets_dir_defaults_to_home(self):
        old = os.environ.pop("COWORK_SESSIONS_ROOT", None)
        if old is not None:
            self.addCleanup(
                lambda: os.environ.__setitem__("COWORK_SESSIONS_ROOT", old))
        self.assertEqual(
            state_store.session_assets_dir("S1"),
            os.path.join(
                os.path.expanduser(os.path.join("~", ".cowork", "sessions")),
                "S1"))

    def test_read_eval_missing_and_malformed(self):
        d = self._cowork_dir()
        path = os.path.join(d, "eval.scout.X.json")
        self.assertEqual(state_store.read_eval(None), [])
        self.assertEqual(state_store.read_eval(path), [])      # missing
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertEqual(state_store.read_eval(path), [])      # non-JSON
        with open(path, "w") as fh:
            json.dump(["wrong shape"], fh)
        self.assertEqual(state_store.read_eval(path), [])      # not a dict
        with open(path, "w") as fh:
            json.dump({"evaluations": "nope"}, fh)
        self.assertEqual(state_store.read_eval(path), [])      # not a list

    def test_read_eval_normalizes_and_clamps(self):
        d = self._cowork_dir()
        path = os.path.join(d, "eval.scout.X.json")
        with open(path, "w") as fh:
            json.dump({"evaluations": [
                {"evaluatee": "scout-reviewer",
                 "criteria": [
                     {"name": "accuracy", "score": 7, "feedback": "high"},
                     {"name": "helpfulness", "score": 0},
                     {"name": "noise", "score": "4", "feedback": 12},
                     {"name": "", "score": 3},          # nameless: dropped
                     {"name": "bad", "score": "x"},     # unscorable: dropped
                     "not a dict",
                 ],
                 "enhancement_suggestions": "tighten findings"},
                {"evaluatee": "ghost", "criteria": []},  # no criteria: dropped
                {"evaluatee": "ghost2"},                 # no criteria: dropped
                "not a dict",
            ]}, fh)
        entries = state_store.read_eval(path)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["evaluatee"], "scout-reviewer")
        self.assertEqual(entry["enhancement_suggestions"], "tighten findings")
        crits = {c["name"]: c for c in entry["criteria"]}
        self.assertEqual(set(crits), {"accuracy", "helpfulness", "noise"})
        self.assertEqual(crits["accuracy"]["score"], 5)    # clamped down
        self.assertEqual(crits["helpfulness"]["score"], 1)  # clamped up
        self.assertEqual(crits["noise"]["score"], 4)        # coerced
        self.assertEqual(crits["noise"]["feedback"], "12")  # stringified

    def test_append_score_entries_fresh_then_append(self):
        self._scores_root()
        path = state_store.scores_path_for("S1")
        entry = {"evaluatee": "scout", "criteria": [], "evaluator": "x"}
        self.assertTrue(state_store.append_score_entries(path, "S1", [entry]))
        self.assertTrue(state_store.append_score_entries(path, "S1", [entry]))
        data = self._scores("S1")
        self.assertEqual(data["session"], "S1")
        self.assertEqual(len(data["evaluations"]), 2)

    def test_append_score_entries_resets_malformed_existing(self):
        self._scores_root()
        path = state_store.scores_path_for("S1")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertTrue(state_store.append_score_entries(
            path, "S1", [{"evaluatee": "scout"}]))
        data = self._scores("S1")
        self.assertEqual(data["session"], "S1")
        self.assertEqual(len(data["evaluations"]), 1)

    def test_append_score_entries_unwritable_returns_false(self):
        d = self._tmpdir()
        blocker = os.path.join(d, "blocker")
        with open(blocker, "w") as fh:
            fh.write("a file, not a dir")
        path = os.path.join(blocker, "S1", "scores.json")
        self.assertFalse(state_store.append_score_entries(
            path, "S1", [{"evaluatee": "scout"}]))

    def test_has_eval_entry_matches_and_tolerates(self):
        self._scores_root()
        path = state_store.scores_path_for("S1")
        # missing / malformed aggregate reads as "not yet"
        self.assertFalse(state_store.has_eval_entry(
            None, "planner", "scout", "consumed-intel"))
        self.assertFalse(state_store.has_eval_entry(
            path, "planner", "scout", "consumed-intel"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertFalse(state_store.has_eval_entry(
            path, "planner", "scout", "consumed-intel"))
        state_store.append_score_entries(path, "S1", [
            {"evaluator": "planner", "evaluatee": "scout",
             "context": "consumed-intel", "planning_epoch": 1},
            {"evaluator": "planner", "evaluatee": "planning-advisor",
             "context": "review-round"},
        ])
        self.assertTrue(state_store.has_eval_entry(
            path, "planner", "scout", "consumed-intel"))
        # all three fields must match
        self.assertFalse(state_store.has_eval_entry(
            path, "planning-advisor", "scout", "consumed-intel"))
        self.assertFalse(state_store.has_eval_entry(
            path, "planner", "scout", "review-round"))
        # the planning epoch scopes the match to one planning phase
        self.assertTrue(state_store.has_eval_entry(
            path, "planner", "scout", "consumed-intel", planning_epoch=1))
        self.assertFalse(state_store.has_eval_entry(
            path, "planner", "scout", "consumed-intel", planning_epoch=2))

    def test_planning_epoch_persisted_and_bumped(self):
        d = self._tmpdir()
        spath = os.path.join(d, ".cowork", "session.json")
        self.assertEqual(state_store.get_planning_epoch(None), 0)
        self.assertEqual(state_store.get_planning_epoch({}), 0)
        self.assertEqual(state_store.get_planning_epoch(
            {"planning_epoch": "junk"}), 0)
        state = state_store.bump_planning_epoch(spath)
        self.assertEqual(state_store.get_planning_epoch(state), 1)
        state = state_store.bump_planning_epoch(spath, prior=state)
        self.assertEqual(state_store.get_planning_epoch(state), 2)
        # persisted: a fresh load sees the bumped epoch
        self.assertEqual(state_store.get_planning_epoch(
            state_store.load(spath)), 2)

    def test_append_score_entries_empty_is_noop(self):
        self._scores_root()
        path = state_store.scores_path_for("S1")
        self.assertFalse(state_store.append_score_entries(path, "S1", []))
        self.assertFalse(os.path.exists(path))


class EvalPromptTest(unittest.TestCase):
    def test_criteria_matrix_covers_the_pairs(self):
        self.assertEqual(set(cowork.EVAL_CRITERIA), {
            ("scout", "scout-reviewer"), ("scout-reviewer", "scout"),
            ("planner", "planning-advisor"), ("planning-advisor", "planner"),
            ("planner", "scout"), ("planning-advisor", "scout"),
            ("builder", "build-reviewer"), ("build-reviewer", "builder"),
            ("builder", "planner"), ("build-reviewer", "planner"),
        })
        for criteria in cowork.EVAL_CRITERIA.values():
            self.assertTrue(criteria)

    def test_prompt_carries_scratch_path_criteria_and_rules(self):
        specs = [{"evaluatee": "scout-reviewer",
                  "criteria": ["accuracy of findings"],
                  "artifact_block": "VERDICT-JSON-HERE"}]
        prompt = cowork.assemble_eval_prompt(
            "scout", "/tmp/.cowork/eval.scout.S.json", specs)
        self.assertIn("[private evaluation turn]", prompt)
        self.assertIn("/tmp/.cowork/eval.scout.S.json", prompt)
        self.assertIn("Evaluatee: scout-reviewer", prompt)
        self.assertIn("accuracy of findings", prompt)
        self.assertIn("VERDICT-JSON-HERE", prompt)
        self.assertIn("enhancement_suggestions", prompt)
        self.assertIn("never mention this evaluation to the user", prompt)
        self.assertIn("never read any other role's evaluation file", prompt)

    def test_prompt_bundles_multiple_specs(self):
        specs = [
            {"evaluatee": "planning-advisor", "criteria": ["signal-to-noise"],
             "artifact_block": "VERDICT"},
            {"evaluatee": "scout", "criteria": ["goal alignment of intel"],
             "artifact_block": "INTEL-JSON"},
        ]
        prompt = cowork.assemble_eval_prompt("planner", "/x/eval.json", specs)
        self.assertIn("Evaluatee: planning-advisor", prompt)
        self.assertIn("Evaluatee: scout", prompt)
        self.assertIn("INTEL-JSON", prompt)


class MutedSessionTest(unittest.TestCase):
    class _Session:
        def __init__(self, out):
            self.io_out = out

        def send(self, text):
            self.io_out.write("LEAK:" + text)

    def test_send_is_muted_and_io_out_restored(self):
        out = io.StringIO()
        sess = self._Session(out)
        with cowork._muted_session(sess):
            sess.send("secret eval prompt")
        self.assertEqual(out.getvalue(), "")          # nothing leaked
        self.assertIs(sess.io_out, out)               # restored
        sess.send("normal turn")                      # later turns render
        self.assertIn("normal turn", out.getvalue())

    def test_io_out_restored_on_exception(self):
        out = io.StringIO()
        sess = self._Session(out)
        with self.assertRaises(RuntimeError):
            with cowork._muted_session(sess):
                raise RuntimeError("boom")
        self.assertIs(sess.io_out, out)


class EvaluateFnTest(_EvalEnvMixin, unittest.TestCase):
    """The role-side eval closure built by _make_evaluate_fn."""

    def _session(self, scratch_writer=None):
        class FakeSession:
            def __init__(self):
                self.io_out = io.StringIO()
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                # The bridge streams the reply to io_out at send time — the
                # canary the leak test asserts never reaches the real out.
                self.io_out.write("EVAL-REPLY-CANARY " + text)
                if scratch_writer:
                    scratch_writer(text)
        return FakeSession()

    def _scratch_writer(self, path, evaluatees=("scout-reviewer",)):
        def write(_text):
            with open(path, "w") as fh:
                json.dump({"evaluations": [
                    {"evaluatee": e,
                     "criteria": [{"name": "c1", "score": 4,
                                   "feedback": "solid"}],
                     "enhancement_suggestions": "more depth"}
                    for e in evaluatees]}, fh)
        return write

    def test_none_without_paths(self):
        self.assertIsNone(cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", None, "/s", "S"))
        self.assertIsNone(cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", "/p", None, "S"))
        self.assertIsNone(cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", "/p", "/s", None))

    def test_eval_turn_is_muted_and_aggregated_with_stamps(self):
        self._scores_root()
        d = self._cowork_dir()
        scratch = state_store.eval_scratch_path_for(d, "scout", "S")
        sess = self._session(self._scratch_writer(scratch))
        real_out = sess.io_out
        fn = cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", scratch,
            state_store.scores_path_for("S"), "S")
        verdict = {"verdict": "approve", "findings": ["minor note"]}
        fn(sess, verdict, 1)
        # LEAK TEST: no eval prompt or reply text reached the session's real
        # io_out, and io_out is restored for later normal turns.
        self.assertEqual(real_out.getvalue(), "")
        self.assertIs(sess.io_out, real_out)
        # the verdict JSON is embedded even on approve (evidence invariant)
        self.assertIn('"verdict": "approve"', sess.sent[0])
        self.assertIn("minor note", sess.sent[0])
        # privacy: the aggregate scores path never appears in the prompt
        self.assertNotIn(state_store.scores_path_for("S"), sess.sent[0])
        self.assertNotIn("scores.json", sess.sent[0])
        data = self._scores("S")
        self.assertEqual(len(data["evaluations"]), 1)
        entry = data["evaluations"][0]
        self.assertEqual(entry["evaluator"], "scout")
        self.assertEqual(entry["evaluatee"], "scout-reviewer")
        self.assertEqual(entry["phase"], "scouting")
        self.assertEqual(entry["round"], 1)
        self.assertEqual(entry["context"], "review-round")
        self.assertIn("T", entry["timestamp"])
        self.assertEqual(entry["criteria"][0]["score"], 4)

    def test_stale_scratch_cleared_before_send(self):
        # STALE-SCRATCH REGRESSION: a valid scratch from a prior round exists,
        # the current eval turn writes nothing -> cleared before the send,
        # nothing appended, traced eval.written found=false.
        self._scores_root()
        d = self._cowork_dir()
        scratch = state_store.eval_scratch_path_for(d, "scout", "S")
        self._scratch_writer(scratch)("prior round")   # stale but valid
        sess = self._session(scratch_writer=None)      # writes nothing
        trace = self._trace(d)
        fn = cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", scratch,
            state_store.scores_path_for("S"), "S", trace=trace)
        fn(sess, {"verdict": "revise"}, 2)
        self.assertFalse(os.path.exists(scratch))      # cleared, not re-read
        self.assertIsNone(self._scores("S"))           # nothing appended
        events = self._trace_events(d)
        self.assertTrue(any(e["event"] == "eval.scratch.cleared"
                            for e in events))
        self.assertTrue(any(e["event"] == "eval.written"
                            and e["found"] is False for e in events))
        self.assertFalse(any(e["event"] == "eval.aggregated" for e in events))

    def test_malformed_scratch_traced_and_dropped(self):
        self._scores_root()
        d = self._cowork_dir()
        scratch = state_store.eval_scratch_path_for(d, "scout", "S")

        def bad_writer(_text):
            with open(scratch, "w") as fh:
                fh.write("not json")
        sess = self._session(bad_writer)
        trace = self._trace(d)
        fn = cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", scratch,
            state_store.scores_path_for("S"), "S", trace=trace)
        fn(sess, {"verdict": "approve"}, 1)
        self.assertIsNone(self._scores("S"))
        events = self._trace_events(d)
        self.assertTrue(any(e["event"] == "eval.aggregated"
                            and e["result"] == "malformed" for e in events))

    def test_planner_first_turn_bundles_scout_eval_once(self):
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "F-INTEL"}}, fh)
        scratch = state_store.eval_scratch_path_for(d, "planner", "S")
        writers = {"evaluatees": ("planning-advisor", "scout")}

        def writer(text):
            self._scratch_writer(scratch, writers["evaluatees"])(text)
        sess = self._session(writer)
        fn = cowork._make_evaluate_fn(
            "planner", "planning-advisor", "planning", scratch,
            state_store.scores_path_for("S"), "S", intel_path=intel)
        fn(sess, {"verdict": "revise"}, 1)
        # first turn: bundled prompt names both evaluatees; the consumed intel
        # rides path-first (#2 — path + read instruction, not the body), while
        # the small verdict JSON stays inline.
        self.assertIn("Evaluatee: planning-advisor", sess.sent[0])
        self.assertIn("Evaluatee: scout", sess.sent[0])
        self.assertNotIn("F-INTEL", sess.sent[0])
        self.assertIn(intel, sess.sent[0])
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, sess.sent[0])
        writers["evaluatees"] = ("planning-advisor",)
        fn(sess, {"verdict": "approve"}, 2)
        fn(sess, {"verdict": "approve"}, 1)   # round reset: still no re-bundle
        self.assertNotIn("Evaluatee: scout", sess.sent[1])
        self.assertNotIn("Evaluatee: scout", sess.sent[2])
        data = self._scores("S")
        self.assertEqual(len(data["evaluations"]), 4)  # 2 + 1 + 1
        scout_entries = [e for e in data["evaluations"]
                         if e["evaluatee"] == "scout"]
        self.assertEqual(len(scout_entries), 1)        # exactly once per phase
        self.assertEqual(scout_entries[0]["context"], "consumed-intel")
        self.assertEqual(scout_entries[0]["evaluator"], "planner")

    def test_scout_bundle_not_repeated_across_resume(self):
        # The once-per-phase flag must survive a resume/restart: a FRESH
        # closure (new run_planner invocation) must not re-emit the
        # consumed-intel eval already recorded in the aggregate.
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        with open(intel, "w") as fh:
            json.dump({"result": {"finding": "F-INTEL"}}, fh)
        scratch = state_store.eval_scratch_path_for(d, "planner", "S")

        def make_fn():
            return cowork._make_evaluate_fn(
                "planner", "planning-advisor", "planning", scratch,
                state_store.scores_path_for("S"), "S", intel_path=intel,
                planning_epoch=1)

        sess = self._session(self._scratch_writer(
            scratch, ("planning-advisor", "scout")))
        make_fn()(sess, {"verdict": "revise"}, 1)   # run 1: bundles
        self.assertIn("Evaluatee: scout", sess.sent[0])
        sess2 = self._session(self._scratch_writer(
            scratch, ("planning-advisor",)))
        make_fn()(sess2, {"verdict": "approve"}, 1)  # run 2 (resume): no bundle
        self.assertNotIn("Evaluatee: scout", sess2.sent[0])
        data = self._scores("S")
        scout_entries = [e for e in data["evaluations"]
                         if e["evaluatee"] == "scout"]
        self.assertEqual(len(scout_entries), 1)

    def test_no_scout_bundle_when_intel_missing(self):
        self._scores_root()
        d = self._cowork_dir()
        scratch = state_store.eval_scratch_path_for(d, "planner", "S")
        sess = self._session(self._scratch_writer(
            scratch, ("planning-advisor",)))
        fn = cowork._make_evaluate_fn(
            "planner", "planning-advisor", "planning", scratch,
            state_store.scores_path_for("S"), "S",
            intel_path=os.path.join(d, "missing.json"))
        fn(sess, {"verdict": "approve"}, 1)
        self.assertNotIn("Evaluatee: scout", sess.sent[0])

    def test_late_intel_waits_for_a_fresh_round_one(self):
        # The ->scout bundle rides only round-1 eval turns: intel that
        # appears mid-cycle is not bundled on round 2, only on the next
        # round-1 turn (after the user re-engages and rounds reset).
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        scratch = state_store.eval_scratch_path_for(d, "planner", "S")
        sess = self._session(self._scratch_writer(
            scratch, ("planning-advisor",)))
        fn = cowork._make_evaluate_fn(
            "planner", "planning-advisor", "planning", scratch,
            state_store.scores_path_for("S"), "S", intel_path=intel)
        fn(sess, {"verdict": "revise"}, 1)          # intel missing: no bundle
        with open(intel, "w") as fh:
            json.dump({"result": {"finding": "F-LATE"}}, fh)
        fn(sess, {"verdict": "revise"}, 2)          # round 2: still no bundle
        self.assertNotIn("Evaluatee: scout", sess.sent[0])
        self.assertNotIn("Evaluatee: scout", sess.sent[1])
        sess3 = self._session(self._scratch_writer(
            scratch, ("planning-advisor", "scout")))
        fn(sess3, {"verdict": "approve"}, 1)        # fresh round 1: bundles
        self.assertIn("Evaluatee: scout", sess3.sent[0])

    def test_scout_bundle_repeats_for_new_planning_phase(self):
        # A hand-back round trip starts a NEW planning phase (the epoch
        # bumps), so the scout is evaluated again — even when the re-approved
        # intel is byte-identical (once per phase, not once per session or
        # per intel content).
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        with open(intel, "w") as fh:
            json.dump({"result": {"finding": "F-SAME"}}, fh)
        scratch = state_store.eval_scratch_path_for(d, "planner", "S")

        def make_fn(epoch):
            return cowork._make_evaluate_fn(
                "planner", "planning-advisor", "planning", scratch,
                state_store.scores_path_for("S"), "S", intel_path=intel,
                planning_epoch=epoch)

        sess = self._session(self._scratch_writer(
            scratch, ("planning-advisor", "scout")))
        make_fn(1)(sess, {"verdict": "approve"}, 1)   # phase 1: bundles
        sess2 = self._session(self._scratch_writer(
            scratch, ("planning-advisor", "scout")))
        make_fn(2)(sess2, {"verdict": "approve"}, 1)  # phase 2: bundles again
        self.assertIn("Evaluatee: scout", sess2.sent[0])
        data = self._scores("S")
        scout_entries = [e for e in data["evaluations"]
                         if e["evaluatee"] == "scout"]
        self.assertEqual(len(scout_entries), 2)
        self.assertEqual(sorted(e["planning_epoch"] for e in scout_entries),
                         [1, 2])


class RoleLoopEvalTest(_EvalEnvMixin, unittest.TestCase):
    """evaluate_fn fires per review round, for every verdict kind, and never
    affects the flow (observational invariant)."""

    def _intel(self):
        return os.path.join(self._cowork_dir(), "scout.intel.X.json")

    def _session(self, intel_path, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                with open(intel_path, "w") as fh:
                    json.dump({"status": st}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def _review_fn(self, verdicts):
        def review_fn(intel_path, round_index):
            return verdicts.pop(0) if verdicts else {"verdict": "approve"}
        return review_fn

    def _recording_eval(self):
        calls = []

        def evaluate_fn(session, verdict, round_index):
            calls.append(((verdict or {}).get("verdict"), round_index))
        evaluate_fn.calls = calls
        return evaluate_fn

    def test_eval_fires_for_revise_and_approve_rounds(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        efn = self._recording_eval()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(),
            review_fn=self._review_fn([
                {"verdict": "revise", "findings": ["gap"]},
                {"verdict": "approve"}]),
            evaluate_fn=efn)
        self.assertEqual(rc, 0)
        self.assertEqual(efn.calls, [("revise", 1), ("approve", 2)])

    def test_eval_fires_for_needs_user_round(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "needs_input"])
        efn = self._recording_eval()
        cowork._scout_loop(
            sess, "seed", intel, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(),
            review_fn=self._review_fn([
                {"verdict": "needs_user", "user_question": "scope?"}]),
            evaluate_fn=efn)
        self.assertEqual(efn.calls, [("needs_user", 1)])

    def test_eval_fires_on_every_round_up_to_the_cap(self):
        intel = self._intel()
        cap = cowork.REVIEW_ROUND_CAP
        sess = self._session(intel, ["ready_for_review"] * (cap + 1))
        efn = self._recording_eval()
        cowork._scout_loop(
            sess, "seed", intel, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(),
            review_fn=self._review_fn(
                [{"verdict": "revise", "findings": ["no"]}] * (cap + 1)),
            evaluate_fn=efn)
        # round-cap dissent rounds are evaluated too: one eval per round
        self.assertEqual(efn.calls,
                         [("revise", i) for i in range(1, cap + 1)])

    def test_eval_failure_is_traced_and_flow_unchanged(self):
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.X.json")
        statuses = ["ready_for_review", "ready_for_review"]
        verdicts = [{"verdict": "revise", "findings": ["fix the cited path"]},
                    {"verdict": "approve"}]

        def run(evaluate_fn, trace=None):
            sess = self._session(intel, list(statuses))
            out = io.StringIO()
            rc = cowork._scout_loop(
                sess, "seed", intel, context="", io_in=io.StringIO(""),
                io_out=out, review_fn=self._review_fn(list(verdicts)),
                trace=trace, evaluate_fn=evaluate_fn)
            return rc, sess, out.getvalue()

        def boom(session, verdict, round_index):
            raise RuntimeError("eval exploded")

        trace = self._trace(d)
        rc_off, sess_off, out_off = run(None)
        rc_on, sess_on, out_on = run(boom, trace=trace)
        # observational invariant: identical rc, handoffs, and user output
        self.assertEqual(rc_on, rc_off)
        self.assertEqual(sess_on.sent, sess_off.sent)
        self.assertEqual(out_on, out_off)
        self.assertTrue(any(e["event"] == "eval.error"
                            for e in self._trace_events(d)))

    def test_eval_output_never_reaches_the_user_channel(self):
        # End-to-end mute check at the loop level: the eval turn rides the
        # real session (whose send streams to io_out) and the user output must
        # contain no eval prompt or reply text — only the role's own replies.
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.X.json")
        scratch = state_store.eval_scratch_path_for(d, "scout", "X")
        out = io.StringIO()

        class FakeSession:
            def __init__(self):
                self.io_out = out
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                self.io_out.write("[reply to] " + text[:40] + "\n")
                if "[private evaluation turn]" in text:
                    with open(scratch, "w") as fh:
                        json.dump({"evaluations": [
                            {"evaluatee": "scout-reviewer",
                             "criteria": [{"name": "c", "score": 5,
                                           "feedback": "SECRET-FEEDBACK"}],
                             "enhancement_suggestions": "SECRET-SUGGESTION"},
                        ]}, fh)
                else:
                    with open(intel, "w") as fh:
                        json.dump({"status": "ready_for_review"}, fh)

            def close(self):
                pass

        sess = FakeSession()
        efn = cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", scratch,
            state_store.scores_path_for("X"), "X")
        rc = cowork._scout_loop(
            sess, "seed", intel, context="", io_in=io.StringIO(""),
            io_out=out, review_fn=self._review_fn([{"verdict": "approve"}]),
            evaluate_fn=efn)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("[reply to] seed", text)              # normal turn shown
        self.assertNotIn("[private evaluation turn]", text)  # prompt muted
        self.assertNotIn("SECRET-FEEDBACK", text)
        self.assertNotIn("SECRET-SUGGESTION", text)
        # io_out restored after the eval turn (the swap did not stick)
        self.assertIs(sess.io_out, out)
        data = self._scores("X")
        self.assertEqual(len(data["evaluations"]), 1)


class RunReviewerOnceEvalTest(_EvalEnvMixin, unittest.TestCase):
    """The reviewer-side eval turn rides the session run_reviewer_once already
    holds: send -> read -> eval-send -> close."""

    def _paths(self):
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.X.json")
        review = os.path.join(d, "scout-review.X.json")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        return d, intel, review

    def _factory(self, review_path, scratch_path=None, scratch_payload=None):
        record = {"sends": [], "closed_after": None}

        def factory(controller, io_out):
            class FakeRevSession:
                def send(self, text):
                    record["sends"].append(text)
                    if len(record["sends"]) == 1:
                        with open(review_path, "w") as fh:
                            json.dump({"verdict": "approve"}, fh)
                    elif scratch_path and scratch_payload is not None:
                        with open(scratch_path, "w") as fh:
                            json.dump(scratch_payload, fh)

                def close(self):
                    record["closed_after"] = len(record["sends"])
            return FakeRevSession()
        return factory, record

    def test_eval_sent_on_open_session_after_verdict_before_close(self):
        d, intel, review = self._paths()
        scratch = state_store.eval_scratch_path_for(d, "scout-reviewer", "X")
        payload = {"evaluations": [{"evaluatee": "scout",
                                    "criteria": [{"name": "c", "score": 3,
                                                  "feedback": "ok"}]}]}
        factory, record = self._factory(review, scratch, payload)
        specs = [{"evaluatee": "scout",
                  "criteria": ["intel quality/completeness"],
                  "artifact_block": "see your review file",
                  "context": "review-round", "phase": "scouting", "round": 1}]
        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory, eval_scratch_path=scratch,
            eval_specs=specs)
        self.assertEqual(verdict["verdict"], "approve")   # verdict unaffected
        self.assertEqual(len(record["sends"]), 2)         # review + eval turn
        self.assertIn("[private evaluation turn]", record["sends"][1])
        self.assertIn("Evaluatee: scout", record["sends"][1])
        self.assertEqual(record["closed_after"], 2)       # closed after eval
        self.assertEqual(len(state_store.read_eval(scratch)), 1)

    def test_stale_reviewer_scratch_cleared_before_eval_send(self):
        # Reviewer-side stale regression: a valid prior-round scratch exists,
        # this eval turn writes nothing -> the file was cleared before the
        # send, so a later read finds no entry (never the prior round's).
        d, intel, review = self._paths()
        scratch = state_store.eval_scratch_path_for(d, "scout-reviewer", "X")
        with open(scratch, "w") as fh:
            json.dump({"evaluations": [
                {"evaluatee": "scout",
                 "criteria": [{"name": "c", "score": 5}]}]}, fh)
        factory, record = self._factory(review)  # eval turn writes nothing
        specs = [{"evaluatee": "scout", "criteria": ["c"],
                  "artifact_block": "x", "context": "review-round",
                  "phase": "scouting", "round": 2}]
        cfg = cowork.default_config(["scout", "scout-reviewer"])
        cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory, eval_scratch_path=scratch,
            eval_specs=specs)
        self.assertEqual(len(record["sends"]), 2)
        self.assertFalse(os.path.exists(scratch))
        self.assertEqual(state_store.read_eval(scratch), [])

    def test_no_eval_specs_keeps_single_send(self):
        d, intel, review = self._paths()
        factory, record = self._factory(review)
        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory)
        self.assertEqual(verdict["verdict"], "approve")
        self.assertEqual(len(record["sends"]), 1)

    def test_eval_send_failure_does_not_break_the_verdict(self):
        d, intel, review = self._paths()
        scratch = state_store.eval_scratch_path_for(d, "scout-reviewer", "X")
        record = {"sends": 0}

        def factory(controller, io_out):
            class FlakyRevSession:
                def send(self, text):
                    record["sends"] += 1
                    if record["sends"] == 1:
                        with open(review, "w") as fh:
                            json.dump({"verdict": "approve"}, fh)
                    else:
                        raise RuntimeError("eval send died")

                def close(self):
                    pass
            return FlakyRevSession()

        specs = [{"evaluatee": "scout", "criteria": ["c"],
                  "artifact_block": "x", "context": "review-round",
                  "phase": "scouting", "round": 1}]
        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory, eval_scratch_path=scratch,
            eval_specs=specs)
        self.assertEqual(verdict["verdict"], "approve")
        self.assertEqual(record["sends"], 2)


class MakeReviewFnEvalTest(_EvalEnvMixin, unittest.TestCase):
    """review_fn computes the reviewer's eval specs, hands them to the runner,
    and aggregates the reviewer's scratch after the pass."""

    def _runner(self, scratch_entries=None):
        seen = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None,
                   eval_scratch_path=None, eval_specs=None):
            seen.append({"eval_scratch_path": eval_scratch_path,
                         "eval_specs": eval_specs})
            if eval_scratch_path and scratch_entries is not None:
                with open(eval_scratch_path, "w") as fh:
                    json.dump({"evaluations": [
                        {"evaluatee": e,
                         "criteria": [{"name": "c", "score": 5,
                                       "feedback": "good"}],
                         "enhancement_suggestions": "s"}
                        for e in scratch_entries(eval_specs)]}, fh)
            return {"verdict": "approve"}
        runner.seen = seen
        return runner

    def test_back_compat_runner_without_eval_params(self):
        # No eval wiring -> the runner is called WITHOUT eval kwargs, so
        # strict-signature runners (the existing tests' fakes) keep working.
        def strict_runner(config, context, selected, intel_path, review_path,
                          resume_id=None, on_session=None,
                          context_update=None):
            return {"verdict": "approve"}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=strict_runner)
        self.assertEqual(fn(".cowork/scout.intel.X.json", 1)["verdict"],
                         "approve")

    def test_specs_passed_every_round_and_scratch_aggregated(self):
        self._scores_root()
        d = self._cowork_dir()
        scratch = state_store.eval_scratch_path_for(d, "scout-reviewer", "S")
        runner = self._runner(
            scratch_entries=lambda specs: [s["evaluatee"] for s in specs])
        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], os.path.join(d, "scout-review.S.json"),
            reviewer_runner=runner, phase="scouting",
            eval_scratch_path=scratch,
            scores_path=state_store.scores_path_for("S"), session_uuid="S")
        fn(os.path.join(d, "scout.intel.S.json"), 1)
        fn(os.path.join(d, "scout.intel.S.json"), 2)
        for call in runner.seen:
            self.assertEqual(call["eval_scratch_path"], scratch)
            self.assertEqual(
                [s["evaluatee"] for s in call["eval_specs"]], ["scout"])
        data = self._scores("S")
        self.assertEqual(len(data["evaluations"]), 2)
        for i, entry in enumerate(data["evaluations"]):
            self.assertEqual(entry["evaluator"], "scout-reviewer")
            self.assertEqual(entry["evaluatee"], "scout")
            self.assertEqual(entry["phase"], "scouting")
            self.assertEqual(entry["round"], i + 1)
            self.assertEqual(entry["context"], "review-round")
        # Q3a: the scratch remains in .cowork after aggregation (overwritten
        # per round; staleness is handled by clearing BEFORE each eval send)
        self.assertTrue(os.path.exists(scratch))

    def test_planning_first_round_bundles_scout_spec_once(self):
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        with open(intel, "w") as fh:
            json.dump({"result": {"finding": "F-INTEL"}}, fh)
        scratch = state_store.eval_scratch_path_for(d, "planning-advisor", "S")
        runner = self._runner(
            scratch_entries=lambda specs: [s["evaluatee"] for s in specs])
        fn = cowork.make_review_fn(
            cowork.default_config(["planner", "planning-advisor"]), "ctx",
            ["planner", "planning-advisor"],
            os.path.join(d, "planner-review.S.json"),
            reviewer_runner=runner, reviewer_role="planning-advisor",
            phase="planning", eval_scratch_path=scratch,
            scores_path=state_store.scores_path_for("S"), session_uuid="S",
            intel_path=intel)
        fn(os.path.join(d, "planner.plan.S.json"), 1)
        fn(os.path.join(d, "planner.plan.S.json"), 2)
        fn(os.path.join(d, "planner.plan.S.json"), 1)   # reset: no re-bundle
        first = [s["evaluatee"] for s in runner.seen[0]["eval_specs"]]
        self.assertEqual(first, ["planner", "scout"])
        # the ->scout spec sends the intel path-first (#2 — path + read
        # instruction, not the body)
        scout_spec = runner.seen[0]["eval_specs"][1]
        self.assertNotIn("F-INTEL", scout_spec["artifact_block"])
        self.assertIn(intel, scout_spec["artifact_block"])
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION,
                      scout_spec["artifact_block"])
        self.assertEqual(scout_spec["context"], "consumed-intel")
        for later in runner.seen[1:]:
            self.assertEqual(
                [s["evaluatee"] for s in later["eval_specs"]], ["planner"])
        data = self._scores("S")
        self.assertEqual(len(data["evaluations"]), 4)   # 2 + 1 + 1
        scout_entries = [e for e in data["evaluations"]
                         if e["evaluatee"] == "scout"]
        self.assertEqual(len(scout_entries), 1)
        self.assertEqual(scout_entries[0]["evaluator"], "planning-advisor")
        self.assertEqual(scout_entries[0]["context"], "consumed-intel")

    def test_scout_spec_not_repeated_across_resume(self):
        # A FRESH review_fn (new make_review_fn construction, e.g. a cowork
        # resume within the planning phase) must not re-emit the ->scout
        # consumed-intel eval already recorded in the aggregate.
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        with open(intel, "w") as fh:
            json.dump({"result": {"finding": "F-INTEL"}}, fh)
        scratch = state_store.eval_scratch_path_for(d, "planning-advisor", "S")

        def make_fn(runner, epoch=1):
            return cowork.make_review_fn(
                cowork.default_config(["planner", "planning-advisor"]), "ctx",
                ["planner", "planning-advisor"],
                os.path.join(d, "planner-review.S.json"),
                reviewer_runner=runner, reviewer_role="planning-advisor",
                phase="planning", eval_scratch_path=scratch,
                scores_path=state_store.scores_path_for("S"),
                session_uuid="S", intel_path=intel, planning_epoch=epoch)

        runner1 = self._runner(
            scratch_entries=lambda specs: [s["evaluatee"] for s in specs])
        make_fn(runner1)(os.path.join(d, "planner.plan.S.json"), 1)
        self.assertEqual([s["evaluatee"] for s in runner1.seen[0]["eval_specs"]],
                         ["planner", "scout"])
        runner2 = self._runner(
            scratch_entries=lambda specs: [s["evaluatee"] for s in specs])
        make_fn(runner2)(os.path.join(d, "planner.plan.S.json"), 1)  # resume
        self.assertEqual([s["evaluatee"] for s in runner2.seen[0]["eval_specs"]],
                         ["planner"])
        data = self._scores("S")
        scout_entries = [e for e in data["evaluations"]
                         if e["evaluatee"] == "scout"]
        self.assertEqual(len(scout_entries), 1)
        # ...but a NEW planning phase (hand-back round trip bumps the epoch)
        # is evaluated again, even with byte-identical re-approved intel.
        runner3 = self._runner(
            scratch_entries=lambda specs: [s["evaluatee"] for s in specs])
        make_fn(runner3, epoch=2)(os.path.join(d, "planner.plan.S.json"), 1)
        self.assertEqual([s["evaluatee"] for s in runner3.seen[0]["eval_specs"]],
                         ["planner", "scout"])
        self.assertEqual(runner3.seen[0]["eval_specs"][1]["planning_epoch"], 2)


class EvalEndToEndTest(_EvalEnvMixin, unittest.TestCase):
    """Fake-session end-to-end: both sides of a pairing land in scores.json
    with correct stamps, and no eval content reaches the user output."""

    def test_scout_phase_two_evals_per_round(self):
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        review = os.path.join(d, "scout-review.S.json")
        scout_scratch = state_store.eval_scratch_path_for(d, "scout", "S")
        rev_scratch = state_store.eval_scratch_path_for(
            d, "scout-reviewer", "S")
        scores_path = state_store.scores_path_for("S")
        config = cowork.default_config(["scout", "scout-reviewer"])
        config["scout"]["controller"] = "codex"
        prompts = []

        def factory(controller, resume_thread_id=None, on_thread_id=None):
            class FakeScout:
                def __init__(self):
                    self.io_out = io.StringIO()

                def send(self, text):
                    prompts.append(text)
                    # The role spec itself mentions the eval-turn marker, and
                    # the codex first prompt embeds the role spec — detect an
                    # eval turn by its scratch path, which only the eval
                    # prompt carries.
                    if scout_scratch in text:
                        with open(scout_scratch, "w") as fh:
                            json.dump({"evaluations": [
                                {"evaluatee": "scout-reviewer",
                                 "criteria": [{"name": "accuracy of findings",
                                               "score": 4,
                                               "feedback": "on point"}],
                                 "enhancement_suggestions": "cite more"}]}, fh)
                    else:
                        with open(intel, "w") as fh:
                            json.dump({"status": "ready_for_review"}, fh)

                def close(self):
                    pass
            return FakeScout()

        def reviewer_runner(config, context, selected, intel_path, review_path,
                            resume_id=None, on_session=None,
                            context_update=None, eval_scratch_path=None,
                            eval_specs=None):
            prompts.extend(s.get("artifact_block", "") for s in eval_specs or [])
            with open(review_path, "w") as fh:
                json.dump({"verdict": "approve"}, fh)
            if eval_scratch_path and eval_specs:
                with open(eval_scratch_path, "w") as fh:
                    json.dump({"evaluations": [
                        {"evaluatee": s["evaluatee"],
                         "criteria": [{"name": s["criteria"][0], "score": 5,
                                       "feedback": "complete"}],
                         "enhancement_suggestions": "none"}
                        for s in eval_specs]}, fh)
            return {"verdict": "approve"}

        out = io.StringIO()
        rc = cowork.run_scout(
            config, "the goal", ["scout", "scout-reviewer"],
            io_in=io.StringIO(""), io_out=out, intel_path=intel,
            session_factory=factory, review_path=review,
            reviewer_runner=reviewer_runner,
            eval_scratch_path=scout_scratch,
            reviewer_eval_scratch_path=rev_scratch,
            scores_path=scores_path, session_uuid="S")
        self.assertEqual(rc, 0)
        data = self._scores("S")
        self.assertEqual(len(data["evaluations"]), 2)   # one per side
        by_evaluator = {e["evaluator"]: e for e in data["evaluations"]}
        self.assertEqual(set(by_evaluator),
                         {"scout", "scout-reviewer"})
        self.assertEqual(by_evaluator["scout"]["evaluatee"], "scout-reviewer")
        self.assertEqual(by_evaluator["scout-reviewer"]["evaluatee"], "scout")
        for entry in data["evaluations"]:
            self.assertEqual(entry["phase"], "scouting")
            self.assertEqual(entry["round"], 1)
            self.assertEqual(entry["context"], "review-round")
            self.assertIn("timestamp", entry)
        # privacy: no eval text in the user output; no scores path in prompts
        text = out.getvalue()
        self.assertNotIn("on point", text)
        self.assertNotIn("[private evaluation turn]", text)
        for prompt in prompts:
            self.assertNotIn(scores_path, prompt)

    def test_planning_phase_first_round_produces_four_entries(self):
        self._scores_root()
        d = self._cowork_dir()
        intel = os.path.join(d, "scout.intel.S.json")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"finding": "F-INTEL"}}, fh)
        plan_json = os.path.join(d, "planner.plan.S.json")
        plan_md = os.path.join(d, "planner.plan.S.md")
        review = os.path.join(d, "planner-review.S.json")
        planner_scratch = state_store.eval_scratch_path_for(d, "planner", "S")
        advisor_scratch = state_store.eval_scratch_path_for(
            d, "planning-advisor", "S")
        scores_path = state_store.scores_path_for("S")
        config = cowork.default_config(["planner", "planning-advisor"])
        config["planner"]["controller"] = "codex"

        def factory(controller, resume_thread_id=None, on_thread_id=None):
            class FakePlanner:
                def __init__(self):
                    self.io_out = io.StringIO()

                def send(self, text):
                    # Detect an eval turn by its scratch path (the codex first
                    # prompt embeds the role spec, which mentions the marker).
                    if planner_scratch in text:
                        evaluatees = [r for r in ("planning-advisor", "scout")
                                      if "Evaluatee: %s" % r in text]
                        with open(planner_scratch, "w") as fh:
                            json.dump({"evaluations": [
                                {"evaluatee": e,
                                 "criteria": [{"name": "c", "score": 4,
                                               "feedback": "fb"}],
                                 "enhancement_suggestions": "es"}
                                for e in evaluatees]}, fh)
                    else:
                        with open(plan_json, "w") as fh:
                            json.dump({"status": "ready_for_review"}, fh)

                def close(self):
                    pass
            return FakePlanner()

        def reviewer_runner(config, context, selected, artifact_path,
                            review_path, resume_id=None, on_session=None,
                            context_update=None, eval_scratch_path=None,
                            eval_specs=None):
            with open(review_path, "w") as fh:
                json.dump({"verdict": "approve"}, fh)
            if eval_scratch_path and eval_specs:
                with open(eval_scratch_path, "w") as fh:
                    json.dump({"evaluations": [
                        {"evaluatee": s["evaluatee"],
                         "criteria": [{"name": s["criteria"][0], "score": 5,
                                       "feedback": "fb"}],
                         "enhancement_suggestions": "es"}
                        for s in eval_specs]}, fh)
            return {"verdict": "approve"}

        outcomes = []
        rc = cowork.run_planner(
            config, "seed", ["planner", "planning-advisor"],
            io_in=io.StringIO(""), io_out=io.StringIO(),
            plan_json_path=plan_json, plan_md_path=plan_md,
            session_factory=factory, review_path=review,
            reviewer_runner=reviewer_runner,
            eval_scratch_path=planner_scratch,
            reviewer_eval_scratch_path=advisor_scratch,
            scores_path=scores_path, session_uuid="S", intel_path=intel,
            on_outcome=lambda o, p: outcomes.append(o))
        self.assertEqual(rc, 0)
        self.assertEqual(outcomes, ["approved"])
        data = self._scores("S")
        # first planning round: 2 counterpart evals + 2 ->scout evals
        self.assertEqual(len(data["evaluations"]), 4)
        pairs = sorted((e["evaluator"], e["evaluatee"], e["context"])
                       for e in data["evaluations"])
        self.assertEqual(pairs, [
            ("planner", "planning-advisor", "review-round"),
            ("planner", "scout", "consumed-intel"),
            ("planning-advisor", "planner", "review-round"),
            ("planning-advisor", "scout", "consumed-intel"),
        ])
        for entry in data["evaluations"]:
            self.assertEqual(entry["phase"], "planning")
            self.assertEqual(entry["round"], 1)


# --------------------------------------------------------------------------- #
# Building phase: registration, state helpers, builder seed/brief/handoff       #
# helpers, the build-reviewer trio, the builder loop, and the run_flow          #
# planning -> building -> (hand-back) planning chaining + resume cascades.       #
# --------------------------------------------------------------------------- #


class BuildReviewerRegistrationTest(unittest.TestCase):
    def test_roles_reordered_and_build_reviewer_paired(self):
        self.assertEqual(
            cowork.ROLES,
            ["scout", "scout-reviewer", "planner", "planning-advisor",
             "builder", "build-reviewer"])
        # the build-reviewer is paired with the builder: right after it
        self.assertEqual(cowork.ROLES.index("build-reviewer"),
                         cowork.ROLES.index("builder") + 1)
        self.assertNotIn("revisor", cowork.DEFAULTS)
        self.assertEqual(
            cowork.DEFAULTS["build-reviewer"],
            {"controller": "codex", "model": None, "effort": None,
             "yolo": True, "mode": "implement"})
        self.assertEqual(
            cowork.DEFAULTS["builder"],
            {"controller": "claude", "model": None, "effort": None,
             "yolo": True, "mode": "implement"})

    def test_role_prompt_files_exist(self):
        self.assertTrue(os.path.exists(cowork.BUILDER_PROMPT_PATH))
        self.assertTrue(os.path.exists(cowork.BUILD_REVIEWER_PROMPT_PATH))

    def test_reviewer_evaluatee_map_has_build_reviewer(self):
        self.assertEqual(cowork._REVIEWER_EVALUATEE["build-reviewer"], "builder")


class BuildingStateTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_phases_extended_with_building(self):
        self.assertIn("building", state_store.PHASES)
        path = self._tmp()
        state = state_store.save_config(
            path, ["builder"], cowork.default_config(["builder"]))
        state = state_store.save_phase(path, "building", prior=state)
        self.assertEqual(state_store.get_phase(state_store.load(path)),
                         "building")

    def test_build_path_helpers(self):
        # No-uuid filenames: the per-session folder isolates them; the
        # session_uuid arg is kept for call-site stability but unused.
        self.assertEqual(
            state_store.build_status_path_for(".cowork", "abc"),
            ".cowork/builder.status.json")
        self.assertEqual(
            state_store.build_review_path_for(".cowork", "abc"),
            ".cowork/builder-review.json")

    def test_building_epoch_persisted_and_bumped(self):
        path = self._tmp()
        self.assertEqual(state_store.get_building_epoch(None), 0)
        self.assertEqual(state_store.get_building_epoch({}), 0)
        state = state_store.bump_building_epoch(path)
        self.assertEqual(state_store.get_building_epoch(state), 1)
        state = state_store.bump_building_epoch(path, prior=state)
        self.assertEqual(state_store.get_building_epoch(state), 2)

    def test_has_eval_entry_scopes_building_epoch(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = os.path.join(d, "scores.json")
        with open(path, "w") as fh:
            json.dump({"evaluations": [
                {"evaluator": "builder", "evaluatee": "planner",
                 "context": "consumed-plan", "building_epoch": 1}]}, fh)
        self.assertTrue(state_store.has_eval_entry(
            path, "builder", "planner", "consumed-plan", building_epoch=1))
        self.assertFalse(state_store.has_eval_entry(
            path, "builder", "planner", "consumed-plan", building_epoch=2))
        # a planning_epoch-only query ignores the building_epoch entry
        self.assertFalse(state_store.has_eval_entry(
            path, "builder", "planner", "consumed-plan", planning_epoch=1))


class BuilderSeedTest(unittest.TestCase):
    def _plan(self, json_body="{}", md_body="# PLAN"):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        pj = os.path.join(d, "planner.plan.X.json")
        pm = os.path.join(d, "planner.plan.X.md")
        with open(pj, "w") as fh:
            fh.write(json_body)
        with open(pm, "w") as fh:
            fh.write(md_body)
        return pj, pm

    def test_builder_brief_names_status_only_not_a_restriction(self):
        brief = cowork.assemble_builder_brief(".cowork/builder.status.X.json")
        self.assertIn(".cowork/builder.status.X.json", brief)
        self.assertIn("NOT", brief)               # not a write restriction
        self.assertIn("commit", brief)            # no git commit

    def test_builder_seed_carries_plan_and_context(self):
        pj, pm = self._plan('{"result": {"k": 1}}', "# THE PLAN")
        seed = cowork.assemble_builder_seed(pj, pm, "the goal")
        self.assertIn("APPROVED", seed)
        # #1: path-first — both plan paths + a read-from-disk instruction, NOT
        # the embedded bodies.
        self.assertIn(pj, seed)
        self.assertIn(pm, seed)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, seed)
        self.assertNotIn('"k": 1', seed)
        self.assertNotIn("# THE PLAN", seed)
        self.assertIn("the goal", seed)

    def test_plan_updated_block_carries_plan(self):
        pj, pm = self._plan('{"result": {"new": true}}', "# UPDATED")
        block = cowork.plan_updated_block(pj, pm)
        self.assertIn("plan changed", block)
        # #1: path-first wake — both paths + read instruction, not the bodies.
        self.assertIn(pj, block)
        self.assertIn(pm, block)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, block)
        self.assertNotIn('"new": true', block)
        self.assertNotIn("# UPDATED", block)

    def test_plan_handback_wake_block_carries_payload(self):
        block = cowork.plan_handback_wake_block("re-plan the data layer")
        self.assertIn("<handoff>\nre-plan the data layer\n</handoff>", block)
        self.assertIn("ready_for_review", block)

    def test_build_reviewer_context_embeds_plan_and_status_and_diff_note(self):
        pj, pm = self._plan('{"plan": "J"}', "# MD PLAN")
        status = pj + ".status"
        with open(status, "w") as fh:
            fh.write('{"status": "ready_for_review"}')
        ctx = cowork.assemble_build_reviewer_context(
            "goal", ["builder"], pj, pm, status)
        self.assertIn('"plan": "J"', ctx)
        self.assertIn("# MD PLAN", ctx)
        self.assertIn("ready_for_review", ctx)
        self.assertIn("goal", ctx)
        # the full-delta recipe: plain `git diff` is not enough — staged and
        # untracked channels must be named.
        self.assertIn("git status --porcelain", ctx)
        self.assertIn("git diff HEAD", ctx)
        self.assertIn("untracked", ctx)
        resumed = cowork.assemble_build_reviewer_resume_context(
            pj, pm, status, context_update="new goal")
        self.assertIn("<context>\nnew goal\n</context>", resumed)
        self.assertIn("git status --porcelain", resumed)
        self.assertIn("untracked", resumed)

    def test_baseline_note_formats_and_flows_into_context(self):
        # clean start: just names the commit
        clean = cowork.build_baseline_note("abc123def4567890", False)
        self.assertIn("abc123def456", clean)
        self.assertNotIn("dirty", clean)
        # dirty start: warns the reviewer not to attribute every change
        dirty = cowork.build_baseline_note("abc123def4567890", True)
        self.assertIn("dirty", dirty)
        self.assertIn("predate", dirty)
        self.assertEqual(cowork.build_baseline_note(None, True), "")
        pj, pm = self._plan('{"plan": "J"}', "# MD")
        status = pj + ".status"
        with open(status, "w") as fh:
            fh.write("{}")
        ctx = cowork.assemble_build_reviewer_context(
            "g", ["builder"], pj, pm, status, baseline_note=dirty)
        self.assertIn("dirty", ctx)

    def test_git_baseline_tolerates_non_repo(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        self.assertEqual(cowork._git_build_baseline(d), (None, None))

    def test_baselines_note_enumerates_every_repo(self):
        # two repos, one dirty -> both commits listed, dirty warning on the
        # dirty one; a repo with no HEAD still appears (not dropped).
        note = cowork.build_baselines_note([
            {"path": "/x/a", "head": "aaaaaaaaaaaa1111", "dirty": False},
            {"path": "/x/b", "head": "bbbbbbbbbbbb2222", "dirty": True},
            {"path": "/x/c", "head": None, "dirty": None}])
        self.assertIn("/x/a started from commit aaaaaaaaaaaa", note)
        self.assertIn("/x/b started from commit bbbbbbbbbbbb", note)
        self.assertIn("dirty", note)
        self.assertIn("/x/c (no commit baseline)", note)
        self.assertEqual(cowork.build_baselines_note([]), "")

    def test_diff_recipe_branches_per_repo_head(self):
        repos = [{"path": "/x/a", "has_head": True},
                 {"path": "/x/b", "has_head": False}]
        recipe = cowork._build_diff_recipe(repos)
        # every selected root is named
        self.assertIn("/x/a", recipe)
        self.assertIn("/x/b", recipe)
        # has_head=True gets the `git -C <root> diff HEAD` branch
        self.assertIn("git -C /x/a diff HEAD", recipe)
        # has_head=False gets the no-HEAD branch and NOT `diff HEAD`
        self.assertNotIn("git -C /x/b diff HEAD", recipe)
        self.assertIn("git -C /x/b diff --cached", recipe)
        self.assertIn("git -C /x/b status --porcelain", recipe)
        # empty/None -> back-compat single-cwd recipe
        fallback = cowork._build_diff_recipe(None)
        self.assertIn("git status --porcelain", fallback)
        self.assertIn("git diff HEAD", fallback)

    def test_build_reviewer_context_names_each_selected_root(self):
        pj, pm = self._plan('{"plan": "J"}', "# MD")
        status = pj + ".status"
        with open(status, "w") as fh:
            fh.write("{}")
        repos = [{"path": "/repo/code", "has_head": True},
                 {"path": "/repo/infra", "has_head": False}]
        ctx = cowork.assemble_build_reviewer_context(
            "g", ["builder"], pj, pm, status,
            baseline_note="/repo/code started from commit abc.",
            baseline_repos=repos)
        self.assertIn("/repo/code", ctx)
        self.assertIn("/repo/infra", ctx)
        self.assertIn("git -C /repo/code diff HEAD", ctx)
        self.assertNotIn("git -C /repo/infra diff HEAD", ctx)
        resumed = cowork.assemble_build_reviewer_resume_context(
            pj, pm, status, baseline_repos=repos)
        self.assertIn("/repo/infra", resumed)
        self.assertIn("git -C /repo/code diff HEAD", resumed)


class GitRootDiscoveryTest(unittest.TestCase):
    """discover_git_roots / _plan_repo_set: deterministic nearest-root scan."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.realpath(d)

    def _mkrepo(self, *parts):
        path = os.path.join(*parts)
        os.makedirs(os.path.join(path, ".git"), exist_ok=True)
        return path

    def test_self_root(self):
        d = self._tmp()
        self._mkrepo(d)
        self.assertEqual(cowork.discover_git_roots(d),
                         [{"path": d, "relation": "self"}])

    def test_descendant_roots(self):
        d = self._tmp()
        a = self._mkrepo(d, "repoA")
        b = self._mkrepo(d, "repoB")
        roots = cowork.discover_git_roots(d)
        self.assertEqual([r["relation"] for r in roots],
                         ["descendant", "descendant"])
        self.assertEqual([r["path"] for r in roots], sorted([a, b]))

    def test_descendant_roots_stable_sorted_order(self):
        d = self._tmp()
        # created out of alphabetical order
        c = self._mkrepo(d, "ccc")
        a = self._mkrepo(d, "aaa")
        b = self._mkrepo(d, "bbb")
        r1 = cowork.discover_git_roots(d)
        r2 = cowork.discover_git_roots(d)
        self.assertEqual(r1, r2)  # identical across repeated calls
        self.assertEqual([r["path"] for r in r1], [a, b, c])  # sorted
        for r in r1:
            self.assertTrue(os.path.isabs(r["path"]))

    def test_nested_root_excluded(self):
        d = self._tmp()
        a = self._mkrepo(d, "repoA")
        self._mkrepo(d, "repoA", "vendor", "libB")  # nested -> excluded
        self.assertEqual([r["path"] for r in cowork.discover_git_roots(d)], [a])

    def test_ancestor_root(self):
        d = self._tmp()
        self._mkrepo(d)
        child = os.path.join(d, "src", "deep")
        os.makedirs(child, exist_ok=True)
        self.assertEqual(cowork.discover_git_roots(child),
                         [{"path": d, "relation": "ancestor"}])

    def test_fallback_when_no_git(self):
        d = self._tmp()
        sub = os.path.join(d, "plain")
        os.makedirs(sub, exist_ok=True)
        self.assertEqual(cowork.discover_git_roots(sub),
                         [{"path": sub, "relation": "fallback"}])

    def test_plan_repo_set_reads_selected(self):
        d = self._tmp()
        pj = os.path.join(d, "plan.json")
        with open(pj, "w") as fh:
            json.dump({"result": {"repos": [
                {"path": "/x/a", "selected": True},
                {"path": "/x/b", "selected": False},
                {"path": "/x/c", "selected": True}]}}, fh)
        self.assertEqual(cowork._plan_repo_set(pj, d), ["/x/a", "/x/c"])

    def test_plan_repo_set_falls_back_to_discovery(self):
        d = self._tmp()
        self._mkrepo(d)  # d is a root -> discovery yields [d]
        pj = os.path.join(d, "plan.json")
        with open(pj, "w") as fh:
            json.dump({"result": {"goal": "no repos field"}}, fh)
        self.assertEqual(cowork._plan_repo_set(pj, d), [d])
        bad = os.path.join(d, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        self.assertEqual(cowork._plan_repo_set(bad, d), [d])


class BuilderLoopTest(unittest.TestCase):
    """Drive run_builder/_role_loop with a fake session writing build statuses."""

    def _paths(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        base = os.path.join(d, ".cowork")
        # a real plan so the seed/eval helpers can read it
        os.makedirs(base, exist_ok=True)
        pj = os.path.join(base, "planner.plan.X.json")
        pm = os.path.join(base, "planner.plan.X.md")
        with open(pj, "w") as fh:
            json.dump({"result": {"goal": "G"}}, fh)
        with open(pm, "w") as fh:
            fh.write("# PLAN")
        return (os.path.join(base, "builder.status.X.json"),
                os.path.join(base, "builder-review.X.json"), pj, pm)

    def _session(self, status_path, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                entry = statuses.pop(0) if statuses else {
                    "status": "ready_for_review"}
                os.makedirs(os.path.dirname(status_path), exist_ok=True)
                with open(status_path, "w") as fh:
                    json.dump(dict({"session": "X", "role": "builder",
                                    "result": {}}, **entry), fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def _run(self, status_path, review_path, pj, pm, sess, io_in,
             reviewer_runner=None, handoff_confirm=None, selected=None):
        out = io.StringIO()
        outcomes = []
        selected = selected or ["builder", "build-reviewer"]
        config = cowork.default_config(selected)
        config["builder"]["controller"] = "codex"
        rc = cowork.run_builder(
            config, "seed", selected, io_in=io_in, io_out=out,
            build_status_path=status_path, build_review_path=review_path,
            plan_json_path=pj, plan_md_path=pm,
            session_factory=lambda *a, **k: sess,
            reviewer_runner=reviewer_runner,
            handoff_confirm=handoff_confirm,
            on_outcome=lambda o, p: outcomes.append((o, p)))
        return rc, out.getvalue(), outcomes

    def test_tty_gate_is_binary_no_ask(self):
        # The builder gate keeps its prior binary confirm contract on a TTY:
        # the scout/planner-only 'Ask a question' path must NOT appear here.
        import unittest.mock as mock
        status, review, pj, pm = self._paths()
        sess = self._session(status, [{"status": "ready_for_review"}])

        def runner(config, context, selected, p, review_path, **kw):
            return {"verdict": "approve"}

        config = cowork.default_config(["builder", "build-reviewer"])
        config["builder"]["controller"] = "codex"
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "confirm",
                                  return_value=True) as conf, \
                mock.patch.object(cowork.ui, "select") as sel:
            rc = cowork.run_builder(
                config, "seed", ["builder", "build-reviewer"],
                io_in=FakeTTY(), io_out=FakeTTY(),
                build_status_path=status, build_review_path=review,
                plan_json_path=pj, plan_md_path=pm,
                session_factory=lambda *a, **k: sess,
                reviewer_runner=runner,
                on_outcome=lambda o, p: None)
        self.assertEqual(rc, 0)
        conf.assert_called_once()        # binary approve gate
        sel.assert_not_called()          # no 3-way ask gate for the builder

    def test_needs_input_then_ready_then_approve(self):
        status, review, pj, pm = self._paths()
        sess = self._session(status, [{"status": "needs_input"},
                                      {"status": "ready_for_review"}])
        rfn_calls = []

        def runner(config, context, selected, p, review_path, **kw):
            rfn_calls.append(p)
            return {"verdict": "approve"}

        rc, text, outcomes = self._run(
            status, review, pj, pm, sess, io.StringIO("answer\n\n"),
            reviewer_runner=runner)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent[1], "answer")
        self.assertIn("builder needs your input", text)
        self.assertIn("build ready for review", text)
        self.assertIn("builder finished", text)
        self.assertEqual(rfn_calls, [status])     # reviewer saw the status file
        self.assertEqual(outcomes, [("approved", None)])

    def test_reviewer_revise_loops_then_user_gate(self):
        status, review, pj, pm = self._paths()
        sess = self._session(status, [{"status": "ready_for_review"},
                                      {"status": "ready_for_review"}])
        verdicts = [{"verdict": "revise", "findings": ["out-of-plan change"]},
                    {"verdict": "approve"}]

        def runner(config, context, selected, p, review_path, **kw):
            return verdicts.pop(0)

        rc, text, outcomes = self._run(
            status, review, pj, pm, sess, io.StringIO(), reviewer_runner=runner)
        self.assertEqual(rc, 0)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertIn("out-of-plan change", sess.sent[1])
        self.assertIn("update your build", sess.sent[1])  # artifact_noun=build
        # single-voice: reviewer findings never reach the user channel
        self.assertNotIn("out-of-plan change", text)
        self.assertIn("reviewed: changes requested", text)
        self.assertEqual(outcomes, [("approved", None)])

    def test_needs_user_relayed_in_builder_voice(self):
        status, review, pj, pm = self._paths()
        # ready (reviewer needs_user) -> builder relays + writes needs_input ->
        # user answers -> ready (reviewer approves).
        sess = self._session(status, [{"status": "ready_for_review"},
                                      {"status": "needs_input"},
                                      {"status": "ready_for_review"}])
        verdicts = [{"verdict": "needs_user",
                     "user_question": "ship behind a flag or not?"},
                    {"verdict": "approve"}]

        def runner(config, context, selected, p, review_path, **kw):
            return verdicts.pop(0)

        rc, text, outcomes = self._run(
            status, review, pj, pm, sess, io.StringIO("flag it\n"),
            reviewer_runner=runner)
        self.assertEqual(rc, 0)
        # the reviewer's question is relayed to the builder for faithful relay
        self.assertIn("ship behind a flag or not?", sess.sent[1])
        self.assertIn("builder needs your input", text)
        self.assertEqual(outcomes, [("approved", None)])

    def test_round_cap_falls_through_to_dissent_gate(self):
        status, review, pj, pm = self._paths()
        sess = self._session(
            status, [{"status": "ready_for_review"}] * (cowork.REVIEW_ROUND_CAP
                                                        + 1))

        def runner(config, context, selected, p, review_path, **kw):
            return {"verdict": "revise", "findings": ["still wrong"]}

        # off a TTY the dissent gate keeps the blank=finish contract
        rc, text, outcomes = self._run(
            status, review, pj, pm, sess, io.StringIO("\n"),
            reviewer_runner=runner)
        self.assertEqual(rc, 0)
        self.assertIn("review cap reached", text)
        self.assertIn("still wrong", text)        # dissent notes shown to user
        self.assertEqual(outcomes, [("approved", None)])

    def test_handoff_confirmed_returns_payload_and_names_planner(self):
        status, review, pj, pm = self._paths()
        sess = self._session(
            status, [{"status": "handoff_back",
                      "handoff": "re-plan the schema"}])
        prompts = []
        rc, text, outcomes = self._run(
            status, review, pj, pm, sess, io.StringIO(),
            handoff_confirm=lambda io_in, io_out: prompts.append(True) or True)
        self.assertEqual(rc, 0)
        # the gate banner names the PLANNER (not the scout)
        self.assertIn("hand the work back to the planner", text)
        self.assertIn("re-plan the schema", text)
        self.assertEqual(outcomes, [("handoff", "re-plan the schema")])

    def test_handoff_declined_names_planner_and_continues(self):
        status, review, pj, pm = self._paths()
        sess = self._session(
            status, [{"status": "handoff_back", "handoff": "re-plan"},
                     {"status": "ready_for_review"}])

        def runner(config, context, selected, p, review_path, **kw):
            return {"verdict": "approve"}

        rc, text, outcomes = self._run(
            status, review, pj, pm, sess, io.StringIO(),
            reviewer_runner=runner,
            handoff_confirm=lambda io_in, io_out: False)
        self.assertEqual(rc, 0)
        # the decline note injected into the builder names the PLANNER
        self.assertIn("DECLINED", sess.sent[1])
        self.assertIn("planner", sess.sent[1])
        self.assertEqual(outcomes, [("approved", None)])

    def test_no_reviewer_on_team_skips_review(self):
        status, review, pj, pm = self._paths()
        sess = self._session(status, [{"status": "ready_for_review"}])
        rc, text, outcomes = self._run(
            status, review, pj, pm, sess, io.StringIO(),
            selected=["builder"])
        self.assertEqual(rc, 0)
        self.assertNotIn("reviewed", text)
        self.assertEqual(outcomes, [("approved", None)])


class BuildPhaseFlowTest(unittest.TestCase):
    """run_flow planning -> building chaining, builder->planner hand-back round
    trip, resume cascades, and the builder-not-on-team notice."""

    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _fakes(self, scout_outcomes, planner_outcomes, builder_outcomes):
        calls = {"scout": [], "planner": [], "builder": []}

        def fake_scout(config, context, selected, on_outcome=None,
                       on_session=None, resume_id=None, **kw):
            calls["scout"].append({"context": context, "resume_id": resume_id})
            if on_session and resume_id is None:
                on_session("claude", "scout-%d" % len(calls["scout"]))
            if on_outcome:
                on_outcome(scout_outcomes.pop(0))
            return 0

        def fake_planner(config, context, selected, on_outcome=None,
                         on_session=None, resume_id=None, **kw):
            calls["planner"].append({
                "context": context, "resume_id": resume_id,
                "planning_epoch": kw.get("planning_epoch")})
            if on_session and resume_id is None:
                on_session("claude", "planner-%d" % len(calls["planner"]))
            if on_outcome:
                on_outcome(*planner_outcomes.pop(0))
            return 0

        def fake_builder(config, context, selected, on_outcome=None,
                         on_session=None, resume_id=None, **kw):
            calls["builder"].append({
                "context": context, "resume_id": resume_id,
                "build_status_path": kw.get("build_status_path"),
                "build_review_path": kw.get("build_review_path"),
                "plan_json_path": kw.get("plan_json_path"),
                "building_epoch": kw.get("building_epoch")})
            if on_session and resume_id is None:
                on_session("claude", "builder-%d" % len(calls["builder"]))
            if on_outcome:
                on_outcome(*builder_outcomes.pop(0))
            return 0

        return calls, fake_scout, fake_planner, fake_builder

    def _prime_intel_and_plan(self, spath, suid):
        state_store.ensure_session(spath, None, suid)
        # Produced artifacts now live under the session-assets home, not the
        # project-local .cowork dir (which keeps only session.json).
        base = state_store.session_assets_dir(suid)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "scout.intel.json"), "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        with open(os.path.join(base, "planner.plan.json"), "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"step": "S1"}}, fh)
        with open(os.path.join(base, "planner.plan.md"), "w") as fh:
            fh.write("# PLAN MD")

    def test_plan_approval_chains_into_building(self):
        spath = self._tmp_session()
        self._prime_intel_and_plan(spath, "S")
        calls, fs, fp, fb = self._fakes(
            ["approved"], [("approved", None)], [("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team",
                        "scout,planner,builder,build-reviewer",
                        "--context", "do it", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["builder"]), 1)
        # fresh builder seeded with the approved plan + shared context
        seed = calls["builder"][0]["context"]
        self.assertIn("APPROVED", seed)
        # #1: path-first seed (read instruction), not the embedded plan bodies.
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, seed)
        self.assertNotIn('"step": "S1"', seed)
        self.assertNotIn("# PLAN MD", seed)
        self.assertIn("do it", seed)
        # builder artifacts carry uuid-free names; the session FOLDER isolates them
        self.assertIn("builder.status.json",
                      calls["builder"][0]["build_status_path"])
        self.assertIn("builder-review.json",
                      calls["builder"][0]["build_review_path"])
        self.assertIn("/S/", calls["builder"][0]["build_status_path"])  # uuid dir
        self.assertEqual(calls["builder"][0]["building_epoch"], 1)
        # build approval is terminal: phase persisted as building
        self.assertEqual(state_store.get_phase(state_store.load(spath)),
                         "building")

    def test_plan_approval_without_builder_prints_notice(self):
        spath = self._tmp_session()
        self._prime_intel_and_plan(spath, "S")
        calls, fs, fp, fb = self._fakes(
            ["approved"], [("approved", None)], [])
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner",
                        "--context", "x", "--session-file", spath]),
            io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["builder"]), 0)
        self.assertIn("building not selected", out.getvalue())
        self.assertEqual(state_store.get_phase(state_store.load(spath)),
                         "planning")

    def test_builder_handback_round_trip_resumes_planner_then_builder(self):
        spath = self._tmp_session()
        self._prime_intel_and_plan(spath, "S")
        calls, fs, fp, fb = self._fakes(
            ["approved"],
            [("approved", None), ("approved", None)],
            [("handoff", "re-plan the data model"), ("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner,builder",
                        "--context", "x", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        # builder ran twice: fresh, then resumed with the plan-updated block
        self.assertEqual(len(calls["builder"]), 2)
        self.assertEqual(calls["builder"][1]["resume_id"], "builder-1")
        self.assertIn("plan changed", calls["builder"][1]["context"])
        # planner ran twice: fresh, then resumed with the handback wake block
        self.assertEqual(len(calls["planner"]), 2)
        self.assertEqual(calls["planner"][1]["resume_id"], "planner-1")
        self.assertIn("<handoff>\nre-plan the data model\n</handoff>",
                      calls["planner"][1]["context"])
        # the building epoch bumps on each plan-approved -> building transition
        self.assertEqual(calls["builder"][0]["building_epoch"], 1)
        self.assertEqual(calls["builder"][1]["building_epoch"], 2)
        self.assertEqual(state_store.get_phase(state_store.load(spath)),
                         "building")

    def test_resume_into_building_without_builder_falls_back_to_planning(self):
        spath = self._tmp_session()
        state = state_store.save_config(
            spath, ["scout", "planner", "planning-advisor"],
            cowork.default_config(["scout", "planner", "planning-advisor"]))
        state = state_store.save_phase(spath, "building", prior=state)
        state = state_store.save_role_session(
            spath, "planner", "claude", "planner-9", prior=state)
        calls, fs, fp, fb = self._fakes([], [("approved", None)], [])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner,planning-advisor",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["builder"]), 0)
        self.assertEqual(len(calls["scout"]), 0)        # planner resumes
        self.assertEqual(len(calls["planner"]), 1)
        self.assertEqual(calls["planner"][0]["resume_id"], "planner-9")

    def test_resume_into_building_without_builder_or_planner_falls_to_scouting(self):
        spath = self._tmp_session()
        state = state_store.save_config(
            spath, ["scout"], cowork.default_config(["scout"]))
        state = state_store.save_phase(spath, "building", prior=state)
        state = state_store.save_role_session(
            spath, "scout", "claude", "scout-9", prior=state)
        calls, fs, fp, fb = self._fakes([("ended")], [], [])
        rc = cowork.run_flow(
            self._args(["--team", "scout", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["scout"]), 1)        # scout resumes
        self.assertEqual(calls["scout"][0]["resume_id"], "scout-9")

    def test_resume_into_building_without_builder_planner_or_scout_notes(self):
        spath = self._tmp_session()
        state = state_store.save_config(
            spath, ["build-reviewer"],
            cowork.default_config(["build-reviewer"]))
        state = state_store.save_phase(spath, "building", prior=state)
        out = io.StringIO()
        calls, fs, fp, fb = self._fakes([], [], [])
        rc = cowork.run_flow(
            self._args(["--team", "build-reviewer", "--session-file", spath]),
            io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        self.assertIn("scout not selected", out.getvalue())

    def test_dirty_worktree_warns_and_passes_baseline_note(self):
        import unittest.mock as mock
        spath = self._tmp_session()
        self._prime_intel_and_plan(spath, "S")
        calls, fs, fp, fb = self._fakes(
            ["approved"], [("approved", None)], [("approved", None)])
        captured = {}

        def fake_builder(config, context, selected, on_outcome=None,
                         on_session=None, resume_id=None, **kw):
            captured["baseline_note"] = kw.get("baseline_note")
            if on_outcome:
                on_outcome("approved", None)
            return 0

        out = io.StringIO()
        with mock.patch.object(cowork, "_git_build_baseline",
                               return_value=("deadbeefcafe1234", True)):
            rc = cowork.run_flow(
                self._args(["--team", "scout,planner,builder",
                            "--context", "x", "--session-file", spath]),
                io_out=out, which=lambda c: "/bin/" + c,
                run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fake_builder)
        self.assertEqual(rc, 0)
        self.assertIn("dirty worktree", out.getvalue())
        self.assertIn("deadbeefcafe", captured["baseline_note"])
        self.assertIn("dirty", captured["baseline_note"])

    def test_baseline_read_from_cwd_not_session_file_parent(self):
        # Regression: with --session-file OUTSIDE the repo, the baseline must be
        # read from the process cwd (where builder/reviewer's git diff runs),
        # not from the session-file parent.
        import unittest.mock as mock
        spath = self._tmp_session()       # a temp dir, far from cwd
        self._prime_intel_and_plan(spath, "S")
        calls, fs, fp, fb = self._fakes(
            ["approved"], [("approved", None)], [("approved", None)])
        seen = {}

        def fake_baseline(cwd=None):
            seen["cwd"] = cwd
            return (None, None)

        with mock.patch.object(cowork, "_git_build_baseline", fake_baseline):
            rc = cowork.run_flow(
                self._args(["--team", "scout,planner,builder",
                            "--context", "x", "--session-file", spath]),
                io_out=io.StringIO(), which=lambda c: "/bin/" + c,
                run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        self.assertEqual(seen["cwd"], os.getcwd())
        self.assertNotEqual(seen["cwd"], os.path.dirname(os.path.dirname(spath)))

    def test_resume_into_building_without_builder_id_seeds_from_plan(self):
        # Killed between save_phase("building") and the builder id save: the
        # next run starts a FRESH builder from the approved plan.
        spath = self._tmp_session()
        self._prime_intel_and_plan(spath, "S")
        state = state_store.load(spath)
        state = state_store.save_config(
            spath, ["scout", "planner", "builder"],
            cowork.default_config(["scout", "planner", "builder"]),
            prior=state)
        state = state_store.save_phase(spath, "building", prior=state)
        calls, fs, fp, fb = self._fakes([], [], [("approved", None)])
        rc = cowork.run_flow(
            self._args(["--team", "scout,planner,builder",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fs, run_planner_fn=fp, run_builder_fn=fb)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["builder"]), 1)
        self.assertIsNone(calls["builder"][0]["resume_id"])
        seed = calls["builder"][0]["context"]
        self.assertIn("APPROVED", seed)
        # #1: path-first seed (read instruction), not the embedded plan body.
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, seed)
        self.assertNotIn('"step": "S1"', seed)


class BuildingEvalTest(_EvalEnvMixin, unittest.TestCase):
    """Predecessor scoring in the building phase: the builder and the
    build-reviewer each evaluate the planner (consumed plan) once per phase."""

    def _plan(self, d):
        pj = os.path.join(d, "planner.plan.S.json")
        pm = os.path.join(d, "planner.plan.S.md")
        with open(pj, "w") as fh:
            json.dump({"result": {"goal": "G-PLAN"}}, fh)
        with open(pm, "w") as fh:
            fh.write("# PLAN-MD-MARKER")
        return pj, pm

    def _session(self, scratch, evaluatees):
        class FakeSession:
            def __init__(self):
                self.io_out = io.StringIO()
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                with open(scratch, "w") as fh:
                    json.dump({"evaluations": [
                        {"evaluatee": e,
                         "criteria": [{"name": "c", "score": 4,
                                       "feedback": "ok"}],
                         "enhancement_suggestions": "s"}
                        for e in evaluatees]}, fh)
        return FakeSession()

    def test_consumed_plan_descriptor_embeds_both_artifacts(self):
        d = self._cowork_dir()
        pj, pm = self._plan(d)
        consumed = cowork.plan_consumed_upstream(pj, pm, 3)
        self.assertEqual(consumed["role"], "planner")
        self.assertEqual(consumed["context"], "consumed-plan")
        self.assertEqual(consumed["epoch_field"], "building_epoch")
        spec = cowork._consumed_upstream_spec(
            consumed, None, "builder", 1)
        self.assertEqual(spec["evaluatee"], "planner")
        # #2: both plan artifacts ride path-first (paths + read instruction),
        # not their embedded bodies.
        self.assertIn(pj, spec["artifact_block"])
        self.assertIn(pm, spec["artifact_block"])
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION,
                      spec["artifact_block"])
        self.assertNotIn("G-PLAN", spec["artifact_block"])
        self.assertNotIn("# PLAN-MD-MARKER", spec["artifact_block"])
        self.assertEqual(spec["building_epoch"], 3)

    def test_builder_evaluate_fn_bundles_planner_once_per_phase(self):
        self._scores_root()
        d = self._cowork_dir()
        pj, pm = self._plan(d)
        scratch = state_store.eval_scratch_path_for(d, "builder", "S")
        consumed = cowork.plan_consumed_upstream(pj, pm, 1)
        fn = cowork._make_evaluate_fn(
            "builder", "build-reviewer", "building", scratch,
            state_store.scores_path_for("S"), "S", consumed_upstream=consumed)
        sess = self._session(scratch, ("build-reviewer", "planner"))
        fn(sess, {"verdict": "revise"}, 1)
        self.assertIn("Evaluatee: build-reviewer", sess.sent[0])
        self.assertIn("Evaluatee: planner", sess.sent[0])
        # #2: consumed plan rides path-first, not its embedded body.
        self.assertNotIn("G-PLAN", sess.sent[0])
        self.assertIn(pj, sess.sent[0])
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, sess.sent[0])
        # round 2 (and any later round) does not re-bundle the planner eval
        sess2 = self._session(scratch, ("build-reviewer",))
        fn(sess2, {"verdict": "approve"}, 2)
        self.assertNotIn("Evaluatee: planner", sess2.sent[0])
        data = self._scores("S")
        planner_entries = [e for e in data["evaluations"]
                           if e["evaluatee"] == "planner"]
        self.assertEqual(len(planner_entries), 1)
        self.assertEqual(planner_entries[0]["evaluator"], "builder")
        self.assertEqual(planner_entries[0]["context"], "consumed-plan")
        self.assertEqual(planner_entries[0]["building_epoch"], 1)

    def test_consumed_plan_refires_for_new_building_epoch(self):
        self._scores_root()
        d = self._cowork_dir()
        pj, pm = self._plan(d)
        scratch = state_store.eval_scratch_path_for(d, "builder", "S")

        def make_fn(epoch):
            return cowork._make_evaluate_fn(
                "builder", "build-reviewer", "building", scratch,
                state_store.scores_path_for("S"), "S",
                consumed_upstream=cowork.plan_consumed_upstream(pj, pm, epoch))

        make_fn(1)(self._session(scratch, ("build-reviewer", "planner")),
                   {"verdict": "approve"}, 1)
        # resume within the SAME epoch: deduped, no re-emit
        s2 = self._session(scratch, ("build-reviewer",))
        make_fn(1)(s2, {"verdict": "approve"}, 1)
        self.assertNotIn("Evaluatee: planner", s2.sent[0])
        # new epoch (hand-back round trip): re-fires
        s3 = self._session(scratch, ("build-reviewer", "planner"))
        make_fn(2)(s3, {"verdict": "approve"}, 1)
        self.assertIn("Evaluatee: planner", s3.sent[0])
        data = self._scores("S")
        planner_entries = [e for e in data["evaluations"]
                           if e["evaluatee"] == "planner"]
        self.assertEqual(sorted(e["building_epoch"] for e in planner_entries),
                         [1, 2])


# --------------------------------------------------------------------------- #
# Channel delineation: user-facing vs. internal (self-narration / reviewer).    #
# --------------------------------------------------------------------------- #


class ChannelParserTest(unittest.TestCase):
    def test_happy_path_splits_and_strips_markers(self):
        text = "before\n[[internal]]\nnote\n[[/internal]]\nafter\n"
        segs, end = ui.split_channel_segments(text)
        self.assertEqual([c for c, _ in segs], ["user", "internal", "user"])
        joined = "".join(s for _c, s in segs)
        self.assertNotIn("[[internal]]", joined)
        self.assertNotIn("[[/internal]]", joined)
        self.assertIn("note", segs[1][1])     # the internal segment holds 'note'
        self.assertIn("before", segs[0][1])
        self.assertIn("after", segs[2][1])
        self.assertFalse(end)  # closed block -> ends on the user channel

    def test_marker_free_is_byte_identical(self):
        for text in ("", "plain text", "a\n\nb\n", "line1\nline2"):
            segs, end = ui.split_channel_segments(text)
            self.assertEqual("".join(s for _c, s in segs), text)
            self.assertFalse(end)

    def test_unclosed_block_reports_internal_end(self):
        segs, end = ui.split_channel_segments("u\n[[internal]]\nstill open\n")
        self.assertTrue(end)  # force-close is the caller's job (end of turn)
        self.assertEqual(segs[-1][0], "internal")

    def test_stray_close_and_double_open_are_noops(self):
        # stray close with no open: dropped, stays user.
        segs, end = ui.split_channel_segments("[[/internal]]\nplain\n")
        self.assertEqual([c for c, _ in segs], ["user"])
        self.assertNotIn("[[/internal]]", "".join(s for _c, s in segs))
        self.assertFalse(end)
        # second open while already open: no-op (depth-1 boolean).
        segs2, _ = ui.split_channel_segments(
            "[[internal]]\na\n[[internal]]\nb\n[[/internal]]\n")
        self.assertEqual([c for c, _ in segs2], ["internal"])
        self.assertIn("a", "".join(s for _c, s in segs2))
        self.assertIn("b", "".join(s for _c, s in segs2))

    def test_literal_marker_mid_line_is_verbatim(self):
        # Only a full line equal to the marker is control; mid-line is content.
        text = "talk about [[internal]] inline\n"
        segs, _ = ui.split_channel_segments(text)
        self.assertEqual(segs, [("user", text)])

    def test_internal_start_seeds_state(self):
        segs, end = ui.split_channel_segments("carried\n", internal_start=True)
        self.assertEqual(segs[0][0], "internal")
        self.assertTrue(end)


class StreamingChannelTest(unittest.TestCase):
    def test_nontty_strips_marker_lines_plain(self):
        out = io.StringIO()
        with ui.StreamingMarkdown(out, "scout › ") as r:
            r.feed("hi\n[[internal]]\nsecret\n[[/internal]]\nbye\n")
        self.assertEqual(out.getvalue(), "\nscout › hi\nsecret\nbye\n\n")

    def test_nontty_marker_split_across_chunks(self):
        out = io.StringIO()
        with ui.StreamingMarkdown(out, "scout › ") as r:
            r.feed("before\n[[intern")          # marker split mid-line
            r.feed("al]]\nINSIDE\n[[/internal]]\nafter\n")
        text = out.getvalue()
        self.assertNotIn("[[internal]]", text)
        self.assertNotIn("[[/internal]]", text)
        self.assertIn("INSIDE", text)
        self.assertIn("before", text)
        self.assertIn("after", text)

    def test_nontty_marker_free_byte_identical(self):
        out = io.StringIO()
        with ui.StreamingMarkdown(out, "scout › ") as r:
            r.feed("a\n\n")
            r.feed("b")
        self.assertEqual(out.getvalue(), "\nscout › a\n\nb\n")

    def test_fresh_region_starts_on_user_channel(self):
        # Channel state never carries across turns: a new region is fresh.
        r = ui.StreamingMarkdown(io.StringIO(), "scout › ")
        self.assertFalse(r._channel_internal)
        self.assertFalse(r._nontty_internal)

    def test_internal_region_seeds_internal_state(self):
        r = ui.StreamingMarkdown(io.StringIO(), "rev › ", internal=True)
        self.assertTrue(r._channel_internal)

    def test_internal_region_nontty_still_plain(self):
        # Off a TTY there is no styling; an internal region writes plain text.
        out = io.StringIO()
        with ui.StreamingMarkdown(out, "rev › ", internal=True) as r:
            r.feed("verdict notes")
        self.assertEqual(out.getvalue(), "\nrev › verdict notes\n")

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_internal_block_dimmed_markers_stripped(self):
        import unittest.mock as mock
        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with ui.StreamingMarkdown(out, "scout › ") as r:
                r.feed("user line\n\n[[internal]]\ninternal note\n"
                       "[[/internal]]\n\ntail line\n")
        text = out.getvalue()
        self.assertNotIn("[[internal]]", text)
        self.assertNotIn("[[/internal]]", text)
        self.assertIn("internal note", text)
        self.assertIn("user line", text)
        self.assertIn("\x1b[2m", text)  # dim styling emitted for the block

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_internal_region_dims_whole_content(self):
        import unittest.mock as mock
        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with ui.StreamingMarkdown(out, "rev › ", internal=True) as r:
                r.feed("wholly internal narration\n")
        text = out.getvalue()
        self.assertIn("wholly internal narration", text)
        self.assertIn("\x1b[2m", text)

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_internal_region_strips_emitted_markers(self):
        # Even a wholly-internal region must never render literal sentinels if
        # the reviewer/advisor happens to emit them (contract: always stripped).
        import unittest.mock as mock
        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with ui.StreamingMarkdown(out, "rev › ", internal=True) as r:
                r.feed("[[internal]]\nverdict body\n[[/internal]]\n")
        text = out.getvalue()
        self.assertNotIn("[[internal]]", text)
        self.assertNotIn("[[/internal]]", text)
        self.assertIn("verdict body", text)

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_marker_split_across_chunks_no_flash(self):
        # A marker split mid-line must not flash half-matched in the live tail,
        # and must be fully stripped from the final output.
        import unittest.mock as mock
        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            region = ui.StreamingMarkdown(out, "scout › ")
            region.__enter__()
            try:
                region.feed("before\n[[intern")   # partial marker, no newline
                region._live.refresh()             # force a live frame
                # The partial sentinel is held, never shown half-matched.
                self.assertNotIn("[[intern", out.getvalue())
                region.feed("al]]\nINSIDE\n[[/internal]]\nafter\n")
            finally:
                region.__exit__(None, None, None)
        text = out.getvalue()
        self.assertNotIn("[[internal]]", text)
        self.assertNotIn("[[/internal]]", text)
        self.assertIn("INSIDE", text)
        self.assertIn("after", text)


class RenderMarkdownChannelTest(unittest.TestCase):
    def test_nontty_strips_markers(self):
        out = io.StringIO()
        ui.render_markdown(out, "a\n[[internal]]\nb\n[[/internal]]\nc",
                           enabled=False)
        self.assertEqual(out.getvalue(), "a\nb\nc\n")

    def test_nontty_marker_free_byte_identical(self):
        out = io.StringIO()
        ui.render_markdown(out, "# hi\nbody", enabled=False)
        self.assertEqual(out.getvalue(), "# hi\nbody\n")

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_inline_internal_block(self):
        out = FakeTTY()
        ui.render_markdown(out, "user\n\n[[internal]]\nnote\n[[/internal]]\n\nmore",
                           enabled=True)
        text = out.getvalue()
        self.assertNotIn("[[internal]]", text)
        self.assertIn("note", text)
        self.assertIn("user", text)

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_internal_true_dims_whole(self):
        import unittest.mock as mock
        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            ui.render_markdown(out, "wholly internal", enabled=True, internal=True)
        text = out.getvalue()
        self.assertIn("internal", text)
        self.assertIn("\x1b[2m", text)

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_internal_true_strips_emitted_markers(self):
        out = FakeTTY()
        ui.render_markdown(out, "[[internal]]\nbody text\n[[/internal]]",
                           enabled=True, internal=True)
        text = out.getvalue()
        self.assertNotIn("[[internal]]", text)
        self.assertNotIn("[[/internal]]", text)
        self.assertIn("body text", text)

    def test_nontty_internal_true_strips_markers(self):
        out = io.StringIO()
        ui.render_markdown(out, "a\n[[internal]]\nb\n[[/internal]]\nc",
                           enabled=False, internal=True)
        self.assertEqual(out.getvalue(), "a\nb\nc\n")


class CodexChannelPropagationTest(unittest.TestCase):
    def test_internal_flag_reaches_render_markdown(self):
        import unittest.mock as mock

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        lines = [
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "verdict"}}),
        ]
        captured = {}

        def fake_render(io_out, text, enabled=None, internal=False):
            captured["internal"] = internal
            captured["text"] = text

        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)), \
                mock.patch.object(bridge.ui, "render_markdown", fake_render):
            s = bridge.CodexSession("implement", True, io_out=io.StringIO(),
                                    speaker="scout-reviewer", internal=True)
            s.send("review")
        self.assertTrue(captured.get("internal"))  # propagated to render
        self.assertEqual(captured["text"], "verdict")


# --------------------------------------------------------------------------- #
# Caveman compression directive injection (gated on caveman availability).      #
# --------------------------------------------------------------------------- #


class CavemanDirectiveTest(unittest.TestCase):
    def test_directive_text_gates_on_availability(self):
        self.assertIn("IS installed", cowork.caveman_directive(True))
        self.assertIn("NOT installed", cowork.caveman_directive(False))

    def test_briefs_inject_directive(self):
        on_scout = cowork.assemble_scout_brief(["scout"], "/x.json",
                                               caveman_available=True)
        off_scout = cowork.assemble_scout_brief(["scout"], "/x.json",
                                                caveman_available=False)
        self.assertIn("IS installed", on_scout)
        self.assertIn("NOT installed", off_scout)
        # the scout brief still carries its own write-target guardrail.
        self.assertIn("ONLY write target", on_scout)

        self.assertIn("IS installed", cowork.assemble_planner_brief(
            "a.json", "a.md", caveman_available=True))
        self.assertIn("NOT installed", cowork.assemble_planner_brief(
            "a.json", "a.md", caveman_available=False))
        self.assertIn("IS installed", cowork.assemble_builder_brief(
            "s.json", caveman_available=True))
        self.assertIn("NOT installed", cowork.assemble_builder_brief(
            "s.json", caveman_available=False))
        self.assertIn("IS installed", cowork.assemble_reviewer_brief(
            "r.json", caveman_available=True))
        self.assertIn("NOT installed", cowork.assemble_reviewer_brief(
            "r.json", caveman_available=False))

    def test_available_detects_via_env_path(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        marker = os.path.join(d, "caveman", "SKILL.md")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").close()
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"COPLAN_CAVEMAN_PATHS": marker}):
            self.assertTrue(cowork._caveman_available())


# --------------------------------------------------------------------------- #
# Surfacing the reviewer/advisor REVIEW turn on the internal channel.           #
# --------------------------------------------------------------------------- #


class ReviewerSurfacingTest(unittest.TestCase):
    def _paths(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        intel = os.path.join(d, ".cowork", "scout.intel.X.json")
        review = os.path.join(d, ".cowork", "scout-review.X.json")
        scratch = os.path.join(d, ".cowork", "eval.X.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        return intel, review, scratch

    def test_review_turn_streams_to_surface_io_out(self):
        intel, review, _scratch = self._paths()
        surface = io.StringIO()
        seen = {}

        def factory(controller, io_out):
            seen["io_out"] = io_out

            class FakeRevSession:
                def __init__(self):
                    self.io_out = io_out

                def send(self, text):
                    self.io_out.write("reviewer reasoning ")
                    with open(review, "w") as fh:
                        json.dump({"verdict": "approve"}, fh)

                def close(self):
                    pass
            return FakeRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory, surface_io_out=surface)
        self.assertEqual(verdict["verdict"], "approve")
        self.assertIs(seen["io_out"], surface)  # surfaced: real io_out, not quiet
        self.assertIn("reviewer reasoning", surface.getvalue())

    def test_not_surfaced_is_quiet(self):
        # surface_io_out=None -> the reviewer streams to a quiet sink; a user
        # io_out would see nothing (byte-identical to the hidden behavior).
        intel, review, _scratch = self._paths()
        seen = {}

        def factory(controller, io_out):
            seen["io_out"] = io_out

            class FakeRevSession:
                def send(self, text):
                    with open(review, "w") as fh:
                        json.dump({"verdict": "approve"}, fh)

                def close(self):
                    pass
            return FakeRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory)
        self.assertIsInstance(seen["io_out"], cowork._QuietSink)

    def test_eval_stays_muted_when_surfaced(self):
        intel, review, scratch = self._paths()
        surface = io.StringIO()

        def factory(controller, io_out):
            class FakeRevSession:
                def __init__(self):
                    self.io_out = io_out

                def send(self, text):
                    # Visible marker written on EVERY send; the eval send is
                    # wrapped in _muted_session, so it lands on a quiet sink.
                    self.io_out.write("MARK ")
                    if "[private evaluation turn]" in text:
                        with open(scratch, "w") as fh:
                            json.dump({"evaluations": []}, fh)
                    else:
                        with open(review, "w") as fh:
                            json.dump({"verdict": "approve"}, fh)

                def close(self):
                    pass
            return FakeRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory, surface_io_out=surface,
            eval_scratch_path=scratch,
            eval_specs=[{"evaluatee": "scout", "criteria": ["intel quality"]}])
        # The review turn was visible exactly once; the eval send was muted.
        self.assertEqual(surface.getvalue().count("MARK"), 1)
        self.assertTrue(os.path.exists(scratch))  # eval still produced its file


class MakeReviewFnSurfaceTest(unittest.TestCase):
    def test_test_runner_receives_no_surface_kwarg(self):
        # A test-injected reviewer_runner (no surface-capable marker) must NOT
        # receive surface_io_out — its signature stays byte-identical.
        seen = {}

        def fake_runner(config, context, selected, artifact, review_path,
                        **kwargs):
            seen["kwargs"] = kwargs
            return {"verdict": "approve"}

        fn = cowork.make_review_fn(
            {}, "ctx", ["scout", "scout-reviewer"], "/r.json",
            reviewer_runner=fake_runner, surface_io_out=io.StringIO())
        fn("/artifact", 1)
        self.assertNotIn("surface_io_out", seen["kwargs"])

    def test_surface_capable_runner_receives_kwarg(self):
        seen = {}

        def fake_runner(config, context, selected, artifact, review_path,
                        **kwargs):
            seen["kwargs"] = kwargs
            return {"verdict": "approve"}
        fake_runner._coplan_surface_capable = True

        surface = io.StringIO()
        fn = cowork.make_review_fn(
            {}, "ctx", ["scout", "scout-reviewer"], "/r.json",
            reviewer_runner=fake_runner, surface_io_out=surface)
        fn("/artifact", 1)
        self.assertIs(seen["kwargs"].get("surface_io_out"), surface)


class NoUuidAssetNameTest(unittest.TestCase):
    """Item 1: per-session asset filenames drop the uuid (the per-session folder
    isolates them), while the project-local session.<uuid>.json keeps its uuid
    and the picker still discovers sessions by it."""

    def test_asset_builders_drop_uuid(self):
        d = ".cowork"
        self.assertEqual(state_store.review_path_for(d, "U"),
                         ".cowork/scout-review.json")
        self.assertEqual(state_store.planner_plan_json_path_for(d, "U"),
                         ".cowork/planner.plan.json")
        self.assertEqual(state_store.planner_plan_md_path_for(d, "U"),
                         ".cowork/planner.plan.md")
        self.assertEqual(state_store.planner_review_path_for(d, "U"),
                         ".cowork/planner-review.json")
        self.assertEqual(state_store.build_status_path_for(d, "U"),
                         ".cowork/builder.status.json")
        self.assertEqual(state_store.build_review_path_for(d, "U"),
                         ".cowork/builder-review.json")
        self.assertEqual(state_store.eval_scratch_path_for(d, "scout", "U"),
                         ".cowork/eval.scout.json")
        self.assertEqual(cowork.scout_intel_path(d, "U"), ".cowork/scout.intel.json")

    def test_session_file_keeps_uuid_and_picker_discovers(self):
        import tempfile
        cwd = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(cwd, ignore_errors=True))
        # new_session_path STILL carries the uuid (load-bearing picker anchor).
        path = state_store.new_session_path(cwd, "abc-123")
        self.assertTrue(path.endswith("session.abc-123.json"))
        # and the picker still parses + discovers it by that uuid.
        state_store.save(path, {"session_uuid": "abc-123",
                                "context": "do the thing"})
        rows = state_store.list_sessions(cwd)
        self.assertEqual([r["id"] for r in rows], ["abc-123"])


class PathDisplayTest(unittest.TestCase):
    """Item 2a: home-rooted paths render as ~/… and, on a TTY, carry an OSC 8
    hyperlink whose visible text is the short ~ form."""

    def test_display_path_home_to_tilde(self):
        home = os.path.expanduser("~")
        self.assertEqual(ui.display_path(os.path.join(home, ".cowork", "x")),
                         os.path.join("~", ".cowork", "x"))
        self.assertEqual(ui.display_path(home), "~")
        self.assertEqual(ui.display_path("/tmp/elsewhere/x"), "/tmp/elsewhere/x")
        self.assertEqual(ui.display_path(""), "")

    def test_shorten_path_home_rooted_tilde(self):
        home = os.path.expanduser("~")
        p = os.path.join(home, ".cowork", "sessions", "S", "planner.plan.md")
        # Outside cwd but under home -> ~ form, NOT '…/<basename>'.
        self.assertEqual(ui.shorten_path(p, cwd="/some/other/dir"),
                         os.path.join("~", ".cowork", "sessions", "S",
                                      "planner.plan.md"))

    def test_shorten_path_under_cwd_relative(self):
        self.assertEqual(
            ui.shorten_path("/tmp/work/.cowork/x.json", cwd="/tmp/work"),
            ".cowork/x.json")

    def test_shorten_path_outside_home_and_cwd_basename(self):
        self.assertEqual(
            ui.shorten_path("/var/data/x.json", cwd="/tmp/work"), "…/x.json")

    def test_render_path_osc8_on_tty_plain_off(self):
        home = os.path.expanduser("~")
        p = os.path.join(home, ".cowork", "x")
        tilde = os.path.join("~", ".cowork", "x")
        # Off a TTY: just the short ~ form, no escape sequence.
        self.assertEqual(ui.render_path(p, enabled=False), tilde)
        # On a TTY: an OSC 8 hyperlink to file://<abs>, visible text = ~ form.
        on = ui.render_path(p, enabled=True)
        self.assertIn("\033]8;;file://" + os.path.abspath(p), on)
        self.assertIn(tilde, on)
        self.assertTrue(on.endswith("\033]8;;\033\\"))

    def test_render_path_empty_passthrough(self):
        self.assertEqual(ui.render_path("", enabled=True), "")

    def test_raw_start_banner_renders_tilde(self):
        home = os.path.expanduser("~")
        p = os.path.join(home, ".cowork", "sessions", "S", "scout.intel.json")
        tilde = os.path.join("~", ".cowork", "sessions", "S", "scout.intel.json")
        off = cowork.scout_start_text(p, enabled=False)
        self.assertIn(tilde, off)
        self.assertNotIn(os.path.join(home, ".cowork"), off)  # not the long path
        on = cowork.scout_start_text(p, enabled=True)
        self.assertIn("\033]8;;file://", on)
        self.assertIn(tilde, on)

    def test_stuck_gate_raw_banner_renders_tilde(self):
        home = os.path.expanduser("~")
        p = os.path.join(home, ".cowork", "sessions", "S", "builder.status.json")
        tilde = os.path.join("~", ".cowork", "sessions", "S", "builder.status.json")
        txt = cowork._stuck_gate_text(p, "builder", enabled=False)
        self.assertIn(tilde, txt)

    def test_review_done_banners_render_tilde(self):
        home = os.path.expanduser("~")
        p = os.path.join(home, ".cowork", "sessions", "S", "planner.plan.md")
        tilde = os.path.join("~", ".cowork", "sessions", "S", "planner.plan.md")
        self.assertIn(tilde, cowork.planner_review_text(p, enabled=False))
        self.assertIn(os.path.join("~", ".cowork", "b.json"),
                      cowork.builder_done_text(
                          os.path.join(home, ".cowork", "b.json"),
                          enabled=False))


class InternalLeadInTest(unittest.TestCase):
    """Item 2b: a surfaced internal block gets a faint lead-in gap on a TTY, and
    is a no-op (byte-identical) off a TTY — on BOTH controller render paths."""

    def test_lead_in_tty_emits_gap(self):
        out = FakeTTY()
        ui.internal_lead_in(out, True)
        v = out.getvalue()
        self.assertTrue(v.startswith("\n"))
        self.assertIn("─", v)

    def test_lead_in_off_tty_noop(self):
        out = io.StringIO()
        ui.internal_lead_in(out)  # auto-detects: not a TTY
        self.assertEqual(out.getvalue(), "")

    def test_streaming_internal_nontty_byte_identical(self):
        # The claude path off a TTY is unchanged (no gap) — historical contract.
        out = io.StringIO()
        with ui.StreamingMarkdown(out, "rev › ", internal=True) as r:
            r.feed("verdict notes")
        self.assertEqual(out.getvalue(), "\nrev › verdict notes\n")

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_streaming_internal_tty_has_lead_in(self):
        import unittest.mock as mock
        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with ui.StreamingMarkdown(out, "rev › ", internal=True) as r:
                r.feed("note\n")
        text = out.getvalue()
        self.assertIn("─", text)                       # lead-in rule present
        self.assertLess(text.index("─"), text.index("rev"))  # above the label

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_streaming_user_tty_has_no_lead_in(self):
        import unittest.mock as mock
        out = FakeTTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with ui.StreamingMarkdown(out, "scout › ") as r:
                r.feed("hi\n")
        head = out.getvalue().split("scout")[0]
        self.assertNotIn("─", head)                    # no gap for a user region

    def _codex_proc(self, lines):
        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass
        return FakeProc(lines)

    class _FakeSpin:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def stop(self):
            pass

        def set_label(self, _t):
            pass

    def test_codex_internal_nontty_no_gap(self):
        import unittest.mock as mock
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "verdict"}}),
        ]
        out = io.StringIO()
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=self._codex_proc(lines)), \
                mock.patch.object(bridge, "_Spinner", self._FakeSpin):
            s = bridge.CodexSession("implement", True, io_out=out,
                                    speaker="rev", internal=True)
            s.send("go")
        # Off a TTY there is no lead-in rule; the label is written plainly.
        self.assertNotIn("─", out.getvalue())
        self.assertIn("rev › ", out.getvalue())

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_codex_internal_tty_has_lead_in(self):
        import unittest.mock as mock
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "verdict"}}),
        ]
        out = FakeTTY()
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=self._codex_proc(lines)), \
                mock.patch.object(bridge, "_Spinner", self._FakeSpin), \
                mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            s = bridge.CodexSession("implement", True, io_out=out,
                                    speaker="rev", internal=True)
            s.send("go")
        text = out.getvalue()
        self.assertIn("─", text)                       # lead-in rule present
        self.assertLess(text.index("─"), text.index("rev"))  # above the label


class IsReviewFailureTest(unittest.TestCase):
    """Item 3: the failure predicate — no USABLE verdict, validated directly
    against the verdict contract."""

    def test_truth_table(self):
        F = cowork._is_review_failure
        # Failures: every no-usable-verdict mode.
        self.assertTrue(F(None))
        self.assertTrue(F({}))
        self.assertTrue(F({"foo": 1}))                       # no 'verdict' key
        self.assertTrue(F({"verdict": "maybe"}))             # unknown value
        self.assertTrue(F({"verdict": "needs_user"}))        # no question
        self.assertTrue(F({"verdict": "needs_user", "user_question": ""}))
        self.assertTrue(F({"verdict": "needs_user", "user_question": "   "}))
        self.assertTrue(F({"verdict": "revise", "malformed": True}))
        # Non-failures: a usable verdict.
        self.assertFalse(F({"verdict": "approve"}))
        self.assertFalse(F({"verdict": "revise"}))
        self.assertFalse(F({"verdict": "revise", "findings": ["x"]}))
        self.assertFalse(F({"verdict": "needs_user", "user_question": "which?"}))


class ReviewerFailureGateTest(unittest.TestCase):
    """Item 3: a reviewer/advisor that returns no usable verdict twice running
    surfaces the retry/skip-review/end gate (driven against the shared
    _role_loop, so all three paired reviewers inherit the behavior)."""

    def _path(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.json")

    def _trace(self, path):
        return trace_store.Trace(
            os.path.join(os.path.dirname(path), "trace.X.jsonl"),
            session_uuid="X", run_id="R")

    def _events(self, path):
        tpath = os.path.join(os.path.dirname(path), "trace.X.jsonl")
        with open(tpath, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _session(self, path, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    json.dump({"status": st}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def _review_fn(self, verdicts):
        calls = {"n": 0}

        def review_fn(_p, _round):
            calls["n"] += 1
            return verdicts.pop(0) if verdicts else None
        review_fn.calls = calls
        return review_fn

    def _run(self, path, statuses, review_fn, io_in, trace=None):
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            self._session(path, statuses), "seed", path, context="",
            io_in=io.StringIO(io_in), io_out=out, review_fn=review_fn,
            trace=trace)
        return rc, outcome, out.getvalue()

    def test_gate_fires_at_two_consecutive_failures_then_end(self):
        path = self._path()
        rfn = self._review_fn([])  # always None -> always a failure
        trace = self._trace(path)
        rc, outcome, out = self._run(
            path, ["ready_for_review"], rfn, "end\n", trace=trace)
        self.assertEqual((rc, outcome), (0, "ended"))
        # one silent auto-retry then the gate: exactly FAIL_CAP reviewer calls.
        self.assertEqual(rfn.calls["n"], cowork.REVIEW_FAIL_CAP)
        self.assertIn("could not return a usable verdict", out)
        ev = [e for e in self._events(path) if e["event"] == "review.failure"]
        self.assertEqual([e["consecutive"] for e in ev], [1, 2])

    def test_off_tty_default_is_skip_review(self):
        # Blank input at the gate -> skip-review (never trap a scripted run);
        # skip-review then reaches the user gate, which reads blank=approve.
        path = self._path()
        rfn = self._review_fn([])
        rc, outcome, out = self._run(path, ["ready_for_review"], rfn, "")
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(rfn.calls["n"], cowork.REVIEW_FAIL_CAP)
        self.assertIn("could not return a usable verdict", out)
        self.assertIn("scout finished", out)

    def test_retry_reruns_reviewer_counter_not_reset(self):
        path = self._path()
        rfn = self._review_fn([])  # never recovers
        trace = self._trace(path)
        # gate1 -> retry -> gate2 -> end. Retry does NOT consume the role; it
        # re-runs the reviewer in place.
        rc, outcome, out = self._run(
            path, ["ready_for_review"], rfn, "retry\nend\n", trace=trace)
        self.assertEqual((rc, outcome), (0, "ended"))
        # silent retry (2 calls) + the gate retry (1 more) = 3 reviewer calls.
        self.assertEqual(rfn.calls["n"], 3)
        # gate shown twice; the role was never bounced (only the seed sent).
        self.assertEqual(out.count("could not return a usable verdict"), 2)
        actions = [e["action"] for e in self._events(path)
                   if e["event"] == "user.action"]
        self.assertIn("review_fail_retry", actions)
        self.assertIn("review_fail_end", actions)

    def test_skip_review_is_sticky_and_reaches_user_gate(self):
        path = self._path()
        rfn = self._review_fn([])  # would fail if ever called again
        # gate -> skip -> user gate revises -> 2nd ready bypasses the reviewer
        # entirely -> user approves.
        rc, outcome, out = self._run(
            path, ["ready_for_review", "ready_for_review"], rfn,
            "skip\nchange this\n\n")
        self.assertEqual((rc, outcome), (0, "approved"))
        # reviewer ran only in round 1 (FAIL_CAP calls); round 2 bypassed it.
        self.assertEqual(rfn.calls["n"], cowork.REVIEW_FAIL_CAP)
        self.assertEqual(out.count("could not return a usable verdict"), 1)

    def test_legit_revise_never_trips_the_gate(self):
        path = self._path()
        rfn = self._review_fn([
            {"verdict": "revise", "findings": ["a"]},
            {"verdict": "revise", "findings": ["b"]},
            {"verdict": "approve"},
        ])
        rc, outcome, out = self._run(
            path, ["ready_for_review"] * 3, rfn, "")
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertNotIn("could not return a usable verdict", out)
        self.assertIn("reviewed: changes requested", out)

    def test_usable_verdict_resets_failure_counter(self):
        # A failure, then a usable revise (resets), then on the next round a
        # single failure must NOT immediately trip the gate — proving the
        # counter reset. Sequence: None, revise, None, approve.
        path = self._path()
        rfn = self._review_fn([
            None, {"verdict": "revise", "findings": ["x"]},
            None, {"verdict": "approve"},
        ])
        trace = self._trace(path)
        rc, outcome, out = self._run(
            path, ["ready_for_review"] * 2, rfn, "", trace=trace)
        self.assertEqual((rc, outcome), (0, "approved"))
        # The gate never fired: each round saw one failure (silent retry) then a
        # usable verdict; the counter reset, so 2 never accrued.
        self.assertNotIn("could not return a usable verdict", out)
        fails = [e["consecutive"] for e in self._events(path)
                 if e["event"] == "review.failure"]
        self.assertEqual(fails, [1, 1])  # never reached 2

    def test_tty_gate_select_skip_reaches_user_gate(self):
        # The TTY path wires questionary select -> skip-review -> user gate.
        import unittest.mock as mock
        path = self._path()
        rfn = self._review_fn([])
        out = FakeTTY()

        with mock.patch.object(cowork.ui, "select", return_value="skip-review"), \
                mock.patch.object(cowork, "_read_review",
                                  return_value=cowork._END):
            rc, outcome, _ = cowork._role_loop(
                self._session(path, ["ready_for_review"]), "seed", path,
                context="", io_in=FakeTTY(), io_out=out, review_fn=rfn)
        self.assertEqual((rc, outcome), (0, "approved"))
        self.assertEqual(rfn.calls["n"], cowork.REVIEW_FAIL_CAP)


class ScoutIntelMdHelperTest(unittest.TestCase):
    """scout.intel.md path helper + the scout-side md plumbing (brief, reviewer
    brief/context/resume, gate text surfaces)."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def test_scout_intel_md_path_helper(self):
        self.assertEqual(
            state_store.scout_intel_md_path_for(".cowork", "abc-123"),
            ".cowork/scout.intel.md")

    def test_scout_brief_declares_both_write_targets(self):
        brief = cowork.assemble_scout_brief(
            ["scout", "scout-reviewer"],
            "/s/scout.intel.json", "/s/scout.intel.md")
        self.assertIn("/s/scout.intel.json", brief)
        self.assertIn("/s/scout.intel.md", brief)
        self.assertIn("ONLY write target", brief)
        # both files, not a single one
        self.assertIn("two intel files", brief)
        self.assertIn("CONSISTENT", brief)

    def test_scout_brief_single_target_when_no_md(self):
        # back-compat: no md path -> the original single-target instruction
        brief = cowork.assemble_scout_brief(["scout"], "/s/scout.intel.json")
        self.assertIn("/s/scout.intel.json", brief)
        self.assertIn("ONLY write target", brief)
        self.assertNotIn("two intel files", brief)

    def test_reviewer_brief_protects_both_scout_files(self):
        brief = cowork.assemble_reviewer_brief(".cowork/scout-review.json")
        # the widened default names both files; the old substring still holds
        self.assertIn("Do NOT edit the scout intel", brief)
        self.assertIn("markdown", brief)

    def test_reviewer_context_embeds_both_intel_files(self):
        d = self._tmp()
        intel = os.path.join(d, "scout.intel.json")
        intel_md = os.path.join(d, "scout.intel.md")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"objective": "JSON-OBJECTIVE"}}, fh)
        with open(intel_md, "w") as fh:
            fh.write("# MD-RENDERING")
        ctx = cowork.assemble_reviewer_context(
            "the goal", ["scout", "scout-reviewer"], intel, intel_md)
        self.assertIn("JSON-OBJECTIVE", ctx)   # JSON embedded
        self.assertIn("MD-RENDERING", ctx)      # markdown embedded
        self.assertIn("CONSISTENT", ctx)        # the consistency instruction
        # without the md path, only the JSON is embedded (back-compat)
        ctx_json_only = cowork.assemble_reviewer_context(
            "the goal", ["scout"], intel)
        self.assertIn("JSON-OBJECTIVE", ctx_json_only)
        self.assertNotIn("MD-RENDERING", ctx_json_only)

    def test_reviewer_resume_context_embeds_both_intel_files(self):
        d = self._tmp()
        intel = os.path.join(d, "scout.intel.json")
        intel_md = os.path.join(d, "scout.intel.md")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review"}, fh)
        with open(intel_md, "w") as fh:
            fh.write("MD-RESUME-BODY")
        resumed = cowork.assemble_reviewer_resume_context(
            intel, intel_md, context_update="redirected goal")
        self.assertIn("<context>\nredirected goal\n</context>", resumed)
        self.assertIn("ready_for_review", resumed)   # JSON
        self.assertIn("MD-RESUME-BODY", resumed)      # markdown
        # back-compat: positional intel only still works (no md), no block
        plain = cowork.assemble_reviewer_resume_context(intel)
        self.assertIn("ready_for_review", plain)
        self.assertNotIn("MD-RESUME-BODY", plain)
        self.assertNotIn("<context>", plain)

    def test_scout_gate_text_points_at_md_when_wired(self):
        # _scout_loop repoints the review/done surfaces at the intel markdown.
        d = self._tmp()
        intel = os.path.join(d, "scout.intel.json")
        intel_md = os.path.join(d, "scout.intel.md")

        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                with open(intel, "w") as fh:
                    json.dump({"status": "ready_for_review"}, fh)

            def close(self):
                self.closed = True

        out = io.StringIO()
        rc = cowork._scout_loop(
            FakeSession(), "seed", intel, context="",
            io_in=io.StringIO(""), io_out=out, intel_md_path=intel_md)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("ready for review", text)
        self.assertIn("scout.intel.md", text)     # surfaces the markdown path
        self.assertIn("scout finished", text)


class BuilderSummaryMdHelperTest(unittest.TestCase):
    """builder.summary.md path helper + the builder-side md plumbing (brief,
    build-reviewer context/resume, gate text surfaces)."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def test_build_summary_path_helper(self):
        self.assertEqual(
            state_store.build_summary_path_for(".cowork", "abc"),
            ".cowork/builder.summary.md")

    def test_builder_brief_declares_summary_target(self):
        brief = cowork.assemble_builder_brief(
            ".cowork/builder.status.json", ".cowork/builder.summary.md")
        self.assertIn(".cowork/builder.status.json", brief)
        self.assertIn(".cowork/builder.summary.md", brief)
        self.assertIn("self-audit", brief)
        self.assertIn("CONSISTENT", brief)
        # still not a write restriction (whole repo is the write target)
        self.assertIn("NOT a restriction", brief)

    def test_builder_brief_no_summary_when_not_wired(self):
        brief = cowork.assemble_builder_brief(".cowork/builder.status.json")
        self.assertIn(".cowork/builder.status.json", brief)
        self.assertNotIn("builder.summary.md", brief)

    def test_build_reviewer_context_embeds_summary(self):
        d = self._tmp()
        pj = os.path.join(d, "plan.json")
        pm = os.path.join(d, "plan.md")
        status = os.path.join(d, "builder.status.json")
        summary = os.path.join(d, "builder.summary.md")
        for p, body in ((pj, '{"plan": "J"}'), (pm, "# MD PLAN"),
                        (status, '{"status": "ready_for_review"}'),
                        (summary, "# SUMMARY-BODY")):
            with open(p, "w") as fh:
                fh.write(body)
        ctx = cowork.assemble_build_reviewer_context(
            "goal", ["builder"], pj, pm, status, build_summary_path=summary)
        self.assertIn("SUMMARY-BODY", ctx)
        self.assertIn("consistency-check", ctx)
        # still embeds the rest + the diff recipe
        self.assertIn('"plan": "J"', ctx)
        self.assertIn("git status --porcelain", ctx)
        resumed = cowork.assemble_build_reviewer_resume_context(
            pj, pm, status, build_summary_path=summary)
        self.assertIn("SUMMARY-BODY", resumed)
        self.assertIn("git status --porcelain", resumed)
        # back-compat: without a summary path, none is embedded
        ctx_no = cowork.assemble_build_reviewer_context(
            "goal", ["builder"], pj, pm, status)
        self.assertNotIn("SUMMARY-BODY", ctx_no)

    def test_builder_gate_text_points_at_summary(self):
        # builder_review_text / builder_done_text are overridden in run_builder
        # to point at the summary path; assert the text producers render it.
        self.assertIn("builder.summary.md",
                      cowork.builder_review_text("/s/builder.summary.md"))
        self.assertIn("builder.summary.md",
                      cowork.builder_done_text("/s/builder.summary.md"))


class HashGateStateTest(unittest.TestCase):
    """cowork_state hash-gate primitives: scouting epoch, composite hash,
    baseline persistence, and skip eligibility (every negative path)."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, "session.json")

    def test_scouting_epoch_persisted_and_bumped(self):
        path = self._tmp()
        self.assertEqual(state_store.get_scouting_epoch(None), 0)
        self.assertEqual(state_store.get_scouting_epoch({}), 0)
        state = state_store.bump_scouting_epoch(path)
        self.assertEqual(state_store.get_scouting_epoch(state), 1)
        state = state_store.bump_scouting_epoch(path, prior=state)
        self.assertEqual(state_store.get_scouting_epoch(state), 2)

    def _write(self, path, body):
        with open(path, "wb") as fh:
            fh.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def test_composite_hash_order_and_byte_sensitivity(self):
        d = os.path.dirname(self._tmp())
        a = os.path.join(d, "a.json")
        b = os.path.join(d, "b.md")
        self._write(a, "AAA")
        self._write(b, "BBB")
        h1 = state_store.composite_artifact_hash([a, b])
        # order matters: swapping the member order changes the composite
        self.assertNotEqual(h1, state_store.composite_artifact_hash([b, a]))
        # stable when nothing changed
        self.assertEqual(h1, state_store.composite_artifact_hash([a, b]))
        # any byte change to any member changes the composite
        self._write(b, "BBBx")
        self.assertNotEqual(h1, state_store.composite_artifact_hash([a, b]))

    def test_composite_hash_missing_member_sentinel(self):
        d = os.path.dirname(self._tmp())
        a = os.path.join(d, "a.json")
        b = os.path.join(d, "b.md")
        self._write(a, "AAA")
        # b missing -> sentinel; differs from b present-but-empty
        missing = state_store.composite_artifact_hash([a, b])
        self._write(b, "")
        present_empty = state_store.composite_artifact_hash([a, b])
        self.assertNotEqual(missing, present_empty)

    def test_baseline_record_get_roundtrip(self):
        path = self._tmp()
        state = state_store.record_review_baseline(
            path, "scout-reviewer", 2, 3, "HASH-ABC")
        got = state_store.get_review_baseline(state, "scout-reviewer")
        self.assertEqual(got, {"epoch": 2, "context_revision": 3,
                               "hash": "HASH-ABC"})
        # persisted: reload from disk and it is still there
        reloaded = state_store.load(path)
        self.assertEqual(
            state_store.get_review_baseline(reloaded, "scout-reviewer"),
            {"epoch": 2, "context_revision": 3, "hash": "HASH-ABC"})
        # absent for a different reviewer
        self.assertIsNone(
            state_store.get_review_baseline(state, "planning-advisor"))
        self.assertIsNone(state_store.get_review_baseline(None, "scout-reviewer"))

    def _state_with_baseline(self, reviewer, epoch, ctx_rev, h, acked):
        return {
            "version": 1,
            "sessions": {
                reviewer: {
                    "last_approved_baseline": {
                        "epoch": epoch, "context_revision": ctx_rev, "hash": h},
                    "last_context_revision_seen": acked,
                }
            },
        }

    def test_skip_eligible_positive(self):
        st = self._state_with_baseline("scout-reviewer", 2, 3, "H", acked=3)
        self.assertTrue(state_store.review_skip_eligible(
            st, "scout-reviewer", 2, 3, "H"))

    def test_skip_not_eligible_no_baseline(self):
        self.assertFalse(state_store.review_skip_eligible(
            {}, "scout-reviewer", 0, 0, "H"))

    def test_skip_not_eligible_hash_mismatch(self):
        st = self._state_with_baseline("scout-reviewer", 2, 3, "H", acked=3)
        self.assertFalse(state_store.review_skip_eligible(
            st, "scout-reviewer", 2, 3, "DIFFERENT"))

    def test_skip_not_eligible_epoch_mismatch(self):
        st = self._state_with_baseline("scout-reviewer", 2, 3, "H", acked=3)
        # a phase re-entry bumped the epoch -> the stale baseline cannot skip
        self.assertFalse(state_store.review_skip_eligible(
            st, "scout-reviewer", 3, 3, "H"))

    def test_skip_not_eligible_unacked_newer_context(self):
        # byte-identical, same epoch, but a newer context revision arrived that
        # the reviewer never acked -> must NOT skip (a skip can't absorb context)
        st = self._state_with_baseline("scout-reviewer", 2, 3, "H", acked=3)
        self.assertFalse(state_store.review_skip_eligible(
            st, "scout-reviewer", 2, 4, "H"))

    def test_skip_not_eligible_acked_disagrees_with_baseline(self):
        # the reviewer's acked revision no longer matches the baseline's recorded
        # revision -> not the same approval authority -> no skip
        st = self._state_with_baseline("scout-reviewer", 2, 3, "H", acked=2)
        self.assertFalse(state_store.review_skip_eligible(
            st, "scout-reviewer", 2, 3, "H"))


class ReviewerHashGateLoopTest(unittest.TestCase):
    """The hash-gate wired into the shared lead loop via `_scout_loop`: a
    byte-identical, already-approved artifact skips the reviewer; any change (or
    a non-approve prior verdict) re-reviews; the baseline survives a resume and
    a clobbering lead-ack / phase-save."""

    def _setup(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        spath = os.path.join(d, "session.json")
        state_store.save(spath, {"team": ["scout", "scout-reviewer"],
                                 "config": {}, "sessions": {}})
        intel = os.path.join(d, "scout.intel.json")
        intel_md = os.path.join(d, "scout.intel.md")
        with open(intel_md, "w") as fh:
            fh.write("# intel markdown v1")
        return spath, intel, intel_md

    def _session(self, intel, status="ready_for_review"):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                with open(intel, "w") as fh:
                    json.dump({"status": status}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def _review_fn(self, verdicts):
        calls = {"n": 0}

        def review_fn(intel_path, round_index):
            calls["n"] += 1
            return verdicts.pop(0) if verdicts else {"verdict": "approve"}
        review_fn.calls = calls
        return review_fn

    def _bundle(self, spath, covered, epoch=0, current_rev=0,
                reviewer_role="scout-reviewer"):
        # Mirrors run_flow.make_skip_baseline: closures over an in-memory holder
        # threaded as `prior`, so record() updates state in place.
        holder = {"state": state_store.load(spath)}
        epoch_box = {"epoch": epoch}

        def compute():
            return state_store.composite_artifact_hash(covered)

        def eligible(h):
            return state_store.review_skip_eligible(
                holder["state"], reviewer_role, epoch_box["epoch"],
                current_rev, h)

        def record(h):
            holder["state"] = state_store.record_review_baseline(
                spath, reviewer_role, epoch_box["epoch"], current_rev, h,
                prior=holder["state"])
        return cowork.SkipBaseline(compute, eligible, record), holder

    def _run(self, spath, intel, intel_md, review_fn, bundle,
             user_in="", evaluate_fn=None):
        out = io.StringIO()
        rc = cowork._scout_loop(
            self._session(intel), "seed", intel, context="",
            io_in=io.StringIO(user_in), io_out=out, review_fn=review_fn,
            intel_md_path=intel_md, skip_baseline=bundle,
            evaluate_fn=evaluate_fn)
        return rc, out.getvalue()

    def test_skip_on_identical_after_approve(self):
        spath, intel, intel_md = self._setup()
        covered = [intel, intel_md]
        # Round 1: reviewer approves -> baseline seeded.
        b1, _ = self._bundle(spath, covered)
        rfn1 = self._review_fn([{"verdict": "approve"}])
        rc, text1 = self._run(spath, intel, intel_md, rfn1, b1)
        self.assertEqual(rc, 0)
        self.assertEqual(rfn1.calls["n"], 1)
        self.assertIn("reviewed: approved", text1)
        # baseline landed on disk
        self.assertIsNotNone(state_store.get_review_baseline(
            state_store.load(spath), "scout-reviewer"))
        # Round 2 (fresh bundle = resume): identical bytes -> reviewer SKIPPED.
        b2, _ = self._bundle(spath, covered)
        rfn2 = self._review_fn([{"verdict": "approve"}])
        rc, text2 = self._run(spath, intel, intel_md, rfn2, b2)
        self.assertEqual(rc, 0)
        self.assertEqual(rfn2.calls["n"], 0)             # reviewer NOT called
        self.assertIn("review skipped", text2)            # visible marker
        self.assertNotIn("reviewed: approved", text2)     # no reviewer marker
        self.assertIn("scout finished", text2)            # user gate -> approved

    def test_no_skip_when_md_member_changed(self):
        spath, intel, intel_md = self._setup()
        covered = [intel, intel_md]
        b1, _ = self._bundle(spath, covered)
        rc, _ = self._run(spath, intel, intel_md,
                          self._review_fn([{"verdict": "approve"}]), b1)
        self.assertEqual(rc, 0)
        # change ONLY the markdown member -> composite differs -> re-review
        with open(intel_md, "w") as fh:
            fh.write("# intel markdown v2 (edited)")
        b2, _ = self._bundle(spath, covered)
        rfn2 = self._review_fn([{"verdict": "approve"}])
        rc, text2 = self._run(spath, intel, intel_md, rfn2, b2)
        self.assertEqual(rc, 0)
        self.assertEqual(rfn2.calls["n"], 1)              # reviewer DID run
        self.assertNotIn("review skipped", text2)

    def test_no_skip_when_prior_verdict_not_approve(self):
        spath, intel, intel_md = self._setup()
        covered = [intel, intel_md]
        # A revise that rides to the cap: the user approves at the dissent gate,
        # but the reviewer never returned `approve`, so NO baseline is seeded.
        cap = cowork.REVIEW_ROUND_CAP
        b1, holder1 = self._bundle(spath, covered)
        rfn1 = self._review_fn(
            [{"verdict": "revise", "findings": ["x"]} for _ in range(cap + 1)])
        # _session writes ready_for_review on each send; the revise loop bounces
        # the role, so allow many statuses by reusing the default.
        rc, _ = self._run(spath, intel, intel_md, rfn1, b1)
        self.assertEqual(rc, 0)
        self.assertIsNone(state_store.get_review_baseline(
            state_store.load(spath), "scout-reviewer"))
        # Next round, unchanged bytes: still NO skip (no approved baseline).
        b2, _ = self._bundle(spath, covered)
        rfn2 = self._review_fn([{"verdict": "approve"}])
        rc, text2 = self._run(spath, intel, intel_md, rfn2, b2)
        self.assertEqual(rc, 0)
        self.assertEqual(rfn2.calls["n"], 1)              # reviewer ran
        self.assertNotIn("review skipped", text2)

    def test_no_skip_after_epoch_bump(self):
        spath, intel, intel_md = self._setup()
        covered = [intel, intel_md]
        b1, _ = self._bundle(spath, covered, epoch=0)
        self._run(spath, intel, intel_md,
                  self._review_fn([{"verdict": "approve"}]), b1)
        # a planner -> scout hand-back bumps the scouting epoch
        b2, _ = self._bundle(spath, covered, epoch=1)
        rfn2 = self._review_fn([{"verdict": "approve"}])
        rc, text2 = self._run(spath, intel, intel_md, rfn2, b2)
        self.assertEqual(rc, 0)
        self.assertEqual(rfn2.calls["n"], 1)              # epoch moved -> review
        self.assertNotIn("review skipped", text2)

    def test_skipped_round_runs_no_eval(self):
        spath, intel, intel_md = self._setup()
        covered = [intel, intel_md]
        evals = {"n": 0}

        def evaluate_fn(session, verdict, round_index):
            evals["n"] += 1

        b1, _ = self._bundle(spath, covered)
        self._run(spath, intel, intel_md,
                  self._review_fn([{"verdict": "approve"}]), b1,
                  evaluate_fn=evaluate_fn)
        self.assertEqual(evals["n"], 1)                   # round 1 scored
        # Round 2 skips the reviewer -> no eval turn for the skipped round.
        b2, _ = self._bundle(spath, covered)
        rfn2 = self._review_fn([{"verdict": "approve"}])
        self._run(spath, intel, intel_md, rfn2, b2, evaluate_fn=evaluate_fn)
        self.assertEqual(rfn2.calls["n"], 0)
        self.assertEqual(evals["n"], 1)                   # unchanged: no new eval

    def test_baseline_survives_clobbering_lead_ack_and_phase_save(self):
        # REGRESSION: record() updates the in-memory holder in place, so a later
        # mark_context_seen / save_phase that threads that same holder does NOT
        # overwrite the freshly written baseline.
        spath, intel, intel_md = self._setup()
        covered = [intel, intel_md]
        b1, holder = self._bundle(spath, covered)
        rc, _ = self._run(spath, intel, intel_md,
                         self._review_fn([{"verdict": "approve"}]), b1)
        self.assertEqual(rc, 0)
        # holder['state'] now carries the baseline (record reassigned it).
        self.assertIsNotNone(state_store.get_review_baseline(
            holder["state"], "scout-reviewer"))
        # Simulate run_flow's post-loop saves, threading the SAME holder.
        holder["state"] = state_store.mark_context_seen(
            spath, "scout", 0, prior=holder["state"])
        holder["state"] = state_store.save_phase(
            spath, "planning", prior=holder["state"])
        # On disk, the baseline is still present and still qualifies a skip.
        reloaded = state_store.load(spath)
        self.assertIsNotNone(state_store.get_review_baseline(
            reloaded, "scout-reviewer"))
        self.assertTrue(state_store.review_skip_eligible(
            reloaded, "scout-reviewer", 0, 0,
            state_store.composite_artifact_hash(covered)))


class RunLevelGateSurfaceTest(unittest.TestCase):
    """The .md review surfaces must reach the user at the RUN level (run_scout /
    run_builder), not only inside the loop helper — the start banner included."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def _codex_session(self, status_path):
        class FakeSession:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                with open(status_path, "w") as fh:
                    json.dump({"status": "ready_for_review", "result": {}}, fh)

            def close(self):
                pass
        return FakeSession()

    def test_run_scout_start_banner_points_at_intel_md(self):
        d = self._tmp()
        intel = os.path.join(d, "scout.intel.json")
        intel_md = os.path.join(d, "scout.intel.md")
        sess = self._codex_session(intel)
        config = cowork.default_config(["scout"])
        config["scout"]["controller"] = "codex"
        out = io.StringIO()
        rc = cowork.run_scout(
            config, "seed", ["scout"], io_in=io.StringIO(""), io_out=out,
            intel_path=intel, intel_md_path=intel_md,
            session_factory=lambda *a, **k: sess)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        # the start banner (and the review/done gates) surface the markdown
        self.assertIn("scout.intel.md", text)
        self.assertIn("scout — gathering context", text)   # the start banner ran
        self.assertNotIn("scout.intel.json", text)         # not the raw JSON

    def test_run_builder_gate_surfaces_point_at_summary_md(self):
        d = self._tmp()
        status = os.path.join(d, "builder.status.json")
        summary = os.path.join(d, "builder.summary.md")
        pj = os.path.join(d, "plan.json")
        pm = os.path.join(d, "plan.md")
        for p, body in ((pj, "{}"), (pm, "# PLAN")):
            with open(p, "w") as fh:
                fh.write(body)
        sess = self._codex_session(status)
        config = cowork.default_config(["builder"])
        config["builder"]["controller"] = "codex"
        out = io.StringIO()
        rc = cowork.run_builder(
            config, "seed", ["builder"], io_in=io.StringIO(""), io_out=out,
            build_status_path=status, build_summary_path=summary,
            plan_json_path=pj, plan_md_path=pm,
            session_factory=lambda *a, **k: sess,
            on_outcome=lambda o, p: None)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        # start banner + ready-for-review gate + done gate all point at summary.md
        self.assertIn("builder.summary.md", text)
        self.assertIn("build ready for review", text)
        self.assertIn("builder finished", text)
        self.assertNotIn("builder.status.json", text)      # not the status JSON


class PlannerHashGateRunTest(unittest.TestCase):
    """The hash-gate at the run level for the PLANNER / planning-advisor pairing
    over its own composite [planner.plan.json, planner.plan.md] — the planner
    bundle is constructed separately from the scout's, so it gets its own
    skip / no-skip coverage."""

    def _setup(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        spath = os.path.join(d, "session.json")
        state_store.save(spath, {"team": ["planner", "planning-advisor"],
                                 "config": {}, "sessions": {}})
        plan_json = os.path.join(d, "planner.plan.json")
        plan_md = os.path.join(d, "planner.plan.md")
        with open(plan_md, "w") as fh:
            fh.write("# plan markdown v1")
        review = os.path.join(d, "planner-review.json")
        return spath, plan_json, plan_md, review

    def _session(self, plan_json):
        class FakeSession:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                with open(plan_json, "w") as fh:
                    json.dump({"session": "X", "role": "planner",
                               "status": "ready_for_review", "result": {}}, fh)

            def close(self):
                pass
        return FakeSession()

    def _bundle(self, spath, covered, epoch=0, current_rev=0):
        holder = {"state": state_store.load(spath)}
        epoch_box = {"epoch": epoch}

        def compute():
            return state_store.composite_artifact_hash(covered)

        def eligible(h):
            return state_store.review_skip_eligible(
                holder["state"], "planning-advisor", epoch_box["epoch"],
                current_rev, h)

        def record(h):
            holder["state"] = state_store.record_review_baseline(
                spath, "planning-advisor", epoch_box["epoch"], current_rev, h,
                prior=holder["state"])
        return cowork.SkipBaseline(compute, eligible, record)

    def _run(self, spath, plan_json, plan_md, review, bundle, runner):
        out = io.StringIO()
        config = cowork.default_config(["planner", "planning-advisor"])
        config["planner"]["controller"] = "codex"
        rc = cowork.run_planner(
            config, "seed", ["planner", "planning-advisor"],
            io_in=io.StringIO(""), io_out=out,
            plan_json_path=plan_json, plan_md_path=plan_md, review_path=review,
            session_factory=lambda *a, **k: self._session(plan_json),
            reviewer_runner=runner, skip_baseline=bundle,
            on_outcome=lambda o, p: None)
        return rc, out.getvalue()

    def _runner(self, calls):
        def runner(config, context, selected, p, review_path, **kw):
            calls.append(p)
            return {"verdict": "approve"}
        return runner

    def test_planner_skip_on_identical_then_no_skip_on_md_change(self):
        spath, plan_json, plan_md, review = self._setup()
        covered = [plan_json, plan_md]
        # Round 1: advisor approves -> planner baseline seeded.
        calls1 = []
        rc, text1 = self._run(spath, plan_json, plan_md, review,
                              self._bundle(spath, covered), self._runner(calls1))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls1), 1)                 # advisor ran
        self.assertIsNotNone(state_store.get_review_baseline(
            state_store.load(spath), "planning-advisor"))
        # Round 2 (resume): identical composite -> advisor SKIPPED.
        calls2 = []
        rc, text2 = self._run(spath, plan_json, plan_md, review,
                              self._bundle(spath, covered), self._runner(calls2))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls2), 0)                 # advisor NOT called
        self.assertIn("review skipped", text2)
        self.assertIn("planner finished", text2)         # approval reused
        # Round 3: a plan.md-only edit -> composite differs -> advisor re-runs.
        with open(plan_md, "w") as fh:
            fh.write("# plan markdown v2 (edited)")
        calls3 = []
        rc, text3 = self._run(spath, plan_json, plan_md, review,
                              self._bundle(spath, covered), self._runner(calls3))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls3), 1)                 # advisor ran again
        self.assertNotIn("review skipped", text3)


# --------------------------------------------------------------------------- #
# Token-reduction items #1–#4 (.plans/cowork-token-reduction.md).             #
# --------------------------------------------------------------------------- #

import cowork_report  # noqa: E402
import cowork_probe_cache as probe_cache  # noqa: E402
import cowork_diffpacket as diffpacket  # noqa: E402


def _claude_lines(usage=None):
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "ok"}]}})]
    result = {"type": "result", "subtype": "success", "session_id": "S1"}
    if usage is not None:
        result["usage"] = usage
    lines.append(json.dumps(result))
    return lines


class _ClaudeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.stdin = io.StringIO()

    def wait(self):
        return 0


class PromptAccountingTest(unittest.TestCase):
    """#1: per-turn accounting is additive + content-free (T1, T2, T13)."""

    def _tmp_trace(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, "trace.jsonl")

    def _events(self, path):
        with open(path, "r") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_t2_usage_extractor_best_effort(self):
        # result with usage -> populated; without -> None; never raises.
        self.assertEqual(
            bridge._usage_from_result(
                {"usage": {"input_tokens": 7, "output_tokens": 3,
                           "ignored": "x", "flag": True}}),
            {"input_tokens": 7, "output_tokens": 3})
        self.assertIsNone(bridge._usage_from_result({}))
        self.assertIsNone(bridge._usage_from_result({"usage": "nope"}))

    def test_t1_t13_turn_start_enriched_and_content_free(self):
        import unittest.mock as mock
        path = self._tmp_trace()
        trace = trace_store.Trace(path, session_uuid="X", run_id="R")
        secret = "SECRET-PROMPT-BODY-do-not-store"
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=_ClaudeProc(_claude_lines(
                                   usage={"input_tokens": 9}))):
            sess = bridge.ClaudeSession(
                "roles/scout.md", "plan", True, io_out=io.StringIO(),
                speaker="scout-reviewer", session_id="S1", trace=trace)
            sess.send(secret, meta={
                "prompt_kind": "reviewer_pass", "fresh": True,
                "context_revision": 2,
                "artifacts": [{"path": "intel.json", "bytes": 4,
                               "sha256": "abc"}],
                "dropme": None})
        events = self._events(path)
        start = [e for e in events
                 if e["event"] == "controller.turn.start"][0]
        self.assertEqual(start["prompt_kind"], "reviewer_pass")
        self.assertEqual(start["role"], "scout-reviewer")
        self.assertEqual(start["controller"], "claude")
        self.assertTrue(start["fresh"])
        self.assertEqual(start["context_revision"], 2)
        self.assertEqual(start["artifacts"][0]["path"], "intel.json")
        self.assertIn("prompt_sha256", start)
        # None field dropped; raw body never written anywhere in the trace.
        self.assertNotIn("dropme", start)
        self.assertNotIn(secret, json.dumps(self._events(path)))
        # Best-effort usage rides controller.turn.end.
        end = [e for e in events if e["event"] == "controller.turn.end"][0]
        self.assertEqual(end["usage"], {"input_tokens": 9})

    def test_probe_events_carry_prompt_kind_and_usage(self):
        # #1: probe start/end carry prompt_kind='probe'; end carries best-effort
        # usage from the probe-loop result event when present.
        path = self._tmp_trace()
        trace = trace_store.Trace(path, session_uuid="X", run_id="R")

        def spawn(cmd, stdin):
            return [{"type": "result", "subtype": "success",
                     "usage": {"input_tokens": 11, "output_tokens": 2}}]
        ok, _ = bridge.probe_claude_stream_json(spawn, trace=trace)
        self.assertTrue(ok)
        events = self._events(path)
        start = [e for e in events
                 if e["event"] == "controller.probe.start"][0]
        end = [e for e in events if e["event"] == "controller.probe.end"][0]
        self.assertEqual(start["prompt_kind"], "probe")
        self.assertEqual(end["prompt_kind"], "probe")
        self.assertEqual(end["usage"], {"input_tokens": 11, "output_tokens": 2})

    def test_probe_end_usage_absent_when_not_exposed(self):
        path = self._tmp_trace()
        trace = trace_store.Trace(path, session_uuid="X", run_id="R")

        def spawn(cmd, stdin):
            return [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "pong"}]}}]
        bridge.probe_claude_stream_json(spawn, trace=trace)
        end = [e for e in self._events(path)
               if e["event"] == "controller.probe.end"][0]
        self.assertNotIn("usage", end)  # None-valued field dropped

    def test_probe_usage_captured_when_assistant_precedes_result(self):
        # #1 (review fix): the probe must scan to the result to capture usage
        # even when an assistant event is emitted first.
        path = self._tmp_trace()
        trace = trace_store.Trace(path, session_uuid="X", run_id="R")

        def spawn(cmd, stdin):
            return [
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "pong"}]}},
                {"type": "result", "subtype": "success",
                 "usage": {"input_tokens": 8}},
            ]
        ok, _ = bridge.probe_claude_stream_json(spawn, trace=trace)
        self.assertTrue(ok)
        end = [e for e in self._events(path)
               if e["event"] == "controller.probe.end"][0]
        self.assertEqual(end["usage"], {"input_tokens": 8})

    def test_t1_meta_never_collides_with_bridge_kwargs(self):
        # meta carrying role/controller must not raise a duplicate-kwarg error.
        import unittest.mock as mock
        path = self._tmp_trace()
        trace = trace_store.Trace(path, session_uuid="X", run_id="R")
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=_ClaudeProc(_claude_lines())):
            sess = bridge.ClaudeSession(
                "roles/scout.md", "plan", True, io_out=io.StringIO(),
                speaker="scout", session_id="S1", trace=trace)
            sess.send("hi", meta={"role": "scout", "controller": "claude",
                                  "prompt_kind": "role_seed"})
        start = [e for e in self._events(path)
                 if e["event"] == "controller.turn.start"][0]
        self.assertEqual(start["prompt_kind"], "role_seed")


class ReportTest(unittest.TestCase):
    """#2: trace aggregation + the --report flag (T3, T4)."""

    def _synthetic(self):
        return [
            {"event": "controller.turn.start", "role": "scout",
             "controller": "claude", "prompt_kind": "role_seed",
             "prompt_bytes": 100, "fresh": True,
             "artifacts": [{"path": "a.json", "bytes": 40}]},
            {"event": "controller.turn.start", "role": "scout-reviewer",
             "controller": "codex", "prompt_kind": "reviewer_pass",
             "prompt_bytes": 300, "resume": True, "round": 2,
             "artifacts": [{"path": "a.json", "bytes": 40}]},
            {"event": "controller.turn.end", "controller": "codex",
             "usage": {"input_tokens": 50, "output_tokens": 5}},
            {"event": "review.skipped", "role": "scout-reviewer",
             "reason": "unchanged_since_approved"},
            "this is not json and must be skipped",
            "{bad json",
        ]

    def test_t3_aggregation(self):
        s = cowork_report.summarize_trace(self._synthetic())
        self.assertEqual(s["turn_count"], 2)
        self.assertEqual(s["bytes_by_role_controller"][("scout", "claude")], 100)
        self.assertEqual(
            s["bytes_by_role_controller"][("scout-reviewer", "codex")], 300)
        self.assertEqual(s["bytes_by_kind"]["reviewer_pass"], 300)
        self.assertEqual(s["fresh_resume"], {"fresh": 1, "resume": 1,
                                             "unknown": 0})
        self.assertEqual(s["largest_prompts"][0][0], 300)
        self.assertEqual(s["artifact_bytes"]["a.json"]["bytes"], 80)
        self.assertEqual(s["artifact_bytes"]["a.json"]["turns"], 2)
        self.assertEqual(len(s["review_skips"]), 1)
        self.assertEqual(s["usage_by_controller"]["codex"]["input_tokens"], 50)

    def test_t3_render_has_sections(self):
        text = cowork_report.render_report(
            cowork_report.summarize_trace(self._synthetic()), "UUID")
        for needle in ("Prompt bytes by role", "Prompt bytes by prompt kind",
                       "Largest single prompts", "Artifact contribution",
                       "Artifact delivery breakdown",
                       "Role/system-prompt bytes by role",
                       "Review-skip hits", "Controller-reported usage"):
            self.assertIn(needle, text)

    def test_probe_end_usage_is_summarized(self):
        # #2 (review fix): usage on controller.probe.end is aggregated too, not
        # only controller.turn.end.
        s = cowork_report.summarize_trace([
            {"event": "controller.probe.end", "controller": "claude",
             "result": "ok", "usage": {"input_tokens": 5, "output_tokens": 1}},
            {"event": "controller.turn.end", "controller": "claude",
             "usage": {"input_tokens": 10}},
        ])
        self.assertEqual(s["usage_by_controller"]["claude"]["input_tokens"], 15)
        self.assertEqual(s["usage_by_controller"]["claude"]["output_tokens"], 1)

    def test_t3_malformed_lines_never_raise(self):
        # A trace of pure garbage yields an empty (no-turns) report.
        text = cowork_report.render_report(
            cowork_report.summarize_trace(["x", "{", "[}"]))
        self.assertIn("No controller turns", text)

    def test_t4_report_flag_end_to_end(self):
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        try:
            uuid = "11111111-2222-3333-4444-555555555555"
            tpath = trace_store.trace_path_for(uuid)
            os.makedirs(os.path.dirname(tpath), exist_ok=True)
            with open(tpath, "w") as fh:
                for ev in self._synthetic():
                    fh.write((json.dumps(ev) if isinstance(ev, dict) else ev)
                             + "\n")
            args = cowork.build_parser().parse_args(["--report", uuid])
            out = io.StringIO()
            rc = cowork.run_report(args, io_out=out)
            self.assertEqual(rc, 0)
            self.assertIn("Prompt bytes by prompt kind", out.getvalue())
            self.assertIn(uuid, out.getvalue())
            # Unknown session -> a clean exit 1, no crash.
            args2 = cowork.build_parser().parse_args(["--report", "no-such"])
            out2 = io.StringIO()
            self.assertEqual(cowork.run_report(args2, io_out=out2), 1)
            self.assertIn("no trace", out2.getvalue())
        finally:
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old


class DeliveryAccountingTest(unittest.TestCase):
    """#3/#4: honest byte accounting — delivery tags, embedded-vs-touched, the
    role/system-prompt bytes section."""

    def _file(self, body):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        p = os.path.join(d, "art.json")
        with open(p, "w") as fh:
            fh.write(body)
        return p

    def test_descriptors_tag_delivery_and_embedded_bytes(self):
        p = self._file('{"k": "x" }' + " " * 200)  # a sizable body
        full = os.path.getsize(p)
        # embedded delivery (legacy default): embedded_bytes == full body bytes.
        emb = cowork._artifact_descriptors([p])
        self.assertEqual(emb[0]["delivery"], "embedded")
        self.assertEqual(emb[0]["embedded_bytes"], full)
        self.assertEqual(emb[0]["bytes"], full)
        # path delivery: body NOT embedded -> 0 embedded bytes, full touched.
        path = cowork._artifact_descriptors([p], delivery="path")
        self.assertEqual(path[0]["delivery"], "path")
        self.assertEqual(path[0]["embedded_bytes"], 0)
        self.assertEqual(path[0]["bytes"], full)
        # explicit per-path embedded map wins (e.g. a diff chunk size).
        diff = cowork._artifact_descriptors(
            [p], delivery="diff", embedded={p: 17})
        self.assertEqual(diff[0]["delivery"], "diff")
        self.assertEqual(diff[0]["embedded_bytes"], 17)

    def test_full_reread_packet_is_path_delivery_small_embedded(self):
        p = self._file('{"finding": "SECRET-BODY"}' + " " * 500)
        pkt = diffpacket.build_full_reread_packet(
            [{"label": "intel", "path": p, "kind": "json"}])
        self.assertEqual(pkt.delivery, "path")
        self.assertNotIn("SECRET-BODY", pkt)            # body not embedded
        self.assertIn(p, pkt)                           # path referenced
        # embedded bytes ~ the descriptor line, far below the full body.
        self.assertIn(p, pkt.embedded)
        self.assertLess(pkt.embedded[p], os.path.getsize(p))

    def test_report_path_delivery_excludes_embedded_total(self):
        # A path-delivered artifact contributes its FULL size to "touched" but
        # 0 to the embedded total; an embedded one contributes its full body.
        s = cowork_report.summarize_trace([
            {"event": "controller.turn.start", "role": "build-reviewer",
             "controller": "claude", "prompt_kind": "reviewer_pass",
             "prompt_bytes": 200, "resume": True,
             "artifacts": [{"path": "plan.json", "bytes": 900,
                            "delivery": "path", "embedded_bytes": 0}]},
            {"event": "controller.turn.start", "role": "scout",
             "controller": "codex", "prompt_kind": "role_seed",
             "prompt_bytes": 100, "fresh": True,
             "artifacts": [{"path": "intel.json", "bytes": 300,
                            "delivery": "embedded", "embedded_bytes": 300}]},
        ])
        self.assertEqual(s["artifact_bytes"]["plan.json"]["bytes"], 900)
        self.assertEqual(s["artifact_bytes"]["plan.json"]["embedded"], 0)
        self.assertEqual(s["delivery_breakdown"]["path"]["touched"], 900)
        self.assertEqual(s["delivery_breakdown"]["path"]["embedded"], 0)
        self.assertEqual(s["delivery_breakdown"]["embedded"]["embedded"], 300)
        text = cowork_report.render_report(s)
        self.assertIn("touched 900 B", text)
        self.assertIn("embedded 0 B", text)

    def test_report_role_prompt_bytes_section(self):
        s = cowork_report.summarize_trace([
            {"event": "role.prompt.bytes", "role": "planner",
             "bytes": 12000, "delivery": "codex_inline"},
            {"event": "role.prompt.bytes", "role": "scout-reviewer",
             "bytes": 9000, "delivery": "claude_system"},
            {"event": "role.prompt.bytes", "role": "planner",
             "bytes": 12000, "delivery": "codex_inline"},
            {"event": "controller.turn.start", "role": "planner",
             "controller": "codex", "prompt_kind": "role_seed",
             "prompt_bytes": 500, "fresh": True},
        ])
        rp = s["role_prompt_bytes"]
        self.assertEqual(rp[("planner", "codex_inline")]["bytes"], 24000)
        self.assertEqual(rp[("planner", "codex_inline")]["launches"], 2)
        self.assertEqual(rp[("scout-reviewer", "claude_system")]["bytes"], 9000)
        text = cowork_report.render_report(s)
        self.assertIn("Role/system-prompt bytes by role", text)
        self.assertIn("codex_inline", text)
        self.assertIn("claude_system", text)


class ProbeCacheTest(unittest.TestCase):
    """#3: global probe cache — hit/miss/re-probe/corrupt/failure (T5, T6)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.cache = os.path.join(self.dir, "probe_cache.json")
        # A role-prompt file with a stable hash component.
        self.role = os.path.join(self.dir, "role.md")
        with open(self.role, "w") as fh:
            fh.write("ROLE PROMPT")

    def _ok_spawn(self, calls):
        def spawn(cmd, stdin):
            calls.append(cmd)
            return [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "pong"}]}}]
        return spawn

    def test_t5_miss_stores_then_hit_skips_spawn(self):
        calls = []
        # Miss: live probe runs and stores on success.
        ok, _ = bridge.probe_claude_stream_json(
            self._ok_spawn(calls), role_prompt_file=self.role,
            cache_enabled=True, version_fn=lambda p: "claude 1.2.3",
            cache_path=self.cache)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        # Hit: T6 — spawn (a spy that fails if called) is NOT invoked.
        def boom(cmd, stdin):
            raise AssertionError("spawn must not run on a cache hit")
        ok2, alert2 = bridge.probe_claude_stream_json(
            boom, role_prompt_file=self.role, cache_enabled=True,
            version_fn=lambda p: "claude 1.2.3", cache_path=self.cache)
        self.assertTrue(ok2)
        self.assertIsNone(alert2)

    def test_t5_key_change_reprobes(self):
        calls = []
        common = dict(role_prompt_file=self.role, cache_enabled=True,
                      cache_path=self.cache)
        bridge.probe_claude_stream_json(
            self._ok_spawn(calls), version_fn=lambda p: "v1", **common)
        # New version -> miss -> re-probe (live spawn runs again).
        bridge.probe_claude_stream_json(
            self._ok_spawn(calls), version_fn=lambda p: "v2", **common)
        self.assertEqual(len(calls), 2)
        # New yolo shape -> miss again.
        bridge.probe_claude_stream_json(
            self._ok_spawn(calls), version_fn=lambda p: "v2", yolo=False,
            role_prompt_file=self.role, cache_enabled=True,
            cache_path=self.cache)
        self.assertEqual(len(calls), 3)

    def test_t5_corrupt_cache_is_a_miss(self):
        with open(self.cache, "w") as fh:
            fh.write("{not json")
        calls = []
        ok, _ = bridge.probe_claude_stream_json(
            self._ok_spawn(calls), role_prompt_file=self.role,
            cache_enabled=True, version_fn=lambda p: "v1",
            cache_path=self.cache)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)  # treated as miss -> probed

    def test_t5_probe_failure_not_stored(self):
        def bad_spawn(cmd, stdin):
            return [{"type": "other"}]  # unsupported -> failure
        ok, _ = bridge.probe_claude_stream_json(
            bad_spawn, role_prompt_file=self.role, cache_enabled=True,
            version_fn=lambda p: "v1", cache_path=self.cache)
        self.assertFalse(ok)
        key = probe_cache.probe_cache_key(
            "claude", "v1", self.role, "plan", True, False)
        self.assertFalse(probe_cache.cache_hit(key, path=self.cache))

    def test_resolved_binary_path_is_part_of_key(self):
        # D4: a different resolved claude binary (same version) must not hit.
        k1 = probe_cache.probe_cache_key(
            "/usr/bin/claude", "v1", self.role, "plan", True, False)
        k2 = probe_cache.probe_cache_key(
            "/opt/local/bin/claude", "v1", self.role, "plan", True, False)
        self.assertNotEqual(k1, k2)

    def test_resolve_claude_path_realpaths_absolute(self):
        exe = os.path.join(self.dir, "claude")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\n")
        self.assertEqual(probe_cache.resolve_claude_path([exe]),
                         os.path.realpath(exe))

    def test_default_cache_path_is_cowork_home(self):
        old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        old_cache = os.environ.get("COWORK_PROBE_CACHE")
        os.environ.pop("COWORK_PROBE_CACHE", None)
        os.environ["COWORK_SESSIONS_ROOT"] = "/x/y/sessions"
        try:
            self.assertEqual(probe_cache.probe_cache_path(),
                             os.path.join("/x/y", "probe_cache.json"))
        finally:
            if old_root is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old_root
            if old_cache is not None:
                os.environ["COWORK_PROBE_CACHE"] = old_cache

    def test_t5_unknown_version_never_cached(self):
        calls = []
        common = dict(role_prompt_file=self.role, cache_enabled=True,
                      cache_path=self.cache, version_fn=lambda p: None)
        bridge.probe_claude_stream_json(self._ok_spawn(calls), **common)
        bridge.probe_claude_stream_json(self._ok_spawn(calls), **common)
        self.assertEqual(len(calls), 2)  # no version -> always live-probe


class DiffPacketTest(unittest.TestCase):
    """#4: path-first diff packets + full-reread fallbacks (T7–T10, T12)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.snap = os.path.join(self.dir, "snap")
        os.makedirs(self.snap, exist_ok=True)
        self.json_path = os.path.join(self.dir, "plan.json")
        self.md_path = os.path.join(self.dir, "plan.md")

    def _write(self, obj, md):
        with open(self.json_path, "w") as fh:
            json.dump(obj, fh)
        with open(self.md_path, "w") as fh:
            fh.write(md)

    def _arts(self):
        return [{"label": "plan JSON", "path": self.json_path, "kind": "json"},
                {"label": "plan md", "path": self.md_path, "kind": "markdown"}]

    def _packet(self, **kw):
        return diffpacket.build_review_packet(
            "advisor", 1, 3, self._arts(), self.snap, **kw)

    def test_t10_fresh_is_full_reread_and_writes_snapshot(self):
        self._write({"b": 2, "a": 1}, "# Plan\nbody")
        pkt = self._packet(force_full_reread=True)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, pkt)
        self.assertNotIn("unified diff", pkt.lower())
        # The full JSON body is NOT embedded (only path + hash descriptors).
        self.assertNotIn('"a": 1', pkt)
        self.assertIn(self.json_path, pkt)
        # A snapshot exists for the next round's diff.
        self.assertTrue(os.path.exists(
            diffpacket._snapshot_path(self.snap, "advisor")))

    def test_t7_repeat_round_emits_diff_not_bodies(self):
        self._write({"a": 1}, "# Plan\nold")
        self._packet(force_full_reread=True)        # round 1: seed snapshot
        self._write({"a": 2}, "# Plan\nnew")        # change both files
        pkt = self._packet()                         # round 2: same key -> diff
        self.assertIn(diffpacket.DIFF_INSTRUCTION, pkt)
        self.assertIn("@@", pkt)                     # unified diff hunk
        self.assertIn("+new", pkt)

    def test_t7_json_canonicalized_so_key_churn_is_minimal(self):
        self._write({"a": 1, "b": 2}, "same")
        self._packet(force_full_reread=True)
        # Rewrite with reordered keys + different whitespace, same content.
        with open(self.json_path, "w") as fh:
            fh.write('{\n  "b": 2,\n  "a": 1\n}\n')
        pkt = self._packet()
        self.assertIn(diffpacket.DIFF_INSTRUCTION, pkt)
        self.assertIn("no changes", pkt.lower())

    def test_t8_no_snapshot_forces_full_reread(self):
        self._write({"a": 1}, "md")
        pkt = self._packet()  # no prior snapshot for this key
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, pkt)

    def test_t8_context_rev_change_forces_full_reread(self):
        self._write({"a": 1}, "md")
        diffpacket.build_review_packet("advisor", 1, 3, self._arts(), self.snap,
                                       force_full_reread=True)
        # A different context revision -> different key -> no prior snapshot.
        pkt = diffpacket.build_review_packet(
            "advisor", 1, 99, self._arts(), self.snap)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, pkt)

    def test_t8_canonicalization_failure_forces_full_reread(self):
        self._write({"a": 1}, "md")
        self._packet(force_full_reread=True)
        # Corrupt the JSON so canonicalization fails on this round.
        with open(self.json_path, "w") as fh:
            fh.write("{not valid json")
        pkt = self._packet()
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, pkt)

    def test_t8_diff_over_cap_forces_full_reread(self):
        self._write({"a": 1}, "line\n")
        self._packet(force_full_reread=True)
        with open(self.md_path, "w") as fh:
            fh.write("\n".join("changed-%d" % i for i in range(500)))
        pkt = self._packet(diff_line_cap=10)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, pkt)

    def test_t8_artifact_set_change_forces_full_reread(self):
        # The snapshot key folds in the ordered artifact path set: a changed set
        # (here, dropping the markdown) under the same epoch/revision has no
        # prior snapshot and must full-reread rather than diff a new set.
        self._write({"a": 1}, "md")
        self._packet(force_full_reread=True)        # seed snapshot for 2 arts
        json_only = [{"label": "plan JSON", "path": self.json_path,
                      "kind": "json"}]
        pkt = diffpacket.build_review_packet(
            "advisor", 1, 3, json_only, self.snap)
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, pkt)

    def test_t12_force_full_reread_skips_diff_even_with_snapshot(self):
        # The malformed/weak-verdict retry (D8): a prior snapshot exists and a
        # diff WOULD be eligible, but force_full_reread bypasses it.
        self._write({"a": 1}, "md")
        self._packet(force_full_reread=True)
        self._write({"a": 2}, "md2")
        eligible = self._packet()                       # would be a diff
        self.assertIn(diffpacket.DIFF_INSTRUCTION, eligible)
        self._write({"a": 3}, "md3")
        forced = self._packet(force_full_reread=True)   # retry: full reread
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, forced)
        self.assertNotIn("@@", forced)


class ReviewerPacketContextTest(unittest.TestCase):
    """#4 wiring through the reviewer context assemblers (T9, T10)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.snap = os.path.join(self.dir, "snap")
        os.makedirs(self.snap, exist_ok=True)
        self.pj = os.path.join(self.dir, "plan.json")
        self.pm = os.path.join(self.dir, "plan.md")
        self.status = os.path.join(self.dir, "status.json")
        for p, body in ((self.pj, '{"k": "VALUE-IN-JSON"}'),
                        (self.pm, "# Plan\nMD-BODY"),
                        (self.status, '{"s": "STATUS-BODY"}')):
            with open(p, "w") as fh:
                fh.write(body)

    def _ctx(self, role):
        return {"reviewer_role": role, "epoch": 1, "context_revision": 0,
                "snapshot_dir": self.snap}

    def test_t10_advisor_fresh_is_path_first(self):
        out = cowork.assemble_advisor_context(
            "ctx", ["planner", "planning-advisor"], self.pj, self.pm,
            packet_ctx=self._ctx("planning-advisor"))
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, out)
        self.assertNotIn("VALUE-IN-JSON", out)   # body not embedded
        self.assertNotIn("MD-BODY", out)
        self.assertIn(self.pj, out)              # path present
        # #3: the returned block carries its delivery so run_reviewer_once can
        # tag descriptors truthfully without re-inferring the packet form.
        self.assertEqual(getattr(out, "delivery", None), "path")

    def test_t10_advisor_no_packet_ctx_is_embedded_delivery(self):
        # Legacy full-embed fallback (no packet_ctx): bodies are embedded and
        # the block reports the "embedded" delivery (a plain str -> default).
        out = cowork.assemble_advisor_context(
            "ctx", ["planner", "planning-advisor"], self.pj, self.pm)
        self.assertIn("VALUE-IN-JSON", out)      # body embedded
        self.assertEqual(getattr(out, "delivery", "embedded"), "embedded")

    def test_t9_build_reviewer_packet_keeps_live_delta_recipe(self):
        out = cowork.assemble_build_reviewer_resume_context(
            self.pj, self.pm, self.status,
            baseline_repos=None, packet_ctx=self._ctx("build-reviewer"),
            force_full_reread=True)
        # The embedded artifacts go path-first...
        self.assertIn(diffpacket.FULL_REREAD_INSTRUCTION, out)
        self.assertNotIn("STATUS-BODY", out)
        # ...but the live working-tree delta recipe is untouched.
        self.assertIn("FULL working-tree delta", out)

    def test_t9_build_reviewer_resume_emits_diff_on_repeat(self):
        ctx = self._ctx("build-reviewer")
        cowork.assemble_build_reviewer_resume_context(
            self.pj, self.pm, self.status, packet_ctx=ctx,
            force_full_reread=True)  # seed snapshot
        with open(self.status, "w") as fh:
            fh.write('{"s": "CHANGED-STATUS"}')
        out = cowork.assemble_build_reviewer_resume_context(
            self.pj, self.pm, self.status, packet_ctx=ctx)
        self.assertIn(diffpacket.DIFF_INSTRUCTION, out)
        self.assertIn("CHANGED-STATUS", out)     # diff shows the new line
        self.assertIn("FULL working-tree delta", out)
        # #3: a repeat round emits a diff -> the block reports "diff" delivery
        # even after the context-update wake block is prepended.
        self.assertEqual(getattr(out, "delivery", None), "diff")


class TurnMetaWiringTest(unittest.TestCase):
    """#1 D11: lead + reviewer sends carry the full meta contract (T13)."""

    def _dir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def test_lead_meta_phase_fresh_and_seed_artifacts(self):
        d = self._dir()
        status = os.path.join(d, "status.json")
        intel = os.path.join(d, "intel.json")
        for p, body in ((status, '{"status":"x"}'), (intel, '{"x":1}')):
            with open(p, "w") as fh:
                fh.write(body)
        captured = []

        class Fake:
            def send(self, text, meta=None):
                captured.append(meta)
                with open(status, "w") as fh:
                    json.dump({"status": "needs_input", "result": {}}, fh)

            def close(self):
                pass

        cowork._role_loop(
            Fake(), "seed", status, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role="planner", phase="planning", is_resume=False,
            seed_artifact_paths=[intel], context_revision=3)
        m = captured[0]
        self.assertEqual(m["prompt_kind"], "role_seed")
        self.assertEqual(m["phase"], "planning")
        self.assertTrue(m["fresh"])
        self.assertFalse(m["resume"])
        self.assertEqual(m["context_revision"], 3)
        paths = [a["path"] for a in m["artifacts"]]
        self.assertIn(intel, paths)    # seed artifact referenced on first send
        self.assertIn(status, paths)   # role's own status file
        # #1/#3: lead seeds are path-first now — no body rides, so every lead
        # artifact is tagged "path" with 0 embedded bytes.
        for a in m["artifacts"]:
            self.assertEqual(a["delivery"], "path")
            self.assertEqual(a["embedded_bytes"], 0)

    def test_lead_meta_resumed_launch_marks_resume(self):
        d = self._dir()
        status = os.path.join(d, "status.json")
        with open(status, "w") as fh:
            fh.write('{"status":"x"}')
        captured = []

        class Fake:
            def send(self, text, meta=None):
                captured.append(meta)
                with open(status, "w") as fh:
                    json.dump({"status": "needs_input", "result": {}}, fh)

            def close(self):
                pass

        cowork._role_loop(
            Fake(), "seed", status, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role="scout", phase="scouting", is_resume=True)
        self.assertFalse(captured[0]["fresh"])
        self.assertTrue(captured[0]["resume"])

    def test_reviewer_meta_carries_full_artifact_set(self):
        d = self._dir()
        intel = os.path.join(d, "intel.json")
        md = os.path.join(d, "intel.md")
        review = os.path.join(d, "review.json")
        for p in (intel, md):
            with open(p, "w") as fh:
                fh.write("{}")
        captured = {}

        class Fake:
            def send(self, text, meta=None):
                captured["meta"] = meta

            def close(self):
                pass

        cfg = {cowork.SCOUT_REVIEWER: {"controller": "claude",
                                       "mode": "plan", "yolo": True}}
        cowork.run_reviewer_once(
            cfg, "ctx", [cowork.SCOUT_REVIEWER], intel, review,
            session_factory=lambda c, io: Fake(),
            reviewer_role=cowork.SCOUT_REVIEWER,
            artifact_paths=[intel, md], phase="scouting", context_revision=4)
        m = captured["meta"]
        self.assertEqual(m["prompt_kind"], "reviewer_pass")
        self.assertEqual(m["phase"], "scouting")
        self.assertTrue(m["fresh"])
        self.assertEqual(m["context_revision"], 4)
        # The FULL embedded artifact set is described, not just the primary path.
        self.assertEqual([a["path"] for a in m["artifacts"]], [intel, md])
        # No snapshot_dir wired -> legacy full-embed -> "embedded" delivery,
        # embedded_bytes == full body bytes.
        for a in m["artifacts"]:
            self.assertEqual(a["delivery"], "embedded")
            self.assertEqual(a["embedded_bytes"], a["bytes"])

    def test_reviewer_meta_delivery_is_derived_from_packet(self):
        # #3 KEY: with a snapshot_dir wired the reviewer pass is path-first on a
        # fresh send, and a diff on a repeat round against the same key — the
        # meta delivery is DERIVED from the packet ctx_block actually carried,
        # never a static 'embedded' default.
        d = self._dir()
        snap = os.path.join(d, "snap")
        os.makedirs(snap)
        intel = os.path.join(d, "intel.json")
        md = os.path.join(d, "intel.md")
        review = os.path.join(d, "review.json")
        for p, body in ((intel, '{"v": 1}'), (md, "# MD\nbody")):
            with open(p, "w") as fh:
                fh.write(body)
        captured = {}

        class Fake:
            def send(self, text, meta=None):
                captured["meta"] = meta

            def close(self):
                pass

        cfg = {cowork.SCOUT_REVIEWER: {"controller": "claude",
                                       "mode": "plan", "yolo": True}}

        def run(resume_id):
            cowork.run_reviewer_once(
                cfg, "ctx", [cowork.SCOUT_REVIEWER], intel, review,
                session_factory=lambda c, io: Fake(),
                reviewer_role=cowork.SCOUT_REVIEWER,
                context_fn=lambda ctx, sel, p, packet_ctx=None:
                    cowork.assemble_reviewer_context(
                        ctx, sel, p, md, packet_ctx=packet_ctx),
                resume_context_fn=lambda p, context_update=None,
                    packet_ctx=None, force_full_reread=False:
                    cowork.assemble_reviewer_resume_context(
                        p, md, context_update=context_update,
                        packet_ctx=packet_ctx,
                        force_full_reread=force_full_reread),
                artifact_paths=[intel, md], phase="scouting",
                epoch=1, context_revision=0, snapshot_dir=snap,
                resume_id=resume_id)

        run(None)  # fresh: seeds the snapshot, path-first
        for a in captured["meta"]["artifacts"]:
            self.assertEqual(a["delivery"], "path")
            # ~descriptor-line size (path + hash + size), a small bounded
            # overhead — the artifact BODY is never embedded.
            self.assertLess(a["embedded_bytes"], 1024)
        # Change both files, then resume against the same key -> a diff packet.
        with open(intel, "w") as fh:
            fh.write('{"v": 2}')
        with open(md, "w") as fh:
            fh.write("# MD\nbody changed")
        run("scout-reviewer-1")
        deliveries = {a["delivery"]
                      for a in captured["meta"]["artifacts"]}
        self.assertEqual(deliveries, {"diff"})

    def test_lead_eval_send_carries_meta(self):
        # #1 (review fix): the lead's peer-eval send (_make_evaluate_fn) goes
        # through _send with the eval meta, not a raw session.send.
        d = self._dir()
        scratch = os.path.join(d, "scratch.json")
        scores = os.path.join(d, "scores.json")
        captured = {}

        class Fake:
            def __init__(self):
                self.io_out = io.StringIO()

            def send(self, text, meta=None):
                captured["meta"] = meta

        fn = cowork._make_evaluate_fn(
            "scout", cowork.SCOUT_REVIEWER, "scouting", scratch, scores,
            "UUID", context_revision=7)
        fn(Fake(), {"verdict": "approve"}, 2)
        m = captured["meta"]
        self.assertEqual(m["prompt_kind"], "eval")
        self.assertFalse(m["fresh"])
        self.assertTrue(m["resume"])
        self.assertEqual(m["phase"], "scouting")
        self.assertEqual(m["round"], 2)
        self.assertEqual(m["context_revision"], 7)

    def test_lead_eval_meta_describes_consumed_upstream_artifacts(self):
        # #1 (review fix): on the round-1 eval that bundles the consumed-upstream
        # artifact, the eval meta must describe THOSE embedded files (not the
        # deleted scratch output).
        d = self._dir()
        intel = os.path.join(d, "intel.json")
        intel_md = os.path.join(d, "intel.md")
        for p in (intel, intel_md):
            with open(p, "w") as fh:
                fh.write('{"x":1}')
        scratch = os.path.join(d, "scratch.json")
        scores = os.path.join(d, "scores.json")  # fresh -> no dedup
        consumed = cowork._scout_consumed_upstream(intel, 1, intel_md)
        captured = {}

        class Fake:
            def __init__(self):
                self.io_out = io.StringIO()

            def send(self, text, meta=None):
                captured["meta"] = meta

        fn = cowork._make_evaluate_fn(
            "planner", cowork.PLANNING_ADVISOR, "planning", scratch, scores,
            "UUID", consumed_upstream=consumed, context_revision=3)
        fn(Fake(), {"verdict": "approve"}, 1)   # round 1 -> bundle rides
        arts = captured["meta"]["artifacts"]
        self.assertIsNotNone(arts)
        paths = [a["path"] for a in arts]
        self.assertIn(intel, paths)
        self.assertIn(intel_md, paths)
        for a in arts:
            self.assertIn("bytes", a)
            self.assertIn("sha256", a)

    def test_lead_eval_meta_has_no_artifacts_without_bundle(self):
        # A later-round eval (no consumed-upstream bundle) embeds only the inline
        # verdict, so it carries no embedded artifact files.
        d = self._dir()
        scratch = os.path.join(d, "scratch.json")
        scores = os.path.join(d, "scores.json")
        captured = {}

        class Fake:
            def __init__(self):
                self.io_out = io.StringIO()

            def send(self, text, meta=None):
                captured["meta"] = meta

        fn = cowork._make_evaluate_fn(
            "scout", cowork.SCOUT_REVIEWER, "scouting", scratch, scores, "UUID")
        fn(Fake(), {"verdict": "approve"}, 2)
        self.assertIsNone(captured["meta"]["artifacts"])

    def test_review_run_end_carries_correlation_fields(self):
        # #4 (review fix): review.run.start AND review.run.end carry the full
        # correlation field set.
        d = self._dir()
        intel = os.path.join(d, "intel.json")
        review = os.path.join(d, "review.json")
        with open(intel, "w") as fh:
            fh.write("{}")
        tpath = os.path.join(d, "trace.jsonl")
        trace = trace_store.Trace(tpath, session_uuid="X", run_id="R")

        class Fake:
            def send(self, text, meta=None):
                pass

            def close(self):
                pass

        cfg = {cowork.SCOUT_REVIEWER: {"controller": "claude",
                                       "mode": "plan", "yolo": True}}
        cowork.run_reviewer_once(
            cfg, "ctx", [cowork.SCOUT_REVIEWER], intel, review,
            session_factory=lambda c, io_out: Fake(),
            reviewer_role=cowork.SCOUT_REVIEWER,
            artifact_paths=[intel], phase="scouting", context_revision=4,
            trace=trace)
        with open(tpath) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        end = [e for e in events if e["event"] == "review.run.end"][0]
        self.assertEqual(end["prompt_kind"], "reviewer_pass")
        self.assertEqual(end["phase"], "scouting")
        self.assertEqual(end["context_revision"], 4)
        self.assertIn("fresh", end)
        self.assertIn("resume", end)
        self.assertEqual([a["path"] for a in end["artifacts"]], [intel])

    def test_report_counts_artifact_bytes_across_send_types(self):
        # End-to-end: lead + reviewer + eval controller.turn.start events all
        # aggregate into bytes-by-prompt-kind and artifact-by-file.
        trace = [
            {"event": "controller.turn.start", "role": "planner",
             "controller": "claude", "prompt_kind": "role_seed",
             "prompt_bytes": 200, "fresh": True,
             "artifacts": [{"path": "intel.json", "bytes": 50}]},
            {"event": "controller.turn.start", "role": "planning-advisor",
             "controller": "claude", "prompt_kind": "reviewer_pass",
             "prompt_bytes": 120, "resume": True,
             "artifacts": [{"path": "plan.json", "bytes": 30},
                           {"path": "plan.md", "bytes": 20}]},
            {"event": "controller.turn.start", "role": "planning-advisor",
             "controller": "claude", "prompt_kind": "eval",
             "prompt_bytes": 40, "resume": True},
        ]
        s = cowork_report.summarize_trace(trace)
        self.assertEqual(set(s["bytes_by_kind"]),
                         {"role_seed", "reviewer_pass", "eval"})
        self.assertEqual(s["artifact_bytes"]["plan.md"]["bytes"], 20)
        self.assertEqual(s["fresh_resume"], {"fresh": 1, "resume": 2,
                                             "unknown": 0})


class DocsChecklistTest(unittest.TestCase):
    """T11: sections 1–4 of the plan doc carry a checkmark; 5–6 do not."""

    def test_sections_1_to_4_checked(self):
        path = os.path.join(_HERE, "..", ".plans",
                            "cowork-token-reduction.md")
        with open(path, "r") as fh:
            text = fh.read()
        self.assertIn("## 1. Improve Prompt-Size Accounting ✅", text)
        self.assertIn("## 2. Add A Session Token/Byte Report ✅", text)
        self.assertIn("## 3. Cache Claude Probe Success ✅", text)
        self.assertIn("## 4. Use Path-First, Diff-Based Review Packets ✅", text)
        self.assertIn("## 5. Tighten Artifact Schemas", text)
        self.assertNotIn("## 5. Tighten Artifact Schemas ✅", text)
        self.assertNotIn("## 6. Avoid Duplicate Context Replay ✅", text)


def _init_git_repo():
    """Create a committed temp git repo and return its absolute path."""
    import tempfile
    d = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    with open(os.path.join(d, "f.txt"), "w") as fh:
        fh.write("x")
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return d


class WorktreeFlagTest(unittest.TestCase):
    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_worktree_flag_optional_name(self):
        # no name -> const True
        a = self._args(["--worktree", "--context", "x"])
        self.assertIs(a.worktree, True)
        # explicit name
        a = self._args(["--wt", "feat-x", "--context", "x"])
        self.assertEqual(a.worktree, "feat-x")
        # absent
        a = self._args(["--context", "x"])
        self.assertIsNone(a.worktree)

    def test_wt_controller_default_and_choice(self):
        self.assertEqual(self._args(["--context", "x"]).wt_controller, "claude")
        self.assertEqual(
            self._args(["--wt-controller", "codex", "--context", "x"])
            .wt_controller, "codex")

    def test_headless_flag_and_alias_and_non_interactive(self):
        a = self._args(["--headless", "--context", "x"])
        self.assertTrue(a.headless)
        self.assertTrue(cowork._is_non_interactive(a))
        a = self._args(["--auto", "--context", "x"])
        self.assertTrue(a.headless)

    def test_default_worktree_name(self):
        self.assertEqual(cowork.default_worktree_name("abcdef0123456789"),
                         "cowork-abcdef01")


class WorktreeHelperTest(unittest.TestCase):
    def test_git_gate_inside_and_outside(self):
        repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        self.assertEqual(cowork.git_worktree_toplevel(repo), repo)
        # a subdir of the repo still resolves to the toplevel
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        self.assertEqual(cowork.git_worktree_toplevel(sub), repo)
        # a non-git dir -> None (the gate fails fast)
        import tempfile
        nongit = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(nongit, ignore_errors=True))
        self.assertIsNone(cowork.git_worktree_toplevel(nongit))

    def test_brief_carries_collision_policy(self):
        # Collision resolution is delegated to the agent prompt, so the brief
        # MUST carry the explicit-name reuse-or-fail policy and the auto-name
        # numeric-suffix policy — guard against a silent prompt regression.
        explicit = cowork.assemble_worktree_brief("/s.json", "/base", "feat",
                                                  True)
        self.assertIn("EXPLICITLY", explicit)
        self.assertIn("reuse", explicit.lower())
        self.assertIn("report failure", explicit.lower())
        auto = cowork.assemble_worktree_brief("/s.json", "/base", "cowork-ab",
                                              False)
        self.assertIn("AUTO-generated", auto)
        self.assertIn("numeric suffix", auto.lower())
        self.assertIn("cowork-ab-2", auto)

    def _make_worktree(self, repo, name):
        path = os.path.join(repo, ".worktrees", name)
        subprocess.run(["git", "-C", repo, "worktree", "add", path, "-b", name],
                       check=True, capture_output=True)
        return os.path.realpath(path)

    def test_validate_success(self):
        repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        wt = self._make_worktree(repo, "feat")
        artifact = {"status": "ready",
                    "result": {"worktree_path": wt, "branch": "feat"}}
        ok, path, branch, err = cowork.validate_worktree(repo, artifact)
        self.assertTrue(ok, err)
        self.assertEqual(os.path.realpath(path), wt)
        self.assertEqual(branch, "feat")

    def test_validate_failures(self):
        repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        wt = self._make_worktree(repo, "feat")
        # no artifact
        self.assertFalse(cowork.validate_worktree(repo, None)[0])
        # status failed
        self.assertFalse(cowork.validate_worktree(
            repo, {"status": "failed", "result": {"error": "boom"}})[0])
        # handoff_back is also a failure (no hand-back partner)
        self.assertFalse(cowork.validate_worktree(
            repo, {"status": "handoff_back"})[0])
        # non-absolute path
        self.assertFalse(cowork.validate_worktree(
            repo, {"status": "ready",
                   "result": {"worktree_path": "rel/x", "branch": "feat"}})[0])
        # nonexistent path
        self.assertFalse(cowork.validate_worktree(
            repo, {"status": "ready",
                   "result": {"worktree_path": "/nope/zzz", "branch": "f"}})[0])
        # missing branch
        self.assertFalse(cowork.validate_worktree(
            repo, {"status": "ready", "result": {"worktree_path": wt}})[0])
        # unregistered path (a real dir not registered as a worktree)
        import tempfile
        stray = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(stray, ignore_errors=True))
        self.assertFalse(cowork.validate_worktree(
            repo, {"status": "ready",
                   "result": {"worktree_path": stray, "branch": "feat"}})[0])
        # branch mismatch
        self.assertFalse(cowork.validate_worktree(
            repo, {"status": "ready",
                   "result": {"worktree_path": wt, "branch": "other"}})[0])


class RunWorktreeTest(unittest.TestCase):
    """run_worktree spawns one agent (injected) and reads back its artifact."""

    def _status_path(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, "worktree.status.json")

    def test_reads_back_agent_artifact(self):
        status = self._status_path()
        sent = {}

        def factory(controller):
            class FakeSession:
                def send(self, text):
                    sent["text"] = text
                    with open(status, "w") as fh:
                        json.dump({"role": "worktree", "status": "ready",
                                   "result": {"worktree_path": "/abs/wt",
                                              "branch": "feat"}}, fh)

                def close(self):
                    sent["closed"] = True
            return FakeSession()

        cfg = {"controller": "codex", "yolo": True, "mode": "implement"}
        artifact = cowork.run_worktree(
            cfg, status, "/base/repo", "feat", True,
            io_out=io.StringIO(), session_factory=factory)
        self.assertEqual(artifact["status"], "ready")
        self.assertEqual(artifact["result"]["branch"], "feat")
        self.assertTrue(sent.get("closed"))
        # the brief carries the base repo, the name, and the explicit policy
        self.assertIn("/base/repo", sent["text"])
        self.assertIn("feat", sent["text"])

    def test_no_artifact_returns_none(self):
        status = self._status_path()

        def factory(controller):
            class FakeSession:
                def send(self, text):
                    pass  # writes nothing

                def close(self):
                    pass
            return FakeSession()

        cfg = {"controller": "codex", "yolo": True, "mode": "implement"}
        artifact = cowork.run_worktree(
            cfg, status, "/base/repo", "auto", False,
            io_out=io.StringIO(), session_factory=factory)
        self.assertIsNone(artifact)


class WorktreeFlowTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore_env():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore_env)
        cwd = os.getcwd()
        self.addCleanup(lambda: os.chdir(cwd))

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _repo(self):
        repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        return repo

    def _creating_fn(self, calls):
        """A run_worktree_fn that really creates the worktree and records the
        call. Returns a ready artifact pointing at the created path."""
        def fn(wt_config, status_path, base, name, explicit, **kw):
            calls.append({"base": base, "name": name, "explicit": explicit,
                          "controller": wt_config["controller"]})
            path = os.path.join(base, ".worktrees", name)
            subprocess.run(
                ["git", "-C", base, "worktree", "add", path, "-b", name],
                check=True, capture_output=True)
            return {"status": "ready",
                    "result": {"worktree_path": os.path.realpath(path),
                               "branch": name}}
        return fn

    def test_gate_outside_git_repo_is_rc2(self):
        import tempfile
        nongit = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(nongit, ignore_errors=True))
        os.chdir(nongit)
        scout_calls = []

        def fake_scout(*a, **k):
            scout_calls.append(1)
            return 0
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--worktree", "--team", "scout", "--context", "x",
                        "--no-session"]),
            io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout,
            run_worktree_fn=lambda *a, **k: self.fail("role ran outside git"))
        self.assertEqual(rc, 2)
        self.assertIn("requires launching inside a git work tree",
                      out.getvalue())
        self.assertEqual(scout_calls, [])  # never reached scouting

    def test_creates_and_redirects_into_worktree(self):
        repo = self._repo()
        os.chdir(repo)
        calls = []
        seen = {}

        def fake_scout(config, context, selected, **kw):
            seen["cwd"] = os.path.realpath(os.getcwd())
            return 0
        rc = cowork.run_flow(
            self._args(["--worktree", "feat", "--wt-controller", "codex",
                        "--team", "scout", "--context", "x", "--no-session"]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout,
            run_worktree_fn=self._creating_fn(calls))
        self.assertEqual(rc, 0)
        # the role ran once, with the single base toplevel and explicit name
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["base"], repo)
        self.assertEqual(calls[0]["name"], "feat")
        self.assertTrue(calls[0]["explicit"])
        self.assertEqual(calls[0]["controller"], "codex")
        # the session was redirected INTO the created worktree
        self.assertEqual(seen["cwd"],
                         os.path.realpath(os.path.join(repo, ".worktrees",
                                                       "feat")))

    def test_validation_failure_no_chdir_rc2(self):
        repo = self._repo()
        os.chdir(repo)
        before = os.path.realpath(os.getcwd())
        scout_calls = []

        def fake_scout(*a, **k):
            scout_calls.append(1)
            return 0
        rc = cowork.run_flow(
            self._args(["--worktree", "--team", "scout", "--context", "x",
                        "--no-session"]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout,
            run_worktree_fn=lambda *a, **k: {
                "status": "failed", "result": {"error": "boom"}})
        self.assertEqual(rc, 2)
        self.assertEqual(os.path.realpath(os.getcwd()), before)  # no chdir
        self.assertEqual(scout_calls, [])

    def test_auto_name_default(self):
        repo = self._repo()
        os.chdir(repo)
        calls = []
        rc = cowork.run_flow(
            self._args(["--worktree", "--team", "scout", "--context", "x",
                        "--no-session"]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0,
            run_worktree_fn=self._creating_fn(calls))
        self.assertEqual(rc, 0)
        # auto name = cowork-<short session id>; controller defaults to claude
        self.assertTrue(calls[0]["name"].startswith("cowork-"))
        self.assertFalse(calls[0]["explicit"])
        self.assertEqual(calls[0]["controller"], "claude")

    def test_worktree_and_headless_compose(self):
        repo = self._repo()
        os.chdir(repo)
        calls = []
        seen = {}

        def fake_scout(config, context, selected, headless=False, **kw):
            seen["cwd"] = os.path.realpath(os.getcwd())
            seen["headless"] = headless
            return 0
        rc = cowork.run_flow(
            self._args(["--worktree", "feat", "--headless", "--team", "scout",
                        "--context", "x", "--no-session"]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout,
            run_worktree_fn=self._creating_fn(calls))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)  # worktree provisioned first
        self.assertTrue(seen["headless"])  # then the flow runs headless inside
        self.assertEqual(seen["cwd"],
                         os.path.realpath(os.path.join(repo, ".worktrees",
                                                       "feat")))

    def test_resume_reuses_existing_worktree(self):
        repo = self._repo()
        os.chdir(repo)
        spath = os.path.join(repo, ".cowork", "session.json")
        calls = []
        # run 1: creates + records the worktree
        rc = cowork.run_flow(
            self._args(["--worktree", "feat", "--team", "scout",
                        "--context", "x", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0,
            run_worktree_fn=self._creating_fn(calls))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        saved = state_store.load(spath)
        self.assertIsNotNone(state_store.get_worktree(saved))
        os.chdir(repo)  # run 1 redirected us into the worktree; back to launch
        # run 2 (resume): reuses the recorded worktree, role NOT re-run
        rc = cowork.run_flow(
            self._args(["--worktree", "feat", "--team", "scout",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0,
            run_worktree_fn=self._creating_fn(calls))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)  # no second creation

    def test_resume_recreates_when_recorded_worktree_is_stale(self):
        repo = self._repo()
        os.chdir(repo)
        spath = os.path.join(repo, ".cowork", "session.json")
        calls = []
        rc = cowork.run_flow(
            self._args(["--worktree", "feat", "--team", "scout",
                        "--context", "x", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0,
            run_worktree_fn=self._creating_fn(calls))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        # make the recorded worktree STALE: deregister it from git
        wtpath = os.path.join(repo, ".worktrees", "feat")
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force",
                        wtpath], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "branch", "-D", "feat"],
                       check=True, capture_output=True)
        os.chdir(repo)
        # run 2: recorded path no longer validates -> re-create, never a blind
        # chdir into the stale/unregistered path
        rc = cowork.run_flow(
            self._args(["--worktree", "feat", "--team", "scout",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0,
            run_worktree_fn=self._creating_fn(calls))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)  # re-created, not reused

    def test_headless_runtime_note_in_lead_seed_and_reviewer_context(self):
        repo = self._repo()
        os.chdir(repo)
        seen = {}

        def fake_scout(config, context, selected, reviewer_context=None, **kw):
            seen["context"] = context
            seen["reviewer_context"] = reviewer_context
            return 0
        rc = cowork.run_flow(
            self._args(["--worktree", "feat", "--headless", "--team", "scout",
                        "--context", "GOALTEXT", "--no-session"]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout,
            run_worktree_fn=self._creating_fn([]))
        self.assertEqual(rc, 0)
        self.assertIn("[headless mode]", seen["context"])
        self.assertIn("[headless mode]", seen["reviewer_context"])
        # the lead seed still carries the original goal
        self.assertIn("GOALTEXT", seen["context"])

    def test_missing_controller_headless_fails_without_prompt(self):
        repo = self._repo()
        os.chdir(repo)
        rc = cowork.run_flow(
            self._args(["--headless", "--team", "scout", "--context", "x",
                        "--no-session"]),
            io_out=io.StringIO(), which=lambda c: None,  # controller missing
            run_scout_fn=lambda *a, **k: self.fail("should not launch scout"))
        self.assertEqual(rc, 1)  # ensure_controller_available returns False


class HeadlessRoleLoopTest(unittest.TestCase):
    """Drive _role_loop with headless=True and NO human input — every gate must
    auto-resolve and the loop must never hang."""

    def _path(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "x.json")

    def _session(self, path, writes):
        class ScriptedSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                w = writes.pop(0) if writes else None
                if w is not None:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as fh:
                        json.dump(w, fh)

            def close(self):
                self.closed = True
        return ScriptedSession()

    def _review_fn(self, verdicts):
        def fn(status_path, round_index):
            return verdicts.pop(0) if verdicts else {"verdict": "approve"}
        return fn

    _READY = {"status": "ready_for_review", "result": {}}

    def test_needs_input_nudged_then_ready(self):
        path = self._path()
        sess = self._session(
            path, [{"status": "needs_input", "result": {}}, dict(self._READY)])
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(), headless=True)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(sess.sent), 2)
        self.assertIn("headless", sess.sent[1].lower())

    def test_needs_input_loop_bounded(self):
        path = self._path()
        # always a DIFFERENT needs_input (defeats the byte-level no-op detector)
        writes = [{"status": "needs_input", "result": {"n": i}}
                  for i in range(20)]
        sess = self._session(path, writes)
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(), headless=True)
        self.assertEqual(outcome, "ended")  # HEADLESS_NUDGE_CAP backstop
        self.assertLessEqual(len(sess.sent), cowork.HEADLESS_NUDGE_CAP + 1)

    def test_ready_auto_approves_without_input(self):
        path = self._path()
        sess = self._session(path, [dict(self._READY)])
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(), headless=True)
        self.assertEqual(outcome, "approved")
        self.assertEqual(len(sess.sent), 1)

    def test_reviewer_approve_consensus_advances(self):
        path = self._path()
        sess = self._session(path, [dict(self._READY)])
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(), headless=True,
            review_fn=self._review_fn([{"verdict": "approve"}]))
        self.assertEqual(outcome, "approved")

    def test_reviewer_needs_user_downgraded_to_revise(self):
        path = self._path()
        sess = self._session(path, [dict(self._READY), dict(self._READY)])
        rfn = self._review_fn([
            {"verdict": "needs_user", "user_question": "Support legacy X?"},
            {"verdict": "approve"}])
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=out, headless=True, review_fn=rfn)
        self.assertEqual(outcome, "approved")
        # the question reached the LEAD as a revise finding, not the user
        self.assertIn("Support legacy X?", sess.sent[1])
        self.assertNotIn("Support legacy X?", out.getvalue())

    def test_reviewer_failure_skips_under_headless(self):
        path = self._path()
        sess = self._session(path, [dict(self._READY)])
        # every verdict is unusable -> failure; headless skips after the cap
        rfn = self._review_fn([{}, {}, {}, {}])
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(), headless=True, review_fn=rfn)
        self.assertEqual(outcome, "approved")

    def test_round_cap_accepts_with_dissent(self):
        path = self._path()
        sess = self._session(path, [dict(self._READY) for _ in range(8)])
        rfn = self._review_fn([{"verdict": "revise", "findings": ["nit"]}
                               for _ in range(8)])
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=out, headless=True, review_fn=rfn)
        self.assertEqual(outcome, "approved")
        self.assertIn("cap reached", out.getvalue())

    def test_handoff_back_auto_declined(self):
        path = self._path()
        sess = self._session(path, [
            {"status": "handoff_back", "handoff": "re-scope", "result": {}},
            dict(self._READY)])
        rc, outcome, payload = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(), headless=True, handoff_enabled=True)
        self.assertEqual(outcome, "approved")  # NOT "handoff"
        self.assertEqual(len(sess.sent), 2)

    def test_controller_failure_ends_under_headless_no_prompt(self):
        # A send failure with no status write: headless ends cleanly instead of
        # showing the interactive retry/switch/end controller-failure gate.
        path = self._path()

        class FailSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                return {"ok": False, "result": "error",
                        "error_type": "usage_limit"}

            def close(self):
                self.closed = True
        sess = FailSession()
        out = io.StringIO()
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=out, headless=True)
        self.assertEqual(outcome, "ended")
        self.assertNotIn("cannot make progress", out.getvalue())
        self.assertTrue(sess.closed)

    def test_without_headless_does_not_auto_progress(self):
        # Regression guard: same needs_input write, but WITHOUT headless and no
        # input -> the loop ends (EOF) instead of nudging. No bypass.
        path = self._path()
        sess = self._session(path, [{"status": "needs_input", "result": {}}])
        rc, outcome, _ = cowork._role_loop(
            sess, "seed", path, context="", io_in=io.StringIO(""),
            io_out=io.StringIO(), headless=False)
        self.assertEqual(outcome, "ended")
        self.assertEqual(sess.sent, ["seed"])  # no nudge sent


class WorktreeStateTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_set_and_get_worktree_roundtrip(self):
        spath = self._tmp()
        state = state_store.ensure_session(spath, None, "S")
        self.assertIsNone(state_store.get_worktree(state))
        state = state_store.set_worktree(spath, "/abs/wt", "feat", prior=state)
        got = state_store.get_worktree(state)
        self.assertEqual(got, {"path": "/abs/wt", "branch": "feat"})
        # survives a reload
        self.assertEqual(state_store.get_worktree(state_store.load(spath)),
                         {"path": "/abs/wt", "branch": "feat"})

    def test_resume_discovery_is_cwd_relative(self):
        # D3 resume-from-launch-dir: the session store is discovered from the
        # cwd, so a session created at the launch dir is NOT found from inside a
        # (sibling) worktree dir — confirming resume must be from the launch dir.
        import tempfile
        launch = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(launch, ignore_errors=True))
        worktree = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(worktree, ignore_errors=True))
        state_store.ensure_session(
            state_store.session_path(launch), None, "S")
        self.assertTrue(state_store.discover_session_files(launch))
        self.assertEqual(state_store.discover_session_files(worktree), [])


class HeadlessFlowContextTest(unittest.TestCase):
    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_headless_without_context_is_rc2(self):
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--headless", "--team", "scout", "--no-session"]),
            io_out=out, which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: self.fail("should not reach scout"))
        self.assertEqual(rc, 2)
        self.assertIn("requires initial context", out.getvalue())


class HeadlessReviewerResumeTest(unittest.TestCase):
    """A RESUMED reviewer's first headless turn uses context_update (not
    reviewer_context), so the headless note must ride context_update too."""

    def setUp(self):
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        old = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if old is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old
        self.addCleanup(restore)

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_resumed_reviewer_context_update_carries_headless_note(self):
        spath = self._tmp_session()
        team = ["scout", "scout-reviewer"]
        state = state_store.ensure_session(spath, None, "S")
        state = state_store.save_config(
            spath, team, cowork.default_config(team), prior=state)
        # a saved scout-reviewer session id makes it a RESUMED reviewer
        state_store.save_role_session(
            spath, "scout-reviewer", "codex", "rev-1", prior=state)
        seen = {}

        def fake_scout(config, context, selected,
                       reviewer_context_update=None, reviewer_context=None,
                       **kw):
            seen["cu"] = reviewer_context_update
            seen["ctx"] = reviewer_context
            return 0
        rc = cowork.run_flow(
            self._args(["--headless", "--context", "x",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        # the resumed reviewer's first-turn context_update carries the note...
        self.assertIsNotNone(seen["cu"])
        self.assertIn("[headless mode]", seen["cu"])
        # ...and fresh reviewers still get it via reviewer_context
        self.assertIn("[headless mode]", seen["ctx"])


class RolePromptHeadlessDirectiveTest(unittest.TestCase):
    def test_every_role_prompt_carries_headless_directive(self):
        roles_dir = os.path.join(_HERE, "..", "roles")
        for name in ("scout", "planner", "builder", "scout-reviewer",
                     "planning-advisor", "build-reviewer"):
            with open(os.path.join(roles_dir, name + ".md")) as fh:
                text = fh.read()
            self.assertIn("Headless mode", text, name)

    def test_worktree_role_prompt_exists(self):
        path = os.path.join(_HERE, "..", "roles", "worktree.md")
        with open(path) as fh:
            text = fh.read()
        self.assertIn("worktree", text.lower())
        self.assertIn("status", text.lower())


class AssembleUserQuestionTest(unittest.TestCase):
    """The harness-boundary prompt for a gate-time question."""

    def test_carries_question_and_no_edit_instruction(self):
        out = cowork.assemble_user_question("why mongo?", artifact="plan")
        self.assertIn("why mongo?", out)
        self.assertIn("answer", out.lower())
        self.assertIn("plan", out)
        self.assertIn("ready_for_review", out)
        self.assertIn("Do NOT edit", out)
        self.assertIn("not a request to change", out.lower())


class ReadReviewThreeWayTest(unittest.TestCase):
    """The 3-way review gate: Approve & finish / Ask a question / Request
    changes on a TTY; the unchanged blank=finish / text=revise contract off
    one."""

    def _tty(self):
        return FakeTTY(), FakeTTY()

    def test_off_tty_blank_finishes(self):
        self.assertIs(cowork._read_review(io.StringIO("\n"), io.StringIO()),
                      cowork._END)
        self.assertIs(cowork._read_review(io.StringIO(""), io.StringIO()),
                      cowork._END)

    def test_off_tty_text_revises(self):
        self.assertEqual(
            cowork._read_review(io.StringIO("change x\n"), io.StringIO()),
            "change x")

    def test_tty_approve_ends(self):
        import unittest.mock as mock
        i, o = self._tty()
        with mock.patch.object(ui, "select", return_value="approve"):
            self.assertIs(cowork._read_review(i, o), cowork._END)

    def test_tty_ask_returns_marker(self):
        import unittest.mock as mock
        i, o = self._tty()
        with mock.patch.object(ui, "select", return_value="ask"), \
                mock.patch.object(ui, "prompt_user", return_value="why this?"):
            out = cowork._read_review(i, o)
        self.assertIsInstance(out, tuple)
        self.assertIs(out[0], cowork._ASK)
        self.assertEqual(out[1], "why this?")

    def test_tty_ask_blank_reshows_gate_never_approves(self):
        import unittest.mock as mock
        i, o = self._tty()
        sel = mock.Mock(side_effect=["ask", "approve"])
        with mock.patch.object(ui, "select", sel), \
                mock.patch.object(ui, "prompt_user", return_value="   "):
            self.assertIs(cowork._read_review(i, o), cowork._END)
        # A blank question re-showed the select rather than approving.
        self.assertEqual(sel.call_count, 2)

    def test_tty_changes_returns_text(self):
        import unittest.mock as mock
        i, o = self._tty()
        with mock.patch.object(ui, "select", return_value="changes"), \
                mock.patch.object(ui, "prompt_user",
                                  return_value="tighten scope"):
            self.assertEqual(cowork._read_review(i, o), "tighten scope")

    def test_tty_changes_blank_never_traps(self):
        import unittest.mock as mock
        i, o = self._tty()
        with mock.patch.object(ui, "select", return_value="changes"), \
                mock.patch.object(ui, "prompt_user", return_value=""):
            self.assertIs(cowork._read_review(i, o), cowork._END)

    def test_tty_dismissed_select_never_traps(self):
        import unittest.mock as mock
        i, o = self._tty()
        with mock.patch.object(ui, "select", return_value=None), \
                mock.patch.object(ui, "prompt_user", return_value=""):
            self.assertIs(cowork._read_review(i, o), cowork._END)

    def test_tty_no_ask_uses_binary_confirm(self):
        # allow_ask=False (the builder gate) keeps the prior binary confirm
        # contract: no select, no ask path.
        import unittest.mock as mock
        i, o = self._tty()
        with mock.patch.object(ui, "confirm", return_value=True) as conf, \
                mock.patch.object(ui, "select") as sel:
            self.assertIs(cowork._read_review(i, o, allow_ask=False),
                          cowork._END)
        conf.assert_called_once()
        sel.assert_not_called()

    def test_tty_no_ask_decline_revises(self):
        import unittest.mock as mock
        i, o = self._tty()
        with mock.patch.object(ui, "confirm", return_value=False), \
                mock.patch.object(ui, "prompt_user", return_value="fix it"), \
                mock.patch.object(ui, "select") as sel:
            self.assertEqual(cowork._read_review(i, o, allow_ask=False),
                             "fix it")
        sel.assert_not_called()


class ReviewGateQuestionTest(unittest.TestCase):
    """The "Ask a question" path end-to-end through the shared role loop: a
    non-reopen turn that answers in chat, leaves the artifact byte-identical,
    and lets the hash-gate auto-skip the paired advisor — no invalidate, no
    stale-no-op, no re-review. Covered for both the planner/_role_loop generic
    path and the scout loop."""

    def _dir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        os.makedirs(os.path.join(d, ".cowork"), exist_ok=True)
        # The skip-baseline state lives in a SEPARATE session file, never the
        # reviewed status artifact.
        spath = os.path.join(d, "session.json")
        state_store.save(spath, {"team": [], "config": {}, "sessions": {}})
        self._spath = spath
        return d

    def _trace(self, d):
        return trace_store.Trace(os.path.join(d, ".cowork", "trace.X.jsonl"),
                                 session_uuid="X", run_id="R")

    def _events(self, d):
        tpath = os.path.join(d, ".cowork", "trace.X.jsonl")
        with open(tpath) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _raw_trace(self, d):
        with open(os.path.join(d, ".cowork", "trace.X.jsonl")) as fh:
            return fh.read()

    _READY = {"status": "ready_for_review", "result": {}}

    def _session(self, path, writes):
        """`writes` is a per-send list: a dict is written as the status
        artifact, None means the turn writes nothing (the question turn)."""
        class ScriptedSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                w = writes.pop(0) if writes else None
                if w is not None:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as fh:
                        json.dump(w, fh)

            def close(self):
                self.closed = True
        return ScriptedSession()

    def _review_fn(self, verdicts):
        calls = {"n": 0}

        def review_fn(status_path, round_index):
            calls["n"] += 1
            return verdicts.pop(0) if verdicts else {"verdict": "approve"}
        review_fn.calls = calls
        return review_fn

    def _bundle(self, spath, covered, reviewer_role):
        # Mirrors run_flow.make_skip_baseline (see HashGateSkipTest._bundle).
        holder = {"state": state_store.load(spath)}

        def compute():
            return state_store.composite_artifact_hash(covered)

        def eligible(h):
            return state_store.review_skip_eligible(
                holder["state"], reviewer_role, 0, 0, h)

        def record(h):
            holder["state"] = state_store.record_review_baseline(
                spath, reviewer_role, 0, 0, h, prior=holder["state"])
        return cowork.SkipBaseline(compute, eligible, record)

    def _assert_question_was_free(self, d, rfn, status_path,
                                  question="why this approach?"):
        events = self._events(d)
        # The role artifact was never invalidated (no reopen).
        self.assertFalse(any(e["event"] == "status.invalidated"
                             for e in events))
        # No stale-no-op fired even though the question turn wrote nothing.
        self.assertFalse(any(e["event"] == "stale_noop" for e in events))
        self.assertFalse(any(e["event"] == "stale_noop.unresolved"
                             for e in events))
        # The advisor ran exactly once (the original approve), then was skipped
        # on the unchanged follow-up.
        self.assertEqual(rfn.calls["n"], 1)
        self.assertTrue(any(e["event"] == "review.skipped" for e in events))
        # The question is recorded as a distinct, content-free user action.
        q = [e for e in events if e["event"] == "user.action"
             and e.get("action") == "question"]
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["gate"], "ready_for_review")
        self.assertIn("input_sha256", q[0])
        self.assertIn("input_bytes", q[0])
        # The raw question text never lands in the trace (privacy).
        self.assertNotIn(question, self._raw_trace(d))
        # The gate was shown before AND after the question (re-shown).
        gate_shows = [e for e in events if e["event"] == "gate.show"
                      and e.get("gate") == "ready_for_review"]
        self.assertGreaterEqual(len(gate_shows), 2)
        # The status artifact is byte-identical to the approved READY bytes.
        with open(status_path) as fh:
            self.assertEqual(json.load(fh)["status"], "ready_for_review")

    def test_question_gate_planner_role_loop(self):
        import unittest.mock as mock
        d = self._dir()
        path = os.path.join(d, ".cowork", "planner.plan.X.json")
        sess = self._session(path, [dict(self._READY)])  # only the seed writes
        rfn = self._review_fn([{"verdict": "approve"}])
        bundle = self._bundle(self._spath, [path], "planning-advisor")
        trace = self._trace(d)
        out = io.StringIO()
        with mock.patch.object(
                cowork, "_read_review",
                side_effect=[(cowork._ASK, "why this approach?"), cowork._END]):
            rc, outcome, _ = cowork._role_loop(
                sess, "seed", path, context="",
                io_in=io.StringIO(""), io_out=out,
                review_fn=rfn, skip_baseline=bundle, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "approved")
        # The question turn was tagged user_question (per-turn accounting).
        starts = [e for e in self._events(d) if e["event"] == "role.send.start"]
        self.assertIn("user_question",
                      [e.get("prompt_kind") for e in starts])
        self.assertIn("review skipped", out.getvalue())
        self._assert_question_was_free(d, rfn, path)

    def test_question_gate_scout_loop(self):
        import unittest.mock as mock
        d = self._dir()
        intel = os.path.join(d, ".cowork", "scout.intel.X.json")
        intel_md = os.path.join(d, ".cowork", "scout.intel.X.md")
        with open(intel, "w") as fh:
            json.dump(dict(self._READY), fh)
        with open(intel_md, "w") as fh:
            fh.write("# intel markdown")
        sess = self._session(intel, [dict(self._READY)])
        rfn = self._review_fn([{"verdict": "approve"}])
        bundle = self._bundle(self._spath, [intel, intel_md], "scout-reviewer")
        trace = self._trace(d)
        out = io.StringIO()
        with mock.patch.object(
                cowork, "_read_review",
                side_effect=[(cowork._ASK, "why this approach?"), cowork._END]):
            rc = cowork._scout_loop(
                sess, "seed", intel, context="",
                io_in=io.StringIO(""), io_out=out, review_fn=rfn,
                intel_md_path=intel_md, skip_baseline=bundle, trace=trace)
        self.assertEqual(rc, 0)
        self.assertIn("review skipped", out.getvalue())
        self._assert_question_was_free(d, rfn, intel)

    def test_request_changes_still_reopens_as_revise(self):
        # Regression: the 3-way "Request changes" path (a plain string from
        # _read_review) still reopens work exactly like today's revise.
        import unittest.mock as mock
        d = self._dir()
        path = os.path.join(d, ".cowork", "planner.plan.X.json")
        sess = self._session(path, [
            dict(self._READY),
            {"status": "ready_for_review", "result": {"v": 2}}])  # revised bytes
        rfn = self._review_fn([{"verdict": "approve"}, {"verdict": "approve"}])
        bundle = self._bundle(self._spath, [path], "planning-advisor")
        trace = self._trace(d)
        with mock.patch.object(
                cowork, "_read_review",
                side_effect=["please tighten scope", cowork._END]):
            rc, outcome, _ = cowork._role_loop(
                sess, "seed", path, context="",
                io_in=io.StringIO(""), io_out=io.StringIO(),
                review_fn=rfn, skip_baseline=bundle, trace=trace)
        self.assertEqual(rc, 0)
        self.assertEqual(outcome, "approved")
        events = self._events(d)
        revise = [e for e in events if e["event"] == "user.action"
                  and e.get("action") == "revise"]
        self.assertEqual(len(revise), 1)
        self.assertEqual(revise[0]["gate"], "ready_for_review")
        # Work was reopened: the artifact was invalidated and the advisor re-ran.
        self.assertTrue(any(e["event"] == "status.invalidated"
                            and e["changed"] for e in events))
        self.assertEqual(rfn.calls["n"], 2)


# --------------------------------------------------------------------------- #
# Eval traceability: model capture, per-role model pins, the role-identity     #
# registry, eval-turn accounting stamps on scores.json, and the scores report. #
# --------------------------------------------------------------------------- #


class ModelFlagAssemblyTest(unittest.TestCase):
    def test_claude_command_pins_model(self):
        cmd = bridge.build_claude_command("roles/scout.md", "plan", True,
                                          model="claude-opus-4-8")
        i = cmd.index("--model")
        self.assertEqual(cmd[i + 1], "claude-opus-4-8")
        self.assertNotIn("--model",
                         bridge.build_claude_command("roles/scout.md",
                                                     "plan", True))

    def test_codex_command_pins_model(self):
        # Fresh AND resume both spell the pin as `-c model=...` (resume
        # rejects `-m`; one spelling keeps the turns byte-identical in intent).
        cmd = bridge.build_codex_command("p", "plan", True,
                                         model="gpt-5-codex")
        i = cmd.index('model="gpt-5-codex"')
        self.assertEqual(cmd[i - 1], "-c")
        self.assertNotIn("--model", cmd)
        self.assertNotIn('model="gpt-5-codex"',
                         bridge.build_codex_command("p", "plan", True))

    def test_codex_resume_pins_model_via_config_key(self):
        cmd = bridge.build_codex_resume_command("T1", "p", "plan", True,
                                                model="gpt-5-codex")
        i = cmd.index('model="gpt-5-codex"')
        self.assertEqual(cmd[i - 1], "-c")
        # resume rejects --model; the pin must ride -c only
        self.assertNotIn("--model", cmd)
        # no pin -> no model config key at all (implement+yolo has no -c args)
        self.assertNotIn("-c", bridge.build_codex_resume_command(
            "T1", "p", "implement", True))


class ModelConfigTest(unittest.TestCase):
    def test_model_token_sets_and_clears(self):
        config = {"scout": {"controller": "claude", "model": None,
                            "effort": None, "yolo": True,
                            "mode": "implement"}}
        ok, err = cowork.apply_config_override(
            config, "scout", ["model=claude-opus-4-8"])
        self.assertTrue(ok, err)
        self.assertEqual(config["scout"]["model"], "claude-opus-4-8")
        # `model=default` resets to the controller CLI's own default.
        ok, _ = cowork.apply_config_override(config, "scout",
                                             ["model=default"])
        self.assertTrue(ok)
        self.assertIsNone(config["scout"]["model"])
        ok, err = cowork.apply_config_override(config, "scout", ["nope=x"])
        self.assertFalse(ok)
        self.assertIn("nope", err)

    def test_config_args_parse_model_alongside_controller(self):
        config = {"scout": {"controller": "claude", "yolo": True,
                            "mode": "implement"}}
        ok, err = cowork.apply_config_args(
            config, ["scout=codex,model=gpt-5-codex"])
        self.assertTrue(ok, err)
        self.assertEqual(config["scout"]["controller"], "codex")
        self.assertEqual(config["scout"]["model"], "gpt-5-codex")

    def test_summary_shows_model_column(self):
        config = {"scout": {"controller": "claude", "model": None,
                            "effort": None, "yolo": True,
                            "mode": "implement"}}
        plain = cowork.format_config_summary(config)
        self.assertIn("model", plain)
        self.assertIn("default", plain)
        config["scout"]["model"] = "claude-opus-4-8"
        pinned = cowork.format_config_summary(config)
        self.assertIn("claude-opus-4-8", pinned)


class ModelCaptureTest(unittest.TestCase):
    def test_parse_claude_system_event_carries_model(self):
        parsed = bridge.parse_claude_event(
            {"type": "system", "subtype": "init",
             "model": "claude-opus-4-8"})
        self.assertEqual(parsed["kind"], "system")
        self.assertEqual(parsed["model"], "claude-opus-4-8")

    def test_claude_send_returns_usage_model_duration(self):
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
            json.dumps({"type": "system", "subtype": "init",
                        "model": "claude-opus-4-8"}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "ok", "session_id": "S1",
                        "usage": {"input_tokens": 12, "output_tokens": 3}}),
        ]
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)):
            s = bridge.ClaudeSession("roles/scout.md", "plan", True,
                                     io_out=io.StringIO())
            res = s.send("hello")
        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], "claude-opus-4-8")
        # The LIVE model lands on live_model; self.model stays the config pin
        # (None here — no pin was requested).
        self.assertEqual(s.live_model, "claude-opus-4-8")
        self.assertIsNone(s.model)
        self.assertEqual(res["usage"],
                         {"input_tokens": 12, "output_tokens": 3})
        self.assertIsInstance(res["duration_ms"], int)

    def test_codex_send_returns_usage_model_duration(self):
        s = bridge.CodexSession("plan", True, io_out=io.StringIO(),
                                model="gpt-5-codex")
        s._run = lambda command: [
            {"type": "thread.started", "thread_id": "T1"},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "hi"}},
            {"type": "turn.completed",
             "usage": {"input_tokens": 7, "output_tokens": 2}},
        ]
        res = s.send("hello")
        self.assertTrue(res["ok"])
        self.assertEqual(res["usage"],
                         {"input_tokens": 7, "output_tokens": 2})
        # No event named the live model -> fall back to the pinned one.
        self.assertEqual(res["model"], "gpt-5-codex")
        self.assertIsInstance(res["duration_ms"], int)

    def test_codex_event_model_wins_over_pin(self):
        s = bridge.CodexSession("plan", True, io_out=io.StringIO(),
                                model="gpt-5-codex")
        s._run = lambda command: [
            {"type": "thread.started", "thread_id": "T1",
             "model": "gpt-5.3-codex"},
            {"type": "turn.completed", "usage": {}},
        ]
        res = s.send("hello")
        self.assertEqual(res["model"], "gpt-5.3-codex")
        self.assertEqual(s.live_model, "gpt-5.3-codex")
        self.assertEqual(s.model, "gpt-5-codex")  # the pin is untouched


class RoleIdentityRegistryTest(_EvalEnvMixin, unittest.TestCase):
    def test_upsert_merges_and_never_erases_with_none(self):
        path = os.path.join(self._tmpdir(), "identities.json")
        self.assertTrue(state_store.upsert_role_identity(
            path, "scout", {"tool": "claude",
                            "model": "claude-opus-4-8",
                            "session_id": "S1"}))
        # A later observation without a model must not erase the known one.
        self.assertTrue(state_store.upsert_role_identity(
            path, "scout", {"tool": "claude", "model": None,
                            "session_id": "S2"}))
        data = state_store.read_role_identities(path)
        self.assertEqual(data["scout"]["model"], "claude-opus-4-8")
        self.assertEqual(data["scout"]["session_id"], "S2")

    def test_read_tolerates_missing_and_malformed(self):
        self.assertEqual(state_store.read_role_identities(None), {})
        self.assertEqual(state_store.read_role_identities("/nope/x.json"), {})
        path = os.path.join(self._tmpdir(), "identities.json")
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertEqual(state_store.read_role_identities(path), {})

    def test_send_registers_role_identity(self):
        d = self._tmpdir()

        class FakeSession:
            speaker = "scout"
            controller = "claude"
            model = "claude-opus-4-8"
            session_id = None
            extra_writable_dir = d

            def send(self, text):
                return {"ok": True, "result": "ok", "session_id": "SID-1"}
        cowork._send(FakeSession(), "hello")
        data = state_store.read_role_identities(
            os.path.join(d, "identities.json"))
        self.assertEqual(data["scout"], {"tool": "claude",
                                         "model": "claude-opus-4-8",
                                         "session_id": "SID-1"})

    def test_send_skips_non_roles_and_attrless_fakes(self):
        d = self._tmpdir()

        class WorktreeSession:
            speaker = "worktree"
            controller = "claude"
            extra_writable_dir = d

            def send(self, text):
                return None

        class BareFake:
            def send(self, text):
                return None
        cowork._send(WorktreeSession(), "x")
        cowork._send(BareFake(), "x")
        self.assertFalse(os.path.exists(os.path.join(d, "identities.json")))


class EvalTraceabilityStampTest(_EvalEnvMixin, unittest.TestCase):
    """scores.json entries carry evaluator+evaluatee tool/model and the eval
    turn's usage accounting."""

    def _session(self, scratch_writer, assets_dir):
        class FakeSession:
            speaker = "scout"
            controller = "claude"
            model = "claude-opus-4-8"
            session_id = "EVALSID"
            extra_writable_dir = assets_dir

            def __init__(self):
                self.io_out = io.StringIO()
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                if scratch_writer:
                    scratch_writer(text)
                return {"ok": True, "result": "ok",
                        "usage": {"input_tokens": 20, "output_tokens": 5},
                        "model": "claude-opus-4-8",
                        "session_id": "EVALSID", "duration_ms": 42}
        return FakeSession()

    def _scratch_writer(self, path):
        def write(_text):
            with open(path, "w") as fh:
                json.dump({"evaluations": [
                    {"evaluatee": "scout-reviewer",
                     "criteria": [{"name": "c1", "score": 4,
                                   "feedback": "solid"}]}]}, fh)
        return write

    def test_entries_stamped_with_identity_usage_and_verdict(self):
        self._scores_root()
        d = self._cowork_dir()
        scores_path = state_store.scores_path_for("S")
        assets_dir = os.path.dirname(scores_path)
        # The reviewer's identity was registered by its own earlier turns.
        state_store.upsert_role_identity(
            os.path.join(assets_dir, "identities.json"), "scout-reviewer",
            {"tool": "codex", "model": "gpt-5-codex", "session_id": "RSID"})
        scratch = state_store.eval_scratch_path_for(d, "scout", "S")
        sess = self._session(self._scratch_writer(scratch), assets_dir)
        fn = cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", scratch, scores_path, "S")
        fn(sess, {"verdict": "approve"}, 1)
        data = self._scores("S")
        self.assertEqual(data.get("schema"), 2)
        entry = data["evaluations"][0]
        self.assertEqual(entry["evaluator_tool"], "claude")
        self.assertEqual(entry["evaluator_model"], "claude-opus-4-8")
        self.assertEqual(entry["evaluator_session_id"], "EVALSID")
        self.assertEqual(entry["evaluatee_tool"], "codex")
        self.assertEqual(entry["evaluatee_model"], "gpt-5-codex")
        self.assertEqual(entry["evaluatee_session_id"], "RSID")
        self.assertEqual(entry["usage"],
                         {"input_tokens": 20, "output_tokens": 5})
        self.assertEqual(entry["duration_ms"], 42)
        self.assertEqual(entry["specs_in_turn"], 1)
        self.assertEqual(entry["reviewed_verdict"], "approve")
        self.assertTrue(entry["eval_turn_id"])

    def test_stale_sidecar_cleared_before_send(self):
        self._scores_root()
        d = self._cowork_dir()
        scores_path = state_store.scores_path_for("S")
        scratch = state_store.eval_scratch_path_for(d, "scout", "S")
        sidecar = cowork._eval_turn_sidecar_path(scratch)
        with open(sidecar, "w") as fh:
            fh.write('{"eval_turn_id": "STALE"}')

        class NoWriteSession:
            def __init__(self):
                self.io_out = io.StringIO()

            def send(self, text):
                return None
        fn = cowork._make_evaluate_fn(
            "scout", "scout-reviewer", "scouting", scratch, scores_path, "S")
        fn(NoWriteSession(), {"verdict": "revise"}, 1)
        # scratch never written -> nothing aggregated; the STALE sidecar was
        # cleared before the send and replaced by this turn's accounting.
        self.assertIsNone(self._scores("S"))
        with open(sidecar, "r") as fh:
            side = json.load(fh)
        self.assertNotEqual(side.get("eval_turn_id"), "STALE")

    def test_aggregate_without_sidecar_keeps_legacy_shape(self):
        self._scores_root()
        d = self._cowork_dir()
        scratch = state_store.eval_scratch_path_for(d, "scout", "S")
        self._scratch_writer(scratch)("x")
        ok = cowork._aggregate_eval(
            scratch, state_store.scores_path_for("S"), "S", "scout",
            "scouting", 1, {})
        self.assertTrue(ok)
        entry = self._scores("S")["evaluations"][0]
        for key in ("evaluator_tool", "evaluatee_tool", "usage",
                    "eval_turn_id", "reviewed_verdict"):
            self.assertNotIn(key, entry)


class ScoresReportTest(unittest.TestCase):
    def _entry(self, **over):
        entry = {
            "evaluator": "scout", "evaluatee": "scout-reviewer",
            "evaluator_tool": "claude", "evaluator_model": "opus",
            "evaluatee_tool": "codex", "evaluatee_model": "gpt-5-codex",
            "context": "review-round", "phase": "scouting", "round": 1,
            "criteria": [{"name": "accuracy", "score": 4, "feedback": ""}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "duration_ms": 100, "eval_turn_id": "T-A",
            "reviewed_verdict": "approve",
        }
        entry.update(over)
        return entry

    def test_shared_turn_usage_counted_once(self):
        scores = {"evaluations": [
            self._entry(),
            self._entry(evaluatee="scout", evaluatee_tool="claude",
                        evaluatee_model="opus", context="consumed-intel"),
            self._entry(eval_turn_id="T-B", round=2,
                        criteria=[{"name": "accuracy", "score": 2,
                                   "feedback": ""}],
                        reviewed_verdict="revise"),
        ]}
        summary = cowork_report.summarize_scores(scores)
        cost = summary["eval_cost"][("scout", "claude", "opus")]
        self.assertEqual(cost["entries"], 3)
        self.assertEqual(cost["turns"], 2)          # T-A shared by 2 entries
        self.assertEqual(cost["usage"]["input_tokens"], 20)  # 10 x 2, not x3
        self.assertEqual(cost["duration_ms"], 200)
        received = summary["received"][
            ("scout-reviewer", "codex", "gpt-5-codex")]
        self.assertEqual(received["score_count"], 2)
        self.assertEqual(received["score_total"], 6)
        self.assertEqual(received["criteria"]["accuracy"]["count"], 2)
        verdicts = summary["score_by_verdict"]
        self.assertEqual(verdicts["approve"]["total"], 4)
        self.assertEqual(verdicts["revise"]["total"], 2)

    def test_render_and_tolerance(self):
        text = cowork_report.render_scores_report(
            cowork_report.summarize_scores({"evaluations": [self._entry()]}))
        self.assertIn("codex/gpt-5-codex", text)
        self.assertIn("avg 4.00", text)
        self.assertIn("approve", text)
        empty = cowork_report.render_scores_report(
            cowork_report.summarize_scores("/nope/scores.json"))
        self.assertIn("no evaluations", empty)

    def test_trace_usage_by_role_model(self):
        events = [
            {"event": "controller.turn.start", "role": "scout",
             "controller": "claude", "prompt_bytes": 10},
            {"event": "controller.turn.end", "role": "scout",
             "controller": "claude", "model": "opus",
             "usage": {"input_tokens": 9}},
            {"event": "controller.turn.end", "role": "scout",
             "controller": "claude", "model": "opus",
             "usage": {"input_tokens": 1}},
        ]
        summary = cowork_report.summarize_trace(events)
        bucket = summary["usage_by_role_model"][("scout", "claude", "opus")]
        self.assertEqual(bucket["turns"], 2)
        self.assertEqual(bucket["usage"]["input_tokens"], 10)
        text = cowork_report.render_report(summary)
        self.assertIn("claude/opus", text)


# --------------------------------------------------------------------------- #
# Measurable success criteria: the intel contract's structural check (the      #
# auto-finding that rides the scout-reviewer brief) and the new eval criteria. #
# --------------------------------------------------------------------------- #


class SuccessCriteriaFlagTest(unittest.TestCase):
    def _intel(self, payload):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = os.path.join(d, "scout.intel.X.json")
        with open(path, "w") as fh:
            if isinstance(payload, str):
                fh.write(payload)
            else:
                json.dump(payload, fh)
        return path

    def test_flags_missing_or_empty_criteria(self):
        for payload in (
                {"status": "ready_for_review", "result": {}},
                {"status": "ready_for_review",
                 "result": {"success_criteria": []}},
                {"status": "ready_for_review",
                 "result": {"success_criteria": ["not-a-dict"]}},
                {"status": "ready_for_review"},
        ):
            flag = cowork._success_criteria_flag(self._intel(payload))
            self.assertIn("success_criteria", flag or "")

    def test_silent_on_valid_criteria_and_unreadable_intel(self):
        good = self._intel({"status": "ready_for_review", "result": {
            "success_criteria": [{
                "statement": "flag rides the brief",
                "measurement": "unit test",
                "expected": "auto-finding present",
                "tier": "must"}]}})
        self.assertIsNone(cowork._success_criteria_flag(good))
        # tolerant: unreadable/malformed intel is the normal review path's
        # problem, never a structural flag
        self.assertIsNone(cowork._success_criteria_flag("/nope/intel.json"))
        self.assertIsNone(
            cowork._success_criteria_flag(self._intel("not json")))


class StructuralFlagReachesReviewerTest(unittest.TestCase):
    """Goal criterion: intel without success_criteria reaches review -> the
    reviewer packet carries the auto-finding."""

    def _paths(self, result):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        intel = os.path.join(d, ".cowork", "scout.intel.X.json")
        review = os.path.join(d, ".cowork", "scout-review.X.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": result}, fh)
        return intel, review

    def _factory(self, seen, review):
        def factory(controller, io_out):
            class FakeRevSession:
                def send(self, text):
                    seen["prompt"] = text
                    with open(review, "w") as fh:
                        json.dump({"verdict": "approve"}, fh)

                def close(self):
                    pass
            return FakeRevSession()
        return factory

    def test_missing_criteria_intel_carries_auto_finding(self):
        intel, review = self._paths({})
        d = os.path.dirname(intel)
        trace = trace_store.Trace(os.path.join(d, "trace.jsonl"),
                                  session_uuid="X", run_id="R")
        seen = {}
        cfg = cowork.default_config(["scout", "scout-reviewer"])
        cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=self._factory(seen, review), trace=trace)
        self.assertIn("structural check", seen["prompt"])
        self.assertIn("success_criteria", seen["prompt"])
        with open(os.path.join(d, "trace.jsonl")) as fh:
            events = [json.loads(l) for l in fh if l.strip()]
        self.assertTrue(any(
            e["event"] == "review.structural_flag"
            and e["check"] == "success_criteria_missing" for e in events))

    def test_intel_with_criteria_gets_no_flag(self):
        intel, review = self._paths({"success_criteria": [{
            "statement": "s", "measurement": "m", "expected": "e",
            "tier": "must"}]})
        seen = {}
        cfg = cowork.default_config(["scout", "scout-reviewer"])
        cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=self._factory(seen, review))
        self.assertNotIn("structural check", seen["prompt"])

    def test_other_reviewers_never_flagged(self):
        # planning-advisor reviews the plan JSON — the intel contract does not
        # apply there, even when the artifact has no success_criteria.
        plan, review = self._paths({})
        seen = {}
        cfg = cowork.default_config(["planner", "planning-advisor"])
        cowork.run_reviewer_once(
            cfg, "goal", ["planner", "planning-advisor"], plan, review,
            session_factory=self._factory(seen, review),
            reviewer_role="planning-advisor")
        self.assertNotIn("structural check", seen["prompt"])


class MeasurabilityEvalCriteriaTest(unittest.TestCase):
    """Goal criterion: the eval matrix scores the scout on goal measurability
    and the planner on criteria coverage, so scores.json analytics can compare
    tool+model combos on producing measurable goals."""

    def test_matrix_carries_new_criteria(self):
        self.assertIn("goal measurability",
                      cowork.EVAL_CRITERIA[("scout-reviewer", "scout")])
        self.assertIn("criteria coverage",
                      cowork.EVAL_CRITERIA[("planning-advisor", "planner")])

    def test_eval_prompt_names_them(self):
        prompt = cowork.assemble_eval_prompt(
            "scout-reviewer", "/x/eval.json",
            [{"evaluatee": "scout",
              "criteria": cowork.EVAL_CRITERIA[("scout-reviewer", "scout")],
              "artifact_block": "INTEL"}])
        self.assertIn("goal measurability", prompt)


if __name__ == "__main__":
    unittest.main()
