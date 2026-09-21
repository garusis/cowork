#!/usr/bin/env python3
"""Run explicit unittest ids behind the offline provider barrier (test only).

    python3 scripts/cowork_offline_tests.py test_cowork.SomeTest.test_x \
        test_m2_negative_controls [--timeout 1800] [--scratch-base DIR]

Layers, all built fresh in a mkdtemp scratch directory outside the repo:

1. Child environment: an allowlisted base (credentials, GIT_*, COWORK_LIVE
   and provider config variables are dropped; HOME is passed through
   unchanged), a rebuilt PATH of ``stubs:tools:/usr/bin:/bin:/usr/sbin:/sbin``,
   TMPDIR and COWORK_SESSIONS_ROOT (plus the ingest roots) inside scratch.
   ``stubs`` holds deny stubs ``claude``/``codex``/``opencode`` that append to
   the sentinel and exit 97; ``tools`` holds exec shims for this Python and git.
2. ``sitecustomize.py`` on PYTHONPATH installs cowork_offline_guard in the test
   Python and every child Python (see that module for what it blocks).
3. Preflight: before any test-under-test runs, a probe in the same environment
   attempts bare, absolute, renamed, wrapped, shell, exec/spawn, launcher and
   child-Python launches of inert temporary executables. Every one must be
   refused, none may run, and an allowed temp fake plus git must still run.
4. Gate: PASS only if preflight passed, unittest exited 0, it did not time out,
   and the sentinel is empty. Any sentinel line fails the gate even when the
   test caught the exception.

Output (bounded): ``summary.json``, ``unittest.log`` (capped) and the sentinel
files stay in the scratch directory, whose path is printed. Exit codes: 0 pass,
1 tests failed, 2 usage, 3 provider boundary violation (or sentinel missing),
4 setup/preflight failed, 5 timeout or output pipe held open after exit.

Git runs with child-only config isolation (GIT_CONFIG_GLOBAL=/dev/null,
GIT_CONFIG_NOSYSTEM=1, a fixed test identity, signing off); user and global
config files are never written. HOME is deliberately NOT redirected: host
files outside the guarded provider credential paths remain readable, so
results can still depend on the host.

Run the self-tests (test_cowork_offline_guard) PLAIN, never through this
harness: the outer guard re-injects its own config into the inner harness,
whose preflight then reports into the outer sentinel and fails closed.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import cowork_offline_guard as guard

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPTS_DIR)
SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
ENV_ALLOW = ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TZ")
TEST_ID = re.compile(r"^test_[A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
TAIL_LINES = 80
SENTINEL_SHOWN = 20

SITECUSTOMIZE = '''\
import importlib.machinery, importlib.util, os, sys
_here = os.path.dirname(os.path.realpath(__file__))
try:
    sys.path.insert(0, _here)
    import cowork_offline_guard
    cowork_offline_guard.install()
    sys.path.remove(_here)
except BaseException as _exc:
    # site.py swallows sitecustomize errors; an unguarded Python must not run.
    try:
        sys.stderr.write("offline test barrier: guard install failed: %r\\n"
                         % (_exc,))
        sys.stderr.flush()
    finally:
        os._exit(70)
for _p in sys.path:
    if os.path.realpath(_p or ".") == _here:
        continue
    _spec = importlib.machinery.PathFinder.find_spec("sitecustomize", [_p])
    if _spec is not None:
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        break
'''

STUB = '''#!/bin/sh
printf '{"stub": "%s", "argc": %s}\\n' "${0##*/}" "$#" >> '__SENTINEL__'
exit 97
'''

INERT = '''#!/bin/sh
printf 'ran %s\\n' "${0##*/}" >> '__HIT__'
exit 97
'''

# Runs inside the barrier environment. Each attempt must raise
# ProviderLaunchBlocked; if the guard were absent the inert executable would
# run, write the hit file and exit 97, which the runner detects.
PROBE = r'''
import json, os, subprocess, sys, tempfile
bin_dir, link_dir = sys.argv[1], sys.argv[2]
out = {"guard_loaded": "cowork_offline_guard" in sys.modules and
       sys.modules["cowork_offline_guard"].installed(), "attempts": {},
       "allowed": {}}
claude = os.path.join(bin_dir, "claude")
codex = os.path.join(bin_dir, "codex")
opencode = os.path.join(bin_dir, "opencode")
helper = os.path.join(bin_dir, "helper")
CHILD = ("import subprocess, os\n"
         "for k in [k for k in os.environ if k.startswith('COWORK_OFFLINE')]:\n"
         "    del os.environ[k]\n"
         "try:\n    subprocess.run([%r])\n    print('ran')\n"
         "except BaseException as e:\n    print(type(e).__name__)\n" % claude)
def attempt(name, fn):
    try:
        fn()
        out["attempts"][name] = "not_blocked"
    except BaseException as e:
        out["attempts"][name] = type(e).__name__
attempt("bare_name", lambda: subprocess.run(["claude", "--version"]))
attempt("absolute", lambda: subprocess.run([claude]))
attempt("executable_kw", lambda: subprocess.run(["x"], executable=codex))
attempt("renamed_symlink", lambda: subprocess.run([os.path.join(link_dir, "tool")]))
attempt("env_wrapper", lambda: subprocess.run(["/usr/bin/env", "A=1", codex]))
attempt("env_split_unsupported", lambda: subprocess.run(["env", "-S", "true"]))
attempt("sandbox_wrapper", lambda: subprocess.run(
    ["sandbox-exec", "-p", "(version 1)", "/usr/bin/env", "T=1", opencode]))
attempt("bwrap_no_separator", lambda: subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"]))
attempt("shell_true", lambda: subprocess.run("opencode --help", shell=True))
attempt("sh_c_absolute", lambda: subprocess.run(["/bin/sh", "-c", "exec " + claude]))
attempt("sh_stdin", lambda: subprocess.run(["/bin/sh"], input=b"true\n"))
attempt("script_mentions", lambda: subprocess.run([helper]))
attempt("npx_launcher", lambda: subprocess.run([os.path.join(bin_dir, "npx"), "x"]))
attempt("unknown_wrapper_abs", lambda: subprocess.run([os.path.join(bin_dir, "wrap"), claude]))
attempt("env_unknown_option", lambda: subprocess.run(["env", "--bogus", "x", codex]))
attempt("python_isolated", lambda: subprocess.run([sys.executable, "-I", "-c", "pass"]))
attempt("python_W_then_isolated", lambda: subprocess.run(
    [sys.executable, "-W", "ignore", "-E", "-c", "pass"]))
attempt("credential_open", lambda: open(os.path.expanduser(
    "~/.codex/cowork-offline-probe-nonexistent")))
attempt("os_system", lambda: os.system("codex"))
attempt("os_execv", lambda: os.execv(claude, [claude]))
attempt("os_execvp_env", lambda: os.execvpe("codex", ["codex"], {"PATH": bin_dir}))
attempt("posix_spawn", lambda: os.posix_spawn(opencode, [opencode], dict(os.environ)))
child = subprocess.run([sys.executable, "-c", CHILD], env={"LANG": "C"},
                       stdout=subprocess.PIPE, text=True)
out["attempts"]["child_python_env_scrubbed"] = child.stdout.strip() or (
    "rc=%s" % child.returncode)
fake_dir = tempfile.mkdtemp()
fake = os.path.join(fake_dir, "claude")
with open(fake, "w") as fh:
    fh.write("#!/bin/sh\nexit 0\n")
os.chmod(fake, 0o755)
out["allowed"]["temp_fake_controller"] = subprocess.run([fake]).returncode
out["allowed"]["git"] = subprocess.run(["git", "--version"],
                                      stdout=subprocess.DEVNULL).returncode
out["allowed"]["sh_c"] = subprocess.run(["/bin/sh", "-c", "true"]).returncode
signing = subprocess.run(["git", "config", "--get", "commit.gpgsign"],
                         stdout=subprocess.PIPE, text=True, env={})
out["allowed"]["git_signing_off_even_with_empty_env"] = (
    0 if signing.stdout.strip() == "false" else "got %r" % signing.stdout)
print(json.dumps(out))
'''
BLOCKED_PROBES = 23


def _write_exec(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o755)


def _inside_repo(path):
    real, repo = os.path.realpath(path), os.path.realpath(REPO_ROOT)
    return real == repo or real.startswith(repo + os.sep)


def validate_ids(ids, tests_dir):
    errors = []
    for test_id in ids:
        if not TEST_ID.match(test_id) or test_id.endswith(".py"):
            errors.append("unsupported test id %r (use module[.Class[.test]])"
                          % test_id)
        elif not os.path.isfile(os.path.join(
                tests_dir, test_id.split(".")[0] + ".py")):
            errors.append("no module for %r in %s" % (test_id, tests_dir))
    return errors


def prepare(scratch_base=None):
    """Build the scratch layout and the barrier environment."""
    if _inside_repo(scratch_base or tempfile.gettempdir()):
        raise ValueError("scratch directory must be outside the repository")
    scratch = tempfile.mkdtemp(prefix="cwo-", dir=scratch_base)
    if _inside_repo(scratch):
        shutil.rmtree(scratch)
        raise ValueError("scratch directory must be outside the repository")
    d = {k: os.path.join(scratch, k) for k in (
        "stubs", "tools", "tmp", "sessions", "guard", "probe_bin",
        "probe_links", "claude_projects", "codex_sessions")}
    for path in d.values():
        os.mkdir(path)
    d["scratch"] = scratch
    d["sentinel"] = os.path.join(scratch, "provider-sentinel.jsonl")
    d["hit"] = os.path.join(scratch, "probe-hit.txt")
    d["log"] = os.path.join(scratch, "unittest.log")
    d["summary"] = os.path.join(scratch, "summary.json")
    open(d["sentinel"], "w").close()
    for name in guard.PROVIDERS:
        _write_exec(os.path.join(d["stubs"], name),
                    STUB.replace("__SENTINEL__", d["sentinel"]))
    for name in guard.PROVIDERS + ("npx",):
        _write_exec(os.path.join(d["probe_bin"], name),
                    INERT.replace("__HIT__", d["hit"]))
    _write_exec(os.path.join(d["probe_bin"], "wrap"), '#!/bin/sh\nexec "$@"\n')
    _write_exec(os.path.join(d["probe_bin"], "helper"),
                "#!/bin/sh\nclaude --version\n")
    os.symlink(os.path.join(d["probe_bin"], "claude"),
               os.path.join(d["probe_links"], "tool"))
    shutil.copy(os.path.join(SCRIPTS_DIR, "cowork_offline_guard.py"),
                d["guard"])
    with open(os.path.join(d["guard"], "sitecustomize.py"), "w") as fh:
        fh.write(SITECUSTOMIZE)

    shims = {"python3": sys.executable, "python": sys.executable}
    git = shutil.which("git")
    if git:
        shims["git"] = git
    for name, target in shims.items():
        _write_exec(os.path.join(d["tools"], name),
                    '#!/bin/sh\nexec "%s" "$@"\n' % target)
    for sysdir in SYSTEM_DIRS:
        for name in guard.PROVIDERS:
            if os.path.lexists(os.path.join(sysdir, name)):
                raise RuntimeError("provider %s found in system dir %s"
                                   % (name, sysdir))

    env = {k: v for k, v in os.environ.items()
           if k in ENV_ALLOW or k.startswith("LC_")}
    env.update({
        "PATH": os.pathsep.join((d["stubs"], d["tools"]) + SYSTEM_DIRS),
        "TMPDIR": d["tmp"],
        "COWORK_SESSIONS_ROOT": d["sessions"],
        "COWORK_CLAUDE_PROJECTS_ROOT": d["claude_projects"],
        "COWORK_CODEX_SESSIONS_ROOT": d["codex_sessions"],
        "PYTHONPATH": d["guard"],
        guard.ENV_SENTINEL: d["sentinel"],
        guard.ENV_STUB_DIR: d["stubs"],
        guard.ENV_ALLOW_ROOT: d["tmp"],
        guard.ENV_GUARD_DIR: d["guard"],
    })
    env.update(guard.GIT_ENV)
    return d, env


def _sentinel_lines(path):
    """Non-empty lines, or None when the file cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]
    except OSError:
        return None


def check_missing_config_exits(d):
    """A Python that gets the guard on PYTHONPATH without its config must
    exit 70 before running any code."""
    proc = subprocess.run(
        [sys.executable, "-c", "print('ran')"], env={"PYTHONPATH": d["guard"]},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    return proc.returncode, proc.stdout


def run_preflight(d, env):
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, d["probe_bin"], d["probe_links"]],
        env=env, cwd=d["tmp"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=120)
    result = {"ok": False, "returncode": proc.returncode,
              "stderr_tail": proc.stderr[-2000:]}
    try:
        probe = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result["error"] = "probe produced no result"
        return result
    lines = _sentinel_lines(d["sentinel"]) or []
    hits = _sentinel_lines(d["hit"]) or []
    attempts = probe["attempts"]
    problems = []
    code, out = check_missing_config_exits(d)
    if (code, out) != (70, ""):
        problems.append("child without guard config returned %s, stdout %r"
                        % (code, out[:200]))
    if not probe["guard_loaded"]:
        problems.append("guard not loaded in test Python")
    problems += ["%s: %s" % (k, v) for k, v in attempts.items()
                 if v != "ProviderLaunchBlocked"]
    if len(attempts) != BLOCKED_PROBES:
        problems.append("expected %d probes, got %d"
                        % (BLOCKED_PROBES, len(attempts)))
    if len(lines) != BLOCKED_PROBES:
        problems.append("sentinel has %d entries, expected %d"
                        % (len(lines), BLOCKED_PROBES))
    if hits:
        problems.append("inert executables ran: %s" % hits)
    problems += ["allowed launch %s returned %s" % (k, v)
                 for k, v in probe["allowed"].items() if v != 0]
    result.update(ok=not problems and proc.returncode == 0, probe=probe,
                  problems=problems)
    os.replace(d["sentinel"], os.path.join(d["scratch"],
                                           "preflight-sentinel.jsonl"))
    open(d["sentinel"], "w").close()
    return result


PUMP_JOIN_S = 10


def _pump(stream, log_path, max_bytes, state):
    written = 0
    with stream, open(log_path, "wb") as log:
        for chunk in iter(lambda: stream.read(65536), b""):
            state["tail"] = (state["tail"] + chunk)[-65536:]
            if written < max_bytes:
                log.write(chunk[:max_bytes - written])
                written += len(chunk[:max_bytes - written])
            else:
                state["truncated"] = True


def run(ids, tests_dir=SCRIPTS_DIR, timeout=1800, scratch_base=None,
        max_log_bytes=5 << 20, pump_join=PUMP_JOIN_S):
    """Run the barrier and the given test ids. Returns (exit_code, summary)."""
    errors = validate_ids(ids, tests_dir)
    if not ids:
        errors.append("give at least one explicit test module or id")
    if errors:
        return 2, {"gate": "usage", "errors": errors}
    d = None
    summary = {"ids": list(ids), "tests_dir": tests_dir,
               "python": sys.executable}
    try:
        d, env = prepare(scratch_base)
        summary.update(scratch=d["scratch"], summary=d["summary"],
                       log=d["log"])
        summary["preflight"] = run_preflight(d, env)
    except Exception as exc:
        summary["setup_error"] = "%s: %s" % (type(exc).__name__, exc)
        return _finish(4, "setup_failed", summary, d)
    if not summary["preflight"]["ok"]:
        return _finish(4, "preflight_failed", summary, d)

    started = time.time()
    proc = subprocess.Popen(
        [sys.executable, "-m", "unittest", "-v"] + list(ids), cwd=tests_dir,
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True)
    state = {"tail": b"", "truncated": False}
    pump = threading.Thread(target=_pump, daemon=True,
                            args=(proc.stdout, d["log"], max_log_bytes, state))
    pump.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    # Reap anything left in the unittest process group, finished or not.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()
    pump.join(timeout=pump_join)
    # A detached descendant still holding stdout keeps the pump blocked; the
    # daemon thread cannot hang exit, but the run is not clean.
    pump_stalled = pump.is_alive()

    tail = state["tail"].decode("utf-8", "replace").splitlines()[-TAIL_LINES:]
    lines = _sentinel_lines(d["sentinel"])
    summary.update(
        returncode=proc.returncode, timed_out=timed_out,
        pump_stalled=pump_stalled,
        duration_s=round(time.time() - started, 1),
        log_truncated=state["truncated"],
        result_lines=[l for l in tail if re.match(
            r"^(Ran \d+ tests?|OK|FAILED)", l)],
        tail=tail, sentinel_entries=None if lines is None else len(lines),
        sentinel_head=[l[:500] for l in (lines or [])[:SENTINEL_SHOWN]])
    if lines is None:
        return _finish(3, "sentinel_missing", summary, d)
    if lines:
        return _finish(3, "provider_boundary_violation", summary, d)
    if timed_out:
        return _finish(5, "timeout", summary, d)
    if pump_stalled:
        return _finish(5, "output_pipe_held_open", summary, d)
    if proc.returncode != 0:
        return _finish(1, "tests_failed", summary, d)
    return _finish(0, "pass", summary, d)


def _finish(code, gate, summary, d):
    summary.update(gate=gate, exit_code=code)
    if d is not None:
        with open(d["summary"], "w") as fh:
            json.dump(summary, fh, indent=2)
    return code, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("ids", nargs="*",
                        help="unittest ids: test_module[.Class[.method]]")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--scratch-base", default=None,
                        help="parent dir for scratch (default: system temp)")
    parser.add_argument("--max-log-bytes", type=int, default=5 << 20)
    args = parser.parse_args(argv)
    code, summary = run(args.ids, timeout=args.timeout,
                        scratch_base=args.scratch_base,
                        max_log_bytes=args.max_log_bytes)
    brief = {k: summary.get(k) for k in (
        "gate", "exit_code", "errors", "setup_error", "result_lines",
        "sentinel_entries", "timed_out", "pump_stalled", "summary", "log")}
    if summary.get("preflight") and not summary["preflight"]["ok"]:
        brief["preflight_problems"] = summary["preflight"].get(
            "problems") or summary["preflight"]
    print(json.dumps({k: v for k, v in brief.items() if v is not None},
                     indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
