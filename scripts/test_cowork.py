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
        cfg = cowork.configure_roles_interactive(
            ["scout", "planning-advisor"],
            select_fn=lambda opts, default=None, message="": "use these defaults",
            checkbox_fn=lambda *a, **k: [])
        self.assertEqual(cfg["scout"], cowork.DEFAULTS["scout"])

    def test_configure_roles_customizes(self):
        def select_fn(opts, default=None, message=""):
            if "use these defaults" in opts:        # the defaults-vs-customize gate
                return "customize"
            if message.endswith("controller"):
                return "codex"
            if message.endswith("permissions"):
                return "no-yolo"
            if message.endswith("mode"):
                return "implement"
            return default
        cfg = cowork.configure_roles_interactive(
            ["scout"], select_fn=select_fn,
            checkbox_fn=lambda msg, opts, checked=None: ["scout"])
        self.assertEqual(cfg["scout"]["controller"], "codex")
        self.assertFalse(cfg["scout"]["yolo"])
        self.assertEqual(cfg["scout"]["mode"], "implement")

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
                on_session("claude", "sess-" + os.path.basename(
                    intel_path or "x"))
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
        self.assertEqual(ts, {"kind": "thread_started", "thread_id": "T1"})
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


class AdditiveTest(unittest.TestCase):
    """cowork must stay additive: it must not import or reference the existing
    co-plan helper, and the existing files must still be present."""

    def test_cowork_does_not_import_co_plan_file(self):
        import ast
        for name in ("cowork.py", "cowork_bridge.py", "cowork_preflight.py",
                     "cowork_state.py", "cowork_ui.py"):
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


class ScoutReviewerRegistrationTest(unittest.TestCase):
    def test_role_registered_with_codex_yolo_implement(self):
        self.assertIn("scout-reviewer", cowork.ROLES)
        # placed right after scout (paired reviewer)
        self.assertEqual(cowork.ROLES.index("scout-reviewer"), 1)
        self.assertNotIn("revisor", cowork.ROLES)  # reserved slot dropped
        self.assertEqual(
            cowork.DEFAULTS["scout-reviewer"],
            {"controller": "codex", "yolo": True, "mode": "implement"})

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
        self.assertEqual(
            state_store.review_path_for(".cowork", "abc-123"),
            ".cowork/scout-review.abc-123.json")


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

    def test_missing_review_is_safe_revise_not_approve(self):
        # review_fn returns None (missing/unreadable review file) -> must be
        # treated as revise, never a silent fall-through to approval.
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        calls = {"n": 0}

        def review_fn(intel_path, round_index):
            calls["n"] += 1
            return None

        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out,
                                review_fn=review_fn)
        self.assertEqual(rc, 0)
        # round 1 None -> revise handoff injected (not the user gate)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        # cap reached -> user gate with a generic non-approval dissent
        self.assertEqual(calls["n"], cowork.REVIEW_ROUND_CAP)
        self.assertIn("reviewer did not approve", out.getvalue())

    def test_unknown_verdict_is_safe_revise(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        rfn = self._review_fn([{"verdict": "lgtm"}, {"verdict": "approve"}])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        # unknown verdict did NOT approve on round 1; it injected a revise handoff
        self.assertIn("[reviewer handoff]", sess.sent[1])

    def test_needs_user_without_question_does_not_relay_empty(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        # needs_user but empty question -> safe revise, never an empty relay
        rfn = self._review_fn([{"verdict": "needs_user", "user_question": ""},
                               {"verdict": "approve"}])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertNotIn("Question:", sess.sent[1])   # not a needs_user relay

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
    """The review gate uses an explicit confirm on a TTY (#8). ui.confirm /
    ui.prompt_user / ui.banner are patched so no real prompt/library is needed."""

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

    def test_review_confirm_approve(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "confirm", return_value=True) as conf:
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY())
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])
        conf.assert_called_once()

    def test_review_confirm_revise_then_approve(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "confirm",
                                  side_effect=[False, True]), \
                mock.patch.object(cowork.ui, "prompt_user",
                                  return_value="please tweak X"):
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY())
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "please tweak X"])

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
                mock.patch.object(cowork.ui, "confirm", return_value=True):
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
                mock.patch.object(cowork.ui, "confirm", return_value=True):
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
                mock.patch.object(cowork.ui, "confirm", return_value=True):
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
                mock.patch.object(cowork.ui, "confirm") as conf:
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY(),
                                    review_fn=rfn)
        self.assertEqual(rc, 0)
        # approved straight from the dissent gate: no extra reviewer rounds,
        # no plain confirm gate
        self.assertEqual(rfn.calls["n"], cap)
        self.assertEqual(len(sess.sent), cap)  # seed + (cap-1) revise handoffs
        conf.assert_not_called()


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
        self.assertEqual(
            state_store.planner_plan_json_path_for(".cowork", "abc"),
            ".cowork/planner.plan.abc.json")
        self.assertEqual(
            state_store.planner_plan_md_path_for(".cowork", "abc"),
            ".cowork/planner.plan.abc.md")
        self.assertEqual(
            state_store.planner_review_path_for(".cowork", "abc"),
            ".cowork/planner-review.abc.json")

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
            {"controller": "codex", "yolo": True, "mode": "implement"})

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
        self.assertIn('"k": 1', seed)
        self.assertIn("the goal", seed)

    def test_intel_updated_block_carries_intel(self):
        intel = self._intel('{"result": {"new": true}}')
        block = cowork.intel_updated_block(intel)
        self.assertIn("intel changed", block)
        self.assertIn('"new": true', block)

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
                             "scout.intel.%s.json" % suid)
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
                             "scout.intel.S.json")
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
        self.assertIn('"finding": "F1"', seed)
        self.assertIn("build the thing", seed)
        # planner artifacts named by the session uuid
        self.assertIn("planner.plan.S.json", calls["planner"][0]["plan_json_path"])
        self.assertIn("planner.plan.S.md", calls["planner"][0]["plan_md_path"])
        self.assertIn("planner-review.S.json", calls["planner"][0]["review_path"])
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
                             "scout.intel.S.json")
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
                             "scout.intel.S.json")
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
                             "scout.intel.S.json")
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
        self.assertIn('"finding": "F1"', seed)

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
        self.assertEqual(
            state_store.eval_scratch_path_for("/tmp/.cowork", "scout", "S1"),
            "/tmp/.cowork/eval.scout.S1.json")

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
        # first turn: bundled prompt names both evaluatees + embeds the intel
        self.assertIn("Evaluatee: planning-advisor", sess.sent[0])
        self.assertIn("Evaluatee: scout", sess.sent[0])
        self.assertIn("F-INTEL", sess.sent[0])
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
        # the ->scout spec embeds the intel JSON read at eval time
        scout_spec = runner.seen[0]["eval_specs"][1]
        self.assertIn("F-INTEL", scout_spec["artifact_block"])
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
            {"controller": "codex", "yolo": True, "mode": "implement"})
        self.assertEqual(
            cowork.DEFAULTS["builder"],
            {"controller": "claude", "yolo": True, "mode": "implement"})

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
        self.assertEqual(
            state_store.build_status_path_for(".cowork", "abc"),
            ".cowork/builder.status.abc.json")
        self.assertEqual(
            state_store.build_review_path_for(".cowork", "abc"),
            ".cowork/builder-review.abc.json")

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
        self.assertIn('"k": 1', seed)
        self.assertIn("# THE PLAN", seed)
        self.assertIn("the goal", seed)

    def test_plan_updated_block_carries_plan(self):
        pj, pm = self._plan('{"result": {"new": true}}', "# UPDATED")
        block = cowork.plan_updated_block(pj, pm)
        self.assertIn("plan changed", block)
        self.assertIn('"new": true', block)
        self.assertIn("# UPDATED", block)

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
        with open(os.path.join(base, "scout.intel.%s.json" % suid), "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        with open(os.path.join(base, "planner.plan.%s.json" % suid), "w") as fh:
            json.dump({"status": "ready_for_review",
                       "result": {"step": "S1"}}, fh)
        with open(os.path.join(base, "planner.plan.%s.md" % suid), "w") as fh:
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
        self.assertIn('"step": "S1"', seed)
        self.assertIn("# PLAN MD", seed)
        self.assertIn("do it", seed)
        # builder artifacts named by the session uuid
        self.assertIn("builder.status.S.json",
                      calls["builder"][0]["build_status_path"])
        self.assertIn("builder-review.S.json",
                      calls["builder"][0]["build_review_path"])
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
        self.assertIn('"step": "S1"', seed)


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
        self.assertIn("G-PLAN", spec["artifact_block"])
        self.assertIn("# PLAN-MD-MARKER", spec["artifact_block"])
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
        self.assertIn("G-PLAN", sess.sent[0])
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
        out = FakeTTY()
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


if __name__ == "__main__":
    unittest.main()
