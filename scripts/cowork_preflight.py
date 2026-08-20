#!/usr/bin/env python3
"""cowork preflight helpers.

Normal runs check Python/UI dependencies globally and check controller CLIs
on-demand when a role launches, so missing active controllers can reach the
switch-controller recovery gate. `cowork --check` still uses this module to
diagnose all controller CLIs in one shot.

Python 3.9+, stdlib only.
"""

import importlib.util
import os
import shutil
import socket as _socket_mod
import sys
import time

# Interpreter floor. cowork targets 3.9 so it runs on the local interpreter
# without forcing an upgrade.
MIN_PY = (3, 9)

# Exact install commands surfaced when a required CLI tool is missing.
INSTALL_HINTS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": (
        "npm install -g @openai/codex   (Node 18+)\n"
        "    or: brew install --cask codex"
    ),
    "opencode": (
        "curl -fsSL https://opencode.ai/install | bash\n"
        "    or: npm install -g opencode-ai / brew install sst/tap/opencode"
    ),
}

# Python packages powering the interactive UX: prompt_toolkit (conversation
# input), rich (streaming markdown + banners), questionary (menus + confirm).
# Checked by import, not on PATH. Map import-name -> pip-name (identical here).
PY_PACKAGES = ["rich", "prompt_toolkit", "questionary"]
PY_PACKAGE_HINT = (
    "pip install -r requirements.txt\n"
    "    (or: pip install rich prompt_toolkit questionary)"
)


def check_python(version_info=sys.version_info):
    """Return (ok, alert_or_None) for the interpreter floor."""
    if tuple(version_info[:2]) >= MIN_PY:
        return True, None
    detected = "%d.%d.%d" % (version_info[0], version_info[1], version_info[2])
    need = "%d.%d" % MIN_PY
    alert = (
        "cowork needs Python %s or newer; detected %s.\n"
        "    Install/select a newer Python (e.g. via pyenv, python.org, or your "
        "package manager) and rerun cowork." % (need, detected)
    )
    return False, alert


def required_controllers(role_config):
    """Distinct controller CLIs required by the selected roles.

    role_config: mapping role -> dict with a "controller" key ("claude"/"codex").
    """
    controllers = []
    for cfg in role_config.values():
        ctrl = cfg.get("controller")
        if ctrl and ctrl not in controllers:
            controllers.append(ctrl)
    return controllers


def check_tools(tools, which=shutil.which):
    """Return (ok, [alerts]) for the executables that must be on PATH."""
    alerts = []
    for tool in tools:
        if which(tool) is None:
            hint = INSTALL_HINTS.get(tool, "install the %r tool" % tool)
            alerts.append(
                "Required tool %r not found on PATH.\n    Install it with: %s"
                % (tool, hint)
            )
    return (len(alerts) == 0), alerts


def check_governed_runtime(controllers, platform=sys.platform):
    """Report controller rows that cannot run under the governance boundary."""
    if not str(platform).startswith("linux"):
        return True, []
    controllers = list(controllers or ())
    if not controllers:
        return True, []
    alerts = []
    for controller in controllers:
        if controller == "claude":
            reason = (
                "isolated Claude credentials/state provisioning is not "
                "implemented")
        else:
            reason = "no pre-child delegation decision is available"
        alerts.append(
            "Controller %r is unavailable for governed Cowork roles on Linux: "
            "%s. No controller will be launched." % (controller, reason))
    return False, alerts


# Backwards-compatible alias.
check_controllers = check_tools


def check_python_packages(packages, find_spec=importlib.util.find_spec):
    """Return (ok, [alerts]) for importable Python packages. `find_spec` is
    injectable for tests (return None to simulate a missing package)."""
    alerts = []
    for pkg in packages:
        try:
            missing = find_spec(pkg) is None
        except (ImportError, ValueError):
            missing = True
        if missing:
            alerts.append(
                "Required Python package %r not installed.\n    Install it with: %s"
                % (pkg, PY_PACKAGE_HINT)
            )
    return (len(alerts) == 0), alerts


def preflight(role_config, version_info=sys.version_info, which=shutil.which,
              interactive=True, find_spec=importlib.util.find_spec,
              platform=sys.platform):
    """Run all preflight checks. Return (ok, [alerts]).

    The rich UX packages (rich/prompt_toolkit/questionary) are required only for
    the interactive flow; the non-interactive args path (--team/--config/--context)
    uses the plain readline fallback and needs none of them. Every alert is
    collected so the user sees all problems at once.
    """
    alerts = []
    py_ok, py_alert = check_python(version_info)
    if not py_ok:
        alerts.append(py_alert)

    tools = list(required_controllers(role_config))
    tools_ok, tool_alerts = check_tools(tools, which=which)
    alerts.extend(tool_alerts)
    runtime_ok, runtime_alerts = check_governed_runtime(
        tools, platform=platform)
    alerts.extend(runtime_alerts)

    pkg_ok = True
    if interactive:
        pkg_ok, pkg_alerts = check_python_packages(PY_PACKAGES, find_spec=find_spec)
        alerts.extend(pkg_alerts)

    return (py_ok and tools_ok and runtime_ok and pkg_ok), alerts


def main(argv=None):
    """`cowork --check`-style entry: report preflight for all controllers."""
    # Without a chosen team, check the default controllers so the user learns
    # what is missing up front. opencode is optional (it only matters if a role
    # is configured to use it), so a missing opencode is reported as info and
    # never fails the check.
    role_config = {
        "_claude": {"controller": "claude"},
        "_codex": {"controller": "codex"},
    }
    ok, alerts = preflight(role_config, interactive=True)
    opencode_ok, _ = check_tools(["opencode"])
    if ok:
        print("cowork preflight: OK")
        if not opencode_ok:
            print("note: optional controller 'opencode' not found on PATH "
                  "(only needed when a role is configured to use it).\n"
                  "    Install it with: " + INSTALL_HINTS["opencode"])
        return 0
    sys.stderr.write("cowork preflight failed:\n")
    for alert in alerts:
        sys.stderr.write("  - " + alert + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# P2: Capability manifest preflight
# ---------------------------------------------------------------------------

def capability_check_result(capability, ok, reason="", repair_hint=""):
    """Build a CapabilityCheckResult with exactly {capability, ok, reason, repair_hint}."""
    return {
        "capability": capability,
        "ok": bool(ok),
        "reason": reason,
        "repair_hint": repair_hint,
    }


def check_runtime_roots(capability):
    """Fail if any declared runtime root does not exist."""
    cap = capability or {}
    for root in (cap.get("runtime_roots") or []):
        if not os.path.exists(root):
            return capability_check_result(
                "runtime_roots", False,
                reason="runtime root does not exist: %s" % root,
                repair_hint="create the directory or update capability.runtime_roots")
    return capability_check_result("runtime_roots", True)


def check_private_paths(capability):
    """Fail if a private path is missing or world-readable."""
    cap = capability or {}
    for path in (cap.get("private_paths") or []):
        if not path:
            continue
        if not os.path.lexists(path):
            return capability_check_result(
                "private_paths", False,
                reason="private path missing: %s" % path,
                repair_hint="initialize the private path before dispatch")
        try:
            if os.stat(path).st_mode & 0o004:
                return capability_check_result(
                    "private_paths", False,
                    reason="private path is world-readable: %s" % path,
                    repair_hint="restrict permissions: chmod o-r %s" % path)
        except OSError:
            pass
    return capability_check_result("private_paths", True)


def _default_guard_connect(socket_path, timeout):
    """AF_UNIX connect attempt used by check_guard_socket by default."""
    s = _socket_mod.socket(_socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(socket_path)
        s.close()
        return True, None
    except FileNotFoundError:
        return False, "socket not found"
    except ConnectionRefusedError:
        return False, "connection refused"
    except _socket_mod.timeout:
        return False, "connection timed out"
    except OSError as exc:
        return False, str(exc)


def check_guard_socket(capability, connect_fn=None):
    """Fail if guard_required is True and the guard socket is unreachable.

    connect_fn(socket_path, timeout) -> (ok: bool, reason: str | None).
    Defaults to an AF_UNIX connection attempt.
    """
    cap = capability or {}
    if not cap.get("guard_required"):
        return capability_check_result("guard_socket", True)
    socket_path = cap.get("socket")
    if not socket_path:
        return capability_check_result(
            "guard_socket", False,
            reason="guard_required but capability.socket is absent",
            repair_hint="set capability.socket to the guard socket path")
    fn = connect_fn if connect_fn is not None else _default_guard_connect
    ok, reason = fn(socket_path, 2.0)
    if not ok:
        return capability_check_result(
            "guard_socket", False,
            reason="guard socket unreachable: %s" % reason,
            repair_hint="start the guard process or fix the socket path")
    return capability_check_result("guard_socket", True)


def check_kernel_boundary(capability, platform=None):
    """On Linux, fail when the capability crosses any kernel boundaries."""
    if platform is None:
        platform = sys.platform
    cap = capability or {}
    crosses = ((cap.get("kernel_boundary") or {}).get("crosses") or [])
    if str(platform).startswith("linux") and crosses:
        return capability_check_result(
            "kernel_boundary", False,
            reason="kernel boundary crossed on Linux (isolation unavailable): %s" % crosses,
            repair_hint="run on a non-Linux host or remove kernel boundary crossings")
    return capability_check_result("kernel_boundary", True)


def check_artifact_destinations(capability):
    """Fail if any artifact write destination is empty or contains path traversal."""
    cap = capability or {}
    for dest in (cap.get("artifact_writes") or []):
        if not dest:
            return capability_check_result(
                "artifact_destinations", False,
                reason="empty artifact destination in capability.artifact_writes",
                repair_hint="remove empty entries from capability.artifact_writes")
        parts = dest.replace("\\", "/").split("/")
        if ".." in parts:
            return capability_check_result(
                "artifact_destinations", False,
                reason="artifact destination contains path traversal: %s" % dest,
                repair_hint="use paths without '..' in capability.artifact_writes")
    return capability_check_result("artifact_destinations", True)


def check_controller_config(capability, binding):
    """Fail when cowork_action_policy.capability_decision() denies the controller."""
    import cowork_action_policy as _action_policy
    cap = capability or {}
    bind = binding or {}
    controller = bind.get("controller") or ""
    policy_snap = bind.get("policy_snapshot") or {}
    delegation = policy_snap.get("delegation") or "unknown"
    mutation_gate = policy_snap.get("mutation_gate") or "none"
    crosses = ((cap.get("kernel_boundary") or {}).get("crosses") or [])
    kernel_boundary = not bool(crosses)
    action_classes = cap.get("action_classes") or []
    mode = ("implement"
            if any(c in action_classes for c in ("write", "exec"))
            else "plan")
    decision = _action_policy.capability_decision(
        controller=controller,
        mode=mode,
        delegation=delegation,
        mutation_gate=mutation_gate,
        kernel_boundary=kernel_boundary,
    )
    if not decision.get("allow"):
        return capability_check_result(
            "controller_config", False,
            reason=decision.get("reason") or "capability_denied",
            repair_hint="consult the controller capability matrix")
    return capability_check_result("controller_config", True)


_CODEX_CONFIG_MAX_AGE = 60 * 60 * 24 * 7  # 7 days in seconds


def _codex_config_path():
    """Resolve the Codex config file path via CODEX_HOME or the default location."""
    home = os.environ.get("CODEX_HOME")
    if home:
        base = os.path.abspath(os.path.expanduser(home))
    else:
        base = os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(base, "config.toml")


def check_codex_config_freshness(capability, binding, stat_fn=None):
    """Fail if the Codex config is absent or its mtime exceeds _CODEX_CONFIG_MAX_AGE.

    stat_fn(path) -> os.stat_result is injectable so tests perform no real I/O.
    Only checked when binding.controller == 'codex'.
    """
    if stat_fn is None:
        stat_fn = os.stat
    bind = binding or {}
    if bind.get("controller") != "codex":
        return capability_check_result("codex_config_freshness", True)
    config_path = _codex_config_path()
    try:
        st = stat_fn(config_path)
    except OSError:
        return capability_check_result(
            "codex_config_freshness", False,
            reason="Codex config not found: %s" % config_path,
            repair_hint="initialize Codex config or set CODEX_HOME")
    age = time.time() - st.st_mtime
    if age > _CODEX_CONFIG_MAX_AGE:
        return capability_check_result(
            "codex_config_freshness", False,
            reason="Codex config is stale (age %.0fs > max %ds)" % (age, _CODEX_CONFIG_MAX_AGE),
            repair_hint="update the Codex config to refresh its mtime")
    return capability_check_result("codex_config_freshness", True)


def run_manifest_preflight(manifest, connect_fn=None, stat_fn=None, platform=None):
    """Run all capability checks against the manifest. Return a new manifest.

    Does not mutate the input manifest (deep copy). First failure wins — checks
    stop at the first failing result. All checks passing → status.phase='proven';
    any failure → status.phase='refused' with a deterministic refusal block.
    """
    import cowork_dispatch_manifest as _manifest_mod

    if platform is None:
        platform = sys.platform

    cap = (manifest or {}).get("capability") or {}
    bind = (manifest or {}).get("binding") or {}

    checks_run = []
    first_failure = None

    for check_fn in (
        lambda: check_runtime_roots(cap),
        lambda: check_private_paths(cap),
        lambda: check_guard_socket(cap, connect_fn=connect_fn),
        lambda: check_kernel_boundary(cap, platform=platform),
        lambda: check_artifact_destinations(cap),
        lambda: check_controller_config(cap, bind),
        lambda: check_codex_config_freshness(cap, bind, stat_fn=stat_fn),
    ):
        result = check_fn()
        checks_run.append(result)
        if not result["ok"]:
            first_failure = result
            break

    if first_failure is None:
        return _manifest_mod.manifest_proven(manifest, checks_run)
    return _manifest_mod.manifest_refused(
        manifest, checks_run,
        refusal_code=first_failure["capability"],
        refusal_message=first_failure["reason"] or "preflight check failed",
    )
