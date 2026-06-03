#!/usr/bin/env python3
"""cowork preflight: verify the interpreter and the CLI tools the selected
controllers need exist before anything is spun up.

Additive to the co-plan skill; does not import or modify co_plan_file.py.
Python 3.9+, stdlib only.
"""

import shutil
import sys

# Interpreter floor. cowork targets 3.9 so it runs on the local interpreter
# without forcing an upgrade. (co_plan_file.py keeps its own 3.10 requirement.)
MIN_PY = (3, 9)

# Exact install commands surfaced when a required tool is missing.
INSTALL_HINTS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": (
        "npm install -g @openai/codex   (Node 18+)\n"
        "    or: brew install --cask codex"
    ),
    "gum": (
        "brew install gum\n"
        "    or see https://github.com/charmbracelet/gum#installation"
    ),
}


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


def preflight(role_config, version_info=sys.version_info, which=shutil.which,
              interactive=True):
    """Run all preflight checks. Return (ok, [alerts]).

    `gum` is required only for the interactive menus; the non-interactive args
    path (--team/--config/--context) does not use it. Every alert is collected
    so the user sees all problems at once.
    """
    alerts = []
    py_ok, py_alert = check_python(version_info)
    if not py_ok:
        alerts.append(py_alert)

    tools = list(required_controllers(role_config))
    if interactive:
        tools.append("gum")
    tools_ok, tool_alerts = check_tools(tools, which=which)
    alerts.extend(tool_alerts)

    return (py_ok and tools_ok), alerts


def main(argv=None):
    """`cowork --check`-style entry: report preflight for all controllers."""
    # Without a chosen team, check both controllers so the user learns what is
    # missing up front.
    role_config = {
        "_claude": {"controller": "claude"},
        "_codex": {"controller": "codex"},
    }
    ok, alerts = preflight(role_config, interactive=True)
    if ok:
        print("cowork preflight: OK")
        return 0
    sys.stderr.write("cowork preflight failed:\n")
    for alert in alerts:
        sys.stderr.write("  - " + alert + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
