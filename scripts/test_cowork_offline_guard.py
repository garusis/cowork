"""Self-tests for the offline provider barrier.

Uses only inert temporary executables (they write a local file and exit 97)
and tiny temporary unittest modules. Never runs a real provider, the network,
or the cowork suites. Run PLAIN (python3 -m unittest test_cowork_offline_guard),
never through cowork_offline_tests.py: nesting fails closed by design.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest

import cowork_offline_guard as guard
import cowork_offline_tests as harness


def _exe(path, text="#!/bin/sh\nexit 97\n"):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o755)
    return path


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.stubs, self.allow, self.outside = (
            os.path.join(self.root, n) for n in ("stubs", "allow", "outside"))
        for path in (self.stubs, self.allow, self.outside):
            os.mkdir(path)
        _exe(os.path.join(self.stubs, "claude"))
        self.real = _exe(os.path.join(self.outside, "claude"))
        self.fake = _exe(os.path.join(self.allow, "claude"), "#!/bin/sh\n")
        self.config = {"stub_dir": self.stubs, "allow_root": self.allow}
        self.env = {"PATH": self.stubs + os.pathsep + "/usr/bin:/bin"}

    def decide(self, argv, **kw):
        kw.setdefault("env", self.env)
        return guard.decide(argv, self.config, **kw)

    def test_refuses_provider_forms(self):
        cases = {
            "bare": (["claude", "-p"], {}),
            "absolute": ([self.real], {}),
            "executable_kw": (["git"], {"executable": self.real}),
            "env": (["/usr/bin/env", "A=1", "-u", "B", self.real], {}),
            "sandbox": (["sandbox-exec", "-p", "(version 1)", "env",
                         "TMPDIR=/x", "claude"], {}),
            "bwrap": (["bwrap", "--ro-bind", "/", "/", "--", self.real], {}),
            "nice_timeout": (["nice", "-n", "5", "timeout", "9", self.real],
                             {}),
            "shell_true": ("exec codex", {"shell": True}),
            "sh_c": (["/bin/bash", "-lc", "cd /x && %s" % self.real], {}),
            "npx": (["npx", "@anthropic-ai/claude-code"], {}),
            "python_I": (["python3", "-I", "-c", "pass"], {}),
            "env_S": (["env", "-S", "claude -p"], {}),
            "bwrap_no_sep": (["bwrap", "--ro-bind", "/", "/", "true"], {}),
            "sh_stdin": (["/bin/sh", "-s"], {}),
            "unknown_provider": (["opencode"], {"env": {"PATH": "/nonexistent"}}),
            "env_unknown_valued": (["env", "-a", "x", self.real], {}),
            "env_bsd_L": (["/usr/bin/env", "-L", "x", "/bin/true"], {}),
            "nice_unknown": (["nice", "-5", "/bin/true"], {}),
            "timeout_unknown": (["timeout", "--bogus", "5", "/bin/true"], {}),
            "unknown_wrapper_abs": (["caffeinate", "-i", self.real], {}),
            "stdbuf_relative": (["stdbuf", "-o0", "outside/claude"],
                                {"cwd": self.root}),
            "python_W_I": (["python3", "-W", "ignore", "-I", "-c", "1"], {}),
            "python_X_E": (["python3", "-X", "utf8", "-E", "-c", "1"], {}),
            "python_cluster": (["python3", "-bS", "-c", "1"], {}),
            "keychain": (["/usr/bin/security", "find-generic-password"], {}),
        }
        for name, (argv, kw) in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(self.decide(argv, **kw))

    def test_renamed_symlink_and_provider_mentioning_script(self):
        link = os.path.join(self.outside, "tool")
        os.symlink(self.real, link)
        self.assertIsNotNone(self.decide([link]))
        script = _exe(os.path.join(self.outside, "run.sh"),
                      "#!/bin/sh\ncodex exec hi\n")
        self.assertIsNotNone(self.decide([script]))

    def test_allows_fixtures_git_and_plain_shells(self):
        for argv in ([self.fake], ["git", "status"], ["/bin/sh", "-c", "true"],
                     ["/bin/sh", "-c", "sleep 600"],
                     ["python3", "-c", "print('claude')"],
                     ["python3", "-W", "ignore", "-Xutf8", "-c", "1"],
                     ["python3", "-c", "-I"],
                     ["env", "-i", "-u", "X", "A=1", "/bin/sh", "-c", "echo ok"],
                     ["timeout", "--foreground", "-k", "1", "5", "/bin/sh",
                      "-c", "true"],
                     ["nice", "-n", "5", "/bin/sh", "-c", "true"]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.decide(argv))

    def test_oversized_provider_named_file_in_temp_root_is_refused(self):
        big = os.path.join(self.allow, "codex")
        with open(big, "wb") as fh:
            fh.write(b"#!/bin/sh\n" + b"#" * (guard.FAKE_MAX_BYTES + 1))
        self.assertIsNotNone(self.decide([big]))

    def test_inject_env_keeps_barrier_config(self):
        config = dict(self.config, sentinel="/s", guard_dir="/g")
        env = guard.inject_env({"PATH": "/usr/bin", "PYTHONPATH": "/p"},
                               config)
        self.assertEqual(env["PATH"].split(os.pathsep)[0], self.stubs)
        self.assertEqual(env["PYTHONPATH"], "/g" + os.pathsep + "/p")
        for key in guard.CONFIG_VARS:
            self.assertIn(key, env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_VALUE_2"], "false")


# Runs in an inert child Python with the inherited HOME untouched: installs the
# guard, extends its credential roots with a scratch directory holding a
# provider-like ``.codex`` fixture (keeping every real root), then exercises
# pathlib and os file APIs. Denial probes touch only the scratch fixture.
PATHLIB_CHILD = r"""
import json, os, sys
order, guard_dir, root, scratch_credentials_dir = sys.argv[1:5]
if order == "before":
    import pathlib
sys.path.insert(0, guard_dir)
import cowork_offline_guard as guard
guard.install()
import pathlib
out = {}
real_roots = guard.credential_roots(os.path.expanduser("~"))
installed_roots = tuple(guard._config["credential_roots"])
scratch_roots = guard.credential_roots(scratch_credentials_dir)
guard._config["credential_roots"] = installed_roots + scratch_roots
out["real_roots_retained"] = (
    installed_roots == real_roots and len(real_roots) > 0
    and guard._config["credential_roots"][:len(real_roots)] == real_roots)
def attempt(name, fn):
    try:
        out[name] = ["ok", fn()]
    except BaseException as exc:
        out[name] = [type(exc).__name__, str(exc)]
work = pathlib.Path(root, "work")
attempt("mkdir", lambda: work.mkdir())
attempt("mkdir_parents", lambda: str((work / "a" / "b").mkdir(parents=True)))
attempt("write_text", lambda: (work / "f.txt").write_text("hi"))
attempt("read_text", lambda: (work / "f.txt").read_text())
attempt("write_bytes", lambda: (work / "g.bin").write_bytes(b"x"))
attempt("touch", lambda: (work / "t").touch() or (work / "t").exists())
attempt("exists_ok", lambda: work.mkdir(exist_ok=True))
def os_calls():
    fd = os.open(path=str(work / "k"), flags=os.O_CREAT | os.O_WRONLY,
                 mode=0o600)
    os.close(fd)
    os.mkdir(path=str(work / "kd"), mode=0o700)
    dfd = os.open(str(work), os.O_RDONLY)
    try:
        os.mkdir("viafd", 0o755, dir_fd=dfd)
        os.close(os.open("viafd/x", os.O_CREAT | os.O_WRONLY, 0o644,
                         dir_fd=dfd))
    finally:
        os.close(dfd)
    return sorted(os.listdir(str(work)))
attempt("os_keywords_and_dir_fd", os_calls)
class Holder:
    open = os.open
    mkdir = os.mkdir
def holder():
    Holder().mkdir(str(work / "held"))
    os.close(Holder().open(str(work / "held" / "x"), os.O_CREAT | os.O_WRONLY))
    return True
attempt("class_attribute_not_bound", holder)
cred = pathlib.Path(scratch_credentials_dir, ".codex", "probe")
attempt("cred_write_text", lambda: cred.write_text("x"))
attempt("cred_read_text", lambda: cred.read_text())
attempt("cred_mkdir", lambda: cred.mkdir())
attempt("cred_touch", lambda: cred.touch())
attempt("cred_os_open", lambda: os.open(str(cred), os.O_CREAT | os.O_WRONLY))
attempt("cred_os_mkdir", lambda: os.mkdir(path=str(cred)))
attempt("bad_args_still_type_error", lambda: os.open(str(work / "z")))
out["cred_created"] = os.path.lexists(str(cred))
print(json.dumps(out))
"""


class PathlibCompatibilityTests(unittest.TestCase):
    """Python 3.9 pathlib stores os.open/os.mkdir as accessor class
    attributes; the guard wrappers must work and stay enforced there whether
    pathlib is imported before or after install."""

    def run_child(self, order):
        root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        scratch_credentials_dir = os.path.join(root, "scratch_credentials")
        stubs = os.path.join(root, "stubs")
        for path in (scratch_credentials_dir, stubs,
                     os.path.join(scratch_credentials_dir, ".codex")):
            os.mkdir(path)
        sentinel = os.path.join(root, "sentinel.jsonl")
        open(sentinel, "w").close()
        env = {"PATH": "/usr/bin:/bin", "LANG": "C",
               guard.ENV_SENTINEL: sentinel, guard.ENV_STUB_DIR: stubs,
               guard.ENV_ALLOW_ROOT: root,
               guard.ENV_GUARD_DIR: os.path.dirname(guard.__file__)}
        if "HOME" in os.environ:
            env["HOME"] = os.environ["HOME"]
        proc = subprocess.run(
            [sys.executable, "-c", PATHLIB_CHILD, order,
             os.path.dirname(os.path.abspath(guard.__file__)), root,
             scratch_credentials_dir],
            env=env, cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(sentinel) as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        return json.loads(proc.stdout), entries

    def test_pathlib_imported_before_and_after_install(self):
        allowed = ("mkdir", "mkdir_parents", "write_text", "read_text",
                   "write_bytes", "touch", "exists_ok",
                   "os_keywords_and_dir_fd", "class_attribute_not_bound")
        denied = ("cred_write_text", "cred_read_text", "cred_mkdir",
                  "cred_touch", "cred_os_open", "cred_os_mkdir")
        for order in ("before", "after"):
            with self.subTest(order=order):
                out, entries = self.run_child(order)
                for name in allowed:
                    self.assertEqual(out[name][0], "ok", (name, out[name]))
                self.assertEqual(out["read_text"][1], "hi")
                self.assertEqual(out["touch"][1], True)
                self.assertEqual(out["os_keywords_and_dir_fd"][1],
                                 ["a", "f.txt", "g.bin", "k", "kd", "t",
                                  "viafd"])
                for name in denied:
                    self.assertEqual(out[name][0], "ProviderLaunchBlocked",
                                     (name, out[name]))
                self.assertEqual(out["bad_args_still_type_error"][0],
                                 "TypeError")
                self.assertIs(out["real_roots_retained"], True)
                self.assertFalse(out["cred_created"])
                self.assertEqual(len(entries), len(denied))
                scratch_codex = os.path.join("scratch_credentials", ".codex")
                self.assertTrue(all("credential path" in e["reason"]
                                    and scratch_codex in e["reason"]
                                    for e in entries), entries)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base)
        self.tests_dir = os.path.join(self.base, "suite")
        os.mkdir(self.tests_dir)

    def module(self, name, body):
        with open(os.path.join(self.tests_dir, name + ".py"), "w") as fh:
            fh.write(textwrap.dedent(body))

    def run_harness(self, ids, **kw):
        kw.setdefault("timeout", 120)
        return harness.run(ids, tests_dir=self.tests_dir,
                           scratch_base=self.base, **kw)

    def test_preflight_blocks_every_probe_before_tests(self):
        d, env = harness.prepare(self.base)
        result = harness.run_preflight(d, env)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["probe"]["attempts"]),
                         harness.BLOCKED_PROBES)
        self.assertFalse(os.path.exists(d["hit"]))
        self.assertEqual(os.path.getsize(d["sentinel"]), 0)

    def test_preflight_fails_when_guard_is_disabled(self):
        # Negative control: every probe target is inert, so a no-op guard is
        # safe to exercise and must be detected.
        d, env = harness.prepare(self.base)
        with open(os.path.join(d["guard"], "sitecustomize.py"), "w") as fh:
            fh.write("pass\n")
        result = harness.run_preflight(d, env)
        self.assertFalse(result["ok"], result)
        self.assertTrue(os.path.getsize(d["hit"]) > 0)

    def test_child_python_without_config_exits_70_before_user_code(self):
        d, _env = harness.prepare(self.base)
        self.assertEqual(harness.check_missing_config_exits(d), (70, ""))

    def test_timeout_kills_the_run(self):
        self.module("test_slow", """
            import time, unittest
            class T(unittest.TestCase):
                def test_slow(self):
                    time.sleep(60)
        """)
        code, summary = self.run_harness(["test_slow"], timeout=2)
        self.assertEqual((code, summary["gate"]), (5, "timeout"), summary)
        self.assertLess(summary["duration_s"], 30)

    def test_detached_pipe_holder_fails_gate_without_hanging(self):
        self.module("test_holder", """
            import os, subprocess, unittest
            class T(unittest.TestCase):
                def test_leak(self):
                    p = subprocess.Popen(["/bin/sleep", "30"],
                                         start_new_session=True)
                    with open(os.path.join(os.environ["TMPDIR"],
                                           "holder.pid"), "w") as fh:
                        fh.write(str(p.pid))
        """)
        code, summary = self.run_harness(["test_holder"], pump_join=1)
        pid_file = os.path.join(summary["scratch"], "tmp", "holder.pid")
        with open(pid_file) as fh:
            pid = int(fh.read())
        self.addCleanup(os.kill, pid, signal.SIGKILL)
        self.assertEqual(summary["returncode"], 0)
        self.assertEqual((code, summary["gate"]),
                         (5, "output_pipe_held_open"), summary)

    def test_missing_sentinel_fails_gate(self):
        self.module("test_rm", """
            import os, unittest
            class T(unittest.TestCase):
                def test_rm(self):
                    os.remove(os.environ["COWORK_OFFLINE_SENTINEL"])
        """)
        code, summary = self.run_harness(["test_rm"])
        self.assertEqual((code, summary["gate"]), (3, "sentinel_missing"))

    def test_setup_failure_is_structured(self):
        self.module("test_ok", "")
        code, summary = harness.run(["test_ok"], tests_dir=self.tests_dir,
                                    scratch_base=harness.SCRIPTS_DIR)
        self.assertEqual((code, summary["gate"]), (4, "setup_failed"))
        self.assertIn("ValueError", summary["setup_error"])

    def test_swallowed_escape_still_fails_gate(self):
        self.module("test_escape", """
            import os, subprocess, unittest
            class T(unittest.TestCase):
                def test_catches_everything(self):
                    stub = os.path.join(os.environ["COWORK_OFFLINE_STUB_DIR"],
                                        "claude")
                    for argv in (["codex", "exec"], [stub]):
                        try:
                            subprocess.run(argv)
                        except BaseException:
                            pass
        """)
        code, summary = self.run_harness(["test_escape"])
        self.assertEqual(code, 3, summary)
        self.assertEqual(summary["returncode"], 0)
        self.assertEqual(summary["sentinel_entries"], 2)
        self.assertTrue(os.path.isfile(summary["summary"]))

    def test_non_python_child_hitting_stub_fails_gate(self):
        # The guard allows a temp-root script; the bare name inside it resolves
        # to the deny stub, which records the attempt itself.
        self.module("test_stub", """
            import os, subprocess, tempfile, unittest
            class T(unittest.TestCase):
                def test_script(self):
                    path = os.path.join(tempfile.mkdtemp(), "run")
                    with open(path, "w") as fh:
                        fh.write("#!/bin/sh\\nname=clau; ${name}de -p\\n")
                    os.chmod(path, 0o755)
                    self.assertEqual(subprocess.run([path]).returncode, 97)
        """)
        code, summary = self.run_harness(["test_stub"])
        self.assertEqual(code, 3, summary)
        self.assertIn('"stub": "claude"', summary["sentinel_head"][0])

    def test_pass_and_failure_are_reported_unchanged(self):
        self.module("test_ok", """
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    pass
                @unittest.skip("kept visible")
                def test_skip(self):
                    pass
        """)
        self.module("test_bad", """
            import unittest
            class T(unittest.TestCase):
                def test_bad(self):
                    self.fail("real failure")
        """)
        code, summary = self.run_harness(["test_ok"])
        self.assertEqual((code, summary["gate"]), (0, "pass"), summary)
        self.assertIn("OK (skipped=1)", summary["result_lines"])
        code, summary = self.run_harness(["test_bad.T.test_bad"])
        self.assertEqual((code, summary["gate"]), (1, "tests_failed"))
        self.assertTrue(any(l.startswith("FAILED")
                            for l in summary["result_lines"]))

    def test_rejects_unsupported_ids_without_running(self):
        self.module("test_ok", "")
        for ids in ([], ["discover"], ["../test_ok"], ["test_ok.py"],
                    ["test_missing"], ["-k", "x"]):
            with self.subTest(ids=ids):
                code, summary = self.run_harness(ids)
                self.assertEqual(code, 2)
                self.assertNotIn("scratch", summary)

    def test_scratch_inside_repo_is_refused(self):
        with self.assertRaises(ValueError):
            harness.prepare(harness.SCRIPTS_DIR)


if __name__ == "__main__":
    unittest.main()
