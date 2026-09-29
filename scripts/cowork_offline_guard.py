"""In-process provider-launch guard for OFFLINE test runs (test harness only).

Loaded into every test Python through a generated ``sitecustomize.py`` on
PYTHONPATH (see cowork_offline_tests.py). Never imported by product code.

It wraps the stdlib launch points ``subprocess.Popen._execute_child``,
``os.execv``/``os.execve`` (which every ``os.exec*`` variant reaches),
``os.posix_spawn``/``os.posix_spawnp`` and ``os.system``. Before a launch it
unwraps known prefixes (``env``, ``sandbox-exec``, ``bwrap … --``, ``nice``,
``nohup``, ``timeout``) and refuses:

* an executable whose given name, symlink target name, or nearby target
  directory names are ``claude``/``codex``/``opencode``, unless it is a small
  file under the harness temp root (the test-created fake controllers);
* package launchers and JS runtimes (``npx``, ``node``, …) outright;
* shells given ``-c`` text or scripts that mention a provider name, and shells
  reading a script from stdin or from outside the temp root. One bounded
  exception, for Claude's per-call working-directory file: in ``-c`` text a
  whole word matching the closed ``/tmp/claude-<4-5 hex>-cwd`` grammar below,
  with an output-redirect operator immediately in front of it, is a write
  target rather than the program the shell runs. Command position, input
  redirects, process substitutions, quoted targets, other path shapes and
  every other provider token stay refused, and the exception does not extend
  to shell *script files*, whose contents keep the plain-token rule;
* Python children started with ``-I``/``-E``/``-S`` (they would skip this
  guard);
* wrapper options it does not know (anything beyond the listed flags of
  ``env``, ``sandbox-exec``, ``nice``, ``timeout``; ``bwrap`` without ``--``);
* any argv element that is an absolute or relative path to an executable
  file with a provider name outside the temp root, so unknown wrappers
  (``caffeinate``, ``arch``, ``stdbuf``, ``time``) cannot pass one through;
* ``security`` (macOS keychain CLI, where Claude keeps its login).

It also refuses Python ``open``/``os.open``/``os.mkdir`` of the host provider
credential/config locations (``~/.claude``, ``~/.claude.json``, ``~/.codex``,
``~/.config/opencode``, ``~/.local/share/opencode``, ``~/.opencode``). HOME
itself is NOT redirected, so other host files stay visible. The ``os.open`` and
``os.mkdir`` wrappers are non-binding callables, like the builtins they
replace, because Python 3.9 ``pathlib._NormalAccessor`` stores them as class
attributes; an accessor imported before install is re-pointed at them.

A refusal appends one JSON line to the sentinel file and raises
``ProviderLaunchBlocked`` (a ``BaseException``), so ``except Exception``
handlers cannot swallow it; it is raised even if the sentinel write fails.
The sentinel records the attempt even if a test catches the exception; the
outer runner fails the gate on any entry.

Every launch gets this guard's PYTHONPATH, config, Git config isolation and
stub directory re-injected into its environment, so child Pythons inherit the
guard.

Known false positives (fail closed): executables whose name or one of the
last three real-path components contains a provider token (for example a
repo ``.claude/hooks/x`` script or a ``codex-runtimes`` directory) outside the
temp root, and shell text or scripts that merely mention a provider name
(apart from the one ``-c`` redirection target described above; a script file
holding that same text is still refused).
Stubs always go first in PATH, so a bare-name fake ``claude`` placed on PATH
by a test resolves to the deny stub; call fakes by absolute path.

Boundary: this stops accidental launches by ordinary test and product code.
It is not a sandbox against deliberately hostile code (ctypes, direct
``_posixsubprocess`` calls, sqlite/C-level file access, obfuscated shell text,
or a non-Python child that builds a provider path at runtime and ignores the
stub PATH). What bounds the cwd-file exception is its grammar and the
redirect operator in front of the word, not what is on disk: like every other
filesystem lookup here, its stat of the target is taken before the launch and
can be raced.
"""

import builtins
import io
import json
import os
import re
import shutil
import subprocess
import sys

_POPEN_ARGS = list(__import__("inspect").signature(
    subprocess.Popen._execute_child).parameters)[1:]
PROVIDERS = ("claude", "codex", "opencode")
ENV_SENTINEL = "COWORK_OFFLINE_SENTINEL"
ENV_STUB_DIR = "COWORK_OFFLINE_STUB_DIR"
ENV_ALLOW_ROOT = "COWORK_OFFLINE_ALLOW_ROOT"
ENV_GUARD_DIR = "COWORK_OFFLINE_GUARD_DIR"
CONFIG_VARS = (ENV_SENTINEL, ENV_STUB_DIR, ENV_ALLOW_ROOT, ENV_GUARD_DIR)

LAUNCHERS = {"npx", "npm", "pnpm", "pnpx", "yarn", "bun", "bunx", "deno",
             "node", "nodejs", "uvx", "pipx", "sudo", "doas", "xargs",
             "script", "osascript", "open", "launchctl", "expect", "security"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
CREDENTIAL_PATHS = (".claude", ".claude.json", ".codex", ".config/opencode",
                    ".local/share/opencode", ".opencode")
# Child-only Git isolation: no user/system config, fixed identity, no signing.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_COUNT": "4",
           "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "cowork-offline",
           "GIT_CONFIG_KEY_1": "user.email",
           "GIT_CONFIG_VALUE_1": "offline@cowork.invalid",
           "GIT_CONFIG_KEY_2": "commit.gpgsign", "GIT_CONFIG_VALUE_2": "false",
           "GIT_CONFIG_KEY_3": "tag.gpgsign", "GIT_CONFIG_VALUE_3": "false"}
FAKE_MAX_BYTES = 1 << 20
_TOKEN = re.compile(r"(?<![A-Za-z0-9])(claude|codex|opencode)(?![A-Za-z0-9])",
                    re.IGNORECASE)
_PYTHON = re.compile(r"^python(\d+(\.\d+)*)?$", re.IGNORECASE)
# Claude writes the working directory of each call to /tmp/claude-<4 hex>-cwd
# and the darwin write boundary grants exactly that path (product behaviour of
# the claude controller; see kernel_write_boundary claude_cwd_tracker). The
# grammar is closed: the product shape, plus only the concrete near misses the
# seatbelt boundary test has to execute (a fifth hex digit, and the tails "x",
# ".x", "/x", "-evil"). It admits no "..", no extra path segment and no other
# punctuation, so a traversal-shaped word is rejected by its structure rather
# than by a tail-length budget.
_CWD_TRACKER_WORD = re.compile(
    r"^/(?:private/)?tmp/claude-[0-9A-Fa-f]{4,5}-cwd(?:x|\.x|/x|-evil)?$")
# Words are the runs between shell separators. Every separator character is
# non-alphanumeric, so splitting on them leaves _TOKEN's own boundaries -- and
# therefore its verdict on the text as a whole -- unchanged.
_SHELL_SPLIT = re.compile(r"([\s;|&<>()\[\]{}$`\"'=!*?~#\\]+)")
# A separator that ends in an output-redirect operator (">", ">>", ">|", ">!",
# optionally "&>"), followed by blanks only, so the next word is the file the
# redirect writes. "<", "<>", "<<<" and ">(" cannot reach the final ">", and a
# closing quote or any other character after the operator breaks the match, so
# input redirects, process substitutions and quoted targets stay refused.
_OUTPUT_REDIRECT = re.compile(r"(?:\A|[\s;&|(){}])>[>|!]?[ \t]*\Z")

_config = None
_orig = {}


class ProviderLaunchBlocked(BaseException):
    """A real provider launch was refused. Deliberately not an Exception."""


class _Unbound(object):
    """Forward every call to ``func``. Having no ``__get__``, it is not bound
    as a method when stored as a class attribute (``open = os.open`` in 3.9
    pathlib), matching the builtin it replaces."""

    def __init__(self, func):
        self.__wrapped__ = func
        self.__name__ = func.__name__
        self.__qualname__ = func.__qualname__
        self.__doc__ = func.__doc__

    def __call__(self, *args, **kwargs):
        return self.__wrapped__(*args, **kwargs)

    def __repr__(self):
        return "<offline guard %s>" % self.__name__


# --------------------------------------------------------------------------- #
# Decision (pure apart from filesystem lookups)                               #
# --------------------------------------------------------------------------- #

def _names(path):
    real = os.path.realpath(path)
    parts = [os.path.basename(path)] + real.split(os.sep)[-3:]
    return [p for p in parts if p]


def _names_provider(names):
    return any(_TOKEN.search(n) for n in names)


def _under(path, root):
    real = os.path.realpath(path)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _resolve(program, env, cwd):
    if os.sep in program:
        return program if os.path.isabs(program) else os.path.join(
            cwd or os.getcwd(), program)
    return shutil.which(program, path=os.pathsep.join(
        os.get_exec_path(env)))


def _script_mentions_provider(path):
    try:
        if os.path.getsize(path) > FAKE_MAX_BYTES:
            return False
        with open(path, "rb") as fh:
            head = fh.read(2)
            if head != b"#!":
                return False
            return bool(_TOKEN.search(fh.read().decode("utf-8", "replace")))
    except OSError:
        return False


def _cwd_tracker_target(word, separator):
    """True for a whole word in Claude's cwd-file namespace that this shell
    text redirects output into.

    The bound is structural: the closed grammar of _CWD_TRACKER_WORD plus an
    output-redirect operator immediately in front of the word, which is what
    makes it the file the redirect writes rather than the program the shell
    runs. The executable-file check is only a backstop for a path that should
    hold text -- it is a stat, it can be raced, and neither the grammar nor
    the redirect anchor depends on it.
    """
    if not _OUTPUT_REDIRECT.search(separator):
        return False
    if not _CWD_TRACKER_WORD.match(word):
        return False
    return not (os.path.isfile(word) and os.access(word, os.X_OK))


def _shell_text_mentions_provider(text):
    """Provider tokens in ``sh -c`` text. Splitting on shell separators leaves
    detection unchanged -- they are already token boundaries -- and lets the
    cwd-file allowance see a whole word plus the separator before it. Index 0
    is a word with no separator before it, i.e. command position, which no
    output-redirect operator can precede."""
    parts = _SHELL_SPLIT.split(text)
    for index, word in enumerate(parts):
        if index % 2 or not _TOKEN.search(word):
            continue
        if _cwd_tracker_target(word, parts[index - 1] if index else ""):
            continue
        return True
    return False


def _strict_opts(tool, args, flags, valued, assignments=False):
    """Skip only known options; refuse anything else (fail closed)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:], None
        if a in flags:
            i += 1
        elif a in valued:
            i += 2
        elif a.startswith("--") and a.split("=", 1)[0] in valued and "=" in a:
            i += 1
        elif a.startswith("-"):
            return None, "unsupported %s option %s" % (tool, a)
        elif assignments and "=" in a and not a.startswith("="):
            i += 1
        else:
            break
    return args[i:], None


def _unwrap(argv):
    """Return (inner_argv, reason). reason set means refuse; inner False means
    argv[0] is not a known wrapper."""
    base = os.path.basename(argv[0])
    rest = list(argv[1:])
    if base == "env":
        return _strict_opts(
            "env", rest, {"-i", "-0", "-v", "--ignore-environment", "--null"},
            {"-u", "--unset", "-C", "--chdir"}, assignments=True)
    if base == "sandbox-exec":
        return _strict_opts("sandbox-exec", rest, set(),
                            {"-f", "-n", "-p", "-D"})
    if base == "bwrap":
        if "--" not in rest:
            return None, "bwrap without -- separator"
        return rest[rest.index("--") + 1:], None
    if base == "nohup":
        return _strict_opts("nohup", rest, set(), set())
    if base == "nice":
        return _strict_opts("nice", rest, set(), {"-n", "--adjustment"})
    if base == "timeout":
        inner, why = _strict_opts(
            "timeout", rest, {"-v", "--verbose", "--foreground",
                              "--preserve-status"},
            {"-s", "-k", "--signal", "--kill-after"})
        return (None, why) if why else (inner[1:], None)
    return False, None


def _provider_path_arg(argv, cwd, config):
    """First argv element naming an executable provider file outside the temp
    root (covers unknown wrappers such as caffeinate/arch/stdbuf/time)."""
    for a in argv:
        if os.sep not in a:
            continue
        path = a if os.path.isabs(a) else os.path.join(cwd or os.getcwd(), a)
        if (os.path.isfile(path) and os.access(path, os.X_OK)
                and _names_provider(_names(path))
                and not _under(path, config["allow_root"])):
            return os.path.realpath(path)
    return None


def decide(argv, config, executable=None, env=None, cwd=None, shell=False):
    """Return None when the launch is allowed, else a refusal reason."""
    if isinstance(argv, (str, bytes, os.PathLike)):
        argv = [argv]
    argv = [os.fsdecode(a) for a in argv]
    if shell:
        argv = ["/bin/sh", "-c"] + argv
    if executable is not None:
        argv = [os.fsdecode(executable)] + argv[1:]
    for _ in range(8):
        if not argv:
            return "empty command after wrapper"
        inner, why = _unwrap(argv)
        if why:
            return why
        if inner is False:
            break
        argv = inner
    else:
        return "wrapper nesting too deep"
    program = argv[0]
    base = os.path.basename(program)
    smuggled = _provider_path_arg(argv, cwd, config)
    if smuggled:
        return "provider executable in argv %s" % smuggled
    if base in LAUNCHERS:
        return "package launcher/runtime %s is not supported offline" % base
    path = _resolve(program, env, cwd)
    if path is None:
        return ("provider name %s" % base) if _TOKEN.search(base) else None
    names = _names(path)
    real = os.path.realpath(path)
    if os.path.basename(real) in LAUNCHERS:
        return "package launcher/runtime %s is not supported offline" % real
    if _names_provider(names):
        if _under(real, config["stub_dir"]):
            return "provider %s resolved to deny stub" % base
        if not _under(real, config["allow_root"]):
            return "provider executable %s" % real
        try:
            if os.path.getsize(real) > FAKE_MAX_BYTES:
                return "provider-named binary too large for a fake: %s" % real
        except OSError:
            pass
        return None
    if base in SHELLS or os.path.basename(real) in SHELLS:
        return _decide_shell(argv[1:], config)
    if _PYTHON.match(base) or real == os.path.realpath(sys.executable):
        args = argv[1:]
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--" or not a.startswith("-") or a.startswith("--"):
                break
            flags = a[1:]
            cut = min([flags.index(c) for c in "cmWX" if c in flags]
                      or [len(flags)])
            if set(flags[:cut]) & set("IES"):
                return "python flag %s would bypass the guard" % a
            if cut < len(flags) and flags[cut] in "cm":
                break
            # -W/-X take a value, joined ("-Xutf8") or as the next argument.
            i += 2 if cut == len(flags) - 1 else 1
        return None
    if _script_mentions_provider(real):
        return "script %s mentions a provider" % real
    return None


def _decide_shell(args, config):
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c" or (a.startswith("-") and not a.startswith("--")
                         and "c" in a[1:]):
            text = args[i + 1] if i + 1 < len(args) else ""
            if _shell_text_mentions_provider(text):
                return "shell -c text mentions a provider"
            return None
        if a in ("-s", "-i") or a == "-":
            return "shell reading commands from stdin"
        if a in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if not a.startswith("-") and not a.startswith("+"):
            if not _under(a, config["allow_root"]):
                return "shell script outside temp root: %s" % a
            if _script_mentions_provider(a) or _TOKEN.search(
                    _read_small(a)):
                return "shell script %s mentions a provider" % a
            return None
        i += 1
    return "shell reading commands from stdin"


def _read_small(path):
    try:
        if os.path.getsize(path) > FAKE_MAX_BYTES:
            return ""
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# Installation                                                                #
# --------------------------------------------------------------------------- #

def load_config(environ):
    missing = [k for k in CONFIG_VARS if not environ.get(k)]
    if missing:
        raise RuntimeError("offline guard missing config: %s"
                           % ", ".join(missing))
    return {"sentinel": environ[ENV_SENTINEL],
            "stub_dir": os.path.realpath(environ[ENV_STUB_DIR]),
            "allow_root": os.path.realpath(environ[ENV_ALLOW_ROOT]),
            "guard_dir": environ[ENV_GUARD_DIR]}


def inject_env(env, config):
    """Copy of env that keeps child Pythons and shells behind the barrier."""
    env = dict(env)
    env[ENV_SENTINEL] = config["sentinel"]
    env[ENV_STUB_DIR] = config["stub_dir"]
    env[ENV_ALLOW_ROOT] = config["allow_root"]
    env[ENV_GUARD_DIR] = config["guard_dir"]
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(
        [config["guard_dir"]] + [p for p in parts if p != config["guard_dir"]])
    paths = [p for p in env.get("PATH", os.defpath).split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join(
        [config["stub_dir"]] + [p for p in paths if p != config["stub_dir"]])
    if "GIT_CONFIG_COUNT" not in env:
        env.update(GIT_ENV)
    env["GIT_CONFIG_GLOBAL"] = GIT_ENV["GIT_CONFIG_GLOBAL"]
    env["GIT_CONFIG_NOSYSTEM"] = GIT_ENV["GIT_CONFIG_NOSYSTEM"]
    return env


def _record_and_raise(api, argv, reason):
    try:
        if not isinstance(argv, (list, tuple)):
            argv = [argv]
        shown = [os.fsdecode(a)[:200] for a in argv[:20]]
        line = json.dumps({"pid": os.getpid(), "api": api, "reason": reason,
                           "argv": shown}) + "\n"
        fd = _orig["os_open"](_config["sentinel"],
                              os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except BaseException as exc:  # never let a logging failure unblock
        reason = "%s (sentinel write failed: %r)" % (reason, exc)
        try:
            sys.stderr.write("offline test barrier: %s\n" % reason)
        except BaseException:
            pass
    raise ProviderLaunchBlocked("offline test barrier: %s" % reason)


def _check(api, argv, **kw):
    reason = decide(argv, _config, **kw)
    if reason:
        _record_and_raise(api, argv, reason)


def credential_roots(home):
    return tuple(os.path.realpath(os.path.join(home, p))
                 for p in CREDENTIAL_PATHS)


def _check_path(api, path, dir_fd=None):
    if isinstance(path, int) or dir_fd is not None:
        return
    try:
        real = os.path.realpath(os.fsdecode(path))
    except (TypeError, ValueError):
        return
    for root in _config["credential_roots"]:
        if _under(real, root):
            _record_and_raise(api, [real], "provider credential path %s"
                              % root)


def install(environ=None):
    """Install the wrappers once. Config is captured now, so later test edits
    to os.environ cannot switch the guard off."""
    global _config
    if _config is not None:
        return
    config = load_config(os.environ if environ is None else environ)
    config["credential_roots"] = credential_roots(os.path.expanduser("~"))
    _orig.update(execute_child=subprocess.Popen._execute_child,
                 execv=os.execv, execve=os.execve, system=os.system,
                 os_open=os.open, open=builtins.open, mkdir=os.mkdir,
                 posix_spawn=getattr(os, "posix_spawn", None),
                 posix_spawnp=getattr(os, "posix_spawnp", None))
    _config = config
    os.environ.update(inject_env(os.environ, _config))

    def _execute_child(self, args, executable, preexec_fn, close_fds,
                       pass_fds, cwd, env, *rest):
        shell = rest[_POPEN_ARGS.index("shell") - 7]
        child_env = inject_env(os.environ if env is None else env, _config)
        _check("subprocess.Popen", args, executable=executable,
               env=child_env, cwd=cwd, shell=shell)
        return _orig["execute_child"](self, args, executable, preexec_fn,
                                      close_fds, pass_fds, cwd, child_env,
                                      *rest)

    def execv(path, args):
        env = inject_env(os.environ, _config)
        _check("os.execv", [path] + list(args)[1:], env=env)
        return _orig["execve"](path, args, env)

    def execve(path, args, env):
        env = inject_env(env, _config)
        _check("os.execve", [path] + list(args)[1:], env=env)
        return _orig["execve"](path, args, env)

    def system(command):
        os.environ.update(inject_env(os.environ, _config))
        _check("os.system", command, shell=True, env=os.environ)
        return _orig["system"](command)

    def guarded_open(file, *args, **kwargs):
        _check_path("open", file)
        return _orig["open"](file, *args, **kwargs)

    def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        _check_path("os.open", path, dir_fd)
        return _orig["os_open"](path, flags, mode, dir_fd=dir_fd)

    def guarded_mkdir(path, mode=0o777, *, dir_fd=None):
        _check_path("os.mkdir", path, dir_fd)
        return _orig["mkdir"](path, mode, dir_fd=dir_fd)

    subprocess.Popen._execute_child = _execute_child
    os.execv, os.execve, os.system = execv, execve, system
    builtins.open = io.open = guarded_open
    os.open, os.mkdir = _Unbound(guarded_os_open), _Unbound(guarded_mkdir)
    # pathlib imported before install captured the unguarded builtins.
    accessor = getattr(sys.modules.get("pathlib"), "_NormalAccessor", None)
    for name, key, wrapper in (("open", "os_open", os.open),
                               ("mkdir", "mkdir", os.mkdir)):
        if accessor is not None and vars(accessor).get(name) is _orig[key]:
            setattr(accessor, name, wrapper)

    if _orig["posix_spawn"] is not None:
        def posix_spawn(path, argv, env, **kw):
            env = inject_env(env, _config)
            _check("os.posix_spawn", [path] + list(argv)[1:], env=env)
            return _orig["posix_spawn"](path, argv, env, **kw)
        os.posix_spawn = posix_spawn
    if _orig["posix_spawnp"] is not None:
        def posix_spawnp(file, argv, env, **kw):
            env = inject_env(env, _config)
            _check("os.posix_spawnp", [file] + list(argv)[1:], env=env)
            return _orig["posix_spawnp"](file, argv, env, **kw)
        os.posix_spawnp = posix_spawnp


def installed():
    return _config is not None
