#!/usr/bin/env python3
"""cowork preflight helpers.

Normal runs check Python/UI dependencies globally and check controller CLIs
on-demand when a role launches, so missing active controllers can reach the
switch-controller recovery gate. `cowork --check` still uses this module to
diagnose all controller CLIs in one shot.

Python 3.9+, stdlib only.
"""

import importlib.util
import shutil
import sys

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
              interactive=True, find_spec=importlib.util.find_spec):
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

    pkg_ok = True
    if interactive:
        pkg_ok, pkg_alerts = check_python_packages(PY_PACKAGES, find_spec=find_spec)
        alerts.extend(pkg_alerts)

    return (py_ok and tools_ok and pkg_ok), alerts


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
