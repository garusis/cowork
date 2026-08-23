#!/usr/bin/env python3
"""cowork controller bridges.

Three controller back-ends spin up a role's CLI and bridge its conversation to
the user:

- claude: a persistent duplex process driven by stream-json on stdin/stdout.
- codex: one-shot `codex exec --json` plus `codex exec resume <thread_id>` for
  each follow-up turn (codex exec has no persistent duplex stdin).
- opencode: one-shot `opencode run --format json` per turn, resumed with
  `--session <id>`. The role prompt is delivered as an opencode agent file
  (`.opencode/agents/cowork-<role>.md`) regenerated on every spawn, so the
  system prompt and per-mode permissions always match the current config.

The command-assembly, message-framing, event-parsing, and probe logic are pure
functions so they can be unit-tested with fakes; only the thin `*_spawn` drivers
touch real subprocesses.

The `--input-format stream-json` stdin schema is officially undocumented
(anthropics/claude-code issue #24594). `probe_claude_stream_json` confirms the
installed claude accepts our shape before any real turn, so no unverified shape
is baked in silently.

Python 3.9+, stdlib only.
"""

import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_ui as ui  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_probe_cache as probe_cache  # noqa: E402
# The session controller policy. Every entry point below that creates a process
# or writes provider-specific setup calls `policy.guard(...)` as its FIRST
# statement, so a disallowed controller can never be started — not even for a
# probe or an agent-file write — regardless of which call site was used.
import cowork_policy as policy  # noqa: E402
import cowork_action_policy as action_policy  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_guard_broker as guard_broker  # noqa: E402
import cowork_profiles as controller_profiles  # noqa: E402
# Re-exported so existing callers/tests keep using bridge.USER_LABEL /
# bridge.speaker_label; the canonical definitions live in cowork_ui.
from cowork_ui import USER_LABEL, speaker_label  # noqa: E402,F401

DEFAULT_ROLE_PROMPT = "roles/scout.md"
_NESTED_GUARD_ACTIVE = False

# ORCH-001: cowork roles never inherit MCP servers. For claude this is a
# cowork-owned static empty MCP config plus the `--mcp-config <path>
# --strict-mcp-config` flag pair ("Only use MCP servers from --mcp-config,
# ignoring all other MCP configurations" — verified against claude 2.1.220).
# A committed static file (not a per-launch generated one) because the
# unguarded probe has no session/guard directory to generate into, and a
# stable `__file__`-relative path keeps golden argv assertions deterministic.
CLAUDE_EMPTY_MCP_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data",
    "claude_empty_mcp.json")

# The spinner moved to cowork_ui (both bridges + the loop share it). Alias kept
# for back-compat.
_Spinner = ui.Spinner


def set_nested_guard_active(value):
    global _NESTED_GUARD_ACTIVE
    prior = _NESTED_GUARD_ACTIVE
    _NESTED_GUARD_ACTIVE = bool(value)
    return prior


def nested_guard_active():
    return _NESTED_GUARD_ACTIVE


def turn_result(ok=True, result="ok", **fields):
    """Small structured send result consumed by the cowork orchestrator."""
    out = {"ok": bool(ok), "result": result}
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def _terminate(proc):
    """Best-effort: stop a spawned CLI so it is not left running after an
    interrupt. Tries SIGTERM, then SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception:  # noqa: BLE001 - never raise from cleanup
        pass

# --------------------------------------------------------------------------- #
# Command assembly (verified flags, see the signed-off plan D3/D4 + Mode map). #
# --------------------------------------------------------------------------- #


def claude_mode_flags(mode, yolo):
    """Permission/mode flags for claude given (mode, yolo)."""
    if mode == "plan":
        # Plan mode is read-only regardless of the yolo toggle.
        return ["--permission-mode", "plan"]
    # implement
    if yolo:
        return ["--dangerously-skip-permissions"]
    # yolo off: auto-approve edits + common fs commands; anything else is denied
    # and surfaced as an error (no interactive approval relay in v1).
    return ["--permission-mode", "acceptEdits"]


def codex_mode_flags(mode, yolo):
    """Sandbox flags for codex given (mode, yolo).

    `codex exec` is already non-interactive (it never prompts for approval), so
    approval policy is governed entirely by the sandbox — there is no
    `--ask-for-approval` flag on the exec subcommand (verified against codex-cli
    0.133.0; passing it errors).
    """
    if mode == "plan":
        return ["--sandbox", "read-only"]
    # implement
    if yolo:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return ["--sandbox", "workspace-write"]


def build_claude_command(role_prompt_file, mode, yolo, session_id=None,
                         resume_id=None, extra_writable_dir=None,
                         model=None, effort=None, guard_settings_path=None,
                         delegation_allowed=True):
    """Full argv for a persistent duplex claude scout process.

    Pass `session_id` to pin a known UUID on a fresh session (so it can be saved
    and resumed later), or `resume_id` to continue a saved session.
    `extra_writable_dir`, when set, is granted as an additional writable root
    via `--add-dir` so a no-yolo (acceptEdits) role can write its session
    artifacts even though they live outside cwd. Re-applied on every spawn
    (fresh AND resume), so resumed Claude roles keep the grant.
    `model`/`effort`, when set, pin the session model (`--model`) and thinking
    effort (`--effort`: low|medium|high|xhigh|max); unset means the installed
    CLI's own defaults, exactly as before."""
    cmd = [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",  # stream tokens as they are generated
        "--replay-user-messages",
        # Interactive question/plan tools auto-return "skipped" in headless -p and
        # break the clarify loop; the role asks via text + status=needs_input.
        "--disallowedTools",
        "AskUserQuestion",
        "ExitPlanMode",
        "--append-system-prompt-file",
        role_prompt_file,
        # ORCH-001: load ONLY the cowork-owned empty MCP config — no
        # user/project/plugin MCP server schema ever enters a role's context.
        # Unconditional: the probe (guarded and unguarded) and every
        # ClaudeSession fresh/resume launch all inherit it from here.
        "--mcp-config",
        CLAUDE_EMPTY_MCP_CONFIG_PATH,
        "--strict-mcp-config",
    ] + claude_mode_flags(mode, yolo)
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if guard_settings_path:
        # Keep the authenticated profile for Keychain lookup while excluding
        # its user/project/local customizations. The explicit Cowork settings
        # file below is still loaded and remains the only hook source.
        cmd += ["--setting-sources", "",
                "--settings", guard_settings_path]
    if not delegation_allowed:
        # Both names are needed across the Agent/legacy Task CLI transition.
        cmd += ["--disallowedTools", "Agent", "Task"]
    if extra_writable_dir:
        cmd += ["--add-dir", extra_writable_dir]
    if resume_id:
        cmd += ["--resume", resume_id]
    elif session_id:
        cmd += ["--session-id", session_id]
    return cmd


def guard_settings_document(guard_script, socket_path=None,
                            context_path=None):
    """Claude settings with catch-all interception and lifecycle hooks."""
    command = [sys.executable, guard_script]
    if context_path:
        command += ["--context", context_path]
    hook = {"type": "command",
            "command": " ".join(shlex.quote(part) for part in command),
            "timeout": 5}
    # PreToolUse intentionally has no matcher: unknown/plugin/MCP tools must
    # reach the broker too.
    return {"hooks": {
        "PreToolUse": [{"hooks": [hook]}],
        "PostToolUse": [{"hooks": [hook]}],
        "SubagentStart": [{"hooks": [hook]}],
        "SubagentStop": [{"hooks": [hook]}],
    }}


def write_guard_settings(path, guard_script, socket_path=None,
                         context_path=None):
    doc = guard_settings_document(
        guard_script, socket_path=socket_path, context_path=context_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def _declared_outputs_for_role(assets_dir, role):
    names = {
        "scout": ("scout.intel.json", "scout.intel.md"),
        "scout-reviewer": ("scout-review.json",),
        "planner": ("planner.plan.json", "planner.plan.md"),
        "planning-advisor": ("planner-review.json",),
        "builder": ("builder.status.json", "builder.summary.md"),
        "build-reviewer": ("builder-review.json",),
        "worktree": ("worktree.status.json",),
    }.get(role)
    if names is None:
        # Evaluators write only their own role-named scratch artifact.
        names = ("eval.%s.json" % role,)
    return tuple(os.path.join(assets_dir, name) for name in names)


def _git_worktree_scope(cwd):
    """Return active root and its registered sibling worktrees, or fail closed."""
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
        if top.returncode != 0 or not top.stdout.strip():
            raise RuntimeError("git_toplevel_unavailable")
        active = os.path.realpath(top.stdout.strip())
        listed = subprocess.run(
            ["git", "-C", active, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10)
        if listed.returncode != 0:
            raise RuntimeError("git_worktree_inventory_unavailable")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError("git_worktree_inventory_unavailable") from exc
    roots = []
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            root = os.path.realpath(line[len("worktree "):].strip())
            if root and root != active:
                roots.append(root)
    return active, tuple(dict.fromkeys(roots))


def migrate_legacy_claude_resume(controller_state, resume_id,
                                 legacy_root=None):
    """Copy a pre-isolation Claude transcript into the private state root.

    The relative project layout is preserved because Claude uses that layout
    when resolving ``--resume``. Ambiguous legacy ids fail closed.
    """
    if not resume_id:
        return None
    projects = os.path.join(controller_state, "projects")
    wanted = str(resume_id) + ".jsonl"
    for base, _dirs, files in os.walk(projects):
        if wanted in files:
            return os.path.join(base, wanted)
    legacy_projects = legacy_root or os.path.join(
        os.path.expanduser("~"), ".claude", "projects")
    matches = []
    for base, _dirs, files in os.walk(legacy_projects):
        if wanted in files:
            matches.append(os.path.join(base, wanted))
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("legacy_resume_ambiguous")
    relative = os.path.relpath(matches[0], legacy_projects)
    target = os.path.join(projects, relative)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(matches[0], target)
    return target


def _guard_runtime(trace, role, assets_dir, model, effort,
                   delegation_allowed, declared_outputs=None, resume_id=None,
                   repo_writable=True, controller="claude",
                   controller_session_id=None):
    session_uuid = getattr(trace, "session_uuid", None)
    if not (session_uuid and assets_dir):
        raise RuntimeError("guard_context_unavailable")
    if sys.platform.startswith("linux"):
        raise RuntimeError(
            "controller_state_unavailable: isolated %s credentials "
            "were not provisioned" % controller)
    if controller not in ("claude", "codex", "opencode"):
        raise RuntimeError("guard_controller_unsupported")
    if controller == "opencode":
        # opencode has no PreToolUse hook transport, so there is no broker,
        # guard settings, or context file to provision — the runtime is the
        # kernel write boundary alone (ORCH-053: the seatbelt backstops the
        # injectable bash glob allowlist in the agent frontmatter). Its
        # controller state is the REAL data dir: auth.json and the sqlite
        # session store live there and cannot be redirected or symlinked
        # per role, so the boundary grants it instead.
        guard_dir = state_store.guard_dir_for(session_uuid)
        role_temp = os.path.join(guard_dir, "tmp", role)
        os.makedirs(role_temp, exist_ok=True)
        controller_state = controller_profiles.opencode_data_dir()
        os.makedirs(controller_state, exist_ok=True)
        active_root, sibling_worktrees = _git_worktree_scope(os.getcwd())
        if role == "evaluator":
            repo_writable = False
        # The opencode CLI writes its own project bookkeeping
        # (`.opencode/.gitignore`) at startup and dies if refused, so the
        # cowork-generated `.opencode` dir stays writable even when the
        # repository itself is read-only. It holds only generated agent
        # files — never user code.
        opencode_project_dir = os.path.join(os.getcwd(), ".opencode")
        roots = ((active_root,) if repo_writable
                 else (opencode_project_dir,))
        scope = action_policy.OwnedScope(
            repo_roots=roots,
            declared_outputs=(tuple(declared_outputs)
                              if declared_outputs is not None
                              else _declared_outputs_for_role(
                                  assets_dir, role)),
            role_temp_dir=role_temp, controller_state_dir=controller_state,
            session_assets_dir=assets_dir,
            sibling_worktrees=sibling_worktrees, protected_paths=())
        boundary = kernel_write_boundary(scope)
        if not boundary["available"]:
            raise RuntimeError(boundary["reason"])
        env = dict(os.environ)
        env["TMPDIR"] = role_temp
        return {"scope": scope, "boundary": boundary, "broker": None,
                "thread": None, "settings_path": None, "env": env,
                "context_path": None, "delegation_allowed": False,
                "profile": None, "protected_paths": ()}
    guard_dir = state_store.guard_dir_for(session_uuid)
    role_temp = os.path.join(guard_dir, "tmp", role)
    controller_state = state_store.controller_state_dir_for(session_uuid, role)
    os.makedirs(role_temp, exist_ok=True)
    os.makedirs(controller_state, exist_ok=True)
    profile = None
    if controller == "codex":
        profile = controller_profiles.reference_codex_auth(controller_state)
    profile_protected = tuple((profile or {}).get("protected_paths") or ())
    active_root, sibling_worktrees = _git_worktree_scope(os.getcwd())
    if controller == "claude":
        if resume_id:
            migrate_legacy_claude_resume(controller_state, resume_id)
    # Evaluators are intrinsically evidence readers. Keep this invariant at the
    # boundary as well as at their production caller so a future call site
    # cannot accidentally restore repository mutation authority.
    if role == "evaluator":
        repo_writable = False
    scope = action_policy.OwnedScope(
        repo_roots=((active_root,) if repo_writable else ()),
        declared_outputs=(tuple(declared_outputs)
                          if declared_outputs is not None
                          else _declared_outputs_for_role(assets_dir, role)),
        role_temp_dir=role_temp, controller_state_dir=controller_state,
        session_assets_dir=assets_dir, sibling_worktrees=sibling_worktrees,
        protected_paths=profile_protected)
    boundary = kernel_write_boundary(scope)
    if not boundary["available"]:
        controller_profiles.cleanup_claude_session_reference(profile)
        raise RuntimeError(boundary["reason"])
    env = dict(os.environ)
    env["TMPDIR"] = role_temp
    if controller == "codex":
        env["CODEX_HOME"] = controller_state
    token = uuid.uuid4().hex + uuid.uuid4().hex
    socket_path = state_store.guard_socket_path_for(
        session_uuid, role, token)
    settings_path = (
        os.path.join(controller_state, "hooks.json")
        if controller == "codex"
        else state_store.guard_settings_path_for(session_uuid, role))
    parent = {
        "controller": controller, "controller_source": (
            "config_pinned" if controller else "unknown"),
        "model": model, "model_source": (
            "config_pinned" if model else "unknown"),
        "effort": effort, "effort_source": (
            "config_pinned" if effort else "unknown"),
    }
    capability_allowlist, _ = state_store.read_capability_allowlist(
        session_uuid)
    context_path = state_store.guard_context_path_for(session_uuid, role)
    context_tmp = context_path + ".tmp"
    with open(context_tmp, "w") as fh:
        json.dump({
            "role": role, "socket_path": socket_path, "token": token,
            "parent_identity": parent,
            "owned_roots": list(scope.writable_roots),
        }, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(context_tmp, context_path)
    write_guard_settings(
        settings_path,
        os.path.join(os.path.dirname(__file__), "cowork_action_guard.py"),
        socket_path=socket_path, context_path=context_path)
    broker = guard_broker.GuardBroker(
        socket_path, token, scope, state_store.actions_path_for(session_uuid),
        state_store.children_path_for(session_uuid),
        trace_store.trace_path_for(session_uuid), parent,
        capability_allowlist=capability_allowlist,
        session_id=session_uuid)
    thread = threading.Thread(target=broker.serve_forever,
                              name="cowork-guard-%s" % role, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while not os.path.exists(socket_path) and thread.is_alive():
        if time.monotonic() >= deadline:
            broker.stop()
            controller_profiles.cleanup_claude_session_reference(profile)
            raise RuntimeError("guard_broker_start_timeout")
        time.sleep(0.01)
    if not thread.is_alive():
        controller_profiles.cleanup_claude_session_reference(profile)
        raise RuntimeError("guard_broker_start_failed")
    if controller == "claude":
        try:
            profile = controller_profiles.reference_claude_session(
                controller_state, controller_session_id or resume_id,
                os.getcwd(), resume=bool(resume_id))
        except Exception:
            broker.stop()
            raise
    # Neither supported controller exposes a concurrency-safe correlation from
    # a pre-dispatch child decision to SubagentStart. Keep direct role work
    # usable, hard-disable native delegation, and make the broker deny a bypass.
    can_delegate = False
    delegation_reason = (
        "multi_agent_feature_disabled" if controller == "codex"
        else "child_agent_correlation_unavailable")
    try:
        trace.event("nested.guard.ready", role=role, controller=controller,
                    delegation="enforceably_disabled",
                    delegation_reason=delegation_reason,
                    kernel_boundary=boundary.get("platform"))
    except Exception:
        broker.stop()
        controller_profiles.cleanup_claude_session_reference(profile)
        raise
    env["TMPDIR"] = role_temp
    protected_paths = tuple(profile.get("protected_paths") or ())
    if controller == "codex":
        protected_paths += (settings_path,)
    return {"scope": scope, "boundary": boundary, "broker": broker,
            "thread": thread, "settings_path": settings_path, "env": env,
            "context_path": context_path,
            "delegation_allowed": can_delegate, "profile": profile,
            "protected_paths": protected_paths}


def _stamp_guard_parent_work(runtime, work_id):
    """Publish the current parent work id for raw hook payload enrichment."""
    path = (runtime or {}).get("context_path")
    if not path:
        return
    with open(path, "r") as fh:
        context = json.load(fh)
    context["current_parent_work_id"] = work_id
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(context, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _require_controller_auth(runtime, controller, trace, role, run=None):
    """Verify login inside the exact private profile before any model turn."""
    runner = run or subprocess.run
    command = controller_profiles.auth_command(controller)
    boundary = kernel_write_boundary(
        runtime["scope"], command,
        protected_paths=runtime.get("protected_paths") or ())
    if not boundary["available"]:
        raise RuntimeError(boundary["reason"])
    started = time.monotonic()
    try:
        completed = runner(
            boundary["argv"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=runtime["env"], timeout=15, check=False)
        status_output = completed.stdout
        if controller == "codex":
            status_output = (completed.stdout or "") + "\n" + (
                completed.stderr or "")
        status = controller_profiles.parse_auth_status(
            controller, completed.returncode, status_output)
        error_type = None
    except Exception as exc:  # noqa: BLE001 - normalize to a safe preflight
        status = {"authenticated": False, "method": None}
        error_type = type(exc).__name__
    duration_ms = int((time.monotonic() - started) * 1000)
    if trace:
        trace.event(
            "controller.auth.status", controller=controller, role=role,
            authenticated=status["authenticated"], method=status.get("method"),
            private_profile=True, credential_copied=False,
            duration_ms=duration_ms, error_type=error_type)
    if not status["authenticated"]:
        raise RuntimeError(
            "controller_auth_unavailable: %s private profile cannot reuse "
            "the existing login" % controller)
    return status


def _close_guard_runtime(runtime):
    if not runtime:
        return
    broker = runtime.get("broker")
    if broker:
        broker.stop()
    controller_profiles.cleanup_claude_session_reference(
        runtime.get("profile"))


def kernel_write_boundary(owned_scope, argv=None, protected_paths=()):
    """Return a generated kernel-boundary description, or fail closed.

    macOS seatbelt is emitted as an argv wrapper. Linux requires bubblewrap;
    unsupported platforms return an unavailable result rather than silently
    launching without a boundary.
    """
    roots = tuple(os.path.realpath(p) for p in owned_scope.writable_roots)
    siblings = tuple(os.path.realpath(p)
                     for p in owned_scope.sibling_worktrees)
    # Explicit sibling denies are needed only when a sibling is nested inside
    # a writable root. An ancestor sibling (the main checkout when the selected
    # worktree lives under `.worktrees/`) is already read-only under the
    # default deny and must not override the selected nested root.
    nested_siblings = tuple(
        sibling for sibling in siblings
        if any(action_policy._inside(sibling, root) for root in roots))
    protected = tuple(dict.fromkeys(
        os.path.abspath(os.path.expanduser(p))
        for p in protected_paths if p))
    declared = tuple(os.path.realpath(p)
                     for p in owned_scope.declared_outputs)
    # Controllers create their own shell scratch under /tmp/claude-<uid>
    # regardless of the TMPDIR redirect; without this grant every nested Bash
    # call dies creating that directory before the command runs (ORCH-051).
    scratch_base = "/tmp/claude-%d" % os.getuid()
    scratch_bases = tuple(dict.fromkeys(
        (scratch_base, os.path.realpath(scratch_base))))
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        def quote(value):
            return value.replace("\\", "\\\\").replace('"', '\\"')

        def regex_quote(value):
            escaped = re.sub(r'([.^$+*?()\[\]{}|\\])', r'\\\1', value)
            return escaped.replace('"', '\\"')
        lines = ["(version 1)", "(allow default)", "(deny file-write*)",
                 '(allow file-write* (literal "/dev/null"))',
                 '(allow file-write* (literal "/dev/tty"))',
                 '(allow file-write* (subpath "/dev/fd"))']
        for root in roots:
            lines.append('(allow file-write* (literal "%s"))' % quote(root))
            lines.append('(allow file-write* (subpath "%s"))' % quote(root))
        for base in scratch_bases:
            lines.append('(allow file-write* (subpath "%s"))' % quote(base))
        # Controllers persist a declared output atomically through a
        # `<path>.tmp.<rand>` sibling; a bare literal allow rejects that
        # staging file, so cover the pattern beside every declared file
        # (ORCH-051).
        for path in declared:
            lines.append('(allow file-write* (regex #"^%s\\.tmp\\."))'
                         % regex_quote(path))
        # Seatbelt deny rules override broader allows. This is essential when a
        # repository convention nests sibling worktrees below the active root.
        for root in nested_siblings:
            lines.append('(deny file-write* (literal "%s"))' % quote(root))
            lines.append('(deny file-write* (subpath "%s"))' % quote(root))
        for path in protected:
            lines.append('(deny file-write* (literal "%s"))' % quote(path))
            real = os.path.realpath(path)
            if real != path:
                lines.append('(deny file-write* (literal "%s"))' % quote(real))
        profile = "\n".join(lines) + "\n"
        wrapper = ["sandbox-exec", "-p", profile]
        return {"available": True, "platform": "darwin",
                "profile": profile,
                "argv": wrapper + list(argv or ())}
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        wrapper = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/"]
        for root in roots:
            wrapper += ["--bind", root, root]
        for root in nested_siblings:
            wrapper += ["--ro-bind", root, root]
        for path in protected:
            wrapper += ["--ro-bind", os.path.realpath(path),
                        os.path.realpath(path)]
        return {"available": True, "platform": "linux", "profile": None,
                "argv": wrapper + list(argv or ())}
    return {"available": False, "platform": sys.platform, "profile": None,
            "argv": None, "reason": "kernel_boundary_unavailable"}


def codex_model_args(model=None, effort=None):
    """Model/effort args shared by fresh and resume codex turns.

    Both are expressed through `-c` config keys (`model`, `model_reasoning_effort`)
    rather than `-m`, because `codex exec resume` rejects `-m` but accepts `-c`
    — using one spelling keeps fresh and resumed turns byte-identical in intent.
    Values are encoded as TOML basic strings via json.dumps."""
    args = []
    if model:
        args += ["-c", "model=" + json.dumps(model)]
    if effort:
        args += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    return args


def codex_governance_args(guarded=False):
    """Fail-closed Codex settings shared by fresh and resumed role turns."""
    if not guarded:
        return []
    return [
        "--ignore-user-config",
        "--ignore-rules",
        "--dangerously-bypass-hook-trust",
        "--disable", "multi_agent",
        "-c", "agents.enabled=false",
        "-c", "project_doc_max_bytes=0",
        "-c", "projects.%s.trust_level=\"untrusted\"" % json.dumps(
            os.path.abspath(os.getcwd())),
        "--strict-config",
    ]


def codex_mcp_override_args(guarded=False):
    """Fail-closed MCP removal shared by fresh and resumed codex turns
    (ORCH-001). Applied independent of `guarded` so the unguarded path is
    covered too.

    Mechanism verified live against codex-cli 0.145.0:
    - `-c mcp_servers={}` alone deep-MERGES into the loaded config and is a
      no-op (`codex mcp list -c 'mcp_servers={}'` still lists every configured
      server); per-server `-c mcp_servers.<name>.enabled=false` overrides can
      hard-fail config loading on real-world entries. Neither is a viable
      removal mechanism on its own.
    - `--ignore-user-config` ("Do not load `$CODEX_HOME/config.toml`; auth
      still uses `CODEX_HOME`") removes every configured MCP server, because
      config.toml is where codex MCP servers are defined.

    Guarded turns already get `--ignore-user-config` (plus a private
    CODEX_HOME) from `codex_governance_args`, so it is added here only on the
    unguarded path. The explicit `-c mcp_servers={}` is kept on both paths as
    the fail-closed marker of intent: with no user config loaded the effective
    `mcp_servers` table is empty by construction, and if
    `--ignore-user-config` semantics ever drift the override still applies."""
    args = []
    if not guarded:
        args.append("--ignore-user-config")
    return args + ["-c", "mcp_servers={}"]


def build_codex_command(prompt_text, mode, yolo, extra_writable_dir=None,
                        model=None, effort=None, guarded=False):
    """argv for the first one-shot codex exec turn. The role spec is prepended
    into prompt_text by the caller (no AGENTS.md is written into the repo).

    `--skip-git-repo-check` lets cowork run outside a trusted/git directory
    (codex exec otherwise refuses with "Not inside a trusted directory").
    `extra_writable_dir`, when set, is granted as an additional writable root
    via `--add-dir` so a no-yolo (workspace-write) role can write its session
    artifacts outside cwd. The grant is re-applied on every codex resume too
    (see `codex_resume_mode_args` / `build_codex_resume_command`), so resumed
    roles keep the same effective permissions as this fresh turn."""
    return (
        ["codex", "exec", "--json", "--skip-git-repo-check"]
        + codex_governance_args(guarded)
        + codex_mcp_override_args(guarded)
        + codex_mode_flags(mode, yolo)
        + codex_model_args(model, effort)
        + (["--add-dir", extra_writable_dir] if extra_writable_dir else [])
        + [prompt_text]
    )


def codex_resume_mode_args(mode, yolo, extra_writable_dir=None):
    """Resume-compatible permission args mirroring `codex_mode_flags` for a
    `codex exec resume` turn (verified against codex-cli 0.139.0).

    `codex exec resume` rejects `--sandbox`/`--add-dir`, but accepts
    `--dangerously-bypass-approvals-and-sandbox` and `-c <dotted.toml.path>`.
    So the sandboxed modes are re-applied through `-c` config keys instead:

    - plan -> `sandbox_mode="read-only"` (mirrors fresh `--sandbox read-only`).
    - implement + yolo -> `--dangerously-bypass-approvals-and-sandbox`.
    - implement + no-yolo -> `sandbox_mode="workspace-write"`, plus, when
      `extra_writable_dir` is set, `sandbox_workspace_write.writable_roots`
      granting that dir (mirrors fresh `--sandbox workspace-write` + `--add-dir`;
      the root is ADDED to the default roots, verified live).

    The writable-root path is encoded as a TOML basic string via `json.dumps`
    (a valid TOML basic string for filesystem paths, escaping any quotes/
    backslashes); the array value is `[` + json.dumps(dir) + `]`.
    """
    if mode == "plan":
        return ["-c", 'sandbox_mode="read-only"']
    # implement
    if yolo:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    args = ["-c", 'sandbox_mode="workspace-write"']
    if extra_writable_dir:
        roots = "[" + json.dumps(extra_writable_dir) + "]"
        args += ["-c", "sandbox_workspace_write.writable_roots=" + roots]
    return args


def build_codex_resume_command(thread_id, prompt_text, mode, yolo,
                               extra_writable_dir=None, model=None,
                               effort=None, guarded=False):
    """argv for a codex follow-up turn against an explicit thread id (never
    --last, which could grab a concurrent session in the same cwd).

    On codex-cli 0.139.0 `codex exec resume` does NOT inherit the original
    session's sandbox, so each resume must re-apply the role's permissions.
    `--sandbox`/`--add-dir` are rejected on resume, but
    `--dangerously-bypass-approvals-and-sandbox` and `-c` config keys are
    accepted — see `codex_resume_mode_args`, which mirrors the fresh-launch
    permissions for every (mode, yolo) combo. The permission args go after
    `--skip-git-repo-check` and before the thread id; prompt_text stays the
    final positional arg (never --last)."""
    return (
        ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
        + codex_governance_args(guarded)
        + codex_mcp_override_args(guarded)
        + codex_resume_mode_args(mode, yolo, extra_writable_dir)
        + codex_model_args(model, effort)
        + [thread_id, prompt_text]
    )


# --------------------------------------------------------------------------- #
# opencode: agent-file generation + command assembly.                          #
# --------------------------------------------------------------------------- #

# Generated agent files are named cowork-<role>.md so they never collide with a
# user's own agents, and are regenerated on every spawn (mode/yolo live in the
# frontmatter, so a config change must rewrite the file).
OPENCODE_AGENT_PREFIX = "cowork-"
OPENCODE_AGENT_SUBDIR = os.path.join(".opencode", "agents")

# ORCH-001: MCP removal for opencode roles, in the legacy `tools:` map.
# OpenCode names MCP tools `<server>_<tool>`, so the `"*_*"` catch-all denies
# every MCP tool no matter which servers the user's config defines — server
# names are not knowable at agent-generation time, and a name-based list would
# silently miss servers added later. The explicit re-allows after it keep the
# native tools cowork roles depend on (deny-first/allow-last; the last
# matching rule wins, same discipline as `opencode_permission_lines`).
# Live-verified on opencode 1.18.10: a probe MCP server's tool disappeared
# from the model's tool set while native glob/read/bash kept working, and the
# `permission:` map still dominates natives — a `tools: true` entry does NOT
# re-open a permission `deny`, so per-mode permission behavior is unchanged.
# The `"*_*"` glob also matches opencode's underscore-named PERMISSIONS, not
# just tool names: without the `external_directory: true` re-allow directly
# after it, the built-in `external_directory: ask` (auto-approved under
# `--auto`) hardens into a deny and every read/write outside the project dir
# fails — including the role's own declared session artifacts. Live-verified
# on opencode 1.18.10: session 02bd445d's planner had every session-dir write
# rejected until the re-allow was added.
OPENCODE_MCP_DENY_TOOL_LINES = [
    '  "*_*": false',
    "  external_directory: true",
    "  read: true",
    "  edit: true",
    "  write: true",
    "  bash: true",
    "  grep: true",
    "  glob: true",
    "  list: true",
    "  webfetch: true",
    "  skill: true",
    "  todowrite: true",
    "  todoread: true",
    "  apply_patch: true",
]


class OpencodeArtifactContractUnexpressible(policy.DispatchBlocked):
    """Raised by `ensure_opencode_agent` BEFORE any agent file is written and
    before any process exists, when a plan-mode OpenCode role's declared
    outputs cannot be expressed as a permission grant (P4).

    Subclasses `DispatchBlocked` so every existing pre-launch handler that
    already renders a `DispatchBlocked` as a clean message (cowork.py:4210,
    4635, 7159, ...) prints this refusal verbatim with no new handling. It is
    defined only in this module, on the opencode path — the scoping is
    structural, not conditional, so it can never fire for a Codex or Claude
    role (P3)."""

    def __init__(self, role, mode, artifacts):
        self.mode = mode
        self.artifacts = tuple(artifacts)
        super().__init__("opencode", role=role, kind="setup")

    def _message(self):
        artifacts = ", ".join(self.artifacts) if self.artifacts else "none"
        return (
            "cowork: %s is configured for %s mode on opencode, but its "
            "declared artifacts (%s) cannot be expressed as opencode "
            "permissions — no opencode controller was launched."
            % (self.role, self.mode, artifacts))


def _opencode_path_patterns(path, assets_dir):
    """Ordered, de-duplicated exact-file pattern forms for one absolute path
    living directly under `assets_dir`.

    Multiple forms are emitted because OpenCode's pattern normalization
    (absolute vs cwd-relative vs `~`-expanded) could not be confirmed offline
    against the compiled binary (F3): whichever form OpenCode normalizes a
    write target to, one of these matches, so the write is allowed either
    way. None of the three widens the grant beyond this one file — the
    `**/...` form is scoped by the assets-dir basename (the session uuid), so
    no ordinary repository path can match it."""
    absolute = os.path.realpath(path)
    patterns = [absolute]
    home = os.path.expanduser("~")
    if absolute == home or absolute.startswith(home + os.sep):
        patterns.append("~" + absolute[len(home):])
    patterns.append("**/%s/%s" % (
        os.path.basename(os.path.normpath(assets_dir)),
        os.path.basename(absolute)))
    return tuple(dict.fromkeys(patterns))


def _opencode_dir_patterns(assets_dir):
    """Ordered, de-duplicated pattern forms granting exactly `assets_dir`
    itself and everything under it (`<assets_dir>/**`), in the same
    multi-form shape as `_opencode_path_patterns` (P8).

    Scoped to this one session's assets directory only — never the flat
    `external_directory: allow` — so a plan-mode role reads no filesystem
    path outside the session's own folder."""
    absolute = os.path.realpath(assets_dir)
    basename = os.path.basename(absolute)
    home = os.path.expanduser("~")
    home_form = None
    if absolute == home or absolute.startswith(home + os.sep):
        home_form = "~" + absolute[len(home):]
    patterns = [absolute]
    if home_form:
        patterns.append(home_form)
    patterns.append("**/%s" % basename)
    patterns.append(absolute + "/**")
    if home_form:
        patterns.append(home_form + "/**")
    patterns.append("**/%s/**" % basename)
    return tuple(dict.fromkeys(patterns))


def _opencode_yaml_key(pattern):
    """Render `pattern` as a double-quoted YAML mapping key so `/`, `*`, `~`
    are read as literal text, never as YAML aliases or flow indicators.
    JSON string syntax is valid YAML double-quoted-scalar syntax, so this
    also handles escaping."""
    return json.dumps(pattern)


def opencode_plan_declared_outputs(speaker, assets_dir):
    """Resolve `speaker`'s plan-mode declared-output paths under
    `assets_dir`.

    Raises `OpencodeArtifactContractUnexpressible` when no pattern set could
    express the contract: no assets dir was supplied, the role has no
    declared outputs, or a declared output does not live under the supplied
    assets dir (P4). The refusal message names the artifact filenames even
    with no assets dir, resolved from the role -> filename mapping alone."""
    outputs = _declared_outputs_for_role(assets_dir, speaker) if assets_dir else ()
    assets_real = os.path.realpath(assets_dir) if assets_dir else None
    contained = bool(outputs) and assets_real is not None and all(
        os.path.realpath(p) == assets_real
        or os.path.realpath(p).startswith(assets_real + os.sep)
        for p in outputs)
    if not contained:
        names = tuple(os.path.basename(p) for p in
                      (outputs or _declared_outputs_for_role("", speaker)))
        raise OpencodeArtifactContractUnexpressible(speaker, "plan", names)
    return outputs


def _opencode_bash_permission_lines():
    """`bash:` pattern map allowing exactly the vetted read-only commands
    (ORCH-053). Catch-all `ask` first (still a hard deny headless, but an
    interactive run can approve), then one allow per pattern —
    last-match-wins, same ordering discipline as the `edit:` map.

    Glob prefix matching is injectable (`git status; rm -rf` matches
    `"git status *"`), so this map is only ever emitted when the run is
    wrapped in the kernel write boundary — the caller enforces that gate."""
    lines = ["  bash:", "    %s: ask" % _opencode_yaml_key("*")]
    for pattern in action_policy.readonly_bash_glob_patterns():
        lines.append("    %s: allow" % _opencode_yaml_key(pattern))
    return lines


def opencode_permission_lines(mode, yolo, external_dir=False,
                              declared_outputs=(), assets_dir=None,
                              bash_readonly=False):
    """Frontmatter `permission:` lines for (mode, yolo).

    opencode has no OS sandbox; permissions are the only guardrail, and in a
    headless run (stdin is not a TTY) any rule that resolves to `ask` is
    auto-rejected by opencode — so `ask` acts as a hard deny here:

    - plan, WITH declared outputs -> an ordered `edit:` object map: catch-all
      denies (`"*"`, `"**"`) first, then one allow key per pattern form per
      declared output (P1, P2). OpenCode evaluates the map in order with the
      last matching rule winning, so deny-first/allow-last is what yields
      default-deny with narrow exceptions; `external_directory:` gets the
      same deny-first shape, scoped to just the session assets directory
      (P8) rather than the flat `external_directory: allow`, so a read-only
      role gains no filesystem reach beyond its own session folder. `bash`
      stays `ask`, which a headless run auto-rejects, so it is still a hard
      deny in practice.
    - plan, WITHOUT declared outputs -> today's scalar `edit: deny` (P12):
      the formatter stays a total pure function and never raises: a caller
      that reaches it without declared outputs gets the safest possible
      output, not a broader one. (The pre-launch refusal for an
      unexpressible contract lives one layer up, in
      `opencode_plan_declared_outputs` / `ensure_opencode_agent`.)
    - implement + no-yolo -> edit allow + bash ask (mirrors claude
      acceptEdits: edits land, other commands are denied).
      `external_directory: allow` is added when the role needs to write
      session artifacts outside cwd.
    - every mode -> task deny. OpenCode removes denied task targets from the
      model's tool description, so native child delegation is absent before
      the first model turn.
    - implement + yolo -> only the task deny remains; the run gets `--auto`
      (auto-approve everything else).
    """
    if mode == "implement" and yolo:
        return ["permission:", "  task: deny"]
    if mode == "plan" and declared_outputs:
        lines = ["permission:", "  task: deny", "  edit:",
                 "    %s: deny" % _opencode_yaml_key("*"),
                 "    %s: deny" % _opencode_yaml_key("**")]
        seen = set()
        for output in declared_outputs:
            for pattern in _opencode_path_patterns(output, assets_dir):
                if pattern in seen:
                    continue
                seen.add(pattern)
                lines.append("    %s: allow" % _opencode_yaml_key(pattern))
        lines += (_opencode_bash_permission_lines() if bash_readonly
                  else ["  bash: ask"])
        lines += ["  webfetch: allow",
                  "  external_directory:",
                  "    %s: deny" % _opencode_yaml_key("*"),
                  "    %s: deny" % _opencode_yaml_key("**")]
        seen = set()
        for pattern in _opencode_dir_patterns(assets_dir):
            if pattern in seen:
                continue
            seen.add(pattern)
            lines.append("    %s: allow" % _opencode_yaml_key(pattern))
        return lines
    lines = ["permission:",
             "  task: deny",
             "  edit: %s" % ("deny" if mode == "plan" else "allow")]
    lines += (_opencode_bash_permission_lines() if bash_readonly
              else ["  bash: ask"])
    lines.append("  webfetch: allow")
    if mode != "plan" and external_dir:
        lines.append("  external_directory: allow")
    return lines


def opencode_agent_markdown(role_prompt_text, mode, yolo, description,
                            external_dir=False, declared_outputs=(),
                            assets_dir=None, bash_readonly=False):
    """Agent-file markdown: YAML frontmatter + the role prompt as the body."""
    lines = ["---", "description: %s" % description, "mode: primary",
             # Defense in depth across OpenCode versions: `tools` is the
             # deprecated boolean form while `permission` below is current.
             "tools:", "  task: false"]
    # ORCH-001: strip every MCP tool (catch-all) while re-allowing natives.
    lines += OPENCODE_MCP_DENY_TOOL_LINES
    lines += opencode_permission_lines(
        mode, yolo, external_dir=external_dir,
        declared_outputs=declared_outputs, assets_dir=assets_dir,
        bash_readonly=bash_readonly)
    lines.append("---")
    body = role_prompt_text.strip()
    if bash_readonly:
        # Steer the model away from burning a denial on a shell write: the
        # allowlist covers read-only commands only, and a denied heredoc or
        # redirect otherwise ends the turn as `denied`.
        body += (
            "\n\nShell access here is READ-ONLY: only pre-approved "
            "inspection commands (git status/diff/log/show/ls-files/"
            "rev-parse, ls, grep, rg, find, pwd, wc, head, tail) are "
            "allowed. Never write files through the shell (no redirection, "
            "heredocs, tee, or `cat >`); use the write/edit tools for every "
            "file you produce.")
    return "\n".join(lines) + "\n" + body + "\n"


def ensure_opencode_agent(role_prompt_file, speaker, mode, yolo,
                          base_dir=None, external_dir=False, assets_dir=None,
                          bash_readonly=False):
    """Write `.opencode/agents/cowork-<speaker>.md` under base_dir (default cwd)
    and return the agent name. Overwrites any previous file so the prompt and
    permissions always reflect the current role config.

    In plan mode, resolves the role's declared outputs against `assets_dir`
    and raises `OpencodeArtifactContractUnexpressible` BEFORE writing
    anything when that contract cannot be expressed as permissions (P3, P4)."""
    policy.guard("opencode", role=speaker, kind="setup")
    declared_outputs = ()
    if mode == "plan":
        declared_outputs = opencode_plan_declared_outputs(speaker, assets_dir)
    base_dir = base_dir or os.getcwd()
    agent_name = OPENCODE_AGENT_PREFIX + speaker
    agent_dir = os.path.join(base_dir, OPENCODE_AGENT_SUBDIR)
    os.makedirs(agent_dir, exist_ok=True)
    with open(role_prompt_file, "r", encoding="utf-8") as fh:
        role_text = fh.read()
    content = opencode_agent_markdown(
        role_text, mode, yolo,
        description="cowork %s role (generated; do not edit)" % speaker,
        external_dir=external_dir, declared_outputs=declared_outputs,
        assets_dir=assets_dir, bash_readonly=bash_readonly)
    path = os.path.join(agent_dir, agent_name + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return agent_name


def opencode_global_agent_dir():
    """Directory for fallback (global) agent delivery.

    opencode 1.18.10 fails every run that resolves a PROJECT-LOCAL agent file
    with a pre-model "Unexpected server error" (ORCH-052); the identical file
    in the global config dir loads fine. `agent` (singular) is the empirically
    verified segment on the affected version — the docs say `agents`, but the
    fallback only ever fires on versions where singular is what works, and the
    live test is the check if a future version changes the rules."""
    base = os.environ.get("OPENCODE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "opencode")
    return os.path.join(base, "agent")


def _remove_agent_file_quiet(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def write_opencode_global_agent(content, speaker, token):
    """Write `content` as a session-scoped agent in the global config dir and
    return `(agent_name, path)`. The `token` keeps concurrent cowork sessions
    from colliding; the caller removes the file as soon as the run exits."""
    agent_name = OPENCODE_AGENT_PREFIX + speaker + "-" + token
    agent_dir = opencode_global_agent_dir()
    os.makedirs(agent_dir, exist_ok=True)
    path = os.path.join(agent_dir, agent_name + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return agent_name, path


# Signature of the ORCH-052 agent-loading failure: the server dies before any
# model turn, emitting only an error event with this name/message.
_OPENCODE_DELIVERY_ERROR_MARKERS = ("unexpected server error", "unknownerror")


def is_opencode_delivery_failure(parsed_events):
    """True iff a run's parsed events match the agent-delivery failure shape:
    at least one error event carrying the ORCH-052 marker text, and NO
    substantive event (message, tool activity, denial, step_finish) — i.e. the
    run died before any model turn, so a retry costs nothing. An error with
    different text, or one that follows real output, is an ordinary provider
    error and must NOT trigger delivery fallback."""
    saw_marker = False
    for parsed in parsed_events:
        kind = parsed.get("kind")
        if kind in ("message", "tool", "tool_done", "denied", "step_finish"):
            return False
        if kind == "error":
            text = (parsed.get("text") or "").lower()
            if any(m in text for m in _OPENCODE_DELIVERY_ERROR_MARKERS):
                saw_marker = True
            else:
                return False
    return saw_marker


def build_opencode_command(agent_name, prompt_text, mode, yolo, model=None,
                           effort=None, resume_session_id=None,
                           directory=None):
    """argv for one `opencode run` turn (fresh or resumed).

    `--format json` gives JSONL events on stdout; `--print-logs` is NOT passed
    (logs go to stderr anyway, which we discard). `model` is opencode's
    `provider/model` form (e.g. `anthropic/claude-sonnet-4-5`) — the provider
    choice is embedded in the model id. `effort` maps to `--variant`
    (provider-specific reasoning effort). Yolo implement runs get `--auto`;
    everything else relies on the generated agent's permission block (see
    `opencode_permission_lines`)."""
    policy.guard("opencode", kind="setup")
    cmd = ["opencode", "run", "--format", "json", "--agent", agent_name]
    if directory:
        # opencode does not always resolve its project from the process cwd
        # (observed 1.18.10: a run in a fresh tmp git repo executed its bash
        # tool in the last-active project instead). `--dir` pins it, so the
        # role's tools act on the same tree the kernel boundary was built
        # around.
        cmd += ["--dir", directory]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--variant", effort]
    if mode == "implement" and yolo:
        cmd.append("--auto")
    if resume_session_id:
        cmd += ["--session", resume_session_id]
    return cmd + [prompt_text]


# --------------------------------------------------------------------------- #
# Message framing / event parsing.                                            #
# --------------------------------------------------------------------------- #


def encode_user_message(text):
    """Newline-delimited stream-json user message for claude stdin."""
    obj = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(obj) + "\n"


def _text_from_content(content):
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
    return "".join(parts)


def _looks_like_permission_denial(text):
    if not text:
        return False
    low = text.lower()
    return (
        "permission" in low
        or "requires approval" in low
        or "not allowed" in low
        or "denied" in low
    )


def _usage_from_result(obj):
    """Best-effort extraction of token usage from a claude stream-json `result`
    event (#1/D2). Returns a small content-free dict of int counts when the CLI
    exposes them, else None. Tolerant: any unexpected shape yields None and never
    raises. Byte+hash accounting is the load-bearing data; usage is optional."""
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    out = {}
    for key in keys:
        val = usage.get(key)
        if isinstance(val, bool):  # bool is an int subclass; never a token count
            continue
        if isinstance(val, int):
            out[key] = val
    return out or None


def parse_claude_event(obj):
    """Classify one claude stream-json output event.

    Returns a dict with at least {"kind": ...}. Kinds: assistant, result,
    system, partial, user_replay, denied, other.
    """
    etype = obj.get("type")
    if etype == "assistant":
        msg = obj.get("message", {}) or {}
        parent_tool_use_id = obj.get("parent_tool_use_id")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
        if parent_tool_use_id:
            tools = []
            for part in msg.get("content", []) if isinstance(
                    msg.get("content"), list) else []:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tools.append({"name": part.get("name"),
                                  "tool_use_id": part.get("id")})
            return {"kind": "child_usage", "parent_tool_use_id":
                    parent_tool_use_id, "usage": usage, "tools": tools,
                    "text": _text_from_content(msg.get("content")),
                    "event_id": obj.get("uuid") or obj.get("requestId"),
                    "replayed": bool(obj.get("replayed"))}
        text = _text_from_content(msg.get("content"))
        # Claude CLI may wrap an API/authentication failure in a synthetic
        # assistant event and later emit a nominally successful zero-token
        # result.  Preserve the provider's explicit error signal so callers do
        # not mistake that turn for a successful role reply.
        if obj.get("isApiErrorMessage") or obj.get("error"):
            return {"kind": "error", "text": text,
                    "error_type": obj.get("error") or "api_error"}
        # A denied/blocked tool surfaces as an error tool_result in the stream.
        for part in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                if part.get("is_error") and _looks_like_permission_denial(
                    _text_from_content(part.get("content"))
                ):
                    return {"kind": "denied", "text": _text_from_content(part.get("content"))}
        parsed = {"kind": "assistant", "text": text}
        if usage is not None:
            parsed["usage"] = usage
        return parsed
    if etype == "result":
        subtype = obj.get("subtype", "")
        is_error = "error" in (subtype or "")
        return {
            "kind": "result",
            "subtype": subtype,
            "is_error": is_error,
            "text": obj.get("result", ""),
            "session_id": obj.get("session_id"),
            # Best-effort controller-reported usage (#1/D2): present only when the
            # CLI's result event exposes token counts. Never load-bearing.
            "usage": _usage_from_result(obj),
        }
    if etype == "system":
        if obj.get("subtype") in ("subagent_start", "subagent_started"):
            return {"kind": "child_start", "agent_id": obj.get("agent_id"),
                    "agent_type": obj.get("agent_type"),
                    "tool_use_id": obj.get("tool_use_id")
                    or obj.get("parent_tool_use_id")}
        if obj.get("subtype") in ("subagent_stop", "subagent_stopped"):
            return {"kind": "child_end", "agent_id": obj.get("agent_id"),
                    "tool_use_id": obj.get("tool_use_id")
                    or obj.get("parent_tool_use_id"),
                    "event_id": obj.get("uuid") or obj.get("event_id")}
        if obj.get("subtype") == "api_error":
            error = obj.get("error") or {}
            text = (error.get("formatted") or error.get("message")
                    if isinstance(error, dict) else str(error))
            return {"kind": "error", "text": text,
                    "error_type": "api_error"}
        # The init system event names the live model (traceability: which
        # model actually served this session, not just which was requested).
        return {"kind": "system", "subtype": obj.get("subtype", ""),
                "model": obj.get("model"), "tools": obj.get("tools")}
    if etype == "stream_event":
        event = obj.get("event") or {}
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return {"kind": "partial", "text": delta.get("text", "")}
        block = event.get("content_block") or {}
        if (event.get("type") == "content_block_start"
                and block.get("type") == "tool_use"):
            # Fallback to 'tool' so the activity line never reads 'using …'.
            return {"kind": "tool", "name": block.get("name") or "tool"}
        return {"kind": "partial", "text": ""}
    if etype == "user":
        return {"kind": "user_replay"}
    return {"kind": "other", "type": etype}


# Codex item types that mean "the agent is using a tool", with the spinner
# label each one shows. Only these flip the activity label; unknown item types
# stay "other" so future codex events can't reset the label incorrectly.
_CODEX_TOOL_LABELS = {
    "command_execution": "running a command",
    "mcp_tool_call": "calling a tool",
    "file_change": "editing files",
    "patch_apply": "editing files",
    "web_search": "searching the web",
}


def _codex_tool_label(itype, item):
    if itype == "mcp_tool_call" and item.get("tool"):
        return "calling %s" % item["tool"]
    return _CODEX_TOOL_LABELS[itype]


def parse_codex_event(obj):
    """Classify one codex --json (JSONL) event.

    Kinds: thread_started (thread_id), turn_started, turn_completed, message
    (text), denied, error, tool (label), tool_done, other.
    """
    etype = obj.get("type")
    if etype == "thread.started":
        # `model` is best-effort: newer codex CLIs may name the live model on
        # the thread event; absent on older versions and dropped downstream.
        return {"kind": "thread_started", "thread_id": obj.get("thread_id"),
                "model": obj.get("model")}
    if etype == "turn.started":
        return {"kind": "turn_started"}
    if etype == "turn.completed":
        return {"kind": "turn_completed", "usage": obj.get("usage"),
                "model": obj.get("model")}
    if etype == "error":
        return {"kind": "error", "text": obj.get("message", "")}
    if etype in ("item.started", "item.completed"):
        item = obj.get("item", {}) or {}
        itype = item.get("type")
        status = item.get("status")
        if status in ("rejected", "declined", "denied"):
            return {"kind": "denied", "text": item.get("text", "") or itype or ""}
        if itype in ("agent_message", "message", "assistant_message"):
            return {"kind": "message", "text": item.get("text", "")}
        if itype in _CODEX_TOOL_LABELS:
            if etype == "item.started":
                return {"kind": "tool", "item_type": itype,
                        "label": _codex_tool_label(itype, item)}
            return {"kind": "tool_done", "item_type": itype}
        return {"kind": "other", "item_type": itype}
    return {"kind": "other", "type": etype}


def capture_thread_id(events):
    """Return the thread_id from the first thread.started event, or None."""
    for obj in events:
        parsed = parse_codex_event(obj)
        if parsed["kind"] == "thread_started" and parsed.get("thread_id"):
            return parsed["thread_id"]
    return None


# opencode auto-rejects any `ask` permission in a headless run with this error
# on the tool call's state — the only denial signal in the event stream.
_OPENCODE_DENIED_MARKER = "rejected permission"


def parse_opencode_event(obj):
    """Classify one `opencode run --format json` (JSONL) event.

    Envelope: {"type": ..., "sessionID": "ses_...", "part": {...}}. Kinds map
    onto the codex parser's vocabulary so OpencodeSession can mirror
    CodexSession: message (text), tool (label), tool_done, denied, error,
    step_finish (tokens/reason), other. Text events carry the COMPLETE text
    block (opencode does not stream deltas in JSON mode).
    """
    etype = obj.get("type")
    part = obj.get("part") or {}
    if etype == "text":
        return {"kind": "message", "text": part.get("text", "")}
    if etype == "tool":
        tool = part.get("tool") or "tool"
        return {"kind": "tool", "label": "using %s" % tool}
    if etype == "tool_use":
        state = part.get("state") or {}
        err = state.get("error") or ""
        if (state.get("status") == "error"
                and _OPENCODE_DENIED_MARKER in err.lower()):
            return {"kind": "denied", "text": err}
        return {"kind": "tool_done", "tool": part.get("tool")}
    if etype == "step_finish":
        return {"kind": "step_finish", "reason": part.get("reason"),
                "tokens": part.get("tokens")}
    if etype == "error":
        err = obj.get("error") or {}
        data = err.get("data") or {}
        return {"kind": "error",
                "text": data.get("message") or err.get("name") or "error"}
    return {"kind": "other", "type": etype}


def capture_opencode_session_id(events):
    """Return the sessionID carried on the first event that has one, or None."""
    for obj in events:
        sid = obj.get("sessionID")
        if sid:
            return sid
    return None


def opencode_usage(events):
    """Best-effort token usage summed over step_finish events, mapped onto the
    claude-style key names the trace/report already understands. None when no
    step reported tokens."""
    totals = {"input_tokens": 0, "output_tokens": 0,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    seen = False
    for obj in events:
        parsed = parse_opencode_event(obj)
        if parsed["kind"] != "step_finish":
            continue
        tokens = parsed.get("tokens")
        if not isinstance(tokens, dict):
            continue
        seen = True
        cache = tokens.get("cache") or {}
        totals["input_tokens"] += int(tokens.get("input") or 0)
        totals["output_tokens"] += (int(tokens.get("output") or 0)
                                    + int(tokens.get("reasoning") or 0))
        totals["cache_read_input_tokens"] += int(cache.get("read") or 0)
        totals["cache_creation_input_tokens"] += int(cache.get("write") or 0)
    return totals if seen else None


def denial_message():
    return "denied: enable yolo or rerun this role with implement access"


# --------------------------------------------------------------------------- #
# Probe: confirm the installed claude accepts our stdin schema.               #
# --------------------------------------------------------------------------- #


def probe_claude_stream_json(spawn, mode="plan", yolo=True,
                             role_prompt_file=DEFAULT_ROLE_PROMPT, trace=None,
                             role="scout", extra_writable_dir=None,
                             cache_enabled=False, version_fn=None,
                             cache_path=None, model=None, effort=None,
                             auth_run=None):
    """Send one minimal user message to claude and confirm an assistant/result
    event comes back.

    spawn(command, stdin_text) -> iterable of raw event dicts (json objects).
    Returns (ok, alert_or_None). On an unsupported shape, ok is False and alert
    explains the failure rather than proceeding on a guessed schema.
    `extra_writable_dir` locates private session state. A live probe deliberately
    receives a narrower write scope than the role it precedes: controller
    state/temp only, with no repository or declared role outputs.

    #3 probe cache: when `cache_enabled` (the live launch call sites pass True;
    tests and existing callers default to False, keeping the always-live-probe
    behavior), a conservative cache key is computed over the resolved CLI path,
    `claude --version`, the role-prompt hash, mode, yolo, and writable-dir
    presence. On a HIT the live probe is skipped entirely (no spawn) and
    (True, None) is returned. On a MISS the live probe runs and, on success, the
    key is stored. A version-resolution failure forces always-live (never
    cached). `version_fn`/`cache_path` are injectable for tests.
    """
    policy.guard("claude", role=role, kind="probe")
    probe_session_id = (
        str(uuid.uuid4()) if nested_guard_active() else None)
    command = build_claude_command(role_prompt_file, mode, yolo,
                                   extra_writable_dir=extra_writable_dir,
                                   session_id=probe_session_id)
    cache_key = None
    cache_hit = False
    if cache_enabled:
        claude_path = probe_cache.resolve_claude_path(command)
        resolver = version_fn or probe_cache.claude_version
        version = resolver(claude_path)
        cache_key = probe_cache.probe_cache_key(
            claude_path, version, role_prompt_file, mode, yolo,
            bool(extra_writable_dir))
        cache_hit = bool(
            cache_key and probe_cache.cache_hit(cache_key, path=cache_path))
    # The live probe is a distinct audited work item. Mint and publish its id
    # before the process exists so every hook decision can join to it.
    probe_work_id = trace_store.new_work_id()
    probe_started = time.monotonic()
    runtime = None
    if nested_guard_active():
        runtime = _guard_runtime(
            trace, role, extra_writable_dir, model, effort, False,
            declared_outputs=(), repo_writable=False,
            controller_session_id=probe_session_id)
        try:
            _require_controller_auth(
                runtime, "claude", trace, role, run=auth_run)
        except RuntimeError:
            _close_guard_runtime(runtime)
            return False, (
                "Claude Code is logged in globally, but the guarded private "
                "profile could not reuse that login. No model turn was "
                "launched."
            )
        if cache_hit:
            if trace:
                trace.event("controller.probe.cache_hit", controller="claude",
                            role=role, prompt_kind="probe", mode=mode, yolo=yolo,
                            role_prompt_file=role_prompt_file,
                            auth_revalidated=True)
            _close_guard_runtime(runtime)
            return True, None
        _stamp_guard_parent_work(runtime, probe_work_id)
        command = build_claude_command(
            role_prompt_file, mode, yolo,
            extra_writable_dir=extra_writable_dir, model=model, effort=effort,
            guard_settings_path=runtime["settings_path"],
            delegation_allowed=runtime["delegation_allowed"],
            session_id=probe_session_id)
        env_argv = [shutil.which("env") or "/usr/bin/env"]
        env_argv += ["%s=%s" % (key, runtime["env"][key])
                     for key in ("TMPDIR",)]
        boundary = kernel_write_boundary(
            runtime["scope"], env_argv + command)
        if not boundary["available"]:
            _close_guard_runtime(runtime)
            raise RuntimeError(boundary["reason"])
        command = boundary["argv"]
    elif cache_hit:
        if trace:
            trace.event("controller.probe.cache_hit", controller="claude",
                        role=role, prompt_kind="probe", mode=mode, yolo=yolo,
                        role_prompt_file=role_prompt_file,
                        auth_revalidated=False)
        return True, None
    stdin_text = encode_user_message("ping")
    # The probe is its own unit of work (P1): a probe's cost is real and is
    # classified `probe` rather than folded into the role turn that follows it.

    def _probe_elapsed_ms():
        return int((time.monotonic() - probe_started) * 1000)

    def _probe_work(**kw):
        return trace_store.work_meta(probe_work_id, "probe", **kw)

    if trace:
        data = trace_store.command_meta(command)
        data.update(trace_store.prompt_meta(stdin_text, prefix="stdin"))
        trace.event("controller.probe.start", controller="claude", role=role,
                    prompt_kind="probe", mode=mode, yolo=yolo, cwd=os.getcwd(),
                    role_prompt_file=role_prompt_file,
                    mcp_free=True, mcp_mechanism="claude_empty_mcp_config",
                    **dict(data, **_probe_work()))
    try:
        events = spawn(command, stdin_text)
        seen_ok = False
        probe_usage = None
        for obj in events:
            parsed = parse_claude_event(obj)
            kind = parsed.get("kind")
            if kind == "result":
                # The result is terminal AND the only event carrying usage —
                # capture it even when an assistant event preceded it (#1/D2),
                # then stop.
                probe_usage = parsed.get("usage")
                seen_ok = True
                break
            if kind == "assistant":
                # A valid shape, but keep scanning so a following result's usage
                # is not dropped.
                seen_ok = True
        if seen_ok:
            if cache_enabled and cache_key:
                probe_cache.cache_store(cache_key, path=cache_path)
                if trace:
                    trace.event("controller.probe.cache_store",
                                controller="claude", role=role,
                                prompt_kind="probe")
            if trace:
                trace.event("controller.probe.end", controller="claude",
                            role=role, prompt_kind="probe", result="ok",
                            usage=probe_usage, usage_native=probe_usage,
                            **_probe_work(
                                usage_scope="turn_native",
                                duration_ms=_probe_elapsed_ms()))
            return True, None
    except Exception as exc:  # noqa: BLE001 - surface any spawn failure as an alert
        if trace:
            trace.event("controller.probe.end", controller="claude", role=role,
                        prompt_kind="probe", result="error",
                        error_type=type(exc).__name__,
                        **_probe_work(duration_ms=_probe_elapsed_ms()))
        return False, (
            "Could not probe `claude` stream-json input (%s).\n"
            "    Confirm `claude` is installed and supports "
            "`--input-format stream-json`." % exc
        )
    finally:
        if runtime:
            _close_guard_runtime(runtime)
    if trace:
        trace.event("controller.probe.end", controller="claude", role=role,
                    prompt_kind="probe", result="unsupported",
                    **_probe_work(duration_ms=_probe_elapsed_ms()))
    return False, (
        "`claude` did not accept the cowork stream-json stdin message shape.\n"
        "    The stdin schema is undocumented (anthropics/claude-code #24594); "
        "your claude version may differ. Update claude or report the schema."
    )


# --------------------------------------------------------------------------- #
# Thin real-subprocess drivers (not unit-tested; exercised manually).         #
# --------------------------------------------------------------------------- #


def _real_claude_spawn(command, stdin_text):
    """Run a claude command with stdin_text and yield parsed json events.

    Used by the probe in a real run. One-shot: closes stdin after writing.
    """
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    proc.stdin.write(stdin_text)
    proc.stdin.close()
    events = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    proc.wait()
    return events



# --------------------------------------------------------------------------- #
# Synchronous session bridges.                                                #
#                                                                             #
# Each `send(text)` runs exactly one turn, streams the labeled reply, and     #
# returns when the turn completes — so the caller (cowork) can read the scout #
# intel `status` between turns and decide whether to prompt or finish.        #
# --------------------------------------------------------------------------- #


class ClaudeSession:
    """One persistent `claude -p` stream-json process; one turn per send()."""

    def __init__(self, role_prompt_file, mode, yolo, io_out=None, speaker="scout",
                 session_id=None, resume_id=None, on_session_id=None,
                 region_factory=None, trace=None, extra_writable_dir=None,
                 internal=False, model=None, effort=None,
                 guard_settings_path=None, delegation_allowed=True,
                 owned_scope=None, controller_env=None,
                 declared_outputs=None, repo_writable=True, auth_run=None):
        policy.guard("claude", role=speaker, kind="dispatch")
        self.io_out = io_out or sys.stdout
        self.speaker = speaker
        self.controller = "claude"
        self.label = speaker_label(speaker)
        # internal=True streams this whole session on the dim internal channel
        # (reviewer/advisor); role sessions stay False and mark inline blocks.
        self.internal = internal
        self.on_session_id = on_session_id
        self.trace = trace
        self.mode = mode
        self.yolo = yolo
        self.model = model
        self.effort = effort
        self.role_prompt_file = role_prompt_file
        if nested_guard_active() and not (session_id or resume_id):
            session_id = str(uuid.uuid4())
        self.session_id = session_id
        self.resume_id = resume_id
        self._guard_runtime = None
        if nested_guard_active():
            self._guard_runtime = _guard_runtime(
                trace, speaker, extra_writable_dir, model, effort,
                delegation_allowed, declared_outputs=declared_outputs,
                resume_id=resume_id, repo_writable=repo_writable,
                controller_session_id=session_id or resume_id)
            guard_settings_path = self._guard_runtime["settings_path"]
            delegation_allowed = self._guard_runtime["delegation_allowed"]
            owned_scope = self._guard_runtime["scope"]
            controller_env = self._guard_runtime["env"]
            try:
                _require_controller_auth(
                    self._guard_runtime, "claude", trace, speaker, run=auth_run)
            except Exception:
                _close_guard_runtime(self._guard_runtime)
                raise
        # The LIVE model, captured from the first system-init event that names
        # it (traceability: stamped on turn results and eval score entries).
        # Distinct from `self.model`, the config-pinned request (None = the
        # CLI's own default).
        self.live_model = None
        # Markdown render region; injectable for tests. TTY: Rich Live streaming.
        # Non-TTY: raw passthrough, byte-identical to the historical stream.
        self._region_factory = region_factory or ui.StreamingMarkdown
        self._seen_session = False
        self.extra_writable_dir = extra_writable_dir
        self.controller_state_dir = (
            self._guard_runtime["scope"].controller_state_dir
            if self._guard_runtime else None)
        command = build_claude_command(role_prompt_file, mode, yolo,
                                       session_id=session_id, resume_id=resume_id,
                                       extra_writable_dir=extra_writable_dir,
                                       model=model, effort=effort,
                                       guard_settings_path=guard_settings_path,
                                       delegation_allowed=delegation_allowed)
        if owned_scope is not None:
            boundary = kernel_write_boundary(
                owned_scope, command,
                protected_paths=(self._guard_runtime or {}).get(
                    "protected_paths") or ())
            if not boundary["available"]:
                raise RuntimeError(boundary["reason"])
            command = boundary["argv"]
        if self.trace:
            self.trace.event(
                "controller.spawn.start", controller="claude", role=speaker,
                fresh=not bool(resume_id), resume=bool(resume_id), mode=mode,
                yolo=yolo, model=model, effort=effort, cwd=os.getcwd(),
                role_prompt_file=role_prompt_file,
                session_id=session_id, resume_id=resume_id,
                **trace_store.command_meta(command))
            # Item #4 measurement: the static role markdown loaded via
            # --append-system-prompt-file is a SYSTEM prompt — outside the
            # per-turn user-message prompt_bytes. Record its size separately,
            # tagged role_prompt_delivery='claude_system', once per spawn. This
            # single emission covers BOTH lead and reviewer Claude launches
            # (every ClaudeSession passes role_prompt_file through here).
            try:
                rp_bytes = os.path.getsize(role_prompt_file)
            except OSError:
                rp_bytes = None
            if rp_bytes is not None:
                self.trace.event("role.prompt.bytes", role=speaker,
                                 bytes=rp_bytes, delivery="claude_system")
        try:
            self.proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                env=controller_env,
            )
        except Exception as exc:  # noqa: BLE001
            if self.trace:
                self.trace.event(
                    "controller.spawn.end", controller="claude", role=speaker,
                    result="error", error_type=type(exc).__name__)
            raise
        if self.trace:
            self.trace.event("controller.spawn.end", controller="claude",
                             role=speaker, result="ok")

    def send(self, text, meta=None):
        """Send one user message and surface the labeled reply for one turn.

        On a TTY a `scout working…` spinner fills the gap before the first token
        (#13), then the reply renders **live** as markdown in a Rich region (#5) —
        length-independent. Off a TTY the region is a raw passthrough, byte-for-byte
        the historical token stream (so the streaming/test contract is unchanged).

        `meta` is an optional per-turn accounting dict (#1/D11) merged into the
        controller.turn.start event (prompt_kind, role, controller, phase,
        fresh/resume, round, artifact descriptors, context_revision). It is
        content-free metadata only — Trace.event drops any None field.

        The turn is also one immutable unit of work (P1): a `work_id` is minted
        here and echoed on EVERY end path, so an in-flight, failed or cancelled
        turn is joinable to its start rather than invisible (CV-005)."""
        meta = dict(meta or {})
        if (self._guard_runtime
                and not self._guard_runtime["thread"].is_alive()):
            if self.trace:
                self.trace.event("guard.broker.unavailable",
                                 role=self.speaker, result="denied")
            return turn_result(False, "denied", denied=True,
                               error_type="guard_unavailable")
        work_id = trace_store.new_work_id()
        self.last_work_id = work_id
        work_class = meta.get("work_class") or "productive"
        turn_started = time.monotonic()
        try:
            return self._send_turn(text, meta, work_id, work_class,
                                   turn_started)
        except KeyboardInterrupt:
            # Cancellation is its own terminal class (C1), not a flavour of
            # failure: elapsed is computed BEFORE re-raising so the turn keeps
            # its duration instead of losing it (P14).
            if self.trace:
                self.trace.event(
                    "controller.turn.end", controller="claude",
                    role=self.speaker, result="cancelled",
                    model=self.live_model or self.model,
                    **trace_store.work_meta(
                        work_id, "cancelled", usage_scope="turn_native",
                        identity=self._identity(),
                        duration_ms=int(
                            (time.monotonic() - turn_started) * 1000)))
            raise

    def _identity(self):
        """The canonical identity block stamped on this session's work (P1)."""
        return trace_store.identity_meta(
            controller="claude", provider="anthropic",
            model=self.live_model or self.model,
            model_source=("live_event" if self.live_model
                          else ("config_pinned" if self.model else "unknown")),
            controller_session_id=self.session_id,
            effort=self.effort,
            effort_source=("config_pinned" if self.effort else "unknown"))

    def _send_turn(self, text, meta, work_id, work_class, turn_started):
        if self._guard_runtime:
            # MJ-1: `work_id` here is the per-turn TRACE correlation id
            # (`trace_store.new_work_id()`, minted fresh every send to pair
            # controller.turn.start/end) -- never a real, durable WorkUnit
            # identity. When the caller (cowork.py's `_role_loop`) threads
            # the genuine WorkUnit `role_work_id` through `meta`, stamp THAT
            # as the guard context's `current_parent_work_id` instead, so a
            # child-dispatch/ungoverned-terminal hook payload names a parent
            # `cowork_guard_broker`'s existence check (E-WIRE-001) can
            # actually verify against the minted WorkUnit store -- a random
            # per-turn trace id never resolves there and would always fail
            # closed. Falls back to the trace id only when no real
            # WorkUnit context is available (e.g. a reviewer/advisor turn
            # with no live role engagement of its own).
            _stamp_guard_parent_work(
                self._guard_runtime, meta.get("role_work_id") or work_id)
        if self.trace:
            fields = {"controller": "claude", "role": self.speaker,
                      "mcp_free": True,
                      "mcp_mechanism": "claude_empty_mcp_config"}
            fields.update(trace_store.prompt_meta(text))
            fields.update({k: v for k, v in meta.items() if v is not None})
            fields.update(trace_store.work_meta(
                work_id, work_class, usage_scope="turn_native",
                identity=self._identity()))
            self.trace.event("controller.turn.start", **fields)

        def _elapsed_ms():
            return int((time.monotonic() - turn_started) * 1000)

        def _end(**kw):
            """Emit controller.turn.end with the work stamps always present.

            duration_ms is computed by the caller BEFORE this returns, so no end
            path can emit without one (P14)."""
            if not self.trace:
                return
            end_class = kw.pop("work_class", None) or work_class
            fields = {"controller": "claude", "role": self.speaker}
            fields.update(kw)
            fields.update(trace_store.work_meta(
                work_id, end_class, usage_scope="turn_native",
                identity=self._identity()))
            self.trace.event("controller.turn.end", **fields)

        self.proc.stdin.write(encode_user_message(text))
        self.proc.stdin.flush()
        tty = ui.is_tty(self.io_out)
        any_text = False
        denied = False
        controller_error = None
        parent_direct_usage = {}
        region = None
        idle = "%s working" % self.speaker
        status_active = False  # the region currently shows a tool-activity row
        spinner = ui.Spinner(self.io_out, idle) if tty else None
        if spinner:
            spinner.start()

        def _set_status(text):
            # Show/refresh the activity row; guarded so injected/custom regions
            # without status support keep working.
            nonlocal status_active
            st = getattr(region, "set_status", None)
            if st:
                st(text)
                status_active = True

        def _clear_status():
            nonlocal status_active
            if not status_active:
                return
            cs = getattr(region, "clear_status", None)
            if cs:
                cs()
            status_active = False

        def _feed(chunk):
            # Open the render region on the first token (after stopping the
            # gap-filling spinner), then stream into it.
            nonlocal region
            if region is None:
                if spinner:
                    spinner.stop()
                label = ui.label(self.speaker, tty)
                if self._region_factory is ui.StreamingMarkdown:
                    region = self._region_factory(
                        self.io_out, label, trace=self.trace,
                        trace_fields={
                            "controller": "claude",
                            "role": self.speaker,
                        }, internal=self.internal)
                else:
                    region = self._region_factory(self.io_out, label)
                region.__enter__()
            else:
                _clear_status()  # text resumed: drop the tool-activity row
            region.feed(chunk)

        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A new text block after the model already produced text (e.g. it
            # resumed narration after a tool call) must be separated, else the
            # blocks abut with no space ("...off.Enough recon").
            if (obj.get("type") == "stream_event" and region is not None
                    and region.buf):
                ev = obj.get("event") or {}
                if (ev.get("type") == "content_block_start"
                        and (ev.get("content_block") or {}).get("type") == "text"):
                    region.feed("\n\n")
            parsed = parse_claude_event(obj)
            sid = parsed.get("session_id")
            if sid and not self._seen_session and self.on_session_id:
                self._seen_session = True
                if self.trace:
                    self.trace.event("controller.session_id",
                                     controller="claude", role=self.speaker,
                                     session_id=sid)
                self.on_session_id(sid)
            kind = parsed["kind"]
            if kind == "assistant" and isinstance(parsed.get("usage"), dict):
                for axis, value in parsed["usage"].items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        parent_direct_usage[axis] = (
                            parent_direct_usage.get(axis, 0) + value)
            if (kind == "system" and parsed.get("model")
                    and parsed["model"] != self.live_model):
                self.live_model = parsed["model"]
                if self.trace:
                    self.trace.event("controller.model", controller="claude",
                                     role=self.speaker,
                                     model=self.live_model)
            if (kind == "system" and self._guard_runtime
                    and isinstance(parsed.get("tools"), list)):
                schemas = {}
                for tool in parsed["tools"]:
                    if not isinstance(tool, dict):
                        continue
                    name = tool.get("name")
                    schema = tool.get("input_schema") or tool.get(
                        "inputSchema")
                    if name and isinstance(schema, dict):
                        schemas[name] = schema
                self._guard_runtime["broker"].installed_schemas.update(schemas)
            if kind == "child_usage" and self._guard_runtime:
                broker = self._guard_runtime["broker"]
                child_work_id = broker.work_id_for_tool(
                    parsed.get("parent_tool_use_id"))
                if not child_work_id:
                    if self.trace:
                        self.trace.event(
                            "child.ungoverned", role=self.speaker,
                            parent_tool_use_id=parsed.get(
                                "parent_tool_use_id"))
                    _end(result="denied", work_class="failed",
                         error_type="child_ungoverned",
                         duration_ms=_elapsed_ms())
                    return turn_result(
                        False, "denied", denied=True,
                        error_type="child_ungoverned",
                        duration_ms=_elapsed_ms())
                broker.record_child_usage(child_work_id,
                                          parsed.get("usage"),
                                          event_id=parsed.get("event_id"),
                                          replayed=parsed.get("replayed"))
            if kind == "child_end" and self._guard_runtime:
                broker = self._guard_runtime["broker"]
                child_work_id = broker.work_id_for_tool(
                    parsed.get("tool_use_id"))
                if child_work_id:
                    broker.finalize_child(
                        child_work_id, agent_id=parsed.get("agent_id"),
                        terminal_source="subagent_stop")
            if kind == "partial" and parsed.get("text"):
                _feed(parsed["text"])
                any_text = True
            elif kind == "assistant" and parsed.get("text") and not any_text:
                _feed(parsed["text"])
                any_text = True
            elif kind == "tool":
                # The model is calling a tool — keep the UI alive (#loading-state).
                busy = "%s using %s" % (self.speaker, parsed.get("name") or "tool")
                if region is None:
                    if spinner:
                        spinner.set_label(busy)
                else:
                    _set_status(busy + "…")
            elif kind == "user_replay":
                # A tool_result came back; back to plain "working" until the
                # next text token or tool call.
                if region is None:
                    if spinner:
                        spinner.set_label(idle)
                elif status_active:
                    _set_status(idle + "…")
            elif kind == "denied":
                if spinner:
                    spinner.stop()
                _clear_status()  # never leave a tool label over the raw write
                denied = True
                if self.trace:
                    self.trace.event("controller.denied", controller="claude",
                                     role=self.speaker)
                    match = re.search(
                        r"guard_attempt_id=([A-Za-z0-9._:-]+)",
                        parsed.get("text") or "")
                    if match and self._guard_runtime:
                        attempt_id = match.group(1)
                        if not self._guard_runtime["broker"].has_attempt(
                                attempt_id):
                            self.trace.event(
                                "action.policy.denied_offline",
                                controller="claude", role=self.speaker,
                                guard_attempt_id=attempt_id,
                                reason="guard_unavailable")
                self.io_out.write("\n" + ui.label(self.speaker, tty) + denial_message())
            elif kind == "error":
                # Keep reading until the result event so the persistent stream
                # remains framed, but remember that a later synthetic success
                # cannot override this provider error.
                if controller_error is None:
                    controller_error = {
                        "error_type": parsed.get("error_type") or "api_error",
                        "text": parsed.get("text") or "controller API error",
                    }
                    if spinner:
                        spinner.stop()
                    _clear_status()
                    if region is not None:
                        region.__exit__(None, None, None)
                        region = None
                    self.io_out.write(
                        ui.colorize("[error] " + controller_error["text"],
                                    ui.RED, tty) + "\n")
            elif kind == "result":
                if spinner:
                    spinner.stop()
                _clear_status()
                if region is not None:
                    region.__exit__(None, None, None)  # finalize the render
                elif denied:
                    self.io_out.write("\n")
                if parsed.get("is_error") or controller_error is not None:
                    error_type = ((controller_error or {}).get("error_type")
                                  or parsed.get("subtype") or "controller_error")
                    _end(result="error", work_class="failed",
                         subtype=parsed.get("subtype"),
                         error_type=error_type,
                         model=self.live_model or self.model,
                         duration_ms=_elapsed_ms())
                    if controller_error is None:
                        self.io_out.write(
                            ui.colorize("[error] " + (parsed.get("text") or ""),
                                        ui.RED, tty) + "\n")
                    self.io_out.flush()
                    return turn_result(
                        False, "error", subtype=parsed.get("subtype"),
                        error_type=error_type,
                        session_id=sid or self.session_id,
                        model=self.live_model or self.model, duration_ms=_elapsed_ms())
                else:
                    direct_usage = parent_direct_usage or parsed.get("usage")
                    _end(result="denied" if denied else "ok",
                         subtype=parsed.get("subtype"),
                         usage=direct_usage,
                         usage_basis=("parent_message_sum"
                                      if parent_direct_usage
                                      else "parent_counter_ambiguous"),
                         provider_usage_total=parsed.get("usage"),
                         # Claude reports usage per turn already: preserved
                         # verbatim so cache_creation_input_tokens and
                         # cache_read_input_tokens are never renamed away when
                         # reconciled against Codex's differently-named fields.
                         usage_native=parsed.get("usage"),
                         model=self.live_model or self.model,
                         duration_ms=_elapsed_ms())
                self.io_out.flush()
                if denied:
                    return turn_result(
                        False, "denied", denied=True,
                        subtype=parsed.get("subtype"),
                        session_id=sid or self.session_id,
                        usage=parsed.get("usage"), model=self.live_model or self.model,
                        duration_ms=_elapsed_ms())
                return turn_result(
                    True, "ok", subtype=parsed.get("subtype"),
                    session_id=sid or self.session_id,
                    usage=parsed.get("usage"), model=self.live_model or self.model,
                    duration_ms=_elapsed_ms())
            self.io_out.flush()
        if spinner:
            spinner.stop()
        if region is not None:
            region.__exit__(None, None, None)
        # The loop-exhausted / EOF path: `_elapsed_ms()` was already in scope
        # here and simply unused, so this end emitted no duration at all (P14).
        _end(result="error", work_class="failed", error_type="eof",
             model=self.live_model or self.model, duration_ms=_elapsed_ms())
        return turn_result(False, "error", error_type="eof",
                           session_id=self.session_id,
                           duration_ms=_elapsed_ms())

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        _terminate(self.proc)
        if self._guard_runtime:
            _close_guard_runtime(self._guard_runtime)


def _resumed_usage_baseline(thread_id):
    """The thread's cumulative token totals BEFORE this process resumed it.

    Read from Codex's own rollout, which is the only place the prior total
    exists — this process did not run those turns. Returns None when the
    rollout cannot be located or read, which the caller reports as an honest
    `incomparable` first turn rather than as a fabricated baseline.

    Strictly read-only and never raises: a baseline that cannot be established
    degrades the measurement, never the run.
    """
    try:
        import cowork_ingest as ingest_logs
        path = ingest_logs.locate_codex_log(thread_id)
        if not path:
            return None
        result = ingest_logs.ingest_codex(path)
        if not result.ok:
            return None
        # The LAST cross-check total on the thread is its state at resume time.
        for turn in reversed(result.turns):
            totals = turn.get("usage_cross_check")
            if isinstance(totals, dict) and totals:
                running = {}
                for entry in result.turns:
                    usage = entry.get("usage")
                    if isinstance(usage, dict):
                        for field, value in usage.items():
                            if isinstance(value, int):
                                running[field] = running.get(field, 0) + value
                return running or None
        return None
    except Exception:  # noqa: BLE001 - a baseline never breaks a run
        return None


class CodexSession:
    """Turn-based codex bridge: first `codex exec --json`, then
    `codex exec resume <thread_id>` per send(). A spinner runs during each turn."""

    def __init__(self, mode, yolo, io_out=None, speaker="scout",
                 resume_thread_id=None, on_thread_id=None, trace=None,
                 extra_writable_dir=None, internal=False, model=None,
                 effort=None, declared_outputs=None, repo_writable=True,
                 auth_run=None):
        policy.guard("codex", role=speaker, kind="dispatch")
        self.mode = mode
        self.yolo = yolo
        self.model = model
        self.effort = effort
        self.controller = "codex"
        self.io_out = io_out or sys.stdout
        self.speaker = speaker
        self.label = speaker_label(speaker)
        # internal=True renders this whole session's turns on the dim internal
        # channel (reviewer/advisor); role sessions stay False and mark inline.
        self.internal = internal
        self.thread_id = resume_thread_id
        self.on_thread_id = on_thread_id
        self.trace = trace
        self._guard_runtime = None
        self._controller_env = None
        if nested_guard_active():
            self._guard_runtime = _guard_runtime(
                trace, speaker, extra_writable_dir, model, effort, False,
                declared_outputs=declared_outputs,
                resume_id=resume_thread_id, repo_writable=repo_writable,
                controller="codex")
            self._controller_env = self._guard_runtime["env"]
            try:
                _require_controller_auth(
                    self._guard_runtime, "codex", trace, speaker, run=auth_run)
            except Exception:
                _close_guard_runtime(self._guard_runtime)
                raise
        self.controller_state_dir = (
            self._guard_runtime["scope"].controller_state_dir
            if self._guard_runtime else None)
        # The LIVE model, captured best-effort from codex events that name it
        # (traceability: stamped on turn results and eval score entries).
        # Distinct from `self.model`, the config-pinned request (None = the
        # CLI's own default).
        self.live_model = None
        # Granted as a writable root on the fresh exec turn AND re-granted on
        # every resume (via -c sandbox_workspace_write.writable_roots), so a
        # resumed no-yolo role keeps writing its session assets outside cwd.
        self.extra_writable_dir = extra_writable_dir
        self._notified = False
        self._resuming_first = resume_thread_id is not None
        self._started = False
        # Codex reports the THREAD's running totals, not the turn's (C1). A
        # resumed turn therefore reports every prior turn's tokens again unless
        # the cumulative reading is differenced. `_cumulative_usage` holds the
        # previous reading; `usage_native` always keeps the provider's own
        # counters verbatim beside the derived per-turn `usage`.
        # A RESUMED session has a baseline it did not observe: the thread
        # already carries every earlier turn's tokens, and the first cumulative
        # reading of this process is NOT this turn's cost. Seeding it from the
        # rollout is what makes a resumed turn report its own share; when the
        # rollout cannot be read there is no honest baseline, and the first
        # resumed turn says `incomparable` rather than reporting the whole
        # thread as though this turn had spent it.
        self._cumulative_usage = None
        self._baseline_state = "fresh"
        if resume_thread_id:
            seeded = _resumed_usage_baseline(resume_thread_id)
            self._cumulative_usage = seeded
            self._baseline_state = "seeded" if seeded else "unseeded"
            if trace:
                trace.event("controller.resume.baseline", controller="codex",
                            role=speaker, thread_id=resume_thread_id,
                            state=self._baseline_state)
        self.last_work_id = None

    def _identity(self):
        """The canonical identity block stamped on this session's work (P1)."""
        return trace_store.identity_meta(
            controller="codex", provider="openai",
            model=self.live_model or self.model,
            model_source=("live_event" if self.live_model
                          else ("config_pinned" if self.model else "unknown")),
            controller_session_id=self.thread_id)

    def _turn_usage(self, cumulative):
        """Convert Codex's cumulative thread counters into THIS turn's usage.

        Returns `(usage, usage_scope)`. A field that would go negative means the
        counters are not a monotonic cumulative series for this thread, so no
        honest per-turn figure exists: the turn is marked `incomparable` and
        `usage` is None. Never a clamped or fabricated number.
        """
        if not isinstance(cumulative, dict):
            return None, "unknown"
        previous = self._cumulative_usage
        if not isinstance(previous, dict):
            self._cumulative_usage = dict(cumulative)
            if self._baseline_state == "unseeded":
                # A resumed thread whose prior total could not be read. The
                # running total here includes turns this process never ran, so
                # reporting it as this turn's cost is exactly the cumulative
                # defect. There is no honest figure, and saying so is the only
                # correct answer.
                self._baseline_state = "fresh"
                return None, "incomparable"
            # A genuinely fresh thread: the running total IS this turn's.
            return dict(cumulative), "turn_delta"
        delta = {}
        for field, value in cumulative.items():
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            before = previous.get(field)
            before = before if isinstance(before, int) and not isinstance(
                before, bool) else 0
            if value - before < 0:
                self._cumulative_usage = dict(cumulative)
                return None, "incomparable"
            delta[field] = value - before
        self._cumulative_usage = dict(cumulative)
        return delta, "turn_delta"

    def _run(self, command):
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            env=self._controller_env,
        )
        events = []
        tty = ui.is_tty(self.io_out)
        wrote_label = {"done": False}
        try:
            with _Spinner(self.io_out, label="%s working" % self.speaker) as spin:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.append(obj)
                    parsed = parse_codex_event(obj)

                    def _emit(text, render=True):
                        spin.stop()
                        if not wrote_label["done"]:
                            # A surfaced internal block (reviewer/advisor) gets a
                            # faint lead-in gap above its label so it doesn't
                            # crowd the agent text before it (no-op off a TTY).
                            if self.internal:
                                ui.internal_lead_in(self.io_out, tty)
                            self.io_out.write(ui.label(self.speaker, tty))
                            wrote_label["done"] = True
                        if render:
                            ui.render_markdown(self.io_out, text, enabled=tty,
                                               internal=self.internal)
                        else:
                            self.io_out.write(text + "\n")
                        self.io_out.flush()

                    if parsed["kind"] == "message" and parsed.get("text"):
                        _emit(parsed["text"])
                    elif parsed["kind"] == "denied":
                        _emit(denial_message(), render=False)
                    elif parsed["kind"] == "error":
                        _emit("[error] " + (parsed.get("text") or ""), render=False)
                    elif parsed["kind"] == "tool" and not wrote_label["done"]:
                        # Reflect tool activity in the spinner while it's live
                        # (it stops on the first emitted message and never
                        # restarts — codex emits its message at turn end).
                        spin.set_label("%s %s" % (self.speaker, parsed["label"]))
                    elif parsed["kind"] == "tool_done" and not wrote_label["done"]:
                        spin.set_label("%s working" % self.speaker)
            proc.wait()
        except KeyboardInterrupt:
            _terminate(proc)
            raise
        return events

    def send(self, text, meta=None):
        meta = dict(meta or {})
        work_id = trace_store.new_work_id()
        self.last_work_id = work_id
        if self._guard_runtime:
            # MJ-1: prefer the genuine WorkUnit `role_work_id` (see
            # ClaudeSession._send_turn's identical comment) over the
            # per-turn trace `work_id` when the caller supplies one.
            _stamp_guard_parent_work(
                self._guard_runtime, meta.get("role_work_id") or work_id)
        work_class = meta.get("work_class") or "productive"
        if not self._started and not self._resuming_first:
            command = build_codex_command(
                text, self.mode, self.yolo,
                extra_writable_dir=self.extra_writable_dir,
                model=self.model, effort=self.effort,
                guarded=bool(self._guard_runtime))
            fresh = True
        else:
            if not self.thread_id:
                self.io_out.write("[error] no codex thread id; cannot continue\n")
                self.io_out.flush()
                if self.trace:
                    # This path used to emit a `controller.turn.end` whose
                    # matching start is never emitted (the start comes further
                    # down, after the command is built) — an orphan end that no
                    # work_id could join. It is a REJECTED turn: it carries its
                    # own work_id and never pretends a turn began (P14).
                    self.trace.event(
                        "controller.turn.rejected", controller="codex",
                        role=self.speaker, result="error",
                        error_type="missing_thread_id",
                        **trace_store.work_meta(
                            work_id, "failed", usage_scope="unknown",
                            identity=self._identity(), duration_ms=0))
                return turn_result(False, "error",
                                   error_type="missing_thread_id")
            command = build_codex_resume_command(
                self.thread_id, text, self.mode, self.yolo,
                extra_writable_dir=self.extra_writable_dir,
                model=self.model, effort=self.effort,
                guarded=bool(self._guard_runtime))
            fresh = False
        self._started = True
        if self._guard_runtime:
            boundary = kernel_write_boundary(
                self._guard_runtime["scope"], command,
                protected_paths=self._guard_runtime.get(
                    "protected_paths") or ())
            if not boundary["available"]:
                raise RuntimeError(boundary["reason"])
            command = boundary["argv"]
        if self.trace:
            fields = {
                "controller": "codex", "role": self.speaker,
                "fresh": fresh, "resume": not fresh, "mode": self.mode,
                "yolo": self.yolo, "model": self.model, "effort": self.effort,
                "cwd": os.getcwd(), "thread_id": self.thread_id,
                "mcp_free": True, "mcp_mechanism": "codex_mcp_override",
            }
            fields.update(trace_store.command_meta(command, prompt_text=text))
            # Per-turn accounting (#1/D11): caller-supplied meta merges in last,
            # but never overrides the authoritative fresh/resume computed here.
            fields.update({k: v for k, v in meta.items()
                           if v is not None and k not in ("fresh", "resume")})
            fields.update(trace_store.work_meta(
                work_id, work_class, usage_scope="turn_delta",
                identity=self._identity()))
            self.trace.event("controller.turn.start", **fields)
        turn_started = time.monotonic()
        try:
            events = self._run(command)
        except KeyboardInterrupt:
            # Cancellation is its own terminal class, and computing elapsed
            # before re-raising is what keeps its duration (C1, P14).
            if self.trace:
                self.trace.event(
                    "controller.turn.end", controller="codex",
                    role=self.speaker, result="cancelled",
                    thread_id=self.thread_id,
                    **trace_store.work_meta(
                        work_id, "cancelled", usage_scope="unknown",
                        identity=self._identity(),
                        duration_ms=int(
                            (time.monotonic() - turn_started) * 1000)))
            raise
        except Exception as exc:  # noqa: BLE001
            # duration_ms is computed HERE rather than below the except block,
            # which is why this end used to emit without one (P14).
            failed_ms = int((time.monotonic() - turn_started) * 1000)
            if self.trace:
                self.trace.event(
                    "controller.turn.end", controller="codex",
                    role=self.speaker, result="error",
                    error_type=type(exc).__name__,
                    thread_id=self.thread_id,
                    **trace_store.work_meta(
                        work_id, "failed", usage_scope="unknown",
                        identity=self._identity(), duration_ms=failed_ms))
            return turn_result(False, "error", error_type=type(exc).__name__,
                               duration_ms=failed_ms)
        duration_ms = int((time.monotonic() - turn_started) * 1000)
        tid = capture_thread_id(events)
        if tid and not self.thread_id:
            self.thread_id = tid
            if self.trace:
                self.trace.event("controller.thread_id", controller="codex",
                                 role=self.speaker, thread_id=self.thread_id)
        if self.thread_id and self.on_thread_id and not self._notified:
            self._notified = True
            if self.trace:
                self.trace.event("controller.thread_id.notified",
                                 controller="codex", role=self.speaker,
                                 thread_id=self.thread_id)
            self.on_thread_id(self.thread_id)
        parsed_events = [parse_codex_event(obj) for obj in events]
        kinds = [p.get("kind") for p in parsed_events]
        result = "error" if "error" in kinds else "denied" if "denied" in kinds else "ok"
        # Best-effort controller-reported usage (#1/D2): the turn.completed event
        # carries it when codex exposes it; otherwise None and the field is dropped.
        usage = None
        for p in parsed_events:
            if p.get("kind") == "turn_completed" and isinstance(p.get("usage"), dict):
                usage = p["usage"]
            if (p.get("kind") in ("thread_started", "turn_completed")
                    and p.get("model") and p["model"] != self.live_model):
                self.live_model = p["model"]
                if self.trace:
                    self.trace.event("controller.model", controller="codex",
                                     role=self.speaker,
                                     model=self.live_model)
        # Older codex CLIs never name the live model in events; fall back to
        # the config-pinned model so the stamp is still meaningful.
        model = self.live_model or self.model
        # THE CUMULATIVE-CODEX FIX (C1): `usage` off turn.completed is the
        # THREAD's running total, so a resumed turn re-reports every earlier
        # turn's tokens. Difference it into this turn's own share, and keep the
        # provider's raw counters verbatim beside it as `usage_native` — the
        # derived figure never overwrites the source it came from.
        turn_usage, usage_scope = self._turn_usage(usage)
        end_class = work_class if result == "ok" else (
            "failed" if result == "error" else work_class)
        if self.trace:
            self.trace.event("controller.turn.end", controller="codex",
                             role=self.speaker, result=result,
                             thread_id=self.thread_id, event_count=len(events),
                             usage=turn_usage, usage_native=usage, model=model,
                             **trace_store.work_meta(
                                 work_id, end_class, usage_scope=usage_scope,
                                 identity=self._identity(),
                                 duration_ms=duration_ms))
        return turn_result(
            result == "ok", result, denied=(result == "denied"),
            thread_id=self.thread_id, usage=turn_usage, model=model,
            duration_ms=duration_ms)

    def close(self):
        if self._guard_runtime:
            _close_guard_runtime(self._guard_runtime)


# ORCH-052 sticky fallback: once one session's global-dir retry has SUCCEEDED,
# every later OpencodeSession in this process skips the doomed local attempt
# and delivers via the global dir directly. Set only on a successful retry so
# a one-off transient server error can't lock the whole run into fallback.
_OPENCODE_GLOBAL_DELIVERY = False


class OpencodeSession:
    """Turn-based opencode bridge: one `opencode run --format json` per send(),
    resumed with `--session <ses_id>` once the first turn reveals the id.

    Like claude (and unlike codex), the role prompt is a SYSTEM prompt — it is
    delivered via a generated agent file, not inlined into the first user
    message — so callers seed opencode sessions with `brief + context` only.
    The agent file is (re)written on every construction, fresh AND resume,
    because the per-mode permission block lives in its frontmatter."""

    def __init__(self, role_prompt_file, mode, yolo, io_out=None, speaker="scout",
                 resume_session_id=None, on_session_id=None, trace=None,
                 extra_writable_dir=None, internal=False, model=None,
                 effort=None, agent_base_dir=None):
        policy.guard("opencode", role=speaker, kind="dispatch")
        self.mode = mode
        self.yolo = yolo
        self.model = model
        self.effort = effort
        self.controller = "opencode"
        self.io_out = io_out or sys.stdout
        self.speaker = speaker
        self.label = speaker_label(speaker)
        self.internal = internal
        self.role_prompt_file = role_prompt_file
        self.session_id = resume_session_id
        self.on_session_id = on_session_id
        self.trace = trace
        self.extra_writable_dir = extra_writable_dir
        self._notified = False
        self._resuming_first = resume_session_id is not None
        self._started = False
        # One immutable unit of work per attempted send (P1): a work_id is
        # minted in send() and echoed on EVERY terminal path — success, denied,
        # provider error, empty stream, exception, missing session, delivery
        # fallback failure, or cancellation — so an in-flight, failed or
        # cancelled opencode turn is joinable to its start (matching the
        # claude/codex invariant), not invisible.
        self.last_work_id = None
        # ORCH-053: the read-only bash allowlist is glob-prefix-matched by
        # opencode and therefore injectable, so it is only emitted when the
        # run will actually be wrapped in the kernel write boundary. Off
        # darwin (or without sandbox-exec) the frontmatter keeps `bash: ask`
        # and no boundary is attempted — today's behavior.
        guard_wrapped = (nested_guard_active()
                         and sys.platform == "darwin"
                         and bool(shutil.which("sandbox-exec")))
        self.agent_name = ensure_opencode_agent(
            role_prompt_file, speaker, mode, yolo, base_dir=agent_base_dir,
            external_dir=bool(extra_writable_dir),
            assets_dir=extra_writable_dir, bash_readonly=guard_wrapped)
        self._local_agent_path = os.path.join(
            agent_base_dir or os.getcwd(), OPENCODE_AGENT_SUBDIR,
            self.agent_name + ".md")
        self._fallback_token = uuid.uuid4().hex[:8]
        self._use_global_agent = False
        self._guard_runtime = None
        self._controller_env = None
        if guard_wrapped:
            # Fails closed on darwin: no session context -> no launch,
            # never an ungoverned run with the bash allowlist active.
            self._guard_runtime = _guard_runtime(
                trace, speaker, extra_writable_dir, model, effort, False,
                resume_id=None, repo_writable=(mode == "implement"),
                controller="opencode")
            self._controller_env = self._guard_runtime["env"]
        if nested_guard_active() and self.trace:
            self.trace.event(
                "nested.guard.ready", controller="opencode", role=speaker,
                delegation="proven_absent",
                delegation_reason="task_permission_denied",
                kernel_boundary=("darwin" if self._guard_runtime
                                 else "controller_permissions"))
        if self.trace:
            # The agent file is a SYSTEM prompt (mirrors ClaudeSession's
            # role.prompt.bytes accounting, delivery-tagged for the report).
            try:
                rp_bytes = os.path.getsize(role_prompt_file)
            except OSError:
                rp_bytes = None
            if rp_bytes is not None:
                self.trace.event("role.prompt.bytes", role=speaker,
                                 bytes=rp_bytes, delivery="opencode_agent")

    def _identity(self):
        """The canonical identity block stamped on this session's work (P1).

        opencode never names the LIVE model in its event stream, so the
        config-pinned model (None = the CLI's own default) is the best identity
        available. The provider is embedded in the opencode `provider/model` id
        rather than named separately, so it is left unstamped here."""
        return trace_store.identity_meta(
            controller="opencode",
            model=self.model,
            model_source=("config_pinned" if self.model else "unknown"),
            controller_session_id=self.session_id,
            effort=self.effort,
            effort_source=("config_pinned" if self.effort else "unknown"))

    def _wrap(self, command):
        """Wrap an `opencode run` argv in the kernel write boundary when the
        guard runtime is active; identity otherwise. Applied to every command
        this session builds — including the ORCH-052 fallback rebuild — so
        no opencode process ever runs unwrapped while the bash allowlist is
        in its frontmatter."""
        if not self._guard_runtime:
            return command
        boundary = kernel_write_boundary(
            self._guard_runtime["scope"], command,
            protected_paths=self._guard_runtime.get("protected_paths") or ())
        if not boundary["available"]:
            raise RuntimeError(boundary["reason"])
        return boundary["argv"]

    def _run(self, command):
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            env=self._controller_env,
        )
        events = []
        tty = ui.is_tty(self.io_out)
        wrote_label = {"done": False}
        try:
            with _Spinner(self.io_out, label="%s working" % self.speaker) as spin:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.append(obj)
                    parsed = parse_opencode_event(obj)

                    def _emit(text, render=True):
                        spin.stop()
                        if not wrote_label["done"]:
                            if self.internal:
                                ui.internal_lead_in(self.io_out, tty)
                            self.io_out.write(ui.label(self.speaker, tty))
                            wrote_label["done"] = True
                        if render:
                            ui.render_markdown(self.io_out, text, enabled=tty,
                                               internal=self.internal)
                        else:
                            self.io_out.write(text + "\n")
                        self.io_out.flush()

                    if parsed["kind"] == "message" and parsed.get("text"):
                        _emit(parsed["text"])
                    elif parsed["kind"] == "denied":
                        _emit(denial_message(), render=False)
                    elif parsed["kind"] == "error":
                        _emit("[error] " + (parsed.get("text") or ""),
                              render=False)
                    elif parsed["kind"] == "tool" and not wrote_label["done"]:
                        spin.set_label("%s %s" % (self.speaker, parsed["label"]))
                    elif (parsed["kind"] == "tool_done"
                          and not wrote_label["done"]):
                        spin.set_label("%s working" % self.speaker)
            proc.wait()
        except KeyboardInterrupt:
            _terminate(proc)
            raise
        return events

    def _write_global_agent(self):
        """Re-deliver the already-generated agent content via the global
        config dir (same overwrite-per-spawn contract as the local file)."""
        with open(self._local_agent_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        return write_opencode_global_agent(
            content, self.speaker, self._fallback_token)

    def _agent_delivery_failed(self, turn_started, work_id):
        duration_ms = int((time.monotonic() - turn_started) * 1000)
        self.io_out.write(
            "[error] opencode failed to load the cowork agent from %s and "
            "from %s; opencode's agent loading appears broken (ORCH-052) — "
            "no model turn was run\n"
            % (self._local_agent_path, opencode_global_agent_dir()))
        self.io_out.flush()
        if self.trace:
            self.trace.event("controller.turn.end", controller="opencode",
                             role=self.speaker, result="error",
                             error_type="agent_delivery_failed",
                             session_id=self.session_id,
                             **trace_store.work_meta(
                                 work_id, "failed", usage_scope="unknown",
                                 identity=self._identity(),
                                 duration_ms=duration_ms))
        return turn_result(False, "error", error_type="agent_delivery_failed",
                           session_id=self.session_id, duration_ms=duration_ms)

    def send(self, text, meta=None):
        global _OPENCODE_GLOBAL_DELIVERY
        meta = dict(meta or {})
        # One work_id per attempted send, minted BEFORE any setup or early
        # return so EVERY terminal path is joinable — including a pre-start
        # guard-boundary or global-agent setup failure (P1/CV-005).
        work_id = trace_store.new_work_id()
        self.last_work_id = work_id
        work_class = meta.get("work_class") or "productive"
        turn_started = time.monotonic()

        def _elapsed_ms():
            return int((time.monotonic() - turn_started) * 1000)

        def _rejected(error_type, end_class):
            # A PRE-START terminal: no controller.turn.start has been emitted for
            # this work_id, so this is a REJECTED turn (never an orphan end),
            # carrying its own work_id, identity, duration, and failed/cancelled
            # classification (P14) — mirroring codex's missing_thread_id path.
            if self.trace:
                self.trace.event(
                    "controller.turn.rejected", controller="opencode",
                    role=self.speaker,
                    result=("cancelled" if end_class == "cancelled"
                            else "error"),
                    error_type=error_type, session_id=self.session_id,
                    **trace_store.work_meta(
                        work_id, end_class, usage_scope="unknown",
                        identity=self._identity(), duration_ms=_elapsed_ms()))

        def _cancelled_end():
            # A POST-START cancellation: a JOINED controller.turn.end (C1); the
            # elapsed is computed before re-raising so the turn keeps its
            # duration (P14).
            if self.trace:
                self.trace.event(
                    "controller.turn.end", controller="opencode",
                    role=self.speaker, result="cancelled",
                    session_id=self.session_id,
                    **trace_store.work_meta(
                        work_id, "cancelled", usage_scope="unknown",
                        identity=self._identity(), duration_ms=_elapsed_ms()))

        def _error_end(error_type):
            # A POST-START error: a JOINED controller.turn.end. duration_ms is
            # computed HERE so no end can emit without one (P14).
            failed_ms = _elapsed_ms()
            if self.trace:
                self.trace.event(
                    "controller.turn.end", controller="opencode",
                    role=self.speaker, result="error", error_type=error_type,
                    session_id=self.session_id,
                    **trace_store.work_meta(
                        work_id, "failed", usage_scope="unknown",
                        identity=self._identity(), duration_ms=failed_ms))
            return turn_result(False, "error", error_type=error_type,
                               session_id=self.session_id,
                               duration_ms=failed_ms)

        def _prepare(resume_id, use_global):
            # Build the opencode command (redelivering the global agent first
            # when required). EVERY step that can fail — the global-agent write,
            # the command build, and the kernel write-boundary wrap — runs HERE
            # so one caller try can stamp the correct terminal event; no setup or
            # command-building path escapes unstamped (P14).
            agent_name = self.agent_name
            global_path = None
            if use_global:
                self._use_global_agent = True
                agent_name, global_path = self._write_global_agent()
            command = self._wrap(build_opencode_command(
                agent_name, text, self.mode, self.yolo, model=self.model,
                effort=self.effort, resume_session_id=resume_id,
                directory=os.getcwd()))
            return agent_name, global_path, command

        resume_id = None
        if self._started or self._resuming_first:
            if not self.session_id:
                self.io_out.write(
                    "[error] no opencode session id; cannot continue\n")
                self.io_out.flush()
                _rejected("missing_session_id", "failed")
                return turn_result(False, "error",
                                   error_type="missing_session_id")
            resume_id = self.session_id
        fresh = resume_id is None

        # INITIAL setup (pre-start): guard parent-work stamping AND
        # command-building both do file I/O and can raise. BOTH run inside this
        # one try so a failure is a REJECTED turn — no controller.turn.start has
        # been emitted for this work_id yet, so an end would be an orphan. Guard
        # stamping is here (not right after minting) so it too is covered.
        try:
            if self._guard_runtime:
                # MJ-1: prefer the genuine WorkUnit `role_work_id` (see
                # ClaudeSession._send_turn's identical comment) over the
                # per-turn trace `work_id` when the caller supplies one.
                _stamp_guard_parent_work(
                    self._guard_runtime, meta.get("role_work_id") or work_id)
            agent_name, global_path, command = _prepare(
                resume_id, self._use_global_agent or _OPENCODE_GLOBAL_DELIVERY)
        except KeyboardInterrupt:
            _rejected("KeyboardInterrupt", "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            _rejected(type(exc).__name__, "failed")
            return turn_result(False, "error", error_type=type(exc).__name__,
                               session_id=self.session_id,
                               duration_ms=_elapsed_ms())
        self._started = True
        if self.trace:
            fields = {
                "controller": "opencode", "role": self.speaker,
                "fresh": fresh, "resume": not fresh, "mode": self.mode,
                "yolo": self.yolo, "model": self.model, "effort": self.effort,
                "cwd": os.getcwd(), "session_id": self.session_id,
                "mcp_free": True, "mcp_mechanism": "opencode_tools_deny",
            }
            fields.update(trace_store.command_meta(command, prompt_text=text))
            if meta:
                fields.update({k: v for k, v in meta.items()
                               if v is not None and k not in ("fresh", "resume")})
            fields.update(trace_store.work_meta(
                work_id, work_class, usage_scope="turn_native",
                identity=self._identity()))
            self.trace.event("controller.turn.start", **fields)

        try:
            try:
                events = self._run(command)
            finally:
                _remove_agent_file_quiet(global_path)
        except KeyboardInterrupt:
            _cancelled_end()
            raise
        except Exception as exc:  # noqa: BLE001
            return _error_end(type(exc).__name__)
        parsed_events = [parse_opencode_event(obj) for obj in events]
        fallback = False
        if is_opencode_delivery_failure(parsed_events):
            error_text = next((p.get("text") for p in parsed_events
                               if p.get("kind") == "error"), None)
            if self.trace:
                self.trace.event(
                    "opencode.agent_delivery.failed", controller="opencode",
                    role=self.speaker, agent=agent_name,
                    local_path=(None if global_path
                                else self._local_agent_path),
                    global_path=global_path, error_text=error_text,
                    event_count=len(events))
            if global_path is not None:
                # Already on the fallback path — nothing further to try.
                return self._agent_delivery_failed(turn_started, work_id)
            # FALLBACK setup (post-start): a failure or cancellation here is a
            # JOINED controller.turn.end, never an orphan start. ORCH-052: the
            # failed run died server-side before any model turn, so one retry
            # delivered via the global config dir costs nothing; the retry
            # reuses the resume id computed at the top of this send().
            try:
                agent_name, global_path, command = _prepare(resume_id, True)
            except KeyboardInterrupt:
                _cancelled_end()
                raise
            except Exception as exc:  # noqa: BLE001
                return _error_end(type(exc).__name__)
            try:
                try:
                    events = self._run(command)
                except KeyboardInterrupt:
                    _cancelled_end()
                    raise
                except Exception as exc:  # noqa: BLE001
                    return _error_end(type(exc).__name__)
            finally:
                _remove_agent_file_quiet(global_path)
            parsed_events = [parse_opencode_event(obj) for obj in events]
            retry_dead = (not events
                          or is_opencode_delivery_failure(parsed_events))
            if self.trace:
                self.trace.event(
                    "opencode.agent_delivery.fallback", controller="opencode",
                    role=self.speaker, agent=agent_name,
                    global_path=global_path,
                    result="failed" if retry_dead else "ok")
            if retry_dead:
                return self._agent_delivery_failed(turn_started, work_id)
            fallback = True
            self._use_global_agent = True
            _OPENCODE_GLOBAL_DELIVERY = True
        duration_ms = _elapsed_ms()
        sid = capture_opencode_session_id(events)
        if sid and not self.session_id:
            self.session_id = sid
            if self.trace:
                self.trace.event("controller.session_id",
                                 controller="opencode", role=self.speaker,
                                 session_id=sid)
        if self.session_id and self.on_session_id and not self._notified:
            self._notified = True
            self.on_session_id(self.session_id)
        kinds = [p.get("kind") for p in parsed_events]
        result = ("error" if "error" in kinds
                  else "denied" if "denied" in kinds else "ok")
        # A run that produced no events at all (spawn died before the stream)
        # is an error, never a silent ok — mirrors the claude eof path.
        if result == "ok" and not events:
            result = "error"
        usage = opencode_usage(events)
        # A failed turn is terminal-classed `failed`; ok/denied keep the start's
        # purpose class. opencode reports usage per turn, so the scope is native.
        end_class = work_class if result != "error" else "failed"
        # opencode events never name the live model, so the config-pinned one
        # is the best identity available (None = the CLI's own default).
        if self.trace:
            self.trace.event("controller.turn.end", controller="opencode",
                             role=self.speaker, result=result,
                             session_id=self.session_id,
                             event_count=len(events), usage=usage,
                             model=self.model, fallback=fallback,
                             **trace_store.work_meta(
                                 work_id, end_class, usage_scope="turn_native",
                                 identity=self._identity(),
                                 duration_ms=duration_ms))
        if result == "error" and not events:
            return turn_result(False, "error", error_type="no_events",
                               session_id=self.session_id,
                               duration_ms=duration_ms)
        return turn_result(
            result == "ok", result, denied=(result == "denied"),
            session_id=self.session_id, usage=usage, model=self.model,
            duration_ms=duration_ms)

    def close(self):
        _close_guard_runtime(self._guard_runtime)
        self._guard_runtime = None


# ===========================================================================
# M3 Package C -- pure controller raw-failure normalization
# ===========================================================================
#
# Everything below this line is a pure, total, side-effect-free evidence
# transform: (raw controller output) -> Package A's closed ControllerOutcome
# taxonomy (`cowork_capacity.CONTROLLER_OUTCOMES`), plus trusted reset/retry
# evidence and an unpersisted CapacityPacket candidate for quota outcomes.
# It performs zero file writes, zero cowork_state.py calls, and zero
# cowork.py imports; it makes no policy decision (no retry/fallback/overage
# permission is ever granted here) and writes nothing durable -- Package E is
# the sole writer of the real, persisted CapacityPacket via Package B.
#
# Provenance note: this repository contains no captured issue #14/#28/#61
# evidence (no references/ directory, no matching real-world text anywhere
# outside this package's own fixtures). The token/status vocabulary below is
# therefore this package's own closed, documented classification contract --
# aligned with the repository's only two ATTESTED raw claude failure shapes
# (`scripts/test_cowork.py:3079-3096`: an `assistant`/`isApiErrorMessage`
# event carrying the literal string `error="authentication_failed"`, and a
# `system`/`subtype=="api_error"` event carrying only `error={"formatted":
# "401 OAuth token expired"}` with no structured type/status field at all) --
# rather than a verified transcription of a real provider capture. Both
# attested shapes are named fixtures in the accompanying test module and
# both classify correctly (see `_AUTHENTICATION_ERROR_TOKENS` and
# `_extract_leading_http_status` below).
#
# Raw-evidence contract
# ----------------------
# Each `classify_<controller>_failure(raw)` function takes a dict describing
# one already-terminal (non-successful) controller turn. Two shapes are
# shared verbatim across all three controllers, because they name a local,
# controller-agnostic condition rather than anything the provider itself
# returned:
#
#   {"type": "local_guard", "status": "unreachable" | "denied", ...}
#       The cowork guard broker's own local signal, however the controller
#       CLI happened to surface it -- "unreachable" means the broker itself
#       could not be reached (an infra failure); "denied" means the broker
#       responded but refused because a local budget/limit was reached. Any
#       OTHER key on this dict (e.g. a free-text "detail") is carried along
#       only as descriptive evidence and is NEVER inspected to decide the
#       outcome -- classification here is a pure `status` lookup
#       (`classify_local_guard_evidence`), so text that happens to resemble a
#       provider quota message (or vice versa) cannot flip the outcome. A
#       `status` outside {"unreachable", "denied"} (missing, wrong type, or
#       any other value) is itself an unrecognized raw shape: every public
#       `classify_<controller>_failure` catches that case and returns
#       `unknown_provider_failure` rather than raising -- totality holds at
#       the public boundary even though `classify_local_guard_evidence`
#       itself still raises ValueError for a caller invoking it directly
#       with a malformed status.
#
#   {"type": "transport_error", "exception_type": <str, optional>, ...}
#       The controller process/stream could not be communicated with at all
#       (a dead duplex, a subprocess launch exception, a connection reset) --
#       never a response the provider itself returned.
#
# Every other shape is controller-specific, extending that CLI's own raw
# error envelope as already produced elsewhere in this module (see
# `parse_claude_event`/`parse_codex_event`/`parse_opencode_event`) with the
# additional machine-readable discriminants (`type`/`code`/`status`) a real
# backend attaches beside its human-readable message:
#   - claude: the stream-json `assistant` event (an `isApiErrorMessage` flag
#     OR a bare `error` field -- mirroring `parse_claude_event`'s own
#     `obj.get("isApiErrorMessage") or obj.get("error")` trigger) and the
#     `system`/`subtype=="api_error"` event.
#   - codex: the `codex exec --json` `{"type": "error", ...}` event.
#   - opencode: the `{"type": "error", "error": {"name": ..., "data": {...}}}`
#     event.
#
# Classification of the provider-specific shapes keys ONLY on machine-
# readable discriminants -- an exact-match provider error-type/code token
# (e.g. "rate_limit_error", "overloaded_error", "authentication_failed",
# "refusal") or a numeric HTTP-like status code (429/529/503/401/403) -- via
# the closed `_QUOTA_ERROR_TOKENS`/`_OVERLOAD_ERROR_TOKENS`/etc. tables
# below. A present, non-null token always takes precedence over any status
# code also present (`_classify_error_token` only ever falls back to
# `http_status` when `token` is None): a specific, named-but-unrecognized
# token is never silently overridden by a coincidental numeric status, so
# e.g. `policy_blocked` and `authentication_failed` stay distinct even when
# both a policy token and an auth-shaped status happen to appear together.
# One narrow, closed-grammar exception -- NOT a general text heuristic --
# lets the ONE attested claude shape above resolve: when a claude
# `system`/`api_error` event carries neither a `type` token nor a `status`
# int, `_extract_leading_http_status` looks for a 3-digit HTTP-like code at
# the very START of `error["formatted"]` (e.g. "401 OAuth token expired");
# it never searches for a number anywhere else in the string. A human-
# readable message otherwise rides along purely as descriptive evidence and
# is never pattern-matched to decide the outcome: an unrecognized token/
# status (or a shape with none present at all) always falls through to
# `unknown_provider_failure`, never `quota_limited` or `malformed_output` by
# default.
#
# `malformed_output` sits entirely outside this raw-failure vocabulary: it
# names a SUCCESSFUL controller turn whose role reply failed to parse as
# JSON, so it is produced by the separate `classify_role_reply_outcome`
# below, never by a `classify_<controller>_failure` call (a failed turn has
# no successful role reply to validate).
#
# Reset/retry evidence is likewise extracted structurally, never asserted by
# a caller: `extract_retry_evidence` reads an optional `raw["retry_evidence"]
# = {"source": <one of A's own TRUST_SOURCE_KINDS>, "value": <retry-after
# TEXT SHAPE>}` sub-object out of the SAME raw evidence being classified.
# `capacity_packet_candidate` calls this (and independently re-classifies
# `raw_evidence` via the matching `classify_<controller>_failure`, requiring
# `quota_limited`) BEFORE ever calling into Package A's trust/parser
# functions -- so both the trust source and the retry text are cross-checked
# facts pulled from the actual evidence, never caller-supplied assertions.

import hashlib  # noqa: E402

import cowork_capacity as capacity_contracts  # noqa: E402

# ---------------------------------------------------------------------------
# Local-guard disambiguation (structural: a `status` lookup, never text)
# ---------------------------------------------------------------------------

_LOCAL_GUARD_STATUS_OUTCOMES = {
    "unreachable": "guard_unavailable",
    "denied": "local_guard_exhausted",
}


def classify_local_guard_evidence(guard_status):
    """Map the local guard broker's own `status` to its ControllerOutcome:
    "unreachable" -> guard_unavailable (the broker itself could not be
    reached), "denied" -> local_guard_exhausted (the broker responded but a
    local budget/limit was reached). Any other value (including None, an
    empty string, or free text) raises ValueError -- callers must supply the
    broker's own honest status, never a guess derived from message text.
    Called directly, this helper is intentionally partial (a caller-contract
    violation IS an error); the three public `classify_<controller>_failure`
    functions instead catch this ValueError and degrade to
    `unknown_provider_failure`, so totality holds at the public boundary."""
    if not isinstance(guard_status, str) or guard_status not in _LOCAL_GUARD_STATUS_OUTCOMES:
        raise ValueError(
            "guard_status must be 'unreachable' or 'denied', got %r" % (guard_status,))
    return _LOCAL_GUARD_STATUS_OUTCOMES[guard_status]


def _classify_local_guard_or_unknown(guard_status):
    """The public-boundary wrapper every `classify_<controller>_failure`
    calls for a `local_guard` shape: total, never raises -- a malformed
    `guard_status` (missing, wrong type, or any value outside
    {"unreachable","denied"}) is itself an unrecognized raw shape and
    classifies `unknown_provider_failure`, exactly like any other
    unrecognized input, rather than propagating an exception out of a
    classifier whose whole contract is totality."""
    try:
        return classify_local_guard_evidence(guard_status)
    except ValueError:
        return "unknown_provider_failure"


# ---------------------------------------------------------------------------
# Provider raw-failure token/status tables (closed, exact-match only)
# ---------------------------------------------------------------------------
#
# Each set names either a real provider machine-readable error-type/code
# string (e.g. "rate_limit_error", "insufficient_quota") or, where the
# repository's own attested convention already uses an outcome-shaped
# literal directly (see the provenance note above and at module top), that
# exact literal (e.g. "authentication_failed", "usage_limit" -- the latter
# already used as a bridge-level error_type elsewhere in this file).

_QUOTA_ERROR_TOKENS = frozenset({
    "rate_limit_error", "rate_limit_exceeded", "insufficient_quota",
    "usage_limit_reached", "quota_exceeded", "session_limit_reached",
    "quota_limited", "rate_limit", "usage_limit",
})
_OVERLOAD_ERROR_TOKENS = frozenset({
    "overloaded_error", "engine_overloaded", "server_overloaded", "overloaded",
})
_AUTHENTICATION_ERROR_TOKENS = frozenset({
    "authentication_error", "invalid_api_key", "permission_error", "unauthorized",
    "authentication_failed",
})
_POLICY_ERROR_TOKENS = frozenset({
    "refusal", "policy_violation", "content_policy_violation", "policy_blocked",
})
_TRANSPORT_ERROR_TOKENS = frozenset({
    "eof", "connection_error", "connection_reset", "connection_refused",
    "timeout", "broken_pipe",
    "ConnectionError", "ConnectionResetError", "ConnectionRefusedError",
    "ConnectionAbortedError", "TimeoutError", "BrokenPipeError",
    "OSError", "FileNotFoundError",
})

_QUOTA_HTTP_STATUS = frozenset({429})
_OVERLOAD_HTTP_STATUS = frozenset({529, 503})
_AUTHENTICATION_HTTP_STATUS = frozenset({401, 403})

# A narrow, closed grammar (never a generic substring search): matches ONLY
# a 3-digit HTTP-like status code at the very START of a formatted message,
# e.g. "401 OAuth token expired" or "529 Overloaded" -- the exact shape the
# repository's one attested claude system/api_error fixture uses. A number
# appearing anywhere else in the text is never matched.
_LEADING_HTTP_STATUS_RE = re.compile(r'^\s*([1-5]\d{2})\b')


def _extract_leading_http_status(text):
    """Return the leading 3-digit HTTP-like status code in `text` (per
    `_LEADING_HTTP_STATUS_RE`), or None when `text` is not a string or does
    not open with one. This is the ONLY text inspection anywhere in this
    package's outcome-decision path, and it is deliberately narrow: it
    fires only as a last resort, when a claude `system`/`api_error` event
    carries neither a structured `type` token nor a `status` int (see
    `classify_claude_failure`)."""
    if not isinstance(text, str):
        return None
    m = _LEADING_HTTP_STATUS_RE.match(text)
    return int(m.group(1)) if m else None


def _classify_error_token(token, http_status):
    """Shared, closed decision table behind every per-controller classifier
    below. A non-None `token` is matched EXACTLY against the closed tables,
    in priority order (quota, overload, authentication, policy, transport);
    an unrecognized non-None token is `unknown_provider_failure` WITHOUT
    ever falling back to `http_status` -- a specific, named-but-unknown
    token is never silently overridden by a coincidental numeric status.
    `http_status` is consulted only when `token` is None. Never inspects
    any free-text message field."""
    if token is not None:
        if token in _QUOTA_ERROR_TOKENS:
            return "quota_limited"
        if token in _OVERLOAD_ERROR_TOKENS:
            return "overloaded"
        if token in _AUTHENTICATION_ERROR_TOKENS:
            return "authentication_failed"
        if token in _POLICY_ERROR_TOKENS:
            return "policy_blocked"
        if token in _TRANSPORT_ERROR_TOKENS:
            return "transport_failed"
        return "unknown_provider_failure"
    if http_status in _QUOTA_HTTP_STATUS:
        return "quota_limited"
    if http_status in _OVERLOAD_HTTP_STATUS:
        return "overloaded"
    if http_status in _AUTHENTICATION_HTTP_STATUS:
        return "authentication_failed"
    return "unknown_provider_failure"


def _as_str_or_none(value):
    return value if isinstance(value, str) else None


def _as_int_status_or_none(value):
    # bool is an int subclass; never treat True/False as an HTTP-like status.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Per-controller raw-failure classifiers
# ---------------------------------------------------------------------------

def classify_claude_failure(raw):
    """Classify one raw, terminal (non-successful) claude controller failure
    into Package A's closed ControllerOutcome taxonomy. `raw` is one of:
    the shared `local_guard`/`transport_error` shapes documented above; a
    claude `assistant` event carrying an `isApiErrorMessage` flag or a bare
    `error` field (mirroring `parse_claude_event`'s own trigger condition;
    `error` carries the CLI's own already-classified error token); or a
    claude `system`/`subtype=="api_error"` event (`error` is a dict whose
    `type`/`status` are the machine-readable discriminants -- falling back,
    ONLY when both are absent, to a leading HTTP-like status code in
    `error["formatted"]`; see `_extract_leading_http_status`). Any other
    shape -- including a recognized event `type` with no recognized
    discriminant at all, or a wholly unrecognized `type` -- is
    `unknown_provider_failure`. A malformed `local_guard` shape is also
    `unknown_provider_failure`, never a raised exception."""
    if not isinstance(raw, dict):
        raise ValueError("raw must be a dict, got %r" % (type(raw),))
    rtype = raw.get("type")
    if rtype == "local_guard":
        return _classify_local_guard_or_unknown(raw.get("status"))
    if rtype == "transport_error":
        return "transport_failed"
    if rtype == "assistant" and (raw.get("isApiErrorMessage") or raw.get("error")):
        return _classify_error_token(_as_str_or_none(raw.get("error")), None)
    if rtype == "system" and raw.get("subtype") == "api_error":
        error = raw.get("error")
        error = error if isinstance(error, dict) else {}
        token = _as_str_or_none(error.get("type"))
        status = _as_int_status_or_none(error.get("status"))
        if token is None and status is None:
            status = _extract_leading_http_status(error.get("formatted"))
        return _classify_error_token(token, status)
    return "unknown_provider_failure"


def classify_codex_failure(raw):
    """Classify one raw, terminal codex controller failure into Package A's
    closed ControllerOutcome taxonomy. `raw` is one of the shared
    `local_guard`/`transport_error` shapes, or a codex `{"type": "error",
    "message": <str>, "code": <str, optional>, "status": <int, optional>}`
    event -- `code`/`status` are the machine-readable discriminants,
    `message` is descriptive-only text. Any other shape is
    `unknown_provider_failure`; a malformed `local_guard` shape is also
    `unknown_provider_failure`, never a raised exception."""
    if not isinstance(raw, dict):
        raise ValueError("raw must be a dict, got %r" % (type(raw),))
    rtype = raw.get("type")
    if rtype == "local_guard":
        return _classify_local_guard_or_unknown(raw.get("status"))
    if rtype == "transport_error":
        return "transport_failed"
    if rtype == "error":
        return _classify_error_token(
            _as_str_or_none(raw.get("code")),
            _as_int_status_or_none(raw.get("status")))
    return "unknown_provider_failure"


def classify_opencode_failure(raw):
    """Classify one raw, terminal opencode controller failure into Package
    A's closed ControllerOutcome taxonomy. `raw` is one of the shared
    `local_guard`/`transport_error` shapes, or an opencode `{"type": "error",
    "error": {"name": <str, optional>, "data": {"message": <str, optional>,
    "status": <int, optional>}}}` event (mirroring `parse_opencode_event`'s
    own error envelope) -- `name`/`data.status` are the machine-readable
    discriminants, `data.message` is descriptive-only text. Any other shape
    is `unknown_provider_failure`; a malformed `local_guard` shape is also
    `unknown_provider_failure`, never a raised exception."""
    if not isinstance(raw, dict):
        raise ValueError("raw must be a dict, got %r" % (type(raw),))
    rtype = raw.get("type")
    if rtype == "local_guard":
        return _classify_local_guard_or_unknown(raw.get("status"))
    if rtype == "transport_error":
        return "transport_failed"
    if rtype == "error":
        error = raw.get("error")
        error = error if isinstance(error, dict) else {}
        data = error.get("data")
        data = data if isinstance(data, dict) else {}
        return _classify_error_token(
            _as_str_or_none(error.get("name")),
            _as_int_status_or_none(data.get("status")))
    return "unknown_provider_failure"


_CONTROLLER_FAILURE_CLASSIFIERS = {
    "claude": classify_claude_failure,
    "codex": classify_codex_failure,
    "opencode": classify_opencode_failure,
}


# ---------------------------------------------------------------------------
# Successful-turn role-output classification (malformed_output)
# ---------------------------------------------------------------------------

def classify_role_reply_outcome(turn_succeeded, role_json_valid):
    """The ONLY path to `malformed_output`: a controller turn that itself
    SUCCEEDED (no provider-level failure) but whose role reply did not parse
    as the expected JSON. Returns "malformed_output" when
    `role_json_valid` is False, or None when the reply parsed fine (a
    genuinely successful turn, entirely outside the ControllerOutcome
    taxonomy -- callers should not classify it at all).

    Raises ValueError when `turn_succeeded` is not True: a failed turn is
    never eligible for `malformed_output` regardless of what its role output
    looked like -- use one of the `classify_<controller>_failure` functions
    instead. This is what keeps a parse failure on a genuinely successful
    turn from ever being conflated with a raw, unclassifiable provider
    failure (`unknown_provider_failure`), and vice versa."""
    if turn_succeeded is not True:
        raise ValueError(
            "classify_role_reply_outcome requires a successful turn "
            "(turn_succeeded=True); got %r" % (turn_succeeded,))
    if role_json_valid is not True and role_json_valid is not False:
        raise ValueError(
            "role_json_valid must be a bool, got %r" % (role_json_valid,))
    return None if role_json_valid else "malformed_output"


# ---------------------------------------------------------------------------
# Structural reset/retry evidence extraction (never a caller assertion)
# ---------------------------------------------------------------------------

# Any raw evidence that does not carry a well-shaped, closed-set-sourced
# retry_evidence sub-object degrades to this explicit sentinel rather than
# `None`: `capacity_source.kind` must always be a valid nonempty string (so
# it always satisfies `validate_capacity_source`'s shape check and can
# always be copied verbatim), while still classifying "untrustworthy" via
# A's `classify_trust_source` (this literal is deliberately outside
# `TRUST_SOURCE_KINDS`).
_UNVERIFIED_EVIDENCE_SOURCE = "unverified"


def extract_retry_evidence(raw):
    """Pure structural extraction of reset/retry evidence from a raw
    controller-output dict -- never caller-asserted, never inferred from
    any other field (message text, http_status, etc). Returns
    {"source": <str>, "value": <str or None>}.

    The ONLY recognized shape is an optional `raw["retry_evidence"]` dict of
    exactly `{"source", "value"}` where `source` is one of Package A's own
    `TRUST_SOURCE_KINDS` ("provider_header", "provider_event",
    "provider_api") -- literally naming HOW the retry/reset value was
    actually obtained -- and `value` is a nonempty retry-after/reset TEXT
    SHAPE string. Any other raw shape (non-dict `raw`, missing key, wrong
    type, a `source` outside A's closed set, or a missing/empty `value`)
    extracts to `{"source": "unverified", "value": None}` -- absent,
    unverifiable evidence, never a guessed or asserted one -- which A's
    `classify_trust_source` then correctly degrades to "untrustworthy"
    rather than raising."""
    absent = {"source": _UNVERIFIED_EVIDENCE_SOURCE, "value": None}
    if not isinstance(raw, dict):
        return absent
    evidence = raw.get("retry_evidence")
    if not isinstance(evidence, dict):
        return absent
    source = evidence.get("source")
    value = evidence.get("value")
    if not isinstance(source, str) or source not in capacity_contracts.TRUST_SOURCE_KINDS:
        return absent
    if not isinstance(value, str) or not value:
        return absent
    return {"source": source, "value": value}


# ---------------------------------------------------------------------------
# Unpersisted CapacityPacket candidate (quota_limited outcomes only)
# ---------------------------------------------------------------------------

def capacity_packet_candidate(controller, provider, raw_evidence, issued_at):
    """Build the pure, unpersisted CapacityPacket CANDIDATE for a
    `quota_limited` classification -- Package A's `capacity.json` shape,
    partially populated with exactly what a bridge-level evidence-
    normalization step can honestly know.

    `controller`: one of "claude"/"codex"/"opencode". This function FIRST
    re-classifies `raw_evidence` via the matching `classify_<controller>_
    failure` and requires the result to be exactly `quota_limited`, raising
    ValueError otherwise -- a cross-check that runs BEFORE any call into
    Package A's trust/parser functions, so this candidate can never be built
    from evidence this package's own classifier would not itself call a
    quota signal.

    `raw_evidence`: the exact raw controller-output dict that produced the
    quota_limited outcome. Hashed verbatim (via `json.dumps(...,
    sort_keys=True)`, with NO type coercion) into `capacity_source["sha256"]`
    -- this package never fabricates, reshapes, or silently coerces the
    evidence it hashes; a `raw_evidence` containing a non-JSON-serializable
    value raises ValueError rather than producing a non-reproducible digest.

    Reset/retry evidence and its provenance are BOTH extracted from
    `raw_evidence` itself via `extract_retry_evidence` -- never accepted as
    separate caller-supplied parameters, so a caller can never assert a
    trust level or retry text the actual evidence does not carry. The
    extracted `source` is passed UNCHANGED into A's `classify_trust_source`;
    this package computes no trust level of its own and never upgrades an
    untrustworthy source. The extracted `value` (or None) is parsed via A's
    `parse_retry_after_text` only to decide `resume_mode` ("scheduled" when
    it parses, else "manual_signal") -- never itself checked against
    MAX_RETRY_HORIZON_SECONDS here (that bound belongs to
    `validate_capacity_packet`, which only Package E can call once this
    candidate's identity/lease fields are filled in for real).

    `issued_at` must be an RFC3339-shaped timestamp string (validated via
    A's own `rfc3339_to_epoch_seconds`); `provider` must be a nonempty
    string.

    Returns a dict with exactly: schema_version, provider,
    provider_capacity_class (always "subscription_quota_exhausted" -- direct
    Claude, subscription_only per the frozen brief; no credits/overage/
    alias/fallback capacity class exists to name), resume_mode, retry_after,
    capacity_source (exactly Package A's `{"kind", "sha256"}` shape, `kind`
    ALWAYS a valid nonempty string -- either a genuine TRUST_SOURCE_KINDS
    member or the explicit "unverified" sentinel -- so it always satisfies
    `validate_capacity_source` and Package E can copy it verbatim into a
    real CapacityPacket), issued_at, and a sibling diagnostic-only `trust`
    field (NOT part of the durable CapacityPacket schema; Package E copies
    `capacity_source` in, never `trust`).

    `package_id`, `binding`, `wakeup`, and `manual_resume` are deliberately
    ABSENT (not merely null): only Package E, wiring this candidate into a
    real WorkUnit/session/lease/dispatch-package context, knows those
    identity fields, so this candidate can never satisfy
    `validate_capacity_packet`'s exact key set on its own -- it is never
    mistakable for, and can never be persisted as, a complete, durable
    CapacityPacket."""
    classify = _CONTROLLER_FAILURE_CLASSIFIERS.get(controller)
    if classify is None:
        raise ValueError(
            "controller must be one of %s, got %r"
            % (sorted(_CONTROLLER_FAILURE_CLASSIFIERS), controller))
    outcome = classify(raw_evidence)
    if outcome != "quota_limited":
        raise ValueError(
            "capacity_packet_candidate requires raw_evidence to classify "
            "quota_limited via classify_%s_failure; got %r"
            % (controller, outcome))
    if not isinstance(provider, str) or not provider:
        raise ValueError("provider must be a nonempty string, got %r" % (provider,))
    if capacity_contracts.rfc3339_to_epoch_seconds(issued_at) is None:
        raise ValueError(
            "issued_at must be an RFC3339-shaped timestamp string, got %r" % (issued_at,))

    retry_evidence = extract_retry_evidence(raw_evidence)
    evidence_source = retry_evidence["source"]
    retry_after = retry_evidence["value"]
    parsed_retry = capacity_contracts.parse_retry_after_text(retry_after)
    resume_mode = "scheduled" if parsed_retry is not None else "manual_signal"

    try:
        evidence_bytes = json.dumps(raw_evidence, sort_keys=True).encode("utf-8")
    except TypeError as exc:
        raise ValueError(
            "raw_evidence must be JSON-serializable for verbatim hashing: %s" % (exc,))
    digest = hashlib.sha256(evidence_bytes).hexdigest()

    return {
        "schema_version": capacity_contracts.SCHEMA_VERSION,
        "provider": provider,
        "provider_capacity_class": "subscription_quota_exhausted",
        "resume_mode": resume_mode,
        "retry_after": retry_after if resume_mode == "scheduled" else None,
        "capacity_source": {"kind": evidence_source, "sha256": digest},
        "issued_at": issued_at,
        "trust": capacity_contracts.classify_trust_source(evidence_source),
    }
