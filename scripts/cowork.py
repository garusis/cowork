#!/usr/bin/env python3
"""cowork: multi-role CLI orchestration entry flow + the scouting, planning,
and building phases.

The 3-step entry flow (team checklist, per-role tool config, initial context),
the preflight dependency check, and a phase loop that drives the user-facing
roles by spawning the selected CLI and bridging it to the user: the `scout`
(paired with the `scout-reviewer`) gathers context; on intel approval the
`planner` (paired with the `planning-advisor`) turns it into a plan; on plan
approval the `builder` (paired with the `build-reviewer`) executes it. Each
edge has a user-confirmed hand-back to its pre-processor (planner -> scout,
builder -> planner). Build approval ends the run with no git side effects.

Selection uses questionary for real interactive checkbox/choice menus. A
non-interactive args path (--team/--config/--context) skips the menus entirely
so the flow is testable and scriptable.

Python 3.9+, stdlib only.
"""

import argparse
import collections
import contextlib
import datetime
import errno
import glob
import hashlib
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_bridge as bridge  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_report  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_ui as ui  # noqa: E402
import cowork_policy as policy  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_ingest as ingest  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_verification as verification  # noqa: E402
import cowork_eval as evaluation  # noqa: E402
import cowork_dispatch as dispatch  # noqa: E402
import cowork_dispatch_manifest as dispatch_manifest  # noqa: E402
import cowork_guard_broker as guard_broker  # noqa: E402
import cowork_workunit as workunit  # noqa: E402
import cowork_control_plane as control_plane  # noqa: E402
import cowork_recovery_breaker as recovery_breaker  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOUT_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "scout.md")
SCOUT_REVIEWER_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "scout-reviewer.md")
PLANNER_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "planner.md")
PLANNING_ADVISOR_PROMPT_PATH = os.path.join(
    SKILL_ROOT, "roles", "planning-advisor.md")
BUILDER_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "builder.md")
BUILD_REVIEWER_PROMPT_PATH = os.path.join(
    SKILL_ROOT, "roles", "build-reviewer.md")
# The worktree role is a lightweight PRE-PHASE step (runs before scouting when
# --worktree is set), NOT a member of the scout->build ROLES tuple: it has no
# paired reviewer and no approval gate (D4). It creates a git worktree following
# the repo's own convention and the session is redirected into it.
WORKTREE_ROLE = "worktree"
WORKTREE_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "worktree.md")

# Max reviewer<->role review rounds per `ready_for_review` (D5). After this many
# reviewer passes without approval, cowork falls through to the user review gate
# with the reviewer's last dissent attached. Never hard-blocks. Shared by the
# scout-reviewer and the planning-advisor.
REVIEW_ROUND_CAP = 5

# Max CONSECUTIVE reviewer turns with no usable verdict (account limit, crash,
# empty/garbled write) before cowork surfaces the visible reviewer-failure gate
# (retry / skip-review / end). Mirrors the user-facing stuck gate's "2 failing
# tries" — one silent auto-retry of the reviewer, then the gate. Distinct from
# REVIEW_ROUND_CAP, which bounds a reviewer that legitimately keeps requesting
# changes. Shared by all three paired reviewers.
REVIEW_FAIL_CAP = 2

# Runtime headless notes prepended to a role's/reviewer's seed when --headless is
# set, so the role itself KNOWS this session is headless on its very first turn
# (the static role-prompt directives are only "meaningful under --headless" — this
# is the runtime activation, the primary prompt layer behind the orchestrator
# safety-net). Leads must not block; reviewers must not pose user questions.
HEADLESS_LEAD_NOTE = (
    "[headless mode] This session is running headless — there is NO human "
    "available to answer questions. Do not set your status to needs_input and "
    "do not wait for input: choose the most reasonable interpretation of any "
    "open question, record it in result.assumptions, and drive to "
    "ready_for_review.")
HEADLESS_REVIEWER_NOTE = (
    "[headless mode] This session is running headless — there is NO human "
    "available. Do not emit a needs_user verdict and do not pose a product or "
    "review question to the user: review with the context you have, and express "
    "any concern you would otherwise raise as a user question as a revise "
    "finding (or approve).")

# Max headless needs_input nudges per lead role before the phase ends cleanly.
# The primary bound on a headless needs_input loop is the existing stale-no-op /
# stuck handling (a byte-identical re-write ends the phase). This cap is the
# backstop for the pathological case of a role that keeps writing a DIFFERENT
# needs_input each turn (which the byte-level detector would not catch): after
# this many nudges with no ready_for_review, the phase ends cleanly so a
# headless run can never hang. Mirrors REVIEW_ROUND_CAP's "never block" intent.
HEADLESS_NUDGE_CAP = 5

# Role order matches the user's vision and the phase order: context-gather
# (scouting), planning, building. Each user-facing role is followed by its
# paired critical reviewer. All three phases — `scout`/`scout-reviewer`,
# `planner`/`planning-advisor`, `builder`/`build-reviewer` — are implemented.
#
# `scout-reviewer`, `planning-advisor`, and `build-reviewer` are critical
# reviewers paired with their user-facing role DURING that role's session
# (deterministically invoked when the role sets `ready_for_review`). The
# build-reviewer occupies the paired-reviewer slot the `revisor` name once
# reserved; `revisor` is dropped (a future sequential plan-revisor would get a
# new name).
SCOUT_REVIEWER = handoff.ROLE_REGISTRY["scout"]["reviewer"]
PLANNING_ADVISOR = handoff.ROLE_REGISTRY["planner"]["reviewer"]
BUILD_REVIEWER = handoff.ROLE_REGISTRY["builder"]["reviewer"]
ROLES = handoff.selectable_roles()
handoff.validate_role_topology()

# The contributions an EXTERNAL orchestrator/driver may target with a
# `--evaluate-role` evaluation, and the named `orchestration` phase scopes.
# Canonical definitions live in cowork_state (the leaf module the schema
# validation also uses), re-exported here so the CLI, the persisted-history
# validation, and the tests read ONE authoritative list rather than drifting.
VALID_EVAL_ROLES = state_store.ORCHESTRATOR_EVAL_ROLES
VALID_ORCHESTRATION_PHASES = state_store.ORCHESTRATOR_EVAL_PHASES

# Hand-back contract: a user-facing role may set `status: "handoff_back"` (plus
# a `handoff` payload) in its status file to hand the work back to its
# pre-processor through a user-confirmed gate. The contract is role-generic:
# planner -> scout and builder -> planner are wired.
HANDBACK_PREPROCESSOR = {"planner": "scout", "builder": "planner"}

# Per-role defaults (controller, model, effort, yolo, mode), all roles checked
# by default. Roles default to implement mode (write-enabled) and are kept in
# their lane by role-spec guardrails, not by plan mode. `model`/`effort` default
# to None = whatever the controller CLI itself defaults to; opencode models are
# `provider/model` (the provider choice is embedded in the model id).
DEFAULTS = {
    "scout": {"controller": "claude", "model": None, "effort": None,
              "yolo": True, "mode": "implement"},
    SCOUT_REVIEWER: {"controller": "claude", "model": None, "effort": None,
                     "yolo": True, "mode": "implement"},
    "planner": {"controller": "claude", "model": None, "effort": None,
                "yolo": True, "mode": "implement"},
    PLANNING_ADVISOR: {"controller": "claude", "model": None, "effort": None,
                       "yolo": True, "mode": "implement"},
    "builder": {"controller": "claude", "model": None, "effort": None,
                "yolo": True, "mode": "implement"},
    BUILD_REVIEWER: {"controller": "claude", "model": None, "effort": None,
                     "yolo": True, "mode": "implement"},
}

# Canonical definition lives in cowork_policy (the leaf module cowork_bridge and
# cowork_state also import), re-exported here so the existing name and value are
# untouched for every in-tree caller and test.
CONTROLLERS = policy.CONTROLLERS
ROLE_PROMPT_PATHS = {
    "scout": SCOUT_PROMPT_PATH,
    SCOUT_REVIEWER: SCOUT_REVIEWER_PROMPT_PATH,
    "planner": PLANNER_PROMPT_PATH,
    PLANNING_ADVISOR: PLANNING_ADVISOR_PROMPT_PATH,
    "builder": BUILDER_PROMPT_PATH,
    BUILD_REVIEWER: BUILD_REVIEWER_PROMPT_PATH,
}
PHASE_LEADS = {"scouting": "scout", "planning": "planner",
               "building": "builder"}
PHASE_PAIRS = {"scouting": ("scout", SCOUT_REVIEWER),
               "planning": ("planner", PLANNING_ADVISOR),
               "building": ("builder", BUILD_REVIEWER)}


# --------------------------------------------------------------------------- #
# Menu seam (questionary): the interactive menus take injectable ask-callables  #
# so they are unit-testable without a TTY or a real questionary prompt. The     #
# defaults below are the only place questionary is imported.                    #
# --------------------------------------------------------------------------- #


# All three are wrapped in the shared user-wait seam (P15). They are the team,
# controller and session menus plus the approval and recovery choices — four of
# the six prompts that actually block on a human. Instrumenting only the two in
# cowork_ui would have reported an INCOMPLETE user-wait figure as though it were
# complete, which is a quieter and worse failure than reporting `unknown`.
# With no active trace (every unit test) these emit nothing.


def _q_checkbox(message, options, checked=None):
    """questionary multi-select. Returns the picked list (or None on Ctrl-C)."""
    import questionary
    from questionary import Choice
    checked = set(checked or [])
    with trace_store.user_wait("menu.checkbox") as span:
        picked = questionary.checkbox(
            message, choices=[Choice(o, checked=(o in checked))
                              for o in options]
        ).ask()
        if picked is None:
            span.outcome = "cancelled"
        return picked


def _q_select(options, default=None, message=""):
    """questionary single-select. Returns the picked item, falling back to
    `default` on cancel so callers never get None."""
    import questionary
    with trace_store.user_wait("menu.select") as span:
        picked = questionary.select(
            message or "", choices=list(options), default=default).ask()
        if picked is None:
            span.outcome = "cancelled"
        return picked if picked is not None else default


def _q_text(message, default=""):
    """questionary free-text input. Returns `default` on cancel."""
    import questionary
    with trace_store.user_wait("menu.text") as span:
        val = questionary.text(message, default=default).ask()
        if val is None:
            span.outcome = "cancelled"
        return default if val is None else val


# --------------------------------------------------------------------------- #
# Step 1: team checklist (interactive).                                       #
# --------------------------------------------------------------------------- #


def select_team_interactive(checkbox_fn=None):
    """Checkbox menu, all roles preselected. Returns ordered roles ([] on cancel)."""
    checkbox_fn = checkbox_fn or _q_checkbox
    picks = checkbox_fn("Choose your team (space toggles, enter confirms)",
                        ROLES, checked=ROLES)
    if not picks:  # None (cancelled) or empty selection
        return []
    return [r for r in ROLES if r in picks]


# --------------------------------------------------------------------------- #
# Step 2: per-role tool config.                                               #
# --------------------------------------------------------------------------- #


def default_config(selected):
    return {role: dict(DEFAULTS[role]) for role in selected}


def normalize_role_config(cfg):
    """Fill schema keys missing from older saved sessions (model/effort were
    added later); never mutates the input."""
    out = dict(cfg)
    out.setdefault("model", None)
    out.setdefault("effort", None)
    return out


def apply_config_override(config, role, tokens):
    """Apply tokens to one role. Returns (ok, error_or_None). Mutates config.

    Plain tokens: a controller name (claude/codex/opencode), yolo/no-yolo,
    plan/implement. Key=value tokens: model=<id> and effort=<level>
    (model=default / effort=default reset to the controller CLI's default).
    opencode models are provider/model, e.g. model=anthropic/claude-sonnet-4-5."""
    if role not in config:
        return False, "unknown or unselected role: %r" % role
    cfg = config[role]
    for token in tokens:
        if token in CONTROLLERS:
            cfg["controller"] = token
        elif token == "yolo":
            cfg["yolo"] = True
        elif token == "no-yolo":
            cfg["yolo"] = False
        elif token in ("plan", "implement"):
            cfg["mode"] = token
        elif "=" in token:
            key, _, value = token.partition("=")
            key, value = key.strip(), value.strip()
            if key not in ("model", "effort"):
                return False, "unknown option: %r" % token
            cfg[key] = None if value in ("", "default") else value
        else:
            return False, "unknown option: %r" % token
    return True, None


def format_config_summary(config, header="Tool config:"):
    """Aligned per-role summary with a column header row."""
    labels = ("role", "controller", "model", "effort", "permissions", "mode")
    rows = [
        (role, config[role]["controller"],
         config[role].get("model") or "default",
         config[role].get("effort") or "default",
         "yolo" if config[role]["yolo"] else "no-yolo", config[role]["mode"])
        for role in ROLES if role in config
    ]
    if not rows:
        return header
    cols = list(zip(labels, *rows))
    widths = [max(len(str(v)) for v in col) for col in cols]

    def fmt(cells):
        return "  " + "   ".join(
            str(cell).ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [header, fmt(labels), fmt("-" * w for w in widths)]
    for row in rows:
        lines.append(fmt(row))
    return "\n".join(lines)


# Menu sentinels shared by the config screens.
START_CHOICE = "✓ start with this config"
DEFAULT_CHOICE = "default (the CLI's own setting)"
CUSTOM_CHOICE = "custom…"
BACK_CHOICE = "← back: change team"
# Returned by configure_roles_interactive when the user picks BACK_CHOICE.
BACK = object()

# Curated model presets per controller — the silent FALLBACK when live
# discovery fails. Live sources: claude from the public models.dev catalog
# (keyless), codex from `codex debug models`, opencode from `opencode models`
# (only providers with credentials appear there).
MODEL_PRESETS = {
    "claude": ["opus", "sonnet", "haiku"],
    "codex": [],
    "opencode": [],
}

# Thinking-effort levels per controller (claude --effort, codex
# model_reasoning_effort, opencode --variant). opencode variants are
# provider-specific; the per-provider map below refines the generic list once
# a model is chosen.
EFFORT_CHOICES = {
    "claude": ["low", "medium", "high", "xhigh", "max"],
    "codex": ["minimal", "low", "medium", "high", "xhigh"],
    "opencode": ["minimal", "low", "medium", "high", "max"],
}
OPENCODE_EFFORTS_BY_PROVIDER = {
    "anthropic": ["high", "max"],
    "openai": ["none", "minimal", "low", "medium", "high", "xhigh"],
    "google": ["low", "high"],
}

# One access pick sets both yolo and mode: plan+yolo is read-only anyway, so
# the 2x2 grid collapses to the three combos that actually differ.
ACCESS_CHOICES = (
    ("yolo (full access, no approvals)", True, "implement"),
    ("safe (edits only, other commands denied)", False, "implement"),
    ("read-only (plan mode)", True, "plan"),
)


def access_label(cfg):
    if cfg.get("mode") == "plan":
        return ACCESS_CHOICES[2][0]
    return ACCESS_CHOICES[0][0] if cfg.get("yolo") else ACCESS_CHOICES[1][0]


def _run_opencode_models():
    """Raw `opencode models` stdout ('' on any failure)."""
    try:
        res = subprocess.run(["opencode", "models"], capture_output=True,
                             text=True, timeout=20)
    except Exception:  # noqa: BLE001 - a model list is never load-bearing
        return ""
    return res.stdout if res.returncode == 0 else ""


def list_opencode_models(runner=None):
    """Parse `opencode models` (one provider/model per line, credentialed
    providers only) into {provider: [full 'provider/model' ids]}. Empty dict
    when opencode is missing or lists nothing — the picker falls back to free
    text."""
    out = (runner or _run_opencode_models)()
    models = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or "/" not in line or " " in line:
            continue
        provider = line.split("/", 1)[0]
        models.setdefault(provider, []).append(line)
    return models


def _run_codex_models():
    """Raw `codex debug models` stdout ('' on any failure)."""
    try:
        res = subprocess.run(["codex", "debug", "models"], capture_output=True,
                             text=True, timeout=10)
    except Exception:  # noqa: BLE001 - a model list is never load-bearing
        return ""
    return res.stdout if res.returncode == 0 else ""


def list_codex_models(runner=None):
    """Parse the `codex debug models` JSON catalog into an ordered list of
    {'slug', 'efforts'} dicts: visibility=='list' models only, ascending
    priority (the vendor's flagship/newest-first display order). Empty list on
    any failure — the picker falls back to MODEL_PRESETS."""
    try:
        data = json.loads((runner or _run_codex_models)() or "")
        raw = data["models"]
    except Exception:  # noqa: BLE001 - a model list is never load-bearing
        return []
    models = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        # Reasoning levels arrive as {'effort': ..., 'description': ...} dicts
        # today; tolerate plain strings too.
        efforts = []
        for level in entry.get("supported_reasoning_levels") or []:
            if isinstance(level, dict):
                level = level.get("effort")
            if isinstance(level, str) and level:
                efforts.append(level)
        priority = entry.get("priority")
        if not isinstance(priority, (int, float)):
            priority = float("inf")
        models.append((priority, {"slug": slug, "efforts": efforts}))
    models.sort(key=lambda pair: pair[0])
    return [model for _, model in models]


MODELS_DEV_URL = "https://models.dev/api.json"


def _fetch_models_dev():
    """Raw models.dev catalog JSON text ('' on any failure). models.dev
    returns HTTP 403 to a bare urllib request, so the User-Agent header is
    mandatory — without it discovery would silently fall back forever."""
    req = urllib.request.Request(MODELS_DEV_URL,
                                 headers={"User-Agent": "cowork/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - a model list is never load-bearing
        return ""


def list_claude_models(fetcher=None):
    """Full claude model ids from the public models.dev catalog (keyless),
    sorted newest-first by release_date. Empty list on any failure — the
    picker falls back to the MODEL_PRESETS aliases."""
    try:
        data = json.loads((fetcher or _fetch_models_dev)() or "")
        raw = data["anthropic"]["models"]
    except Exception:  # noqa: BLE001 - a model list is never load-bearing
        return []
    models = []
    for model_id, info in raw.items() if isinstance(raw, dict) else []:
        if not isinstance(model_id, str) or not model_id:
            continue
        released = info.get("release_date") if isinstance(info, dict) else None
        models.append((released if isinstance(released, str) else "", model_id))
    models.sort(reverse=True)
    return [model_id for _, model_id in models]


def preload_model_catalogs(opencode_models_fn=None, claude_models_fn=None,
                           codex_models_fn=None):
    """Fetch all live model catalogs concurrently, once per config-menu open,
    so the pickers themselves never do I/O. Each discovery fn is bounded by
    its own timeout and failure-silent, so the join is bounded too; a failed
    source just leaves its controller on the preset fallback."""
    fns = {
        "opencode": opencode_models_fn or list_opencode_models,
        "claude": claude_models_fn or list_claude_models,
        "codex": codex_models_fn or list_codex_models,
    }
    results = {"opencode": {}, "claude": [], "codex": []}

    def fetch(key):
        try:
            results[key] = fns[key]() or results[key]
        except Exception:  # noqa: BLE001 - a model list is never load-bearing
            pass

    threads = [threading.Thread(target=fetch, args=(key,), daemon=True)
               for key in fns]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def pick_model_interactive(role, controller, current, select_fn, text_fn,
                           opencode_models_fn=None, claude_models=None,
                           codex_models=None):
    """One model pick for a role. Returns the model id or None (= default).

    opencode is a two-step pick — provider first (satisfying the provider
    choice), then that provider's models — discovered live; claude and codex
    offer their preloaded live catalogs (claude_models newest-first, codex
    slugs flagship-first) and drop to the curated presets when discovery
    failed. Every path has a custom… escape hatch to a free-text id."""
    if controller == "opencode":
        by_provider = (opencode_models_fn or list_opencode_models)()
        if by_provider:
            providers = sorted(by_provider)
            cur_provider = (current or "").split("/", 1)[0]
            prov = select_fn(
                [DEFAULT_CHOICE] + providers + [CUSTOM_CHOICE],
                default=cur_provider if cur_provider in providers
                else DEFAULT_CHOICE,
                message="%s provider (opencode)" % role)
            if prov is None or prov == DEFAULT_CHOICE:
                return None
            if prov != CUSTOM_CHOICE:
                options = by_provider[prov] + [CUSTOM_CHOICE]
                pick = select_fn(
                    options,
                    default=current if current in by_provider[prov] else None,
                    message="%s model (%s)" % (role, prov))
                if pick and pick != CUSTOM_CHOICE:
                    return pick
        val = text_fn("%s model (provider/model, empty = default)" % role,
                      default=current or "")
        return val.strip() or None
    if controller == "claude" and claude_models:
        presets = list(claude_models)
    elif controller == "codex" and codex_models:
        presets = [model["slug"] for model in codex_models]
    else:
        presets = MODEL_PRESETS.get(controller) or []
    options = [DEFAULT_CHOICE] + presets + [CUSTOM_CHOICE]
    pick = select_fn(options,
                     default=current if current in presets else DEFAULT_CHOICE,
                     message="%s model (%s)" % (role, controller))
    if pick is None or pick == DEFAULT_CHOICE:
        return None
    if pick == CUSTOM_CHOICE:
        val = text_fn("%s model id (empty = default)" % role,
                      default=current or "")
        return val.strip() or None
    return pick


def pick_effort_interactive(role, controller, current, select_fn, model=None,
                            codex_models=None):
    """One thinking-effort pick. Returns the level or None (= default).
    For codex, a model found in the preloaded catalog narrows the levels to
    its supported_reasoning_levels; otherwise the generic list stands."""
    levels = EFFORT_CHOICES.get(controller) or []
    if controller == "opencode" and model and "/" in model:
        levels = OPENCODE_EFFORTS_BY_PROVIDER.get(
            model.split("/", 1)[0], levels)
    if controller == "codex" and model:
        for entry in codex_models or []:
            if entry.get("slug") == model and entry.get("efforts"):
                levels = entry["efforts"]
                break
    options = [DEFAULT_CHOICE] + levels
    pick = select_fn(options,
                     default=current if current in levels else DEFAULT_CHOICE,
                     message="%s thinking effort (%s)" % (role, controller))
    if pick is None or pick == DEFAULT_CHOICE:
        return None
    return pick


def configure_role_interactive(role, cfg, select_fn, text_fn,
                               opencode_models_fn=None, claude_models=None,
                               codex_models=None):
    """Edit one role in place: controller -> model -> effort -> access."""
    controller = select_fn(list(CONTROLLERS), default=cfg["controller"],
                           message=role + " controller")
    if controller and controller != cfg["controller"]:
        # Model ids/effort levels are controller-specific; never carry over.
        cfg["model"] = None
        cfg["effort"] = None
        cfg["controller"] = controller
    cfg["model"] = pick_model_interactive(
        role, cfg["controller"], cfg.get("model"), select_fn, text_fn,
        opencode_models_fn=opencode_models_fn, claude_models=claude_models,
        codex_models=codex_models)
    cfg["effort"] = pick_effort_interactive(
        role, cfg["controller"], cfg.get("effort"), select_fn,
        model=cfg.get("model"), codex_models=codex_models)
    labels = [label for label, _y, _m in ACCESS_CHOICES]
    pick = select_fn(labels, default=access_label(cfg),
                     message=role + " access")
    for label, yolo, mode in ACCESS_CHOICES:
        if pick == label:
            cfg["yolo"], cfg["mode"] = yolo, mode


def configure_roles_interactive(selected, select_fn=None, text_fn=None,
                                opencode_models_fn=None, claude_models_fn=None,
                                codex_models_fn=None, config=None,
                                catalogs=None, allow_back=False):
    """Step 2: one screen. The current config is shown as a table and the menu
    is 'start' (default — one Enter accepts everything) plus one entry per
    role; picking a role walks a short controller -> model -> effort -> access
    edit and returns to the same screen. No nested defaults-gate, no
    role-checkbox re-pick. Live model catalogs are preloaded once here and
    reused across every role edit — the pickers never fetch.

    With `allow_back` a '← back: change team' entry is appended and picking it
    returns the BACK sentinel (the merged team screen loops to the checkbox).
    `config`/`catalogs` let that caller keep role edits and preloaded catalogs
    alive across back-and-forth trips."""
    select_fn = select_fn or _q_select
    text_fn = text_fn or _q_text
    if config is None:
        config = default_config(selected)
    if catalogs is None:
        catalogs = preload_model_catalogs(
            opencode_models_fn=opencode_models_fn,
            claude_models_fn=claude_models_fn,
            codex_models_fn=codex_models_fn)
    options = ([START_CHOICE] + list(selected)
               + ([BACK_CHOICE] if allow_back else []))
    while True:
        summary = format_config_summary(
            config, header="Team config (pick a role to edit it):")
        choice = select_fn(options, default=START_CHOICE, message=summary)
        if allow_back and choice == BACK_CHOICE:
            return BACK
        if choice is None or choice == START_CHOICE:
            return config
        if choice in config:
            configure_role_interactive(
                choice, config[choice], select_fn, text_fn,
                opencode_models_fn=lambda: catalogs["opencode"],
                claude_models=catalogs["claude"],
                codex_models=catalogs["codex"])


def select_and_configure_interactive(checkbox_fn=None, select_fn=None,
                                     text_fn=None, opencode_models_fn=None,
                                     claude_models_fn=None,
                                     codex_models_fn=None):
    """Steps 1+2 as one navigable flow. Returns (selected, config).

    Team checkbox -> config screen; the config screen's '← back: change team'
    entry reopens the checkbox with the current picks checked. Role edits and
    the preloaded model catalogs survive the round trip (a role dropped and
    re-added does reset to its defaults). Cancelling the checkbox returns
    ([], {}) — same 'nothing to do' contract as select_team_interactive."""
    checkbox_fn = checkbox_fn or _q_checkbox
    selected = list(ROLES)
    config = {}
    catalogs = None
    while True:
        picks = checkbox_fn("Choose your team (space toggles, enter confirms)",
                            ROLES, checked=selected)
        if not picks:  # None (cancelled) or empty selection
            return [], {}
        selected = [r for r in ROLES if r in picks]
        config = {r: config[r] if r in config else dict(DEFAULTS[r])
                  for r in selected}
        if catalogs is None:  # preload once; back trips reuse it
            catalogs = preload_model_catalogs(
                opencode_models_fn=opencode_models_fn,
                claude_models_fn=claude_models_fn,
                codex_models_fn=codex_models_fn)
        result = configure_roles_interactive(
            selected, select_fn, text_fn, config=config, catalogs=catalogs,
            allow_back=True)
        if result is not BACK:
            return selected, result


# --------------------------------------------------------------------------- #
# Step 3: initial context.                                                    #
# --------------------------------------------------------------------------- #


def gather_context_interactive(prompt_fn=None):
    """One multiline editor for the initial context. EOF/cancel => no context."""
    prompt_fn = prompt_fn or (lambda: ui.prompt_user(
        sys.stdin, sys.stdout,
        header="What do you want to build or change? Describe the goal — "
               "paste any files, code, or context that matter."))
    val = prompt_fn()
    if val is ui.EOF or val is ui.CANCEL:
        return ""
    return val


def resolve_context(args, resuming=False):
    """Context from --context, --context-file (or '-' for stdin), or the editor.

    When `resuming` a saved session, skip the interactive goal prompt and return
    "" — run_scout turns that into "Continue the session." so the resumed scout
    picks up where it left off automatically. An explicit --context/--context-file
    still wins (lets you redirect a resumed session)."""
    if args.context is not None:
        return args.context
    if args.context_file is not None:
        if args.context_file == "-":
            return sys.stdin.read()
        with open(args.context_file, "r") as fh:
            return fh.read()
    if _is_non_interactive(args):
        return ""
    if resuming:
        return ""  # auto-continue; no goal prompt on resume
    return gather_context_interactive()


# --------------------------------------------------------------------------- #
# Argument parsing / non-interactive path.                                    #
# --------------------------------------------------------------------------- #


def parse_switch_controller(value):
    """Parse --switch-controller ROLE=CONTROLLER."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "--switch-controller must be ROLE=CONTROLLER")
    role, controller = [p.strip() for p in value.split("=", 1)]
    if role not in ROLES:
        raise argparse.ArgumentTypeError(
            "unknown role %r for --switch-controller" % role)
    if controller not in CONTROLLERS:
        raise argparse.ArgumentTypeError(
            "unknown controller %r for --switch-controller "
            "(expected one of: %s)" % (controller, ", ".join(CONTROLLERS)))
    return role, controller


def parse_allow_controllers(value):
    """argparse type for `--allow-controllers`: the shared policy parser, with
    its ValueError re-raised as an ArgumentTypeError so argparse reports the
    helpful message (naming the valid controllers) rather than a generic one."""
    try:
        return policy.parse_allowed(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def build_parser():
    p = argparse.ArgumentParser(prog="cowork", add_help=True)
    p.add_argument("--check", action="store_true",
                   help="run the preflight dependency check only")
    p.add_argument("--report", nargs="?", const=True, metavar="SESSION_UUID",
                   help="print a plain-text token/byte report for a cowork "
                        "session (defaults to this directory's most recent "
                        "session) and exit")
    p.add_argument("--json", dest="report_json", action="store_true",
                   help="with --report: print the authoritative measurement "
                        "record instead of its rendered text form")
    p.add_argument("--rebuild", action="store_true",
                   help="with --report: rebuild the measurement record from "
                        "the raw sources before printing (by default a report "
                        "loads the existing record and never rebuilds it)")
    p.add_argument("--evaluation-policy", dest="evaluation_policy",
                   choices=list(state_store.EVALUATION_POLICIES),
                   help="how much of the run gets scored: all_rounds "
                        "(default), final_round, sampled, or off. The overhead "
                        "of the choice is reported separately.")
    p.add_argument("--team",
                   help="comma-separated roles, e.g. scout,planner "
                        "(non-interactive)")
    p.add_argument("--config", action="append", default=[],
                   metavar="ROLE=opt,opt",
                   help="per-role override, e.g. scout=codex,no-yolo,implement "
                        "or builder=opencode,model=anthropic/claude-sonnet-4-5,"
                        "effort=high (options: claude/codex/opencode, "
                        "model=<id>, effort=<level>, yolo/no-yolo, "
                        "plan/implement; repeatable)")
    p.add_argument("--context", help="initial context text (non-interactive)")
    p.add_argument("--context-file",
                   help="read initial context from a file, or '-' for stdin")
    p.add_argument("--session-file",
                   help="path to the session store (default: ./.cowork/session.json)")
    p.add_argument("--no-session", action="store_true",
                   help="do not read or write the session store")
    p.add_argument("--new", action="store_true",
                   help="start a fresh session, skipping the resume-or-new "
                        "prompt (prior sessions stay intact)")
    p.add_argument("--resume", action="store_true",
                   help="open the session picker for this directory (newest "
                        "first); needs an interactive terminal")
    p.add_argument("--switch-controller", type=parse_switch_controller,
                   action="append", default=[],
                   metavar="ROLE=CONTROLLER",
                   help="switch one current-phase role in an existing saved "
                        "session to %s, then continue (repeatable: every "
                        "switch in one invocation is applied as a single "
                        "all-or-nothing update)"
                        % (", ".join(CONTROLLERS[:-1]) + " or " + CONTROLLERS[-1]))
    p.add_argument("--allow-controllers", dest="allow_controllers",
                   type=parse_allow_controllers, default=None, metavar="LIST",
                   help="restrict this saved session to the given controllers "
                        "(e.g. claude,codex), or 'all' to remove an existing "
                        "restriction. Combines with --switch-controller; the "
                        "policy change and every role move are validated and "
                        "persisted together before anything resumes.")
    p.add_argument("--worktree", "--wt", dest="worktree", nargs="?",
                   const=True, metavar="NAME",
                   help="before scouting, spin up a small agent that creates a "
                        "git worktree (following the repo's convention) and run "
                        "the rest of the session inside it. Optional NAME names "
                        "the worktree/branch (default: cowork-<short session "
                        "id>). Requires launching inside a git work tree.")
    p.add_argument("--wt-controller", dest="wt_controller",
                   choices=list(CONTROLLERS), default="claude",
                   help="controller for the worktree role (default: claude)")
    p.add_argument("--headless", "--auto", dest="headless",
                   action="store_true",
                   help="drive the whole scout->plan->build flow with no human "
                        "gates: roles never block, reviewers work with what they "
                        "have, rounds end on reviewer consensus or the review "
                        "round cap. Requires --context/--context-file.")
    # Targeted orchestrator-owned evaluations. `--evaluate-role` is the dispatch
    # flag (handled in main() before run_flow, like --check/--report). The
    # session is named by --eval-session (NOT --session: that would make --sess
    # ambiguous against --session-file under argparse's allow_abbrev). Artifact
    # provenance is derived from the historical trace fingerprint, so there is
    # deliberately NO --artifact-digest flag (D-eval-12).
    p.add_argument("--evaluate-role", dest="evaluate_role",
                   choices=list(VALID_EVAL_ROLES), metavar="ROLE",
                   help="record ONE targeted, orchestrator-owned evaluation of "
                        "a single role contribution in an existing session, "
                        "then exit. Written to orchestrator-evaluations.json — "
                        "SEPARATE from peer scores.json and never read by any "
                        "phase gate. ROLE is one of: %s."
                        % ", ".join(VALID_EVAL_ROLES))
    p.add_argument("--eval-session", dest="eval_session", metavar="SESSION_UUID",
                   help="with --evaluate-role: the session UUID whose "
                        "contribution is being evaluated")
    p.add_argument("--work-id", dest="work_id", metavar="WORK_ID",
                   help="with --evaluate-role: the trace work_id identifying "
                        "the exact team-role contribution (found in "
                        "trace.jsonl controller.turn.start events). Required "
                        "for team roles; not used for orchestration.")
    p.add_argument("--phase", dest="eval_phase", metavar="PHASE",
                   help="with --evaluate-role: for orchestration, the scope "
                        "(one of: %s); optional annotation for team roles"
                        % ", ".join(VALID_ORCHESTRATION_PHASES))
    p.add_argument("--round", dest="eval_round", type=int, metavar="N",
                   help="with --evaluate-role: optional review-round annotation")
    p.add_argument("--output-quality", dest="output_quality", type=int,
                   metavar="1-5",
                   help="with --evaluate-role: output-quality score (1-5, "
                        "higher is better)")
    p.add_argument("--intent-alignment", dest="intent_alignment", type=int,
                   metavar="1-5",
                   help="with --evaluate-role: intent-alignment score (1-5, "
                        "higher is better)")
    p.add_argument("--evidence-quality", dest="evidence_quality", type=int,
                   metavar="1-5",
                   help="with --evaluate-role: evidence/reasoning-quality score "
                        "(1-5, higher is better)")
    p.add_argument("--self-sufficiency", dest="self_sufficiency", type=int,
                   metavar="1-5",
                   help="with --evaluate-role: self-sufficiency score (1-5, "
                        "higher is better — the reverse framing of "
                        "intervention/rework required, so high is always good)")
    p.add_argument("--cost-worthiness", dest="cost_worthiness", type=int,
                   metavar="1-5",
                   help="with --evaluate-role: cost/latency-worthiness score "
                        "(1-5, higher is better)")
    p.add_argument("--notes", dest="eval_notes", metavar="TEXT",
                   help="with --evaluate-role: optional free-form note")
    return p


def _is_non_interactive(args):
    return bool(args.team or args.config or args.context is not None
                or args.context_file or getattr(args, "headless", False))


def run_report(args, io_out=None):
    """Handle `cowork --report [<session-uuid>]` — THREE ORDERED STEPS with no
    coupling between them (P2).

    (a) LOAD `measurement.json`. It is built only when none exists (and the
        report says so) or when `--rebuild` is passed. NEVER implicitly: a
        report that rebuilt every time could not be distinguished from one that
        recomputed its figures, which is the failure D3 exists to prevent.
    (b) CHECK PROVENANCE and print its banner. It hashes the raw sources to
        decide whether to warn, and produces no measurement figure — which is
        what keeps "the report computes nothing" literally true. Its result is
        never passed into the renderer.
    (c) RENDER the record, with the record as the renderer's only argument.

    A stale record still renders the RECORD's values under the banner. Reporting
    stale-but-authoritative numbers with a warning is honest; silently
    recomputing them is not.
    """
    io_out = io_out or sys.stdout
    session_uuid = args.report if isinstance(args.report, str) else None
    if not session_uuid:
        sessions = state_store.list_sessions()
        if not sessions:
            io_out.write("cowork: no sessions found for this directory.\n")
            return 1
        session_uuid = sessions[0]["id"]
    trace_path = trace_store.trace_path_for(session_uuid)
    record_path = state_store.measurement_path_for(session_uuid)
    if not os.path.exists(trace_path) and not os.path.exists(record_path):
        io_out.write(
            "cowork: no trace or measurement record found for session %s "
            "(looked at %s).\n" % (session_uuid, trace_path))
        return 1

    rebuild = bool(getattr(args, "rebuild", False))
    # A session whose assets are TRACKED FILES is read-only to the report: the
    # checked-in measurement fixtures are source truth, and persisting a record
    # or reconciling a ledger into them made verification mutate the very tree
    # it was verifying. Reporting is a read; nothing about it requires a write.
    persist = not _session_assets_are_tracked(session_uuid)
    record = None if rebuild else measure.load_record(session_uuid)
    if record is None:
        # Reconcile ingested observations into the ledger BEFORE the first
        # build, so a session reported without ever having run under this
        # orchestrator (a fixture, an archived session) still has identified
        # verification attempts rather than an empty list.
        try:
            identities = state_store.read_role_identities(
                state_store.identities_path_for(session_uuid))
            bundled = os.path.join(
                state_store.session_assets_dir(session_uuid),
                "controller_logs")
            claude_root = os.path.join(bundled, "claude")
            codex_root = os.path.join(bundled, "codex")
            results = ingest.ingest_session(
                identities, cwd=os.getcwd(),
                claude_root=claude_root if os.path.isdir(claude_root) else None,
                codex_root=codex_root if os.path.isdir(codex_root) else None)
            if persist:
                ledger.reconcile_attempts(
                    state_store.ledger_path_for(session_uuid),
                    ingest.observations_for(results))
        except Exception:  # noqa: BLE001 - reporting never breaks on this
            pass
        reason = "rebuilding on request" if rebuild else (
            "no measurement record yet — building one now")
        if not getattr(args, "report_json", False):
            io_out.write("cowork: %s.\n\n" % reason)
        record = (measure.build_and_write(session_uuid) if persist
                  else measure.build_record(session_uuid, cwd=os.getcwd()))

    if getattr(args, "report_json", False):
        # The authoritative artifact itself. D3 makes the record the authority;
        # a user with no way to read it would be told it exists and shown only
        # the derivation.
        json.dump(record, io_out, indent=2, sort_keys=True, default=str)
        io_out.write("\n")
        io_out.flush()
        return 0

    provenance = measure.check_provenance(session_uuid, record)
    banner = cowork_report.render_provenance_banner(provenance)
    if banner:
        io_out.write(banner)
    # The record is the renderer's ONLY argument. `provenance` deliberately does
    # not travel with it.
    io_out.write(cowork_report.render_report(record))

    io_out.flush()
    return 0


# --------------------------------------------------------------------------- #
# Targeted orchestrator-owned evaluations (`cowork --evaluate-role ...`).      #
#                                                                             #
# An external orchestrator records structured, per-contribution scores for a  #
# single Cowork role. Everything here is ADDITIVE and provably targeted: a    #
# team-role evaluation is written only after the (role, work_id) contribution #
# is CONFIRMED in historical trace/identity evidence, and the artifact digest #
# is derived from the historical trace fingerprint recorded at that exact     #
# turn — never re-hashed from the current on-disk file, which would bind an    #
# older work_id to a later revision when a role runs multiple turns.          #
# --------------------------------------------------------------------------- #


def _ts_seconds(value):
    """Parse a trace `ts` (ISO-8601, e.g. '2026-08-06T00:00:00Z') to a float
    epoch-seconds figure, or None when it cannot be parsed. Tolerant."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def _verify_work_id_exists(session_uuid, role, work_id):
    """Proof-of-contribution gate (D-eval-09): confirm that (role, work_id) is a
    REAL historical contribution before any evaluation is written.

    Two evidence sources are checked so the gate works even for a session whose
    identities.json was never written: (1) identities observations[] for a
    matching (role, work_id); (2) trace.jsonl controller.turn.start events with
    the matching (role, work_id). Returns True if EITHER confirms it. Fully
    tolerant: any read failure is treated as 'not found' (returns False),
    never raised."""
    if not (session_uuid and role and work_id):
        return False
    try:
        identities = state_store.read_role_identities(
            state_store.identities_path_for(session_uuid))
        for obs in (identities.get("observations") or []):
            if isinstance(obs, dict) and obs.get("role") == role \
                    and obs.get("work_id") == work_id:
                return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        events = state_store.read_jsonl_tolerant(
            trace_store.trace_path_for(session_uuid))
        for event in events:
            if event.get("event") == "controller.turn.start" \
                    and event.get("work_id") == work_id \
                    and event.get("role") == role:
                return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def _resolve_identity_from_trace(session_uuid, role, work_id):
    """AUTHORITATIVE identity resolution for a (role, work_id) contribution.

    The real identities.json observation carries role/tool/model/session_id but
    NO work_id, so it cannot pinpoint a specific turn. The trace can: every
    controller.turn.start/end event for the turn carries `work_id`, `role`, and
    the per-turn `identity` object (controller/model/effort/controller_session_id
    — see cowork_trace.identity_meta), so a contribution made before a
    mid-session controller switch is stamped with the controller it ACTUALLY ran
    on, not the role's latest one.

    The turn's START and END both carry an identity, and they are NOT
    interchangeable: a fresh start can still name a config-pinned model
    (`model=sonnet`) that the live provider event later corrects on the end
    (`model=claude-sonnet-4-6`). The END identity is therefore preferred and
    merged FIELD BY FIELD, falling back to the START only for a field the end
    left absent — so the settled, live values win without discarding a field the
    end never observed.

    Returns `{'tool', 'model', 'session_id', 'effort'}` with None values
    stripped, or `{}` when no matching turn carries an identity. Tolerant."""
    if not (session_uuid and role and work_id):
        return {}
    try:
        events = state_store.read_jsonl_tolerant(
            trace_store.trace_path_for(session_uuid))
    except (OSError, ValueError, TypeError):
        return {}
    start_identity = None
    end_identity = None
    for event in events:
        if event.get("role") != role or event.get("work_id") != work_id:
            continue
        identity = event.get("identity")
        if not isinstance(identity, dict):
            continue
        name = event.get("event")
        if name == "controller.turn.end" and end_identity is None:
            end_identity = identity
        elif name == "controller.turn.start" and start_identity is None:
            start_identity = identity
    if end_identity is None and start_identity is None:
        return {}

    def _prefer(field):
        # End first (settled truth for the turn), start only as a gap-filler.
        for source in (end_identity, start_identity):
            if isinstance(source, dict):
                value = source.get(field)
                if value is not None:
                    return value
        return None

    resolved = {
        "tool": _prefer("controller"),
        "model": _prefer("model"),
        "session_id": _prefer("controller_session_id"),
        "effort": _prefer("effort"),
    }
    return {k: v for k, v in resolved.items() if v is not None}


def _lookup_artifact_fingerprint_from_trace(trace_path, role, work_id):
    """Derive the artifact digest for one contribution turn from the HISTORICAL
    trace fingerprint, never from the current on-disk file.

    Evidence chain, strictly positional AND bounded to the target turn: find the
    single controller.turn.end event whose (role, work_id) match; from that
    position scan forward for THIS turn's role.fingerprint.after event (emitted
    in-sequence right after the turn's send). The scan STOPS the instant it hits
    another controller.turn.start/end for the same role first — the fingerprint
    belongs to the next turn, and drifting into it would bind an old work_id to a
    later revision. Ambiguity (more than one matching controller.turn.end) is
    likewise rejected.

    A fingerprint is `observed` only when the artifact actually existed
    (`exists` is true), its `sha256` is a valid 64-hex digest, and its `size` is
    a sensible non-negative integer. An absent artifact, or a missing/invalid
    digest, is `unavailable` with a reason — never a fabricated `observed`.
    Fully tolerant."""
    try:
        events = state_store.read_jsonl_tolerant(trace_path)
    except (OSError, ValueError, TypeError):
        return {"state": "unavailable", "reason": "trace_unreadable"}
    if not events:
        return {"state": "unavailable", "reason": "trace_unreadable"}
    end_indices = [
        i for i, event in enumerate(events)
        if event.get("event") == "controller.turn.end"
        and event.get("work_id") == work_id and event.get("role") == role]
    if not end_indices:
        return {"state": "unavailable", "reason": "turn_not_found"}
    if len(end_indices) > 1:
        return {"state": "unavailable", "reason": "ambiguous"}
    for event in events[end_indices[0] + 1:]:
        name = event.get("event")
        # A later same-role turn boundary before this turn's fingerprint means
        # the fingerprint never landed — do NOT walk into the next turn's.
        if name in ("controller.turn.start", "controller.turn.end") \
                and event.get("role") == role:
            return {"state": "unavailable", "reason": "fingerprint_not_found"}
        if name == "role.fingerprint.after" and event.get("role") == role:
            if event.get("exists") is not True:
                return {"state": "unavailable", "reason": "artifact_absent"}
            sha256 = event.get("sha256")
            if not state_store.is_sha256_hex(sha256):
                return {"state": "unavailable", "reason": "invalid_digest"}
            size = event.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                return {"state": "unavailable", "reason": "invalid_size"}
            return {"state": "observed", "sha256": sha256, "size": size,
                    "artifact_status": event.get("status")}
    return {"state": "unavailable", "reason": "fingerprint_not_found"}


def _lookup_work_id_evidence(session_uuid, work_id, model=None, tool=None):
    """Non-fatal usage/duration/cost evidence for one contribution, joined from
    trace.jsonl by work_id (D-eval-07). Returns a dict of ONLY the fields that
    could be established — an absent field is omitted, never null, and never
    fatal. Returns `{}` when the trace is missing or has no matching turn."""
    out = {}
    try:
        events = state_store.read_jsonl_tolerant(
            trace_store.trace_path_for(session_uuid))
    except (OSError, ValueError, TypeError):
        return out
    start_event = None
    end_event = None
    for event in events:
        if event.get("work_id") != work_id:
            continue
        name = event.get("event")
        if name == "controller.turn.start" and start_event is None:
            start_event = event
        elif name == "controller.turn.end":
            end_event = event
    if end_event is not None:
        duration_ms = end_event.get("duration_ms")
        if isinstance(duration_ms, (int, float)) \
                and not isinstance(duration_ms, bool):
            out["duration_s"] = round(duration_ms / 1000.0, 3)
        elif start_event is not None:
            started = _ts_seconds(start_event.get("ts"))
            ended = _ts_seconds(end_event.get("ts"))
            if started is not None and ended is not None and ended >= started:
                out["duration_s"] = round(ended - started, 3)
        usage = end_event.get("usage")
        if isinstance(usage, dict) and usage:
            out["usage"] = usage
            if model:
                try:
                    import cowork_pricing as pricing
                    priced = pricing.price_usage(usage, model)
                    if isinstance(priced, dict) \
                            and priced.get("state") == "priced" \
                            and priced.get("cost") is not None:
                        out["cost_usd"] = priced.get("cost")
                except Exception:  # noqa: BLE001 - cost is best-effort only
                    pass
    return out


def run_orchestrator_eval(args, io_out=None):
    """Handle `cowork --evaluate-role ...` — record ONE targeted evaluation of a
    single role contribution, then exit. Never touches run_flow's machinery.

    Exit codes: 0 success; 1 write/malformed error; 2 validation error. Every
    validation error writes a specific message to stderr and writes NO file.
    """
    io_out = io_out or sys.stdout
    role = args.evaluate_role

    # (1) role — already constrained by argparse choices, but re-check so a
    # direct call (tests) still fails cleanly rather than writing a bad entry.
    if role not in VALID_EVAL_ROLES:
        sys.stderr.write(
            "cowork: --evaluate-role: unknown role %r (expected one of: %s)\n"
            % (role, ", ".join(VALID_EVAL_ROLES)))
        return 2

    # (2) five scores: each required, an int in [1, 5]. Naming the bad dimension.
    score_fields = (
        ("output_quality", args.output_quality),
        ("intent_alignment", args.intent_alignment),
        ("evidence_quality", args.evidence_quality),
        ("self_sufficiency", args.self_sufficiency),
        ("cost_worthiness", args.cost_worthiness),
    )
    for name, value in score_fields:
        if value is None:
            sys.stderr.write(
                "cowork: --evaluate-role: missing required score "
                "--%s (an integer 1-5)\n" % name.replace("_", "-"))
            return 2
        if not isinstance(value, int) or isinstance(value, bool) \
                or value < 1 or value > 5:
            sys.stderr.write(
                "cowork: --evaluate-role: --%s must be an integer 1-5 "
                "(got %r)\n" % (name.replace("_", "-"), value))
            return 2

    # (3) session must exist (its assets dir is the authoritative per-session
    # root; checking it is cheaper than loading a session file).
    session_uuid = args.eval_session
    if not session_uuid or not os.path.isdir(
            state_store.session_assets_dir(session_uuid)):
        sys.stderr.write(
            "cowork: --evaluate-role: session not found: %r\n" % session_uuid)
        return 2

    is_orchestration = role == "orchestration"

    if not is_orchestration:
        # (4) team roles require --work-id.
        if not args.work_id:
            sys.stderr.write(
                "cowork: --evaluate-role: role %r requires --work-id\n" % role)
            return 2
        # (4a) proof-of-contribution: the (role, work_id) must be real.
        if not _verify_work_id_exists(session_uuid, role, args.work_id):
            sys.stderr.write(
                "cowork: --evaluate-role: contribution not found for role %s "
                "work_id %s (no matching trace/identity evidence)\n"
                % (role, args.work_id))
            return 2
    else:
        # (5) orchestration requires --phase, validated against the enum.
        if not args.eval_phase:
            sys.stderr.write(
                "cowork: --evaluate-role: orchestration requires --phase "
                "(one of: %s)\n" % ", ".join(VALID_ORCHESTRATION_PHASES))
            return 2
        # (5a)
        if args.eval_phase not in VALID_ORCHESTRATION_PHASES:
            sys.stderr.write(
                "cowork: --evaluate-role: invalid orchestration --phase %r "
                "(expected one of: %s)\n"
                % (args.eval_phase, ", ".join(VALID_ORCHESTRATION_PHASES)))
            return 2

    # (6) identity — historically-correct for this exact turn, non-fatal. The
    # trace's per-turn identity object is AUTHORITATIVE (it is the only source
    # keyed by work_id); identities.json observations are a defensive fallback
    # for the rare case one carries a work_id, since the real observation schema
    # cannot correlate to a specific turn.
    identity = {}
    if not is_orchestration:
        identity = _resolve_identity_from_trace(session_uuid, role, args.work_id)
        if not identity:
            identities = state_store.read_role_identities(
                state_store.identities_path_for(session_uuid))
            identity = state_store.resolve_work_id_identity(
                identities, role, args.work_id)

    # (7) usage/duration/cost evidence, non-fatal.
    evidence = {}
    if not is_orchestration:
        evidence = _lookup_work_id_evidence(
            session_uuid, args.work_id, model=identity.get("model"),
            tool=identity.get("tool"))

    # (8) artifact fingerprint from the historical trace event.
    if not is_orchestration:
        fingerprint = _lookup_artifact_fingerprint_from_trace(
            trace_store.trace_path_for(session_uuid), role, args.work_id)
    else:
        fingerprint = {"state": "unavailable",
                       "reason": "orchestration_no_artifact"}

    # (9) build the entry — required fields always present; optional fields
    # present ONLY when set (an absent field is omitted, never null).
    entry = {
        "session_uuid": session_uuid,
        "timestamp": _eval_now(),
        "role": role,
        "output_quality": args.output_quality,
        "intent_alignment": args.intent_alignment,
        "evidence_quality": args.evidence_quality,
        "self_sufficiency": args.self_sufficiency,
        "cost_worthiness": args.cost_worthiness,
        "artifact_digest_state": fingerprint.get("state"),
    }
    if not is_orchestration:
        entry["work_id"] = args.work_id
    if fingerprint.get("state") == "observed":
        if fingerprint.get("sha256") is not None:
            entry["artifact_digest"] = fingerprint.get("sha256")
        if fingerprint.get("size") is not None:
            entry["artifact_size"] = fingerprint.get("size")
        if fingerprint.get("artifact_status") is not None:
            entry["artifact_fingerprint_status"] = \
                fingerprint.get("artifact_status")
    # Optional annotations.
    if args.eval_phase:
        entry["phase"] = args.eval_phase
    if args.eval_round is not None:
        entry["round"] = args.eval_round
    if args.eval_notes:
        entry["notes"] = args.eval_notes
    # Identity (best-effort; contribution already proven).
    for key in ("tool", "model", "session_id", "effort"):
        if identity.get(key) is not None:
            entry[key] = identity[key]
    # Evidence (best-effort).
    for key in ("duration_s", "usage", "cost_usd"):
        if key in evidence:
            entry[key] = evidence[key]

    # (10) append atomically.
    path = state_store.orchestrator_evaluations_path_for(session_uuid)
    result = state_store.append_orchestrator_evaluation(path, entry)
    if not result.get("ok"):
        sys.stderr.write(
            "cowork: --evaluate-role: could not record evaluation (%s); "
            "existing file preserved: %s\n"
            % (result.get("error", "unknown"), path))
        return 1
    io_out.write(
        "cowork: recorded evaluation for %s%s in %s\n"
        % (role, (" work_id %s" % args.work_id) if not is_orchestration
           else " phase %s" % args.eval_phase, path))
    return 0


def _eval_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def parse_team(team_arg):
    """Validate a --team value. Returns (selected, error_or_None)."""
    requested = [r.strip() for r in team_arg.split(",") if r.strip()]
    unknown = [r for r in requested if r not in ROLES]
    if unknown:
        return None, "unknown role(s): %s" % ", ".join(unknown)
    return [r for r in ROLES if r in requested], None


def apply_config_args(config, config_args):
    """Apply --config ROLE=opt,opt entries. Returns (ok, error_or_None)."""
    for item in config_args:
        if "=" not in item:
            return False, "bad --config %r (expected ROLE=opt,opt)" % item
        role, _, rest = item.partition("=")
        tokens = [t.strip() for t in rest.split(",") if t.strip()]
        ok, err = apply_config_override(config, role.strip(), tokens)
        if not ok:
            return False, err
    return True, None


# --------------------------------------------------------------------------- #
# Scout run.                                                                  #
# --------------------------------------------------------------------------- #


def scout_intel_path(intel_dir, session_uuid):
    # The per-session folder carries the uuid, so the filename does not;
    # `session_uuid` is accepted for call-site stability but unused.
    return os.path.join(intel_dir, "scout.intel.json")


# --------------------------------------------------------------------------- #
# Optional caveman compression: detected once on the cowork side and injected   #
# as a one-line writing-style directive into each role's and reviewer's brief    #
# (Q3) — deterministic, identical for claude and codex, never a role self-check. #
# --------------------------------------------------------------------------- #


def _caveman_available():
    """Whether the optional caveman terse-style tool is installed.

    Cheap — a few `shutil.which` lookups plus path-existence checks — and run
    at brief assembly, i.e. effectively at session start."""
    for command in ("caveman", "caveman-compress", "caveman-shrink"):
        if shutil.which(command) is not None:
            return True
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".claude", "skills", "caveman", "SKILL.md"),
        os.path.join(home, ".claude", "skills", "cavecrew", "SKILL.md"),
        os.path.join(home, ".claude", "plugins", "caveman", "SKILL.md"),
        os.path.join(home, ".claude", "plugins", "caveman",
                     ".claude-plugin", "plugin.json"),
        os.path.join(home, ".codex", "skills", "caveman", "SKILL.md"),
        os.path.join(home, ".codex", "skills", "cavecrew", "SKILL.md"),
        os.path.join(home, ".codex", "plugins", "caveman", "SKILL.md"),
        os.path.join(home, ".codex", "plugins", "caveman",
                     ".codex-plugin", "plugin.json"),
        os.path.join(home, ".agents", "skills", "caveman", "SKILL.md"),
        os.path.join(home, ".agents", "skills", "cavecrew", "SKILL.md"),
        os.path.join(home, ".config", "caveman"),
    ]
    extra = os.environ.get("COPLAN_CAVEMAN_PATHS", "")
    for value in extra.split(os.pathsep):
        value = value.strip()
        if value:
            candidates.append(value)
    if any(os.path.exists(p) for p in candidates):
        return True
    for base, pattern in (
        (os.path.join(home, ".claude", "skills"), "*caveman*/SKILL.md"),
        (os.path.join(home, ".claude", "skills"), "*cavecrew*/SKILL.md"),
        (os.path.join(home, ".codex", "skills"), "*caveman*/SKILL.md"),
        (os.path.join(home, ".codex", "skills"), "*cavecrew*/SKILL.md"),
        (os.path.join(home, ".agents", "skills"), "*caveman*/SKILL.md"),
        (os.path.join(home, ".agents", "skills"), "*cavecrew*/SKILL.md"),
    ):
        if glob.glob(os.path.join(base, pattern)):
            return True
    return False


def caveman_directive(available=None):
    """The one-line compression directive appended to every role/reviewer brief.

    A WRITING-STYLE instruction only: it never invokes /caveman and never
    changes any global mode. Internal/peer content is compressed only when
    caveman is installed; user-facing content is always full prose. `available`
    defaults to live detection; tests pass it explicitly."""
    if available is None:
        available = _caveman_available()
    if available:
        return (
            "Compression directive: the caveman terse-style tool IS installed. "
            "Write all INTERNAL-channel content — your `[[internal]]` "
            "self-narration, and for reviewers your whole review narration — in "
            "terse caveman ultra style (drop articles/filler/pleasantries, "
            "fragments OK), preserving every bit of technical substance and any "
            "required structure. NEVER compress user-facing content: your "
            "replies to the user stay full, clear prose. Do not invoke /caveman "
            "or change any global mode."
        )
    return (
        "Compression directive: the caveman terse-style tool is NOT installed. "
        "Write everything — user-facing and internal-channel alike — in normal, "
        "full prose. Internal-channel content still routes to the internal "
        "channel (inside `[[internal]]` blocks, or for reviewers your whole "
        "narration), just uncompressed."
    )


def assemble_scout_brief(selected, intel_path, intel_md_path=None,
                         caveman_available=None):
    """Dynamic first-message brief for the scout: where to write, the JSON +
    domain guardrail, and the plan-only fallthrough for this team.

    When `intel_md_path` is given, the scout writes TWO files: the JSON (machine
    source of truth + status channel) and a human-first markdown rendering (the
    user's review surface, also reviewed by the scout-reviewer). Both are the
    scout's write targets and nothing else."""
    if "planner" in selected:
        plan_note = (
            "A dedicated `planner` role is on the team: stop at the intel file "
            "and hand off; do NOT produce a plan."
        )
    else:
        plan_note = (
            "NO `planner` role is on the team: in the same intel JSON, also "
            "include a lightweight plan/handoff."
        )
    if intel_md_path:
        target = (
            "Write your findings as TWO files, to exactly these paths:\n"
            "  JSON (machine source of truth + your status channel): %s\n"
            "  Markdown (the user's review surface, small scannable sections): "
            "%s\n"
            "Those two intel files are your ONLY write targets. Do not create, "
            "edit, or delete any other file (reading/searching the repo is "
            "fine). Keep the markdown CONSISTENT with the JSON — it must not "
            "under- or mis-report what the JSON says."
            % (intel_path, intel_md_path)
        )
    else:
        target = (
            "Write your findings as a single JSON object to exactly this file:\n"
            "  %s\n"
            "That intel file is your ONLY write target. Do not create, edit, or "
            "delete any other file (reading/searching the repo is fine)."
            % intel_path
        )
    return "%s\n%s\n\n%s" % (
        target, plan_note, caveman_directive(caveman_available))


def read_scout_prompt(path=SCOUT_PROMPT_PATH):
    with open(path, "r") as fh:
        return fh.read()


def assemble_codex_prompt(role_text, team_note, context):
    static_prefix = (str(role_text or "").strip() + ("\n\n" + str(team_note or "").strip() if str(team_note or "").strip() else "")).strip()
    if isinstance(context, handoff.HandoffBlock):
        static_part = _codex_static_prefix_fragment(role_text, team_note)
        return handoff.compose_handoff_blocks(static_part, context)
    return _role_seed_delivery(static_prefix, str(context) if context else "")


def _emit_codex_role_prompt_bytes(trace, role, role_text):
    """Item #4 measurement: record the static role-markdown bytes inlined into a
    FRESH Codex prompt body (`assemble_codex_prompt` prepends `role_text`), as a
    dedicated `role.prompt.bytes` event tagged `role_prompt_delivery=codex_inline`.

    This is the static role/system-prompt cost, kept SEPARATE from the per-turn
    user-message `prompt_bytes` (which, for Codex, silently folds the role text
    in today). Emitted at every codex launch that actually inlines the role —
    the pure string builder has no trace handle, so each launch site calls this.
    No-op without a trace handle or role text."""
    if trace and role_text:
        trace.event("role.prompt.bytes", role=role,
                    bytes=len(role_text.encode("utf-8")),
                    delivery="codex_inline")


# --------------------------------------------------------------------------- #
# scout-reviewer: a critical reviewer paired with the scout. Invoked            #
# deterministically when the scout sets `ready_for_review`. It shares the       #
# scout's initial context (the user `context`, NOT the scout's write-target     #
# brief), reads the scout intel, and writes a verdict to its own review file.   #
# --------------------------------------------------------------------------- #


def _read_text(path):
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _call_review_fn(review_fn, status_path, round_index, force_full_reread):
    """Call the review_fn, passing `force_full_reread` (#4/D8) only when the
    callable accepts it. The real make_review_fn closure does; test-injected
    review functions keep their historical `(status_path, round)` signature."""
    if force_full_reread:
        try:
            params = inspect.signature(review_fn).parameters
            if ("force_full_reread" in params
                    or any(p.kind == p.VAR_KEYWORD for p in params.values())):
                return review_fn(status_path, round_index,
                                 force_full_reread=force_full_reread)
        except (ValueError, TypeError):
            pass
    return review_fn(status_path, round_index)


def _record_role_identity(session, result=None):
    """Upsert the session role's live identity — tool, model, effort, provider
    session id — into the per-session `identities.json` registry, so eval
    aggregation can stamp the EVALUATEE's tool+model onto score entries.

    The effort recorded is the session's CONFIG-PINNED effort: no controller
    reports a live effort, so the pinned value is the only honest source (it is
    what the trace already calls `config_pinned`). A role left on the
    controller's default records no effort at all, exactly as an unobserved
    model is left blank rather than guessed, and downstream reads it as
    unknown.

    Anchored on the session's `extra_writable_dir` (the session-assets dir for
    every real role/reviewer session). Only eval-relevant roles (ROLES) are
    registered; fake test sessions without the attrs no-op. Observational:
    never raises."""
    try:
        role = getattr(session, "speaker", None)
        directory = getattr(session, "extra_writable_dir", None)
        if not role or role not in ROLES or not directory:
            return
        result = result if isinstance(result, dict) else {}
        state_store.upsert_role_identity(
            os.path.join(directory, "identities.json"), role, {
                "tool": getattr(session, "controller", None),
                "model": (result.get("model")
                          or getattr(session, "live_model", None)
                          or getattr(session, "model", None)),
                "effort": getattr(session, "effort", None),
                "session_id": (result.get("session_id")
                               or result.get("thread_id")
                               or getattr(session, "session_id", None)
                               or getattr(session, "thread_id", None)),
                "controller_state_dir": getattr(
                    session, "controller_state_dir", None),
            })
    except Exception:  # noqa: BLE001 - identity is observational only
        pass


def _initial_user_delivery(text):
    return handoff.direct_delivery(handoff._initial_user_text(text))


def _closed_static_delivery(text):
    """Low-level mint used only by purpose-specific static boundaries."""
    return handoff.direct_delivery(handoff._static_role_text(text))


def _codex_static_prefix_fragment(role_text, team_note):
    """Typed static prefix for a fresh Codex role prompt."""
    prefix = (
        str(role_text or "").strip()
        + ("\n\n" + str(team_note or "").strip()
           if str(team_note or "").strip() else "")
    ).strip()
    return handoff._static_role_text(prefix + "\n\n")


def _headless_lead_fragment():
    """The one closed runtime note that may prefix a cross-role lead seed."""
    return handoff._static_role_text(HEADLESS_LEAD_NOTE)


def _repo_discovery_fragment(candidates, base):
    """Typed standing repo-discovery instruction for scout turns."""
    return handoff._static_role_text(
        assemble_repo_discovery_note(candidates, base))


def _worktree_seed_delivery(text):
    return _closed_static_delivery(text)


def _build_pending_source_ref(session, send_start_event_id, text):
    """Return a pending_source/v1 dict for a failed send, using the first
    available truthful discriminator: trace event ID > provider session ID >
    SHA-256 delivery fingerprint.  Never invents an attempt_id."""
    now = time.time()
    if send_start_event_id:
        return {
            "kind": "trace_event",
            "event_id": send_start_event_id,
            "event_name": "role.send.start",
            "session_id": None,
            "prompt_sha256": None,
            "created": now,
        }
    session_id = getattr(session, "session_id", None)
    if session_id:
        return {
            "kind": "provider_session",
            "event_id": None,
            "event_name": None,
            "session_id": session_id,
            "prompt_sha256": None,
            "created": now,
        }
    fingerprint = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    return {
        "kind": "delivery_fingerprint",
        "event_id": None,
        "event_name": None,
        "session_id": None,
        "prompt_sha256": fingerprint,
        "created": now,
    }


def _normalize_pending_source_for_replay(pending_entry):
    """Return (source_ref, text) for a pending switch entry, raising on mismatch.

    Legacy entries (only pending_turn, no pending_source) receive a
    deterministic in-memory source_ref of kind='legacy' derived from the text
    fingerprint.  No historical attempt or event ID is invented.

    For delivery_fingerprint sources, the stored prompt_sha256 must match the
    SHA-256 of the current pending_turn text; a mismatch raises ValueError so
    the replay fails closed rather than proceeding with mismatched state.
    """
    text = str(pending_entry.get("pending_turn") or "")
    raw_source = pending_entry.get("pending_source")
    if raw_source is None:
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source_ref = {
            "kind": "legacy",
            "event_id": None,
            "event_name": None,
            "session_id": None,
            "prompt_sha256": sha,
            "created": 0.0,
        }
        return source_ref, text
    if raw_source.get("kind") == "delivery_fingerprint":
        expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stored_sha = raw_source.get("prompt_sha256")
        if stored_sha != expected_sha:
            raise ValueError(
                "pending_source/text mismatch: stored fingerprint %r does not "
                "match SHA-256 of pending_turn text" % stored_sha)
    return dict(raw_source), text


def _mint_pending_replay_link(role, phase, pending_entry, session_uuid=None,
                              trace=None):
    """Mint and append one AttemptLink/v1 with kind='pending_replay'.

    Called at the moment the pending turn is replayed (first send accepted).
    Idempotent: re-observing the same entry is a no-op via the exactly-once
    writer.  Returns the validated link dict.
    """
    source_ref, text = _normalize_pending_source_for_replay(pending_entry)
    prompt_bytes = text.encode("utf-8")
    sha = hashlib.sha256(prompt_bytes).hexdigest()
    now = max(time.time(), source_ref.get("created") or 0.0)
    delivery_ref = {
        "prompt_kind": "pending_replay",
        "prompt_sha256": sha,
        "prompt_bytes": len(prompt_bytes),
    }
    key = dispatch.build_attempt_link_idempotency_key(
        role, "pending_replay", source_ref, 0)
    record = {
        "schema_version": 1,
        "record": "AttemptLink",
        "attempt_id": str(uuid.uuid4()),
        "role": role,
        "phase": phase,
        "kind": "pending_replay",
        "source_ref": source_ref,
        "delivery_ref": delivery_ref,
        "idempotency_key": key,
        "created": now,
    }
    link = dispatch.validate_attempt_link(record)
    if session_uuid:
        guard_broker.append_once(
            state_store.dispatch_links_path_for(session_uuid),
            link, key="idempotency_key")
    if trace:
        trace.event("dispatch.attempt_link", role=role,
                    kind="pending_replay",
                    attempt_id=link["attempt_id"],
                    idempotency_key=link["idempotency_key"])
    return link


def _make_pending_replay_cb(role, pending_entry, curr_phase, session_uuid, trace,
                            clear_fn):
    """Return an on_first_send_accepted callback that mints a pending_replay
    AttemptLink then clears the pending switch.  Minting failure is non-fatal
    (logged as a trace event) so the replay is never blocked by a missing link."""
    def _cb():
        if pending_entry and pending_entry.get("pending_turn"):
            try:
                _mint_pending_replay_link(
                    role, curr_phase, pending_entry, session_uuid, trace)
            except Exception as exc:
                if trace:
                    trace.event("dispatch.attempt_link.error", role=role,
                                kind="pending_replay", error=str(exc))
        clear_fn(role)
    return _cb


def _first_send_delivery_tracker(pending_cb=None):
    """Return `(on_first_send_accepted, on_first_send_rejected, box)` for one
    role-phase invocation.

    `box["delivered"]` starts True -- the historical assumption every
    existing caller (production and test-injected `run_scout_fn`/
    `run_planner_fn`/`run_builder_fn` alike) already relies on: a role
    invocation that returns success delivered whatever it was seeded with.
    `_role_loop` flips it to False ONLY when it can affirmatively prove this
    invocation's very first send was never accepted before the loop ended
    (see `_role_loop`'s own `first_send and on_first_send_rejected` call,
    fired at each of its send-failure-terminates-the-loop sites) -- the one
    scenario where `rc == 0` alone would otherwise wrongly justify acking a
    context the role never actually saw. A caller that never reaches (or
    never wires) either callback leaves the box at its safe default, so a
    test-injected fake that bypasses `_role_loop` entirely is byte-identical
    to its historical behavior. `pending_cb` (the pending-replay callback,
    when a pending switch turn is being replayed) fires exactly as before;
    this wrapper adds the flag without changing that callback's own
    behavior or arguments."""
    box = {"delivered": True}

    def _accepted():
        if pending_cb:
            pending_cb()

    def _rejected():
        box["delivered"] = False
    return _accepted, _rejected, box


def _build_gate_repair_attempt_link(role, phase, source_ref, prompt_text, ordinal):
    """Construct and validate a gate_repair AttemptLink for one repair delivery."""
    prompt_bytes = str(prompt_text).encode("utf-8")
    sha = hashlib.sha256(prompt_bytes).hexdigest()
    now = max(time.time(), source_ref.get("created", 0.0))
    delivery_ref = {
        "prompt_kind": "repair",
        "prompt_sha256": sha,
        "prompt_bytes": len(prompt_bytes),
    }
    key = dispatch.build_attempt_link_idempotency_key(role, "gate_repair",
                                                      source_ref, ordinal)
    record = {
        "schema_version": 1,
        "record": "AttemptLink",
        "attempt_id": str(uuid.uuid4()),
        "role": role,
        "phase": phase,
        "kind": "gate_repair",
        "source_ref": source_ref,
        "delivery_ref": delivery_ref,
        "idempotency_key": key,
        "created": now,
    }
    return dispatch.validate_attempt_link(record)


def _repair_delivery(artifact_noun, attempt_link=None):
    """Build the static repair prompt delivery and attach a gate_repair AttemptLink.

    When `attempt_link` is provided (from `_role_loop` with full role/phase/source
    context), it is attached as-is.  When omitted (standalone callers, tests),
    a minimal link is constructed from the delivery fingerprint of the repair
    prompt itself — giving the delivery a unique attempt_id while remaining
    a valid AttemptLink/v1.  The repair prompt bytes are NEVER changed.
    """
    prompt_text = _repair_prompt(artifact_noun)
    envelope = _closed_static_delivery(prompt_text)
    if attempt_link is None:
        now = time.time()
        prompt_bytes = prompt_text.encode("utf-8")
        sha = hashlib.sha256(prompt_bytes).hexdigest()
        source_ref = {
            "kind": "delivery_fingerprint",
            "event_id": None,
            "event_name": None,
            "session_id": None,
            "prompt_sha256": sha,
            "created": now,
        }
        key = dispatch.build_attempt_link_idempotency_key(
            "unknown", "gate_repair", source_ref, 0)
        attempt_link = dispatch.validate_attempt_link({
            "schema_version": 1,
            "record": "AttemptLink",
            "attempt_id": str(uuid.uuid4()),
            "role": "unknown",
            "phase": None,
            "kind": "gate_repair",
            "source_ref": source_ref,
            "delivery_ref": {
                "prompt_kind": "repair",
                "prompt_sha256": sha,
                "prompt_bytes": len(prompt_bytes),
            },
            "idempotency_key": key,
            "created": now,
        })
    envelope.attempt_link = attempt_link
    return envelope


def _missing_question_delivery(artifact_noun):
    return _closed_static_delivery(
        _missing_question_repair_prompt(artifact_noun))


def _headless_nudge_delivery(artifact_noun):
    return _closed_static_delivery(_headless_nudge_text(artifact_noun))


def _handoff_declined_delivery(text_fn):
    if text_fn not in (handoff_declined_text, handoff_declined_to_planner_text):
        raise TypeError("handoff-declined text must use a closed static source")
    return _closed_static_delivery(text_fn())


def _user_lead_delivery(text):
    return handoff.direct_delivery(handoff._user_lead_reply(text))


def _is_known_static_fragment(s):
    t = str(s or "").strip()
    if not t:
        return True
    if getattr(s, "kind", None) == "static_role" or type(s).__name__ == "_BoundaryText":
        return True
    known_markers = (
        HEADLESS_LEAD_NOTE, HEADLESS_REVIEWER_NOTE,
    )
    if any(t in str(k).strip() for k in known_markers if k):
        return True
    if ("private evaluation request" in t
            or "Write your verdict" in t
            or "scout for a cowork" in t
            or "planner for a cowork" in t
            or "builder for a cowork" in t
            or "scout-reviewer" in t
            or "planning-advisor" in t
            or "build-reviewer" in t
            or "declined" in t):
        return True
    return False


def _cross_delivery(text, blocks, static_fragments=(), trust_static=False):
    """Split exact rendered blocks from static instructions, then compose."""
    parts = []
    remainder = str(text)
    trusted_static = {str(fragment) for fragment in static_fragments}
    for block in blocks:
        marker = str(block)
        before, found, remainder = remainder.partition(marker)
        if not found:
            raise ValueError("delivered cross-role text omits its handoff block")
        if before:
            if (not trust_static
                    and before not in trusted_static
                    and not _is_known_static_fragment(before)):
                raise TypeError("cannot mint static role fragment from arbitrary text: %r" % (before,))
            parts.append(handoff._static_role_text(before))
        parts.append(block)
    if remainder:
        if (not trust_static
                and remainder not in trusted_static
                and not _is_known_static_fragment(remainder)):
            raise TypeError("cannot mint static role fragment from arbitrary text: %r" % (remainder,))
        parts.append(handoff._static_role_text(remainder))
    return handoff.cross_role_delivery(*parts)


def _role_seed_delivery(brief, context):
    """Compose a first role turn without losing handoff provenance."""
    text = (str(brief or "") + "\n\n" + str(context or "")).strip()
    if isinstance(context, handoff.HandoffBlock):
        brief_prefix = (
            str(brief).strip() + "\n\n" if str(brief or "").strip() else "")
        return _cross_delivery(
            text, [context], static_fragments=[brief_prefix])
    return _initial_user_delivery(text)


def _lead_turn_delivery(value):
    """Classify one lead continuation through closed transport constructors."""
    if isinstance(value, handoff.DeliveryEnvelope):
        return value
    if isinstance(value, handoff.HandoffBlock):
        return _cross_delivery(str(value), [value])
    raise TypeError("lead turn lacks typed user/static/handoff provenance")


def _eval_delivery(prompt, specs):
    blocks = [s.get("artifact_block") for s in specs or []
              if isinstance(s.get("artifact_block"), handoff.HandoffBlock)]
    # assemble_eval_prompt owns every byte outside the exact renderer blocks.
    # _eval_delivery is an inventoried boundary, so those generated evaluation
    # instructions are the permitted static fragments for this one envelope.
    return _cross_delivery(prompt, blocks, trust_static=True)


def _send(session, text, meta=None):
    """Send one turn, passing per-turn accounting `meta` (#1) only when the
    session's send() accepts it. Real bridge sessions do; test-injected fake
    sessions keep their historical `send(text)` signature and receive no meta,
    so the streaming/test contract stays byte-identical.

    Every turn also refreshes the role-identity registry (tool/model/session
    id) from the session + its result — see `_record_role_identity`.

    The gateway accepts ONLY an opaque transport-produced DeliveryEnvelope:
    either cross-role provenance originating in render_handoff, or one of the
    closed direct-user/static constructors. Raw controller send remains private
    to this function."""
    if not isinstance(text, handoff.DeliveryEnvelope):
        raise TypeError("_send requires a transport-produced DeliveryEnvelope")
    if text.delivery_class == "cross_role":
        # SC5 at the final gateway: controller-visible accounting comes from
        # the same opaque envelope that supplies the delivered bytes. Caller
        # metadata cannot forge, omit, or independently re-infer artifacts.
        meta = dict(meta or {})
        meta["artifacts"] = [dict(rec) for rec in text.descriptors]
    try:
        if meta is not None:
            try:
                if "meta" in inspect.signature(session.send).parameters:
                    result = session.send(text, meta=meta)
                    result = result or bridge.turn_result(True, "ok")
                    _record_role_identity(session, result)
                    return result
            except (ValueError, TypeError):
                pass
        result = session.send(text)
        if result is None:
            result = bridge.turn_result(True, "ok")
        elif isinstance(result, dict):
            if "ok" not in result:
                result = dict(result, ok=True)
            if "result" not in result:
                result = dict(result, result="ok" if result.get("ok") else "error")
        else:
            result = bridge.turn_result(True, "ok")
        _record_role_identity(session, result)
        return result
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        return bridge.turn_result(False, "error",
                                  error_type=type(exc).__name__)


def _artifact_descriptors(paths, delivery="embedded", embedded=None):
    """Content-free per-file accounting (#1/D11, #3): one
    ``{path, bytes, sha256, delivery, embedded_bytes}`` per existing file in
    `paths`, in order. Missing files are skipped. Returns None when nothing is
    present (Trace.event drops a None field).

    `delivery` is how these artifacts were sent — "embedded" (full body inline,
    the legacy default), "path" (path-first full-reread), or "diff". `embedded`
    optionally maps a path to the BYTES it actually contributed to the prompt
    (descriptor line, or descriptor + diff chunk); when absent, an embedded
    delivery counts the full body and a path/diff delivery counts 0. This lets
    the report separate "artifact size touched" (`bytes`) from "bytes actually
    embedded in the prompt" (`embedded_bytes`)."""
    embedded = embedded or {}
    out = []
    for path in paths or []:
        if not path:
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        size = len(raw)
        if path in embedded:
            emb = embedded[path]
        elif delivery == "embedded":
            emb = size
        else:
            emb = 0
        out.append({"path": path, "bytes": size,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "delivery": delivery, "embedded_bytes": emb})
    return out or None


def read_scout_reviewer_prompt(path=SCOUT_REVIEWER_PROMPT_PATH):
    with open(path, "r") as fh:
        return fh.read()


def assemble_reviewer_brief(review_path,
                            protected="the scout intel files (JSON and markdown)",
                            caveman_available=None):
    """The reviewer's write-target instruction — its analogue of the scout brief.
    It points at the review file only (never the reviewed artifact, named by
    `protected`)."""
    return (
        "Write your verdict as a single JSON object to exactly this file:\n"
        "  %s\n"
        "That review file is your ONLY write target. Do NOT edit %s "
        "or any other file (reading/searching the repo is fine). Use the "
        "verdict schema from your role (verdict: approve|revise|needs_user, "
        "findings, and user_question when needs_user).\n\n%s"
        % (review_path, protected, caveman_directive(caveman_available))
    )


def _success_criteria_flag(intel_path):
    """Light structural check (measurable-goal contract): when scout intel
    reaches review without a non-empty `result.success_criteria` list, return
    an auto-finding note to ride the reviewer's prompt; else None.

    Structure-only by design — the reviewer owns all quality judgment (are the
    criteria decidable, do the measurements fit the build context); this just
    catches the field being absent so prompt drift can't skip the contract
    silently. Tolerant: unreadable/malformed intel yields None (handled by the
    normal review path)."""
    try:
        with open(intel_path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    result = data.get("result") if isinstance(data, dict) else None
    crit = (result.get("success_criteria")
            if isinstance(result, dict) else None)
    if isinstance(crit, list) and any(
            isinstance(c, dict) and c for c in crit):
        return None
    return (
        "Orchestrator structural check: the intel JSON carries no non-empty "
        "`result.success_criteria` list. Treat this as a finding under your "
        "goal-measurability criterion — the intel must define measurable "
        "success criteria (statement, measurement, expected, tier) before it "
        "can be approved.")


def _intel_artifacts(intel_path, intel_md_path=None):
    arts = [{"label": "intel JSON (machine source of truth)",
             "path": intel_path, "kind": "json", "source": "intel_json"}]
    if intel_md_path:
        arts.append({"label": "intel markdown (the user's review surface)",
                     "path": intel_md_path, "kind": "markdown",
                     "source": "intel_md"})
    return arts


def _tempfile_artifact(text, label, kind="markdown", prefix="cowork_handoff_",
                       suffix=".txt", source=None):
    """Fallback: materialize `text` to a CONTENT-DETERMINISTIC path under the
    system temp dir and return its descriptor artifact dict. Used when no
    session-assets dir is available (direct calls/tests) so a cross-role prompt
    is still path-only. The filename is keyed by a hash of (prefix, label, text)
    so identical inputs always resolve to the SAME path — cross-run prompts stay
    byte-stable (no random tmp suffix leaking into the prompt)."""
    digest = hashlib.sha256(
        ("%s\x1f%s\x1f%s" % (prefix, label, text or "")).encode("utf-8")
    ).hexdigest()[:16]
    path = os.path.join(tempfile.gettempdir(), "%s%s%s" % (prefix, digest, suffix))
    try:
        with open(path, "w") as fh:
            fh.write(text or "")
    except OSError:
        pass
    return {"label": label, "path": os.path.abspath(path), "kind": kind,
            "source": source}


def _shared_context_artifact(context, assets_dir=None, revision=None):
    """Materialize the shared session context to a revision-keyed authoritative
    file and return its descriptor artifact dict (tagged source "context"), so a
    cross-role prompt carries the context by PATH (never inline). Under a session
    this writes to the session-assets dir; standalone (no dir — direct
    calls/tests) it writes to a fresh tempfile. The prompt is always path-only
    either way.

    Tolerant: a write failure yields an artifact whose path still points at the
    intended file, which the transport degrades to a "(missing on disk)"
    descriptor — it never raises."""
    label = "shared session context (same the reviewed role was given)"
    path = handoff.persist_context_file(assets_dir, revision, context or "")
    if path is None:
        return _tempfile_artifact(context, label, kind="markdown",
                                  prefix="cowork_context_", suffix=".md",
                                  source="context")
    return {"label": label, "path": os.path.abspath(path), "kind": "markdown",
            "source": "context"}


def _handback_payload_artifact(payload, assets_dir=None, filename=None,
                               label="hand-back note", source="payload"):
    """Materialize a hand-back payload (free-form authored text) to a file and
    return its descriptor artifact dict (tagged `source`), so the edge carries it
    by PATH."""
    path = handoff._write_file(assets_dir, filename or "handback.txt",
                               payload or "") if assets_dir else None
    if path is None:
        return _tempfile_artifact(payload, label, kind="markdown",
                                  prefix="cowork_handback_", suffix=".txt",
                                  source=source)
    return {"label": label, "path": os.path.abspath(path), "kind": "markdown",
            "source": source}


def assemble_reviewer_context(context, selected, intel_path, intel_md_path=None,
                              assets_dir=None, context_revision=None):
    """The reviewer's situational context, delivered FILE-ONLY via the shared
    transport: the SAME shared session context the scout received (materialized
    to a revision-keyed file, referenced by path — never embedded), the team
    framing, and the scout's current intel to review (JSON, and markdown when
    given — both by path). No body is inlined; the reviewer reads the files from
    disk.

    Deliberately excludes the scout's write-target `brief` / `first` payload —
    that carries the scout's own guardrail and would mis-instruct the reviewer."""
    artifacts = [_shared_context_artifact(context, assets_dir, context_revision)]
    artifacts.extend(_intel_artifacts(intel_path, intel_md_path))
    return handoff.render_handoff(
        "scout->scout-reviewer:review_ctx",
        artifacts=artifacts, facts={"team": list(selected or [])})


def assemble_reviewer_handoff(verdict, review, artifact="intel",
                              review_path=None):
    """Build the role-facing hand-back string (routes 2/5/8) via the shared
    transport. The reviewer's findings / user_question are NOT embedded: the
    lead's prompt names the REVIEW FILE path and instructs it to read the
    findings (and the user_question, for needs_user) there and relay them in its
    own voice — preserving the single-voice, faithful-relay guardrail while
    keeping the transport file-only. `artifact` names what was reviewed ("intel"
    for the scout, "plan" for the planner, "build" for the builder). Returns ""
    for `approve` (no handoff; fall through to the user gate).

    `review_path` is the reviewer's verdict file; when absent (a legacy/test
    call passing only the verdict dict), the verdict is materialized to a
    tempfile so the transport still carries a real path."""
    if verdict not in ("needs_user", "revise"):
        return ""
    if review_path:
        art = {"label": "reviewer verdict + findings (JSON)",
               "path": os.path.abspath(review_path), "kind": "json",
               "source": "review"}
    else:
        art = _handback_payload_artifact(
            json.dumps(review or {}, indent=2, sort_keys=True),
            label="reviewer verdict + findings (JSON)", source="review")
        art["kind"] = "json"
    facts = {"artifact_noun": artifact}
    if verdict == "needs_user":
        return handoff.render_handoff(
            "reviewer->lead:handback_needs_user", artifacts=[art], facts=facts)
    return handoff.render_handoff(
        "reviewer->lead:handback_revise", artifacts=[art], facts=facts)


def assemble_user_question(text, artifact="intel"):
    """Wrap a user's gate-time question in a harness-authored prompt.

    The user picked "Ask a question" at the `ready_for_review` gate (not
    "Request changes"). This is NOT reopened work: the role answers in chat and
    leaves its artifact byte-identical, so the existing hash-gate auto-skips the
    paired advisor on the unchanged follow-up. Putting the instruction at the
    harness boundary makes the behavior robust regardless of role-contract
    drift. The escape hatch is explicit: if the question genuinely surfaces new
    work, the role may edit its %s and set needs_input itself — then bytes
    change and a re-review is correct."""
    return (
        "[user question — answer in chat] The user asked a question at the "
        "review gate. This is NOT a request to change the work. Answer it "
        "conversationally in your reply. Do NOT edit your %s, and keep its "
        "status at `ready_for_review` — you will return to the same gate so the "
        "user can ask again, approve, or request changes. Only if the question "
        "genuinely surfaces new work should you edit your %s and set status "
        "back to `needs_input`.\n\n"
        "Question: %s" % (artifact, artifact, text)
    )


def scout_reviewed_text(verdict=None, round_index=None, round_cap=None):
    """Marker shown to the user so they can see a review happened (D7).

    It exposes only the verdict class, never reviewer findings or questions. The
    substring 'reviewed' is asserted by tests. With `round_index`/`round_cap` it
    appends a round counter so the user can see review-budget progress and spot
    resets (a fresh '(round 1/N)' after they re-engage)."""
    v = verdict.get("verdict") if isinstance(verdict, dict) else verdict
    counter = ""
    if round_index is not None and round_cap:
        counter = " (round %d/%d)" % (round_index, round_cap)
    if v == "approve":
        return "reviewed: approved" + counter
    if v == "revise":
        return "reviewed: changes requested" + counter
    if v == "needs_user":
        return "reviewed: needs user input" + counter
    return "reviewed" + counter


def review_skipped_text():
    """Marker shown to the user when the paired reviewer turn is SKIPPED by the
    hash-gate: the lead's reviewed artifact set is byte-identical to what that
    reviewer last approved this phase, so the prior approval is reused (D6).

    Content-free and single-voice (modeled on scout_reviewed_text); never a
    silent bypass. The substring 'review skipped' is asserted by tests."""
    return "review skipped — unchanged since last approved"


class _QuietSink:
    """A write sink that discards everything — used as the reviewer session's
    `io_out` so its raw stream is never interleaved into the user conversation
    (single-voice invariant, D7). Reports not-a-tty so sessions take plain
    paths."""

    def write(self, _s):
        return None

    def flush(self):
        return None

    def isatty(self):
        return False


def _with_status_spinner(io_out, label, fn):
    """Run `fn()` while a labeled `ui.Spinner` turns on `io_out`, ALWAYS
    stopping the spinner before returning (and therefore before any real
    io_out write `fn`'s caller makes next). Single chokepoint for the
    otherwise-silent background windows (reviewer/advisor pass, phase-boot
    probe, build-baseline git snapshot): because the spinner is torn down in a
    `finally`, the CR-frame loop can never interleave with a subsequent io_out
    write. Off a TTY `ui.Spinner` is a no-op, so scripted/test paths stay
    byte-identical. The `label` argument must NOT end in '…' — the primitive
    appends one itself."""
    spin = ui.Spinner(io_out, label=label)
    spin.start()
    try:
        return fn()
    finally:
        spin.stop()


# --------------------------------------------------------------------------- #
# Peer evaluations: after every review round both sides of the active pairing  #
# privately score each other (1-5 per criterion + feedback + enhancement       #
# suggestions); planner and planning-advisor additionally evaluate the scout    #
# once per planning phase. Each evaluator writes only its own scratch file;    #
# the orchestrator stamps metadata and aggregates into the per-session         #
# scores.json. Purely observational: failures are traced and skipped, and no   #
# evaluation content ever reaches the user or the evaluated role.              #
# --------------------------------------------------------------------------- #

# The criteria are part of the orchestration contract, not role-spec prose:
# the role specs reference "the criteria supplied in the prompt". Keyed by
# (evaluator, evaluatee). Every evaluation additionally carries free-text
# enhancement_suggestions.
EVAL_CRITERIA = {
    ("scout", SCOUT_REVIEWER): [
        "accuracy of findings",
        "helpfulness/actionability",
        "false-positive rate (nitpicks vs real gaps)",
    ],
    (SCOUT_REVIEWER, "scout"): [
        "intel quality/completeness",
        "requirement-gathering quality (questions asked vs assumptions buried)",
        "goal alignment",
        "goal measurability",
    ],
    ("planner", PLANNING_ADVISOR): [
        "accuracy of findings",
        "helpfulness toward a better plan",
        "signal-to-noise",
    ],
    (PLANNING_ADVISOR, "planner"): [
        "plan quality/feasibility",
        "responsiveness to feedback",
        "goal alignment",
        "criteria coverage",
    ],
    ("planner", "scout"): [
        "usefulness/sufficiency of intel for planning",
        "accuracy of cited code/constraints",
    ],
    (PLANNING_ADVISOR, "scout"): [
        "intel quality from planning lens",
        "goal alignment of intel",
    ],
    ("builder", BUILD_REVIEWER): [
        "accuracy of findings",
        "helpfulness toward a better build",
        "signal-to-noise",
    ],
    (BUILD_REVIEWER, "builder"): [
        "build quality vs the approved plan",
        "responsiveness to feedback",
        "goal alignment",
    ],
    ("builder", "planner"): [
        "usefulness/sufficiency of plan for building",
        "accuracy of cited code/constraints",
    ],
    (BUILD_REVIEWER, "planner"): [
        "plan-quality from build-execution lens",
        "goal alignment of plan",
    ],
}

# Which user-facing role a paired reviewer evaluates on its eval turn.
_REVIEWER_EVALUATEE = handoff.reviewer_pairs()


def _eval_artifact_descriptors(specs):
    """SC5: the per-turn artifact descriptors for an eval send, aggregated from
    the SAME `handoff.HandoffBlock`s that built the prompt — each spec's
    `artifact_block` carries the content-free `.descriptors` (verdict path,
    consumed-upstream paths) it emitted, so the trace/report never re-read or
    re-infer them. Returns None when no descriptors are present (a legacy/test
    spec whose artifact_block is a plain string)."""
    out = []
    seen = set()
    for spec in specs or []:
        for rec in getattr(spec.get("artifact_block"), "descriptors", None) or []:
            path = rec.get("path")
            if path and path not in seen:
                seen.add(path)
                out.append(rec)
    return out or None


def assemble_eval_prompt(evaluator, scratch_path, specs):
    """The private evaluation request sent to `evaluator` on its own session.

    `specs` is a list of {evaluatee, criteria, artifact_block} dicts — the
    artifact_block is a path-first descriptor block (paths + hashes + a
    read-from-disk instruction) naming the evidence FILES (the reviewer's verdict
    file for role->reviewer evals; the approved upstream artifact files for
    ->scout / ->planner evals), never their bodies. The aggregate scores path is
    deliberately never part of this prompt."""
    blocks = []
    for spec in specs:
        criteria = "\n".join("- " + c for c in spec["criteria"])
        blocks.append(
            "Evaluatee: %s\nCriteria (score each 1-5):\n%s\nEvidence:\n%s"
            % (spec["evaluatee"], criteria,
               (spec.get("artifact_block") or "").strip()))
    return (
        "[private evaluation turn] This is a private evaluation request from "
        "the cowork orchestrator. It is NOT part of the task conversation: it "
        "is never shown to the user, and the roles you evaluate never see "
        "your scores.\n\n"
        "Evaluate the following peer(s) on this session:\n\n%s\n\n"
        "Write your evaluation as a single JSON object to exactly this file:\n"
        "  %s\n"
        "For this turn only, that scratch file is an additional, exceptional "
        "write target. Use exactly this shape:\n"
        "{\"evaluations\": [{\"evaluatee\": \"<role>\", \"criteria\": "
        "[{\"name\": \"<criterion>\", \"score\": <1-5>, \"feedback\": "
        "\"<concrete feedback>\"}], \"enhancement_suggestions\": "
        "\"<free text>\"}]}\n"
        "One evaluations[] entry per evaluatee above. Score each listed "
        "criterion 1-5 with honest, concrete feedback, and always include "
        "enhancement_suggestions.\n"
        "Rules: do NOT modify your status/intel/plan/review files or any "
        "other file on this turn; never read any other role's evaluation "
        "file or any scores file; never mention this evaluation to the user. "
        "Keep your reply text minimal — the scratch file is the deliverable."
        % ("\n\n".join(blocks), scratch_path)
    )


@contextlib.contextmanager
def _muted_session(session):
    """Temporarily swap a role session's io_out for a quiet sink.

    The scout/planner session is user-facing: both bridges stream assistant
    text, spinners, and denial messages to `session.io_out`, resolved at send
    time — so a temporary swap suppresses all of it for the duration of an
    eval send with zero bridge changes. Restored in finally."""
    saved = session.io_out
    session.io_out = _QuietSink()
    try:
        yield session
    finally:
        session.io_out = saved


def _eval_timestamp():
    return datetime.datetime.now().astimezone().isoformat()


def _intel_sha256(intel_text):
    return hashlib.sha256((intel_text or "").encode("utf-8")).hexdigest()


# Backwards-compatible alias: the consumed-upstream provenance hash used to be
# named `intel_sha256` (scout intel only). It is now a generic
# `artifact_sha256` (the planning phase scores the intel, the building phase
# scores the plan). Aggregate entries written before this change still carry
# `intel_sha256`; nothing reads the hash for matching, so the rename is purely
# a field name on newly written entries.
_artifact_sha256 = _intel_sha256


def _eval_spec_stamp(spec):
    """The orchestrator-stamped fields one eval spec contributes to its
    aggregate entry: the context, plus — on consumed-upstream specs — the
    phase epoch (it scopes the once-per-phase dedupe: a hand-back round trip
    bumps it even when the re-approved upstream artifact is byte-identical)
    and the consumed-artifact hash (provenance: which artifact revision was
    scored). The epoch is stamped under whichever field the spec names
    (`planning_epoch` for the planning phase, `building_epoch` for the
    building phase)."""
    stamp = {"context": spec.get("context") or "review-round"}
    epoch_field = spec.get("epoch_field")
    if epoch_field and spec.get("epoch_value") is not None:
        stamp[epoch_field] = spec["epoch_value"]
    # Legacy specs constructed with the epoch under its own key.
    for legacy in ("planning_epoch", "building_epoch"):
        if legacy not in stamp and spec.get(legacy) is not None:
            stamp[legacy] = spec[legacy]
    sha = spec.get("artifact_sha256") or spec.get("intel_sha256")
    if sha:
        stamp["artifact_sha256"] = sha
    return stamp


def _consumed_upstream_queued(session_uuid, evaluator, evaluatee, context):
    """Whether this phase's consumed-upstream eval is already QUEUED.

    The scores-based dedupe alone stopped being sufficient once scoring was
    deferred: between enqueue and drain the entry exists but has no score, so a
    resume in that window would queue the same evaluation twice and the phase
    would be scored twice for one consumption. The queue is checked alongside
    the aggregate.
    """
    if not session_uuid:
        return False
    try:
        records = evaluation.read_queue(
            state_store.evaluation_queue_path_for(session_uuid))
    except Exception:  # noqa: BLE001
        return False
    for record in records:
        # Matched on the seat and the consumed CONTEXT. Not on `evaluatee`: a
        # queue entry's evaluatee is the round's own pairing (the planner),
        # while the consumed-upstream bundle is about a different role (the
        # scout) and is identified by its context.
        if (record.get("evaluator_seat") == evaluator
                and record.get("consumed_context") == context):
            return True
    return False


def _consumed_upstream_spec(consumed, scores_path, evaluator, round_index,
                            session_uuid=None):
    """The once-per-phase consumed-upstream eval spec `evaluator` should emit
    for the role whose artifact this phase consumed (the planner scoring the
    scout's intel in the planning phase; the builder/build-reviewer scoring
    the planner's approved plan in the building phase), or None to skip, or
    the string "deduped" when the aggregate already holds this phase's entry.

    Skips (None) when: there is no consumed-upstream wiring, it is not the
    first eval turn of the phase (the bundle rides round 1 only), the
    (evaluator, evaluatee) pair is not in EVAL_CRITERIA, or any consumed
    artifact file is missing. The evidence is a path-first FULL-REREAD packet
    over the consumed artifact files (#2 — paths/hashes/sizes + a read-from-disk
    instruction, NOT the embedded bodies), so the prompt stays self-contained
    without moving the large bodies through it. The provenance
    `artifact_sha256` is still computed by reading the files at eval time (hash
    only, never embedded)."""
    if not consumed or round_index != 1:
        return None
    evaluatee = consumed["role"]
    if (evaluator, evaluatee) not in EVAL_CRITERIA:
        return None
    paths = [p for p in (consumed.get("artifact_paths") or []) if p]
    if not paths or not all(os.path.exists(p) for p in paths):
        return None
    epoch_field = consumed.get("epoch_field")
    epoch_value = consumed.get("epoch_value")
    if state_store.has_eval_entry(
            scores_path, evaluator, evaluatee, consumed["context"],
            planning_epoch=epoch_value if epoch_field == "planning_epoch"
            else None,
            building_epoch=epoch_value if epoch_field == "building_epoch"
            else None):
        return "deduped"
    if _consumed_upstream_queued(session_uuid, evaluator, evaluatee,
                                 consumed["context"]):
        return "deduped"
    text = "\n\n".join(_read_text(p).strip() for p in paths)
    arts = [{"path": p, "kind": "json" if str(p).endswith(".json") else
             "markdown", "source": "upstream"} for p in paths]
    packet = handoff.render_handoff("eval->upstream", artifacts=arts)
    spec = {
        "evaluatee": evaluatee,
        "criteria": EVAL_CRITERIA[(evaluator, evaluatee)],
        "artifact_block": packet,
        "context": consumed["context"],
        "epoch_field": epoch_field,
        "epoch_value": epoch_value,
        "artifact_sha256": _artifact_sha256(text),
    }
    if epoch_field:
        # Legacy-named convenience key (planning_epoch / building_epoch) so
        # eval-spec consumers reading the epoch by its own name still work.
        spec[epoch_field] = epoch_value
    return spec


def _scout_consumed_upstream(intel_path, planning_epoch, intel_md_path=None):
    """The consumed-upstream descriptor for the planning phase: the planner
    and planning-advisor scoring the approved scout intel once per phase. When
    `intel_md_path` is given, BOTH intel files (JSON, then markdown) are the
    consumed artifact, so the downstream eval evidence covers both."""
    if intel_path is None:
        return None
    paths = [p for p in (intel_path, intel_md_path) if p]
    embed = (
        "The approved scout intel this phase consumed (intel JSON, then intel "
        "markdown):\n%s" if intel_md_path
        else "The approved scout intel JSON this phase consumed:\n%s")
    return {
        "role": "scout",
        "label": "scout intel",
        "artifact_paths": paths,
        "epoch_field": "planning_epoch",
        "epoch_value": planning_epoch,
        "context": "consumed-intel",
        "embed": embed,
    }


def plan_consumed_upstream(plan_json_path, plan_md_path, building_epoch):
    """The consumed-upstream descriptor for the building phase: the builder
    and build-reviewer scoring the approved plan (JSON + markdown) once per
    building phase."""
    paths = [p for p in (plan_json_path, plan_md_path) if p]
    if not paths:
        return None
    return {
        "role": "planner",
        "label": "approved plan",
        "artifact_paths": paths,
        "epoch_field": "building_epoch",
        "epoch_value": building_epoch,
        "context": "consumed-plan",
        "embed": "The approved plan this building phase consumed "
                 "(plan JSON, then plan markdown):\n%s",
    }


def _eval_turn_sidecar_path(scratch_path):
    """The eval-turn accounting sidecar that rides next to an eval scratch
    file: written by the eval SENDER right after the turn, read back (and
    stamped onto every aggregated entry) by `_aggregate_eval`."""
    return scratch_path + ".turn.json" if scratch_path else None


def _write_eval_turn_sidecar(scratch_path, session, send_result,
                             eval_turn_id, specs_count, verdict=None):
    """Persist one eval turn's accounting: the EVALUATOR's live identity
    (tool + model + provider session id), the turn's controller-reported token
    usage and wall-clock duration, and the round verdict being evaluated.

    `specs_in_turn` records how many evaluations shared this single turn (a
    round-1 consumed-upstream bundle rides the same send), so token analysis
    can attribute the turn's usage once instead of double-counting it per
    entry. Tolerant: never raises — accounting must not break an eval."""
    path = _eval_turn_sidecar_path(scratch_path)
    if not path:
        return
    send_result = send_result if isinstance(send_result, dict) else {}
    info = {
        "eval_turn_id": eval_turn_id,
        "evaluator_tool": getattr(session, "controller", None),
        "evaluator_model": (send_result.get("model")
                            or getattr(session, "live_model", None)
                            or getattr(session, "model", None)),
        "evaluator_session_id": (send_result.get("session_id")
                                 or send_result.get("thread_id")
                                 or getattr(session, "session_id", None)
                                 or getattr(session, "thread_id", None)),
        "usage": send_result.get("usage"),
        "duration_ms": send_result.get("duration_ms"),
        "specs_in_turn": specs_count,
        "reviewed_verdict": (verdict or {}).get("verdict"),
    }
    try:
        with open(path, "w") as fh:
            json.dump({k: v for k, v in info.items() if v is not None},
                      fh, indent=2, sort_keys=True)
            fh.write("\n")
    except (OSError, TypeError, ValueError):
        pass


def _read_eval_turn_sidecar(scratch_path):
    path = _eval_turn_sidecar_path(scratch_path)
    if not path:
        return {}
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clear_eval_scratch(scratch_path, role, trace=None):
    """Remove a stale eval scratch AND its turn sidecar before an eval send,
    so a turn that writes nothing yields 'no entry' with no stale accounting."""
    try:
        os.remove(scratch_path)
        if trace:
            trace.event("eval.scratch.cleared", role=role, path=scratch_path)
    except OSError:
        pass
    sidecar = _eval_turn_sidecar_path(scratch_path)
    if sidecar:
        try:
            os.remove(sidecar)
        except OSError:
            pass


def _aggregate_eval(scratch_path, scores_path, session_uuid, evaluator, phase,
                    round_index, stamp_by_evaluatee, trace=None,
                    verification=None, envelope=None, eval_work_id=None):
    """Read an evaluator's scratch file, stamp metadata, and append the
    entries to the per-session aggregate. Evaluators only provide evaluatee,
    criteria scores/feedback, and enhancement_suggestions — the orchestrator
    stamps evaluator, phase, round, context, and timestamp here so they cannot
    be misattributed or forged. A turn that wrote nothing yields 'no entry'
    (traced and skipped), never a re-read of a previous round's scores.

    The scratch file is left in place after aggregation (Q3a: gitignored,
    overwritten per round) — staleness is prevented by the clearing BEFORE
    every eval send, on both sides."""
    entries = state_store.read_eval(scratch_path)
    existed = os.path.exists(scratch_path)
    if trace:
        trace.event("eval.written", evaluator=evaluator, found=bool(entries),
                    malformed=bool(existed and not entries))
    if not entries:
        if existed and trace:
            trace.event("eval.aggregated", evaluator=evaluator, phase=phase,
                        round=round_index, count=0, result="malformed")
        return False
    stamp = _eval_timestamp()
    # Traceability stamps: the eval turn's accounting sidecar (evaluator
    # tool/model/session id, token usage, duration, shared-turn count, the
    # verdict under evaluation) plus the per-session identity registry (the
    # EVALUATEE's live tool/model/session id). Both are optional — a legacy
    # or test path without them aggregates exactly as before.
    turn_info = _read_eval_turn_sidecar(scratch_path)
    identities = state_store.read_role_identities(
        os.path.join(os.path.dirname(scores_path), "identities.json")
        if scores_path else None)
    turn_stamp = {k: turn_info.get(k) for k in (
        "eval_turn_id", "evaluator_tool", "evaluator_model",
        "evaluator_session_id", "usage", "duration_ms", "specs_in_turn")
        if turn_info.get(k) is not None}
    reviewed_verdict = turn_info.get("reviewed_verdict")
    stamped = []
    for entry in entries:
        entry = dict(entry)
        entry["evaluator"] = evaluator
        entry["phase"] = phase
        entry["round"] = round_index
        entry["context"] = "review-round"
        entry.update(stamp_by_evaluatee.get(entry.get("evaluatee")) or {})
        entry.update(turn_stamp)
        if reviewed_verdict and entry["context"] == "review-round":
            entry["reviewed_verdict"] = reviewed_verdict
        evaluatee_identity = identities.get(entry.get("evaluatee"))
        if isinstance(evaluatee_identity, dict):
            for src, dst in (("tool", "evaluatee_tool"),
                             ("model", "evaluatee_model"),
                             ("session_id", "evaluatee_session_id")):
                if evaluatee_identity.get(src) is not None:
                    entry[dst] = evaluatee_identity[src]
        entry["timestamp"] = stamp
        # THE EVIDENCE BINDING (CV-008). The seal was taken after the verdict
        # was written and validated; it is re-checked here, at scoring time.
        # Evidence that changed while the entry waited in the queue makes the
        # score UNVERIFIABLE — it is not re-hashed to whatever the file says
        # now, because that would make every score verifiable by construction.
        if isinstance(envelope, dict):
            entry["envelope_id"] = envelope.get("envelope_id")
            entry["envelope_artifacts"] = [
                {"path": a.get("path"), "sha256": a.get("sha256"),
                 "present": a.get("present"), "validated": a.get("validated")}
                for a in envelope.get("artifacts") or []]
        if isinstance(verification, dict):
            entry["verification_state"] = verification.get("state")
            if verification.get("changed"):
                entry["verification_changed"] = verification["changed"]
        if eval_work_id:
            entry["eval_work_id"] = eval_work_id
        # An evaluator that cites a round or a finding the ledger never held is
        # making a claim about history that did not happen. Both make the entry
        # unverifiable rather than merely wrong.
        cited = entry.get("cited_ids")
        if cited:
            citations = ledger.validate_citations(
                ledger.read_ledger(state_store.ledger_path_for(session_uuid)),
                cited)
            entry["citations"] = citations
            if citations.get("invented") or citations.get("withdrawn"):
                entry["verification_state"] = "changed"
                entry["citation_failure"] = True
        stamped.append(entry)
    ok = state_store.append_score_entries(scores_path, session_uuid, stamped)
    if trace:
        trace.event("eval.aggregated", evaluator=evaluator, phase=phase,
                    round=round_index, count=len(stamped),
                    result="ok" if ok else "write_failed")
    return ok


def _make_enqueue_eval_fn(role, reviewer_role, phase, scratch_path,
                          scores_path, session_uuid, intel_path=None,
                          planning_epoch=None, consumed_upstream=None,
                          trace=None, intel_md_path=None,
                          context_revision=None, review_path=None,
                          evaluation_policy=None, identities_path=None,
                          artifact_path=None):
    """Build the role-side `enqueue_fn(session, verdict, round_index)` for
    `_role_loop`, or None when eval is not wired (missing paths).

    IT SEALS AND ENQUEUES; IT NEVER SENDS (P4/P12). The old closure sent an
    evaluation turn on the ROLE'S OWN session, which had two consequences worth
    stating plainly: the agent being measured shared a context with the
    measurement, and the round waited for its own scoring before the fix could
    go back. Both are gone. What happens here is a hash and a file append.

    Sealing is done AFTER the verdict file is written and validated, which is
    the structural fix for CV-008: the evidence digest can no longer be taken
    before the evidence exists.

    The queue entry is SELF-CONTAINED, because the process that drains it may
    not be this one — a session killed mid-phase leaves its rounds queued, and
    the next start drains them from disk with the original digests.
    """
    if not (scratch_path and scores_path and session_uuid):
        return None
    if consumed_upstream is None:
        consumed_upstream = _scout_consumed_upstream(
            intel_path, planning_epoch, intel_md_path)
    consumed_done = {"done": consumed_upstream is None}

    def enqueue_fn(session, verdict, round_index):
        # The DURABLE round identity, allocated BEFORE the policy decision.
        # `round_index` is the in-loop counter, which restarts at 0 on a resume;
        # deciding from it made `sampled` restart its 1/3/5 selection after
        # every resume instead of continuing the monotonic sequence, and it made
        # the queue, ledger, chain and cost joins merge pre- and post-resume
        # rounds. One durable number now drives all of them.
        durable_round = state_store.next_phase_round(session_uuid, phase, role)
        if durable_round is None:
            durable_round = round_index
        decision = evaluation.decide(
            evaluation_policy or state_store.DEFAULT_EVALUATION_POLICY,
            durable_round)
        # THE CHAIN ROTATES ON EVERY VALIDATED ROUND, selected or not. Returning
        # early on a skip left the chain pointing at the last SCORED round, so
        # under `sampled` a selected round 3 received round 1 as its "prior
        # feedback" — evidence from two rounds ago, presented as immediately
        # prior. Whether a round is scored is a policy question; what came just
        # before it is a fact.
        rotated = _rotate_evidence_chain(session_uuid, role, review_path,
                                         artifact_path, phase, durable_round)
        if not decision.get("selected"):
            # A skipped round is RECORDED as skipped with its reason, so the
            # saving a lower policy bought is visible rather than merely absent.
            if trace:
                trace.event("eval.skipped", evaluator=role, phase=phase,
                            round=durable_round, loop_round=round_index,
                            policy=decision.get("policy"),
                            rule=decision.get("rule"),
                            reason=decision.get("reason"))
            return
        # The verdict file is OVERWRITTEN every round, so sealing its live path
        # would seal a moving target: by drain time it holds a later round's
        # bytes and every deferred entry goes unverifiable by construction.
        # Each round's verdict is therefore frozen to its own immutable
        # revision file first, and THAT is what gets sealed.
        # THE FULL P6 CHAIN, on both ends: the artifact revision under review,
        # the verdict being scored, and the prior round's BOTH. Sealing only the
        # verdicts left an evaluator unable to see what the verdict was about,
        # and sealing no prior artifact left it unable to see what changed.
        frozen = rotated["verdict_path"]
        frozen_artifact = rotated["artifact_path"]
        prior = rotated["prior"]
        artifacts = _chain_artifacts(frozen, frozen_artifact, prior,
                                     review_path, artifact_path, reviewer_role,
                                     role)
        envelope = evaluation.seal_round(
            artifacts, validate=_validated_verdict_file,
            context={"phase": phase, "round": durable_round,
                     "prior_round": prior.get("round")})
        entry = {
            "entry_id": str(uuid.uuid4()),
            "session_uuid": session_uuid,
            "evaluator_seat": role,
            "evaluatee": reviewer_role,
            "criteria": EVAL_CRITERIA.get((role, reviewer_role)) or [],
            "phase": phase,
            "round": durable_round,
            "loop_round": round_index,
            "policy_decision": decision,
            "scratch_path": scratch_path,
            "scores_path": scores_path,
            "review_path": frozen or (os.path.abspath(review_path)
                                      if review_path else None),
            "context_revision": context_revision,
            "reviewed_verdict": (verdict or {}).get("verdict"),
            "identity_snapshot": evaluation.evaluator_identity(
                state_store.read_role_identities(identities_path), role),
            "envelope": envelope.as_dict(),
            "consumed_upstream": (None if consumed_done["done"]
                                  else consumed_upstream),
            # Named the same way on both seats, so the once-per-phase dedupe
            # reads one field regardless of which side queued it.
            "consumed_context": (None if consumed_done["done"]
                                 else (consumed_upstream or {}).get("context")),
        }
        consumed_done["done"] = True
        queue_path = state_store.evaluation_queue_path_for(session_uuid)
        ok = evaluation.enqueue(queue_path, entry)
        if trace:
            # Recorded BEFORE anything scores this round, and before the fix
            # handoff is assembled — the ordering invariant C4 asserts.
            trace.event("eval.enqueued", evaluator=role,
                        evaluatee=reviewer_role, phase=phase,
                        round=round_index, entry_id=entry["entry_id"],
                        envelope_id=envelope.envelope_id,
                        sealed_complete=envelope.complete,
                        result="ok" if ok else "write_failed")

    return enqueue_fn


def _enqueue_reviewer_eval(specs, scratch_path, scores_path, session_uuid,
                           reviewer_role, phase, round_index, review_path,
                           artifact_path, verdict, trace=None,
                           evaluation_policy=None):
    """Seal and queue the REVIEWER-seat evaluation for one round.

    The mirror of `_make_enqueue_eval_fn` for the other side of the pairing. It
    seals after the reviewer's verdict file exists and is validated, and it
    sends nothing — the reviewer's own session never scores again.
    """
    if not (scratch_path and scores_path and session_uuid and specs):
        return False
    # Durable identity first, then decide from it (see the role seat).
    durable_round = state_store.next_phase_round(session_uuid, phase,
                                                 reviewer_role)
    if durable_round is None:
        durable_round = round_index
    decision = evaluation.decide(
        evaluation_policy or state_store.DEFAULT_EVALUATION_POLICY,
        durable_round)
    # Rotate BEFORE the policy decision is acted on: a skipped round is still
    # the round that immediately preceded the next one.
    rotated = _rotate_evidence_chain(session_uuid, reviewer_role, review_path,
                                     artifact_path, phase, durable_round)
    if not decision.get("selected"):
        if trace:
            trace.event("eval.skipped", evaluator=reviewer_role, phase=phase,
                        round=durable_round, loop_round=round_index,
                        policy=decision.get("policy"),
                        rule=decision.get("rule"),
                        reason=decision.get("reason"))
        return False
    # Same freeze as the role seat: the reviewer's verdict and the artifact it
    # reviewed are both rewritten between rounds, so each is pinned to an
    # immutable per-round revision before it is sealed.
    # The reviewer seat carried NO prior round at all, so its evaluations could
    # never observe responsiveness.
    frozen_verdict = rotated["verdict_path"]
    frozen_artifact = rotated["artifact_path"]
    prior = rotated["prior"]
    artifacts = _chain_artifacts(frozen_verdict, frozen_artifact, prior,
                                 review_path, artifact_path, reviewer_role,
                                 specs[0].get("evaluatee"))
    envelope = evaluation.seal_round(
        artifacts, validate=_validated_verdict_file,
        context={"phase": phase, "round": round_index})
    entry = {
        "entry_id": str(uuid.uuid4()),
        "session_uuid": session_uuid,
        "evaluator_seat": reviewer_role,
        "evaluatee": specs[0].get("evaluatee"),
        "criteria": specs[0].get("criteria") or [],
        "phase": phase,
        "round": durable_round,
        "loop_round": round_index,
        "policy_decision": decision,
        "scratch_path": scratch_path,
        "scores_path": scores_path,
        "review_path": frozen_verdict or (os.path.abspath(review_path)
                                          if review_path else None),
        "reviewed_verdict": (verdict or {}).get("verdict"),
        "identity_snapshot": evaluation.evaluator_identity(
            state_store.read_role_identities(
                state_store.identities_path_for(session_uuid)),
            reviewer_role),
        "envelope": envelope.as_dict(),
        "consumed_context": (specs[1].get("context")
                             if len(specs) > 1 else None),
    }
    ok = evaluation.enqueue(
        state_store.evaluation_queue_path_for(session_uuid), entry)

    if trace:
        trace.event("eval.enqueued", evaluator=reviewer_role,
                    evaluatee=entry["evaluatee"], phase=phase,
                    round=round_index, entry_id=entry["entry_id"],
                    envelope_id=envelope.envelope_id,
                    sealed_complete=envelope.complete,
                    result="ok" if ok else "write_failed")
    return ok


def _record_findings(session_uuid, verdict, discoverer, phase, round_index,
                     review_path=None):
    """Append a reviewer's typed corrective findings to the ledger.

    Best-effort in every direction: no session, no ledger, no typed findings, or
    a write failure all leave the run untouched. A finding the reviewer wrote as
    prose rather than as a typed entry is NOT invented into a typed one — it is
    simply not a corrective finding, which is the CV-030 distinction.
    """
    if not (session_uuid and isinstance(verdict, dict)):
        return []
    typed = verdict.get("corrective_findings")
    if not isinstance(typed, list) or not typed:
        return []
    path = state_store.ledger_path_for(session_uuid)
    out = []
    for finding in typed:
        if not isinstance(finding, dict):
            continue
        record = ledger.append_finding(
            path, summary=finding.get("summary"),
            severity=finding.get("severity"),
            criterion=finding.get("criterion"),
            evidence_path=finding.get("evidence_path") or review_path,
            evidence_sha256=finding.get("evidence_sha256"),
            discoverer=discoverer, round_index=round_index, phase=phase,
            disposition=finding.get("disposition"),
            closure=finding.get("closure"),
            superseded_by_transaction=finding.get(
                "superseded_by_transaction"))
        if record:
            out.append(record["id"])
    return out


def _corrective_finding_count(verdict):
    """How many TYPED CORRECTIVE findings a verdict carries (CV-030).

    The raw length of `findings` was the wrong measure: an approving reviewer
    puts its summary prose in that array, so an approval could report several
    "findings" and look indistinguishable from a round that demanded changes.
    Only entries that are actually corrective count, and an approval counts
    ZERO — which is what makes "how much did review change" measurable at all.
    """
    if not isinstance(verdict, dict):
        return 0
    typed = verdict.get("corrective_findings")
    if isinstance(typed, list):
        return len([f for f in typed if isinstance(f, dict) and (
            f.get("summary") or f.get("severity"))])
    if str(verdict.get("verdict") or "").strip() == "approve":
        # An approving verdict from a reviewer that has not yet moved to the
        # typed shape: its `findings` are prose, not corrections.
        return 0
    findings = verdict.get("findings")
    return len(findings) if isinstance(findings, list) else 0


def _source_paths_for_manifest(cwd=None):
    """Every file the build's result depends on: tracked AND untracked-but-not-
    ignored.

    `git ls-files` alone was the defect. A build that ADDS modules leaves them
    untracked until someone commits, so a tracked-only digest is structurally
    blind to exactly the files the build created — this very build added eight
    new source files, and none of them could have invalidated a readiness
    claim. `--others --exclude-standard` adds new files while still respecting
    .gitignore, so build products and caches stay out.
    """
    import subprocess
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, timeout=30)
        if listed.returncode != 0:
            return None
        paths = {p for p in listed.stdout.splitlines() if p.strip()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    # FIXTURES ARE SOURCE TRUTH and are NOT excluded. An earlier version dropped
    # them because running the report checks appended to a fixture ledger, so
    # verification mutated the tree it was verifying — but excluding them meant
    # a fixture could change without invalidating a promotion, and a fixture IS
    # the evidence several criteria are decided on. The mutation is fixed at its
    # source instead: tracked session assets take the read-only report path.
    # That path skips ledger reconciliation and record persistence, building
    # any requested record in memory, so the checked-in fixture is never
    # written to.
    return sorted(paths)


def _stamp_observed_provenance(observations, digest=None, clock=None):
    """Bind cowork's OWN view of the tree to each attempt it can vouch for.

    An attempt that started after the sources last changed is, as far as this
    process can observe, an attempt against the current tree — so the current
    digest is recorded on it as an observation cowork made, distinct from
    anything the builder claimed. Attempts it cannot place are left unstamped
    rather than given a digest they did not earn.
    """
    digest = digest or _current_tree_digest()
    if clock is None:
        clock = measure.newest_source_mtime(os.getcwd(),
                                            _source_paths_for_manifest())
    if not (digest and getattr(clock, "usable", False)):
        return observations
    for attempt in observations or []:
        if ingest.attempt_predates_tree(attempt, clock.mtime) is False:
            attempt["observed_source_digest"] = digest
    return observations


def _session_assets_are_tracked(session_uuid):
    """Whether this session's assets are files git tracks.

    True for the checked-in measurement fixtures, which a report must never
    write to — they are inputs the criteria are decided on, and a report that
    edited them would invalidate its own evidence. False for a real session
    under ~/.cowork, which is where a record belongs.
    """
    import subprocess
    directory = state_store.session_assets_dir(session_uuid)
    if not os.path.isdir(directory):
        return False
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", directory],
            capture_output=True, text=True, timeout=10)
        return listed.returncode == 0 and bool(listed.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        # Cannot tell: assume tracked and do not write. Refusing to write is
        # always safe; writing into source truth is not.
        return True


# A controller flushes its log asynchronously, so an attempt that has already
# happened can be briefly invisible. Re-ingesting a bounded number of times is
# cheap and turns most `evidence_pending` into `evidence_present` without
# re-running any work. It is BOUNDED because waiting forever for evidence that
# is never coming is just a hang with better manners.
JOIN_RETRY_ATTEMPTS = 3
JOIN_RETRY_DELAY_SECONDS = 2.0


def _joined_verification_claims(session_uuid, entries, retries=None):
    """The builder's claims with their controller-log state attached.

    Re-ingests up to `JOIN_RETRY_ATTEMPTS` times while any claim is still
    `evidence_pending`, so a log that is merely behind resolves itself instead
    of being reported as evidence that does not exist.

    Best-effort: if the join cannot run, the claims come back unjoined and the
    gate falls through to its other conditions rather than passing on a check it
    could not perform.
    """
    import time
    rounds = JOIN_RETRY_ATTEMPTS if retries is None else retries
    joined = entries
    for index in range(max(1, rounds)):
        joined = _join_once(session_uuid, entries)
        pending = [c for c in joined
                   if isinstance(c, dict)
                   and c.get("evidence_state") == "evidence_pending"]
        if not pending or index == max(1, rounds) - 1:
            break
        time.sleep(JOIN_RETRY_DELAY_SECONDS)
    return joined


def _join_once(session_uuid, entries):
    try:
        identities = state_store.read_role_identities(
            state_store.identities_path_for(session_uuid))
        bundled = os.path.join(state_store.session_assets_dir(session_uuid),
                               "controller_logs")
        claude_root = os.path.join(bundled, "claude")
        codex_root = os.path.join(bundled, "codex")
        results = ingest.ingest_session(
            identities, cwd=os.getcwd(),
            claude_root=claude_root if os.path.isdir(claude_root) else None,
            codex_root=codex_root if os.path.isdir(codex_root) else None)
        # ALL tool activity, not only verification-classified attempts: a
        # readiness claim is about a command that ran, and `--check` / `--report`
        # are not verify-classified.
        observations = ingest.observations_for(results,
                                               verification_only=False)
        # ORCHESTRATOR-OBSERVED PROVENANCE. Each attempt is stamped with the
        # digest of the tree as cowork sees it right now, for attempts that
        # started after the last source change. This is a first-hand
        # observation, not an inference from the claim and not an inference
        # from a clock; where it exists the gate prefers it outright.
        _stamp_observed_provenance(observations)
        # THROUGH THE LEDGER, not around it. Joining readiness directly against
        # id-free observations left every claim citing `log_attempt_ids: []`
        # and every corroborating attempt with a null id — so a durable claim
        # was not checkable against orchestrator-minted attempt IDs at all,
        # which is the whole point of the ledger owning them. Reconciliation is
        # idempotent, so doing it here cannot renumber history.
        ledger_path = state_store.ledger_path_for(session_uuid)
        ledger.reconcile_attempts(ledger_path, observations)
        minted = ledger.active_attempts(ledger.read_ledger(ledger_path))
        joined, _ = measure.join_claims_and_attempts(
            entries, minted, log_lag_seconds=measure.log_lag(results))
        return joined
    except Exception:  # noqa: BLE001
        return entries


def _required_verification_labels(session_uuid):
    """The verification labels the approved plan actually names, or None.

    Without this the gate accepted ANY nonempty set of green entries — a role
    could run one cheap check and be promoted. Readiness has to mean the plan's
    inventory ran, not that something did.
    """
    try:
        directory = state_store.session_assets_dir(session_uuid)
        plan = measure._read_json(os.path.join(directory,
                                               "planner.plan.json"))
        entries = ((plan or {}).get("result") or {}).get("verification")
        # label -> the exact command the plan names for it.
        mapping = {e.get("label"): e.get("command") for e in entries
                   if isinstance(e, dict) and e.get("label")}
        return mapping or None
    except Exception:  # noqa: BLE001
        return None


def _raw_plan_verification(session_uuid):
    """The approved plan's raw `result.verification` array, or None when the
    plan artifact is absent/unreadable. The sole read of that array for the
    owned-transaction path below — everything downstream goes through
    `cowork_verification.normalize_inventory`, never a hand-rolled label-only
    reading of the plan."""
    try:
        directory = state_store.session_assets_dir(session_uuid)
        plan = measure._read_json(os.path.join(directory,
                                               "planner.plan.json"))
        entries = ((plan or {}).get("result") or {}).get("verification")
        return entries if isinstance(entries, list) else None
    except Exception:  # noqa: BLE001
        return None


def _declared_plan_schema(session_uuid):
    """The plan's OWN `result.verification_schema` field, or None when the
    plan never set one. Read separately from `_raw_plan_verification` (the
    entries array) so `normalize_inventory` can compare what the PLAN
    declared against the SHAPE of its entries — an entry-level field can
    never upgrade or downgrade what the plan itself declared."""
    try:
        directory = state_store.session_assets_dir(session_uuid)
        plan = measure._read_json(os.path.join(directory,
                                               "planner.plan.json"))
        return ((plan or {}).get("result") or {}).get("verification_schema")
    except Exception:  # noqa: BLE001
        return None


def _plan_inventory(session_uuid):
    """Normalize the approved plan's verification array via
    `cowork_verification.normalize_inventory`, preserving label/command/
    execution_mode/kind and any measurement metadata (schema-2), or applying
    the explicit schema-1 legacy normalization (label/command only plans).

    Returns `(schema, entries, final_suite_label)`, or `(None, None, None)`
    when the plan carries no verification array at all or the array fails
    validation — the caller decides how to treat that (an empty/invalid
    inventory is reported, never silently treated as "nothing required")."""
    raw = _raw_plan_verification(session_uuid)
    if not raw:
        return None, None, None
    try:
        return verification.normalize_inventory(
            raw, declared_schema=_declared_plan_schema(session_uuid))
    except verification.InventoryError:
        return None, None, None


def _adjudicate_readiness(entries, claimed, required=None):
    """Decide whether a promotion is verified. Returns `(state, manifest, why)`.

    FAILS CLOSED on every ambiguity. Each condition below was a way a promotion
    used to slip through:

    - a missing plan label means the inventory did not run;
    - an entry with no `source_manifest` is not evidence about any tree, and
      previously it was simply ignored if some OTHER entry carried one;
    - a declared expectation that its own output contradicts is a failure even
      when the entry says `ok`;
    - and a claim the controller log contradicts is not verification at all.
    """
    if not entries:
        return "unverified", None, "no verification entries recorded"
    labels = {e.get("label") for e in entries if e.get("label")}
    if required:
        missing = sorted(set(required) - labels)
        if missing:
            return ("unverified", None,
                    "the plan's verification inventory did not all run; "
                    "missing: %s" % ", ".join(missing[:4]))
        # A matching label set proves only that the NAMES line up. Each entry
        # must have run the command the plan names for that label, or a role
        # could satisfy the inventory by relabelling something cheaper.
        if isinstance(required, dict):
            by_label = {e.get("label"): e for e in entries}
            wrong = []
            for label, command in required.items():
                if not command:
                    continue
                actual = (by_label.get(label) or {}).get("command")
                # A MISSING command is wrong, not exempt. Requiring `actual` to
                # be truthy meant an entry that recorded no command at all
                # satisfied the exact-command gate — the easiest way to pass it
                # was to record nothing.
                if not actual or actual.strip() != command.strip():
                    wrong.append(label)
            if wrong:
                return ("unverified", None,
                        "these ran a different command than the plan names: %s"
                        % ", ".join(sorted(wrong)[:4]))
    failed = [e.get("label") for e in entries if not e.get("ok")]
    if failed:
        return ("unverified", None, "verification failed: %s"
                % ", ".join(str(label) for label in failed[:4]))
    unstamped = [e.get("label") for e in entries
                 if not e.get("source_manifest")]
    if unstamped:
        return ("unverified", None,
                "no source_manifest on: %s — those results describe no "
                "known tree" % ", ".join(str(label) for label in unstamped[:4]))
    manifests = {e.get("source_manifest") for e in entries}
    if len(manifests) > 1:
        return ("unverified", None,
                "verification spans %d source manifests, so no single tree "
                "state was fully verified" % len(manifests))
    verified = next(iter(manifests))
    # A declared expectation the output contradicts.
    for entry in entries:
        expected = entry.get("expected_test_count")
        observed = entry.get("observed_test_count")
        if observed is None:
            observed = ingest.parse_test_count(entry.get("output_excerpt") or "")
        if isinstance(expected, int) and observed is not None and (
                observed != expected):
            return ("unverified", verified,
                    "%s expected %d tests, its output reports %d"
                    % (entry.get("label"), expected, observed))
        if expected == 0 or observed == 0:
            return ("unverified", verified,
                    "%s executed 0 tests: exit status certifies nothing"
                    % entry.get("label"))
    if not claimed:
        return ("unverified", verified,
                "the promoted tree could not be hashed, so it cannot be "
                "compared with what was verified")
    if verified != claimed:
        return ("unverified", verified,
                "the tree moved after verification ran: verified %s, "
                "promoting %s" % (str(verified)[:12], str(claimed)[:12]))
    # LAST, because it is the strongest requirement and its message should not
    # mask a simpler problem with the tree or the inventory: every mandatory
    # claim needs a fresh, unpiped, positively corroborated run against the tree
    # being promoted. Old failures stay on the record; they are simply not the
    # evidence a promotion runs on.
    # The independent clock: when the sources last changed. `None` paths mean
    # the clock could not be read at all, which the adjudicator fails closed on
    # rather than treating as "nothing has changed".
    newest_mtime = measure.newest_source_mtime(
        os.getcwd(), _source_paths_for_manifest())
    unsupported = []
    for entry in entries:
        if required and entry.get("label") not in required:
            continue          # not mandatory: reported, not gated
        why_not = measure.blocks_readiness(entry, manifest=verified,
                                           newest_mtime=newest_mtime)
        if why_not:
            unsupported.append("%s (%s)" % (entry.get("label"), why_not))
    if unsupported:
        return ("unverified", verified,
                "not corroborated by a fresh unpiped run against this tree: %s"
                % "; ".join(unsupported[:3]))
    return "verified", verified, None


def unverified_readiness_text(reason):
    """What the user sees when a promotion is handed back unverified."""
    return ("Readiness was claimed before it was verified, so the work was "
            "reopened rather than reviewed.\n%s\nThe cause has been named so "
            "it can be repaired; a new owned verification transaction "
            "decides the next promotion, not a rerun of this one."
            % (reason or "The verified tree and the promoted tree differ."))


# The hand-back body is a STATIC template with one normalized reason code
# substituted in; the reason comes from a closed set computed by
# `_record_readiness`/`_record_readiness_from_transaction`, never from role
# output, so nothing free-form rides here. THE SOLE CALLER (`_role_loop`) is
# always the owned-transaction builder path (see `_run_owned_verification_
# transaction` at the one call site) — so this text must never tell a role
# to "re-run" anything: an owned transaction is never re-run to manufacture
# evidence, and repeating this exact wording for a non-owned reason (an
# invalid/missing plan inventory) would be equally wrong, since the fix
# there is a plan repair, not a rerun either.
UNVERIFIED_READINESS_HANDBACK = (
    "Your `ready_for_review` was not accepted: %s\n\n"
    "Do not re-run any verification command to try to produce evidence — "
    "an owned transaction is never replayed to manufacture a result, and "
    "the named cause above (or the plan's approved verification inventory, "
    "if that is what's actually wrong) is what needs repairing. Fix the "
    "underlying cause, leave the tree stable, and set `ready_for_review` "
    "again once you believe it's fixed: submitting again starts a new "
    "owned verification transaction against the tree as it then stands.")


def unverified_readiness_handback_text(reason=None):
    return UNVERIFIED_READINESS_HANDBACK % (
        reason or "verification did not cover the promoted tree")


def _unverified_readiness_delivery(reason):
    """The hand-back sent to a role whose readiness did not verify."""
    return _closed_static_delivery(unverified_readiness_handback_text(reason))


def _current_tree_digest(cwd=None):
    """The digest of every source file as it is right now, or None."""
    paths = _source_paths_for_manifest(cwd)
    if paths is None:
        return None
    return state_store.manifest_digest(
        state_store.build_manifest(cwd or os.getcwd(), paths))


def _record_readiness(session_uuid, role, status_path, round_index, trace):
    """Emit a `role.readiness` event stamped with the manifest actually verified.

    `claimed_manifest` is the tree as it is at promotion; `verified_manifest` is
    the digest the role's own verification entries say they ran against. They
    match only when nothing moved in between, which is the whole check.
    Best-effort: measurement never blocks a promotion.
    """
    if not (trace and session_uuid):
        return
    try:
        # The tree AS IT IS NOW, recomputed at promotion. Reading the persisted
        # start-of-build baseline instead compared a stale digest against
        # itself and could never detect the thing this gate exists for: a tree
        # that moved after verification ran.
        claimed = _current_tree_digest()
        artifact = measure._read_json(status_path) if status_path else None
        entries = []
        if isinstance(artifact, dict):
            result = artifact.get("result")
            candidate = (result or {}).get("verification")
            entries = candidate if isinstance(candidate, list) else []
        entries = [e for e in entries if isinstance(e, dict)]
        # JOIN THE CLAIMS TO THE LOGS BEFORE GATING. The gate's controller-log
        # check was unreachable in production: `claim_state` is attached by
        # `join_claims_and_attempts` at record-build time and never written back
        # into builder.status.json, so the raw entries the gate read carried no
        # such field and the contradiction list was always empty. Doing the join
        # here is what makes that check real.
        entries = _joined_verification_claims(session_uuid, entries)
        required = _required_verification_labels(session_uuid)
        state, verified, reason = _adjudicate_readiness(entries, claimed,
                                                        required)
        event_id = trace.event(
            "role.readiness", role=role, round=round_index,
            claimed_manifest=claimed, verified_manifest=verified,
            state=state, reason=reason)
        return {"state": state, "reason": reason, "event_id": event_id,
                "claimed_manifest": claimed, "verified_manifest": verified}
    except Exception:  # noqa: BLE001
        # A gate that cannot evaluate must not PASS. Returning None here read as
        # permission to proceed, which is a fail-open gate — the one thing a
        # gate may never be.
        return {"state": "unverified", "event_id": None,
                "reason": "the readiness gate could not be evaluated"}


# The reason NAMED to the builder when an owned transaction invalidates
# readiness: the underlying reason (from `_owned_transaction_reason` below)
# plus a pointer to exactly which owned run produced the evidence. This is
# text substituted INTO `_unverified_readiness_delivery`'s existing static
# template — the closed hand-back boundary this session already has — never a
# second delivery path of its own (see TransportChokePointTests).
OWNED_TRANSACTION_REASON_SUFFIX = (
    "%s (owned verification transaction %s, verdict %s; see "
    "verification/transactions/%s/result.json under the session's assets "
    "for the exact attempt(s) that did not pass)")


def _owned_transaction_reason_text(result, reason):
    transaction_id = result.get("transaction_id")
    return OWNED_TRANSACTION_REASON_SUFFIX % (
        reason or "the owned verification transaction did not certify "
        "this candidate", transaction_id, result.get("verdict"),
        transaction_id)


def _owned_transaction_reason(result):
    """A short, closed-vocabulary reason string for a red/unverified
    `TransactionResult`, naming the first attempt (if any) that did not pass
    or the structural cause (mutation, worker identity) otherwise. Static and
    derived entirely from the result dict — never from role-authored prose."""
    if not isinstance(result, dict):
        return "the transaction produced no result"
    if not result.get("worker_identity_verified"):
        return ("the verification worker's self-reported source did not "
                "match the immutable snapshot, so nothing it ran can be "
                "trusted")
    mutation = result.get("mutation")
    if mutation:
        changed = mutation.get("changed_paths") or []
        return ("source or the git index moved during verification (%s)"
                % (", ".join(changed[:4]) or mutation.get("reason")
                   or "unspecified change"))
    for attempt in result.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if attempt.get("evidence_state") not in (
                None, verification.EVIDENCE_PRESENT):
            return ("%s: evidence %s"
                    % (attempt.get("label"), attempt.get("evidence_state")))
        if attempt.get("timed_out"):
            return "%s timed out" % attempt.get("label")
        if attempt.get("exit_code") not in (0, None):
            return ("%s exited %s" % (attempt.get("label"),
                                       attempt.get("exit_code")))
    return "the transaction did not reach a green verdict"


def _run_owned_verification_transaction(session_uuid, role, round_index,
                                        trace, repo=None,
                                        run_transaction_fn=None,
                                        work_id=None):
    """Synchronously submit ONE owned verification transaction for the
    approved plan's inventory, at the builder's ready-for-review transition.

    This is the orchestrator gate itself (P: "builder readiness becomes an
    orchestrator gate instead of an agent verification claim"): the builder
    never runs verification commands inside its own controller turn for this
    check, and its prose cannot override the result. Returns
    `(TransactionResult_or_None, reason_or_None)` — a `None` result with a
    reason means the transaction could not even be attempted (missing/invalid
    plan inventory, no session), which is itself an unverified outcome for
    the caller to hand back exactly like a red transaction.

    `work_id` (M2 Package E, additive): the builder's own WorkUnit join key
    (see `_role_work_id`), threaded straight through to `run_transaction`'s
    own additive `work_id` — purely a correlation field on the persisted
    verification request document.
    """
    run_transaction_fn = run_transaction_fn or verification.run_transaction
    if not session_uuid or not trace:
        # No session identity (or no trace to attach the transaction event
        # to) means the gate cannot even be evaluated at all — mirrors
        # `_record_readiness`'s own best-effort bypass (`if not (trace and
        # session_uuid): return`) for scout/planner, so a caller that has not
        # wired session tracking is not blocked at every builder promotion.
        return None, None
    raw = _raw_plan_verification(session_uuid)
    if not raw:
        return None, "the approved plan carries no verification inventory"
    declared_schema = _declared_plan_schema(session_uuid)
    try:
        verification.normalize_inventory(raw, declared_schema=declared_schema)
    except verification.InventoryError as exc:
        return None, "the approved plan's verification inventory is " \
            "invalid (%s): %s" % (exc.code, exc)
    repo = repo or os.getcwd()
    try:
        result = run_transaction_fn(repo, session_uuid, raw, work_id=work_id)
    except verification.InventoryError as exc:
        return None, "the approved plan's verification inventory is " \
            "invalid (%s): %s" % (exc.code, exc)
    if trace:
        trace.event(
            "verification.transaction", role=role, round=round_index,
            transaction_id=result.get("transaction_id"),
            request_key=result.get("request_key"),
            verdict=result.get("verdict"),
            final_suite_binding=result.get("final_suite_binding"),
            reused_lock_result=bool(result.get("reused_lock_result")))
    return result, None


def _record_readiness_from_transaction(session_uuid, role, round_index,
                                       trace, result, missing_reason=None,
                                       repo=None):
    """Emit the builder's `role.readiness` event directly from an owned
    `TransactionResult` — the manifest/index the transaction itself captured
    and verified against, never a controller-log rejoin. This is what
    replaces `_record_readiness`'s controller-log-derived truth for owned
    commands; scout/planner promotion (which has no candidate build or
    verification inventory) is unaffected and keeps using `_record_readiness`.

    `result is None` with `missing_reason is None` means the gate itself
    could not be evaluated at all (no session identity / no trace) —
    mirroring `_record_readiness`'s own best-effort bypass, this does NOT
    invalidate the promotion (a gate that cannot run must not fabricate a
    failure any more than it may fabricate a pass). `result is None` WITH a
    `missing_reason` means the gate DID run and found the plan's inventory
    missing/invalid — that is a genuine unverified outcome.

    A GREEN verdict alone is never sufficient for `state="verified"`: the
    candidate as it stands RIGHT NOW (at promotion time) must be IDENTICAL
    to the exact candidate the transaction's snapshot verified — otherwise
    a green result for an earlier tree state would silently certify a
    later, unverified edit. Both digests are computed by `verification.
    current_candidate_identity`/`build_snapshot`'s ONE canonical algorithm
    (never `cowork_state.manifest_digest`, a different scheme that would
    disagree with the transaction's own digest even when nothing moved) and
    compared for EXACT equality.
    """
    if result is None and missing_reason is None:
        return None
    if result is None:
        reason = missing_reason
        event_id = trace.event(
            "role.readiness", role=role, round=round_index,
            claimed_manifest=None, verified_manifest=None,
            state="unverified", reason=reason) if trace else None
        return {"state": "unverified", "reason": reason,
                "event_id": event_id, "claimed_manifest": None,
                "verified_manifest": None, "transaction_id": None}
    verdict = result.get("verdict")
    snapshot = result.get("snapshot") or {}
    verified_manifest = snapshot.get("manifest_digest")
    verified_index = snapshot.get("index_digest")
    claimed, claimed_index = verification.current_candidate_identity(
        repo or os.getcwd())
    if verdict == verification.VERDICT_GREEN:
        if (claimed is not None and claimed == verified_manifest
                and claimed_index is not None
                and claimed_index == verified_index):
            state, reason = "verified", None
        else:
            state = "unverified"
            reason = ("the candidate has moved since the owned "
                     "verification transaction verified it (manifest/index "
                     "no longer match the reviewed snapshot); a green "
                     "verdict for a DIFFERENT tree state does not certify "
                     "this one")
    else:
        state = "unverified"
        reason = _owned_transaction_reason(result)
    event_id = trace.event(
        "role.readiness", role=role, round=round_index,
        claimed_manifest=claimed, verified_manifest=verified_manifest,
        state=state, reason=reason,
        transaction_id=result.get("transaction_id"),
        verdict=verdict) if trace else None
    return {"state": state, "reason": reason, "event_id": event_id,
            "claimed_manifest": claimed, "verified_manifest": verified_manifest,
            "transaction_id": result.get("transaction_id"), "verdict": verdict}


# --------------------------------------------------------------------------- #
# Owned-verification receipt: pointer, overlay, dispositions, supersession.    #
# (ORCH-050 / CV-050 / UX-021 — the receipt the owned transaction already      #
# writes is bound to the promotion here and rendered as ONE derived overlay on #
# both gate surfaces; review dispositions ride `verification.disposition`      #
# trace events + a reconciled sidecar; defeated verification challenges are    #
# mechanically superseded so they can never by themselves reopen the builder.) #
# --------------------------------------------------------------------------- #


def _verification_contradiction(session_uuid, txn_result, readiness,
                                status_path, summary_path=None):
    """The D-0008 contradiction signal, computed ONCE at the builder
    ready_for_review branch from STRUCTURED state — never prose parsing.

    Fires only alongside a GREEN, currently-bound owned receipt, and only when:
      (i)   builder.status.json `result.verification` asserts a pass/fail that
            disagrees with the owned verdict (a stale red attempt, ok:null);
      (ii)  builder.status.json `result.verification` is null/absent/incomplete
            OR builder.summary.md is absent while the receipt binds (agent
            verification prose missing in EITHER carrier); or
      (iii) builder.status.json binds a manifest other than the receipt's.
    """
    if not (isinstance(txn_result, dict)
            and txn_result.get("verdict") == verification.VERDICT_GREEN
            and isinstance(readiness, dict)
            and readiness.get("state") == "verified"):
        return False
    status = state_store.read_json_tolerant(status_path) or {}
    entries = (status.get("result") or {}).get("verification")
    if summary_path is None and isinstance(status_path, str) \
            and status_path.endswith("builder.status.json"):
        summary_path = (status_path[:-len("builder.status.json")]
                        + "builder.summary.md")
    summary_missing = bool(summary_path) and not os.path.exists(summary_path)
    if not isinstance(entries, list) or not entries or summary_missing:
        return True
    manifest = (txn_result.get("snapshot") or {}).get("manifest_digest")
    for entry in entries:
        if not isinstance(entry, dict):
            return True
        if entry.get("ok") is not True:
            # A green receipt with agent prose claiming anything but a pass
            # (False, None, missing) is a disagreement (i) or incomplete (ii).
            return True
        source = entry.get("source_manifest")
        if source and manifest and source != manifest:
            return True
    return False


def _latest_verification_disposition(session_uuid, transaction_id):
    """The latest sidecar disposition value for one transaction id, or None.
    The sidecar is the reconciled read-through cache of the
    `verification.disposition` trace events (D-0001); render surfaces read it
    rather than replaying the trace."""
    if not (session_uuid and transaction_id):
        return None
    entry = state_store.read_verification_dispositions(
        session_uuid).get(transaction_id)
    return (entry or {}).get("disposition")


def _emit_verification_disposition(session_uuid, trace, transaction_id,
                                   disposition, review_round=None,
                                   reviewed_manifest_digest=None):
    """Record ONE review disposition for an owned transaction (D-0001):
    PRIMARY the `verification.disposition` trace event, PLUS the reconciled
    sidecar entry written at the same moment, PLUS an in-place update of the
    current-receipt pointer's own `disposition` field when the pointer names
    this transaction (so a same-process render sees the new value without a
    re-join, D-0002)."""
    if not (session_uuid and transaction_id
            and disposition in verification.DISPOSITIONS):
        return
    if trace:
        trace.event("verification.disposition",
                    transaction_id=transaction_id, disposition=disposition,
                    review_round=review_round,
                    reviewed_manifest_digest=reviewed_manifest_digest)
    state_store.write_verification_disposition(session_uuid, {
        "transaction_id": transaction_id, "disposition": disposition,
        "review_round": review_round,
        "reviewed_manifest_digest": reviewed_manifest_digest})
    pointer = state_store.read_current_receipt_pointer(session_uuid)
    if isinstance(pointer, dict) \
            and pointer.get("transaction_id") == transaction_id \
            and pointer.get("disposition") != disposition:
        state_store.write_current_receipt_pointer(
            session_uuid, dict(pointer, disposition=disposition))


def _update_receipt_pointer_for_readiness(session_uuid, role, round_index,
                                          trace, txn_result, readiness,
                                          status_path, summary_path=None):
    """Bind the promotion to its owned receipt (D-0002) at the builder
    ready_for_review transition.

    GREEN + bound: any prior pointer still `pending_review` for a DIFFERENT
    transaction means that transaction's candidate was abandoned — it is
    recorded `rejected` (D-0005) — then the new pointer is written carrying
    every overlay field plus the ONCE-computed contradiction flag (D-0008).

    RED/UNVERIFIED transaction: recorded `rejected` immediately (a red or
    unverified transaction can never later be accepted). A GREEN transaction
    whose candidate already moved is left untouched — single-flight reuse can
    still bind it to a later, identical promotion.

    RE-BINDING THE SAME transaction id (the single-flight reuse case, D-0006):
    the candidate is genuinely up for review again, so a disposition recorded
    in an EARLIER round is stale. Resetting only the pointer file would leave
    the trace/sidecar — which every render-time join and the gate-acceptance
    guard actually read (D-0001/D-0002) — holding the earlier round's value, so
    the reset is emitted as a REAL `pending_review` disposition event rather
    than patched into pointer.json alone.
    """
    if not (session_uuid and isinstance(txn_result, dict)
            and isinstance(readiness, dict)):
        return None
    transaction_id = txn_result.get("transaction_id")
    if not transaction_id:
        return None
    if readiness.get("state") != "verified":
        if txn_result.get("verdict") != verification.VERDICT_GREEN:
            _emit_verification_disposition(
                session_uuid, trace, transaction_id,
                verification.DISPOSITION_REJECTED,
                reviewed_manifest_digest=(
                    txn_result.get("snapshot") or {}).get("manifest_digest"))
        return None
    prior = state_store.read_current_receipt_pointer(session_uuid)
    if isinstance(prior, dict) and prior.get("transaction_id") \
            and prior.get("transaction_id") != transaction_id \
            and (prior.get("disposition")
                 or verification.DISPOSITION_PENDING_REVIEW) == \
            verification.DISPOSITION_PENDING_REVIEW:
        _emit_verification_disposition(
            session_uuid, trace, prior["transaction_id"],
            verification.DISPOSITION_REJECTED,
            review_round=prior.get("review_round"),
            reviewed_manifest_digest=prior.get("manifest_digest"))
    pointer = {
        "transaction_id": transaction_id,
        "receipt_path": state_store.verification_result_path_for(
            session_uuid, transaction_id),
        "manifest_digest": (txn_result.get("snapshot") or {}).get(
            "manifest_digest"),
        "index_digest": (txn_result.get("snapshot") or {}).get("index_digest"),
        "verdict": txn_result.get("verdict"),
        "final_suite_label": txn_result.get("final_suite_label"),
        "final_suite_binding": txn_result.get("final_suite_binding"),
        "command_count": len(txn_result.get("attempts") or []),
        "review_round": state_store.current_phase_round(
            session_uuid, "building", role, default=round_index),
        "disposition": verification.DISPOSITION_PENDING_REVIEW,
        "contradiction": bool(_verification_contradiction(
            session_uuid, txn_result, readiness, status_path,
            summary_path=summary_path)),
    }
    state_store.write_current_receipt_pointer(session_uuid, pointer)
    stale = _latest_verification_disposition(session_uuid, transaction_id)
    if stale and stale != verification.DISPOSITION_PENDING_REVIEW:
        # Emitted AFTER the pointer write, so the in-place pointer patch inside
        # the emitter is a no-op and the trace + sidecar are what get corrected.
        _emit_verification_disposition(
            session_uuid, trace, transaction_id,
            verification.DISPOSITION_PENDING_REVIEW,
            review_round=pointer["review_round"],
            reviewed_manifest_digest=pointer["manifest_digest"])
    return pointer


def verification_overlay(pointer, disposition=None):
    """THE ONE overlay renderer (D-0003): a dict of content-free tokens derived
    from the current-receipt pointer (which itself carries only owned state —
    never a byte of agent prose). `disposition` is the render-time join onto
    the latest disposition known for the transaction (D-0002); absent, the
    pointer's own field is used. Returns None when there is no bound receipt."""
    if not isinstance(pointer, dict) or not pointer.get("transaction_id"):
        return None
    return {
        "txn_id": pointer.get("transaction_id"),
        "manifest_digest": pointer.get("manifest_digest"),
        "index_digest": pointer.get("index_digest"),
        "verdict": pointer.get("verdict"),
        "final_suite_label": pointer.get("final_suite_label"),
        "final_suite_binding": pointer.get("final_suite_binding"),
        "command_count": pointer.get("command_count"),
        "disposition": (disposition or pointer.get("disposition")
                        or verification.DISPOSITION_PENDING_REVIEW),
        "contradiction": bool(pointer.get("contradiction")),
    }


def _current_verification_overlay(session_uuid):
    """`(overlay, pointer)` for the CURRENT bound receipt, or `(None, None)`.
    The disposition is joined at render time from the sidecar so a resumed
    reviewer edge mid-loop shows the CURRENT value rather than hardcoding
    `pending_review` (D-0002)."""
    pointer = state_store.read_current_receipt_pointer(session_uuid)
    if not isinstance(pointer, dict) or not pointer.get("transaction_id"):
        return None, None
    disposition = _latest_verification_disposition(
        session_uuid, pointer["transaction_id"])
    return verification_overlay(pointer, disposition=disposition), pointer


def render_verification_overlay_block(overlay, receipt_path=None,
                                      agent_status_path=None):
    """The human-gate banner block (UX-021): the owned facts first, the
    agent-authored verification prose named SEPARATELY as self-reported, and a
    visible WARNING line when the contradiction flag is set. Empty string when
    no overlay binds (the legacy no-transaction gate is unchanged)."""
    if not overlay:
        return ""
    lines = ["", "Owned verification (orchestrator-derived):"]
    lines.append("  transaction %s  verdict=%s  final_suite=%s (%s)"
                 % (overlay.get("txn_id"), overlay.get("verdict"),
                    overlay.get("final_suite_label"),
                    overlay.get("final_suite_binding")))
    lines.append("  manifest=%s  index=%s  commands=%s  disposition=%s"
                 % (str(overlay.get("manifest_digest"))[:12],
                    str(overlay.get("index_digest"))[:12],
                    overlay.get("command_count"), overlay.get("disposition")))
    if receipt_path:
        lines.append("  receipt → %s" % receipt_path)
    if agent_status_path:
        lines.append("Agent-reported verification (self-reported prose — the "
                     "owned receipt above is authoritative): see "
                     "result.verification in %s" % agent_status_path)
    if overlay.get("contradiction"):
        lines.append("  WARNING: the builder's own verification prose is "
                     "missing or disagrees with this receipt — trust the "
                     "receipt, not the prose.")
    return "\n".join(lines)


def _classify_blocking_verification_challenges(verdict, pointer):
    """D-0004 citation validation: classify a revise verdict's BLOCKING
    corrective findings against the current-receipt pointer's owned state.

    Returns `(blocking, defeated)`: every blocking finding, and the subset
    that are DEFEATED verification challenges — either UNCITED (the
    `verification_challenge` field carries no transaction id) or CONTRADICTED
    (it cites a transaction id the owned receipt contradicts). A blocking
    finding with no `verification_challenge` field is a NON-verification
    finding; a challenge citing the bound receipt's own transaction id is
    VALIDLY CITED — neither is defeated, and either keeps the full reopen
    power of a normal revise."""
    typed = verdict.get("corrective_findings") if isinstance(
        verdict, dict) else None
    findings = [f for f in (typed or []) if isinstance(f, dict)]
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    defeated = []
    for finding in blocking:
        challenge = finding.get("verification_challenge")
        if not isinstance(challenge, dict):
            continue
        cited = challenge.get("transaction_id")
        if not cited or cited != (pointer or {}).get("transaction_id"):
            defeated.append(finding)
    return blocking, defeated


def _verdict_with_superseded_challenges(verdict, defeated, pointer):
    """A copy of the verdict in which each defeated verification challenge is
    marked `closure=superseded` + `superseded_by_transaction` — the FINDING is
    the thing superseded (it stays on the ledger, never erased); the bound
    transaction SURVIVES as `pending_review` (D-0004)."""
    defeated_ids = {id(f) for f in defeated}
    typed = []
    for finding in verdict.get("corrective_findings") or []:
        if isinstance(finding, dict) and id(finding) in defeated_ids:
            finding = dict(finding, closure="superseded",
                           superseded_by_transaction=pointer.get(
                               "transaction_id"))
        typed.append(finding)
    return dict(verdict, corrective_findings=typed)


def _accepted_manifest_matches(pointer, repo=None):
    """The A5/D-0005 equality check: the candidate being accepted RIGHT NOW
    must be IDENTICAL to the candidate the transaction's snapshot verified —
    computed with the ONE canonical algorithm (fail-closed: an unreadable git
    state is not equal to anything)."""
    claimed, claimed_index = verification.current_candidate_identity(
        repo or os.getcwd())
    return (claimed is not None
            and claimed == (pointer or {}).get("manifest_digest")
            and claimed_index is not None
            and claimed_index == (pointer or {}).get("index_digest"))


def _grant_gate_acceptance(session_uuid, trace):
    """The D-0004/D-0005 gate-outcome grant at the building-phase user gate:
    an explicit human approve OR a headless auto-approve accepts the bound
    transaction — but ONLY while it is still `pending_review` (a reviewer that
    already judged it, either way, is not second-guessed by the gate) and only
    while the accepted candidate manifest still equals the receipt's captured
    manifest. A manifest mismatch means the receipt's candidate was abandoned
    before acceptance, which is `rejected`, never `accepted`."""
    if not session_uuid:
        return
    pointer = state_store.read_current_receipt_pointer(session_uuid)
    if not isinstance(pointer, dict) or not pointer.get("transaction_id"):
        return
    transaction_id = pointer["transaction_id"]
    current = (_latest_verification_disposition(session_uuid, transaction_id)
               or pointer.get("disposition")
               or verification.DISPOSITION_PENDING_REVIEW)
    if current != verification.DISPOSITION_PENDING_REVIEW:
        return
    disposition = (verification.DISPOSITION_ACCEPTED
                   if _accepted_manifest_matches(pointer)
                   else verification.DISPOSITION_REJECTED)
    _emit_verification_disposition(
        session_uuid, trace, transaction_id, disposition,
        review_round=pointer.get("review_round"),
        reviewed_manifest_digest=pointer.get("manifest_digest"))


def record_milestone(trace, role, milestone_phase, round_index=None):
    """Emit one append-only builder milestone (editing / verification / repair).

    The marker is content-free — a phase name and a timestamp — and the spans
    between markers are what let a turn's cost be partitioned by what the
    builder was actually doing, rather than reported as one undifferentiated
    lump."""
    if not trace or milestone_phase not in measure.MILESTONE_PHASES:
        return None
    return trace.event("role.milestone", role=role,
                       milestone_phase=milestone_phase, round=round_index)


def _rotate_evidence_chain(session_uuid, seat, review_path, artifact_path,
                           phase, round_index):
    """Freeze this round's evidence and make it the next round's prior.

    Returns `{verdict_path, artifact_path, prior}` where `prior` is what the
    chain held BEFORE this call — the genuinely immediately-preceding round.
    Called for every validated round regardless of whether that round is
    selected for scoring, because "what came just before" is a fact about the
    work and not a consequence of the sampling policy.

    Persisted rather than held in a closure, so a resume keeps the chain.
    """
    prior = state_store.read_evidence_chain(session_uuid, seat)
    verdict_path = _freeze_round_evidence(session_uuid, review_path, phase,
                                          round_index, seat)
    frozen_artifact = _freeze_round_evidence(
        session_uuid, artifact_path, phase, round_index, "%s-reviewed" % seat)
    if verdict_path or frozen_artifact:
        state_store.write_evidence_chain(session_uuid, seat, {
            "verdict_path": verdict_path, "artifact_path": frozen_artifact,
            "round": round_index})
    return {"verdict_path": verdict_path, "artifact_path": frozen_artifact,
            "prior": prior}


def _chain_artifacts(frozen_verdict, frozen_artifact, prior, review_path,
                     artifact_path, verdict_role, artifact_role):
    """Assemble P6's four-part evidence chain as sealable descriptors.

    Current verdict, current artifact revision, prior verdict, prior artifact
    revision. A part that does not exist is simply absent — round 1 genuinely
    has no prior, and `not_applicable` is the correct score there. What must not
    happen is a part existing and being left out, which is what made
    responsiveness unobservable in every round.
    """
    out = []
    if frozen_verdict or review_path:
        out.append({"path": frozen_verdict or os.path.abspath(review_path),
                    "label": "reviewer verdict + findings (JSON)",
                    "role": verdict_role})
    if frozen_artifact or artifact_path:
        out.append({"path": (frozen_artifact or os.path.abspath(artifact_path)),
                    "label": "the artifact revision under review",
                    "role": artifact_role})
    prior = prior if isinstance(prior, dict) else {}
    if prior.get("verdict_path"):
        out.append({"path": prior["verdict_path"],
                    "label": "prior round verdict (the revision reviewed then)",
                    "role": verdict_role, "prior_round": prior.get("round")})
    if prior.get("artifact_path"):
        out.append({"path": prior["artifact_path"],
                    "label": "prior round artifact revision",
                    "role": artifact_role, "prior_round": prior.get("round")})
    return out


def _freeze_round_evidence(session_uuid, path, phase, round_index, label):
    """Copy one round's evidence to an IMMUTABLE per-round revision file.

    Verdict and artifact files are overwritten every round, so a sealed digest
    of their live path describes bytes that will not be there when the entry is
    drained. Freezing gives each round its own revision under
    `evidence/<phase>-r<n>-<label>.json`, so the seal stays true and the prior
    round's evidence is genuinely available later — which is what P6's chain
    needs to mean anything.

    Returns the frozen path, or None when there is nothing to freeze. Never
    raises: evidence that cannot be frozen falls back to the live path, which is
    weaker but still honest, because the seal will then correctly report it as
    changed rather than pretending otherwise.
    """
    if not (session_uuid and path and os.path.exists(path)):
        return None
    try:
        target_dir = os.path.join(state_store.session_assets_dir(session_uuid),
                                  "evidence")
        os.makedirs(target_dir, exist_ok=True)
        with open(path, "rb") as src:
            raw = src.read()
        # The revision name includes the CONTENT DIGEST, so it is collision-free
        # across resumes. Naming by phase/round/role alone was not: `review_
        # rounds` restarts at 0 in every fresh `_role_loop`, so the first round
        # after a resume reused the pre-resume `-r1-` file and sealed STALE
        # bytes as the current evidence. Content-addressing makes re-freezing
        # identical bytes a no-op and different bytes a different revision,
        # which is what "immutable revision" has to mean.
        digest = hashlib.sha256(raw).hexdigest()[:12]
        base = os.path.basename(path)
        target = os.path.join(
            target_dir, "%s-r%s-%s-%s-%s" % (phase or "phase", round_index,
                                             label or "role", digest, base))
        if os.path.exists(target):
            # Same bytes already frozen: nothing to rewrite, and the seal that
            # describes them stays true.
            return target
        tmp = target + ".tmp"
        with open(tmp, "wb") as dst:
            dst.write(raw)
        os.replace(tmp, target)
        return target
    except OSError:
        return None


def _validated_verdict_file(path, raw):
    """Whether a just-written verdict file is actually usable.

    An artifact that exists but does not parse is not evidence, and sealing it
    as though it were is how an empty or half-written file came to be scored as
    content."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return False
    return isinstance(data, dict)


def _legacy_make_evaluate_fn(role, reviewer_role, phase, scratch_path,
                             scores_path, session_uuid, intel_path=None,
                             planning_epoch=None, consumed_upstream=None,
                             trace=None, intel_md_path=None,
                             context_revision=None, review_path=None):
    """The in-session evaluation closure, retained ONLY as the prompt/spec
    builder the isolated evaluator reuses at drain time. It is no longer wired
    into any role loop."""
    if not (scratch_path and scores_path and session_uuid):
        return None
    if consumed_upstream is None:
        consumed_upstream = _scout_consumed_upstream(
            intel_path, planning_epoch, intel_md_path)
    consumed_done = {"done": consumed_upstream is None}

    def evaluate_fn(session, verdict, round_index):
        # The scratch is per-turn output, not durable state: clear any prior
        # round's file (and its accounting sidecar) BEFORE the send so a turn
        # that writes nothing yields 'no entry', never a re-read of the
        # previous round's scores.
        _clear_eval_scratch(scratch_path, role, trace=trace)
        if review_path:
            verdict_art = {"label": "reviewer verdict + findings (JSON)",
                           "path": os.path.abspath(review_path), "kind": "json",
                           "source": "verdict"}
        else:
            verdict_art = _handback_payload_artifact(
                json.dumps(verdict or {}, indent=2, sort_keys=True),
                label="reviewer verdict + findings (JSON)", source="verdict")
            verdict_art["kind"] = "json"
        specs = [{
            "evaluatee": reviewer_role,
            "criteria": EVAL_CRITERIA[(role, reviewer_role)],
            "artifact_block": handoff.render_handoff(
                "eval->reviewer_verdict", artifacts=[verdict_art]),
            "context": "review-round",
        }]
        # The consumed-upstream bundle rides only the FIRST eval turn of the
        # phase (round_index == 1): an artifact that appears mid-cycle waits
        # for the next round-1 turn. Once per phase survives a resume/restart:
        # the in-memory flag only covers this closure, so the aggregate itself
        # is the durable record — scoped by the phase epoch, which bumps on
        # every phase transition, so a hand-back round trip (a new phase) is
        # evaluated again even when the re-approved artifact is byte-identical.
        if not consumed_done["done"]:
            spec = _consumed_upstream_spec(
                consumed_upstream, scores_path, role, round_index)
            if spec == "deduped":
                consumed_done["done"] = True
            elif spec:
                specs.append(spec)
        if trace:
            trace.event("eval.request", evaluator=role,
                        evaluatees=[s["evaluatee"] for s in specs],
                        phase=phase, round=round_index)
        prompt = assemble_eval_prompt(role, scratch_path, specs)
        # Per-turn accounting (#1/D11, SC5): the eval's artifact descriptors are
        # taken from the SAME handoff objects that built the prompt — every
        # spec's `artifact_block` is a HandoffBlock, so its `.descriptors` (the
        # verdict file path-first, plus the consumed-upstream files when they
        # ride this turn) are aggregated here rather than re-read/re-inferred.
        eval_artifacts = _eval_artifact_descriptors(specs)
        eval_turn_id = str(uuid.uuid4())
        with _muted_session(session):
            send_result = _send(session, _eval_delivery(prompt, specs), meta={
                "prompt_kind": "eval", "fresh": False, "resume": True,
                "phase": phase, "round": round_index,
                "context_revision": context_revision,
                "artifacts": eval_artifacts,
                "eval_turn_id": eval_turn_id})
        # Per-eval accounting (traceability): who evaluated (tool+model+session
        # id), what the turn cost (usage/duration), and which verdict was under
        # evaluation — stamped onto every aggregated entry below.
        _write_eval_turn_sidecar(scratch_path, session, send_result,
                                 eval_turn_id, len(specs), verdict=verdict)
        if len(specs) > 1:
            consumed_done["done"] = True
        _aggregate_eval(
            scratch_path, scores_path, session_uuid, role, phase, round_index,
            {s["evaluatee"]: _eval_spec_stamp(s) for s in specs}, trace=trace)

    return evaluate_fn


EVALUATOR_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "evaluator.md")


def drain_evaluations(session_uuid, config=None, trace=None, io_out=None,
                      session_factory=None, at=None, closed_phases=None,
                      effective_policy=None, mode="drain"):
    """Drain the durable evaluation queue through ISOLATED evaluator sessions.

    Called at every phase transition, at session end, and once at session start
    (P12) — the same points that rebuild the record and reconcile the ledger.
    The session-start drain is what makes a crash cost a delay rather than the
    scores: entries a previous process left pending are still on disk, with
    their ORIGINAL sealed digests.

    THIS IS THE ONE SEAM EVERY DRAIN GOES THROUGH, which is why the effective
    policy is resolved HERE rather than in per-phase branches: one check covers
    startup, phase end, session end and recovery, and there is no fourth path
    that can quietly keep spending under `off`. The policy that governs is the
    one in force NOW, not the one stored on an entry when it was enqueued —
    that is what makes turning evaluation off take effect on historical work.

    `mode='preview'` is a read-only projection: it scores nothing and writes no
    marker, and is what the foreground transition consults to decide whether a
    drain is about to block the run.

    Each entry gets a FRESH session that has never touched the work, running
    `roles/evaluator.md` on the SAME controller and model as the seat it
    occupies (P5) — collapsing evaluations onto one controller would break
    comparability with the sessions already recorded.

    Every failure here degrades the measurement and never the run.
    """
    if effective_policy is None:
        effective_policy = state_store.DEFAULT_EVALUATION_POLICY
    queue_path = state_store.evaluation_queue_path_for(session_uuid)
    # Every key the renderer and the gate read, present and zeroed: a missing
    # queue is a quiet queue, not a blank screen.
    if not os.path.exists(queue_path):
        return evaluation.empty_summary(policy=effective_policy)

    # Only a phase the orchestrator has actually left is closed. A recovery
    # drain at session start knows of none, so its final-round candidates are
    # HELD rather than resolved — otherwise a crash mid-phase would score a
    # round that later turns out not to be the final one.
    closed = set(closed_phases or ())
    phase_closed = (lambda phase: phase in closed) if closed else None

    if mode == "preview":
        return evaluation.preview(queue_path, phase_closed=phase_closed,
                                  effective_policy=effective_policy)

    if trace:
        trace.event("eval.drain.start", session_uuid=session_uuid, at=at,
                    policy=effective_policy)

    def _score(entry, verification):
        return _score_queued_entry(entry, verification, session_uuid,
                                   config=config, trace=trace, io_out=io_out,
                                   session_factory=session_factory)

    def _on_transition(event):
        if trace:
            trace.event("eval.entry.lifecycle",
                        entry_id=event.get("entry_id"),
                        from_state=event.get("from_state"),
                        to_state=event.get("to_state"),
                        attempt=event.get("attempt"),
                        limit=event.get("limit"),
                        error_class=event.get("error_class"))

    summary = evaluation.drain(
        queue_path, _score, phase_closed=phase_closed,
        effective_policy=effective_policy, on_transition=_on_transition)
    if trace:
        trace.event("eval.drain.end", session_uuid=session_uuid, at=at,
                    policy=effective_policy,
                    drained=summary.get("drained"),
                    failed=summary.get("failed"),
                    unverifiable=summary.get("unverifiable"),
                    superseded=summary.get("superseded"),
                    held=summary.get("held"),
                    terminal=summary.get("terminal"),
                    retired=summary.get("retired"),
                    pending=summary.get("pending"))
    return summary


def run_evaluation_transition(session_uuid, effective_policy, config=None,
                              trace=None, io_in=None, io_out=None, at=None,
                              closed_phases=None, headless=False, ask_fn=None,
                              session_factory=None):
    """The evaluation drain at one boundary, and its visible foreground state.

    DELIBERATELY NOT INSIDE the best-effort measurement block that surrounds its
    caller. Two reasons, both load-bearing: a blocking interactive prompt must
    not sit inside a handler documented as "may never degrade the run", and its
    exceptions must not be silently eaten by a swallow-all meant for
    measurement.

    A BLOCKING GATE OPENS ONLY WHEN THE DRAIN IS ACTUALLY GOING TO RUN WORK:
    the policy is not `off` AND there is something scoreable or something
    already terminal to retry. Everything else — `off`, an empty queue, a queue
    with nothing left to do — stays exactly as quiet as it was, because a prompt
    at a boundary that was never going to block is just noise. That is what
    preserves deferred evaluation: an ordinary reviewer round never starts
    waiting for scoring.

    THE NON-BLOCKING BRANCH STILL DRAINS, and that is not an oversight. With
    nothing scoreable this is pure reconciliation rather than scoring: it is
    what records the policy-off holds, and what RETIRES superseded candidates.
    Returning early would mean a boundary whose only work is a retire set never
    retires anything — and session end is exactly such a boundary, since it
    closes every phase. Those entries would be re-partitioned and re-counted at
    every boundary forever, which is the lifecycle trap this exists to close.
    """
    def _drain(policy_value):
        return drain_evaluations(
            session_uuid, config=config, trace=trace, at=at,
            closed_phases=closed_phases, effective_policy=policy_value,
            session_factory=session_factory)

    summary = drain_evaluations(
        session_uuid, config=config, trace=trace, at=at,
        closed_phases=closed_phases, effective_policy=effective_policy,
        mode="preview")
    blocking = (effective_policy != "off"
                and (summary.get("scoreable", 0)
                     or summary.get("terminal_existing", 0)))
    if not blocking:
        result = _drain(effective_policy)
        if any(result.get(key) for key in
               ("pending_running", "drained_total", "held", "terminal_total")):
            ui.render_drain_state(io_out, effective_policy, result,
                                  blocking=False)
        return result
    # A drain that WILL block gets a distinct visible state and bounded, safe
    # control. Headless and non-TTY runs get identical bookkeeping and identical
    # counts with NO prompt: they continue.
    #
    # THE NON-TTY RULE IS LOAD-BEARING, not a nicety. Off a real terminal there
    # is nobody to answer, so opening the gate would hand the question to
    # whatever happens to be on stdin — a scripted run, a pipe, or a test
    # harness — and wait. A drain that used to be silent must not be able to
    # stall a non-interactive run. An INJECTED `ask_fn` is the deliberate
    # exception: that is a caller supplying the answer itself.
    action = "continue"
    interactive = (ui.is_real_terminal(io_in)
                   and ui.is_real_terminal(io_out))
    if not headless and (ask_fn is not None or interactive):
        try:
            action = ui.drain_gate(io_in, io_out, effective_policy, summary,
                                   ask_fn=ask_fn)
        except (KeyboardInterrupt, EOFError):
            # Walking away is `end`: nothing scored, the queue preserved.
            action = "end"
        except Exception:  # noqa: BLE001
            # A NAMED failure of the render/prompt only — never the blanket
            # measurement swallow — falling back to the non-interactive
            # behavior rather than losing the drain entirely.
            if trace:
                trace.event("eval.gate.error", at=at)
            action = "continue"
    if action == "end":
        # Nothing scored, nothing marked, every entry preserved.
        return summary
    if action == "hold":
        queue_path = state_store.evaluation_queue_path_for(session_uuid)
        records = evaluation.read_queue(queue_path)
        for entry in evaluation.pending_entries(queue_path):
            entry_id = entry.get("entry_id")
            evaluation.mark_held(
                queue_path, entry_id, held_reason="user_hold",
                fold=evaluation.read_entry_lifecycle(records, entry_id))
        # Recount from the durable state WITHOUT scoring — a preview, not a
        # drain, so holding cannot score the very work it just held.
        held_summary = drain_evaluations(
            session_uuid, config=config, trace=trace, at=at,
            closed_phases=closed_phases, effective_policy=effective_policy,
            mode="preview")
        ui.render_drain_state(io_out, effective_policy, held_summary,
                              blocking=False)
        return held_summary
    if action == "retry":
        retry_terminal_evaluations(session_uuid)
    result = _drain(effective_policy)
    ui.render_drain_state(io_out, effective_policy, result, blocking=False)
    return result


def retry_terminal_evaluations(session_uuid):
    """Explicitly reopen every terminal entry in the queue. Returns the count.

    Terminal work is NEVER released by a policy change or by another drain
    coming round again — only by this, a deliberate user action. The retry is
    LINKED to what came before (`prior_attempt_ref` names the terminal marker it
    reopened) rather than overwriting it, so the earlier attempts stay readable:
    a retry that erased its own history would make the second failure look like
    the first.
    """
    queue_path = state_store.evaluation_queue_path_for(session_uuid)
    if not os.path.exists(queue_path):
        return 0
    records = evaluation.read_queue(queue_path)
    reopened = 0
    for entry_id in {rec.get("entry_id") for rec in records
                     if rec.get("entry_id")}:
        fold = evaluation.read_entry_lifecycle(records, entry_id)
        if fold.get("state") not in ("terminal", "failed_permanent"):
            continue
        history = fold.get("transition_history") or []
        if evaluation.mark_retried(queue_path, entry_id,
                                   prior_attempt_ref=(history[-1]
                                                      if history else None)):
            reopened += 1
    return reopened


def _score_queued_entry(entry, verification, session_uuid, config=None,
                        trace=None, io_out=None, session_factory=None):
    """Run one queued evaluation in an isolated session and aggregate it.

    `verification` is the re-check of the sealed envelope. When it reports
    `changed`, the entry is still SCORED but every resulting entry is stamped
    `verification_state='changed'` so aggregation treats it as `unverifiable`
    and excludes it. Re-hashing the current file instead would make every score
    verifiable by construction and prove nothing.

    RETURNS A CLASSIFIED OUTCOME, `{"ok": bool, "error_class": str|None}`, not
    a bare bool. Every failure path used to collapse into one undifferentiated
    `False`, which is why a retry could not be bounded: nothing on disk could
    say whether trying again might ever help. The classes map to real paths —

      malformed_entry   the entry cannot be run at all (no scratch/scores path,
                        no controller in its identity snapshot, or no session).
                        NO controller turn is ever started on these.
      malformed_output  the evaluator ran but produced nothing aggregatable.
      transient         a timeout or a dropped connection: worth one retry.
      permanent         any other exception; a retry cannot change it.
    """
    scratch_path = entry.get("scratch_path")
    scores_path = entry.get("scores_path")
    if not (scratch_path and scores_path):
        return {"ok": False, "error_class": "malformed_entry"}
    seat = entry.get("evaluator_seat")
    identity = entry.get("identity_snapshot") or {}
    controller = identity.get("tool")
    if not controller:
        # Without the seat's controller there is no comparable evaluation to
        # run. Reported as a malformed entry rather than run on a substitute
        # controller, which would silently change what is compared.
        if trace:
            trace.event("eval.drain.skipped", entry_id=entry.get("entry_id"),
                        reason="unknown_controller", role=seat)
        return {"ok": False, "error_class": "malformed_entry"}
    _clear_eval_scratch(scratch_path, seat, trace=trace)
    # THE EVALUATOR MUST SEE WHAT WAS SEALED. Building this block from
    # `review_path` alone meant the sealed chain — the frozen current revision
    # AND the prior round's — was hashed into the envelope and then never shown,
    # so "responsiveness to feedback" had nothing to be responsive to and the
    # seal protected evidence the evaluator never received. The envelope is the
    # source of the prompt, not just of the digests.
    artifacts = _envelope_artifacts(entry)
    if not artifacts and entry.get("review_path"):
        artifacts.append({"label": "reviewer verdict + findings (JSON)",
                          "path": entry["review_path"], "kind": "json",
                          "source": "verdict"})
    specs = [{
        "evaluatee": entry.get("evaluatee"),
        "criteria": entry.get("criteria") or [],
        "artifact_block": handoff.render_handoff(
            "eval->reviewer_verdict", artifacts=artifacts),
        "context": "review-round",
        "phase": entry.get("phase"),
        "round": entry.get("round"),
    }]
    prompt = assemble_eval_prompt(seat, scratch_path, specs)
    eval_turn_id = str(uuid.uuid4())
    session = None
    # Taken BEFORE the factory call so a failed attempt can report the time it
    # actually took. See the failure path below.
    started_at = time.monotonic()
    try:
        if session_factory is None:
            session = _isolated_evaluator_session(entry, identity, config=config,
                                                  trace=trace, io_out=io_out,
                                                  session_uuid=session_uuid)
        else:
            session = session_factory(entry, identity, config=config, trace=trace,
                                      io_out=io_out)
        if session is None:
            return {"ok": False, "error_class": "malformed_entry"}
        if trace:
            trace.event("eval.turn.start", role="evaluator", seat=seat,
                        phase=entry.get("phase"), round=entry.get("round"),
                        entry_id=entry.get("entry_id"),
                        **trace_store.work_meta(eval_turn_id, "evaluation"))
        with _muted_session(session):
            send_result = _send(session, _eval_delivery(prompt, specs), meta={
                "prompt_kind": "eval", "fresh": True, "resume": False,
                "phase": entry.get("phase"), "round": entry.get("round"),
                "work_class": "evaluation",
                "artifacts": _eval_artifact_descriptors(specs),
                "eval_turn_id": eval_turn_id})
        _write_eval_turn_sidecar(scratch_path, session, send_result,
                                 eval_turn_id, len(specs),
                                 verdict={"verdict":
                                          entry.get("reviewed_verdict")})
    except Exception as exc:  # noqa: BLE001 - a failed eval never breaks the run
        error_class = _eval_error_class(exc)
        if trace:
            # A REAL DURATION, not a hardcoded zero. A failed attempt stays in
            # the `failed` cost class — it is not productive work and must never
            # count as an evaluation success — but it is now first-class,
            # bounded and counted work, so reporting the time it consumed as 0
            # would under-report what evaluation actually cost.
            trace.event("eval.turn.end", role="evaluator", result="error",
                        entry_id=entry.get("entry_id"),
                        error_class=error_class,
                        **trace_store.work_meta(
                            eval_turn_id, "failed",
                            duration_ms=int(
                                (time.monotonic() - started_at) * 1000)))
        return {"ok": False, "error_class": error_class}
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
    aggregated = _aggregate_eval(
        scratch_path, scores_path, session_uuid, seat, entry.get("phase"),
        entry.get("round"),
        {s["evaluatee"]: _eval_spec_stamp(s) for s in specs}, trace=trace,
        verification=verification, envelope=entry.get("envelope"),
        eval_work_id=eval_turn_id)
    # The evaluator ran and returned. Nothing aggregatable coming back means it
    # produced missing or unparseable scores — a distinct thing from the run
    # itself failing, and one a retry cannot fix.
    if aggregated:
        return {"ok": True, "error_class": None}
    return {"ok": False, "error_class": "malformed_output"}


# Exception types that mean "the environment blipped" rather than "this cannot
# work". Only these earn a second attempt; everything else is permanent, so a
# retry budget is never spent on a failure that will simply recur.
_TRANSIENT_ERRNOS = frozenset(
    code for code in (getattr(errno, "ECONNRESET", None),
                      getattr(errno, "EPIPE", None)) if code is not None)


def _eval_error_class(exc):
    """Name the failure an evaluation attempt hit, for the retry budget."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "transient"
    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
        return "transient"
    return "permanent"


# How a sealed artifact's role maps onto the handoff transport's slots. The
# prior round's verdict rides the `upstream` slot because that is the slot for
# "an artifact from earlier that this evaluation consumes".
_ENVELOPE_SLOTS = {"verdict": "verdict", "reviewed": "reviewed",
                   "prior": "upstream"}


def _envelope_artifacts(entry):
    """The artifact descriptors for the evaluator prompt, FROM the sealed
    envelope.

    Only artifacts that were actually present and validated at seal time are
    shown: an artifact sealed as absent is not evidence, and putting its path in
    front of an evaluator would invite it to read whatever is there now — which
    is precisely the binding the seal exists to prevent.
    """
    envelope = entry.get("envelope")
    if not isinstance(envelope, dict):
        return []
    out = []
    for sealed in envelope.get("artifacts") or []:
        if not isinstance(sealed, dict) or not sealed.get("path"):
            continue
        if not (sealed.get("present") and sealed.get("validated")):
            continue
        label = sealed.get("label") or os.path.basename(sealed["path"])
        prior = sealed.get("prior_round") is not None or "prior" in label.lower()
        source = "reviewed" if "artifact" in label.lower() else "verdict"
        if prior:
            source = "upstream"
        out.append({
            "label": label,
            "path": sealed["path"],
            "kind": "json" if str(sealed["path"]).endswith(".json")
            else "markdown",
            "source": source,
        })
    return out


def _isolated_evaluator_session(entry, identity, config=None, trace=None,
                                io_out=None, session_uuid=None):
    """A FRESH controller session for one evaluation.

    Fresh is the requirement, not an optimization: an evaluator resumed from a
    role's thread would carry that role's context, which is exactly the
    contamination D2 removes. It also costs more — the cached-context discount
    of reusing a session is lost — which is why evaluation is its own cost class
    and why the policy setting exists.
    """
    controller = identity.get("tool")
    model = identity.get("model")
    effort = identity.get("effort")
    scratch_path = entry.get("scratch_path")
    assets_dir = os.path.dirname(scratch_path) if scratch_path else None
    cfg_obj = config or {}
    if session_uuid:
        try:
            _eval_manifest, _ = _compile_role_manifest(
                role="evaluator", session_uuid=session_uuid,
                work_id="evaluator",
                controller=controller or "claude",
                mode="plan",
                model=model, effort=effort,
                instruction_paths=[EVALUATOR_PROMPT_PATH],
                sessions_dir=assets_dir,
                force_recompile=False)
        except Exception:
            _eval_manifest = {}
        _edec = _decide_and_trace(
            trace, "evaluator", controller or "claude", "evaluator",
            "_isolated_evaluator_session", manifest=_eval_manifest,
            preflight_result=_manifest_preflight_fact(_eval_manifest))
        if _edec["outcome"] == "refuse":
            _emit_dispatch_escalation(trace, "evaluator", "manifest_proven",
                                      "recompile and preflight the manifest",
                                      "session_creation")
            return None
    if controller == "claude":
        return bridge.ClaudeSession(
            EVALUATOR_PROMPT_PATH, "plan", False,
            io_out=io_out or open(os.devnull, "w"), speaker="evaluator",
            trace=trace, internal=True, model=model, effort=effort,
            extra_writable_dir=assets_dir,
            declared_outputs=((scratch_path,) if scratch_path else ()),
            repo_writable=False)
    if controller == "codex":
        return bridge.CodexSession(
            "plan", False, io_out=io_out or open(os.devnull, "w"),
            speaker="evaluator", trace=trace, internal=True, model=model,
            effort=effort, extra_writable_dir=assets_dir,
            declared_outputs=((scratch_path,) if scratch_path else ()),
            repo_writable=False)
    return None


def context_update_block(text, assets_dir=None, revision=None):
    """Wake block (route 13) for any role resuming a CLI session that has not
    acknowledged the current session context revision. Role-agnostic. The
    context text is materialized to a revision-keyed authoritative file and
    referenced by PATH via the shared transport — never inlined."""
    return handoff.render_handoff(
        "context->update",
        artifacts=[_shared_context_artifact(text, assets_dir, revision)])


def assemble_reviewer_resume_context(intel_path, intel_md_path=None,
                                     context_update=None, assets_dir=None,
                                     context_revision=None):
    """Lighter context for a RESUMED reviewer session, delivered FILE-ONLY via
    the shared transport: its thread already holds the role + the prior context,
    so only the updated intel is sent (by path — JSON, and markdown when given).
    When the session context changed since the reviewer last acknowledged it
    (`context_update` is the un-acked context text), a context-update wake block
    referencing the persisted context FILE is prepended. No body is inlined."""
    prefix = None
    if context_update:
        prefix = context_update_block(context_update, assets_dir,
                                      context_revision)
    return handoff.render_handoff(
        "scout->scout-reviewer:review_resume",
        artifacts=_intel_artifacts(intel_path, intel_md_path),
        ctx={"context_update_prefix": prefix} if prefix else None)


def make_scout_reviewer_runner(intel_md_path, trace=None,
                               extra_writable_dir=None):
    """Build the real (non-test) reviewer runner for the scouting phase: a
    `run_reviewer_once` closure carrying the scout-reviewer role, prompt, and
    the dual-artifact (intel JSON + markdown) context assemblers, so the
    scout-reviewer actually RECEIVES both files (the load-bearing invariant
    behind the hash-gate composite, D8). Mirrors `make_planning_advisor_runner`.
    `extra_writable_dir` is the relocated session-assets root, granted to the
    reviewer CLI so its review/eval writes (outside cwd) succeed on the no-yolo
    path."""
    def runner(config, context, selected, intel_path, review_path,
               resume_id=None, on_session=None, context_update=None,
               eval_scratch_path=None, eval_specs=None, surface_io_out=None,
               context_revision=None, session_uuid=None):
        return run_reviewer_once(
            config, context, selected, intel_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            context_revision=context_revision, session_uuid=session_uuid,
            artifact_paths=[intel_path, intel_md_path], phase="scouting",
            reviewer_role=SCOUT_REVIEWER,
            prompt_path=SCOUT_REVIEWER_PROMPT_PATH,
            protected="the scout intel files (JSON and markdown)",
            context_fn=lambda ctx, sel, p, assets_dir=None,
                context_revision=None:
                assemble_reviewer_context(
                    ctx, sel, p, intel_md_path, assets_dir=assets_dir,
                    context_revision=context_revision),
            resume_context_fn=lambda p, context_update=None, assets_dir=None,
                context_revision=None:
                assemble_reviewer_resume_context(
                    p, intel_md_path, context_update=context_update,
                    assets_dir=assets_dir, context_revision=context_revision))
    # See make_planning_advisor_runner: marks a real surface-capable closure.
    runner._coplan_surface_capable = True
    return runner


# --------------------------------------------------------------------------- #
# planner: the single user-facing voice of the planning phase, paired with the  #
# planning-advisor exactly as the scout pairs with the scout-reviewer. The      #
# planner writes TWO artifacts: a plan JSON (machine deliverable and status     #
# channel) and a human-first plan MD (the user's review surface).               #
# --------------------------------------------------------------------------- #


def assemble_planner_brief(plan_json_path, plan_md_path, caveman_available=None):
    """The planner's write-target instruction — its analogue of the scout brief.
    It names BOTH plan artifacts and nothing else."""
    return (
        "Write your plan as TWO files, to exactly these paths:\n"
        "  JSON (machine deliverable + your status channel): %s\n"
        "  Markdown (the user's review surface, small scannable sections): %s\n"
        "Those two plan files are your ONLY write targets. Do not create, edit, "
        "or delete any other file (reading/searching the repo is fine).\n\n%s"
        % (plan_json_path, plan_md_path, caveman_directive(caveman_available))
    )


def assemble_planner_seed(intel_path, context, assets_dir=None,
                          context_revision=None):
    """The fresh planner's situational context (route 3), FILE-ONLY: the approved
    scout intel AND the shared session context, both carried by PATH via the
    shared transport. scout->planner is a cross-role handoff, so the context is
    persisted and referenced by path — never inlined (only the original
    user->scout prompt inlines user text)."""
    artifacts = [_shared_context_artifact(context, assets_dir, context_revision)]
    artifacts.extend(_intel_artifacts(intel_path))
    return handoff.render_handoff("scout->planner:seed", artifacts=artifacts)


def intel_updated_block(intel_path):
    """Wake block (route 3, resume) for a resumed planner after a hand-back round
    trip: the scout re-ran its full cycle and the user approved the UPDATED
    intel. The intel is carried path-first via the shared transport."""
    return handoff.render_handoff(
        "scout->planner:intel_updated", artifacts=_intel_artifacts(intel_path))


def handoff_wake_block(payload, assets_dir=None):
    """Wake block (route 9) for the scout session resumed by a planner hand-back.
    The planner's hand-back payload (free-form authored text) is materialized to
    a file and carried by PATH via the shared transport — never inlined."""
    return handoff.render_handoff(
        "planner->scout:handback_wake",
        artifacts=[_handback_payload_artifact(
            payload, assets_dir, filename="handback.scout.txt",
            label="planner hand-back note")])


def handoff_declined_text():
    """Turn injected into the planner when the user declines the hand-back."""
    return (
        "The user DECLINED the hand-back to the scout. Continue planning with "
        "the current intel; raise anything unresolved with the user directly "
        "and update your plan status as appropriate."
    )


# --------------------------------------------------------------------------- #
# builder: the single user-facing voice of the building phase, paired with the  #
# build-reviewer exactly as the scout pairs with the scout-reviewer and the     #
# planner with the planning-advisor. The builder edits the repository to        #
# execute the approved plan; its status JSON is a status channel + verification  #
# log, NOT a deliverable in itself.                                             #
# --------------------------------------------------------------------------- #


def assemble_builder_brief(build_status_path, build_summary_path=None,
                           caveman_available=None):
    """The builder's status-file instruction. Unlike the scout/planner, the
    builder's write target is the WHOLE REPO (it edits source to execute the
    plan); the status file named here is only its status/verification channel,
    not a write restriction.

    When `build_summary_path` is given, the builder ALSO emits a human-first
    markdown summary at its self-audit (when it marks ready_for_review): the
    user's review surface for the build, consistency-checked by the
    build-reviewer against the working-tree delta. It is a deliverable, not a
    write restriction (the builder still edits the whole repo)."""
    summary_note = ""
    if build_summary_path:
        summary_note = (
            "At your self-audit, when you mark the build ready_for_review, also "
            "write a human-first markdown summary of the build to exactly this "
            "file:\n  %s\n"
            "Cover, in small scannable sections: a TL;DR; the changes by file; "
            "the verification results; any issues & deviations from the plan; "
            "and anything left for the user. Keep it CONSISTENT with the actual "
            "working-tree changes and your status JSON.\n" % build_summary_path
        )
    return (
        "Write and keep current your status as a single JSON object to exactly "
        "this file:\n  %s\n"
        "That status file is your status + verification channel (status, "
        "handoff, and the result.verification log) — NOT a restriction on what "
        "you may edit. You execute the approved plan by editing the repository "
        "itself. Do NOT run any git commit or PR/branch tooling: approval ends "
        "the run and leaves the changes in the working tree for the user.\n%s\n%s"
        % (build_status_path, summary_note,
           caveman_directive(caveman_available))
    )


def assemble_builder_seed(plan_json_path, plan_md_path, context,
                          assets_dir=None, context_revision=None):
    """The fresh builder's situational context (route 6), FILE-ONLY: the approved
    plan (JSON + markdown) AND the shared session context, both carried by PATH
    via the shared transport. planner->builder is a cross-role handoff, so the
    context is persisted and referenced by path — never inlined."""
    artifacts = [_shared_context_artifact(context, assets_dir, context_revision)]
    artifacts.extend(_plan_artifacts(plan_json_path, plan_md_path))
    return handoff.render_handoff("planner->builder:seed", artifacts=artifacts)


def plan_updated_block(plan_json_path, plan_md_path):
    """Wake block (route 6, resume) for a resumed builder after a hand-back round
    trip: the builder handed back to the planner, the planner re-planned, and the
    user approved the UPDATED plan. The plan is carried path-first via the shared
    transport."""
    return handoff.render_handoff(
        "planner->builder:plan_updated",
        artifacts=_plan_artifacts(plan_json_path, plan_md_path))


def plan_handback_wake_block(payload, assets_dir=None):
    """Wake block (route 10) for the planner session resumed by a builder
    hand-back. The builder's hand-back payload (free-form authored text) is
    materialized to a file and carried by PATH via the shared transport."""
    return handoff.render_handoff(
        "builder->planner:handback_wake",
        artifacts=[_handback_payload_artifact(
            payload, assets_dir, filename="handback.planner.txt",
            label="builder hand-back note")])


def handoff_declined_to_planner_text():
    """Turn injected into the builder when the user declines the hand-back."""
    return (
        "The user DECLINED the hand-back to the planner. Continue building with "
        "the current plan; raise anything unresolved with the user directly "
        "and update your build status as appropriate."
    )


def _plan_artifacts(plan_json_path, plan_md_path):
    return [
        {"label": "plan JSON (machine source of truth)",
         "path": plan_json_path, "kind": "json", "source": "plan_json"},
        {"label": "plan markdown (the user's review surface)",
         "path": plan_md_path, "kind": "markdown", "source": "plan_md"},
    ]


def assemble_advisor_context(context, selected, plan_json_path, plan_md_path,
                             intel_path=None, intel_md_path=None,
                             assets_dir=None, context_revision=None):
    """The planning-advisor's situational context (route 4), delivered FILE-ONLY
    via the shared transport: the shared session context (by path), the team
    framing, BOTH planner artifacts to review, AND the approved scout intel
    (JSON + markdown) the plan must cover — every one by path, no body inlined.
    Route 4 is the explicit multi-source edge carrying plan AND intel paths."""
    artifacts = [_shared_context_artifact(context, assets_dir, context_revision)]
    artifacts.extend(_plan_artifacts(plan_json_path, plan_md_path))
    # Route 4 is a MULTI-SOURCE edge: it also carries the approved scout intel
    # paths so the advisor can verify the plan's criteria-coverage against the
    # approved intel.
    if intel_path:
        artifacts.extend(_intel_artifacts(intel_path, intel_md_path))
    return handoff.render_handoff(
        "planner->planning-advisor:review_ctx",
        artifacts=artifacts, facts={"team": list(selected or [])})


def assemble_advisor_resume_context(plan_json_path, plan_md_path,
                                    context_update=None, assets_dir=None,
                                    context_revision=None):
    """Lighter context for a RESUMED planning-advisor session, delivered
    FILE-ONLY via the shared transport: only the updated plan artifacts (by
    path) — plus a context-update wake block referencing the persisted context
    FILE when the session context changed since the advisor last acknowledged
    it. No body is inlined."""
    prefix = None
    if context_update:
        prefix = context_update_block(context_update, assets_dir,
                                      context_revision)
    return handoff.render_handoff(
        "planner->planning-advisor:review_resume",
        artifacts=_plan_artifacts(plan_json_path, plan_md_path),
        facts={"team": []},
        ctx={"context_update_prefix": prefix} if prefix else None)


def make_planning_advisor_runner(plan_md_path, trace=None,
                                 extra_writable_dir=None, intel_path=None,
                                 intel_md_path=None):
    """Build the real (non-test) reviewer runner for the planning phase: a
    `run_reviewer_once` closure carrying the advisor role, prompt, and the
    context assemblers. Route 4 is multi-source: `intel_path`/`intel_md_path`
    are the approved scout intel the advisor also receives (by path) so it can
    verify plan criteria-coverage against the approved intel. `extra_writable_dir`
    is the relocated session-assets root, granted to the advisor CLI so its
    review/eval writes (now outside cwd) succeed on the no-yolo path."""
    def runner(config, context, selected, plan_json_path, review_path,
               resume_id=None, on_session=None, context_update=None,
               eval_scratch_path=None, eval_specs=None, surface_io_out=None,
               context_revision=None, session_uuid=None):
        return run_reviewer_once(
            config, context, selected, plan_json_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            context_revision=context_revision, session_uuid=session_uuid,
            artifact_paths=[plan_json_path, plan_md_path], phase="planning",
            reviewer_role=PLANNING_ADVISOR,
            prompt_path=PLANNING_ADVISOR_PROMPT_PATH,
            protected="the planner's plan files",
            context_fn=lambda ctx, sel, p, assets_dir=None,
                context_revision=None:
                assemble_advisor_context(
                    ctx, sel, p, plan_md_path, intel_path=intel_path,
                    intel_md_path=intel_md_path, assets_dir=assets_dir,
                    context_revision=context_revision),
            resume_context_fn=lambda p, context_update=None, assets_dir=None,
                context_revision=None:
                assemble_advisor_resume_context(
                    p, plan_md_path, context_update=context_update,
                    assets_dir=assets_dir, context_revision=context_revision))
    # Marks this as a real run_reviewer_once closure (vs. a test-injected
    # reviewer_runner) so make_review_fn forwards surface_io_out only to runners
    # that accept it — test runners keep a byte-identical signature.
    runner._coplan_surface_capable = True
    return runner


# --------------------------------------------------------------------------- #
# build-reviewer: a critical reviewer paired with the builder. Invoked          #
# deterministically when the builder sets `ready_for_review`. Unlike the other  #
# paired reviewers, its unit of review is the builder's WORKING-TREE DIFF: the  #
# reviewer runs `git diff` itself (so the snapshot is never stale) and checks   #
# it against the approved plan + the builder's status/verification log.         #
# --------------------------------------------------------------------------- #


def discover_git_roots(base):
    """Discover the NEAREST git roots around `base`, in a DETERMINISTIC order.

    Returns an ordered list of ``{"path": <abs>, "relation": <rel>}`` where
    `relation` is one of ``self|descendant|ancestor|fallback``. Order:

      - `base` is itself a git root -> ``[{base, 'self'}]``;
      - else the nearest git roots BENEATH `base` (descendant scan, pruning at
        the first `.git` on each branch so nested submodules / vendored libs are
        excluded), sorted by path, relation ``descendant``;
      - else the nearest git root ABOVE `base` (walk parents to the first root),
        relation ``ancestor``;
      - else `base` itself as the root, relation ``fallback``.

    Determinism: `base` is abspath-normalized, `os.walk` dirnames are sorted
    in place before descent (so traversal order is filesystem-independent), and
    descendant roots are returned sorted by path. All returned paths are
    absolute. Tolerant by design — any error degrades to the fallback."""
    def is_root(d):
        return os.path.exists(os.path.join(d, ".git"))

    try:
        base = os.path.abspath(base)
        if is_root(base):
            return [{"path": base, "relation": "self"}]

        # Nearest descendant roots: prune at the first .git on each branch so a
        # root nested inside another root (submodule / vendored lib) is excluded.
        descendants = []
        for dirpath, dirnames, _filenames in os.walk(base):
            dirnames.sort()  # deterministic, filesystem-independent descent
            if dirpath == base:
                continue
            if is_root(dirpath):
                descendants.append(dirpath)
                dirnames[:] = []  # do not descend INTO a found root
        if descendants:
            return [{"path": p, "relation": "descendant"}
                    for p in sorted(descendants)]

        # Nearest ancestor root: walk parents to the first root.
        cur = base
        while True:
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            if is_root(parent):
                return [{"path": parent, "relation": "ancestor"}]
            cur = parent

        return [{"path": base, "relation": "fallback"}]
    except Exception:  # noqa: BLE001 - discovery degrades to fallback, never blocks
        return [{"path": os.path.abspath(base), "relation": "fallback"}]


def _plan_repo_set(plan_json_path, run_cwd):
    """The selected repo-root paths for the build phase. Read from the plan
    JSON's ``result.repos`` (entries with a truthy ``selected``), falling back
    to ``discover_git_roots(run_cwd)`` when the field is missing, unparseable,
    or empty. Tolerant by design — the plan JSON is the builder's contract, the
    discovery fallback keeps no-planner / older-plan runs working."""
    try:
        with open(plan_json_path, encoding="utf-8") as fh:
            data = json.load(fh)
        repos = (data.get("result") or {}).get("repos") or []
        selected = [r["path"] for r in repos
                    if isinstance(r, dict) and r.get("selected") and r.get("path")]
        if selected:
            return selected
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [r["path"] for r in discover_git_roots(run_cwd)]


# --------------------------------------------------------------------------- #
# Worktree provisioning (--worktree).                                          #
#                                                                              #
# A deterministic git gate (cowork's own check, never the agent's word) plus a #
# lightweight pre-scouting role that creates the worktree following the repo's #
# convention, then a deterministic validation of the result (D13) before the   #
# session is redirected into the worktree (os.chdir).                          #
# --------------------------------------------------------------------------- #


def git_worktree_toplevel(cwd):
    """Return the absolute git work-tree toplevel for `cwd`, or None if `cwd` is
    not inside a git work tree (the deterministic --worktree gate, D1).

    Uses `git rev-parse --is-inside-work-tree` + `--show-toplevel`; never calls
    discover_git_roots() — the base is the single launch toplevel. Tolerant by
    design: a missing git, a bare repo, or any error reads as 'not a work tree'
    (None) so the caller fails fast with rc 2 rather than half-initializing."""
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd,
            capture_output=True, text=True, timeout=10)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None
        path = top.stdout.strip()
        return os.path.abspath(path) if path else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _worktree_registered(base_toplevel, path):
    """Look up `path` in `git -C <base_toplevel> worktree list --porcelain`.

    Returns `{worktree, branch}` (branch as a short name, "" when detached) for
    the registered entry whose worktree path resolves to the same real path as
    `path`, or None when git fails or no entry matches. The deterministic half
    of the creation contract (D13c/d): cowork confirms the agent's reported path
    is actually a registered worktree of the launch repo, not the agent's word."""
    try:
        res = subprocess.run(
            ["git", "-C", base_toplevel, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    target = os.path.realpath(path)
    entries = []
    cur = {}
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            if cur:
                entries.append(cur)
            cur = {"worktree": line[len("worktree "):].strip(), "branch": ""}
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref[len("refs/heads/"):] \
                if ref.startswith("refs/heads/") else ref
    if cur:
        entries.append(cur)
    for entry in entries:
        if os.path.realpath(entry.get("worktree", "")) == target:
            return {"worktree": entry["worktree"], "branch": entry["branch"]}
    return None


def validate_worktree(base_toplevel, artifact):
    """Deterministically validate the worktree role's result BEFORE any chdir
    (D13). Returns `(ok, worktree_path, branch, error)`.

    Requires: a status artifact dict with status='ready'; an absolute,
    existing-directory worktree path; that path registered in
    `git worktree list` for `base_toplevel`; and a reported branch that matches
    the branch checked out there. A missing/malformed artifact, status='failed',
    status='handoff_back' (the worktree role has no hand-back partner), a
    non-absolute/nonexistent/unregistered path, or a branch mismatch all fail —
    so a malformed/partial creation can never silently chdir the session into a
    bad tree."""
    if not isinstance(artifact, dict) or not artifact:
        return False, None, None, "worktree role wrote no status artifact"
    status = artifact.get("status")
    if status != "ready":
        result = artifact.get("result") or {}
        reason = (result.get("error") if isinstance(result, dict) else None) \
            or artifact.get("handoff") or "no reason given"
        return (False, None, None,
                "worktree role did not succeed (status=%r): %s"
                % (status, reason))
    result = artifact.get("result") or {}
    if not isinstance(result, dict):
        return False, None, None, "worktree artifact result is malformed"
    path = result.get("worktree_path") or result.get("path")
    branch = result.get("branch")
    if not path or not os.path.isabs(str(path)):
        return (False, None, None,
                "worktree path missing or not absolute: %r" % (path,))
    if not os.path.isdir(path):
        return False, None, None, "worktree path does not exist: %s" % path
    if not branch:
        return False, None, None, "worktree branch missing from artifact"
    registered = _worktree_registered(base_toplevel, path)
    if not registered:
        return (False, None, None,
                "worktree path is not registered in `git worktree list` for %s: "
                "%s" % (base_toplevel, path))
    reg_branch = registered.get("branch") or ""
    if reg_branch != branch:
        return (False, None, None,
                "worktree branch mismatch: artifact reported %r but the "
                "registered worktree is on %r" % (branch, reg_branch or
                                                  "(detached)"))
    return True, os.path.realpath(path), branch, None


def default_worktree_name(session_uuid):
    """The auto worktree/branch name when --worktree is given without a NAME:
    `cowork-<first 8 of session uuid>` (D7) — deterministic and tied to the
    session. The agent appends a numeric suffix on an auto-name collision."""
    return "cowork-" + (session_uuid or "00000000")[:8]


def assemble_worktree_brief(status_path, base_toplevel, name, explicit):
    """The worktree role's deterministic brief: the base repo path, the desired
    name + branch, the explicit-vs-auto collision policy (D13), and the exact
    status artifact it must write. Pure string templating — no model call."""
    collision = (
        "The name was requested EXPLICITLY (via --worktree NAME). On a "
        "collision (a worktree or branch of this name already exists), do NOT "
        "rename it: reuse it ONLY if an existing worktree at the matching path "
        "is already on this exact branch (idempotent reuse); otherwise report "
        "failure (status=failed) with a clear reason."
        if explicit else
        "The name was AUTO-generated. On a collision (a worktree or branch of "
        "this name already exists and is not an exact reusable match), append a "
        "numeric suffix (%s-2, %s-3, ...) to find a free name." % (name, name))
    return (
        "You are the cowork worktree role. Create a git worktree for the "
        "repository below, FOLLOWING that repository's own worktree "
        "convention, WITHOUT asking the user anything.\n\n"
        "Base repository (git work-tree toplevel): %s\n"
        "Desired worktree/branch name: %s\n\n"
        "Steps:\n"
        "1. Inspect the base repo for its worktree convention, in order: its "
        "docs/notes (AGENTS.md, README, CONTRIBUTING, etc.), `git worktree "
        "list`, an existing `.worktrees/` directory, and existing sibling "
        "worktree directories. Follow whatever convention you find. If the repo "
        "documents NO convention, create the worktree as a sibling directory "
        "`../<repo>-worktrees/<name>` next to the base repo.\n"
        "2. Create the worktree AND a same-named branch off the current HEAD "
        "(e.g. `git -C <base> worktree add <path> -b <name>`). %s\n"
        "3. ALSO perform any post-create setup the repo documents as part of "
        "its convention (e.g. creating a per-worktree virtualenv and installing "
        "dependencies). If the repo documents no setup, create the bare "
        "worktree + branch only — do not invent setup steps.\n"
        "4. Write your status artifact to EXACTLY this file (absolute path):\n"
        "   %s\n"
        "   On success, write JSON:\n"
        "     {\"role\": \"worktree\", \"status\": \"ready\", \"result\": "
        "{\"worktree_path\": \"<ABSOLUTE path to the created worktree>\", "
        "\"branch\": \"<branch name>\"}}\n"
        "   The worktree_path MUST be absolute and MUST be the path you passed "
        "to `git worktree add`. On failure (you could not create or reuse a "
        "worktree), write status=failed with result.error explaining why. "
        "There is no reviewer and no approval gate — the status artifact is the "
        "only channel cowork reads, and cowork independently verifies the "
        "worktree exists and is git-registered."
        % (base_toplevel, name, collision, status_path))


def run_worktree(wt_config, status_path, base_toplevel, name, explicit,
                 io_in=None, io_out=None, session_factory=None,
                 claude_spawn=None, session_uuid=None, trace=None,
                 extra_writable_dir=None):
    """Spawn ONE agent (controller from --wt-controller) to create the worktree,
    then read back its status artifact. No reviewer, no gate (D4). Returns the
    parsed artifact dict (or None when the agent wrote nothing); the CALLER
    validates it deterministically via validate_worktree (D13).

    `wt_config` is the single-role config dict {controller, yolo, mode}. The
    role runs with execution enabled (yolo) so it can run `git worktree add`
    (D5). `session_factory` is injectable for tests."""
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    controller = wt_config["controller"]
    # Fail-closed order: check policy FIRST (cheap, no side effects) so a
    # policy-disallowed controller never pays for a manifest compile; only a
    # policy-allowed controller reaches compile/revalidate. Either way, the
    # single dispatch decision (policy + manifest preflight) binds to it and
    # refuses before any brief/prompt assembly.
    _wf = _guard_to_policy_fact(controller, WORKTREE_ROLE, trace=trace)
    _wt_manifest = None
    if _wf["allowed"] and session_uuid:
        try:
            # base_toplevel is the real evidence for this exact dispatch: the
            # `--worktree` gate already proved it via `git rev-parse
            # --show-toplevel` (git_worktree_toplevel) before this role was
            # ever considered, so the manifest declares the SAME safe,
            # read-only git operation that produced it — never a fabricated
            # or mutating one (`git worktree add` is the AGENT's own, later,
            # live-guarded action, not a manifest-declared capability).
            _wt_manifest, _ = _compile_role_manifest(
                role=WORKTREE_ROLE, session_uuid=session_uuid,
                work_id=WORKTREE_ROLE,
                controller=controller,
                mode=wt_config.get("mode", "implement"),
                model=wt_config.get("model"),
                effort=wt_config.get("effort"),
                instruction_paths=[WORKTREE_PROMPT_PATH],
                sessions_dir=extra_writable_dir,
                action_classes=["git"] if base_toplevel else [],
                command_adapters=(
                    {"git": {"subcommand": "rev-parse",
                             "flags": ["--show-toplevel"]}}
                    if base_toplevel else {}),
                force_recompile=False)
        except Exception:
            _wt_manifest = {}
    _wdec = _decide_and_trace(
        trace, WORKTREE_ROLE, controller, "worktree", "run_worktree",
        manifest=_wt_manifest, policy_result=_wf,
        preflight_result=_manifest_preflight_fact(_wt_manifest))
    if _wdec["outcome"] == "refuse":
        manifest_refused = _wdec["source"] == "preflight"
        if manifest_refused:
            _emit_dispatch_escalation(
                trace, WORKTREE_ROLE, "manifest_proven",
                "recompile and preflight the manifest", "prompt_assembly")
        if trace:
            trace.event(
                "worktree.run.end",
                result="manifest_refused" if manifest_refused
                else "policy_blocked",
                controller=controller)
        # Base semantics: a manifest refusal is escalated via the trace, not
        # surfaced on io_out — only a policy refusal writes its message here.
        if not manifest_refused:
            io_out.write(_wdec["refusal_message"] + "\n")
            io_out.flush()
        return None
    brief = assemble_worktree_brief(status_path, base_toplevel, name, explicit)
    # Clear any stale artifact so a failed/no-write run reads as None, never a
    # leftover 'ready' from an earlier attempt.
    try:
        os.remove(status_path)
    except OSError:
        pass
    if trace:
        trace.event("worktree.run.start", controller=controller,
                    base_toplevel=base_toplevel, worktree_name=name,
                    explicit=explicit, status_path=status_path)
    ui.banner(io_out, "worktree — creating a git worktree for this session\n"
              "name → %s\nbase → %s" % (name, base_toplevel), "start")
    io_out.flush()

    if controller == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        if session_factory:
            session = session_factory("claude")
        else:
            ok, alert = _with_status_spinner(
                io_out, "starting worktree role",
                lambda: bridge.probe_claude_stream_json(
                    spawn, mode=wt_config["mode"], yolo=wt_config["yolo"],
                    role_prompt_file=WORKTREE_PROMPT_PATH, trace=trace,
                    role=WORKTREE_ROLE, extra_writable_dir=extra_writable_dir,
                    cache_enabled=True))
            if not ok:
                _decide_and_trace(
                    trace, WORKTREE_ROLE, controller, "worktree",
                    "run_worktree", manifest=_wt_manifest,
                    policy_result=_ALLOW_FACT,
                    preflight_result=(_ALLOW_FACT if _wt_manifest
                                      else None),
                    probe_result=_probe_fact(alert))
                if trace:
                    trace.event("worktree.run.end", result="probe_failed")
                io_out.write("cowork: " + alert + "\n")
                io_out.flush()
                return None
            session = bridge.ClaudeSession(
                WORKTREE_PROMPT_PATH, wt_config["mode"], wt_config["yolo"],
                io_out=io_out, speaker=WORKTREE_ROLE, trace=trace,
                extra_writable_dir=extra_writable_dir,
                model=wt_config.get("model"), effort=wt_config.get("effort"))
        first = brief
    elif controller == "opencode":
        try:
            if session_factory:
                session = session_factory("opencode")
            else:
                session = bridge.OpencodeSession(
                    WORKTREE_PROMPT_PATH, wt_config["mode"], wt_config["yolo"],
                    io_out=io_out, speaker=WORKTREE_ROLE, trace=trace,
                    extra_writable_dir=extra_writable_dir,
                    model=wt_config.get("model"), effort=wt_config.get("effort"))
        except policy.DispatchBlocked as exc:
            _bwf = {"allowed": False,
                    "refusal_code": "controller_not_allowed",
                    "refusal_message": str(exc),
                    "source": "bridge_backstop"}
            _bwdec = _decide_and_trace(
                trace, WORKTREE_ROLE, controller, "worktree", "run_worktree",
                manifest=_wt_manifest, policy_result=_bwf)
            if trace:
                trace.event("worktree.run.end", result="policy_blocked",
                            controller=controller)
            io_out.write(_bwdec["refusal_message"] + "\n")
            io_out.flush()
            return None
        first = brief  # role prompt rides in the generated agent file
    else:
        if session_factory:
            session = session_factory("codex")
        else:
            session = bridge.CodexSession(
                wt_config["mode"], wt_config["yolo"], io_out=io_out,
                speaker=WORKTREE_ROLE, trace=trace,
                extra_writable_dir=extra_writable_dir,
                model=wt_config.get("model"), effort=wt_config.get("effort"))
        wt_role_text = _read_text(WORKTREE_PROMPT_PATH)
        first = assemble_codex_prompt(wt_role_text, "", brief)
        _emit_codex_role_prompt_bytes(trace, WORKTREE_ROLE, wt_role_text)
    try:
        _send(session, _worktree_seed_delivery(first),
              meta={"prompt_kind": "worktree_seed",
                                    "phase": "worktree"})
    finally:
        session.close()
    artifact = _read_worktree_artifact(status_path)
    if trace:
        trace.event("worktree.run.end", result="closed",
                    status=(artifact or {}).get("status"))
    return artifact


def _read_worktree_artifact(status_path):
    """Read the worktree role's status artifact, or None if missing/malformed."""
    try:
        with open(status_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def assemble_repo_discovery_note(candidates, base=None):
    """The repo-discovery note prepended to EVERY scout seed (initial, hand-back
    re-run, and resumed) so the scout's discovery responsibility survives every
    cycle. Names the launch folder and the discovered candidate git roots with
    their relation; identical text across all paths so it never drifts."""
    lines = "\n".join(
        "  - %s (%s)" % (c["path"], c["relation"]) for c in candidates)
    base_line = ("Launch folder: %s\n" % base) if base else ""
    return (
        "Repository discovery (computed for you from the launch folder):\n"
        "%s%s\n\n"
        "Your discovery responsibility: confirm with the user WHICH of these "
        "git roots the ticket actually touches, and record the chosen subset in "
        "your intel (result.repos, with a `selected` flag per root, plus "
        "result.repo_discovery). When exactly ONE root was discovered (including "
        "an ancestor or fallback single-root outcome), take it as the set and "
        "skip the confirmation question; ask only when 2+ candidate roots exist."
        % (base_line, lines))


def _git_build_baseline(cwd=None):
    """Read-only git snapshot at building-phase entry: `(head_sha, dirty)`, or
    `(None, None)` when this is not a git repo or git is unavailable.

    `head_sha` is the commit the build delta is measured from; `dirty` flags a
    non-empty worktree at build start (pre-existing changes that would
    otherwise be conflated into the delta). Tolerant by design — any failure
    degrades to no baseline rather than blocking the build."""
    import subprocess
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
            text=True, timeout=10)
        if head.returncode != 0:
            return None, None
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
            text=True, timeout=10)
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
        return head.stdout.strip(), dirty
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None


def write_build_baseline_manifest(session_uuid, cwd=None):
    """Persist the PER-FILE build baseline (`build_baseline.json`).

    The prose baseline records a HEAD sha and a dirty flag, which is enough for a
    human and not enough for a measurement: a session that starts from a dirty
    tree — as this project's own runs do — has no commit describing what the
    build actually started from, so "what did this build change" measured against
    HEAD attributes someone else's uncommitted work to the builder.

    The manifest hashes every tracked file instead, so build and review metrics
    are computed against the tree as it actually was. Tolerant: any failure
    returns None and the run continues.
    """
    import subprocess
    try:
        listed = subprocess.run(
            ["git", "ls-files"], cwd=cwd, capture_output=True, text=True,
            timeout=30)
        if listed.returncode != 0:
            return None
        paths = [p for p in listed.stdout.splitlines() if p.strip()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    manifest = state_store.build_manifest(cwd or os.getcwd(), paths)
    manifest["digest"] = state_store.manifest_digest(manifest)
    path = state_store.build_manifest_path_for(session_uuid)
    return path if state_store.write_build_manifest(path, manifest) else None


def build_baseline_note(head_sha, dirty):
    """The reviewer-facing note describing the build baseline, or "" when there
    is no git baseline. Names the start commit and, on a dirty start, warns the
    reviewer not to assume every change in the delta is the builder's."""
    if not head_sha:
        return ""
    note = "The build started from commit %s." % head_sha[:12]
    if dirty:
        note += (" NOTE: the worktree was ALREADY dirty at build start, so "
                 "some changes in the delta below may predate this build — "
                 "judge each change against the plan, do not assume every "
                 "change is the builder's.")
    return note


def build_baselines_note(entries):
    """Per-repo baseline METADATA block over the selected repo set. Each repo
    WITH a HEAD contributes a ``<path> started from commit <sha12>`` line (plus
    the dirty warning); a repo with NO HEAD (no commits yet / non-git fallback)
    still appears as ``<path> (no commit baseline)`` so the set is never
    silently narrowed. Returns ``""`` only when `entries` is empty.

    This is metadata ABOUT the roots, NOT the authoritative root list — that
    list is threaded separately to ``_build_diff_recipe``. `entries` are
    ``{path, head, dirty}`` dicts (head may be None)."""
    lines = []
    for e in entries:
        path = e.get("path")
        head = e.get("head")
        if head:
            line = "%s started from commit %s." % (path, head[:12])
            if e.get("dirty"):
                line += (" NOTE: this worktree was ALREADY dirty at build "
                         "start, so some changes in its delta may predate this "
                         "build — judge each change against the plan, do not "
                         "assume every change is the builder's.")
            lines.append(line)
        else:
            lines.append("%s (no commit baseline)." % path)
    return "\n".join(lines)


def _build_diff_recipe(repos=None, baseline_note=""):
    """Back-compat thin wrapper: the build-reviewer's live-delta capture recipe
    is now OWNED by the transport (`handoff.build_diff_recipe`), which builds it
    from validated repo metadata so no free-form text can ride through it. The
    build-baseline note is a separate path-first artifact, so `baseline_note` is
    no longer folded in here (kept for signature compatibility)."""
    return handoff.build_diff_recipe(repos)


def _build_reviewer_artifacts(plan_json_path, plan_md_path, build_status_path,
                              build_summary_path=None,
                              verification_receipt_path=None):
    arts = [
        {"label": "approved plan JSON (machine source of truth)",
         "path": plan_json_path, "kind": "json", "source": "plan_json"},
        {"label": "approved plan markdown", "path": plan_md_path,
         "kind": "markdown", "source": "plan_md"},
        {"label": "builder status JSON (status + verification log)",
         "path": build_status_path, "kind": "json", "source": "build_status"},
    ]
    if build_summary_path:
        arts.append({"label": "builder markdown summary (the user's review "
                     "surface)", "path": build_summary_path, "kind": "markdown",
                     "source": "build_summary"})
    if verification_receipt_path:
        # ORCH-050: the owned transaction's terminal result.json reaches the
        # reviewer by ABSOLUTE PATH as its own declared slot (optional, like
        # build_summary — a legacy session has none).
        arts.append({"label": "owned verification receipt (orchestrator-run "
                     "transaction result)", "path": verification_receipt_path,
                     "kind": "json", "source": "verification_receipt"})
    return arts


def assemble_build_reviewer_context(context, selected, plan_json_path,
                                    plan_md_path, build_status_path,
                                    baseline_note="", baseline_repos=None,
                                    build_summary_path=None, assets_dir=None,
                                    context_revision=None,
                                    verification_receipt_path=None,
                                    verification_overlay=None):
    """The build-reviewer's situational context (route 7), delivered FILE-ONLY
    via the shared transport: the shared session context, BOTH plan artifacts,
    the builder's status JSON, the builder's markdown summary (when wired), and
    the build-baseline metadata — every one by PATH. The live working-tree delta
    is NOT embedded (a stale snapshot would mis-review): the build-reviewer
    captures it itself via the diff recipe (content-free static instructions).
    `baseline_repos` is the explicit selected repo-root list (each
    ``{path, has_head}``) that drives the per-root capture recipe.
    `verification_receipt_path` + `verification_overlay` carry the owned
    verification receipt (ORCH-050): the receipt file by absolute path and the
    derived owned-facts overlay as closed-schema edge facts."""
    artifacts = [_shared_context_artifact(context, assets_dir, context_revision)]
    artifacts.extend(_build_reviewer_artifacts(
        plan_json_path, plan_md_path, build_status_path, build_summary_path,
        verification_receipt_path=verification_receipt_path))
    artifacts.append(_build_baseline_artifact(baseline_note, assets_dir))
    facts = {"team": list(selected or [])}
    if verification_overlay:
        facts.update(verification_overlay)
    return handoff.render_handoff(
        "builder->build-reviewer:review_ctx",
        artifacts=artifacts, facts=facts,
        ctx={"repos": list(baseline_repos or [])})


def _build_baseline_artifact(baseline_note, assets_dir=None):
    """Materialize the build-baseline / repo metadata note (content-free: per-
    root commit sha, dirty flag, repo list) to a file and return its descriptor
    artifact so the build-reviewer edge carries it by path."""
    label = "build-baseline metadata (per-root start commit + dirty flag)"
    path = handoff.persist_build_baseline_file(assets_dir, baseline_note or "")
    if path is None:
        return _tempfile_artifact(baseline_note, label, kind="markdown",
                                  prefix="cowork_baseline_", suffix=".txt",
                                  source="build_baseline")
    return {"label": label, "path": os.path.abspath(path), "kind": "markdown",
            "source": "build_baseline"}


def assemble_build_reviewer_resume_context(plan_json_path, plan_md_path,
                                           build_status_path,
                                           context_update=None,
                                           baseline_note="",
                                           baseline_repos=None,
                                           build_summary_path=None,
                                           assets_dir=None,
                                           context_revision=None,
                                           verification_receipt_path=None,
                                           verification_overlay=None):
    """Lighter context for a RESUMED build-reviewer session, delivered FILE-ONLY
    via the shared transport: only the updated artifacts are sent by PATH (plan,
    status, summary, build-baseline) — plus a context-update wake block
    referencing the persisted context FILE when the session context changed. The
    full delta is still read live via the diff recipe; no body is inlined. The
    owned verification receipt + overlay ride exactly as on the fresh edge
    (ORCH-050), so a resumed reviewer never loses the receipt mid-loop."""
    artifacts = list(_build_reviewer_artifacts(
        plan_json_path, plan_md_path, build_status_path, build_summary_path,
        verification_receipt_path=verification_receipt_path))
    artifacts.append(_build_baseline_artifact(baseline_note, assets_dir))
    ctx = {"repos": list(baseline_repos or [])}
    if context_update:
        ctx["context_update_prefix"] = context_update_block(
            context_update, assets_dir, context_revision)
    facts = {"team": []}
    if verification_overlay:
        facts.update(verification_overlay)
    return handoff.render_handoff(
        "builder->build-reviewer:review_resume",
        artifacts=artifacts, facts=facts, ctx=ctx)


def make_build_reviewer_runner(plan_json_path, plan_md_path, baseline_note="",
                               baseline_repos=None, trace=None,
                               extra_writable_dir=None, build_summary_path=None,
                               session_uuid=None):
    """Build the real (non-test) reviewer runner for the building phase: a
    `run_reviewer_once` closure carrying the build-reviewer role, prompt, and
    the full-delta context assemblers. The reviewed artifact passed to the
    runner is the builder's status file path; the delta itself is read live by
    the reviewer (`baseline_note` tells it which commit each repo's delta is
    measured from and whether a worktree started dirty; `baseline_repos` is the
    explicit selected repo-root list, each ``{path, has_head}``, that drives the
    per-root capture recipe). When `session_uuid` is wired, each context
    assembly looks up the CURRENT owned-receipt pointer from state at render
    time (ORCH-050), so both the fresh and the resumed reviewer edge carry the
    receipt file by absolute path plus the derived overlay facts with the
    disposition current as of that render (D-0002)."""
    def receipt_kwargs():
        overlay, pointer = _current_verification_overlay(session_uuid)
        return {
            "verification_overlay": overlay,
            "verification_receipt_path": (
                pointer.get("receipt_path")
                if isinstance(pointer, dict) else None),
        }

    def runner(config, context, selected, build_status_path, review_path,
               resume_id=None, on_session=None, context_update=None,
               eval_scratch_path=None, eval_specs=None, surface_io_out=None,
               context_revision=None, session_uuid=None):
        return run_reviewer_once(
            config, context, selected, build_status_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            context_revision=context_revision, session_uuid=session_uuid,
            artifact_paths=[plan_json_path, plan_md_path, build_status_path,
                            build_summary_path], phase="building",
            reviewer_role=BUILD_REVIEWER,
            prompt_path=BUILD_REVIEWER_PROMPT_PATH,
            protected="the builder's working-tree delta and status file",
            context_fn=lambda ctx, sel, p, assets_dir=None,
                context_revision=None:
                assemble_build_reviewer_context(
                    ctx, sel, plan_json_path, plan_md_path, p,
                    baseline_note=baseline_note, baseline_repos=baseline_repos,
                    build_summary_path=build_summary_path,
                    assets_dir=assets_dir, context_revision=context_revision,
                    **receipt_kwargs()),
            resume_context_fn=lambda p, context_update=None, assets_dir=None,
                context_revision=None:
                assemble_build_reviewer_resume_context(
                    plan_json_path, plan_md_path, p,
                    context_update=context_update, baseline_note=baseline_note,
                    baseline_repos=baseline_repos,
                    build_summary_path=build_summary_path,
                    assets_dir=assets_dir, context_revision=context_revision,
                    **receipt_kwargs()))
    # See make_planning_advisor_runner: marks a real surface-capable closure.
    runner._coplan_surface_capable = True
    return runner


def _run_reviewer_eval(session, reviewer_role, eval_scratch_path, eval_specs,
                       trace=None, context_revision=None, artifact_paths=None,
                       verdict=None):
    """Send the reviewer its private evaluation turn on the still-open session
    (after its verdict was read back, before close — no resume round-trip).

    The reviewer already streams to a quiet sink, so no muting wrapper is
    needed. Failures are traced and swallowed: the eval is observational and
    must never affect the verdict. `verdict` (this pass's verdict dict, when
    the caller has it) rides into the turn-accounting sidecar so aggregated
    entries can correlate scores with the round's outcome."""
    if not (eval_specs and eval_scratch_path):
        return
    # Per-turn output, not durable state: clear any prior round's scratch
    # (and its accounting sidecar) BEFORE the send (mirrors the review-file
    # clearing above).
    _clear_eval_scratch(eval_scratch_path, reviewer_role, trace=trace)
    if trace:
        trace.event("eval.request", evaluator=reviewer_role,
                    evaluatees=[s.get("evaluatee") for s in eval_specs],
                    phase=eval_specs[0].get("phase"),
                    round=eval_specs[0].get("round"))
    try:
        # An eval is always a follow-up turn on the still-open reviewer session,
        # so it is a resume. SC5: the artifact descriptors are aggregated from
        # the SAME handoff objects that built the eval prompt (each spec's
        # artifact_block is a HandoffBlock) — never re-inferred from a path list.
        eval_turn_id = str(uuid.uuid4())
        eval_prompt = assemble_eval_prompt(
            reviewer_role, eval_scratch_path, eval_specs)
        send_result = _send(session, _eval_delivery(eval_prompt, eval_specs),
            meta={"prompt_kind": "eval", "fresh": False, "resume": True,
                  "phase": eval_specs[0].get("phase"),
                  "round": eval_specs[0].get("round"),
                  "context_revision": context_revision,
                  "artifacts": _eval_artifact_descriptors(eval_specs),
                  "eval_turn_id": eval_turn_id})
        _write_eval_turn_sidecar(eval_scratch_path, session, send_result,
                                 eval_turn_id, len(eval_specs),
                                 verdict=verdict)
    except Exception:  # noqa: BLE001 - eval must never break the review pass
        if trace:
            trace.event("eval.send.error", evaluator=reviewer_role)


def run_reviewer_once(config, context, selected, intel_path, review_path,
                      session_factory=None, claude_spawn=None,
                      resume_id=None, on_session=None, context_update=None,
                      trace=None, reviewer_role=SCOUT_REVIEWER,
                      prompt_path=None, context_fn=None,
                      resume_context_fn=None,
                      protected="the scout intel file",
                      eval_scratch_path=None, eval_specs=None,
                      extra_writable_dir=None, surface_io_out=None,
                      context_revision=None, artifact_paths=None, phase=None,
                      session_uuid=None):
    """Spawn (or resume) a paired reviewer for one pass and return its verdict.

    Role-generic: by default this is the scout-reviewer reviewing the scout
    intel; the planning phase passes `reviewer_role`, `prompt_path`, and the
    context assemblers to run the planning-advisor against the planner's plan
    (`intel_path` is then the plan JSON path).

    The reviewer is a PERSISTENT session: its id is captured via `on_session`
    (so cowork can store it) and `resume_id` resumes it on later rounds and on a
    cowork resume, preserving its accumulated context across invocations. A fresh
    session gets the full context (brief + shared context + artifact); a resumed
    one gets only the updated artifact — prefixed with a context-update wake
    block (`context_update`) when the session context changed since the reviewer
    last acknowledged it, so a resumed reviewer never operates on stale context.

    The reviewer writes its verdict to `review_path`; we read it back via
    `state_store.read_review` (the review file is the handoff channel because the
    session bridges stream to io_out and return no value). Its raw stream goes to
    a quiet sink so nothing reaches the user. On any failure or missing/malformed
    file, read_review yields a safe non-approving `revise` (or None, which the
    caller treats as revise)."""
    prompt_path = prompt_path or SCOUT_REVIEWER_PROMPT_PATH
    cfg = config.get(reviewer_role) or DEFAULTS[reviewer_role]
    # Fail-closed order: check policy FIRST (cheap, no side effects) so a
    # policy-disallowed controller never pays for a manifest compile; only a
    # policy-allowed controller reaches compile/revalidate. Either way, the
    # single dispatch decision (policy + manifest preflight) binds to it and
    # refuses before any brief/prompt assembly.
    _rf = _guard_to_policy_fact(cfg["controller"], reviewer_role, phase=phase,
                                trace=trace)
    _rev_manifest = None
    if _rf["allowed"] and session_uuid:
        try:
            _rev_manifest, _ = _compile_role_manifest(
                role=reviewer_role, session_uuid=session_uuid,
                work_id=reviewer_role,
                controller=cfg["controller"], mode=cfg.get("mode", "implement"),
                model=cfg.get("model"), effort=cfg.get("effort"),
                instruction_paths=[prompt_path or SCOUT_REVIEWER_PROMPT_PATH],
                sessions_dir=extra_writable_dir,
                # intel_path is the exact artifact THIS reviewer pass judges
                # (scout intel, plan, or build status depending on
                # reviewer_role) — a real candidate snapshot, not fabricated;
                # its content changing between compiles is a genuine
                # revalidation trigger.
                candidate_snapshot=_file_snapshot(intel_path),
                force_recompile=bool(resume_id))
        except Exception:
            _rev_manifest = {}
    _rdec = _decide_and_trace(
        trace, reviewer_role, cfg["controller"], "review", "run_reviewer_once",
        manifest=_rev_manifest, policy_result=_rf,
        preflight_result=_manifest_preflight_fact(_rev_manifest), phase=phase,
        resume_session_id=resume_id)
    if _rdec["outcome"] == "refuse":
        manifest_refused = _rdec["source"] == "preflight"
        if manifest_refused:
            _emit_dispatch_escalation(trace, reviewer_role, "manifest_proven",
                                      "recompile and preflight the manifest",
                                      "prompt_assembly")
        if trace:
            trace.event("review.run.end", role=reviewer_role,
                        result=("manifest_refused" if manifest_refused
                                else "policy_blocked"),
                        controller=cfg["controller"], phase=phase)
        # Base semantics: a manifest refusal is escalated via the trace, not
        # surfaced as a controller_failure_alert — only a policy refusal
        # carries its message onto the verdict.
        return _controller_failure_verdict(
            {"ok": False,
             "result": "manifest_refused" if manifest_refused
             else "policy_blocked"},
            alert=None if manifest_refused else _rdec["refusal_message"])
    quiet = _QuietSink()
    # When `surface_io_out` is set the REVIEW turn streams to the user on the
    # wholly-internal (dim) channel under the reviewer's own label; otherwise it
    # goes to the quiet sink, byte-identical to the historical hidden behavior.
    # The reviewer's peer-eval send always stays muted (D-eval-stays-muted).
    surface = surface_io_out is not None
    review_io = surface_io_out if surface else quiet
    brief = assemble_reviewer_brief(review_path, protected=protected)
    # Measurable-goal structural check: scout intel that reached review without
    # a non-empty result.success_criteria gets an auto-finding note in the
    # reviewer's brief (fresh AND resume passes — the brief rides both). Scoped
    # to the scout-reviewer: the other reviewers' artifacts (plan JSON, build
    # status) carry their own contracts.
    if reviewer_role == SCOUT_REVIEWER:
        criteria_flag = _success_criteria_flag(intel_path)
        if criteria_flag:
            brief = brief + "\n\n" + criteria_flag
            if trace:
                trace.event("review.structural_flag", role=reviewer_role,
                            check="success_criteria_missing",
                            intel_path=intel_path)
    # Build the reviewer context FIRST (before the trace + accounting) via the
    # shared FILE-ONLY transport. The shared session context is materialized to a
    # revision-keyed file under the session-assets dir and referenced by PATH; a
    # standalone/test call (no dir) writes a tempfile. ctx_block is a
    # handoff.HandoffBlock carrying .delivery ("path") + per-path .embedded +
    # the content-free .descriptors the trace/report accounting derive from (SC5).
    assets_dir = extra_writable_dir
    if resume_id:
        ctx_block = (resume_context_fn or assemble_reviewer_resume_context)(
            intel_path, context_update=context_update, assets_dir=assets_dir,
            context_revision=context_revision)
    else:
        ctx_block = (context_fn or assemble_reviewer_context)(
            context, selected, intel_path, assets_dir=assets_dir,
            context_revision=context_revision)
    # Per-turn accounting (#1/D11) merged into the bridge's controller.turn.start:
    # what kind of prompt, fresh-vs-resume, and the FULL reviewed artifact-set
    # descriptors — derived from the SAME handoff object the prompt was built
    # from (SC5), never re-inferred. role/controller are set by the bridge
    # itself. The single review_artifacts value is reused across the fresh AND
    # resume sends and all run.start/run.end traces.
    meta_artifact_paths = artifact_paths or [intel_path]
    review_artifacts = getattr(ctx_block, "descriptors", None) or \
        _artifact_descriptors(meta_artifact_paths, delivery="path")
    if trace:
        trace.event("review.run.start", role=reviewer_role,
                    controller=cfg["controller"], resume=bool(resume_id),
                    fresh=not bool(resume_id), prompt_kind="reviewer_pass",
                    phase=phase, context_revision=context_revision,
                    artifacts=review_artifacts,
                    intel_path=intel_path, review_path=review_path,
                    context_update=bool(context_update))
    # The review file is per-pass output, not durable state: clear any previous
    # verdict BEFORE the pass so a reviewer that fails (or never writes) yields
    # None -> safe revise, instead of a stale `approve` from an earlier round
    # being read back as this pass's verdict.
    try:
        os.remove(review_path)
        if trace:
            trace.event("review.file.cleared", role=reviewer_role,
                        review_path=review_path)
    except OSError:
        pass
    review_meta = {
        "prompt_kind": "reviewer_pass",
        "phase": phase,
        "fresh": not bool(resume_id),
        "resume": bool(resume_id),
        "context_revision": context_revision,
        "artifacts": review_artifacts,
    }

    if cfg["controller"] == "claude":
        cb = (lambda i: on_session("claude", i)) if on_session else None
        if session_factory:
            session = session_factory("claude", review_io)
        elif resume_id:
            session = bridge.ClaudeSession(
                prompt_path, cfg["mode"], cfg["yolo"],
                io_out=review_io, speaker=reviewer_role, internal=surface,
                resume_id=resume_id, on_session_id=cb, trace=trace,
                extra_writable_dir=extra_writable_dir,
                model=cfg.get("model"), effort=cfg.get("effort"))
        else:
            spawn = claude_spawn or bridge._real_claude_spawn
            ok, alert = bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=prompt_path, trace=trace,
                role=reviewer_role, extra_writable_dir=extra_writable_dir,
                cache_enabled=True)
            if not ok:
                _decide_and_trace(
                    trace, reviewer_role, cfg["controller"], "review",
                    "run_reviewer_once", manifest=_rev_manifest,
                    policy_result=_ALLOW_FACT,
                    preflight_result=(_ALLOW_FACT if _rev_manifest
                                      else None),
                    probe_result=_probe_fact(alert), phase=phase)
                verdict = _controller_failure_verdict(
                    {"ok": False, "result": "probe_failed"}, alert=alert)
                if trace:
                    trace.event("review.run.end", role=reviewer_role,
                                result="probe_failed",
                                verdict=None,
                                controller_failure=True,
                                prompt_kind="reviewer_pass", phase=phase,
                                context_revision=context_revision,
                                fresh=not bool(resume_id),
                                resume=bool(resume_id),
                                artifacts=review_artifacts)
                return verdict
            # Pin a known id up front so it is resumable even if killed early.
            sid = str(uuid.uuid4())
            if on_session:
                on_session("claude", sid)
            session = bridge.ClaudeSession(
                prompt_path, cfg["mode"], cfg["yolo"],
                io_out=review_io, speaker=reviewer_role, internal=surface,
                session_id=sid, on_session_id=cb, trace=trace,
                extra_writable_dir=extra_writable_dir,
                model=cfg.get("model"), effort=cfg.get("effort"))
        prompt = (brief + "\n\n" + ctx_block).strip()
    elif cfg["controller"] == "opencode":
        # Role prompt rides in the generated agent file (system prompt, like
        # claude) — never inlined into the reviewer prompt body.
        cb = (lambda i: on_session("opencode", i)) if on_session else None
        try:
            if session_factory:
                session = session_factory("opencode", review_io)
            else:
                session = bridge.OpencodeSession(
                    prompt_path, cfg["mode"], cfg["yolo"], io_out=review_io,
                    speaker=reviewer_role, internal=surface,
                    resume_session_id=resume_id, on_session_id=cb, trace=trace,
                    extra_writable_dir=extra_writable_dir,
                    model=cfg.get("model"), effort=cfg.get("effort"))
        except policy.DispatchBlocked as exc:
            _brf = {"allowed": False,
                    "refusal_code": "controller_not_allowed",
                    "refusal_message": str(exc),
                    "source": "bridge_backstop"}
            _brdec = _decide_and_trace(
                trace, reviewer_role, cfg["controller"], "review",
                "run_reviewer_once", manifest=_rev_manifest,
                policy_result=_brf, phase=phase, resume_session_id=resume_id)
            if trace:
                trace.event("review.run.end", role=reviewer_role,
                            result="policy_blocked",
                            controller=cfg["controller"], phase=phase)
            return _controller_failure_verdict(
                {"ok": False, "result": "policy_blocked"},
                alert=_brdec["refusal_message"])
        prompt = (brief + "\n\n" + ctx_block).strip()
    else:  # codex
        cb = (lambda i: on_session("codex", i)) if on_session else None
        if resume_id:
            prompt = (brief + "\n\n" + ctx_block).strip()  # thread already has role
        else:
            reviewer_role_text = _read_text(prompt_path)
            prompt = assemble_codex_prompt(reviewer_role_text, brief, ctx_block)
            _emit_codex_role_prompt_bytes(trace, reviewer_role,
                                          reviewer_role_text)
        if session_factory:
            session = session_factory("codex", review_io)
        else:
            session = bridge.CodexSession(
                cfg["mode"], cfg["yolo"], io_out=review_io,
                speaker=reviewer_role, internal=surface,
                resume_thread_id=resume_id, on_thread_id=cb,
                trace=trace, extra_writable_dir=extra_writable_dir,
                model=cfg.get("model"), effort=cfg.get("effort"))
    try:
        send_result = _send(
            session, _cross_delivery(prompt, [ctx_block]),
            meta=review_meta)
        if not send_result.get("ok", True):
            verdict = _controller_failure_verdict(send_result)
            if trace:
                trace.event(
                    "review.run.end", role=reviewer_role,
                    result="controller_failed",
                    controller_result=send_result.get("result"),
                    error_type=send_result.get("error_type"),
                    subtype=send_result.get("subtype"),
                    verdict=None,
                    malformed=True,
                    prompt_kind="reviewer_pass", phase=phase,
                    context_revision=context_revision,
                    fresh=not bool(resume_id), resume=bool(resume_id),
                    artifacts=review_artifacts)
            return verdict
        verdict = state_store.read_review(review_path)
        # Keep the eval send muted even when the review turn is surfaced.
        with _muted_session(session) if surface else contextlib.nullcontext():
            _run_reviewer_eval(session, reviewer_role, eval_scratch_path,
                               eval_specs, trace=trace,
                               context_revision=context_revision,
                               artifact_paths=(meta_artifact_paths
                                               + [review_path]),
                               verdict=verdict)
    finally:
        session.close()
    if trace:
        trace.event("review.run.end", role=reviewer_role, result="ok",
                    verdict=(verdict or {}).get("verdict"),
                    malformed=bool((verdict or {}).get("malformed")),
                    prompt_kind="reviewer_pass", phase=phase,
                    context_revision=context_revision,
                    fresh=not bool(resume_id), resume=bool(resume_id),
                    artifacts=review_artifacts)
    return verdict


# Banner text producers. The text is rendered through ui.banner (a gum-styled box
# on a TTY, plain text otherwise). The full intel path is shown once, in the start
# banner; later banners use the shortened form (#11/#12). Keyword substrings
# ("needs your input", "ready for review", "scout finished") are preserved so the
# non-TTY/test path can assert them.


def scout_start_text(intel_path, resuming=False, enabled=False):
    if resuming:
        head = (
            "scout — resuming our previous session\n"
            "Picking up where we left off with the earlier context (no goal prompt "
            "needed). To start fresh instead, run with --no-session; to redirect, "
            "pass --context. Ctrl-C aborts."
        )
    else:
        head = (
            "scout — gathering context\n"
            "I'll investigate, ask what I need, and propose options. I finish on my\n"
            "own once we agree. You drive — answer my questions. Ctrl-C aborts."
        )
    return head + "\nintel → %s" % ui.render_path(intel_path, enabled)


def scout_needs_input_text():
    return "scout needs your input"


def scout_review_text(intel_path, enabled=False):
    return "scout intel ready for review — %s" % ui.render_path(
        intel_path, enabled)


def scout_done_text(intel_path, enabled=False):
    return "scout finished — intel → %s" % ui.render_path(intel_path, enabled)


def planner_start_text(plan_md_path, resuming=False, enabled=False):
    if resuming:
        head = (
            "planner — resuming our previous planning session\n"
            "Picking up where we left off. Ctrl-C aborts."
        )
    else:
        head = (
            "planner — planning from the approved intel\n"
            "I'll draft the plan, ask what I need, and mark it ready when we "
            "agree. You drive — answer my questions. Ctrl-C aborts."
        )
    return head + "\nplan → %s" % ui.render_path(plan_md_path, enabled)


def planner_needs_input_text():
    return "planner needs your input"


def planner_review_text(plan_md_path, enabled=False):
    return "plan ready for review — %s" % ui.render_path(plan_md_path, enabled)


def planner_done_text(plan_md_path, enabled=False):
    return "planner finished — plan approved → %s" % ui.render_path(
        plan_md_path, enabled)


def handoff_gate_text(payload):
    return ("planner wants to hand the work back to the scout\n"
            "handoff note:\n%s" % (payload or "").strip())


def builder_start_text(build_surface_path, resuming=False, enabled=False):
    if resuming:
        head = (
            "builder — resuming our previous build session\n"
            "Picking up where we left off. Ctrl-C aborts."
        )
    else:
        head = (
            "builder — building from the approved plan\n"
            "I'll make the changes, verify them, and mark the build ready when "
            "it's done. You drive — answer my questions. Ctrl-C aborts."
        )
    # The review surface is the build summary when one is wired (mirrors the
    # scout's intel.md / planner's plan.md start banner); falls back to the
    # status file when no summary path is given.
    return head + "\nsummary → %s" % ui.render_path(build_surface_path, enabled)


def builder_needs_input_text():
    return "builder needs your input"


def builder_review_text(build_status_path, enabled=False, overlay=None,
                        receipt_path=None, agent_status_path=None):
    """The building-phase human-gate banner line, plus — when an owned
    verification receipt binds the current candidate (ORCH-050/UX-021) — the
    derived overlay block: owned facts, the separately-labeled agent prose
    pointer, and the contradiction warning. With no overlay the historical
    one-line banner is returned byte-identically."""
    text = "build ready for review — %s" % ui.render_path(
        build_status_path, enabled)
    block = render_verification_overlay_block(
        overlay, receipt_path=receipt_path,
        agent_status_path=agent_status_path)
    return text + block if block else text


def builder_done_text(build_status_path, enabled=False):
    return ("builder finished — review your working tree → %s"
            % ui.render_path(build_status_path, enabled))


def builder_handoff_gate_text(payload):
    return ("builder wants to hand the work back to the planner\n"
            "handoff note:\n%s" % (payload or "").strip())


# The reviewer hash-gate bundle threaded into `_role_loop` for the scout and
# planner (never the builder). Its three callables close over run_flow's active
# session-state holder + the phase epoch + the paired reviewer role + the
# current context revision:
#   - compute_composite() -> the sha256 over the reviewer's covered file set;
#   - eligible(composite)  -> True when that composite was the LAST APPROVED one
#                             in this epoch + acked context revision (skip OK);
#   - record(composite)    -> persist it as the new last-approved baseline
#                             (called only on an explicit reviewer approve).
# Default None in `_role_loop` preserves today's always-review behavior.
SkipBaseline = collections.namedtuple(
    "SkipBaseline", ["compute_composite", "eligible", "record"])


# The single, historical `_role_loop`/run_* outcome string for "this role's
# turn loop ended without an approval, a handoff, or a controller switch" —
# EOF, /quit, /stop, a controller failure, a stuck gate, or a pre-loop
# refusal (manifest/policy/probe/start) all collapse to this ONE outward
# value, which `run_flow` and the existing test suite depend on byte-for-byte
# (see e.g. test_run_flow_traces_context_and_saved_session and every
# `self.assertEqual(outcome, "ended")` in test_cowork.py). M2 Package E does
# NOT change this outward contract — "preserve legacy behavior outside the
# named seams" — it replaces the literal `"ended"` source pattern at every
# assignment/call site with this named constant AND, at each site, drives an
# individually justified `cowork_control_plane.advance()` event so the
# durable PhaseState this outward "ended" was silently standing in for is no
# longer ambiguous. See `_advance_phase`/`_role_work_id` below.
_OUTCOME_ENDED = "ended"


# Returned by the turn readers to mean "end the conversation" (EOF / Ctrl-D /
# explicit /quit), distinct from a blank line (which re-prompts).
_END = object()

# Returned by the dissent review gate to mean "hand the reviewer's unresolved
# findings back to the role for another pass" — the user opted to keep
# iterating without writing their own feedback.
_ITERATE = object()

# Tags the ('_ASK', text) marker tuple returned by _read_review when the user
# picks "Ask a question" at the review gate: the question is answered in chat
# WITHOUT reopening work, editing the artifact, or re-running the advisor.
_ASK = object()

# Returned by the gate readers when the user picks the non-default "Stop"
# choice (TTY only). _role_loop maps it to the clean-end path
# (outcome_kind='ended'): no approval, no revision turn, no done banner — the
# same terminal outcome as an off-TTY 'end', so run_flow never advances the
# phase and the saved (resumable) session record is left intact.
_STOP = object()


# The phase- and team-aware facts run_flow supplies about what approval at a
# given gate does, so the gate readers can render concise consequence previews
# beside every choice. Built by make_gate_preview and threaded through the
# run_* helpers into _role_loop, which passes it into the readers. When None
# (the historical default) the readers keep their plain, preview-free labels so
# no existing caller or test regresses.
GatePreview = collections.namedtuple(
    "GatePreview",
    ["approve_suffix",    # e.g. 'continue to planning' | 'intel is the deliverable'
     "terminal",          # True when approving ends the run (drives 'finish' wording)
     "next_phase",        # 'planning' | 'building' | None (dissent approve-anyway)
     "resuming_role",     # 'scout' | 'planner' | 'builder' (request-changes/ask/iterate/tell)
     "artifact_noun",     # 'intel' | 'plan' | 'build' (ask 'stays as-is' clause)
     "session_enabled"])  # drives the Stop label variant


# The per-role approve descriptor: (next_phase_name, terminal_suffix, noun).
# next_phase_name is None for the builder (approval is always terminal).
_GATE_APPROVE = {
    "scout": ("planning", "intel is the deliverable", "intel"),
    "planner": ("building", "plan is the deliverable", "plan"),
    "builder": (None, "review your working tree", "build"),
}


def make_gate_preview(role, downstream_on_team, session_enabled):
    """Build the GatePreview for `role`'s review gate. `downstream_on_team` is
    whether the phase that approval would chain into has its lead on the team
    (a planner for the scout gate, a builder for the planner gate; ignored for
    the always-terminal builder gate). Terminality — and thus whether 'finish'
    appears — depends on that downstream membership, not the phase alone."""
    next_phase_name, terminal_suffix, artifact_noun = _GATE_APPROVE[role]
    if next_phase_name and downstream_on_team:
        return GatePreview(
            approve_suffix="continue to %s" % next_phase_name,
            terminal=False, next_phase=next_phase_name, resuming_role=role,
            artifact_noun=artifact_noun, session_enabled=session_enabled)
    return GatePreview(
        approve_suffix=terminal_suffix, terminal=True, next_phase=None,
        resuming_role=role, artifact_noun=artifact_noun,
        session_enabled=session_enabled)


def _preview_approve_label(preview):
    lead = "Approve & finish" if preview.terminal else "Approve"
    return "%s — %s" % (lead, preview.approve_suffix)


def _preview_ask_label(preview):
    return ("Ask a question — answered in chat; the %s stays as-is"
            % preview.artifact_noun)


def _preview_changes_label(preview):
    return ("Request changes — the %s revises; you'll be asked for feedback"
            % preview.resuming_role)


def _preview_stop_label(preview):
    if preview.session_enabled:
        return "Stop — session remains resumable"
    return "Stop — end this run without approving"


def _preview_dissent_iterate_label(preview):
    return ("Keep iterating — hand the reviewer's findings back to the %s"
            % preview.resuming_role)


def _preview_dissent_tell_label(preview):
    return ("Tell it what to do — your instructions go to the %s"
            % preview.resuming_role)


def _preview_dissent_approve_label(preview):
    if preview.terminal:
        return "Approve & finish anyway — accept despite the reviewer"
    return "Approve anyway — continue to %s" % preview.next_phase


def _pending_question(status_path):
    """Return the question recorded by a ``needs_input`` status artifact.

    ``result.pending_question`` is the canonical field.  The small legacy-key
    fallback keeps resumable sessions written by older role prompts useful.
    Invalid/missing artifacts simply return an empty string; status validation
    remains tolerant everywhere else in the harness.
    """
    try:
        with open(status_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""

    containers = [data]
    if isinstance(data.get("result"), dict):
        containers.insert(0, data["result"])
    for container in containers:
        for key in ("pending_question", "question", "questions",
                    "open_questions"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                parts = [str(item).strip() for item in value
                         if isinstance(item, str) and item.strip()]
                if parts:
                    return "\n".join("- " + part for part in parts)
    return ""


def _missing_question_repair_prompt(artifact="intel"):
    return (
        "Your %s status says `needs_input`, but its JSON records no non-empty "
        "`result.pending_question`. Repair the status artifact now: if a user "
        "decision is truly required, record the exact question in "
        "`result.pending_question`, keep `status: needs_input`, and ask that "
        "same question plainly in your reply. If no decision is required, "
        "finish the work and set `status: ready_for_review`. Do not wait for "
        "an answer until the artifact contains the question." % artifact)


def _read_turn(io_in, io_out):
    """Read one working-turn reply. Blank input and a cancelled editor re-prompt;
    only EOF or an explicit /quit (or /stop) ends the loop (#4/#10)."""
    while True:
        ui.turn_separator(io_out)
        reply = ui.prompt_user(io_in, io_out, header="your answer")
        if reply is ui.EOF:        # input exhausted / Ctrl-D — end
            return _END
        if reply is ui.CANCEL:     # editor dismissed — discard draft, re-prompt
            continue
        if reply.strip() == "":    # blank line — re-prompt, never abort (#10)
            continue
        if reply.strip() in ("/quit", "/stop"):
            return _END
        return reply


def _gate_trace_callbacks(trace, role):
    """The (on_discard, on_drain_fail) pair every protected gate reader takes.

    Tracing lives here rather than in cowork_ui so the UI layer keeps no trace
    dependency, and so the emitted fields are assertable in a plain unit test.
    Neither closure ever calls cowork_trace.prompt_meta: a hash of discarded
    input is content-derived and is forbidden by the discard policy. `errno_name`
    is a symbolic errno such as 'ENOTTY', never a message that could carry user
    data.

    Both closures match cowork_ui's single declared callback signature exactly,
    and Trace.event drops None-valued fields, so `input.drain_failed` carries no
    `reopens` key and `input.gate_abandoned` carries neither `errno` nor
    `typeahead_cleared`. With no trace both are None, which the ui wrappers read
    as 'do not report'.

    Event routing is by OUTCOME, not by channel: `reopen_limit` is the only
    reason that is a policy give-up rather than a boundary failure, so it alone
    becomes `input.gate_abandoned`. Every genuine failure to clear a stale-input
    channel — `tcflush`, `typeahead`, `key_queue` — is an `input.drain_failed`
    whose `reason` field names the channel. A new channel therefore cannot
    silently be traced as an abandonment."""
    if not trace:
        return None, None

    def on_discard(gate, epoch, phase, count):
        trace.event("input.discarded", role=role, gate=gate, epoch=epoch,
                    phase=phase, chars=count)

    def on_drain_fail(gate, epoch, phase, reason, errno_name,
                      typeahead_cleared, reopens):
        trace.event(
            "input.gate_abandoned" if reason == "reopen_limit"
            else "input.drain_failed",
            role=role, gate=gate, epoch=epoch, phase=phase, reason=reason,
            errno=errno_name, typeahead_cleared=typeahead_cleared,
            reopens=reopens)

    return on_discard, on_drain_fail


def _read_review(io_in, io_out, allow_ask=True, preview=None,
                 on_discard=None, on_drain_fail=None):
    """At ready_for_review, decide approve-vs-(ask)-vs-revise.

    With `allow_ask` (the scout intel gate and planner plan gate) a TTY shows a
    questionary select: 'Ask a question' / 'Request changes' / 'Approve &
    finish', plus a 'Stop' when a `preview` is supplied. Without `allow_ask`
    (the builder build gate) a TTY with a `preview` shows a 3-way select —
    Request changes / Approve & finish / Stop — while a preview-less call keeps
    the binary confirm 'Approve & finish?' contract, now defaulting to No. Off a
    TTY both keep the historical blank=finish / text=revise contract (no Stop)
    so the scripted/test path is unchanged.

    Approve is never the highlighted choice and never happens by omission: blank,
    whitespace-only, cancelled and end-of-input feedback all re-open the gate,
    and a dismissed menu stops. The ONLY route to _END is an explicit approve
    selection (or an explicit Yes at the confirm) made after activation.

    When `preview` (a GatePreview) is given, every choice label carries a short,
    phase- and team-aware consequence; when None the plain labels are used.

    Returns _END to approve & finish, _STOP for the Stop choice, a dismissed
    menu such as Ctrl-C, or a failed input boundary (clean exit), the
    ('_ASK', text) marker tuple to ask a question (answered in chat without
    reopening work; only when `allow_ask`), or revision feedback."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        if not allow_ask:
            if preview is None:
                # Builder gate, preview-less: the binary confirm contract, now
                # default=No and looping, so nothing finishes by omission.
                while True:
                    approved = ui.confirm(
                        "Approve & finish?", default=False,
                        io_in=io_in, io_out=io_out, gate="ready_for_review",
                        on_discard=on_discard, on_drain_fail=on_drain_fail)
                    if approved is ui.DRAIN_FAILED:
                        return _STOP
                    if approved:
                        return _END
                    fb = ui.prompt_user(io_in, io_out,
                                        header="Revise — your feedback",
                                        gate="review_feedback",
                                        on_discard=on_discard,
                                        on_drain_fail=on_drain_fail)
                    if fb is ui.DRAIN_FAILED:
                        return _STOP
                    if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                        # Nothing typed: re-ask rather than finish. Blank or
                        # cancelled feedback must never be read as a sign-off.
                        continue
                    return fb
            # Builder gate with a preview: a 3-way select so every consequence
            # is visible before selection — Request changes / Approve & finish /
            # Stop, with approve deliberately not the highlighted choice.
            while True:
                choice = ui.select(
                    "Ready for review — what now?",
                    [("changes", _preview_changes_label(preview)),
                     ("approve", _preview_approve_label(preview)),
                     ("stop", _preview_stop_label(preview))],
                    io_in=io_in, io_out=io_out, gate="ready_for_review",
                    on_discard=on_discard, on_drain_fail=on_drain_fail)
                if choice is ui.DRAIN_FAILED:
                    return _STOP
                if choice == "approve":
                    return _END
                if choice == "stop" or choice is None:
                    # Questionary returns None for a dismissed menu, including
                    # a single Ctrl-C.  Cancellation follows the explicit Stop
                    # path; it must never be reinterpreted as a revision.
                    return _STOP
                # 'changes': request changes. Blank or cancelled feedback
                # re-opens the gate — it is never an approval.
                fb = ui.prompt_user(io_in, io_out,
                                    header="Request changes — your feedback",
                                    gate="review_feedback",
                                    on_discard=on_discard,
                                    on_drain_fail=on_drain_fail)
                if fb is ui.DRAIN_FAILED:
                    return _STOP
                if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                    continue
                return fb
        approve_label = (_preview_approve_label(preview) if preview is not None
                         else "Approve & finish")
        ask_label = (_preview_ask_label(preview) if preview is not None
                     else "Ask a question")
        changes_label = (_preview_changes_label(preview) if preview is not None
                         else "Request changes")
        # 'Ask a question' leads: the highlighted choice must change nothing.
        choices = [("ask", ask_label),
                   ("changes", changes_label),
                   ("approve", approve_label)]
        if preview is not None:
            choices.append(("stop", _preview_stop_label(preview)))
        while True:
            choice = ui.select("Ready for review — what now?", choices,
                               io_in=io_in, io_out=io_out,
                               gate="ready_for_review",
                               on_discard=on_discard,
                               on_drain_fail=on_drain_fail)
            if choice is ui.DRAIN_FAILED:
                return _STOP
            if choice == "approve":
                return _END
            if choice == "stop" or choice is None:
                # A dismissed Questionary menu (notably Ctrl-C) is the same
                # clean non-approving outcome as the visible Stop choice —
                # cancelling a gate is never an approval.
                return _STOP
            if choice == "ask":
                q = ui.prompt_user(io_in, io_out, header="Your question",
                                   gate="review_question",
                                   on_discard=on_discard,
                                   on_drain_fail=on_drain_fail)
                if q is ui.DRAIN_FAILED:
                    return _STOP
                if q is ui.CANCEL or q is ui.EOF or q.strip() == "":
                    # Nothing typed: re-show the gate rather than approve — a
                    # blank question must never be read as a sign-off.
                    continue
                return (_ASK, q)
            # 'changes': request changes. Blank or cancelled feedback re-opens
            # the gate, exactly like the blank-question path above.
            fb = ui.prompt_user(io_in, io_out,
                                header="Request changes — your feedback",
                                gate="review_feedback",
                                on_discard=on_discard,
                                on_drain_fail=on_drain_fail)
            if fb is ui.DRAIN_FAILED:
                return _STOP
            if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                continue
            return fb
    line = io_in.readline()
    if line == "" or line.strip() == "":
        return _END
    return line.rstrip("\n")


def _read_review_dissent(io_in, io_out, preview=None,
                         on_discard=None, on_drain_fail=None):
    """The `ready_for_review` gate when the reviewer's round cap was exhausted
    without approval. On a TTY a questionary select — the safe default (Enter)
    keeps iterating on the reviewer's feedback, so unresolved dissent is never
    approved by accident. 'Tell it what to do' prompts for custom instructions;
    blank custom input falls back to iterating. A dismissed preview-enabled
    menu (including Ctrl-C) follows Stop. With a
    `preview` (a GatePreview) a fourth non-default 'Stop' choice is added and
    every label carries a phase-truthful consequence — the approve-anyway label
    reads 'Approve & finish anyway' only when approval is terminal, else
    'Approve anyway — continue to <phase>'. Off a TTY it keeps the historical
    blank=finish / text=revise contract (no Stop) so the scripted/test path is
    unchanged.

    Returns _END to approve & finish, _STOP for the non-default Stop choice,
    menu cancellation, or a failed input boundary (clean exit), _ITERATE to hand
    the reviewer's unresolved findings back to the role, or the custom feedback
    text."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        if preview is not None:
            choices = [
                ("iterate", _preview_dissent_iterate_label(preview)),
                ("tell", _preview_dissent_tell_label(preview)),
                ("approve", _preview_dissent_approve_label(preview)),
                ("stop", _preview_stop_label(preview))]
        else:
            choices = [
                ("iterate", "Keep iterating on the reviewer's feedback"),
                ("tell", "Tell it what to do"),
                ("approve", "Approve & finish anyway")]
        choice = ui.select(
            "Reviewer still requests changes — what now?", choices,
            io_in=io_in, io_out=io_out, gate="review_dissent",
            on_discard=on_discard, on_drain_fail=on_drain_fail)
        if choice is ui.DRAIN_FAILED:
            return _STOP
        if choice == "approve":
            return _END
        if choice == "stop" or (choice is None and preview is not None):
            # Questionary cancellation/Ctrl-C follows the explicit Stop path
            # on every preview-enabled real-flow gate.
            return _STOP
        if choice == "tell":
            fb = ui.prompt_user(io_in, io_out, header="Your instructions",
                                gate="review_dissent_tell",
                                on_discard=on_discard,
                                on_drain_fail=on_drain_fail)
            if fb is ui.DRAIN_FAILED:
                return _STOP
            if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                return _ITERATE
            return fb
        # 'iterate' (or a dismissed legacy preview-less select): the safe
        # non-approving default.
        return _ITERATE
    line = io_in.readline()
    if line == "" or line.strip() == "":
        return _END
    return line.rstrip("\n")


def _dissent_suffix(verdict):
    """A short, user-visible note attached to the review gate when the reviewer's
    concerns were not resolved within the round cap."""
    header = ("\nreview cap reached (%d rounds) — reviewer still requests "
              "changes; you are the tiebreaker." % REVIEW_ROUND_CAP)
    findings = (verdict or {}).get("findings") or []
    if not findings:
        # No specific findings (e.g. a missing/unreadable review): still tell the
        # user the reviewer did not sign off, rather than implying a clean pass.
        return header + "\nreviewer's unresolved notes:\n  - reviewer did not " \
               "approve within the review round cap."
    return header + "\nreviewer's unresolved notes:\n" + "\n".join(
        "  - " + str(f) for f in findings)


# Returned by the stuck-gate reader (the visible escalation shown when an
# automatic repair turn also fails to change the status artifact).
_STUCK_RETRY = object()
_STUCK_INSPECT = object()
_STUCK_END = object()
_STUCK_SWITCH = object()

_CTRL_RETRY = object()
_CTRL_SWITCH = object()
_CTRL_END = object()


def _repair_prompt(artifact_noun):
    """The firm, role-parameterized instruction sent on the single automatic
    repair turn (and on a user-driven stuck-gate retry). It tells the role that
    its status artifact did not change on disk and that the harness gates on the
    literal on-disk `status` field, not on what the role claims in chat."""
    return (
        "Your last turn reopened work, but the %s status file was NOT changed "
        "on disk — its raw bytes are byte-identical to before your turn. The "
        "cowork harness gates strictly on that file's literal top-level "
        "`status` field, never on what you write in chat. Rewrite the %s "
        "status artifact NOW: address the reopened work and set the correct "
        "`status` (`needs_input` if you still need an answer, "
        "`ready_for_review` once the work is complete). Writing the file is "
        "mandatory — a chat-only reply will be treated as no progress."
        % (artifact_noun, artifact_noun))


def _headless_nudge_text(artifact_noun):
    """The canned directive re-sent to a LEAD that set needs_input under
    --headless (F2): no human is available, so proceed on the best assumption
    and drive to ready_for_review. Bounded by the existing stale-no-op/stuck
    handling and a headless nudge cap so it can never hang."""
    return (
        "This session is running in headless mode — there is NO human available "
        "to answer questions. Do not wait for input. Choose the most reasonable "
        "interpretation of the open question, record it explicitly in your "
        "status artifact's result.assumptions, complete the work, and set the "
        "%s status to ready_for_review. If the work genuinely cannot proceed, "
        "make your best effort and still move to ready_for_review with the "
        "assumption recorded — never leave the status at needs_input."
        % artifact_noun)


def _switch_option_text(eligible):
    """The switch clause of a recovery-gate banner.

    `eligible=None` (this session carries no policy) reproduces today's wording
    byte-for-byte. A list names the specific controllers this session still
    permits; an EMPTY list means the policy leaves no target at all, so the
    switch option is dropped and the reason is stated instead."""
    if eligible is None:
        return "switch-controller (move this role to the alternate controller)"
    if not eligible:
        return None
    return ("switch-controller (move this role to %s)"
            % " or ".join(eligible))


def _no_eligible_note(eligible):
    if eligible is None or eligible:
        return ""
    return ("\nswitching controllers is not offered: this session's controller "
            "policy leaves no other controller available.")


def _choice_clause(options):
    """'a, b, or c' — the historical phrasing of every recovery-gate banner."""
    if len(options) == 1:
        return options[0]
    return ", ".join(options[:-1]) + ", or " + options[-1]


def _stuck_gate_text(status_path, role, enabled=False, eligible=None):
    """The banner shown at the visible stuck gate. With `eligible=None` (a
    session with no controller policy) the text is byte-identical to what it has
    always been."""
    options = ["retry (run it once more)"]
    switch = _switch_option_text(eligible)
    if switch:
        options.append(switch)
    options.append("inspect (show the status file)")
    options.append("end (end this phase cleanly)")
    return (
        "the %s appears stuck — it reopened work but its status file did not "
        "change across an automatic repair attempt.\n  status file: %s\n"
        "choose: %s.%s" % (role, ui.render_path(status_path, enabled),
                           _choice_clause(options), _no_eligible_note(eligible)))


def _controller_failure_text(role, controller, reason, alert=None,
                             eligible=None):
    """With `eligible=None` (no controller policy) the text is byte-identical to
    what it has always been."""
    options = ["retry (try %s again)" % controller]
    switch = _switch_option_text(eligible)
    if switch:
        options.append(switch)
    options.append("end (end this phase cleanly)")
    text = (
        "the %s controller for %s cannot make progress (%s).\n"
        "choose: %s.%s"
        % (controller, role, reason, _choice_clause(options),
           _no_eligible_note(eligible)))
    if alert:
        text += "\n\n" + str(alert)
    return text


def _emit_stuck_inspect(io_out, status_path):
    """Print the diagnostic for the stuck-gate `inspect` action: the artifact
    path, its current on-disk status field, and the raw file content. Read-only
    — never runs the role."""
    io_out.write("status file: %s\n" % ui.render_path(
        status_path, ui.is_tty(io_out)))
    io_out.write("on-disk status: %s\n" % state_store.read_status(status_path))
    try:
        with open(status_path, "r") as fh:
            content = fh.read()
    except OSError:
        content = "<missing or unreadable>"
    io_out.write(content)
    if not content.endswith("\n"):
        io_out.write("\n")
    io_out.flush()


def _eligible_switch_choices(eligible, label_fmt):
    """The per-controller switch entries for a policy-aware gate select."""
    return [("switch-controller:%s" % name, label_fmt % name)
            for name in eligible]


def _eligible_switch_token(token, eligible):
    """Resolve an off-TTY gate token into a `_SwitchTo`, or None.

    `switch <name>` and `switch-controller=<name>` name a target explicitly;
    a bare `switch` picks the first eligible controller."""
    if not eligible:
        return None
    parts = token.replace("=", " ").split()
    if not parts or parts[0] not in ("switch", "switch-controller"):
        return None
    if len(parts) == 1:
        return _SwitchTo(eligible[0])
    if parts[1] in eligible:
        return _SwitchTo(parts[1])
    return None


def _read_stuck_gate(io_in, io_out, eligible=None,
                     on_discard=None, on_drain_fail=None):
    """Read the stuck-gate choice. On a TTY a questionary select; off a TTY a
    readline where `retry`/`inspect` map to those actions and anything else
    (including blank/EOF) ends the phase — the safe terminating default so a
    scripted/test path is never trapped at the gate.

    With `eligible=None` (a session with no controller policy) the choices and
    the return sentinels are exactly what they have always been: one opaque
    switch option returning `_STUCK_SWITCH`. With a list, one option per
    eligible controller returning `_SwitchTo(target)`; with an EMPTY list the
    switch option is absent entirely.

    A failed input boundary ends the phase cleanly (`_STUCK_END`), exactly like
    EOF.

    Returns `_STUCK_RETRY`, `_STUCK_INSPECT`, `_STUCK_END`, `_STUCK_SWITCH`, or
    a `_SwitchTo`."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        choices = [("retry", "Run it once more")]
        if eligible is None:
            choices.append(
                ("switch-controller",
                 "Move this role to the alternate controller"))
        else:
            choices += _eligible_switch_choices(
                eligible, "Move this role to %s")
        choices += [("inspect", "Show the status file"),
                    ("end", "End this phase")]
        choice = ui.select(
            "The role reopened work but didn't update its status — what now?",
            choices, io_in=io_in, io_out=io_out, gate="stuck",
            on_discard=on_discard, on_drain_fail=on_drain_fail)
        if choice is ui.DRAIN_FAILED:
            return _STUCK_END
        if isinstance(choice, str) and choice.startswith("switch-controller:"):
            return _SwitchTo(choice.split(":", 1)[1])
        return {"retry": _STUCK_RETRY, "inspect": _STUCK_INSPECT,
                "switch-controller": _STUCK_SWITCH,
                "end": _STUCK_END}.get(choice, _STUCK_END)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _STUCK_RETRY
    if eligible is None:
        if token in ("switch", "switch-controller"):
            return _STUCK_SWITCH
    else:
        picked = _eligible_switch_token(token, eligible)
        if picked is not None:
            return picked
    if token == "inspect":
        return _STUCK_INSPECT
    return _STUCK_END


def _read_controller_failure_gate(io_in, io_out, eligible=None,
                                  on_discard=None, on_drain_fail=None):
    """Read the controller-failure gate choice.

    Off a TTY, only explicit retry/switch tokens continue; blank/EOF keeps the
    historical safe terminating behavior. A failed input boundary takes the same
    safe terminating exit (`_CTRL_END`). `eligible` follows the same three-way
    contract as `_read_stuck_gate`.
    """
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        choices = [("retry", "Try the same controller again")]
        if eligible is None:
            choices.append(
                ("switch-controller",
                 "Move this role to the alternate controller"))
        else:
            choices += _eligible_switch_choices(
                eligible, "Move this role to %s")
        choices.append(("end", "End this phase"))
        choice = ui.select("The controller cannot continue — what now?",
                           choices, io_in=io_in, io_out=io_out,
                           gate="controller_failure",
                           on_discard=on_discard, on_drain_fail=on_drain_fail)
        if choice is ui.DRAIN_FAILED:
            return _CTRL_END
        if isinstance(choice, str) and choice.startswith("switch-controller:"):
            return _SwitchTo(choice.split(":", 1)[1])
        return {"retry": _CTRL_RETRY, "switch-controller": _CTRL_SWITCH,
                "end": _CTRL_END}.get(choice, _CTRL_END)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _CTRL_RETRY
    if eligible is None:
        if token in ("switch", "switch-controller"):
            return _CTRL_SWITCH
    else:
        picked = _eligible_switch_token(token, eligible)
        if picked is not None:
            return picked
    return _CTRL_END


# Returned by the reviewer-failure gate reader (the visible escalation shown when
# the paired reviewer/advisor fails to return a usable verdict REVIEW_FAIL_CAP
# times running — an account limit, a crash, or an empty/garbled write — distinct
# from a reviewer that legitimately keeps asking for changes, which the
# REVIEW_ROUND_CAP dissent path already handles).
_REVFAIL_RETRY = object()
_REVFAIL_SKIP = object()
_REVFAIL_END = object()
_REVFAIL_SWITCH = object()


def _is_review_failure(verdict):
    """Whether a reviewer turn produced NO USABLE verdict — the failure mode the
    reviewer-failure gate counts, as opposed to a reviewer that legitimately asks
    for changes.

    True when ANY of: the verdict is missing/empty or carries no 'verdict' key;
    its value is not one of `state_store.VALID_VERDICTS`; it is 'needs_user' with
    a blank/absent 'user_question' (cannot be relayed faithfully); or it is
    flagged `malformed` (read_review's safe-revise coercion of an
    unparseable/missing review). A genuine 'approve'/'revise'/valid 'needs_user'
    (non-blank question) is NOT a failure. Validated directly against the verdict
    contract (not the looser '(not verdict.get("verdict")) or malformed') so an
    unknown verdict value or a question-less needs_user is caught even on an
    injected/direct verdict dict that bypassed read_review."""
    if not isinstance(verdict, dict) or not verdict:
        return True
    v = verdict.get("verdict")
    if v not in state_store.VALID_VERDICTS:
        return True
    if v == "needs_user" and not str(verdict.get("user_question") or "").strip():
        return True
    if verdict.get("malformed"):
        return True
    return False


def _reviewer_fail_gate_text(reviewer_role, role, detail=None, eligible=None):
    """The banner shown at the visible reviewer-failure gate. With
    `eligible=None` (no controller policy) the text is byte-identical to what it
    has always been."""
    options = [
        "retry (run the reviewer once more)",
        "skip-review (stop reviewing for the rest of this phase and go straight "
        "to the approve/revise gate)",
    ]
    if eligible is None:
        options.append(
            "switch-controller (move the reviewer to the alternate controller)")
    elif eligible:
        options.append("switch-controller (move the reviewer to %s)"
                       % " or ".join(eligible))
    options.append("end (end this phase cleanly)")
    text = (
        "the %s could not return a usable verdict (account limit, crash, or an "
        "empty/garbled write) across %d tries — it is not reviewing the %s's "
        "work.\nchoose: %s.%s"
        % (reviewer_role, REVIEW_FAIL_CAP, role, _choice_clause(options),
           _no_eligible_note(eligible)))
    if detail:
        text += "\n\n" + str(detail)
    return text


def _controller_failure_verdict(send_result=None, alert=None):
    out = {"malformed": True, "controller_failure": True}
    if send_result:
        out["controller_failure_result"] = dict(send_result)
    if alert:
        out["controller_failure_alert"] = alert
    return out


def _read_reviewer_fail_gate(io_in, io_out, eligible=None,
                             on_discard=None, on_drain_fail=None):
    """Read the reviewer-failure-gate choice. On a TTY a questionary select; off
    a TTY a readline where `retry`/`end` map to those actions and anything else
    (including blank/EOF) skips the review — the safe default so a
    scripted/test path is never trapped AND a broken reviewer never blocks a
    headless run (skip-review then reaches the user gate, which off a TTY reads
    blank=approve, preserving the historical 'scripted runs complete' contract).

    `eligible` follows the same three-way contract as `_read_stuck_gate`.

    A failed input boundary ends the phase cleanly (`_REVFAIL_END`) — never
    `_REVFAIL_SKIP`, which would advance the work past a review that never
    happened.

    Returns `_REVFAIL_RETRY`, `_REVFAIL_SKIP`, `_REVFAIL_END`,
    `_REVFAIL_SWITCH`, or a `_SwitchTo`."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        choices = [
            ("retry", "Run the reviewer once more"),
            ("skip-review", "Skip review for this phase — go to approve/revise"),
        ]
        if eligible is None:
            choices.append(
                ("switch-controller",
                 "Move the reviewer to the alternate controller"))
        else:
            choices += _eligible_switch_choices(
                eligible, "Move the reviewer to %s")
        choices.append(("end", "End this phase"))
        choice = ui.select(
            "The reviewer isn't returning a usable verdict — what now?",
            choices, io_in=io_in, io_out=io_out, gate="reviewer_failure",
            on_discard=on_discard, on_drain_fail=on_drain_fail)
        if choice is ui.DRAIN_FAILED:
            return _REVFAIL_END
        if isinstance(choice, str) and choice.startswith("switch-controller:"):
            return _SwitchTo(choice.split(":", 1)[1])
        return {"retry": _REVFAIL_RETRY, "skip-review": _REVFAIL_SKIP,
                "switch-controller": _REVFAIL_SWITCH,
                "end": _REVFAIL_END}.get(choice, _REVFAIL_SKIP)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _REVFAIL_RETRY
    if eligible is None:
        if token in ("switch", "switch-controller"):
            return _REVFAIL_SWITCH
    else:
        picked = _eligible_switch_token(token, eligible)
        if picked is not None:
            return picked
    if token == "end":
        return _REVFAIL_END
    return _REVFAIL_SKIP


def _read_handoff_confirm(io_in, io_out, prompt="Hand the work back to the scout?",
                          on_discard=None, on_drain_fail=None):
    """The hand-back confirmation gate. On a TTY an explicit questionary
    confirm (with the role-appropriate `prompt`); off a TTY a readline where
    blank/y/yes confirms (mirrors the blank=approve contract of `_read_review`
    for the scripted/test path).

    The confirm keeps default=True: 'yes' hands work back to the scout, which is
    consequential but is not an approval. A failed input boundary declines."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        confirmed = ui.confirm(prompt, io_in=io_in, io_out=io_out,
                               gate="handoff_back", on_discard=on_discard,
                               on_drain_fail=on_drain_fail)
        if confirmed is ui.DRAIN_FAILED:
            return False
        return confirmed
    line = io_in.readline()
    return line.strip().lower() in ("", "y", "yes")


def _role_loop(session, first, status_path, context, io_in, io_out,
               role="scout", review_fn=None, trace=None,
               reviewer_role=SCOUT_REVIEWER,
               needs_input_text=scout_needs_input_text,
               review_text=scout_review_text,
               done_text=scout_done_text,
               artifact_noun="intel",
               handoff_enabled=False, handoff_confirm=None,
               handoff_gate_text_fn=handoff_gate_text,
               handoff_confirm_prompt="Hand the work back to the scout?",
               handoff_declined_text_fn=handoff_declined_text,
               evaluate_fn=None, skip_baseline=None, context_revision=None,
               phase=None, is_resume=False, seed_artifact_paths=None,
               on_first_send_accepted=None, on_first_send_rejected=None,
               headless=False,
                 review_allow_ask=True, gate_preview=None,
                  require_pending_question=False, review_path=None,
                  save_pending_turn_fn=None, clear_pending_turn_fn=None,
                  spath=None, session_uuid=None, build_summary_path=None,
                  role_work_id=None):
    """Drive a user-facing role's per-turn loop: send → read status → prompt,
    gate, or finish. Role-generic: the scout and the planner both run on this
    loop, differing only in banners, status file, paired reviewer, and whether
    the hand-back contract is enabled.

    Returns `(rc, outcome, payload)` where outcome is one of:
      - "approved": the user approved at the `ready_for_review` gate.
      - "ended": EOF/Ctrl-D or /quit ended the conversation.
      - "interrupted": Ctrl-C.
      - "handoff": the role signaled `handoff_back` with a payload and the
        user CONFIRMED the gate; `payload` carries the handoff note.
      - "switch_controller": a recovery gate asked the caller to switch the
        active role; `payload` carries role/reason/pending-turn metadata.

    A blank line re-prompts. When `review_fn` is provided (the paired reviewer
    is on the team), each `ready_for_review` first runs the reviewer (topology
    D) BEFORE the user gate: `review_fn(status_path, round_index)` returns a
    verdict dict {verdict, findings, user_question}. The reviewer is bounded by
    REVIEW_ROUND_CAP rounds, after which cowork falls through to the user with
    the reviewer's dissent attached. The reviewer never writes to the user
    channel; only the content-free `reviewed` marker and the role's own replies
    appear.

    When `evaluate_fn(session, verdict, round_index)` is provided, it runs
    right after each verdict readback and BEFORE branching on the verdict kind
    — one seam that covers approve, revise, needs_user, and round-cap rounds
    identically. It is purely observational: failures are traced and skipped,
    and the only user-visible sign is a content-free 'Handoff in progress'
    spinner (no-op off a TTY).

    When `handoff_enabled`, a `handoff_back` status with a payload shows the
    user confirmation gate: confirmed → the loop returns the "handoff" outcome;
    declined → the status is downgraded and the role continues with a declined
    note. A `handoff_back` without a payload degrades to the needs-input gate
    (never an implicit hand-back)."""
    # `_role_loop` is the actual initial lead boundary. Production callers
    # already pass a typed seed; direct/test callers enter through this one
    # closed initial-user constructor rather than a generic lead fallback.
    pending = (first if isinstance(
        first, (handoff.DeliveryEnvelope, handoff.HandoffBlock))
               else _initial_user_delivery(first))
    first = pending
    # E-MJ2-UNBOUND-FIRST-SEND: bound here, BEFORE the `try:` below, so an
    # interrupt landing during one of the pre-first-send I/O seams inside it
    # (the `you: ...` echo, `read_status`/`invalidate_ready_status`,
    # `fingerprint_status`) -- all of which run before the loop body's own
    # `first_send = pending is first` at the top of its first iteration --
    # never reaches the `except KeyboardInterrupt` handler with `first_send`
    # unbound. `pending is first` is trivially True here (identical object,
    # just assigned above), matching the loop's own computation for this
    # same first iteration exactly, so this is not a distinct value, only an
    # earlier binding of the same one.
    first_send = True
    # The event that caused the pending reopen, so the resulting
    # status.invalidated can name its cause (P16).
    pending_reopen_event_id = None
    pending_reopens_work = False
    # A source-tagged reason set at every work-reopening site (one of
    # 'user_revise'/'user_iterate'/'user_answer'/'reviewer_needs_user'/
    # 'reviewer_revise'/'handoff_declined'). Detection keys off this being set —
    # NOT off `pending_reopens_work` — because the handoff-declined branch
    # invalidates inline and never sets the boolean, yet is still a reopen the
    # stale-no-op detector must cover (the general invariant, D1/D9).
    pending_reopen_reason = None
    # Transient flag set at the review gate's "Ask a question" branch and
    # consumed at the loop top: a user question is a NON-reopen turn (it never
    # sets pending_reopen_reason), so it is tagged for per-turn accounting here
    # without tripping the invalidate / stale-no-op / baseline machinery.
    pending_user_question = False
    # Stale-no-op repair state: True between the firing of the single automatic
    # repair turn and its result (or a user-driven stuck-gate retry). When True,
    # the next send is re-checked for a no-op even without a fresh reopen.
    in_repair = False
    repair_reason = None  # reopen reason carried into the repair/escalation
    repair_ordinal = 0  # increments each time _repair_delivery is actually dispatched
    send_start_event_id = None  # trace event ID from the most recent role.send.start
    last_send_source_ref = None  # source_ref built for the most recent send
    review_rounds = 0
    # Consecutive reviewer turns with no usable verdict (reset by a usable
    # verdict or by user re-engagement); once `skip_review` is set at the
    # reviewer-failure gate it stays set for the rest of this phase, bypassing
    # the reviewer straight to the user gate.
    review_failures = 0
    skip_review = False
    # Headless needs_input nudges fired this phase (HEADLESS_NUDGE_CAP backstop).
    headless_nudges = 0
    # A real wrapper may require needs_input artifacts to record the question.
    # One automatic repair is enough; a second malformed turn becomes an
    # explicit diagnostic at the input gate instead of an invisible loop.
    missing_question_repairs = 0
    outcome_kind = _OUTCOME_ENDED
    payload = None

    def _breaker_decision(reason):
        """Ask D's durable recovery breaker whether ANOTHER attempt at this
        exact cause (role, config_digest, controller, candidate, reason) is
        still permitted, atomically recording this attempt against durable
        history first. Returns the breaker's decision dict, or None when
        there is no session/proven manifest to key a genuine fingerprint on
        -- the breaker is silent rather than fabricating one. Keyed on the
        SAME manifest digest `_complete_phase` binds as this WorkUnit's
        candidate, so the breaker's cause identity and the WorkUnit's
        candidate identity are never two competing definitions of "which
        dispatch this is"."""
        if not session_uuid:
            return None
        manifest = dispatch_manifest.load_manifest(
            state_store.manifest_path_for(session_uuid, role))
        config_digest = ((manifest or {}).get("binding") or {}).get(
            "config_digest")
        if not config_digest:
            return None
        controller_name = getattr(session, "controller", None) or "unknown"
        candidate = (manifest or {}).get("digest")
        decision = recovery_breaker.attempt(
            state_store.ledger_path_for(session_uuid), role, config_digest,
            controller_name, candidate, reason)
        if trace:
            trace.event("recovery.breaker.decision", role=role, reason=reason,
                        fingerprint=decision["fingerprint"],
                        attempt_count=decision["attempt_count"],
                        threshold=decision["threshold"],
                        tripped=decision["tripped"])
        return decision

    try:
        # Controller-switch packets are controller-only recovery context.  The
        # switch itself already has a compact user-facing status line; echoing
        # this packet here would dump artifacts and orchestration markup into
        # the terminal as though the user had written it.
        internal_switch_context = context.lstrip().startswith(
            handoff.SWITCH_HANDOFF_MARKER)
        if context.strip() and not internal_switch_context:
            io_out.write(ui.label("you", ui.is_tty(io_out)) + context.strip() + "\n")
            io_out.flush()
        while True:
            # Capture the reopen signal BEFORE the invalidate/reset block runs.
            reopened_this_turn = pending_reopen_reason is not None
            reopen_reason_this_turn = pending_reopen_reason
            # Consume the transient user-question flag (a non-reopen turn).
            question_turn = pending_user_question
            pending_user_question = False
            if pending_reopens_work:
                before_status = state_store.read_status(status_path)
                changed = state_store.invalidate_ready_status(status_path)
                after_status = state_store.read_status(status_path)
                if trace and before_status != after_status:
                    # Emitted ONLY when the observed state actually moved
                    # (CV-016). The event used to fire whenever invalidation was
                    # ATTEMPTED, so a no-op invalidation of an already-correct
                    # status read as a real transition. `requested_status` is the
                    # transition asked for; `before`/`after`/`changed` are what
                    # was observed, and they are deliberately distinct.
                    trace.event("status.invalidated", role=role,
                                path=status_path, changed=changed,
                                requested_status="needs_input",
                                before=before_status, after=after_status,
                                reason="work_reopened",
                                triggering_event_id=pending_reopen_event_id)
                pending_reopens_work = False
            pending_reopen_reason = None
            pending_reopen_event_id = None
            if role == "builder" and not reopened_this_turn:
                # A fresh builder turn that is not a reopen is editing work.
                record_milestone(trace, role, "editing", review_rounds)
            fp_before = state_store.fingerprint_status(status_path)
            # Per-turn accounting (#1/D11): classify this lead send and attach the
            # status-artifact descriptor + context revision. The reopen reason
            # (set at every work-reopening site) keys the kind; the very first
            # send is the role seed.
            if in_repair:
                lead_kind = "repair"
            elif reopen_reason_this_turn in (
                    "reviewer_needs_user", "reviewer_revise"):
                lead_kind = "reviewer_handoff"
            elif reopen_reason_this_turn == "handoff_declined":
                lead_kind = "handoff_wake"
            elif reopen_reason_this_turn:
                lead_kind = "user_answer"
            elif question_turn:
                lead_kind = "user_question"
            elif pending is first:
                lead_kind = "role_seed"
            else:
                lead_kind = "role_turn"
            # The seed prompt references the upstream artifact(s) (planner:
            # approved intel; builder: approved plan) path-first on the first
            # send only (#1 — the bodies are read from disk, not embedded);
            # every send also touches the role's own status file (its write
            # target, never embedded). So no artifact body rides a lead send:
            # tag all lead artifacts path-first. fresh-vs-resume: the first send
            # of a non-resumed launch is fresh; a resumed launch and every
            # continuation turn are resume turns.
            first_send = pending is first
            delivery = _lead_turn_delivery(pending)
            lead_artifacts = (
                [dict(rec) for rec in delivery.descriptors]
                if delivery.descriptors
                else _artifact_descriptors(
                    (list(seed_artifact_paths or []) if first_send else []) + [status_path],
                    delivery="path")
            )
            lead_meta = {
                "prompt_kind": lead_kind,
                "phase": phase,
                "fresh": first_send and not is_resume,
                "resume": is_resume or not first_send,
                "context_revision": context_revision,
                "artifacts": lead_artifacts,
                # MJ-1: the genuine WorkUnit identity for this engagement --
                # threaded through so the bridge session can stamp the REAL
                # parent WorkUnit (not its own per-turn trace work_id) into
                # the guard context a child-dispatch/ungoverned-terminal
                # hook payload carries as `parent_work_id` (see
                # `cowork_bridge.py`'s `_send_turn`/`send` methods).
                "role_work_id": role_work_id,
            }
            if trace:
                trace.event("role.fingerprint.before", role=role,
                            status=fp_before["status"],
                            sha256=fp_before["sha256"],
                            size=fp_before["size"], exists=fp_before["exists"])
                send_start_event_id = trace.event(
                    "role.send.start", role=role,
                    prompt_kind=lead_kind, phase=phase,
                    fresh=lead_meta["fresh"], resume=lead_meta["resume"],
                    context_revision=context_revision,
                    artifacts=lead_meta["artifacts"],
                    **trace_store.prompt_meta(pending))
            else:
                send_start_event_id = None
            last_send_source_ref = _build_pending_source_ref(
                session, send_start_event_id, str(pending))
            send_result = _send(
                session, delivery, meta=lead_meta)
            if trace:
                trace.event("role.send.end", role=role,
                            ok=bool(send_result.get("ok", True)),
                            result=send_result.get("result"),
                            error_type=send_result.get("error_type"),
                            subtype=send_result.get("subtype"))
            fp_after = state_store.fingerprint_status(status_path)
            if trace:
                trace.event("role.fingerprint.after", role=role,
                            status=fp_after["status"], sha256=fp_after["sha256"],
                            size=fp_after["size"], exists=fp_after["exists"])
            if (not send_result.get("ok", True)
                    and fp_after["sha256"] == fp_before["sha256"]):
                if save_pending_turn_fn and pending:
                    save_pending_turn_fn(role, pending,
                                        source=last_send_source_ref)
                elif spath and pending:
                    state_store.save_pending_turn(spath, role, pending,
                                                  source=last_send_source_ref)
                if trace:
                    trace.event("controller.failure", role=role, phase=phase,
                                reason="send_failed",
                                result=send_result.get("result"),
                                error_type=send_result.get("error_type"),
                                subtype=send_result.get("subtype"),
                                artifact_progress=False)
                if headless:
                    # No human to choose retry/switch/end: a controller failure
                    # is an environment problem, so end the phase cleanly rather
                    # than show an interactive gate (F2: never block headless).
                    if trace:
                        trace.event("headless.auto", role=role,
                                    gate="controller_failure", action="end")
                    _advance_phase(
                        session_uuid, role_work_id, "execution_failed",
                        evidence={"reason": "send_failed", "headless": True},
                        source="headless.auto")
                    if first_send and on_first_send_rejected:
                        on_first_send_rejected()
                    outcome_kind = _OUTCOME_ENDED
                    break
                outcome_kind = None
                while True:
                    gate_eligible = gate_eligible_for(
                        getattr(session, "controller", None))
                    ui.banner(io_out, _controller_failure_text(
                        role, getattr(session, "controller", "configured"),
                        send_result.get("error_type")
                        or send_result.get("subtype")
                        or send_result.get("result") or "send failed",
                        eligible=gate_eligible),
                        "dissent")
                    gate_discard, gate_drain_fail = _gate_trace_callbacks(
                        trace, role)
                    action = _read_controller_failure_gate(
                        io_in, io_out, eligible=gate_eligible,
                        on_discard=gate_discard,
                        on_drain_fail=gate_drain_fail)
                    if action is _CTRL_RETRY:
                        breaker = _breaker_decision("controller_failure")
                        if breaker and breaker["tripped"]:
                            if trace:
                                trace.event(
                                    "user.action", role=role,
                                    action="controller_failure_retry_blocked",
                                    fingerprint=breaker["fingerprint"])
                            _advance_phase(
                                session_uuid, role_work_id, "execution_failed",
                                evidence={
                                    "reason": "recovery_breaker_tripped",
                                    "fingerprint": breaker["fingerprint"]},
                                source="recovery_breaker")
                            if first_send and on_first_send_rejected:
                                on_first_send_rejected()
                            outcome_kind = _OUTCOME_ENDED
                            break
                        if trace:
                            trace.event("user.action", role=role,
                                        action="controller_failure_retry")
                        outcome_kind = None
                        break
                    if action is _CTRL_SWITCH or isinstance(action, _SwitchTo):
                        if trace:
                            trace.event("user.action", role=role,
                                        action="controller_failure_switch")
                        outcome_kind = "switch_controller"
                        payload = {
                            "role": role,
                            "reason": "send_failed",
                            "pending": pending,
                            "prompt_kind": lead_kind,
                            "result": dict(send_result),
                            "target": _switch_target_of(action),
                        }
                        break
                    if trace:
                        trace.event("user.action", role=role,
                                    action="controller_failure_end")
                    _advance_phase(
                        session_uuid, role_work_id, "execution_failed",
                        evidence={"reason": "send_failed",
                                 "user_action": "controller_failure_end"},
                        source="user.action")
                    if first_send and on_first_send_rejected:
                        on_first_send_rejected()
                    outcome_kind = _OUTCOME_ENDED
                    break
                if outcome_kind in ("switch_controller", _OUTCOME_ENDED):
                    break
                continue
            if send_result.get("ok", True):
                if first_send and on_first_send_accepted:
                    on_first_send_accepted()
                    on_first_send_accepted = None
                if clear_pending_turn_fn:
                    clear_pending_turn_fn(role)
                elif spath:
                    state_store.clear_pending_switch(spath, role)
            # Stale-no-op detection: a reopened (or in-repair) turn that left the
            # status file byte-identical made no progress. Both-missing
            # (None == None) also counts as a no-op — the role never wrote.
            if (reopened_this_turn or in_repair) and (
                    fp_after["sha256"] == fp_before["sha256"]):
                if not in_repair:
                    # First no-op of the episode: one automatic, invisible
                    # repair turn (bounded — never a repair loop).
                    in_repair = True
                    repair_reason = reopen_reason_this_turn
                    if trace:
                        trace.event(
                            "stale_noop", role=role,
                            reopen_reason=reopen_reason_this_turn,
                            before_status=fp_before["status"],
                            after_status=fp_after["status"],
                            before_sha256=fp_before["sha256"],
                            after_sha256=fp_after["sha256"],
                            repair_attempted=True)
                    repair_ordinal += 1
                    _repair_link = _build_gate_repair_attempt_link(
                        role, phase, last_send_source_ref,
                        _repair_prompt(artifact_noun), repair_ordinal)
                    if session_uuid:
                        guard_broker.append_once(
                            state_store.dispatch_links_path_for(session_uuid),
                            _repair_link, key="idempotency_key")
                    if trace:
                        trace.event("dispatch.attempt_link", role=role,
                                    kind="gate_repair", ordinal=repair_ordinal,
                                    attempt_id=_repair_link["attempt_id"],
                                    idempotency_key=_repair_link["idempotency_key"])
                    pending = _repair_delivery(artifact_noun,
                                              attempt_link=_repair_link)
                    continue
                # Second consecutive no-op: the automatic repair failed. Show the
                # visible stuck gate instead of looping forever.
                if trace:
                    trace.event(
                        "stale_noop.unresolved", role=role,
                        reopen_reason=repair_reason,
                        before_status=fp_before["status"],
                        after_status=fp_after["status"],
                        before_sha256=fp_before["sha256"],
                        after_sha256=fp_after["sha256"],
                        repair_attempted=True)
                in_repair = False
                if headless:
                    # No human to choose retry/switch/inspect: the bounded
                    # nudge (the automatic repair turn) already failed, so end
                    # the phase cleanly rather than hang (F2_auto_resolve_gates).
                    if trace:
                        trace.event("headless.auto", role=role, gate="stuck",
                                    action="end")
                    _advance_phase(
                        session_uuid, role_work_id, "execution_failed",
                        evidence={"reason": "stale_noop", "headless": True},
                        source="headless.auto")
                    outcome_kind = _OUTCOME_ENDED
                    break
                gate_decision = None
                while gate_decision is None:
                    gate_eligible = gate_eligible_for(
                        getattr(session, "controller", None))
                    ui.banner(io_out, _stuck_gate_text(
                        status_path, role, ui.is_tty(io_out),
                        eligible=gate_eligible), "dissent")
                    gate_discard, gate_drain_fail = _gate_trace_callbacks(
                        trace, role)
                    action = _read_stuck_gate(io_in, io_out,
                                              eligible=gate_eligible,
                                              on_discard=gate_discard,
                                              on_drain_fail=gate_drain_fail)
                    if action is _STUCK_INSPECT:
                        if trace:
                            trace.event("user.action", role=role,
                                        action="stuck_inspect")
                        _emit_stuck_inspect(io_out, status_path)
                        continue
                    gate_decision = action
                if gate_decision is _STUCK_RETRY:
                    if trace:
                        trace.event("user.action", role=role,
                                    action="stuck_retry")
                    repair_ordinal += 1
                    _repair_link = _build_gate_repair_attempt_link(
                        role, phase, last_send_source_ref,
                        _repair_prompt(artifact_noun), repair_ordinal)
                    if session_uuid:
                        guard_broker.append_once(
                            state_store.dispatch_links_path_for(session_uuid),
                            _repair_link, key="idempotency_key")
                    if trace:
                        trace.event("dispatch.attempt_link", role=role,
                                    kind="gate_repair", ordinal=repair_ordinal,
                                    attempt_id=_repair_link["attempt_id"],
                                    idempotency_key=_repair_link["idempotency_key"])
                    pending = _repair_delivery(artifact_noun,
                                              attempt_link=_repair_link)
                    in_repair = True  # re-checked; re-shows gate if still stuck
                    continue
                if gate_decision is _STUCK_SWITCH or isinstance(
                        gate_decision, _SwitchTo):
                    if trace:
                        trace.event("user.action", role=role,
                                    action="stuck_switch")
                    outcome_kind = "switch_controller"
                    payload = {
                        "role": role,
                        "reason": "stuck",
                        "pending": _repair_prompt(artifact_noun),
                        "prompt_kind": "repair",
                        "target": _switch_target_of(gate_decision),
                    }
                    break
                # _STUCK_END: end this phase cleanly, like EOF.
                if trace:
                    trace.event("user.action", role=role, action="stuck_end")
                _advance_phase(
                    session_uuid, role_work_id, "execution_failed",
                    evidence={"reason": "stale_noop", "user_action": "stuck_end"},
                    source="user.action")
                outcome_kind = _OUTCOME_ENDED
                break
            # Progress (the file changed) — clear any repair state and proceed.
            in_repair = False
            repair_reason = None
            status = state_store.read_status(status_path)
            if trace:
                trace.event("status.read", role=role, path=status_path,
                            status=status)
            if status != "needs_input":
                missing_question_repairs = 0
            if handoff_enabled and status == "handoff_back":
                note = state_store.read_handoff(status_path)
                if trace:
                    trace.event("handoff.signal", role=role, path=status_path,
                                has_payload=bool(note))
                if note:
                    ui.banner(io_out, handoff_gate_text_fn(note), "review")
                    if headless:
                        # Headless auto-DECLINES a hand-back (D10): no human to
                        # arbitrate, and auto-executing cross-phase hand-backs
                        # could loop unbounded. Downgrade + nudge to proceed.
                        confirmed = False
                        if trace:
                            trace.event("headless.auto", role=role,
                                        gate="handoff_back", action="decline")
                    elif handoff_confirm:
                        confirmed = handoff_confirm(io_in, io_out)
                    else:
                        gate_discard, gate_drain_fail = _gate_trace_callbacks(
                            trace, role)
                        confirmed = _read_handoff_confirm(
                            io_in, io_out, handoff_confirm_prompt,
                            on_discard=gate_discard,
                            on_drain_fail=gate_drain_fail)
                    decline_event_id = None
                    if trace:
                        # The gate decision is what causes the invalidation
                        # below, so its id is the transition's referent (P16).
                        decline_event_id = trace.event(
                            "handoff.gate", role=role,
                            confirmed=bool(confirmed))
                    if confirmed:
                        outcome_kind, payload = "handoff", note
                        break
                    # Declined: downgrade the stale handoff_back so the status
                    # file cannot re-trigger the gate, then let the role
                    # continue planning.
                    decl_before = state_store.read_status(status_path)
                    changed = state_store.invalidate_ready_status(
                        status_path, from_status="handoff_back")
                    decl_after = state_store.read_status(status_path)
                    if trace and decl_before != decl_after:
                        trace.event("status.invalidated", role=role,
                                    path=status_path, changed=changed,
                                    requested_status="needs_input",
                                    before=decl_before, after=decl_after,
                                    reason="handoff_declined",
                                    triggering_event_id=decline_event_id)
                    pending = _handoff_declined_delivery(
                        handoff_declined_text_fn)
                    # Detection keys off the reason, not the boolean: this branch
                    # invalidates inline and intentionally does not set
                    # pending_reopens_work, so the top-of-loop invalidate is not
                    # re-run, but the next send is still checked for a no-op.
                    pending_reopen_reason = "handoff_declined"
                    continue
                # Payload-less handoff_back: degrade to the needs-input gate
                # (D10) — never an implicit hand-back.
                status = "needs_input"
            if status == "ready_for_review":
                if role == "builder":
                    # The builder finished editing and is claiming its work is
                    # verified: that transition is the `verification`
                    # milestone. Scout and planner promotion happen before a
                    # candidate build or verification inventory exists.
                    record_milestone(trace, role, "verification", review_rounds)
                    # READINESS IS AN ORCHESTRATOR-OWNED GATE, NOT A SELF-
                    # REPORTED CLAIM. Cowork itself submits and runs the
                    # approved plan's verification inventory as one owned,
                    # hermetic, manifest-bound transaction here — synchronously,
                    # before the build-reviewer ever runs — rather than
                    # trusting whatever the builder ran inside its own
                    # controller turn. A red/unverified transaction invalidates
                    # readiness through the SAME hand-back mechanism as any
                    # other unverified promotion; a green one records verified
                    # readiness against the transaction's OWN captured
                    # manifest/index, never a controller-log rejoin.
                    txn_result, txn_missing_reason = (
                        _run_owned_verification_transaction(
                            session_uuid, role, review_rounds, trace,
                            work_id=role_work_id))
                    readiness = _record_readiness_from_transaction(
                        session_uuid, role, review_rounds, trace, txn_result,
                        missing_reason=txn_missing_reason)
                    # Bind this promotion to its owned receipt (D-0002): write
                    # the current-receipt pointer carrying every overlay field
                    # + the once-computed contradiction signal (D-0008) when the
                    # transaction is green and bound; record a red/unverified
                    # transaction `rejected`; abandon a prior still-pending
                    # pointer whose candidate is being replaced (D-0005).
                    _update_receipt_pointer_for_readiness(
                        session_uuid, role, review_rounds, trace, txn_result,
                        readiness, status_path,
                        summary_path=build_summary_path)
                    if readiness and readiness.get("state") == "unverified":
                        state_store.invalidate_ready_status(status_path)
                        # SAME wrap-and-hand-back mechanism as any other
                        # unverified promotion — `_unverified_readiness_
                        # delivery` is the one closed-set boundary wrapper for
                        # this text (enforced by
                        # TransportChokePointTests). A transaction-backed
                        # reason is expanded to name the transaction id
                        # BEFORE crossing that boundary, never by adding a
                        # second delivery path.
                        handback_reason = readiness.get("reason")
                        if txn_result is not None:
                            handback_reason = _owned_transaction_reason_text(
                                txn_result, handback_reason)
                        pending = _unverified_readiness_delivery(
                            handback_reason)
                        pending_reopens_work = True
                        pending_reopen_reason = "unverified_readiness"
                        pending_reopen_event_id = readiness.get("event_id")
                        ui.banner(io_out, unverified_readiness_text(
                            readiness.get("reason") or ""), "warn")
                        continue
                dissent = ""
                dissent_verdict = None
                # Hash-gate (scout + planner): when the lead's reviewed artifact
                # set is byte-identical to what the paired reviewer LAST APPROVED
                # in this phase epoch + acked context revision, skip the reviewer
                # turn entirely — reuse that approval and fall through to the
                # user gate with a visible marker (never a silent bypass, D6).
                # Only on the FIRST round of a fresh ready_for_review
                # (review_rounds == 0); a revise loop already in progress always
                # re-reviews. Latched skip_review (reviewer-failure) takes
                # precedence. The builder passes no bundle, so it never skips.
                review_skipped = False
                if (skip_baseline is not None and review_fn is not None
                        and not skip_review and review_rounds == 0):
                    composite = skip_baseline.compute_composite()
                    if skip_baseline.eligible(composite):
                        review_skipped = True
                        if trace:
                            trace.event("review.skipped", role=reviewer_role,
                                        reason="unchanged_since_approved",
                                        composite=composite)
                        ui.banner(io_out, review_skipped_text(), "info")
                # Reviewer gate (topology D): runs transparently before the user.
                # `skip_review` (latched at the reviewer-failure gate) bypasses it
                # for the rest of the phase, straight to the user gate.
                if review_fn is not None and not skip_review \
                        and not review_skipped and \
                        review_rounds < REVIEW_ROUND_CAP:
                    review_rounds += 1
                    if trace:
                        trace.event("review.round.start", role=reviewer_role,
                                    round=review_rounds,
                                    round_cap=REVIEW_ROUND_CAP)
                    # None: fall through to the user gate this round.
                    # "continue"/"end": act on the OUTER loop after the inner one.
                    review_action = None
                    # A reviewer-failure RETRY (D8) re-runs the reviewer with the
                    # path-first full-reread packet instead of a diff: a
                    # malformed/weak verdict means the diff was insufficient to
                    # judge, so the retry forces a full reread.
                    force_full_reread = False
                    # Inner loop so a reviewer-failure RETRY (and the one silent
                    # auto-retry) re-runs the reviewer in place — same round, no
                    # bounce through the role.
                    while True:
                        # The review turn streams on the internal channel (the
                        # bridge raises its own pre-first-token spinner on io_out);
                        # no outer \r-frame spinner here — it would collide with the
                        # Live region the bridge opens on the same io_out. The muted
                        # probe/eval inside the pass need no visible spinner.
                        verdict = _call_review_fn(
                            review_fn, status_path, review_rounds,
                            force_full_reread) or {}
                        if trace:
                            trace.event(
                                "review.verdict", role=reviewer_role,
                                round=review_rounds,
                                verdict=verdict.get("verdict"),
                                has_question=bool(str(
                                    verdict.get("user_question") or "").strip()),
                                findings_count=_corrective_finding_count(verdict),
                                malformed=bool(verdict.get("malformed")))
                        # No usable verdict (account limit, crash, empty/garbled
                        # write): count it. One silent auto-retry, then the gate.
                        if _is_review_failure(verdict):
                            review_failures += 1
                            if trace:
                                trace.event(
                                    "review.failure", role=reviewer_role,
                                    round=review_rounds,
                                    consecutive=review_failures,
                                    fail_cap=REVIEW_FAIL_CAP)
                            if review_failures < REVIEW_FAIL_CAP:
                                # Silent auto-retry of the reviewer (mirrors the
                                # stuck gate's one automatic repair attempt).
                                force_full_reread = True  # D8: retry full-reread
                                continue
                            if headless:
                                # No human to choose retry/skip/switch: skip
                                # review for the rest of this phase and fall
                                # through to the (auto-approving) user gate
                                # (F2_auto_resolve_gates).
                                if trace:
                                    trace.event(
                                        "headless.auto", role=role,
                                        reviewer_role=reviewer_role,
                                        gate="reviewer_failure", action="skip")
                                skip_review = True
                                review_failures = 0
                                break
                            rev_eligible = gate_eligible_for(
                                _call_reviewer_controller(review_fn))
                            ui.banner(io_out, _reviewer_fail_gate_text(
                                reviewer_role, role,
                                verdict.get("controller_failure_alert"),
                                eligible=rev_eligible),
                                "dissent")
                            gate_discard, gate_drain_fail = (
                                _gate_trace_callbacks(trace, role))
                            decision = _read_reviewer_fail_gate(
                                io_in, io_out, eligible=rev_eligible,
                                on_discard=gate_discard,
                                on_drain_fail=gate_drain_fail)
                            if decision is _REVFAIL_RETRY:
                                # Re-run the reviewer, SAME round, counter kept —
                                # re-shows the gate if it fails again.
                                if trace:
                                    trace.event("user.action", role=role,
                                                action="review_fail_retry")
                                force_full_reread = True  # D8: retry full-reread
                                continue
                            if decision is _REVFAIL_SKIP:
                                # Stop reviewing for the rest of this phase; fall
                                # through to the normal approve/revise gate.
                                if trace:
                                    trace.event("user.action", role=role,
                                                action="review_fail_skip")
                                skip_review = True
                                review_failures = 0
                                break
                            if decision is _REVFAIL_SWITCH or isinstance(
                                    decision, _SwitchTo):
                                switcher = getattr(
                                    review_fn, "switch_controller", None)
                                if switcher and _call_reviewer_switch(
                                        switcher, "reviewer_failure",
                                        _switch_target_of(decision)):
                                    if trace:
                                        trace.event(
                                            "user.action", role=role,
                                            reviewer_role=reviewer_role,
                                            action="review_fail_switch")
                                    force_full_reread = True
                                    review_failures = 0
                                    continue
                                if trace:
                                    trace.event(
                                        "user.action", role=role,
                                        reviewer_role=reviewer_role,
                                        action="review_fail_switch_failed")
                                # Re-show the failure gate if the switch could
                                # not be committed (for example the alternate
                                # CLI is missing).
                                continue
                            # _REVFAIL_END: end this phase cleanly, like EOF.
                            if trace:
                                trace.event("user.action", role=role,
                                            action="review_fail_end")
                            review_action = "end"
                            break
                        # Usable verdict: clear the failure counter and branch.
                        review_failures = 0
                        ui.banner(io_out, scout_reviewed_text(
                            verdict, review_rounds, REVIEW_ROUND_CAP), "info")
                        if evaluate_fn is not None:
                            # SEAL AND ENQUEUE ONLY — no send, no spinner, no
                            # wait (P12). Scoring used to run here as an extra
                            # turn on the role's own session, which put
                            # measurement between the reviewer's verdict and the
                            # fix going back. It now costs a file append; the
                            # queue drains at phase end.
                            try:
                                evaluate_fn(session, verdict, review_rounds)
                            except Exception:  # noqa: BLE001 - observational only
                                if trace:
                                    trace.event("eval.error", evaluator=role,
                                                round=review_rounds)
                        v = verdict.get("verdict")
                        has_question = bool(str(
                            verdict.get("user_question") or "").strip())
                        if headless and v == "needs_user" and has_question:
                            # Headless orchestrator safety-net
                            # (F2_reviewer_needs_user): no human to answer, so a
                            # reviewer's user question is downgraded to a 'revise'
                            # finding handed back to the lead — never surfaced as
                            # a user question.
                            q = str(verdict.get("user_question") or "").strip()
                            verdict = dict(
                                verdict, verdict="revise",
                                findings=list(verdict.get("findings") or [])
                                + ["(headless) reviewer raised a question with "
                                   "no human to answer — resolve it with your "
                                   "best judgment: " + q])
                            v = "revise"
                            has_question = False
                            if trace:
                                trace.event(
                                    "headless.auto", role=role,
                                    reviewer_role=reviewer_role,
                                    gate="reviewer_needs_user",
                                    action="downgrade_revise")
                        # ORCH-050 / CV-050 (D-0001/D-0004/D-0005): review
                        # dispositions for the bound owned receipt, and the
                        # mechanical supersession of defeated verification
                        # challenges. Builder + a current receipt pointer only;
                        # every other role and every no-receipt path behaves
                        # exactly as before.
                        receipt_pointer = (
                            state_store.read_current_receipt_pointer(
                                session_uuid)
                            if role == "builder" and session_uuid else None)
                        blocking_findings = []
                        defeated_challenges = []
                        if receipt_pointer and v == "revise":
                            blocking_findings, defeated_challenges = (
                                _classify_blocking_verification_challenges(
                                    verdict, receipt_pointer))
                        suppress_reopen_for_challenges = bool(
                            blocking_findings) and len(
                                defeated_challenges) == len(blocking_findings)
                        disposition_round = (
                            state_store.current_phase_round(
                                session_uuid, phase, reviewer_role,
                                default=review_rounds)
                            if session_uuid else None)
                        if v == "approve":
                            if receipt_pointer:
                                # accepted ONLY when the candidate being
                                # approved is still exactly the candidate the
                                # receipt verified (A5/D-0005); otherwise the
                                # receipt's candidate was abandoned (rejected).
                                _emit_verification_disposition(
                                    session_uuid, trace,
                                    receipt_pointer["transaction_id"],
                                    (verification.DISPOSITION_ACCEPTED
                                     if _accepted_manifest_matches(
                                         receipt_pointer)
                                     else verification.DISPOSITION_REJECTED),
                                    review_round=disposition_round,
                                    reviewed_manifest_digest=(
                                        receipt_pointer.get(
                                            "manifest_digest")))
                            # Only an explicit approve reaches the user gate.
                            review_rounds = 0
                            # Seed the hash-gate baseline so the NEXT unchanged
                            # ready_for_review skips the reviewer (D4: only a
                            # real approve seeds it). The composite is recomputed
                            # over the artifact the reviewer just approved; the
                            # record() closure updates the in-memory session
                            # state in place so a later lead-ack / phase-save
                            # cannot clobber it.
                            if skip_baseline is not None:
                                skip_baseline.record(
                                    skip_baseline.compute_composite())
                        elif v == "needs_user" and has_question:
                            review_rounds = 0
                            pending = assemble_reviewer_handoff(
                                "needs_user", verdict, artifact=artifact_noun,
                                review_path=review_path)
                            pending_reopens_work = True
                            pending_reopen_reason = "reviewer_needs_user"
                            if trace:
                                # The correction handoff is RECORDED here,
                                # strictly before anything scores this round —
                                # the ordering invariant C4 asserts. Its id is
                                # the referent for the status transition below.
                                pending_reopen_event_id = trace.event(
                                    "review.handoff",
                                    from_role=reviewer_role,
                                    to_role=role, kind="needs_user")
                                trace.event(
                                    "review.handoff.recorded", phase=phase,
                                    round=state_store.current_phase_round(
                                        session_uuid, phase, role,
                                        default=review_rounds),
                                    loop_round=review_rounds,
                                    from_role=reviewer_role,
                                    to_role=role, kind="needs_user")
                            review_action = "continue"
                        elif suppress_reopen_for_challenges:
                            # D-0004 mechanical supersession: EVERY blocking
                            # finding is an uncited-or-contradicted verification
                            # challenge against a candidate the green owned
                            # receipt certifies. The FINDINGS are superseded
                            # (recorded, never erased); the transaction
                            # SURVIVES as `pending_review` — NO builder reopen,
                            # NO review.handoff — and the fall-through user-gate
                            # outcome drives the final disposition (D-0005).
                            _record_findings(
                                session_uuid,
                                _verdict_with_superseded_challenges(
                                    verdict, defeated_challenges,
                                    receipt_pointer),
                                reviewer_role, phase, disposition_round,
                                review_path)
                            if trace:
                                trace.event(
                                    "verification.challenges_superseded",
                                    role=reviewer_role, round=review_rounds,
                                    transaction_id=receipt_pointer.get(
                                        "transaction_id"),
                                    superseded_count=len(defeated_challenges))
                        elif review_rounds < REVIEW_ROUND_CAP:
                            # A legitimate revise (reviewer wants changes): hand
                            # back to the role for another pass. A VALID
                            # blocking finding invalidates the green transaction
                            # (D-0005): superseded_by_finding, NEVER accepted.
                            if receipt_pointer and blocking_findings:
                                _emit_verification_disposition(
                                    session_uuid, trace,
                                    receipt_pointer["transaction_id"],
                                    verification
                                    .DISPOSITION_SUPERSEDED_BY_FINDING,
                                    review_round=disposition_round,
                                    reviewed_manifest_digest=(
                                        receipt_pointer.get(
                                            "manifest_digest")))
                            pending = assemble_reviewer_handoff(
                                "revise", verdict, artifact=artifact_noun,
                                review_path=review_path)
                            pending_reopens_work = True
                            pending_reopen_reason = "reviewer_revise"
                            # Sent back for changes: whatever the role does
                            # next is REPAIR, not fresh editing, which is what
                            # makes "how much did this build spend on rework"
                            # answerable.
                            record_milestone(trace, role, "repair",
                                             review_rounds)
                            # Every corrective finding gets its id HERE, from
                            # the one writer (P3). Identity across rounds cannot
                            # be reconstructed afterwards — "is this the same
                            # finding as last round?" is only answerable while
                            # both are in hand.
                            _record_findings(session_uuid, verdict,
                                             reviewer_role, phase,
                                             state_store.current_phase_round(
                                                 session_uuid, phase,
                                                 reviewer_role,
                                                 default=review_rounds),
                                             review_path)
                            if trace:
                                trace.event(
                                    "review.handoff.recorded", phase=phase,
                                    round=state_store.current_phase_round(
                                        session_uuid, phase, role,
                                        default=review_rounds),
                                    loop_round=review_rounds,
                                    from_role=reviewer_role,
                                    to_role=role, kind="revise")
                                pending_reopen_event_id = trace.event(
                                    "review.handoff", from_role=reviewer_role,
                                    to_role=role, kind="revise")
                            review_action = "continue"
                        else:
                            # Round cap reached on a legitimate revise: fall
                            # through to the user with the dissent attached (D5).
                            # The unresolved blocking finding still invalidates
                            # the green transaction (D-0005): the gate cannot
                            # later accept it.
                            if receipt_pointer and blocking_findings:
                                _emit_verification_disposition(
                                    session_uuid, trace,
                                    receipt_pointer["transaction_id"],
                                    verification
                                    .DISPOSITION_SUPERSEDED_BY_FINDING,
                                    review_round=disposition_round,
                                    reviewed_manifest_digest=(
                                        receipt_pointer.get(
                                            "manifest_digest")))
                            dissent = _dissent_suffix(verdict)
                            dissent_verdict = verdict
                            review_rounds = 0
                            if trace:
                                trace.event("review.round_cap",
                                            role=reviewer_role,
                                            round_cap=REVIEW_ROUND_CAP)
                        break
                    if review_action == "continue":
                        continue
                    if review_action == "end":
                        _advance_phase(
                            session_uuid, role_work_id, "execution_failed",
                            evidence={"reason": "reviewer_failure_end"},
                            source="user.action")
                        outcome_kind = _OUTCOME_ENDED
                        break
                if trace:
                    trace.event("gate.show", role=role,
                                gate="ready_for_review", path=status_path,
                                has_dissent=bool(dissent))
                ui.banner(io_out,
                          review_text(status_path, ui.is_tty(io_out)) + dissent,
                          "dissent" if dissent else "review")
                if headless:
                    # Headless auto-approves the ready_for_review user gate
                    # (F2_auto_resolve_gates). At the review round cap the dissent
                    # was already attached to the banner above and traced, so the
                    # unresolved dissent is recorded as the work is accepted and
                    # the phase advances (F2_consensus_and_cap).
                    if trace:
                        trace.event("headless.auto", role=role,
                                    gate="ready_for_review", action="approve",
                                    has_dissent=bool(dissent))
                    outcome = _END
                elif dissent:
                    gate_discard, gate_drain_fail = _gate_trace_callbacks(
                        trace, role)
                    outcome = _read_review_dissent(io_in, io_out,
                                                   preview=gate_preview,
                                                   on_discard=gate_discard,
                                                   on_drain_fail=gate_drain_fail)
                else:
                    gate_discard, gate_drain_fail = _gate_trace_callbacks(
                        trace, role)
                    outcome = _read_review(io_in, io_out,
                                           allow_ask=review_allow_ask,
                                           preview=gate_preview,
                                           on_discard=gate_discard,
                                           on_drain_fail=gate_drain_fail)
                if outcome is _STOP:
                    # The explicit non-default Stop choice (TTY only): a clean
                    # exit — no approval, no revision turn, no done banner. This
                    # mirrors the off-TTY 'end' path, so run_flow never advances
                    # the phase and the saved (resumable) session is left intact.
                    if trace:
                        trace.event("user.action", role=role, action="stop",
                                    gate="ready_for_review")
                    _advance_phase(
                        session_uuid, role_work_id, "cancelled",
                        evidence={"reason": "user_stop",
                                 "gate": "ready_for_review"},
                        source="user.action")
                    outcome_kind = _OUTCOME_ENDED
                    break
                if outcome is _ITERATE:
                    # Hand the reviewer's unresolved findings straight back to
                    # the role — the user shouldn't have to retype them.
                    pending = assemble_reviewer_handoff(
                        "revise", dissent_verdict, artifact=artifact_noun,
                        review_path=review_path)
                    if trace:
                        trace.event("user.action", role=role,
                                    action="iterate_review",
                                    gate="ready_for_review")
                    pending_reopens_work = True
                    pending_reopen_reason = "user_iterate"
                    review_rounds = 0  # user re-engaged: fresh review budget
                    review_failures = 0  # and a fresh reviewer-failure budget
                    continue
                if outcome is _END:
                    if trace:
                        trace.event("user.action", role=role,
                                    action="approve", gate="ready_for_review")
                        trace.event("gate.show", role=role, gate="done",
                                    path=status_path)
                    # D-0004/D-0005 gate-outcome grant: an explicit human
                    # approve (or the headless auto-approve above) accepts the
                    # still-pending bound transaction while the candidate
                    # manifest equals the receipt's; a reviewer judgment
                    # already made is never second-guessed here.
                    if role == "builder":
                        _grant_gate_acceptance(session_uuid, trace)
                    ui.banner(io_out, done_text(
                        status_path, ui.is_tty(io_out)), "done")
                    if session_uuid and role_work_id:
                        _approved_manifest = dispatch_manifest.load_manifest(
                            state_store.manifest_path_for(session_uuid, role))
                        _approved_digest = (_approved_manifest or {}).get("digest")
                        if _approved_digest:
                            _complete_phase(session_uuid, role_work_id,
                                            _approved_digest,
                                            source="user.action")
                    outcome_kind = "approved"
                    break
                if (isinstance(outcome, tuple) and len(outcome) == 2
                        and outcome[0] is _ASK):
                    # "Ask a question": a NON-reopen turn. Send the question as an
                    # ordinary pending turn; leave pending_reopens_work=False and
                    # pending_reopen_reason=None so the invalidate / stale-no-op /
                    # baseline machinery never fires — the role answers in chat,
                    # the artifact stays byte-identical, and the existing
                    # hash-gate auto-skips the advisor on the unchanged follow-up.
                    question_text = outcome[1]
                    pending = _user_lead_delivery(
                        assemble_user_question(question_text, artifact_noun))
                    pending_user_question = True
                    if trace:
                        trace.event(
                            "user.action", role=role, action="question",
                            gate="ready_for_review",
                            **trace_store.prompt_meta(question_text,
                                                      prefix="input"))
                    continue
                pending = _user_lead_delivery(
                    outcome)  # revision feedback → another turn
                if trace:
                    trace.event("user.action", role=role, action="revise",
                                gate="ready_for_review",
                                **trace_store.prompt_meta(outcome, prefix="input"))
                pending_reopens_work = True
                pending_reopen_reason = "user_revise"
                review_rounds = 0  # user re-engaged: fresh review budget
                review_failures = 0  # and a fresh reviewer-failure budget
            else:
                if status == "needs_input":
                    review_rounds = 0  # role re-opened work: fresh review budget
                    pending_question = _pending_question(status_path)
                    if pending_question:
                        missing_question_repairs = 0
                    elif (require_pending_question and not headless
                          and missing_question_repairs == 0):
                        # Do not present a blank "your answer" box when the role
                        # failed its needs_input contract.  Give it one bounded
                        # repair turn to either record the exact question or
                        # finish the work and move to review.
                        missing_question_repairs = 1
                        if trace:
                            trace.event(
                                "status.invalid", role=role, path=status_path,
                                status=status,
                                reason="needs_input_without_question",
                                repair_attempted=True)
                        ui.banner(
                            io_out,
                            "%s\nNo question was recorded; asking %s to repair "
                            "its status." % (needs_input_text(), role),
                            "dissent")
                        pending = _missing_question_delivery(artifact_noun)
                        pending_reopen_reason = "missing_question"
                        continue
                    if trace:
                        trace.event("gate.show", role=role,
                                    gate="needs_input", path=status_path,
                                    has_question=bool(pending_question),
                                    missing_question_repaired=bool(
                                        missing_question_repairs))
                    gate_text = needs_input_text()
                    if pending_question:
                        gate_text += "\nquestion:\n" + pending_question
                    elif require_pending_question:
                        gate_text += (
                            "\nNo question was provided after an automatic "
                            "repair. Tell the role what to do, or type /stop "
                            "to leave this phase.")
                    ui.banner(io_out, gate_text, "needs_input")
                if headless:
                    # No human to answer: re-send the canned nudge so the role
                    # records an assumption and proceeds (F2_roles_never_block).
                    # The stale-no-op/stuck handling bounds a role that keeps
                    # re-writing the SAME status; HEADLESS_NUDGE_CAP backstops a
                    # role that keeps writing DIFFERENT needs_input each turn.
                    headless_nudges += 1
                    if headless_nudges > HEADLESS_NUDGE_CAP:
                        if trace:
                            trace.event("headless.auto", role=role,
                                        gate="needs_input", action="end",
                                        nudges=headless_nudges)
                        _advance_phase(
                            session_uuid, role_work_id, "execution_failed",
                            evidence={"reason": "headless_nudge_cap",
                                     "nudges": headless_nudges},
                            source="headless.auto")
                        outcome_kind = _OUTCOME_ENDED
                        break
                    if trace:
                        trace.event("headless.auto", role=role,
                                    gate="needs_input", action="nudge",
                                    nudges=headless_nudges)
                    pending = _headless_nudge_delivery(artifact_noun)
                    pending_reopens_work = True
                    pending_reopen_reason = "user_answer"
                    continue
                outcome = _read_turn(io_in, io_out)
                if outcome is _END:
                    if trace:
                        trace.event("user.action", role=role, action="eof")
                    _advance_phase(
                        session_uuid, role_work_id, "cancelled",
                        evidence={"reason": "eof"}, source="user.action")
                    outcome_kind = _OUTCOME_ENDED
                    break
                pending = _user_lead_delivery(outcome)
                if trace:
                    trace.event("user.action", role=role, action="answer",
                                **trace_store.prompt_meta(outcome, prefix="input"))
                pending_reopens_work = True
                pending_reopen_reason = "user_answer"
    except KeyboardInterrupt:
        if trace:
            trace.event("role.interrupted", role=role)
        # MJ-2: an interrupt on the FIRST send means the context that rode
        # it was never affirmatively delivered (the model may never have
        # received or processed it) -- the same "no accepted send this
        # invocation" fact `on_first_send_rejected` already exists to
        # report, just reached via an interrupt rather than a refused send.
        if first_send and on_first_send_rejected:
            on_first_send_rejected()
        _advance_phase(session_uuid, role_work_id, "aborted",
                       evidence={"reason": "keyboard_interrupt"},
                       source="signal")
        outcome_kind = "interrupted"
    finally:
        session.close()
        if trace:
            trace.event("role.end", role=role, result="closed")
    return 0, outcome_kind, payload


def _scout_loop(session, first, intel_path, context, io_in, io_out,
                review_fn=None, trace=None, on_outcome=None,
                evaluate_fn=None, intel_md_path=None, skip_baseline=None,
                context_revision=None, is_resume=False,
                on_first_send_accepted=None, on_first_send_rejected=None,
                headless=False,
                gate_preview=None, review_path=None,
                save_pending_turn_fn=None, clear_pending_turn_fn=None,
                session_uuid=None, role_work_id=None):
    """The scout instantiation of `_role_loop` (kept as the historical entry
    point). Returns 0; the loop outcome is reported via `on_outcome` so
    `run_flow` can chain into the planning phase on approval.

    `intel_md_path`, when given, repoints the review/done gate surfaces at the
    human-first intel markdown (mirroring the planner gate pointing at plan.md);
    the status file driving the loop stays the intel JSON. `skip_baseline` wires
    the reviewer hash-gate (see `_role_loop`)."""
    loop_kwargs = dict(
        role="scout", review_fn=review_fn, trace=trace,
        reviewer_role=SCOUT_REVIEWER, evaluate_fn=evaluate_fn,
        skip_baseline=skip_baseline, context_revision=context_revision,
        phase="scouting", is_resume=is_resume, headless=headless,
        gate_preview=gate_preview, require_pending_question=True,
        review_path=review_path, save_pending_turn_fn=save_pending_turn_fn,
        clear_pending_turn_fn=clear_pending_turn_fn)
    if intel_md_path:
        loop_kwargs["review_text"] = (
            lambda _p, en=False: scout_review_text(intel_md_path, en))
        loop_kwargs["done_text"] = (
            lambda _p, en=False: scout_done_text(intel_md_path, en))
    rc, outcome, payload = _role_loop(
        session, first, intel_path, context, io_in, io_out,
        on_first_send_accepted=on_first_send_accepted,
        on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
            session_uuid=session_uuid, role_work_id=role_work_id)
    if on_outcome:
        try:
            params = inspect.signature(on_outcome).parameters
            if (len(params) >= 2
                    or any(p.kind == p.VAR_POSITIONAL
                           for p in params.values())):
                on_outcome(outcome, payload)
            else:
                on_outcome(outcome)
        except (ValueError, TypeError):
            on_outcome(outcome)
    return rc


def make_review_fn(config, context, selected, review_path, reviewer_runner=None,
                   reviewer_resume_id=None, on_reviewer_session=None,
                   context_update=None, on_context_ack=None, trace=None,
                   reviewer_role=SCOUT_REVIEWER, phase=None,
                   eval_scratch_path=None, scores_path=None,
                   session_uuid=None, intel_path=None, planning_epoch=None,
                   consumed_upstream=None, extra_writable_dir=None,
                   surface_io_out=None, intel_md_path=None,
                   review_packet_ctx=None, switch_controller_fn=None,
                   switch_note_fn=None, on_switch_consumed=None,
                   reviewer_controller_check_fn=None,
                   evaluation_policy=None):
    """Build the `review_fn` passed to `_role_loop` when the paired reviewer
    (`reviewer_role`, default scout-reviewer) is on the team, or None when it is
    not. The closure runs one reviewer pass and returns its verdict dict.

    The reviewer is a persistent session: the first pass creates it (id captured
    and persisted via `on_reviewer_session`); every later pass — within this run
    and after a cowork resume (seeded by `reviewer_resume_id`) — resumes it.

    Context invariant: `context` must be the CURRENT session context. A fresh
    session receives it in full; a resumed session that has not acknowledged the
    current revision receives it as a `context_update` wake block on its first
    pass. After the first successful pass, `on_context_ack()` records the
    acknowledgment (and the block is not repeated on later rounds).
    `reviewer_runner` is injectable for tests.

    Peer evaluation (when `eval_scratch_path`/`scores_path`/`session_uuid` are
    wired): every pass also carries the reviewer's eval specs into the runner —
    always the reviewer->role spec, plus the once-per-phase ->scout spec (the
    approved intel JSON carried by path, read from disk at eval time) in the planning phase.
    After the runner returns, the reviewer's scratch is read back, stamped,
    and appended to the aggregate — the evaluator is never given the
    aggregate path (the scratch itself stays under the session-assets home,
    ~/.cowork/sessions/<uuid>/, overwritten per round; it is cleared before
    each eval send, not after)."""
    if reviewer_role not in selected or not review_path:
        return None
    runner = reviewer_runner or run_reviewer_once
    eval_enabled = bool(eval_scratch_path and scores_path and session_uuid)
    evaluatee = _REVIEWER_EVALUATEE.get(reviewer_role)
    # Diff-packet snapshot scope (#4): keyed by reviewer role + phase epoch +
    # context revision, stored under the session-assets dir. Only wired when both
    # present; the real runners / default run_reviewer_once accept the param,
    # test-injected runners do not (kept byte-identical).
    if consumed_upstream is None:
        consumed_upstream = _scout_consumed_upstream(
            intel_path, planning_epoch, intel_md_path)
    holder = {"resume_id": reviewer_resume_id,
              "context_update": context_update,
              "ack": on_context_ack,
              "consumed_done": consumed_upstream is None,
              "switch_note": None}

    def review_fn(artifact_path, round_index, force_full_reread=False):
        if holder["switch_note"] is None and switch_note_fn:
            holder["switch_note"] = switch_note_fn(reviewer_role)
        runner_context = context
        if holder["switch_note"]:
            runner_context = (holder["switch_note"] + "\n\n"
                              + (runner_context or "")).strip()
        if reviewer_controller_check_fn:
            alerts = reviewer_controller_check_fn(reviewer_role)
            if alerts:
                return _controller_failure_verdict(
                    {"ok": False, "result": "missing_executable",
                     "error_type": "missing_executable"},
                    alert="\n".join(alerts))

        def capture(controller, sid):
            if sid:
                holder["resume_id"] = sid
            if on_reviewer_session:
                on_reviewer_session(controller, sid)

        kwargs = {
            "resume_id": holder["resume_id"],
            "on_session": capture,
            "context_update": holder["context_update"],
        }
        if trace is not None and reviewer_runner is None:
            kwargs["trace"] = trace
        # The default scout-reviewer path calls run_reviewer_once directly, so
        # the writable-root grant is threaded through kwargs here. The planner/
        # builder real runners are closures (reviewer_runner is set) that bake
        # the grant in themselves; test runners get nothing (byte-identical).
        if reviewer_runner is None and extra_writable_dir is not None:
            kwargs["extra_writable_dir"] = extra_writable_dir
        # Surface the review turn on the internal channel. The default
        # scout-reviewer path calls run_reviewer_once directly (reviewer_runner
        # is None); the planner/builder real runners are marked surface-capable.
        # Test-injected runners are neither, so they receive no new kwarg and
        # stay byte-identical.
        if surface_io_out is not None and (
                reviewer_runner is None
                or getattr(runner, "_coplan_surface_capable", False)):
            kwargs["surface_io_out"] = surface_io_out
        # The default run_reviewer_once path carries the phase for #1 accounting
        # (the real runner closures bake their own phase in). Guarded so
        # test-injected runners stay byte-identical.
        if phase is not None and reviewer_runner is None:
            kwargs["phase"] = phase
        # The manifest preflight fence (D-manifest-fence) is inside
        # run_reviewer_once, so session_uuid must reach it not only on the
        # default path but through every real closure runner too — the same
        # surface-capable guard that already carries surface_io_out and
        # context_revision to those closures. Test-injected runners are
        # neither None nor surface-capable, so they stay byte-identical.
        if session_uuid is not None and (
                reviewer_runner is None
                or getattr(runner, "_coplan_surface_capable", False)):
            kwargs["session_uuid"] = session_uuid
        # Context-revision (#4) rides the same surface-capable guard so the
        # transport can key the persisted shared-context file by revision: the
        # default run_reviewer_once and the real runner closures accept it;
        # test-injected runners stay byte-identical (no new kwargs).
        if review_packet_ctx and (
                reviewer_runner is None
                or getattr(runner, "_coplan_surface_capable", False)):
            kwargs["context_revision"] = review_packet_ctx.get(
                "context_revision")
        specs = None
        if eval_enabled and evaluatee:
            specs = [{
                "evaluatee": evaluatee,
                "criteria": EVAL_CRITERIA[(reviewer_role, evaluatee)],
                # Route 12 through the choke point: the reviewer's own verdict
                # file and the artifact it reviewed, both path-first.
                "artifact_block": handoff.render_handoff(
                    "eval->reviewer_verdict", artifacts=[
                        {"label": "your verdict file", "path":
                         os.path.abspath(review_path), "kind": "json",
                         "source": "verdict"},
                        {"label": "the %s artifact you reviewed" % evaluatee,
                         "path": os.path.abspath(artifact_path),
                         "kind": "json" if str(artifact_path).endswith(".json")
                         else "markdown", "source": "reviewed"}]),
                "context": "review-round",
                "phase": phase, "round": round_index,
            }]
            # The consumed-upstream bundle rides only the FIRST eval turn of
            # the phase (round_index == 1). Once per phase survives a
            # resume/restart: the aggregate itself is the durable record (the
            # holder flag only covers this closure) — scoped by the phase
            # epoch, which bumps on every phase transition, so a hand-back
            # round trip (a new phase) is evaluated again even when the
            # re-approved artifact is byte-identical. The reviewer never
            # consumed the upstream artifact through its review context, so the
            # orchestrator reads it at eval time and embeds it — self-contained
            # evidence.
            if not holder["consumed_done"]:
                spec = _consumed_upstream_spec(
                    consumed_upstream, scores_path, reviewer_role, round_index,
                    session_uuid=session_uuid)
                if spec == "deduped":
                    holder["consumed_done"] = True
                elif spec:
                    spec = dict(spec, phase=phase, round=round_index)
                    specs.append(spec)
            # The specs are NOT handed to the runner. Passing them made the
            # reviewer score its own round on its own session — an evaluation
            # turn against an operational role's controller session, which is
            # exactly the isolation violation D2 removes. They are sealed and
            # queued below instead, and an isolated evaluator runs them at
            # phase end.
            kwargs["eval_scratch_path"] = eval_scratch_path
        verdict = runner(config, runner_context, selected, artifact_path,
                         review_path, **kwargs)
        if specs:
            if len(specs) > 1:
                holder["consumed_done"] = True
            _enqueue_reviewer_eval(
                specs, eval_scratch_path, scores_path, session_uuid,
                reviewer_role, phase, round_index, review_path,
                artifact_path, verdict, trace=trace,
                evaluation_policy=evaluation_policy)
        if verdict is not None:
            # The reviewer ran against the current context: acknowledge the
            # revision once and stop repeating the wake block.
            holder["context_update"] = None
            if holder["ack"]:
                holder["ack"]()
                holder["ack"] = None
            if holder["switch_note"] and not _is_review_failure(verdict):
                if on_switch_consumed:
                    on_switch_consumed(reviewer_role)
                holder["switch_note"] = None
        return verdict

    def switch_review_controller(reason="reviewer_failure", target=None):
        if not switch_controller_fn:
            return False
        # `target` is passed only when the gate named one, so an injected
        # switch double keeping the historical (role, reason, source) signature
        # is still callable.
        extra = {"target": target} if target is not None else {}
        ok = switch_controller_fn(reviewer_role, reason=reason, source="gate",
                                  **extra)
        if not ok:
            return False
        holder["resume_id"] = None
        holder["context_update"] = None
        holder["ack"] = None
        holder["switch_note"] = switch_note_fn(
            reviewer_role) if switch_note_fn else None
        return True

    review_fn.switch_controller = switch_review_controller
    # Read live (not snapshotted): a gate switch rewrites config[reviewer_role],
    # and the next gate must offer eligible targets for the NEW controller.
    review_fn.reviewer_controller = (
        lambda: (config.get(reviewer_role) or {}).get("controller"))

    return review_fn


def _file_snapshot(path):
    """Read-only content snapshot of one real file: `{"path", "sha256"}`.

    `None` means the fact itself is not applicable to this dispatch (no
    candidate/guard file governs it). A non-null `path` with `sha256=None`
    means the fact IS applicable but the file does not exist/is unreadable
    right now — still real, distinguishable evidence, never fabricated."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        digest = None
    return {"path": path, "sha256": digest}


def _capability_evidence_covered(requested, existing):
    """True when `existing` capability already declares (with matching
    adapter evidence) every action class `requested` declares.

    Binding-only staleness (manifest_is_stale) cannot see capability at all,
    so a role-generic gate that compiles first with no evidence (e.g. the
    pre-launch check) would otherwise "prove" a manifest a later,
    evidence-owning call (e.g. run_builder's real git evidence) would then
    silently reuse as-is — losing the very evidence the later call declared.
    This is intentionally ASYMMETRIC/monotonic: a request that declares LESS
    than what is already proven still reuses the existing (fuller) proof
    unchanged — it never downgrades a persisted capability — while a request
    that declares evidence the existing capability lacks forces a fresh
    compile so that evidence is actually captured and preflighted."""
    existing = existing or {}
    requested_classes = set(requested.get("action_classes") or [])
    existing_classes = set(existing.get("action_classes") or [])
    if not requested_classes <= existing_classes:
        return False
    requested_adapters = requested.get("command_adapters") or {}
    existing_adapters = existing.get("command_adapters") or {}
    for key, value in requested_adapters.items():
        if existing_adapters.get(key) != value:
            return False
    return True


def _compile_role_manifest(
        role, session_uuid, work_id,
        controller, mode, model, effort=None,
        instruction_paths=None,
        sessions_dir=None,
        worktree=None,
        worktree_base=None,
        policy_snapshot=None,
        guard_snapshot=None,
        candidate_snapshot=None,
        action_classes=None,
        command_adapters=None,
        force_recompile=False):
    """Compile, preflight, and persist the dispatch manifest for one role.

    `effort` joins controller/model/mode as dispatch identity (bound in both
    `config_digest` and its own binding field, so an effort change alone
    changes the digest and forces recompile/revalidation).

    `worktree` is the real git worktree this dispatch runs in (or None when
    none is active — genuinely not applicable, not missing evidence);
    `worktree_base` is the real ancestor root it was created under, added to
    `capability.runtime_roots` ONLY when a worktree is bound, so preflight's
    `check_cwd` can prove the declared worktree is a strict descendant of a
    declared runtime root. Never invents either.

    `candidate_snapshot` is a real `_file_snapshot(...)` of the artifact this
    dispatch is bound to (an upstream artifact a role builds/plans from, or
    the artifact a reviewer is reviewing) — callers that own no such artifact
    pass None (not applicable). `guard_snapshot` defaults (when the caller
    passes none) to a snapshot of this session's own pinned capability
    allowlist file, real per-session state this compiler can read without
    fabricating anything; a caller that owns a more specific guard fact may
    override it explicitly.

    `action_classes`/`command_adapters` declare real command evidence this
    exact dispatch site possesses (e.g. the git subcommand/flags cowork
    itself already ran to produce a fact this dispatch depends on) — absent
    real evidence, callers leave them empty, never a permissive constant.

    Returns (manifest, was_recompiled). Raises OSError if persist fails
    (the caller must catch this and treat it as a fence failure — fail closed).
    """
    import cowork_action_policy as _ap

    if policy_snapshot is None:
        row = _ap.CONTROLLER_CAPABILITY_MATRIX.get((controller, mode)) or {}
        policy_snapshot = {
            "delegation": row.get("delegation", "unknown"),
            "mutation_gate": row.get("mutation_gate", "none"),
        }

    inst_digests = {}
    for p in (instruction_paths or []):
        try:
            with open(p, "rb") as fh:
                inst_digests[p] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            inst_digests[p] = ""

    config_digest = hashlib.sha256(
        json.dumps({"controller": controller, "mode": mode, "model": model,
                    "effort": effort},
                   sort_keys=True).encode()).hexdigest()

    if sessions_dir:
        os.makedirs(sessions_dir, exist_ok=True)

    runtime_roots = []
    if sessions_dir:
        runtime_roots.append(sessions_dir)
    if worktree is not None and worktree_base:
        runtime_roots.append(worktree_base)

    capability = {
        "inputs": list(instruction_paths or []),
        "outputs": [],
        "runtime_roots": runtime_roots,
        "private_paths": [],
        "guard_required": False,
        "socket": None,
        "kernel_boundary": {"crosses": []},
        "artifact_writes": [],
        "action_classes": list(action_classes or []),
        "command_adapters": dict(command_adapters or {}),
    }

    if guard_snapshot is None and session_uuid:
        guard_snapshot = _file_snapshot(
            state_store.capability_pins_path_for(session_uuid))

    binding = {
        "work_id": work_id,
        "controller": controller,
        "model": model,
        "effort": effort,
        "config_digest": config_digest,
        "instruction_digests": inst_digests,
        "policy_snapshot": policy_snapshot,
        "worktree": worktree,
        "candidate_snapshot": candidate_snapshot,
        "guard_snapshot": guard_snapshot,
    }

    mpath = state_store.manifest_path_for(session_uuid, work_id)
    existing = dispatch_manifest.load_manifest(mpath)
    already_proven = (
        existing is not None
        and (existing.get("status") or {}).get("phase") == "proven"
        and not dispatch_manifest.manifest_is_stale(existing, binding)
        and _capability_evidence_covered(capability, existing.get("capability"))
    )
    if already_proven and not force_recompile:
        return existing, False

    fresh = dispatch_manifest.compile_manifest(work_id, capability, binding)
    result = preflight.run_manifest_preflight(fresh)

    dispatch_manifest.persist_manifest(mpath, result)

    return result, True


# --------------------------------------------------------------------------- #
# M2 Package E: live phase-truth integration.                                 #
#                                                                              #
# WorkUnit (Package A/`cowork_workunit`) is the join key for a role's live    #
# dispatch: `_role_work_id` derives ONE stable identity per (session, role,   #
# phase-engagement epoch) and every seam below — manifest preflight, launch,  #
# gates, and terminal correlation — keys off it. `_advance_phase` is the ONE  #
# place in this file that calls A's closed reducer (`cowork_control_plane.    #
# advance`) and persists the result via B's public                           #
# `cowork_state.append_phase_state_entry`; no exit code, EOF, headless        #
# fallthrough, or status-file read ever sets `lifecycle_state` directly. A    #
# transition the reducer refuses (illegal for the current state, or missing/ #
# mismatched gate evidence) writes nothing and this helper returns the durable#
# record UNCHANGED — advancing on a bad event can never fabricate progress.   #
# --------------------------------------------------------------------------- #


def _role_work_id(session_uuid, role, epoch=None, attempt=None):
    """The stable WorkUnit identity for one (session, role) phase engagement
    attempt.

    `epoch` is the role-family's existing phase-engagement counter (the
    scouting/planning/building epoch already bumped on every hand-back round
    trip — see `bump_scouting_epoch`/`bump_planning_epoch`/
    `bump_building_epoch`), so a genuine re-engagement of the same role after
    a hand-back mints a FRESH WorkUnit rather than reusing one whose history
    may already be terminal (a terminal PhaseState record has no legal
    outbound transition — see `cowork_control_plane.TERMINAL_STATES`).

    `attempt` (M2 Package E, BL-3) is the SAME epoch's own relaunch counter
    (see `scout_attempt_box`/`planner_attempt_box`/`builder_attempt_box` in
    `run_flow`, bumped on a launch-time retry, a launch-time controller
    switch, or a mid-turn controller switch — every same-epoch `continue`
    that re-invokes `run_scout_fn`/`run_planner_fn`/`run_builder_fn` without
    a fresh hand-back): a launch-time failure can leave the epoch's WorkUnit
    terminal (`rejected_preflight`/`needs_authority`), and a mid-turn switch
    leaves it `running` -- neither state has a legal `preflight_started`
    reducer edge back to `preflighting`, so reusing that identity for the
    next attempt would silently no-op every PhaseState call for it. Folding
    `attempt` into the identity mints a fresh WorkUnit per attempt instead,
    exactly like `epoch` already does per hand-back.

    Deterministic: a resumed process re-derives the SAME work_id for an
    in-flight engagement instead of minting a second WorkUnit for it."""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        "cowork:workunit:%s:%s:%s:%s" % (
            session_uuid, role, epoch if epoch is not None else 0,
            attempt if attempt is not None else 0)))


# A resumed process re-derives `_role_work_id`'s attempt-0 identity for its
# current epoch exactly like an in-flight engagement (see `_role_work_id`'s
# own docstring) -- correct ONLY while that identity's durable PhaseState is
# still live. `scout_attempt_box`/etc (in `run_flow`) are plain in-process
# dicts with no memory across a restart, so a process that died mid-attempt
# (a launch-time retry/switch already bumped the in-memory counter past 0,
# then a real SIGTERM landed) always restarts scanning from 0 -- reusing an
# attempt whose WorkUnit the dead process already drove to
# `rejected_preflight`/`needs_authority`/`aborted`. Neither state has a
# legal `preflight_started` reducer edge back to `preflighting`
# (`cowork_control_plane.TRANSITIONS`), so every subsequent PhaseState call
# on that reused identity would silently no-op (`illegal_transition`),
# permanently losing observability into the resumed attempt (BL-3-RESIDUAL).
_MAX_ATTEMPT_SCAN = 10000


def _resolve_attempt_start(session_uuid, role, epoch):
    """The attempt number a process must begin at for (role, epoch),
    derived deterministically from durable PhaseState alone -- never
    persisted itself, so there is nothing new to keep in sync or corrupt.

    Scans attempt 0, 1, 2, ... at this epoch and returns the first one whose
    WorkUnit has no durable PhaseState yet (never minted -- a genuinely
    unused identity) or whose current PhaseState is neither terminal
    (`cowork_control_plane.TERMINAL_STATES`) nor `needs_authority` (a live,
    resumable engagement -- `pending`/`preflighting`/`running`/
    `awaiting_gate`/`blocked`). A fresh session (no session_uuid) or an
    epoch that has never been touched both resolve to 0 on the very first
    read, so this is a no-op cost for the overwhelmingly common case.

    Capped at `_MAX_ATTEMPT_SCAN` purely as a defensive bound against a
    hypothetically corrupt store wedging every attempt terminal forever; in
    that pathological case it returns the cap rather than spinning, so a
    fresh WorkUnit still eventually mints (see `_ensure_work_unit`) instead
    of the process hanging."""
    if not session_uuid:
        return 0
    attempt = 0
    while attempt < _MAX_ATTEMPT_SCAN:
        work_id = _role_work_id(session_uuid, role, epoch, attempt)
        current = state_store.current_phase_state(session_uuid, work_id)
        if current is None:
            return attempt
        state = current.get("state")
        if state not in control_plane.TERMINAL_STATES and state != "needs_authority":
            return attempt
        attempt += 1
    return attempt


def _ensure_work_unit(session_uuid, work_id, role, controller, model=None,
                      effort=None):
    """Mint (once) the WorkUnit naming this role engagement's live dispatch,
    or return the already-minted record. Idempotent: a second call for an
    already-minted work_id (a resume) returns the existing record rather
    than raising, including when this call loses a mint race against a
    sibling process for the same identity.

    `model`/`effort` are the role's OWN resolved config values (`cfg.get(
    "model")`/`cfg.get("effort")` at the call site) -- the same genuine
    identity `_guard_runtime`'s parent-identity dict threads into child-
    dispatch pinning. Never fabricated: an unconfigured value stays `None`
    on the WorkUnit exactly as it is in the role config, rather than
    inventing a placeholder string."""
    if not session_uuid or not work_id:
        return None
    existing = state_store.current_work_unit_state(session_uuid, work_id)
    if existing is not None:
        return existing
    record = {
        "schema_version": workunit.SCHEMA_VERSION, "record": "WorkUnit",
        "work_id": work_id, "session_id": session_uuid, "phase": None,
        "role": role, "seat": 0, "round": 0, "attempt": 0,
        "controller": controller or "unknown",
        "provider": controller or "unknown",
        "requested_model": model, "effective_model": None, "effort": effort,
        "candidate_manifest_digest": None, "candidate_index": None,
        "prompt_digest": None, "pending_turn_digest": None,
        "parent_work_id": None, "governed_child_policy": None,
        "graph_revision": None, "predecessor_work_ids": [],
        "fan_join_id": None,
        "lifecycle_state": "pending", "terminal_reason": None,
    }
    try:
        return state_store.mint_work_unit(record)
    except ValueError:
        return state_store.current_work_unit_state(session_uuid, work_id)


def _bind_candidate(session_uuid, work_id, candidate_manifest_digest,
                    candidate_index=None):
    """Bind the role engagement's WorkUnit to the real dispatch-manifest
    digest governing it, so a later `gate_validated` advance can name it as
    the candidate `cowork_control_plane.advance`'s fail-closed identity rule
    requires (`_validate_phase_state_args` derives `expected_candidate` from
    exactly this field on the durable WorkUnit — never from a value the
    caller merely asserts at advance time). A no-op when already bound to
    this exact identity, or when the WorkUnit was never minted."""
    if not session_uuid or not work_id or not candidate_manifest_digest:
        return None
    current = state_store.current_work_unit_state(session_uuid, work_id)
    projected = state_store.work_unit_from_history_record(current)
    if projected is None:
        return None
    if (projected.get("candidate_manifest_digest") == candidate_manifest_digest
            and projected.get("candidate_index") == candidate_index):
        return current
    projected["candidate_manifest_digest"] = candidate_manifest_digest
    projected["candidate_index"] = candidate_index
    try:
        return state_store.append_work_unit_transition(projected)
    except ValueError:
        return current


def _advance_phase(session_uuid, work_id, event, evidence=None, source=None,
                   unlocked=False):
    """The one seam every production phase advance in this file passes
    through: A's closed reducer decides the next PhaseState from the durable
    current one, and B's public persistence contract makes it durable.

    `unlocked=True` routes the durable append through B's reentrant twin
    (`cowork_state.append_phase_state_entry_unlocked`) instead of the
    locked `append_phase_state_entry` -- the ONLY safe choice for a caller
    that may run while THIS SAME PROCESS already holds this (session_uuid,
    work_id)'s PhaseState lock via a DIFFERENT fd (a `signal.signal`
    handler interrupting an in-flight locked append for the same work_id):
    flock locks attach to the open file description, not the process, so a
    second locked append here would block forever, not merely wait (B's own
    SELF-DEADLOCK banner in `cowork_state.py`). `_handle_external_kill` is
    the one production caller that sets this; it also routes the WorkUnit
    lifecycle mirror (MJ-4) through its own reentrant twin
    (`_mirror_work_unit_lifecycle_unlocked`) on this path, for the identical
    self-deadlock reason applied to the WorkUnit lock instead of the
    PhaseState lock -- see that function's docstring.

    Returns the durably persisted PhaseState record, or the unchanged
    current record when there is no session to bind to (--no-session), the
    reducer refused the transition, or a prior call already made this
    work_id's history terminal (a concurrent external kill, most often —
    `append_phase_state_entry`/`_unlocked` raises ValueError for any append
    attempted after a terminal record; that durable terminal truth is never
    overwritten or masked here)."""
    if not session_uuid or not work_id:
        return None
    current = state_store.current_phase_state(session_uuid, work_id)
    state = (current or {}).get("state", "pending")
    new_state, reason_code = control_plane.advance(state, event, evidence=evidence)
    if new_state == state and reason_code in (
            "illegal_transition", "gate_evidence_missing",
            "gate_evidence_candidate_mismatch"):
        return current
    append = (state_store.append_phase_state_entry_unlocked if unlocked
             else state_store.append_phase_state_entry)
    try:
        record = append(
            session_uuid, work_id, new_state, reason_code, event,
            evidence, source)
    except ValueError:
        return state_store.current_phase_state(session_uuid, work_id)
    mirror = (_mirror_work_unit_lifecycle_unlocked if unlocked
             else _mirror_work_unit_lifecycle)
    mirror(session_uuid, work_id, new_state, reason_code)
    return record


def _mirror_work_unit_lifecycle(session_uuid, work_id, state, reason_code):
    """Best-effort: keep the WorkUnit's own `lifecycle_state` (the join-key
    record every later seam reads) in step with the PhaseState record
    `_advance_phase` just durably appended, so a reader that joins on
    WorkUnit alone never sees a stale `pending`/`running` after real
    progress. `current_phase_state` remains the sole AUTHORITATIVE source
    every decision in this file is actually based on; this mirror is
    read-side convenience only -- a failure here (no minted WorkUnit, a
    lost race) never blocks or reverts the PhaseState write that already
    landed durably."""
    current = state_store.current_work_unit_state(session_uuid, work_id)
    projected = state_store.work_unit_from_history_record(current)
    if projected is None or projected.get("lifecycle_state") == state:
        return
    projected["lifecycle_state"] = state
    projected["terminal_reason"] = (
        reason_code if state in control_plane.TERMINAL_STATES else None)
    try:
        state_store.append_work_unit_transition(projected)
    except ValueError:
        pass


def _mirror_work_unit_lifecycle_unlocked(session_uuid, work_id, state,
                                         reason_code):
    """Reentrant twin of `_mirror_work_unit_lifecycle`, for the SAME
    self-deadlock reason `_advance_phase`'s `unlocked=True` path exists
    (see its docstring): calls B's `append_work_unit_transition_unlocked`
    instead of the locked `append_work_unit_transition`, so
    `_handle_external_kill` mirrors the terminal PhaseState it just wrote
    onto the SAME WorkUnit join key without risking a second `flock()` on
    this process's own already-open WorkUnit lock fd for this work_id
    (MJ-4: without this, a real SIGTERM left the WorkUnit's own
    `lifecycle_state` stale at whatever it was before the kill -- most
    often `running` -- even though PhaseState itself was durably `aborted`,
    so a reader joining on the WorkUnit alone saw a contradictory,
    non-terminal record for an engagement a real SIGTERM had already ended).

    Best-effort, exactly like the locked twin: no minted WorkUnit, an
    already-matching lifecycle_state, or a lost race against a concurrent
    writer is silently skipped -- this mirror never raises out of a signal
    handler, and never blocks or reverts the PhaseState write that already
    landed durably."""
    current = state_store.current_work_unit_state(session_uuid, work_id)
    projected = state_store.work_unit_from_history_record(current)
    if projected is None or projected.get("lifecycle_state") == state:
        return
    projected["lifecycle_state"] = state
    projected["terminal_reason"] = (
        reason_code if state in control_plane.TERMINAL_STATES else None)
    try:
        state_store.append_work_unit_transition_unlocked(projected)
    except ValueError:
        pass


def _complete_phase(session_uuid, work_id, candidate_manifest_digest,
                    source=None):
    """Advance a role engagement's WorkUnit to `completed` — the ONLY path
    any production code in this file uses to reach that state. Binds the
    real candidate digest first (see `_bind_candidate`), then drives the
    reducer through `turn_completed` (running -> awaiting_gate) and
    `gate_validated` with candidate-bound evidence naming it (awaiting_gate
    -> completed). Exit code 0, EOF, headless fallthrough, and status-file
    presence never appear here — only an explicit user/headless APPROVAL at
    the `ready_for_review` gate calls this, and only when a real proven
    manifest digest exists to bind."""
    if not session_uuid or not work_id or not candidate_manifest_digest:
        return None
    _bind_candidate(session_uuid, work_id, candidate_manifest_digest)
    _advance_phase(session_uuid, work_id, "turn_completed", source=source)
    evidence = {"gate_validation": {
        "candidate_manifest_digest": candidate_manifest_digest,
        "candidate_index": None, "verdict": "pass"}}
    return _advance_phase(session_uuid, work_id, "gate_validated",
                          evidence=evidence, source=source)


def _is_policy_preserving_repair(old_allowed, new_allowed):
    """True only when new_allowed does not broaden the active controller set.

    A repair that adds any controller not already in old_allowed is rejected.
    When old_allowed is None (unrestricted), all repairs are trivially preserving.
    """
    if old_allowed is None:
        return True
    return frozenset(new_allowed or ()) <= frozenset(old_allowed)


def _emit_dispatch_escalation(trace, role, missing_capability,
                               repair_hint, blocked_action):
    """Emit a typed dispatch.escalation trace event."""
    if trace:
        trace.event("dispatch.escalation",
                    role=role,
                    missing_capability=missing_capability,
                    repair_hint=repair_hint,
                    blocked_action=blocked_action)


def _make_dispatch_contract(role, controller, purpose, site,
                            resume_session_id=None, phase=None):
    """Build a DispatchContract/v1 record for a dispatch decision site."""
    return {
        "schema_version": 1,
        "record": "DispatchContract",
        "contract_id": str(uuid.uuid4()),
        "role": role,
        "phase": phase,
        "controller": controller,
        "kind": "dispatch",
        "purpose": purpose,
        "site": site,
        "resume_session_id": resume_session_id,
        "created": time.time(),
    }


_ALLOW_FACT = {"allowed": True, "refusal_code": None, "refusal_message": None,
              "source": None}


def _manifest_preflight_fact(manifest):
    """Turn a compiled/preflighted capability manifest into the
    `preflight_result` reducer fact for `dispatch.decide()`: allowed only
    when `status.phase == 'proven'`. `manifest` is `None` when no manifest
    governs this dispatch (no `preflight_result` is contributed). Any other
    falsy value — notably `{}`, what a failed compile/persist attempt leaves
    behind — still governs this dispatch and must refuse, not silently drop
    the fence."""
    if manifest is None:
        return None
    status = manifest.get("status") or {}
    if status.get("phase") == "proven":
        return dict(_ALLOW_FACT)
    refusal = status.get("refusal") or {}
    return {
        "allowed": False,
        "refusal_code": "capability_missing",
        "refusal_message": refusal.get("message") or (
            "capability manifest is not proven; recompile and preflight "
            "the manifest"),
        "source": "preflight",
    }


def _probe_fact(alert):
    """The `probe_result` reducer fact for a failed controller-CLI probe."""
    return {"allowed": False, "refusal_code": "probe_failed",
            "refusal_message": alert or "controller probe failed",
            "source": "probe"}


def _decide_and_trace(trace, role, controller, purpose, site, manifest=None,
                      policy_result=None, preflight_result=None,
                      probe_result=None, resume_session_id=None, phase=None):
    """Build a fresh DispatchContract, call `dispatch.decide()` bound to the
    exact manifest identifier governing this dispatch (`manifest['digest']`,
    when a manifest was compiled/revalidated for this attempt), and emit the
    paired `dispatch.contract` / `dispatch.decision` trace events every
    production call site shares. Returns the DispatchDecision dict."""
    contract = _make_dispatch_contract(role, controller, purpose, site,
                                       resume_session_id=resume_session_id,
                                       phase=phase)
    decision = dispatch.decide(
        contract, policy_result=policy_result,
        preflight_result=preflight_result, probe_result=probe_result,
        manifest_id=(manifest or {}).get("digest"))
    if trace:
        trace.event("dispatch.contract", role=role, site=site,
                    contract_id=contract["contract_id"])
        trace.event("dispatch.decision", role=role, site=site,
                    outcome=decision["outcome"],
                    decision_id=decision["decision_id"],
                    trace_event_id=decision["trace_event_id"],
                    refusal_code=decision["refusal_code"],
                    refusal_message=decision["refusal_message"],
                    source=decision["source"])
    return decision


def _guard_to_policy_fact(controller, role, phase=None, trace=None):
    """Call policy.guard and return a reducer fact dict for dispatch.decide()."""
    try:
        policy.guard(controller, role=role, kind="dispatch", phase=phase,
                     trace=trace)
        return {"allowed": True, "refusal_code": None,
                "refusal_message": None, "source": None}
    except policy.DispatchBlocked as exc:
        return {"allowed": False, "refusal_code": "controller_not_allowed",
                "refusal_message": str(exc), "source": "policy_guard"}


def run_scout(config, context, selected, io_in=None, io_out=None,
              evaluation_policy=None,
              claude_spawn=None, resume_id=None, on_session=None,
              intel_path=None, session_factory=None, review_path=None,
              reviewer_runner=None, reviewer_resume_id=None,
              on_reviewer_session=None, reviewer_context=None,
              reviewer_context_update=None, on_reviewer_context_ack=None,
              trace=None, on_outcome=None,
              eval_scratch_path=None, reviewer_eval_scratch_path=None,
              scores_path=None, session_uuid=None, intel_md_path=None,
              skip_baseline=None, review_packet_ctx=None,
              switch_controller_fn=None, reviewer_switch_note_fn=None,
              on_reviewer_switch_consumed=None,
              on_first_send_accepted=None, on_first_send_rejected=None,
              reviewer_controller_check_fn=None, headless=False,
              gate_preview=None, save_pending_turn_fn=None,
              clear_pending_turn_fn=None, worktree=None, worktree_base=None):
    """Spin up the scout's CLI and drive the review loop.

    `resume_id` continues a saved CLI session; `on_session(controller, id)` is
    called so the session id can be persisted for a future resume.
    `intel_path` is the scout's only write target
    (`~/.cowork/sessions/<uuid>/scout.intel.*.json`).
    `session_factory(controller, **kw)` overrides session creation (for tests).
    `review_path` + the scout-reviewer being on the team enable the reviewer gate;
    `reviewer_runner` overrides the reviewer pass (for tests).
    `reviewer_resume_id` resumes a stored reviewer session; `on_reviewer_session`
    persists a new one. `reviewer_context` is the CURRENT session context for the
    reviewer (defaults to `context`); `reviewer_context_update` is set when a
    resumed reviewer has not acknowledged the current context revision (it is
    delivered as a wake block) and `on_reviewer_context_ack` records the
    acknowledgment after the first successful pass.
    `eval_scratch_path`/`reviewer_eval_scratch_path` + `scores_path` +
    `session_uuid` wire the per-round peer evaluations (scout <->
    scout-reviewer); absent, no evaluations happen.
    """
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    cfg = config["scout"]
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
    # WorkUnit join key for this scout engagement (M2 Package E): minted once
    # per (session, role, scouting-epoch) so a genuine re-engagement after a
    # hand-back gets a fresh WorkUnit rather than reusing an already-terminal
    # one. See `_role_work_id`.
    role_work_id = (
        _role_work_id(session_uuid, "scout",
                     (review_packet_ctx or {}).get("epoch"),
                     (review_packet_ctx or {}).get("attempt"))
        if session_uuid else None)
    if role_work_id:
        _ensure_work_unit(session_uuid, role_work_id, "scout",
                          cfg["controller"], model=cfg.get("model"),
                          effort=cfg.get("effort"))
        _advance_phase(session_uuid, role_work_id, "preflight_started",
                       source="run_scout")
    # Fail-closed order: compile/revalidate the manifest FIRST, bind a
    # dispatch decision to it (resume included — force_recompile revalidates),
    # and require allow before any brief/prompt assembly.
    _scout_manifest = None
    if session_uuid:
        try:
            _scout_manifest, _ = _compile_role_manifest(
                role="scout", session_uuid=session_uuid, work_id="scout",
                controller=cfg["controller"],
                mode=cfg.get("mode", "implement"),
                model=cfg.get("model"), effort=cfg.get("effort"),
                instruction_paths=[SCOUT_PROMPT_PATH],
                sessions_dir=sessions_dir,
                worktree=worktree, worktree_base=worktree_base,
                force_recompile=bool(resume_id))
        except Exception:
            _scout_manifest = {}
        _mdec = _decide_and_trace(
            trace, "scout", cfg["controller"], "launch", "run_scout",
            manifest=_scout_manifest,
            preflight_result=_manifest_preflight_fact(_scout_manifest),
            resume_session_id=resume_id)
        if _mdec["outcome"] == "refuse":
            _emit_dispatch_escalation(
                trace, "scout", "manifest_proven",
                "recompile and preflight the manifest", "prompt_assembly")
            _advance_phase(
                session_uuid, role_work_id, "capability_missing",
                evidence={"refusal_code": _mdec.get("refusal_code"),
                         "refusal_message": _mdec.get("refusal_message")},
                source="manifest_preflight")
            return 1
    brief = assemble_scout_brief(selected, intel_path or "", intel_md_path)
    # The real scout-reviewer runner embeds BOTH intel files (JSON + markdown) so
    # the reviewer actually receives the markdown (D8); a test-injected
    # reviewer_runner overrides it byte-identically to the other phases.
    runner = reviewer_runner
    if runner is None and intel_md_path:
        runner = make_scout_reviewer_runner(
            intel_md_path, trace=trace, extra_writable_dir=sessions_dir)
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, review_path, reviewer_runner=runner,
        reviewer_resume_id=reviewer_resume_id,
        evaluation_policy=evaluation_policy,
        on_reviewer_session=on_reviewer_session,
        context_update=reviewer_context_update,
        trace=trace, phase="scouting",
        on_context_ack=on_reviewer_context_ack,
        eval_scratch_path=reviewer_eval_scratch_path,
        scores_path=scores_path, session_uuid=session_uuid,
        extra_writable_dir=sessions_dir, surface_io_out=io_out,
        review_packet_ctx=review_packet_ctx,
        switch_controller_fn=switch_controller_fn,
        switch_note_fn=reviewer_switch_note_fn,
        on_switch_consumed=on_reviewer_switch_consumed,
        reviewer_controller_check_fn=reviewer_controller_check_fn)
    evaluate_fn = None
    if review_fn is not None:
        evaluate_fn = _make_enqueue_eval_fn(
            "scout", SCOUT_REVIEWER, "scouting", eval_scratch_path,
            scores_path, session_uuid, trace=trace, review_path=review_path,
            artifact_path=intel_path,
            evaluation_policy=evaluation_policy,
            identities_path=(state_store.identities_path_for(session_uuid)
                             if session_uuid else None),
            context_revision=(review_packet_ctx or {}).get("context_revision"))
    if resume_id and not context.strip():
        context = "Continue the session."
    if trace:
        trace.event("role.start", role="scout", controller=cfg["controller"],
                    resume=bool(resume_id), intel_path=intel_path,
                    review_path=review_path)
    ui.banner(io_out, scout_start_text(
        intel_md_path or intel_path or "", resuming=bool(resume_id),
        enabled=ui.is_tty(io_out)), "start")
    io_out.flush()
    # `preflight_passed` (preflighting -> running) is bound per controller
    # branch below, ONLY once that branch's own policy/probe/session-start
    # checks have all actually succeeded -- never here, unconditionally,
    # before them. Firing it this early would move the WorkUnit to `running`
    # while policy_blocked/probe_failed/start_failed can still legitimately
    # reject the dispatch, and the reducer has no `("running",
    # "preflight_rejected")` edge -- only `("preflighting",
    # "preflight_rejected")` -- so every one of those later rejections would
    # silently no-op (`illegal_transition`) against an already-`running`
    # state instead of durably recording the rejection (BL-2).

    if cfg["controller"] == "claude":
        _sf = _guard_to_policy_fact(cfg["controller"], "scout", trace=trace)
        _dec = _decide_and_trace(
            trace, "scout", cfg["controller"], "launch", "run_scout",
            manifest=_scout_manifest, policy_result=_sf,
            resume_session_id=resume_id)
        # A resumed claude scout still pays the live probe (pinned
        # characterization): the manifest-bound decision above is traced
        # either way, but a policy refusal on resume is NOT short-circuited
        # here — it is surfaced by the probe's own uncaught `policy.guard`
        # (kind="probe") below, exactly as it always has been. Only a fresh
        # dispatch (no resume) refuses cleanly before ever reaching it.
        if _dec["outcome"] == "refuse" and not resume_id:
            if trace:
                trace.event("role.end", role="scout",
                            result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(_dec["refusal_message"] + "\n")
            io_out.flush()
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"refusal_code": _dec.get("refusal_code")},
                source="policy_guard")
            return 1
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting scout",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=SCOUT_PROMPT_PATH, trace=trace, role="scout",
                extra_writable_dir=sessions_dir, cache_enabled=True))
        if not ok:
            _decide_and_trace(
                trace, "scout", cfg["controller"], "launch", "run_scout",
                manifest=_scout_manifest, policy_result=_ALLOW_FACT,
                preflight_result=(_ALLOW_FACT if _scout_manifest else None),
                probe_result=_probe_fact(alert), resume_session_id=resume_id)
            if trace:
                trace.event("role.end", role="scout", result="probe_failed")
            io_out.write("cowork: " + alert + "\n")
            io_out.flush()
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"reason": "probe_failed"}, source="probe")
            return 1
        if resume_id:
            session_id, rid = None, resume_id
            io_out.write("cowork: resuming claude session %s\n" % resume_id)
        else:
            # Pin a known UUID up front so the session is resumable even if the
            # run is killed immediately.
            session_id, rid = str(uuid.uuid4()), None
            if on_session:
                on_session("claude", session_id)
        cb = (lambda i: on_session("claude", i)) if on_session else None
        try:
            if session_factory:
                session = session_factory("claude", session_id=session_id,
                                          resume_id=rid, on_session_id=cb)
            else:
                session = bridge.ClaudeSession(
                    SCOUT_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                    speaker="scout", session_id=session_id, resume_id=rid,
                    on_session_id=cb, trace=trace,
                    extra_writable_dir=sessions_dir,
                    model=cfg.get("model"), effort=cfg.get("effort"))
        except KeyboardInterrupt:
            raise
        except policy.DispatchBlocked as exc:
            _bfact = {"allowed": False, "refusal_code": "controller_not_allowed",
                      "refusal_message": str(exc), "source": "bridge_backstop"}
            _bdec = _decide_and_trace(
                trace, "scout", cfg["controller"], "launch", "run_scout",
                manifest=_scout_manifest, policy_result=_bfact,
                resume_session_id=resume_id)
            if trace:
                trace.event("role.end", role="scout", result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(_bdec["refusal_message"] + "\n")
            io_out.flush()
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"reason": "policy_blocked"}, source="bridge_backstop")
            return 1
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="scout", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start scout controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"reason": "start_failed",
                         "error_type": type(exc).__name__},
                source="session_start")
            return 1
        # The claude session is genuinely live now: every preceding check
        # that could still legally reject this dispatch (manifest, policy,
        # probe, session-start) has already passed, so this is the ONE point
        # this branch may legally advance preflighting -> running (BL-2).
        _advance_phase(session_uuid, role_work_id, "preflight_passed",
                       source="run_scout")
        first = _role_seed_delivery(brief, context)
        return _scout_loop(session, first, intel_path, context, io_in, io_out,
                           review_fn=review_fn, trace=trace,
                           on_outcome=on_outcome, evaluate_fn=evaluate_fn,
                           intel_md_path=intel_md_path,
                           skip_baseline=skip_baseline,
                           context_revision=(review_packet_ctx or {}).get(
                               "context_revision"),
                           is_resume=bool(resume_id),
                           on_first_send_accepted=on_first_send_accepted,
                           on_first_send_rejected=on_first_send_rejected,
                           headless=headless, gate_preview=gate_preview,
                           review_path=review_path,
                           save_pending_turn_fn=save_pending_turn_fn,
                           clear_pending_turn_fn=clear_pending_turn_fn,
                               session_uuid=session_uuid,
                               role_work_id=role_work_id)

    if cfg["controller"] == "opencode":
        # opencode delivers the role prompt as a generated agent file (a system
        # prompt, like claude) — seed with brief + context only, never the role
        # text.
        if resume_id:
            io_out.write("cowork: resuming opencode session %s\n" % resume_id)
        cb = (lambda i: on_session("opencode", i)) if on_session else None
        try:
            if session_factory:
                session = session_factory("opencode",
                                          resume_session_id=resume_id,
                                          on_session_id=cb)
            else:
                session = bridge.OpencodeSession(
                    SCOUT_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                    speaker="scout", resume_session_id=resume_id,
                    on_session_id=cb, trace=trace,
                    extra_writable_dir=sessions_dir,
                    model=cfg.get("model"), effort=cfg.get("effort"))
        except KeyboardInterrupt:
            raise
        except policy.DispatchBlocked as exc:
            # The bridge-level backstop fired: surface the policy message
            # instead of the generic "failed to start" text.
            if trace:
                trace.event("role.end", role="scout", result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(str(exc) + "\n")
            io_out.flush()
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"reason": "policy_blocked"}, source="bridge_backstop")
            return 1
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="scout", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start scout controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"reason": "start_failed",
                         "error_type": type(exc).__name__},
                source="session_start")
            return 1
        # The opencode session is genuinely live now -- see the claude
        # branch's identical comment above (BL-2): this is the ONE point
        # this branch may legally advance preflighting -> running.
        _advance_phase(session_uuid, role_work_id, "preflight_passed",
                       source="run_scout")
        first = _role_seed_delivery(brief, context)
        return _scout_loop(session, first, intel_path, context, io_in, io_out,
                           review_fn=review_fn, trace=trace,
                           on_outcome=on_outcome, evaluate_fn=evaluate_fn,
                           intel_md_path=intel_md_path,
                           skip_baseline=skip_baseline,
                           context_revision=(review_packet_ctx or {}).get(
                               "context_revision"),
                           is_resume=bool(resume_id),
                           on_first_send_accepted=on_first_send_accepted,
                           on_first_send_rejected=on_first_send_rejected,
                           headless=headless, gate_preview=gate_preview,
                           review_path=review_path,
                           save_pending_turn_fn=save_pending_turn_fn,
                           clear_pending_turn_fn=clear_pending_turn_fn,
                               session_uuid=session_uuid,
                               role_work_id=role_work_id)

    role_text = read_scout_prompt()
    prompt = assemble_codex_prompt(role_text, brief, context)
    _emit_codex_role_prompt_bytes(trace, "scout", role_text)
    if resume_id:
        io_out.write("cowork: resuming codex session %s\n" % resume_id)
    cb = (lambda i: on_session("codex", i)) if on_session else None
    try:
        if session_factory:
            session = session_factory("codex", resume_thread_id=resume_id,
                                      on_thread_id=cb)
        else:
            session = bridge.CodexSession(
                cfg["mode"], cfg["yolo"], io_out=io_out, speaker="scout",
                resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
                extra_writable_dir=sessions_dir,
                model=cfg.get("model"), effort=cfg.get("effort"))
    except policy.DispatchBlocked as exc:
        if trace:
            trace.event("role.end", role="scout", result="policy_blocked",
                        controller="codex")
        io_out.write(str(exc) + "\n")
        io_out.flush()
        _advance_phase(
            session_uuid, role_work_id, "preflight_rejected",
            evidence={"reason": "policy_blocked"}, source="bridge_backstop")
        return 1
    # The codex session is genuinely live now -- see the claude branch's
    # identical comment above (BL-2): this is the ONE point this branch may
    # legally advance preflighting -> running.
    _advance_phase(session_uuid, role_work_id, "preflight_passed",
                   source="run_scout")
    return _scout_loop(session, prompt, intel_path, context, io_in, io_out,
                       review_fn=review_fn, trace=trace, on_outcome=on_outcome,
                       evaluate_fn=evaluate_fn, intel_md_path=intel_md_path,
                       skip_baseline=skip_baseline,
                       context_revision=(review_packet_ctx or {}).get(
                           "context_revision"),
                       is_resume=bool(resume_id),
                       on_first_send_accepted=on_first_send_accepted,
                       on_first_send_rejected=on_first_send_rejected,
                        headless=headless, gate_preview=gate_preview,
                        review_path=review_path,
                        save_pending_turn_fn=save_pending_turn_fn,
                        clear_pending_turn_fn=clear_pending_turn_fn,
                            session_uuid=session_uuid,
                            role_work_id=role_work_id)


def run_planner(config, context, selected, io_in=None, io_out=None,
                evaluation_policy=None,
                claude_spawn=None, resume_id=None, on_session=None,
                plan_json_path=None, plan_md_path=None,
                session_factory=None, review_path=None,
                reviewer_runner=None, reviewer_resume_id=None,
                on_reviewer_session=None, reviewer_context=None,
                reviewer_context_update=None, on_reviewer_context_ack=None,
                trace=None, handoff_confirm=None, on_outcome=None,
                eval_scratch_path=None, reviewer_eval_scratch_path=None,
                scores_path=None, session_uuid=None, intel_path=None,
                planning_epoch=None, skip_baseline=None, intel_md_path=None,
                review_packet_ctx=None, switch_controller_fn=None,
                reviewer_switch_note_fn=None,
                on_reviewer_switch_consumed=None,
                on_first_send_accepted=None, on_first_send_rejected=None,
                reviewer_controller_check_fn=None, headless=False,
                gate_preview=None, save_pending_turn_fn=None,
                clear_pending_turn_fn=None, worktree=None, worktree_base=None):
    """Spin up the planner's CLI and drive the planning loop (the planner
    instantiation of `_role_loop`).

    `context` is the seed message for this cycle: the approved-intel seed on a
    fresh chain, a digest wake block after a hand-back round trip, or "" on a
    plain resume (auto-continue). The plan JSON (`plan_json_path`) doubles as
    the planner's status channel; `plan_md_path` is the user's review surface.
    `review_path` + the planning-advisor being on the team enable the advisor
    gate; `reviewer_runner` overrides the advisor pass (for tests).
    `eval_scratch_path`/`reviewer_eval_scratch_path` + `scores_path` +
    `session_uuid` wire the per-round peer evaluations (planner <->
    planning-advisor, each bundling a one-time ->scout eval of the approved
    intel at `intel_path`); absent, no evaluations happen.
    `on_outcome(outcome, payload)` reports how the loop ended so `run_flow` can
    execute a confirmed hand-back ("handoff" outcome) or finish the session."""
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    cfg = config["planner"]
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
    # WorkUnit join key for this planner engagement (M2 Package E): see
    # `run_scout`'s twin comment.
    role_work_id = (
        _role_work_id(session_uuid, "planner", planning_epoch,
                     (review_packet_ctx or {}).get("attempt"))
        if session_uuid else None)
    if role_work_id:
        _ensure_work_unit(session_uuid, role_work_id, "planner",
                          cfg["controller"], model=cfg.get("model"),
                          effort=cfg.get("effort"))
        _advance_phase(session_uuid, role_work_id, "preflight_started",
                       source="run_planner")
    # Fail-closed order: compile/revalidate the manifest FIRST, bind a
    # dispatch decision to it (resume included — force_recompile revalidates),
    # and require allow before any brief/prompt assembly.
    _planner_manifest = None
    if session_uuid:
        try:
            _planner_manifest, _ = _compile_role_manifest(
                role="planner", session_uuid=session_uuid, work_id="planner",
                controller=cfg["controller"],
                mode=cfg.get("mode", "implement"),
                model=cfg.get("model"), effort=cfg.get("effort"),
                instruction_paths=[PLANNER_PROMPT_PATH],
                sessions_dir=sessions_dir,
                worktree=worktree, worktree_base=worktree_base,
                # intel_path is the approved scout intel the planner plans
                # FROM — a real upstream candidate; it changing between
                # compiles is a genuine revalidation trigger.
                candidate_snapshot=_file_snapshot(intel_path),
                force_recompile=bool(resume_id))
        except Exception:
            _planner_manifest = {}
        _mdec = _decide_and_trace(
            trace, "planner", cfg["controller"], "launch", "run_planner",
            manifest=_planner_manifest,
            preflight_result=_manifest_preflight_fact(_planner_manifest),
            resume_session_id=resume_id)
        if _mdec["outcome"] == "refuse":
            _emit_dispatch_escalation(
                trace, "planner", "manifest_proven",
                "recompile and preflight the manifest", "prompt_assembly")
            _advance_phase(
                session_uuid, role_work_id, "capability_missing",
                evidence={"refusal_code": _mdec.get("refusal_code"),
                         "refusal_message": _mdec.get("refusal_message")},
                source="manifest_preflight")
            if on_outcome:
                on_outcome(_OUTCOME_ENDED, None)
            return 1
    brief = assemble_planner_brief(plan_json_path or "", plan_md_path or "")
    runner = reviewer_runner or make_planning_advisor_runner(
        plan_md_path, trace=trace, extra_writable_dir=sessions_dir,
        intel_path=intel_path, intel_md_path=intel_md_path)
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, review_path, reviewer_runner=runner,
        reviewer_resume_id=reviewer_resume_id,
        evaluation_policy=evaluation_policy,
        on_reviewer_session=on_reviewer_session,
        context_update=reviewer_context_update,
        on_context_ack=on_reviewer_context_ack,
        reviewer_role=PLANNING_ADVISOR, phase="planning",
        eval_scratch_path=reviewer_eval_scratch_path,
        scores_path=scores_path, session_uuid=session_uuid,
        intel_path=intel_path, planning_epoch=planning_epoch,
        intel_md_path=intel_md_path,
        extra_writable_dir=sessions_dir, surface_io_out=io_out,
        review_packet_ctx=review_packet_ctx,
        switch_controller_fn=switch_controller_fn,
        switch_note_fn=reviewer_switch_note_fn,
        on_switch_consumed=on_reviewer_switch_consumed,
        reviewer_controller_check_fn=reviewer_controller_check_fn)
    evaluate_fn = None
    if review_fn is not None:
        evaluate_fn = _make_enqueue_eval_fn(
            "planner", PLANNING_ADVISOR, "planning", eval_scratch_path,
            scores_path, session_uuid, intel_path=intel_path,
            artifact_path=plan_json_path,
            planning_epoch=planning_epoch, intel_md_path=intel_md_path,
            trace=trace, review_path=review_path,
            evaluation_policy=evaluation_policy,
            identities_path=(state_store.identities_path_for(session_uuid)
                             if session_uuid else None),
            context_revision=(review_packet_ctx or {}).get("context_revision"))
    if resume_id and not context.strip():
        context = "Continue the session."
    if trace:
        trace.event("role.start", role="planner", controller=cfg["controller"],
                    resume=bool(resume_id), plan_json_path=plan_json_path,
                    plan_md_path=plan_md_path, review_path=review_path)
    ui.banner(io_out, planner_start_text(plan_md_path or "",
                                         resuming=bool(resume_id),
                                         enabled=ui.is_tty(io_out)), "start")
    io_out.flush()
    # `preflight_passed` (preflighting -> running) is bound per controller
    # branch below, ONLY once that branch's own policy/probe/session-start
    # checks have all actually succeeded -- see run_scout's identical BL-2
    # comment for why firing it unconditionally here would silently drop
    # every later `_reject(...)` (no `("running", "preflight_rejected")`
    # reducer edge).

    def report(outcome, payload):
        if on_outcome:
            on_outcome(outcome, payload)

    def _reject(reason, source, **extra):
        evidence = {"reason": reason}
        evidence.update(extra)
        _advance_phase(session_uuid, role_work_id, "preflight_rejected",
                       evidence=evidence, source=source)

    loop_kwargs = dict(
        role="planner", review_fn=review_fn, trace=trace,
        reviewer_role=PLANNING_ADVISOR,
        needs_input_text=planner_needs_input_text,
        review_text=lambda _p, en=False: planner_review_text(
            plan_md_path or "", en),
        done_text=lambda _p, en=False: planner_done_text(plan_md_path or "", en),
        artifact_noun="plan",
        handoff_enabled=True, handoff_confirm=handoff_confirm,
        evaluate_fn=evaluate_fn, skip_baseline=skip_baseline,
        context_revision=(review_packet_ctx or {}).get("context_revision"),
        phase="planning", is_resume=bool(resume_id),
        seed_artifact_paths=[intel_path], headless=headless,
        gate_preview=gate_preview, require_pending_question=True,
        review_path=review_path, save_pending_turn_fn=save_pending_turn_fn,
        clear_pending_turn_fn=clear_pending_turn_fn)

    if cfg["controller"] == "claude":
        _pf = _guard_to_policy_fact(cfg["controller"], "planner", trace=trace)
        _pdec = _decide_and_trace(
            trace, "planner", cfg["controller"], "launch", "run_planner",
            manifest=_planner_manifest, policy_result=_pf,
            resume_session_id=resume_id)
        # A resumed claude planner still pays the live probe (base
        # semantics): the manifest-bound decision above is traced either way,
        # but a policy refusal on resume is surfaced by the probe's own
        # uncaught `policy.guard` (kind="probe") below, not short-circuited
        # here. Only a fresh dispatch (no resume) refuses cleanly first.
        if _pdec["outcome"] == "refuse" and not resume_id:
            if trace:
                trace.event("role.end", role="planner",
                            result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(_pdec["refusal_message"] + "\n")
            io_out.flush()
            _reject("policy_blocked", "policy_guard")
            report(_OUTCOME_ENDED, None)
            return 1
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting planner",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=PLANNER_PROMPT_PATH, trace=trace,
                role="planner", extra_writable_dir=sessions_dir,
                cache_enabled=True))
        if not ok:
            _decide_and_trace(
                trace, "planner", cfg["controller"], "launch", "run_planner",
                manifest=_planner_manifest, policy_result=_ALLOW_FACT,
                preflight_result=(_ALLOW_FACT if _planner_manifest else None),
                probe_result=_probe_fact(alert), resume_session_id=resume_id)
            if trace:
                trace.event("role.end", role="planner", result="probe_failed")
            io_out.write("cowork: " + alert + "\n")
            io_out.flush()
            _reject("probe_failed", "probe")
            report(_OUTCOME_ENDED, None)
            return 1
        if resume_id:
            session_id, rid = None, resume_id
            io_out.write("cowork: resuming claude session %s\n" % resume_id)
        else:
            # Pin a known UUID up front so the session is resumable even if the
            # run is killed immediately.
            session_id, rid = str(uuid.uuid4()), None
            if on_session:
                on_session("claude", session_id)
        cb = (lambda i: on_session("claude", i)) if on_session else None
        try:
            if session_factory:
                session = session_factory("claude", session_id=session_id,
                                          resume_id=rid, on_session_id=cb)
            else:
                session = bridge.ClaudeSession(
                    PLANNER_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                    speaker="planner", session_id=session_id, resume_id=rid,
                    on_session_id=cb, trace=trace,
                    extra_writable_dir=sessions_dir,
                    model=cfg.get("model"), effort=cfg.get("effort"))
        except KeyboardInterrupt:
            raise
        except policy.DispatchBlocked as exc:
            _bpf = {"allowed": False, "refusal_code": "controller_not_allowed",
                    "refusal_message": str(exc), "source": "bridge_backstop"}
            _bpdec = _decide_and_trace(
                trace, "planner", cfg["controller"], "launch", "run_planner",
                manifest=_planner_manifest, policy_result=_bpf,
                resume_session_id=resume_id)
            if trace:
                trace.event("role.end", role="planner", result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(_bpdec["refusal_message"] + "\n")
            io_out.flush()
            _reject("policy_blocked", "bridge_backstop")
            report(_OUTCOME_ENDED, None)
            return 1
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="planner", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start planner controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            _reject("start_failed", "session_start",
                   error_type=type(exc).__name__)
            report(_OUTCOME_ENDED, None)
            return 1
        # The claude session is genuinely live now (BL-2): the ONE point
        # this branch may legally advance preflighting -> running.
        _advance_phase(session_uuid, role_work_id, "preflight_passed",
                       source="run_planner")
        first = _role_seed_delivery(brief, context)
        rc, outcome, payload = _role_loop(
            session, first, plan_json_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
                session_uuid=session_uuid, role_work_id=role_work_id)
        report(outcome, payload)
        return rc

    if cfg["controller"] == "opencode":
        # Role prompt rides in the generated agent file (system prompt); the
        # seed is brief + context only, fresh and resumed alike.
        if resume_id:
            io_out.write("cowork: resuming opencode session %s\n" % resume_id)
        cb = (lambda i: on_session("opencode", i)) if on_session else None
        try:
            if session_factory:
                session = session_factory("opencode",
                                          resume_session_id=resume_id,
                                          on_session_id=cb)
            else:
                session = bridge.OpencodeSession(
                    PLANNER_PROMPT_PATH, cfg["mode"], cfg["yolo"],
                    io_out=io_out, speaker="planner",
                    resume_session_id=resume_id, on_session_id=cb, trace=trace,
                    extra_writable_dir=sessions_dir,
                    model=cfg.get("model"), effort=cfg.get("effort"))
        except KeyboardInterrupt:
            raise
        except policy.DispatchBlocked as exc:
            # The bridge-level backstop fired: surface the policy message
            # instead of the generic "failed to start" text.
            if trace:
                trace.event("role.end", role="planner", result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(str(exc) + "\n")
            io_out.flush()
            _reject("policy_blocked", "bridge_backstop")
            report(_OUTCOME_ENDED, None)
            return 1
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="planner", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start planner controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            _reject("start_failed", "session_start",
                   error_type=type(exc).__name__)
            report(_OUTCOME_ENDED, None)
            return 1
        # The opencode session is genuinely live now (BL-2): the ONE point
        # this branch may legally advance preflighting -> running.
        _advance_phase(session_uuid, role_work_id, "preflight_passed",
                       source="run_planner")
        first = _role_seed_delivery(brief, context)
        rc, outcome, payload = _role_loop(
            session, first, plan_json_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
                session_uuid=session_uuid, role_work_id=role_work_id)
        report(outcome, payload)
        return rc

    role_text = _read_text(PLANNER_PROMPT_PATH)
    prompt = assemble_codex_prompt(role_text, brief, context)
    if resume_id:
        io_out.write("cowork: resuming codex session %s\n" % resume_id)
        prompt = (brief + "\n\n" + context).strip()  # thread already has role
    else:
        # Role text is inlined into the fresh prompt body only (the resume
        # branch drops it); measure it there (#4).
        _emit_codex_role_prompt_bytes(trace, "planner", role_text)
    cb = (lambda i: on_session("codex", i)) if on_session else None
    try:
        if session_factory:
            session = session_factory("codex", resume_thread_id=resume_id,
                                      on_thread_id=cb)
        else:
            session = bridge.CodexSession(
                cfg["mode"], cfg["yolo"], io_out=io_out, speaker="planner",
                resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
                extra_writable_dir=sessions_dir,
                model=cfg.get("model"), effort=cfg.get("effort"))
    except policy.DispatchBlocked as exc:
        if trace:
            trace.event("role.end", role="planner", result="policy_blocked",
                        controller="codex")
        io_out.write(str(exc) + "\n")
        io_out.flush()
        _reject("policy_blocked", "bridge_backstop")
        report(_OUTCOME_ENDED, None)
        return 1
    # The codex session is genuinely live now (BL-2): the ONE point this
    # branch may legally advance preflighting -> running.
    _advance_phase(session_uuid, role_work_id, "preflight_passed",
                   source="run_planner")
    rc, outcome, payload = _role_loop(
        session, prompt, plan_json_path, context, io_in, io_out,
        on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
            session_uuid=session_uuid, role_work_id=role_work_id)
    report(outcome, payload)
    return rc


def run_builder(config, context, selected, io_in=None, io_out=None,
                evaluation_policy=None,
                claude_spawn=None, resume_id=None, on_session=None,
                build_status_path=None, build_review_path=None,
                session_factory=None,
                reviewer_runner=None, reviewer_resume_id=None,
                on_reviewer_session=None, reviewer_context=None,
                reviewer_context_update=None, on_reviewer_context_ack=None,
                trace=None, handoff_confirm=None, on_outcome=None,
                eval_scratch_path=None, reviewer_eval_scratch_path=None,
                scores_path=None, session_uuid=None, plan_json_path=None,
                plan_md_path=None, building_epoch=None, baseline_note="",
                baseline_repos=None, build_summary_path=None,
                review_packet_ctx=None, switch_controller_fn=None,
                reviewer_switch_note_fn=None,
                on_reviewer_switch_consumed=None,
                on_first_send_accepted=None, on_first_send_rejected=None,
                reviewer_controller_check_fn=None, headless=False,
                gate_preview=None, save_pending_turn_fn=None,
                clear_pending_turn_fn=None, worktree=None, worktree_base=None):
    """Spin up the builder's CLI and drive the building loop (the builder
    instantiation of `_role_loop`).

    `context` is the seed message for this cycle: the approved-plan seed on a
    fresh chain, a plan-updated wake block after a hand-back round trip, or ""
    on a plain resume (auto-continue). `build_status_path` is the builder's
    status + verification channel (NOT a write restriction — the builder edits
    the repo). `build_review_path` + the build-reviewer being on the team
    enable the reviewer gate; `reviewer_runner` overrides the reviewer pass
    (for tests). `eval_scratch_path`/`reviewer_eval_scratch_path` + `scores_path`
    + `session_uuid` wire the per-round peer evaluations (builder <->
    build-reviewer, each bundling a one-time ->planner eval of the approved
    plan at `plan_json_path`/`plan_md_path`); absent, no evaluations happen.
    `on_outcome(outcome, payload)` reports how the loop ended so `run_flow` can
    execute a confirmed hand-back ("handoff" outcome, builder -> planner) or
    finish the session."""
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    cfg = config["builder"]
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
    # WorkUnit join key for this builder engagement (M2 Package E): see
    # `run_scout`'s twin comment.
    role_work_id = (
        _role_work_id(session_uuid, "builder", building_epoch,
                     (review_packet_ctx or {}).get("attempt"))
        if session_uuid else None)
    if role_work_id:
        _ensure_work_unit(session_uuid, role_work_id, "builder",
                          cfg["controller"], model=cfg.get("model"),
                          effort=cfg.get("effort"))
        _advance_phase(session_uuid, role_work_id, "preflight_started",
                       source="run_builder")
    # Fail-closed order: compile/revalidate the manifest FIRST, bind a
    # dispatch decision to it (resume included — force_recompile revalidates),
    # and require allow before any brief/prompt assembly.
    _builder_manifest = None
    if session_uuid:
        try:
            # baseline_repos is real evidence: run_flow's build_baseline()
            # already ran `git status --porcelain` against each entry (via
            # _git_build_baseline) before this dispatch, so a non-empty set
            # declares the SAME safe, read-only git operation as proof —
            # never a fabricated one.
            _builder_manifest, _ = _compile_role_manifest(
                role="builder", session_uuid=session_uuid, work_id="builder",
                controller=cfg["controller"],
                mode=cfg.get("mode", "implement"),
                model=cfg.get("model"), effort=cfg.get("effort"),
                instruction_paths=[BUILDER_PROMPT_PATH],
                sessions_dir=sessions_dir,
                worktree=worktree, worktree_base=worktree_base,
                # plan_json_path is the approved plan the builder builds
                # FROM — a real upstream candidate; it changing between
                # compiles is a genuine revalidation trigger.
                candidate_snapshot=_file_snapshot(plan_json_path),
                action_classes=["git"] if baseline_repos else [],
                command_adapters=(
                    {"git": {"subcommand": "status", "flags": ["--porcelain"]}}
                    if baseline_repos else {}),
                force_recompile=bool(resume_id))
        except Exception:
            _builder_manifest = {}
        _mdec = _decide_and_trace(
            trace, "builder", cfg["controller"], "launch", "run_builder",
            manifest=_builder_manifest,
            preflight_result=_manifest_preflight_fact(_builder_manifest),
            resume_session_id=resume_id)
        if _mdec["outcome"] == "refuse":
            _emit_dispatch_escalation(
                trace, "builder", "manifest_proven",
                "recompile and preflight the manifest", "prompt_assembly")
            _advance_phase(
                session_uuid, role_work_id, "capability_missing",
                evidence={"refusal_code": _mdec.get("refusal_code"),
                         "refusal_message": _mdec.get("refusal_message")},
                source="manifest_preflight")
            if on_outcome:
                on_outcome(_OUTCOME_ENDED, None)
            return 1
    brief = assemble_builder_brief(build_status_path or "", build_summary_path)
    runner = reviewer_runner or make_build_reviewer_runner(
        plan_json_path, plan_md_path, baseline_note=baseline_note,
        baseline_repos=baseline_repos, trace=trace,
        extra_writable_dir=sessions_dir, build_summary_path=build_summary_path,
        session_uuid=session_uuid)
    consumed = plan_consumed_upstream(plan_json_path, plan_md_path,
                                      building_epoch)
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, build_review_path, reviewer_runner=runner,
        reviewer_resume_id=reviewer_resume_id,
        evaluation_policy=evaluation_policy,
        on_reviewer_session=on_reviewer_session,
        context_update=reviewer_context_update,
        on_context_ack=on_reviewer_context_ack,
        reviewer_role=BUILD_REVIEWER, phase="building",
        eval_scratch_path=reviewer_eval_scratch_path,
        scores_path=scores_path, session_uuid=session_uuid,
        consumed_upstream=consumed, extra_writable_dir=sessions_dir,
        surface_io_out=io_out, review_packet_ctx=review_packet_ctx,
        switch_controller_fn=switch_controller_fn,
        switch_note_fn=reviewer_switch_note_fn,
        on_switch_consumed=on_reviewer_switch_consumed,
        reviewer_controller_check_fn=reviewer_controller_check_fn)
    evaluate_fn = None
    if review_fn is not None:
        evaluate_fn = _make_enqueue_eval_fn(
            "builder", BUILD_REVIEWER, "building", eval_scratch_path,
            scores_path, session_uuid, consumed_upstream=consumed, trace=trace,
            artifact_path=build_status_path,
            review_path=build_review_path,
            evaluation_policy=evaluation_policy,
            identities_path=(state_store.identities_path_for(session_uuid)
                             if session_uuid else None),
            context_revision=(review_packet_ctx or {}).get("context_revision"))
    if resume_id and not context.strip():
        context = "Continue the session."
    if trace:
        trace.event("role.start", role="builder", controller=cfg["controller"],
                    resume=bool(resume_id), build_status_path=build_status_path,
                    review_path=build_review_path)
    # The user-facing gate surfaces (start / review / done) point at the build
    # summary markdown when one is wired — the readable review surface — mirroring
    # the scout's intel.md and the planner's plan.md; the status file driving the
    # loop stays build_status_path. Falls back to the status file otherwise.
    build_surface_path = build_summary_path or build_status_path
    ui.banner(io_out, builder_start_text(build_surface_path or "",
                                         resuming=bool(resume_id),
                                         enabled=ui.is_tty(io_out)), "start")
    io_out.flush()
    # `preflight_passed` (preflighting -> running) is bound per controller
    # branch below, ONLY once that branch's own policy/probe/session-start
    # checks have all actually succeeded -- see run_scout's identical BL-2
    # comment for why firing it unconditionally here would silently drop
    # every later `_reject(...)` (no `("running", "preflight_rejected")`
    # reducer edge).

    def report(outcome, payload):
        if on_outcome:
            on_outcome(outcome, payload)

    def _reject(reason, source, **extra):
        evidence = {"reason": reason}
        evidence.update(extra)
        _advance_phase(session_uuid, role_work_id, "preflight_rejected",
                       evidence=evidence, source=source)

    def _gate_review_text(_p, en=False):
        # UX-021: the human gate renders the SAME derived overlay the reviewer
        # surface carries — read fresh from the current-receipt pointer at
        # banner time, with the agent-authored prose labeled separately and a
        # visible warning when the contradiction flag is set.
        overlay, pointer = _current_verification_overlay(session_uuid)
        return builder_review_text(
            build_surface_path or "", en, overlay=overlay,
            receipt_path=(pointer.get("receipt_path")
                          if isinstance(pointer, dict) else None),
            agent_status_path=build_status_path)

    loop_kwargs = dict(
        role="builder", review_fn=review_fn, trace=trace,
        reviewer_role=BUILD_REVIEWER,
        needs_input_text=builder_needs_input_text,
        review_text=_gate_review_text,
        done_text=lambda _p, en=False: builder_done_text(
            build_surface_path or "", en),
        artifact_noun="build",
        # review_allow_ask=False removes only the "Ask a question" choice from
        # the builder gate (scoped to the scout/planner artifact gates). With a
        # gate_preview the builder gate is still the preview-enabled 3-way CLI
        # select — Request changes / Approve & finish / Stop (see _read_review);
        # the plain binary approve/revise confirm survives only for the
        # preview-less (gate_preview=None) compatibility path.
        review_allow_ask=False,
        handoff_enabled=True, handoff_confirm=handoff_confirm,
        handoff_gate_text_fn=builder_handoff_gate_text,
        handoff_confirm_prompt="Hand the work back to the planner?",
        handoff_declined_text_fn=handoff_declined_to_planner_text,
        evaluate_fn=evaluate_fn,
        context_revision=(review_packet_ctx or {}).get("context_revision"),
        phase="building", is_resume=bool(resume_id),
        seed_artifact_paths=[plan_json_path, plan_md_path], headless=headless,
        gate_preview=gate_preview, require_pending_question=True,
        review_path=build_review_path, save_pending_turn_fn=save_pending_turn_fn,
        clear_pending_turn_fn=clear_pending_turn_fn,
        build_summary_path=build_summary_path)

    if cfg["controller"] == "claude":
        _bf = _guard_to_policy_fact(cfg["controller"], "builder", trace=trace)
        _bdec2 = _decide_and_trace(
            trace, "builder", cfg["controller"], "launch", "run_builder",
            manifest=_builder_manifest, policy_result=_bf,
            resume_session_id=resume_id)
        # A resumed claude builder still pays the live probe (base
        # semantics): the manifest-bound decision above is traced either way,
        # but a policy refusal on resume is surfaced by the probe's own
        # uncaught `policy.guard` (kind="probe") below, not short-circuited
        # here. Only a fresh dispatch (no resume) refuses cleanly first.
        if _bdec2["outcome"] == "refuse" and not resume_id:
            if trace:
                trace.event("role.end", role="builder",
                            result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(_bdec2["refusal_message"] + "\n")
            io_out.flush()
            _reject("policy_blocked", "policy_guard")
            report(_OUTCOME_ENDED, None)
            return 1
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting builder",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=BUILDER_PROMPT_PATH, trace=trace,
                role="builder", extra_writable_dir=sessions_dir,
                cache_enabled=True))
        if not ok:
            _decide_and_trace(
                trace, "builder", cfg["controller"], "launch", "run_builder",
                manifest=_builder_manifest, policy_result=_ALLOW_FACT,
                preflight_result=(_ALLOW_FACT if _builder_manifest else None),
                probe_result=_probe_fact(alert), resume_session_id=resume_id)
            if trace:
                trace.event("role.end", role="builder", result="probe_failed")
            io_out.write("cowork: " + alert + "\n")
            io_out.flush()
            _reject("probe_failed", "probe")
            report(_OUTCOME_ENDED, None)
            return 1
        if resume_id:
            session_id, rid = None, resume_id
            io_out.write("cowork: resuming claude session %s\n" % resume_id)
        else:
            session_id, rid = str(uuid.uuid4()), None
            if on_session:
                on_session("claude", session_id)
        cb = (lambda i: on_session("claude", i)) if on_session else None
        try:
            if session_factory:
                session = session_factory("claude", session_id=session_id,
                                          resume_id=rid, on_session_id=cb)
            else:
                session = bridge.ClaudeSession(
                    BUILDER_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                    speaker="builder", session_id=session_id, resume_id=rid,
                    on_session_id=cb, trace=trace,
                    extra_writable_dir=sessions_dir,
                    model=cfg.get("model"), effort=cfg.get("effort"))
        except KeyboardInterrupt:
            raise
        except policy.DispatchBlocked as exc:
            _bbf = {"allowed": False, "refusal_code": "controller_not_allowed",
                    "refusal_message": str(exc), "source": "bridge_backstop"}
            _bbdec = _decide_and_trace(
                trace, "builder", cfg["controller"], "launch", "run_builder",
                manifest=_builder_manifest, policy_result=_bbf,
                resume_session_id=resume_id)
            if trace:
                trace.event("role.end", role="builder", result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(_bbdec["refusal_message"] + "\n")
            io_out.flush()
            _reject("policy_blocked", "bridge_backstop")
            report(_OUTCOME_ENDED, None)
            return 1
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="builder", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start builder controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            _reject("start_failed", "session_start",
                   error_type=type(exc).__name__)
            report(_OUTCOME_ENDED, None)
            return 1
        # The claude session is genuinely live now (BL-2): the ONE point
        # this branch may legally advance preflighting -> running.
        _advance_phase(session_uuid, role_work_id, "preflight_passed",
                       source="run_builder")
        first = _role_seed_delivery(brief, context)
        rc, outcome, payload = _role_loop(
            session, first, build_status_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
                session_uuid=session_uuid, role_work_id=role_work_id)
        report(outcome, payload)
        return rc

    if cfg["controller"] == "opencode":
        # Role prompt rides in the generated agent file (system prompt); the
        # seed is brief + context only, fresh and resumed alike.
        if resume_id:
            io_out.write("cowork: resuming opencode session %s\n" % resume_id)
        cb = (lambda i: on_session("opencode", i)) if on_session else None
        try:
            if session_factory:
                session = session_factory("opencode",
                                          resume_session_id=resume_id,
                                          on_session_id=cb)
            else:
                session = bridge.OpencodeSession(
                    BUILDER_PROMPT_PATH, cfg["mode"], cfg["yolo"],
                    io_out=io_out, speaker="builder",
                    resume_session_id=resume_id, on_session_id=cb, trace=trace,
                    extra_writable_dir=sessions_dir,
                    model=cfg.get("model"), effort=cfg.get("effort"))
        except KeyboardInterrupt:
            raise
        except policy.DispatchBlocked as exc:
            # The bridge-level backstop fired: surface the policy message
            # instead of the generic "failed to start" text.
            if trace:
                trace.event("role.end", role="builder", result="policy_blocked",
                            controller=cfg["controller"])
            io_out.write(str(exc) + "\n")
            io_out.flush()
            _reject("policy_blocked", "bridge_backstop")
            report(_OUTCOME_ENDED, None)
            return 1
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="builder", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start builder controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            _reject("start_failed", "session_start",
                   error_type=type(exc).__name__)
            report(_OUTCOME_ENDED, None)
            return 1
        # The opencode session is genuinely live now (BL-2): the ONE point
        # this branch may legally advance preflighting -> running.
        _advance_phase(session_uuid, role_work_id, "preflight_passed",
                       source="run_builder")
        first = _role_seed_delivery(brief, context)
        rc, outcome, payload = _role_loop(
            session, first, build_status_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
                session_uuid=session_uuid, role_work_id=role_work_id)
        report(outcome, payload)
        return rc

    role_text = _read_text(BUILDER_PROMPT_PATH)
    prompt = assemble_codex_prompt(role_text, brief, context)
    if resume_id:
        io_out.write("cowork: resuming codex session %s\n" % resume_id)
        prompt = (brief + "\n\n" + context).strip()  # thread already has role
    else:
        # Role text is inlined into the fresh prompt body only (the resume
        # branch drops it); measure it there (#4).
        _emit_codex_role_prompt_bytes(trace, "builder", role_text)
    cb = (lambda i: on_session("codex", i)) if on_session else None
    try:
        if session_factory:
            session = session_factory("codex", resume_thread_id=resume_id,
                                      on_thread_id=cb)
        else:
            session = bridge.CodexSession(
                cfg["mode"], cfg["yolo"], io_out=io_out, speaker="builder",
                resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
                extra_writable_dir=sessions_dir,
                model=cfg.get("model"), effort=cfg.get("effort"))
    except policy.DispatchBlocked as exc:
        if trace:
            trace.event("role.end", role="builder", result="policy_blocked",
                        controller="codex")
        io_out.write(str(exc) + "\n")
        io_out.flush()
        _reject("policy_blocked", "bridge_backstop")
        report(_OUTCOME_ENDED, None)
        return 1
    # The codex session is genuinely live now (BL-2): the ONE point this
    # branch may legally advance preflighting -> running.
    _advance_phase(session_uuid, role_work_id, "preflight_passed",
                   source="run_builder")
    rc, outcome, payload = _role_loop(
        session, prompt, build_status_path, context, io_in, io_out,
        on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
            session_uuid=session_uuid, role_work_id=role_work_id)
    report(outcome, payload)
    return rc


# --------------------------------------------------------------------------- #
# Entry point.                                                                #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Session selection.                                                           #
#                                                                              #
# A directory holds many resumable sessions (each its own                     #
# .cowork/session.<uuid>.json, plus a legacy .cowork/session.json discovered   #
# in place). `select_session` decides which one this run uses BEFORE any       #
# team/config/phase logic, from the flags and the directory's existing         #
# sessions. Its result is an explicit tri-state so --no-session runs the flow  #
# while only a user dismissal returns rc 0.                                    #
# --------------------------------------------------------------------------- #

# path: chosen session-file path (None only on error/cancel). new_uuid: the
# minted uuid on a New path (so run_flow names the file and the internal
# session_uuid identically), else None. cancelled: user dismissed a picker/menu
# (benign rc 0). error: a message for a conflicting/invalid invocation (rc 2).
# action: an interactive action the user picked for the chosen session, handled
# later in run_flow ('edit_controllers' = the guided controller-policy flow).
# select_session owns only the CHOICE: it asks no controller questions and reads
# no saved config, because at that point neither exists yet.
SessionChoice = collections.namedtuple(
    "SessionChoice", ["path", "new_uuid", "cancelled", "error", "action"])
SessionChoice.__new__.__defaults__ = (None, None, False, None, None)


def _session_picker_label(row, now):
    """Compose a picker row: '<relative time> · <phase> — <summary|fallback>'."""
    when = ui.format_relative_time(row.get("last_active") or row.get("created"),
                                   now)
    summary = row.get("summary") or state_store.fallback_label(
        row.get("id"), row.get("created") or row.get("last_active"))
    return "%s · %s — %s" % (when, row.get("phase") or "scouting", summary)


def select_session(args, io_in, io_out, select_fn=None, now=None):
    """Decide which session this run uses. Returns a SessionChoice.

    Decision order (conflicts FIRST, before any path resolution, so an explicit
    --session-file never bypasses them):
      1. --new + --resume          -> error
         --no-session + --resume   -> error
      2. --no-session              -> run the flow with the default/explicit
                                      path; never read/written (ephemeral).
      3. --session-file            -> single-session mode (no discovery/picker).
      4. discover the directory's sessions.
      5. --new                     -> mint a fresh uuid + per-session path.
      6. --resume                  -> picker (errors with no sessions or no TTY).
      7. no flag                   -> zero sessions: mint fresh; non-interactive:
                                      most-recent; interactive: Resume/New menu.
    """
    select_fn = select_fn or ui.select
    if now is None:
        now = time.time()

    # 1. Conflict checks first (before --session-file / --no-session / discovery).
    if args.new and args.resume:
        return SessionChoice(error="--new and --resume cannot be combined.")
    if args.no_session and args.resume:
        return SessionChoice(
            error="--resume cannot be combined with --no-session "
                  "(there is no session to resume).")

    # The two session-mutating controller flags share one selection path: both
    # must land on an EXISTING saved session, and each error names the flag that
    # was actually supplied.
    controller_flag = None
    if args.switch_controller:
        controller_flag = "--switch-controller"
    elif getattr(args, "allow_controllers", None) is not None:
        controller_flag = "--allow-controllers"
    if controller_flag:
        if args.no_session:
            return SessionChoice(
                error="%s cannot be combined with --no-session "
                      "(it must update an existing saved session)."
                      % controller_flag)
        if args.new:
            return SessionChoice(
                error="%s cannot be combined with --new "
                      "(it must update an existing saved session)."
                      % controller_flag)
        if args.team:
            return SessionChoice(
                error="%s cannot be combined with --team "
                      "(it reuses the saved team)." % controller_flag)
        if args.config:
            return SessionChoice(
                error="%s cannot be combined with --config "
                      "(it reuses the saved role config)." % controller_flag)

        cwd = os.getcwd()
        interactive_picker_ok = ui.is_tty(io_in) and ui.is_tty(io_out)
        if args.session_file:
            if not os.path.exists(args.session_file):
                return SessionChoice(
                    error="%s: session file does not exist: %s"
                          % (controller_flag, args.session_file))
            return SessionChoice(path=args.session_file)

        discovered = state_store.list_sessions(cwd)
        if not discovered:
            return SessionChoice(
                error="%s: no saved sessions found in %s."
                      % (controller_flag, state_store.session_dir(cwd)))

        def run_picker():
            choices = [(row["path"], _session_picker_label(row, now))
                       for row in discovered]
            chosen = select_fn("Switch controller in which session?", choices)
            if not chosen:
                return SessionChoice(cancelled=True)
            return SessionChoice(path=chosen)

        if args.resume:
            if not interactive_picker_ok:
                return SessionChoice(
                    error="--resume with %s needs an interactive terminal; use "
                          "--session-file instead." % controller_flag)
            return run_picker()
        if len(discovered) == 1:
            return SessionChoice(path=discovered[0]["path"])
        if interactive_picker_ok:
            return run_picker()
        return SessionChoice(
            error="%s found multiple saved sessions; pass --session-file to "
                  "choose one." % controller_flag)

    # 2. --no-session: not cancelled, not error — the flow still runs with an
    # ephemeral session; the path is computed exactly as today but never read or
    # written because session_enabled stays False downstream.
    if args.no_session:
        return SessionChoice(
            path=args.session_file or state_store.session_path())

    # 3. --session-file forces single-session mode: operate on that exact file,
    # skipping discovery and the picker (preserves existing scripts/tests).
    if args.session_file:
        return SessionChoice(path=args.session_file)

    cwd = os.getcwd()
    interactive_picker_ok = (not _is_non_interactive(args)
                             and ui.is_tty(io_in) and ui.is_tty(io_out))

    # 4. Discover this directory's sessions (newest-first).
    discovered = state_store.list_sessions(cwd)

    def mint_new():
        u = str(uuid.uuid4())
        return SessionChoice(path=state_store.new_session_path(cwd, u),
                             new_uuid=u)

    def run_picker():
        choices = [(row["path"], _session_picker_label(row, now))
                   for row in discovered]
        chosen = select_fn("Resume which session?", choices)
        if not chosen:
            return SessionChoice(cancelled=True)
        return SessionChoice(path=chosen)

    # 5. --new: skip the prompt, fresh session.
    if args.new:
        return mint_new()

    # 6. --resume: jump straight to the picker.
    if args.resume:
        if not discovered:
            return SessionChoice(
                error="--resume: no sessions to resume in %s."
                      % state_store.session_dir(cwd))
        if not interactive_picker_ok:
            return SessionChoice(
                error="--resume needs an interactive terminal; direct resume "
                      "by id is out of scope.")
        return run_picker()

    # 7. No flag.
    if not discovered:
        return mint_new()  # nothing to resume -> start fresh, no prompt
    if not interactive_picker_ok:
        # Piped/scripted: continue the most-recent session (today's behavior).
        return SessionChoice(path=discovered[0]["path"])
    # The third entry is the INTERACTIVE equivalent of --allow-controllers /
    # --switch-controller. It appears only here — a TTY with at least one
    # discovered session — so --new/--resume/--no-session, the piped/scripted
    # path and headless never produce it. select_session only returns the
    # ACTION; every prompt, validation, write and resume happens later at
    # run_flow's single controller-update call site.
    choice = select_fn("Resume an existing session or start a new one?",
                       [("resume", "Resume an existing session"),
                        ("new", "Start a new session"),
                        ("controllers", "Change this session's controllers")])
    if choice == "resume":
        return run_picker()
    if choice == "new":
        return mint_new()
    if choice == "controllers":
        picked = run_picker()
        if picked.path is None:
            return picked  # cancelled at the picker
        return SessionChoice(path=picked.path, action="edit_controllers")
    return SessionChoice(cancelled=True)  # menu dismissed


def effective_phase_for(state, selected):
    """Apply the persisted phase fallback rules for the saved team."""
    phase = state_store.get_phase(state)
    planner_on_team = "planner" in selected
    builder_on_team = "builder" in selected
    if phase == "building" and not builder_on_team:
        phase = "planning"
    if phase == "planning" and not planner_on_team:
        phase = "scouting"
    return phase


def alternate_controller(controller):
    """Fallback target when a switch is requested without an explicit target on
    a session with NO policy. claude <-> codex stay a toggle; opencode falls
    back to claude."""
    return "codex" if controller == "claude" else "claude"


def eligible_controllers(current, allowed=None):
    """The controllers a role on `current` may be switched to under `allowed`
    (None = unrestricted). Thin delegation to the shared policy helper so the
    recovery gates and the policy decision can never disagree."""
    return policy.eligible(allowed, current)


def gate_eligible_for(controller):
    """The eligible-controller list a recovery gate should offer for a role
    currently on `controller`, or None when this session carries NO policy.

    None is the back-compat signal: the gate then renders today's single
    "alternate controller" option, wording and return sentinels byte-identical,
    which is what keeps every pre-existing gate test untouched. Reads the
    process-global active policy directly, so no eligible-list parameter has to
    be threaded through the role loops."""
    meta = policy.active_meta()
    if meta.get("mode") == "allowed":
        return policy.eligible(meta.get("allowed"), controller)
    if meta.get("mode") == "invalid":
        # Unreadable policy: nothing may start, so no switch target exists.
        return []
    return None


class _SwitchTo:
    """A recovery-gate choice naming a SPECIFIC controller (the policy-aware
    rendering). The policy-free rendering keeps returning the historical
    `_STUCK_SWITCH` / `_CTRL_SWITCH` / `_REVFAIL_SWITCH` sentinels."""

    __slots__ = ("target",)

    def __init__(self, target):
        self.target = target

    def __repr__(self):
        return "_SwitchTo(%r)" % self.target

    def __eq__(self, other):
        return isinstance(other, _SwitchTo) and other.target == self.target

    def __hash__(self):
        return hash(("_SwitchTo", self.target))


def _switch_target_of(action):
    """The controller a gate choice names, or None for the generic sentinel."""
    return getattr(action, "target", None)


def _call_reviewer_controller(review_fn):
    """The paired reviewer's CURRENT controller, or None when the review_fn does
    not expose one (test-injected doubles). Used only to compute the reviewer
    gate's eligible list; None simply yields the unrestricted rendering."""
    getter = getattr(review_fn, "reviewer_controller", None)
    if getter is None:
        return None
    try:
        return getter() if callable(getter) else getter
    except Exception:  # noqa: BLE001 - a gate must never crash on a double
        return None


def _call_reviewer_switch(switcher, reason, target):
    """Invoke a reviewer switch callable, passing `target` only when the
    callable actually accepts it — test-injected doubles keep the historical
    `switch_controller(reason=...)` signature."""
    if target is not None:
        try:
            params = inspect.signature(switcher).parameters
        except (TypeError, ValueError):
            params = {}
        if "target" in params:
            return switcher(reason=reason, target=target)
    return switcher(reason=reason)


def validate_switch_role(role, target, phase, selected, state,
                         effective_allowed=None):
    """Validate one ROLE=CONTROLLER move. With `effective_allowed=None`
    (unrestricted) the checks and messages are unchanged; with an allowed set,
    a target outside it is rejected AFTER the existing four checks so an
    off-phase or unknown role still reports its own, more specific problem."""
    if not state_store.has_config(state):
        return "--switch-controller requires a saved session with saved team/config."
    if role not in selected:
        return "role %r is not on the saved team." % role
    if role not in PHASE_PAIRS.get(phase, ()):
        return (
            "role %r is not switchable in the current %s phase; choose one of: %s."
            % (role, phase, ", ".join(PHASE_PAIRS.get(phase, ()))))
    if target not in CONTROLLERS:
        return "controller must be one of: %s." % ", ".join(CONTROLLERS)
    if not policy.is_allowed(effective_allowed, target):
        return (
            "cannot move %s to %s: this session allows only %s. Pass "
            "--allow-controllers to change what the session permits."
            % (role, target, policy.format_allowed(effective_allowed)))
    return None


def validate_controller_proposal(proposal, saved_allowed, phase, selected,
                                 state):
    """Validate a whole controller update BEFORE anything is written or started.

    Resolves the effective allowed set ONCE (PRESERVE -> the currently saved
    set, ALL -> unrestricted, a tuple -> that tuple) and judges everything
    against it: duplicate roles, each mapping via `validate_switch_role`, and
    whether the CURRENT PHASE would still be compliant afterwards.

    Returns `(effective_allowed, error_message, warnings)`. `error_message` is
    None when the proposal is acceptable. `warnings` lists roles OUTSIDE the
    current phase that would be left on a now-disallowed controller: those are
    deliberately left untouched (a policy change never reassigns a role on its
    own) and fail closed if they are ever dispatched. A PRESERVE proposal
    produces no warnings — nothing about the policy changed."""
    try:
        effective = policy.effective_allowed(proposal.policy, saved_allowed)
    except ValueError as exc:
        return (None, str(exc), [])

    mappings = list(proposal.mappings or [])
    seen = {}
    for role, target in mappings:
        if role in seen:
            # ANY second occurrence of a role is rejected, whether or not the
            # targets agree. A repeated identical mapping is not harmless: the
            # transition would apply the role switch twice, and the second pass
            # reads the ALREADY-SWITCHED controller — producing a
            # from == to pending_switches marker and a duplicated switch line.
            detail = ("twice" if seen[role] == target
                      else "with two different controllers (%s and %s)"
                      % (seen[role], target))
            return (effective,
                    "role %r was named %s in one update; name it once."
                    % (role, detail), [])
        seen[role] = target

    for role, target in mappings:
        err = validate_switch_role(role, target, phase, selected, state,
                                   effective_allowed=effective)
        if err:
            return (effective, err, [])

    # Current-phase conformance: every current-phase role on the saved team must
    # END UP inside the effective set. Roles from finished phases only warn.
    config = (state or {}).get("config") or {}
    current_pair = PHASE_PAIRS.get(phase, ())
    for role in current_pair:
        if role not in selected or role not in config:
            continue
        target = seen.get(role, config[role].get("controller"))
        if not policy.is_allowed(effective, target):
            return (effective,
                    "%s is on %s, which this session would no longer allow "
                    "(allowed: %s). Add --switch-controller %s=<controller> to "
                    "move it in the same command."
                    % (role, target, policy.format_allowed(effective), role),
                    [])

    warnings = []
    if proposal.policy is not policy.PRESERVE:
        for role in selected:
            if role in current_pair or role not in config:
                continue
            target = seen.get(role, config[role].get("controller"))
            if not policy.is_allowed(effective, target):
                warnings.append(
                    "%s (not in the current %s phase) stays on %s, which this "
                    "session no longer allows; it will be blocked if reached."
                    % (role, phase, target))
    return (effective, None, warnings)


def interactive_proposal_policy(chosen, saved_allowed):
    """Map the interactive screen's chosen set onto the SAME three-way
    representation the flags produce, so both surfaces persist identical state
    for the same intent.

    The PRESERVE rule is checked FIRST: a user who opens the guided flow only to
    move a role, leaving the pre-checked boxes alone, must not rewrite the
    policy field where the equivalent CLI invocation leaves it byte-identical.
    Choosing all three controllers is the interactive `--allow-controllers all`.
    """
    picked = policy.normalize(chosen)
    if saved_allowed is not None and picked == policy.normalize(saved_allowed):
        return policy.PRESERVE
    if picked == policy.CONTROLLERS:
        return policy.ALL
    return picked


def _policy_effect_sentence(proposal_policy, effective):
    """What the confirmation summary says is about to happen to the allowed
    set — in words, so the user confirms the actual effect."""
    if proposal_policy is policy.PRESERVE:
        return "leaving the allowed controllers unchanged (%s)" % (
            policy.format_allowed(effective))
    if proposal_policy is policy.ALL:
        return "removing the restriction — this session may use any controller"
    return "restricting this session to %s" % policy.format_allowed(effective)


def prompt_controller_update(saved_kind, saved_raw, config, selected, phase,
                             io_out, multiselect_fn=None, select_fn=None,
                             confirm_fn=None):
    """The guided interactive controller update. Returns a ControllerProposal,
    or None when the user cancelled (dismissing ANY prompt cancels the whole
    invocation cleanly — nothing written, nothing resumed).

    Runs at the SAME call site as the CLI update, after the session state,
    config and phase are resolved, so it can pre-check the current policy and
    only offer real roles and real targets. Declining the final summary re-opens
    the allowed-set prompt once so a mistake is fixable."""
    multiselect_fn = multiselect_fn or ui.multiselect
    select_fn = select_fn or ui.select
    confirm_fn = confirm_fn or ui.confirm
    saved_allowed = saved_raw if saved_kind == "allowed" else None
    # An unrestricted OR unreadable policy pre-checks everything: there is no
    # meaningful saved set to reproduce, and pre-checking all three makes the
    # obvious confirm ("leave it open") the safe one.
    precheck = list(saved_allowed) if saved_allowed else list(policy.CONTROLLERS)

    for attempt in range(2):
        chosen = multiselect_fn(
            "Which controllers may this session use?",
            [(c, c) for c in policy.CONTROLLERS], selected=precheck)
        if not chosen:
            return None  # dismissed, or nothing checked -> cancel, never "none"
        try:
            proposal_policy = interactive_proposal_policy(chosen, saved_allowed)
        except ValueError:
            return None
        effective = policy.effective_allowed(proposal_policy, saved_allowed)

        mappings = []
        cancelled = False
        for role in PHASE_PAIRS.get(phase, ()):
            if role not in selected or role not in config:
                continue
            current = (config[role] or {}).get("controller")
            if policy.is_allowed(effective, current):
                continue
            targets = [c for c in policy.CONTROLLERS
                       if policy.is_allowed(effective, c) and c != current]
            picked = select_fn(
                "%s is on %s, which the new set does not allow — move it to:"
                % (role, current), [(c, c) for c in targets])
            if not picked:
                cancelled = True
                break
            mappings.append((role, picked))
        if cancelled:
            return None

        lines = ["This will commit, as one update:",
                 "  - " + _policy_effect_sentence(proposal_policy, effective)]
        for role, target in mappings:
            lines.append("  - moving %s to %s" % (role, target))
        if not mappings:
            lines.append("  - no role moves")
        for role in selected:
            if role in PHASE_PAIRS.get(phase, ()) or role not in config:
                continue
            current = (config[role] or {}).get("controller")
            if not policy.is_allowed(effective, current):
                lines.append(
                    "  - warning: %s (not in the current %s phase) stays on %s "
                    "and will be blocked if reached" % (role, phase, current))
        io_out.write("\n".join(lines) + "\n")
        io_out.flush()
        answer = confirm_fn("Apply this controller update?")
        if answer:
            return policy.ControllerProposal(
                proposal_policy, tuple(mappings), "interactive")
        if attempt == 0:
            continue  # a 'no' re-opens the allowed-set prompt exactly once
        return None
    return None


def controller_policy_invalid_text(session_file, enabled=False):
    """The one message shown when a saved policy cannot be read. Names the
    session file and BOTH repair routes; nothing can launch while this state is
    loaded, so the message has to be actionable on its own."""
    return (
        "cowork: this session's saved controller policy is unreadable, so "
        "nothing can start.\n"
        "  session file: %s\n"
        "  fix it either way:\n"
        "    - re-run with --allow-controllers claude,codex (or 'all' to remove "
        "the restriction), which replaces the saved policy outright; or\n"
        "    - remove the controller_policy field from the session file by "
        "hand.\n"
        "  read-only commands (--check, --report) keep working meanwhile.\n"
        % ui.render_path(session_file, enabled))


def switch_handoff_packet(role, phase, pending_switch, artifact_paths=None,
                          shared_context="", pending_turn=None, assets_dir=None,
                          context_revision=None):
    """Fresh-provider controller-switch handoff (route 11), delivered FILE-ONLY
    via the shared transport. The switch carries only content-free facts inline
    (phase, role, from/to controller, and a NORMALIZED reason/source CODE when
    one exists); every body — the shared context, the artifact files, any
    free-form switch reason/source/diagnostic, and the failed pending turn — is
    materialized to a file (or already on disk) and carried by PATH. The switched
    role reads them from disk and then processes the failed pending turn."""
    if not pending_switch:
        return ""
    facts = {
        "phase": phase or "unknown",
        "role": role or "unknown",
        "from_controller": pending_switch.get("from_controller") or "unknown",
        "to_controller": pending_switch.get("to_controller") or "unknown",
    }
    artifacts = []
    if shared_context:
        artifacts.append(_shared_context_artifact(shared_context, assets_dir, context_revision))
    # Free-form reason/source (whitespace / authored text) rides a file; a
    # normalized single-token code may ride inline as a content-free fact.
    recovery_bits = []
    for key, fact_key in (("reason", "reason_code"), ("source", "source_code")):
        value = pending_switch.get(key)
        if not value:
            continue
        if handoff.is_content_free_token(value):
            facts[fact_key] = value
        else:
            recovery_bits.append("%s: %s" % (key, value))
    if recovery_bits:
        path = handoff.persist_switch_recovery_file(
            assets_dir, role, "\n".join(recovery_bits))
        artifacts.append(
            (path and {"label": "switch recovery note (free-form)",
                       "path": os.path.abspath(path), "kind": "markdown",
                       "source": "recovery"})
            or _tempfile_artifact("\n".join(recovery_bits),
                                  "switch recovery note (free-form)",
                                  prefix="cowork_switch_", suffix=".txt",
                                  source="recovery"))
    for path in artifact_paths or []:
        if not path:
            continue
        abs_p = os.path.abspath(path)
        if any(a.get("path") == abs_p for a in artifacts):
            continue
        artifacts.append({"label": "session artifact (%s)"
                          % os.path.basename(path),
                          "path": abs_p,
                          "kind": "json" if str(path).endswith(".json")
                          else "markdown", "source": "artifacts"})
    if pending_turn:
        path = handoff.persist_pending_turn_file(assets_dir, role, pending_turn)
        artifacts.append(
            (path and {"label": "failed pending turn (process it after "
                       "orienting)", "path": os.path.abspath(path),
                       "kind": "markdown", "source": "pending_turn"})
            or _tempfile_artifact(pending_turn, "failed pending turn (process "
                                  "it after orienting)",
                                  prefix="cowork_pending_", suffix=".txt",
                                  source="pending_turn"))
    return handoff.render_handoff(
        "controller->switch", artifacts=artifacts, facts=facts)


def pending_resume_packet(role, phase, pending_entry, artifact_paths=None,
                          shared_context="", pending_turn=None, assets_dir=None,
                          context_revision=None):
    """Same-controller failed-turn resume handoff (edge lead->pending_resume), delivered
    FILE-ONLY via the shared transport without switch markers or fake switch premises.
    """
    if not (pending_entry or pending_turn):
        return ""
    facts = {
        "phase": phase or "unknown",
        "role": role or "unknown",
    }
    artifacts = []
    if shared_context:
        artifacts.append(_shared_context_artifact(shared_context, assets_dir, context_revision))
    recovery_bits = []
    if isinstance(pending_entry, dict):
        for key, fact_key in (("reason", "reason_code"), ("source", "source_code")):
            value = pending_entry.get(key)
            if not value:
                continue
            if handoff.is_content_free_token(value):
                facts[fact_key] = value
            else:
                recovery_bits.append("%s: %s" % (key, value))
    if recovery_bits:
        path = handoff.persist_switch_recovery_file(
            assets_dir, role, "\n".join(recovery_bits))
        artifacts.append(
            (path and {"label": "switch recovery note (free-form)",
                       "path": os.path.abspath(path), "kind": "markdown",
                       "source": "recovery"})
            or _tempfile_artifact("\n".join(recovery_bits),
                                  "switch recovery note (free-form)",
                                  prefix="cowork_switch_", suffix=".txt",
                                  source="recovery"))
    for path in artifact_paths or []:
        if not path:
            continue
        abs_p = os.path.abspath(path)
        if any(a.get("path") == abs_p for a in artifacts):
            continue
        artifacts.append({"label": "session artifact (%s)"
                          % os.path.basename(path),
                          "path": abs_p,
                          "kind": "json" if str(path).endswith(".json")
                          else "markdown", "source": "artifacts"})
    pt = pending_turn or (pending_entry.get("pending_turn") if isinstance(pending_entry, dict) else None)
    if pt:
        path = handoff.persist_pending_turn_file(assets_dir, role, pt)
        artifacts.append(
            (path and {"label": "failed pending turn (process it after "
                       "orienting)", "path": os.path.abspath(path),
                       "kind": "markdown", "source": "pending_turn"})
            or _tempfile_artifact(pt, "failed pending turn (process "
                                  "it after orienting)",
                                  prefix="cowork_pending_", suffix=".txt",
                                  source="pending_turn"))
    return handoff.render_handoff(
        "lead->pending_resume", artifacts=artifacts, facts=facts)


def run_flow(args, io_in=None, io_out=None, which=None, run_scout_fn=None,
             run_planner_fn=None, run_builder_fn=None, run_worktree_fn=None):
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    run_scout_fn = run_scout_fn or run_scout
    run_planner_fn = run_planner_fn or run_planner
    run_builder_fn = run_builder_fn or run_builder
    run_worktree_fn = run_worktree_fn or run_worktree
    interactive = not _is_non_interactive(args)
    headless = bool(getattr(args, "headless", False))
    worktree_requested = bool(getattr(args, "worktree", None))
    # The builder and reviewer CLI sessions spawn in the process cwd, so their
    # `git diff` is relative to cwd — NOT to the session-file parent (which may
    # live outside the repo when --session-file points elsewhere). The build
    # baseline must be read from the same cwd to match what they see.
    run_cwd = os.getcwd()

    # Headless requires its initial context up front (F2_context_required): no
    # human will be prompted, so a missing --context/--context-file is a hard
    # error before any phase runs.
    if headless and args.context is None and not args.context_file:
        io_out.write("cowork: --headless requires initial context; pass "
                     "--context or --context-file.\n")
        return 2

    # Deterministic --worktree git gate (D1): runs early, before session
    # selection, so a non-git launch fails fast with rc 2 and no half-init. The
    # base is the single launch toplevel — NOT discover_git_roots (single repo
    # only). Carried to the worktree creation block below.
    worktree_base = None
    if worktree_requested:
        worktree_base = git_worktree_toplevel(run_cwd)
        if worktree_base is None:
            io_out.write("cowork: --worktree requires launching inside a git "
                         "work tree; %s is not one.\n" % run_cwd)
            return 2
    # The real worktree path this session's roles dispatch into, once
    # created/reused below — None until then (and always None when no
    # worktree was requested), never invented. Bound to every role manifest
    # compiled after that point so `binding.worktree`/`check_cwd` reflect the
    # actual working directory, not the launch directory.
    active_worktree = None
    # The real runtime root that CONTAINS active_worktree, derived from the
    # validated worktree path itself (its own parent directory) rather than
    # assumed to be worktree_base (the base repo toplevel): roles/worktree.md
    # documents BOTH a nested convention (base/.worktrees/<name>, a
    # descendant of worktree_base) AND a sibling convention
    # (../<repo>-worktrees/<name>, a descendant of worktree_base's PARENT,
    # not of worktree_base itself). Taking the validated worktree's own
    # dirname is correct for either convention (or any other a repo
    # documents) without broadening trust beyond what was actually created
    # and independently verified by validate_worktree.
    active_worktree_root = None

    # Session store: select which of the directory's sessions this run uses
    # (resume-or-new prompt, --new/--resume, picker) BEFORE any team/config/phase
    # logic. The result is an explicit tri-state: error -> rc 2; cancelled -> rc 0
    # (benign); else proceed with the chosen path.
    session_enabled = not args.no_session
    choice = select_session(args, io_in, io_out)
    if choice.error or choice.cancelled:
        # No session was chosen, so there is no session_uuid to key a trace on:
        # record run.end under an ephemeral uuid (only when persistence is on)
        # so these early exits are still traced, exactly as the plan prescribes.
        eph_uuid = str(uuid.uuid4())
        etrace = trace_store.Trace(
            trace_store.trace_path_for(eph_uuid) if session_enabled else None,
            session_uuid=eph_uuid, enabled=session_enabled)
        if choice.error:
            etrace.event("run.end", rc=2, reason="session_select_error")
            io_out.write("cowork: " + choice.error + "\n")
            return 2
        etrace.event("run.end", rc=0, reason="session_select_cancelled")
        io_out.write("cowork: cancelled; nothing to do.\n")
        return 0
    spath = choice.path
    saved = state_store.load(spath) if session_enabled else None
    # Both session-mutating controller flags — and the interactive equivalent —
    # need a loadable saved session with saved team/config; each error names the
    # flag that was actually supplied.
    controller_update_requested = (
        bool(args.switch_controller) or args.allow_controllers is not None
        or choice.action == "edit_controllers")
    if controller_update_requested and session_enabled:
        controller_flag = ("--switch-controller" if args.switch_controller
                           else "--allow-controllers"
                           if args.allow_controllers is not None
                           else "changing this session's controllers")
        reason = None
        message = None
        if saved is None:
            reason = "switch_controller_unloadable_session"
            message = (
                "%s: session file is not a loadable cowork session: %s"
                % (controller_flag, spath))
        elif not state_store.has_config(saved):
            reason = "switch_controller_missing_config"
            message = (
                "%s requires a saved session with saved team/config."
                % controller_flag)
        if message:
            eph_uuid = (state_store.get_session_uuid(saved)
                        if isinstance(saved, dict)
                        and state_store.get_session_uuid(saved)
                        else str(uuid.uuid4()))
            etrace = trace_store.Trace(
                trace_store.trace_path_for(eph_uuid),
                session_uuid=eph_uuid, enabled=True)
            etrace.event("run.end", rc=2, reason=reason)
            io_out.write("cowork: " + message + "\n")
            return 2
    # cowork session UUID (distinct from any claude/codex session id): names this
    # session's assets, e.g. the scout intel file. On a New path, reuse the uuid
    # select_session minted into the filename so the filename uuid, the internal
    # session_uuid, and the ~/.cowork/sessions/<uuid>/ assets key always agree.
    if session_enabled:
        saved = state_store.ensure_session(
            spath, saved, choice.new_uuid or str(uuid.uuid4()))
        session_uuid = state_store.get_session_uuid(saved)
    else:
        session_uuid = str(uuid.uuid4())
    trace = trace_store.Trace(
        trace_store.trace_path_for(session_uuid) if session_enabled else None,
        session_uuid=session_uuid,
        enabled=session_enabled,
    )
    trace.event("run.start", cwd=os.getcwd(), session_file=spath,
                session_enabled=session_enabled)
    # The user-visible lever on measurement overhead. A CLI value is persisted
    # so the choice survives a resume; otherwise the saved value stands, and the
    # default is `all_rounds`. Whatever it is, the overhead of scoring is
    # reported as its own cost class, so the choice can be made from data.
    evaluation_policy = getattr(args, "evaluation_policy", None)
    if evaluation_policy and session_enabled:
        try:
            saved = state_store.save_evaluation_policy(
                spath, evaluation_policy, prior=saved)
        except ValueError:
            pass
    elif not evaluation_policy:
        evaluation_policy = state_store.get_evaluation_policy(saved)
    trace.event("evaluation.policy", policy=evaluation_policy)
    # `user_wait` needs an emitter at the six blocking prompts, whose signatures
    # carry no trace parameter (P15). Same process-global pattern the controller
    # policy already uses and documents: one cowork process, one session.
    trace_store.set_active(trace)
    reuse_config = (session_enabled and state_store.has_config(saved)
                    and not args.team and not args.config)

    # Step 1: team. When both team and config are interactive they run as one
    # merged flow (checkbox <-> config screen with back navigation).
    merged_config = None
    if args.team:
        selected, err = parse_team(args.team)
        if err:
            trace.event("run.end", rc=2, reason="parse_team_error")
            io_out.write("cowork: " + err + "\n")
            return 2
    elif reuse_config:
        selected = [r for r in ROLES if r in saved["team"]]
    elif interactive and not args.config:
        selected, merged_config = select_and_configure_interactive()
    elif interactive:
        selected = select_team_interactive()
    else:
        selected = list(ROLES)
    if not selected:
        trace.event("run.end", rc=0, reason="no_roles_selected")
        io_out.write("cowork: no roles selected; nothing to do.\n")
        return 0

    # Step 2: config.
    config = default_config(selected)
    if args.config:
        ok, err = apply_config_args(config, args.config)
        if not ok:
            trace.event("run.end", rc=2, reason="config_error")
            io_out.write("cowork: " + err + "\n")
            return 2
    elif reuse_config:
        # normalize: older saved sessions predate the model/effort keys.
        config = {r: normalize_role_config(saved["config"][r])
                  for r in selected if r in saved["config"]}
        io_out.write("cowork: using saved session config (%s)\n" % spath)
    elif merged_config is not None:
        config = merged_config
    elif interactive:
        config = configure_roles_interactive(selected)
    trace.event("run.config", selected=selected, reuse_config=reuse_config,
                config={r: dict(config[r]) for r in selected if r in config})

    # Persist team + config the first time (or whenever freshly chosen).
    if session_enabled and not reuse_config:
        saved = state_store.save_config(spath, selected, config, prior=saved or {})

    # Global preflight (Python + interactive UI packages only). Controller
    # executables are checked on-demand when each role is about to launch, so a
    # missing active controller can reach the switch-controller recovery gate.
    kwargs = {"interactive": interactive}
    if which is not None:
        kwargs["which"] = which
    ok, alerts = preflight.preflight({}, **kwargs)
    trace.event("preflight.result", ok=ok, alerts_count=len(alerts))
    if not ok:
        trace.event("run.end", rc=1, reason="preflight_failed")
        io_out.write("cowork preflight failed:\n")
        for alert in alerts:
            io_out.write("  - " + alert + "\n")
        io_out.flush()
        return 1

    # Phase: resume into the persisted phase (default scouting). The cascade
    # falls back when the resumed phase's lead role is not on the team: a
    # `building` phase without a builder falls back to planning; a `planning`
    # phase without a planner falls back to scouting.
    phase = effective_phase_for(saved, selected) if session_enabled else "scouting"
    planner_on_team = "planner" in selected
    builder_on_team = "builder" in selected
    if phase == "scouting" and "scout" not in selected:
        trace.event("run.end", rc=0, reason="scout_not_selected")
        if planner_on_team:
            io_out.write(
                "cowork: scout not selected. Planning requires approved scout "
                "intel: add the scout role to the team (a session already in "
                "the planning phase resumes without re-running the scout).\n")
        else:
            io_out.write(
                "cowork: scout not selected. Every cowork run begins with the "
                "scouting phase; add the scout role to the team (a session "
                "already past scouting resumes into its saved phase).\n")
        return 0

    # Saved CLI session ids per role. With the session store enabled they are
    # persisted; otherwise they are kept in-run only, so phase chaining (and a
    # hand-back round trip) can still resume sessions within this run.
    holder = {"state": saved}
    local_ids = {}

    def role_resume_id(role):
        if role not in config:
            return None
        controller = config[role]["controller"]
        if session_enabled:
            return state_store.get_role_session(holder["state"], role, controller)
        entry = local_ids.get(role)
        if entry and entry[0] == controller:
            return entry[1]
        return None

    def role_saver(role):
        def on_sess(controller, sid):
            if not sid:
                return
            if session_enabled:
                holder["state"] = state_store.save_role_session(
                    spath, role, controller, sid, prior=holder["state"])
            local_ids[role] = (controller, sid)
            trace.event("role.session_saved", role=role,
                        controller=controller, session_id=sid)
        return on_sess

    pending_switches = {}
    pending_switch_turns = {}
    if session_enabled and holder.get("state"):
        for r, entry in (holder["state"].get("pending_switches") or {}).items():
            if isinstance(entry, dict) and entry.get("pending_turn"):
                pending_switch_turns[r] = entry["pending_turn"]

    def check_controller_tool(controller):
        ok, alerts = preflight.check_tools(
            [controller], which=which if which is not None else shutil.which)
        runtime_ok, runtime_alerts = preflight.check_governed_runtime(
            [controller])
        return ok and runtime_ok, alerts + runtime_alerts

    def reviewer_controller_check(role):
        if role not in config:
            return None
        controller = config[role].get("controller")
        # Policy first: a reviewer on a disallowed controller is blocked before
        # its executable is even looked for, and never spawned. No manifest
        # exists yet at this point (this is a pre-check ahead of the real
        # reviewer dispatch, which compiles and binds its own manifest inside
        # run_reviewer_once), so nothing is bound here.
        _rcf = _guard_to_policy_fact(controller, role, phase=phase, trace=trace)
        _rcdec = _decide_and_trace(
            trace, role, controller, "review",
            "run_flow.reviewer_controller_check", policy_result=_rcf,
            phase=phase)
        if _rcdec["outcome"] == "refuse":
            trace.event("review.controller_policy_blocked", role=role,
                        phase=phase, controller=controller)
            return [_rcdec["refusal_message"]]
        ok, alerts = check_controller_tool(controller)
        if ok:
            return None
        trace.event("review.controller_preflight_failed", role=role,
                    phase=phase, controller=controller,
                    alerts_count=len(alerts))
        return alerts

    def default_switch_target(current):
        """The implicit target for a switch with no explicit one. Under a
        policy, the first ELIGIBLE controller; otherwise today's toggle."""
        eligible = gate_eligible_for(current)
        if eligible is None:
            return alternate_controller(current)
        return eligible[0] if eligible else None

    def switch_controller(role, reason=None, target=None, source="gate", pending_turn=None):
        if role not in config:
            io_out.write("cowork: cannot switch %s — role is not configured.\n"
                         % role)
            return False
        current = config[role].get("controller")
        target = target or default_switch_target(current)
        if target is None:
            io_out.write(
                "cowork: cannot switch %s — this session's controller policy "
                "leaves no other controller available (allowed: %s).\n"
                % (role, policy.format_allowed(policy.active_allowed())))
            trace.event("controller.switch.end", role=role, phase=phase,
                        result="no_eligible_controller",
                        allowed=list(policy.active_allowed() or ()))
            return False
        trace.event("controller.switch.request", role=role, phase=phase,
                    source=source, reason=reason, from_controller=current,
                    to_controller=target)
        if target == current:
            io_out.write("cowork: %s is already using %s.\n" % (role, target))
            trace.event("controller.switch.end", role=role, phase=phase,
                        result="already_current", controller=target)
            return False
        # The policy decision runs FIRST — before the executable preflight and
        # before the claude probe below — so a disallowed target is never
        # preflighted, never probed, and never spawned. No manifest exists yet
        # at this point (it is compiled below, only for an allowed target), so
        # nothing is bound here — binding a not-yet-compiled manifest would be
        # inventing an identifier.
        _swf = _guard_to_policy_fact(target, role, phase=phase, trace=trace)
        _swdec = _decide_and_trace(
            trace, role, target, "switch", "run_flow.switch_controller",
            policy_result=_swf, phase=phase)
        if _swdec["outcome"] == "refuse":
            io_out.write(_swdec["refusal_message"] + "\n")
            io_out.flush()
            trace.event("controller.switch.end", role=role, phase=phase,
                        result="policy_blocked", controller=target)
            return False
        ok, alerts = check_controller_tool(target)
        if not ok:
            trace.event("controller.switch.preflight_failed", role=role,
                        phase=phase, target_controller=target,
                        alerts_count=len(alerts))
            io_out.write("cowork: cannot switch %s to %s yet:\n" % (role, target))
            for alert in alerts:
                io_out.write("  - " + alert + "\n")
            io_out.flush()
            return False
        if target == "claude":
            cfg = dict(config[role])
            prompt_path = ROLE_PROMPT_PATHS.get(role)
            ok, alert = _with_status_spinner(
                io_out, "checking claude for %s" % role,
                lambda: bridge.probe_claude_stream_json(
                    bridge._real_claude_spawn, mode=cfg["mode"],
                    yolo=cfg["yolo"], role_prompt_file=prompt_path,
                    trace=trace, role=role,
                    extra_writable_dir=state_store.session_assets_dir(
                        session_uuid),
                    cache_enabled=True))
            if not ok:
                trace.event("controller.switch.probe_failed", role=role,
                            phase=phase, target_controller=target)
                io_out.write("cowork: cannot switch %s to claude: %s\n"
                             % (role, alert))
                io_out.flush()
                return False
        if session_uuid:
            _sw_cfg = config.get(role) or {}
            _sw_sdir = state_store.session_assets_dir(session_uuid)
            try:
                _sw_artifacts = switch_artifacts_for(role)
                _sw_manifest, _ = _compile_role_manifest(
                    role=role, session_uuid=session_uuid, work_id=role,
                    controller=target,
                    mode=_sw_cfg.get("mode", "implement"),
                    model=_sw_cfg.get("model"), effort=_sw_cfg.get("effort"),
                    instruction_paths=[ROLE_PROMPT_PATHS.get(role)
                                       or SCOUT_PROMPT_PATH],
                    sessions_dir=_sw_sdir,
                    worktree=active_worktree, worktree_base=active_worktree_root,
                    candidate_snapshot=(
                        _file_snapshot(_sw_artifacts[0])
                        if _sw_artifacts else None),
                    force_recompile=True)
            except Exception:
                _sw_manifest = {}
            _swmdec = _decide_and_trace(
                trace, role, target, "switch", "run_flow.switch_controller",
                manifest=_sw_manifest,
                preflight_result=_manifest_preflight_fact(_sw_manifest),
                phase=phase)
            if _swmdec["outcome"] == "refuse":
                _emit_dispatch_escalation(trace, role, "manifest_proven",
                                          "recompile and preflight the manifest",
                                          "switch_controller")
                io_out.write(
                    "cowork: manifest not proven for %s switch — blocked.\n"
                    % role)
                io_out.flush()
                return False
        entry = {
            "from_controller": current,
            "to_controller": target,
            "reason": reason,
            "source": source,
            "created": time.time(),
        }
        pt = pending_turn if pending_turn is not None else pending_switch_turns.get(role)
        if session_enabled:
            # One mapping, set_policy defaulting to False: a gate/recovery
            # switch is a single-write, POLICY-PRESERVING transition — exactly
            # the semantics the CLI mapping-only path has.
            holder["state"] = state_store.apply_controller_transition(
                spath, [(role, target)], prior=holder["state"], reason=reason,
                source=source, created=entry["created"],
                pending_turns={role: pt} if pt is not None else None)
            # Keep the in-memory config in lockstep with the saved config.
            config[role] = dict(holder["state"]["config"][role])
        else:
            config[role] = dict(config[role], controller=target)
            pending_switches[role] = entry
        local_ids.pop(role, None)
        trace.event("controller.switch.commit", role=role, phase=phase,
                    source=source, reason=reason, from_controller=current,
                    to_controller=target)
        if session_uuid:
            state_store.invalidate_manifest_for(session_uuid, role)
        io_out.write("cowork: switched %s controller %s -> %s\n"
                     % (role, current, target))
        io_out.flush()
        return True

    def ensure_controller_dispatchable(role, reason="launch"):
        """The pre-launch gate for one role: is its configured controller both
        ALLOWED by this session's policy and actually installed?

        The policy check runs first and is NOT retryable — a policy block is not
        an environment problem, so retrying or prompting would be theatre. It
        prints the block, traces it, and returns False in headless AND
        interactive mode alike. The missing-executable retry/switch/end loop
        below it is unchanged."""
        controller = config[role].get("controller")
        # No manifest exists yet at this point (it is compiled below, only
        # once policy allows this controller), so nothing is bound here —
        # binding a not-yet-compiled manifest would be inventing an
        # identifier.
        _elf = _guard_to_policy_fact(controller, role, phase=phase, trace=trace)
        _eldec = _decide_and_trace(
            trace, role, controller, "launch", "run_flow_pre_launch",
            policy_result=_elf, phase=phase)
        if _eldec["outcome"] == "refuse":
            io_out.write(_eldec["refusal_message"] + "\n")
            io_out.flush()
            trace.event("controller.failure", role=role, phase=phase,
                        controller=controller, reason="policy_blocked",
                        artifact_progress=False)
            return False
        if session_uuid:
            _disp_cfg = config.get(role) or {}
            _disp_sdir = state_store.session_assets_dir(session_uuid)
            try:
                _disp_artifacts = switch_artifacts_for(role)
                _disp_manifest, _ = _compile_role_manifest(
                    role=role, session_uuid=session_uuid, work_id=role,
                    controller=controller,
                    mode=_disp_cfg.get("mode", "implement"),
                    model=_disp_cfg.get("model"), effort=_disp_cfg.get("effort"),
                    instruction_paths=[ROLE_PROMPT_PATHS.get(role)
                                       or SCOUT_PROMPT_PATH],
                    sessions_dir=_disp_sdir,
                    worktree=active_worktree, worktree_base=active_worktree_root,
                    candidate_snapshot=(
                        _file_snapshot(_disp_artifacts[0])
                        if _disp_artifacts else None),
                    force_recompile=False)
            except Exception:
                _disp_manifest = {}
            _dispdec = _decide_and_trace(
                trace, role, controller, "launch", "run_flow_pre_launch",
                manifest=_disp_manifest,
                preflight_result=_manifest_preflight_fact(_disp_manifest),
                phase=phase)
            if _dispdec["outcome"] == "refuse":
                _emit_dispatch_escalation(trace, role, "manifest_proven",
                                          "recompile and preflight the manifest",
                                          "pre_launch")
                io_out.write(
                    "cowork: manifest not proven for %s — dispatch blocked.\n"
                    % role)
                io_out.flush()
                return False
        while True:
            controller = config[role].get("controller")
            ok, alerts = check_controller_tool(controller)
            if ok:
                return True
            alert = "\n".join(alerts)
            trace.event("controller.failure", role=role, phase=phase,
                        controller=controller, reason="missing_executable",
                        artifact_progress=False)
            if headless:
                # No human to choose retry/switch/end: a missing controller is
                # an environment problem cowork cannot fix, so fail cleanly
                # instead of showing an interactive gate.
                trace.event("headless.auto", role=role,
                            gate="controller_failure", action="end",
                            reason=reason)
                return False
            gate_eligible = gate_eligible_for(controller)
            ui.banner(io_out, _controller_failure_text(
                role, controller, "missing executable", alert,
                eligible=gate_eligible), "dissent")
            gate_discard, gate_drain_fail = _gate_trace_callbacks(trace, role)
            action = _read_controller_failure_gate(
                io_in, io_out, eligible=gate_eligible,
                on_discard=gate_discard, on_drain_fail=gate_drain_fail)
            if action is _CTRL_RETRY:
                trace.event("user.action", role=role,
                            action="controller_failure_retry",
                            reason=reason)
                continue
            if action is _CTRL_SWITCH or isinstance(action, _SwitchTo):
                trace.event("user.action", role=role,
                            action="controller_failure_switch",
                            reason=reason)
                if switch_controller(role, reason="missing_executable",
                                     source="gate",
                                     target=_switch_target_of(action)):
                    return True
                continue
            trace.event("user.action", role=role,
                        action="controller_failure_end", reason=reason)
            return False

    # Kept as the historical name for in-tree callers; the policy check is now
    # part of the same pre-launch decision.
    ensure_controller_available = ensure_controller_dispatchable

    def recover_controller_failure(role, reason, alert=None):
        while True:
            controller = config[role].get("controller")
            trace.event("controller.failure", role=role, phase=phase,
                        controller=controller, reason=reason,
                        artifact_progress=False)
            if headless:
                # No human to choose retry/switch/end: end cleanly instead of
                # showing an interactive recovery gate.
                trace.event("headless.auto", role=role,
                            gate="controller_failure", action="end",
                            reason=reason)
                return "end"
            gate_eligible = gate_eligible_for(controller)
            ui.banner(io_out, _controller_failure_text(
                role, controller, reason, alert,
                eligible=gate_eligible), "dissent")
            gate_discard, gate_drain_fail = _gate_trace_callbacks(trace, role)
            action = _read_controller_failure_gate(
                io_in, io_out, eligible=gate_eligible,
                on_discard=gate_discard, on_drain_fail=gate_drain_fail)
            if action is _CTRL_RETRY:
                trace.event("user.action", role=role,
                            action="controller_failure_retry",
                            reason=reason)
                return "retry"
            if action is _CTRL_SWITCH or isinstance(action, _SwitchTo):
                trace.event("user.action", role=role,
                            action="controller_failure_switch",
                            reason=reason)
                if switch_controller(role, reason=reason, source="gate",
                                     target=_switch_target_of(action)):
                    return "switch"
                continue
            trace.event("user.action", role=role,
                        action="controller_failure_end", reason=reason)
            return "end"

    # ---------------------------------------------------------------------- #
    # Session controller policy: ONE validated, all-or-nothing transition,    #
    # then activation, and only then does anything resume.                    #
    #                                                                          #
    # Ordering (result.design.ordering): resolve the proposal's three-way      #
    # policy state -> validate every mapping and current-phase conformance      #
    # against the EFFECTIVE allowed set -> activate that set -> preflight and   #
    # probe every target inside it -> ONE state write -> keep it active ->      #
    # resume. Any validation failure: rc 2, one message, no write, no dispatch. #
    # ---------------------------------------------------------------------- #

    def _durable_transition_policy():
        """C's durable `controller_transition.json` policy field, or None
        when nothing has ever been explicitly committed through the atomic
        transition primitive (a PRESERVE-shaped or absent durable record) --
        see `cowork_policy.decide_controller_policy_transition`."""
        if not session_uuid:
            return None
        transition = state_store.read_controller_transition(session_uuid)
        if not transition.get("revision", 0):
            return None
        return transition.get("policy")

    def _saved_policy():
        """The tagged `(kind, raw)` policy read, resolved across BOTH stores
        this session may carry: the legacy session-embedded `controller_
        policy` key, and C's durable CAS'd `controller_transition.json`.
        `read_controller_policy` alone is legacy-only and blind to a policy
        committed only through the atomic transition primitive; consulting
        only the durable store would be blind to a session that has never
        gone through it. When both carry an explicit policy and they
        DISAGREE, this fails CLOSED (`invalid`) rather than picking one --
        two sources of truth that should never diverge in correctly-wired
        code are treated as untrustworthy the moment they do, exactly like a
        present-but-unreadable legacy policy already is."""
        if not session_enabled:
            return ("unrestricted", None)
        legacy_kind, legacy_raw = state_store.read_controller_policy(
            holder["state"])
        if legacy_kind == "invalid":
            return (legacy_kind, legacy_raw)
        durable_policy = _durable_transition_policy()
        if durable_policy is None:
            return (legacy_kind, legacy_raw)
        durable_allowed = (durable_policy.get("allowed")
                           if isinstance(durable_policy, dict) else None)
        durable_kind = "unrestricted" if durable_allowed is None else "allowed"
        agrees = (durable_kind == legacy_kind and (
            durable_kind == "unrestricted"
            or sorted(durable_allowed) == sorted(legacy_raw or ())))
        if agrees:
            return (legacy_kind, legacy_raw)
        return ("invalid", {"legacy_kind": legacy_kind,
                            "durable_kind": durable_kind,
                            "reason": "two_store_policy_disagreement"})

    def _activate_policy(kind, raw):
        """The ONE place in this file that calls `policy.activate`/
        `policy.activate_invalid` directly. `kind` is `"unrestricted"` /
        `"allowed"` / `"invalid"` (the same vocabulary `_saved_policy` and
        `policy.active_meta()['mode']` both use); every other seam in this
        file that needs to put a policy in force calls THIS function, never
        the raw primitives, so activation is always derived from one
        consistent decision (the structural zero-bypass invariant)."""
        if kind == "invalid":
            policy.activate_invalid(raw, trace=trace, phase=phase)
        else:
            policy.activate(raw if kind == "allowed" else None,
                            trace=trace, phase=phase)

    def apply_controller_update(proposal):
        """Run one controller update end to end. Returns `(ok, message, rc)`;
        on success `message` is None. Nothing is written and nothing is started
        unless the whole proposal validates."""
        kind, raw = _saved_policy()
        saved_allowed = raw if kind == "allowed" else None
        effective, err, warnings = validate_controller_proposal(
            proposal, saved_allowed, phase, selected, holder["state"])
        action_name = ("preserve" if proposal.policy is policy.PRESERVE
                       else "remove" if proposal.policy is policy.ALL
                       else "set")
        mapping_list = ["%s=%s" % (r, c) for r, c in (proposal.mappings or [])]
        if err:
            if proposal.policy is policy.PRESERVE:
                # The lone --switch-controller mapping-only path is the CLI's
                # policy-preserving repair (cowork_state.apply_controller_transition
                # calls this exact shape "a single-write, POLICY-PRESERVING
                # transition"). Invoke the named predicate on THIS real path —
                # not in isolation — and escalate every mapping it would have
                # widened beyond the session's currently allowed set.
                for role, target in (proposal.mappings or []):
                    if not _is_policy_preserving_repair(saved_allowed, (target,)):
                        _emit_dispatch_escalation(
                            trace, role, "policy_preserving_repair",
                            "choose a controller within the session's allowed "
                            "set, or pass --allow-controllers to widen it",
                            "controller_change")
            trace.event("controller.policy.rejected", reason=err,
                        source=proposal.source, persisted=False,
                        policy_action=action_name,
                        effective_allowed=list(effective or ()),
                        mappings=mapping_list)
            return (False, err, 2)

        mappings = list(proposal.mappings or [])
        from_controllers = {r: (config.get(r) or {}).get("controller")
                            for r, _c in mappings}
        for role, target in mappings:
            trace.event("controller.switch.request", role=role, phase=phase,
                        source=proposal.source, reason=proposal.source,
                        from_controller=from_controllers[role],
                        to_controller=target)
            if target == from_controllers[role]:
                trace.event("controller.switch.end", role=role, phase=phase,
                            result="already_current", controller=target)
                return (False, "%s is already using %s." % (role, target), 1)

        prior_meta = policy.active_meta()

        def reject(message, rc_code):
            _restore_policy(prior_meta)
            trace.event("controller.policy.rejected", reason=message,
                        source=proposal.source, persisted=False,
                        policy_action=action_name,
                        effective_allowed=list(effective or ()),
                        mappings=mapping_list)
            return (False, message, rc_code)

        # The effective set is in force for the ENTIRE pre-write window, so
        # preflight and the claude probe are themselves guarded and can only
        # ever touch a controller that will be permitted once this completes.
        _activate_policy("allowed" if effective is not None else "unrestricted",
                         effective)
        for target in dict.fromkeys(t for _r, t in mappings):
            ok, alerts = check_controller_tool(target)
            if not ok:
                first_role = next(r for r, t in mappings if t == target)
                trace.event("controller.switch.preflight_failed",
                            role=first_role, phase=phase,
                            target_controller=target, alerts_count=len(alerts))
                return reject("cannot switch %s to %s yet:\n  - %s"
                              % (first_role, target, "\n  - ".join(alerts)), 1)
        for role, target in mappings:
            if target != "claude":
                continue
            cfg = dict(config.get(role) or {})
            ok, alert = _with_status_spinner(
                io_out, "checking claude for %s" % role,
                lambda c=cfg, r=role: bridge.probe_claude_stream_json(
                    bridge._real_claude_spawn,
                    mode=c.get("mode", "implement"),
                    yolo=c.get("yolo", True),
                    role_prompt_file=ROLE_PROMPT_PATHS.get(r),
                    trace=trace, role=r,
                    extra_writable_dir=state_store.session_assets_dir(
                        session_uuid),
                    cache_enabled=True))
            if not ok:
                trace.event("controller.switch.probe_failed", role=role,
                            phase=phase, target_controller=target)
                return reject("cannot switch %s to claude: %s" % (role, alert),
                              1)

        # -- the single state write, atomic via C's CAS primitive ---------- #
        # `decide_controller_policy_transition` is proposed and must COMMIT
        # before anything durable or in-memory changes: a conflicting (stale
        # revision) or invalid transition is rejected here with zero writes
        # and zero dispatch — `reject()` below restores the pre-attempt
        # active policy and returns without ever reaching the legacy state
        # write or a role launch.
        from_allowed = list(saved_allowed) if kind == "allowed" else None
        stamp = time.time()
        if session_enabled:
            pending = {r: pending_switch_turns.get(r) for r, _c in mappings
                       if pending_switch_turns.get(r) is not None}
            if session_uuid:
                expected_revision = state_store.read_controller_transition(
                    session_uuid).get("revision", 0)
                cas_policy = (
                    policy.ALL if proposal.policy is policy.ALL
                    else policy.PRESERVE if proposal.policy is policy.PRESERVE
                    else {"allowed": list(effective)})
                transition_result = policy.decide_controller_policy_transition(
                    session_uuid, expected_revision, policy=cas_policy,
                    reason=proposal.source, source=proposal.source)
                if transition_result.get("outcome") != "committed":
                    return reject(
                        "controller policy transition conflicted (%s); "
                        "nothing was switched." % transition_result.get("reason"),
                        1)
            # MJ-3: the CAS transition above may already have committed
            # (durable, revision bumped) by the time THIS write is attempted
            # -- an interruption here must still leave the trace narrative
            # coherent (a clean rejection, not a bare crash mid-request) and
            # the in-memory policy restored to its pre-attempt value, exactly
            # like every other rejection this function already routes
            # through `reject()`. Catching only OSError: this is a durability
            # failure of the write itself (see `cowork_state.save`'s
            # tmp-write + os.replace), never a validation error, which has
            # already run to completion above.
            try:
                holder["state"] = state_store.apply_controller_transition(
                    spath, mappings,
                    allowed=(None if proposal.policy is policy.ALL else effective),
                    set_policy=(proposal.policy is not policy.PRESERVE),
                    prior=holder["state"], source=proposal.source,
                    reason=proposal.source, created=stamp,
                    pending_turns=pending or None)
            except OSError as exc:
                return reject(
                    "controller state write failed (%s); nothing was "
                    "switched." % type(exc).__name__, 1)
            for role, _target in mappings:
                config[role] = dict(holder["state"]["config"][role])
        else:
            for role, target in mappings:
                config[role] = dict(config[role], controller=target)
                pending_switches[role] = {
                    "from_controller": from_controllers[role],
                    "to_controller": target, "reason": proposal.source,
                    "source": proposal.source, "created": stamp,
                }
        for role, _target in mappings:
            local_ids.pop(role, None)
        if session_uuid:
            for role, _target in mappings:
                state_store.invalidate_manifest_for(session_uuid, role)

        # -- only now is anything reported, and only then does work resume -- #
        trace.event("controller.policy.change", policy_action=action_name,
                    from_allowed=from_allowed,
                    to_allowed=list(effective) if effective else None,
                    source=proposal.source, mappings=mapping_list, phase=phase)
        for role, target in mappings:
            trace.event("controller.switch.commit", role=role, phase=phase,
                        source=proposal.source, reason=proposal.source,
                        from_controller=from_controllers[role],
                        to_controller=target)
            io_out.write("cowork: switched %s controller %s -> %s\n"
                         % (role, from_controllers[role], target))
        if proposal.policy is policy.ALL:
            io_out.write("cowork: this session may now use any controller.\n")
        elif proposal.policy is not policy.PRESERVE:
            io_out.write("cowork: this session is now restricted to %s.\n"
                         % policy.format_allowed(effective))
        for warning in warnings:
            io_out.write("cowork: " + warning + "\n")
        io_out.flush()
        return (True, None, 0)

    def _restore_policy(meta):
        """Put back whatever was active before a rejected proposal's probe
        window, so a rejection leaves not a trace of itself in force."""
        mode = meta.get("mode")
        _activate_policy(mode, meta.get("raw") if mode == "invalid"
                         else meta.get("allowed"))

    saved_policy_kind, saved_policy_raw = _saved_policy()

    proposal = None
    if args.switch_controller or args.allow_controllers is not None:
        proposal = policy.ControllerProposal(
            policy.PRESERVE if args.allow_controllers is None
            else args.allow_controllers,
            tuple(args.switch_controller or ()),
            "cli")
    elif choice.action == "edit_controllers":
        proposal = prompt_controller_update(
            saved_policy_kind, saved_policy_raw, config, selected, phase,
            io_out)
        if proposal is None:
            trace.event("run.end", rc=0, reason="controller_update_cancelled")
            io_out.write("cowork: cancelled; nothing to do.\n")
            return 0

    # A present-but-invalid policy fails CLOSED. Only a proposal that REPLACES
    # it (ALL or SET, from either surface) may repair it — PRESERVE has nothing
    # to replace it with, so a lone --switch-controller takes the same abort.
    if saved_policy_kind == "invalid" and (
            proposal is None or proposal.policy is policy.PRESERVE):
        _activate_policy("invalid", saved_policy_raw)
        trace.event("controller.policy.invalid", session_file=spath,
                    raw_policy_type=type(saved_policy_raw).__name__,
                    reason="controller_policy is not a readable allowed set",
                    repairable=True)
        io_out.write(controller_policy_invalid_text(spath, ui.is_tty(io_out)))
        io_out.flush()
        trace.event("run.end", rc=2, reason="controller_policy_invalid")
        return 2

    if proposal is not None:
        ok, message, rc_reject = apply_controller_update(proposal)
        if not ok:
            io_out.write("cowork: " + message + "\n")
            io_out.flush()
            trace.event("run.end", rc=rc_reject,
                        reason=("switch_controller_failed" if rc_reject == 1
                                else "controller_policy_rejected"))
            return rc_reject
        # Re-activate from the freshly saved state so the in-force policy and
        # the persisted one can never disagree after a write.
        kind_now, raw_now = _saved_policy()
        _activate_policy(kind_now, raw_now)
    else:
        # Ordinary resume of a saved session: the policy is activated here,
        # BEFORE any role dispatch or worktree launch, so a restricted resume is
        # guarded exactly like an update.
        _activate_policy(saved_policy_kind, saved_policy_raw)

    # Resolved BEFORE the context step so we can skip the goal prompt on a
    # resume of the current phase's user-facing role.
    lead_role = PHASE_LEADS[phase]
    lead_resume_id = role_resume_id(lead_role)
    lead_switch_pending = bool(
        session_enabled
        and state_store.read_pending_switch(holder["state"], lead_role))
    if lead_resume_id:
        trace.event("run.resume", role=lead_role,
                    controller=config[lead_role]["controller"],
                    session_id=lead_resume_id, phase=phase)

    # Step 3: context. On a resume, skip the goal prompt and auto-continue.
    context = resolve_context(
        args, resuming=(bool(lead_resume_id)
                        or bool(args.switch_controller)
                        or lead_switch_pending))

    # Context invariant: explicit context is a session-wide event. Persist it as
    # the CURRENT session context (bumping the revision when it changed), and
    # make sure every role invoked from here on receives the current revision —
    # fresh sessions get it in their prompt; resumed sessions that have not
    # acknowledged it get an explicit context-update wake block.
    current_rev = 0
    current_text = context
    if session_enabled:
        if context.strip():
            holder["state"] = state_store.save_context(
                spath, context, prior=holder.get("state"))
            trace.event("context.saved", source="input",
                        context_revision=state_store.get_context_revision(
                            holder["state"]))
            if session_uuid:
                for _inv_role in list(config):
                    state_store.invalidate_manifest_for(session_uuid, _inv_role)
        state = holder["state"]
        current_text = state_store.get_context(state) or ""
        current_rev = state_store.get_context_revision(state)
        trace.event("context.current", revision=current_rev,
                    context_revision=current_rev,
                    has_context=bool(current_text),
                    context_sha256=(state.get("context") or {}).get("hash")
                    if isinstance(state.get("context"), dict) else None)

    shared_context = (current_text or context) if session_enabled else context

    def with_headless_lead(seed):
        """Prepend the runtime headless note to a LEAD seed when --headless is
        set, so the lead knows on its first turn that no human is available
        (F2_roles_never_block, runtime activation of the prompt layer)."""
        if not headless:
            return seed
        note = HEADLESS_LEAD_NOTE
        if not seed:
            return note
        if isinstance(seed, handoff.HandoffBlock):
            return handoff.compose_handoff_blocks(
                _headless_lead_fragment(), handoff.STATIC_SEPARATOR, seed)
        return (str(note) + "\n\n" + str(seed).strip()).strip()

    # The reviewer context passed to every paired reviewer this run: under
    # --headless it carries the runtime headless reviewer note so the reviewer
    # itself works with what it has (F2_reviewer_needs_user, prompt layer).
    reviewer_ctx = ((HEADLESS_REVIEWER_NOTE + "\n\n" + (shared_context or ""))
                    .strip() if headless else shared_context)

    def deliver_context(role, seed):
        """Prepend the current-context wake block to `seed` when `role` is a
        RESUMED session that has not acknowledged the current revision.

        Applied at EVERY phase invocation — not just the run's initial lead
        role — so a role re-entered mid-run (a hand-back resuming the scout, a
        re-approval resuming the planner) never has the revision marked seen
        without the context actually having been delivered. When the seed is
        empty or is exactly the (just-saved) context text, the block alone is
        sent — never the same text twice."""
        if not session_enabled or not role_resume_id(role):
            return seed
        gap = state_store.role_context_gap(holder["state"], role)
        if not gap:
            return seed
        trace.event("context.gap", role=role, revision=current_rev,
                    context_revision=current_rev,
                    delivered=True, reason="phase_invocation")
        block = context_update_block(gap, intel_dir, current_rev)
        if not seed or (isinstance(seed, str) and str(seed).strip() == gap.strip()):
            return block
        if (isinstance(seed, handoff.HandoffBlock)
                or getattr(seed, "kind", None) == "static_role"):
            return handoff.compose_handoff_blocks(
                block, handoff.STATIC_SEPARATOR, seed)
        raise TypeError(
            "context-update handoff cannot be combined with untyped seed text")

    def reviewer_gap(reviewer_role):
        """The context-update wake block for a RESUMED paired reviewer that has
        not acknowledged the current revision, else None.

        Under --headless the runtime headless reviewer note is prepended (or sent
        alone when there is no other gap) so a RESUMED reviewer — whose first
        pass uses context_update, not reviewer_context — still gets the 'no human
        available' instruction on its first headless turn. A FRESH reviewer
        ignores context_update and gets the note via reviewer_context instead, so
        the note is never doubled."""
        gap = None
        if session_enabled and role_resume_id(reviewer_role):
            gap = state_store.role_context_gap(holder["state"], reviewer_role)
            trace.event("context.gap", role=reviewer_role, revision=current_rev,
                        context_revision=current_rev,
                        delivered=bool(gap), reason="reviewer_resume")
        if headless and role_resume_id(reviewer_role):
            if gap:
                return (HEADLESS_REVIEWER_NOTE + "\n\n" + gap).strip()
            return HEADLESS_REVIEWER_NOTE
        return gap

    def context_acker(role):
        if not session_enabled:
            return None

        def ack():
            holder["state"] = state_store.mark_context_seen(
                spath, role, current_rev, prior=holder["state"])
            trace.event("context.ack", role=role, revision=current_rev,
                        context_revision=current_rev)
        return ack

    def ack_lead(role):
        # The lead role received the current context in its prompt this run;
        # record the acknowledgment after a successful run (a crash leaves it
        # unacknowledged, so the next resume re-delivers the wake block — the
        # safe direction).
        if session_enabled and current_rev:
            holder["state"] = state_store.mark_context_seen(
                spath, role, current_rev, prior=holder["state"])
            trace.event("context.ack", role=role, revision=current_rev,
                        context_revision=current_rev)

    def _measurement_ingest(at):
        """Ingest + reconcile. Best-effort: it may degrade the measurement and
        never the run."""
        try:
            identities = state_store.read_role_identities(
                state_store.identities_path_for(session_uuid))
            results = ingest.ingest_session(identities, cwd=os.getcwd())
            ledger.reconcile_attempts(
                state_store.ledger_path_for(session_uuid),
                ingest.observations_for(results))
            return results
        except Exception:  # noqa: BLE001 - measurement never breaks a run
            trace.event("measurement.checkpoint.error", at=at)
            return None

    def _measurement_rebuild(at, results):
        """Rebuild the record. Best-effort, exactly as before."""
        try:
            measure.build_and_write(session_uuid, cwd=os.getcwd(),
                                    ingest_results=results)
        except Exception:  # noqa: BLE001 - measurement never breaks a run
            trace.event("measurement.checkpoint.error", at=at)

    def evaluation_transition(at, closed_phases=None):
        """This boundary's evaluation drain and its visible foreground state.

        A thin binding of the run's context onto `run_evaluation_transition`,
        which holds the actual behavior so it is reachable without standing up a
        whole run.
        """
        return run_evaluation_transition(
            session_uuid, evaluation_policy, config=config, trace=trace,
            io_in=io_in, io_out=io_out, at=at, closed_phases=closed_phases,
            headless=headless)

    def measurement_checkpoint(at, closed_phases=None):
        """The three measurement steps that run together at every boundary.

        Ordered on purpose: ingest and reconcile FIRST (so the ledger holds the
        minted verification attempts), then drain the evaluation queue, then
        rebuild the record from all of it. Doing it in this order is what makes
        the record current by construction during a live run, so an ordinary
        `--report` loads rather than rebuilds.

        The ingest and rebuild steps stay entirely best-effort: they degrade the
        measurement and never the run. The evaluation transition in the middle
        has its OWN error policy (see `evaluation_transition`) because it can
        block on the user, and a blocking prompt inside a swallow-all handler is
        how a gate fails invisibly.
        """
        results = _measurement_ingest(at)
        evaluation_transition(at, closed_phases=closed_phases)
        _measurement_rebuild(at, results)

    def set_phase(new_phase):
        if session_enabled:
            holder["state"] = state_store.save_phase(
                spath, new_phase, prior=holder["state"])
        trace.event("phase.change", context_revision=current_rev,
                    **{"from": phase, "to": new_phase})
        # The phase being left is now CLOSED, which is what lets a
        # `final_round` candidate be resolved.
        measurement_checkpoint("phase.change:%s->%s" % (phase, new_phase),
                               closed_phases=[phase])
        return new_phase

    # All per-session produced artifacts live under the session-assets home
    # (~/.cowork/sessions/<uuid>/, COWORK_SESSIONS_ROOT-overridable), joining
    # the trace and scores already kept there; only .cowork/session.json stays
    # project-local as the per-directory anchor. Create the home up front so the
    # agent CLIs (which write their own artifacts) always have a target dir.
    intel_dir = state_store.session_assets_dir(session_uuid)
    os.makedirs(intel_dir, exist_ok=True)
    # SESSION START: drain anything a PREVIOUS process left queued (P12). This
    # is what bounds the cost of deferring scoring — a session killed mid-phase
    # leaves its rounds on disk with their original sealed digests, and they are
    # scored here rather than lost.
    measurement_checkpoint("session.start")
    intel_path = scout_intel_path(intel_dir, session_uuid)
    intel_md_path = state_store.scout_intel_md_path_for(intel_dir, session_uuid)
    review_path = state_store.review_path_for(intel_dir, session_uuid)
    plan_json_path = state_store.planner_plan_json_path_for(intel_dir, session_uuid)
    plan_md_path = state_store.planner_plan_md_path_for(intel_dir, session_uuid)
    planner_review_path = state_store.planner_review_path_for(
        intel_dir, session_uuid)
    build_status_path = state_store.build_status_path_for(
        intel_dir, session_uuid)
    build_summary_path = state_store.build_summary_path_for(
        intel_dir, session_uuid)
    build_review_path = state_store.build_review_path_for(
        intel_dir, session_uuid)

    # --worktree pre-phase (D2/D3/D4/D6/D13): create (or reuse) a git worktree
    # and redirect the session into it BEFORE scouting. The cowork session store
    # (.cowork/session.<uuid>.json) and per-session assets stay at the LAUNCH
    # location: spath is absolutized here so later save_* calls keep writing
    # there after the os.chdir, and the assets dir is home-dir keyed by uuid
    # (unaffected by cwd). The worktree role has NO reviewer and NO gate.
    if worktree_requested:
        spath = os.path.abspath(spath)
        worktree_status_path = state_store.worktree_status_path_for(
            intel_dir, session_uuid)
        explicit_name = (args.worktree if isinstance(args.worktree, str)
                         else None)
        # D6: reuse a recorded worktree — but ONLY when it still passes the same
        # deterministic D13 validation (git-registered path on the recorded
        # branch), so a stale/unregistered/wrong-branch recorded path can never
        # redirect the session into a bad tree. A recorded path that no longer
        # validates falls through to re-creation (idempotent resume), never a
        # blind chdir.
        recorded = (state_store.get_worktree(holder["state"])
                    if session_enabled else None)
        wt_path = wt_branch = None
        if recorded:
            rok, rpath, rbranch, rerr = validate_worktree(
                worktree_base,
                {"status": "ready",
                 "result": {"worktree_path": recorded.get("path"),
                            "branch": recorded.get("branch")}})
            if rok:
                wt_path, wt_branch = rpath, rbranch
                trace.event("worktree.reuse", path=wt_path, branch=wt_branch)
            else:
                trace.event("worktree.reuse_rejected",
                            path=recorded.get("path"),
                            branch=recorded.get("branch"), detail=rerr)
        if wt_path is None:
            wt_name = explicit_name or default_worktree_name(session_uuid)
            wt_controller = getattr(args, "wt_controller", "claude")
            # --wt-controller is checked against the policy BEFORE the worktree
            # agent launches, so a disallowed worktree controller is a clean
            # pre-launch block rather than a mid-launch exception.
            try:
                policy.guard(wt_controller, role=WORKTREE_ROLE,
                             kind="dispatch", phase=phase, trace=trace)
            except policy.DispatchBlocked as exc:
                trace.event("run.end", rc=2, reason="worktree_policy_blocked")
                io_out.write(str(exc) + "\n")
                io_out.flush()
                return 2
            wt_cfg = {"controller": wt_controller,
                      "model": None, "effort": None,
                      "yolo": True, "mode": "implement"}
            artifact = run_worktree_fn(
                wt_cfg, worktree_status_path, worktree_base, wt_name,
                bool(explicit_name), io_in=io_in, io_out=io_out,
                session_uuid=session_uuid, trace=trace,
                extra_writable_dir=intel_dir)
            ok, wt_path, wt_branch, err = validate_worktree(
                worktree_base, artifact)
            if not ok:
                # Fail-fast (D13): no chdir, no scouting — the session never
                # half-redirects into a bad/nonexistent tree.
                trace.event("run.end", rc=2, reason="worktree_failed",
                            detail=err)
                io_out.write("cowork: worktree creation failed: %s\n" % err)
                io_out.flush()
                return 2
            if session_enabled:
                holder["state"] = state_store.set_worktree(
                    spath, wt_path, wt_branch, prior=holder["state"])
                if session_uuid:
                    state_store.invalidate_manifest_for(session_uuid,
                                                        WORKTREE_ROLE)
            trace.event("worktree.created", path=wt_path, branch=wt_branch)
        # Redirect the rest of the session into the worktree: every spawned CLI
        # uses cwd=os.getcwd() (cowork_bridge), and run_cwd drives discovery and
        # the build baseline.
        os.chdir(wt_path)
        run_cwd = wt_path
        active_worktree = wt_path
        # wt_path is already validate_worktree()'s realpath'd, git-registered
        # result (fresh-create or reuse alike) — its own dirname is a real,
        # already-existing directory that strictly contains it by
        # construction, whichever worktree convention the repo used.
        active_worktree_root = os.path.dirname(wt_path)
        trace.event("worktree.redirect", cwd=wt_path)
        io_out.write("cowork: running inside worktree %s (branch %s)\n"
                     % (wt_path, wt_branch))
        io_out.flush()

    def save_pending_turn_for(role, pending_text, source=None):
        if session_enabled:
            holder["state"] = state_store.save_pending_turn(
                spath, role, pending_text, prior=holder["state"],
                source=source)

    def pending_switch_for(role):
        if session_enabled:
            return state_store.read_pending_switch(holder["state"], role)
        entry = pending_switches.get(role)
        return dict(entry) if entry else None

    def clear_pending_switch_for(role):
        pending_switch_turns.pop(role, None)
        if session_enabled:
            holder["state"] = state_store.clear_pending_switch(
                spath, role, prior=holder["state"])
        else:
            pending_switches.pop(role, None)

    def switch_artifacts_for(role):
        if role == "scout":
            return [intel_path, intel_md_path, review_path]
        if role == SCOUT_REVIEWER:
            return [intel_path, intel_md_path, review_path]
        if role == "planner":
            return [intel_path, intel_md_path, plan_json_path, plan_md_path,
                    planner_review_path]
        if role == PLANNING_ADVISOR:
            return [intel_path, intel_md_path, plan_json_path, plan_md_path,
                    planner_review_path]
        if role == "builder":
            return [plan_json_path, plan_md_path, build_status_path,
                    build_summary_path, build_review_path]
        if role == BUILD_REVIEWER:
            return [plan_json_path, plan_md_path, build_status_path,
                    build_summary_path, build_review_path]
        return []

    def switch_note_for(role):
        ps = pending_switch_for(role)
        pt = pending_switch_turns.get(role)
        if pt is None and ps and isinstance(ps, dict):
            pt = ps.get("pending_turn")
        if not ps and not pt:
            return ""
        from_c = ps.get("from_controller") if isinstance(ps, dict) else None
        to_c = ps.get("to_controller") if isinstance(ps, dict) else None
        if from_c and to_c and from_c != to_c:
            return switch_handoff_packet(
                role, phase, ps,
                artifact_paths=switch_artifacts_for(role),
                shared_context=shared_context,
                pending_turn=pt,
                assets_dir=intel_dir,
                context_revision=current_rev)
        return pending_resume_packet(
            role, phase, ps,
            artifact_paths=switch_artifacts_for(role),
            shared_context=shared_context,
            pending_turn=pt,
            assets_dir=intel_dir,
            context_revision=current_rev)

    def seed_with_switch_note(role, seed):
        note = switch_note_for(role)
        if not note:
            return seed
        if not seed:
            return note
        if isinstance(note, handoff.HandoffBlock):
            if (isinstance(seed, handoff.HandoffBlock)
                    or getattr(seed, "kind", None) == "static_role"):
                return handoff.compose_handoff_blocks(
                    note, handoff.STATIC_SEPARATOR, seed)
            raise TypeError(
                "controller-switch handoff cannot be combined with untyped "
                "seed text")
        raise TypeError("controller-switch note lacks handoff provenance")

    def prepare_fresh_seed_after_switch(role):
        if role == "scout":
            # The switch packet already carries the shared-context FILE path.
            # Re-inlining shared_context here would both duplicate it and erase
            # the handoff provenance required by the send boundary.
            return with_discovery("")
        if role == "planner":
            return assemble_planner_seed(intel_path, shared_context, intel_dir, current_rev)
        if role == "builder":
            return assemble_builder_seed(
                plan_json_path, plan_md_path, shared_context,
                intel_dir, current_rev)
        return shared_context

    # Peer-evaluation assets: a per-role scratch file (each evaluator's only
    # eval write target) and the orchestrator-only aggregate scores file.
    eval_scratch = {
        role: state_store.eval_scratch_path_for(intel_dir, role, session_uuid)
        for role in ("scout", SCOUT_REVIEWER, "planner", PLANNING_ADVISOR,
                     "builder", BUILD_REVIEWER)
    }
    scores_path = state_store.scores_path_for(session_uuid)
    # Planning-phase epoch: bumped on every scouting -> planning transition so
    # the once-per-phase ->scout evals re-run after a hand-back round trip,
    # even when the re-approved intel is byte-identical. Resuming into the
    # planning phase keeps the persisted epoch.
    epoch_box = {"epoch": state_store.get_planning_epoch(holder["state"])
                 if session_enabled else 0}
    # Building-phase epoch: the analogue for the building phase (every
    # plan-approved -> building transition bumps it, so the once-per-phase
    # ->planner consumed-plan evals re-run after a builder -> planner hand-back
    # round trip even when the re-approved plan is byte-identical).
    building_epoch_box = {"epoch": state_store.get_building_epoch(
        holder["state"]) if session_enabled else 0}

    # M2 Package E (BL-3): a per-epoch attempt counter, folded into
    # `_role_work_id` alongside the epoch, so a same-epoch relaunch (a
    # launch-time retry, a launch-time controller switch, or a mid-turn
    # controller switch) mints a FRESH WorkUnit instead of reusing one whose
    # PhaseState history may already be terminal (`rejected_preflight`,
    # `needs_authority`) or `running` from the PRIOR controller -- neither of
    # which has a legal `("...", "preflight_started")` reducer edge back to
    # `preflighting`, so every subsequent PhaseState call on a reused
    # identity would silently no-op (`illegal_transition`), permanently
    # losing observability into the retried/switched attempt. Reset to 0
    # exactly when the epoch itself bumps (a hand-back is a genuinely fresh
    # engagement, not a same-epoch retry).
    scout_attempt_box = {"attempt": 0}
    planner_attempt_box = {"attempt": 0}
    builder_attempt_box = {"attempt": 0}

    def bump_planning_epoch():
        if session_enabled:
            holder["state"] = state_store.bump_planning_epoch(
                spath, prior=holder["state"])
            epoch_box["epoch"] = state_store.get_planning_epoch(
                holder["state"])
        else:
            epoch_box["epoch"] += 1
        planner_attempt_box["attempt"] = 0

    def bump_building_epoch():
        if session_enabled:
            holder["state"] = state_store.bump_building_epoch(
                spath, prior=holder["state"])
            building_epoch_box["epoch"] = state_store.get_building_epoch(
                holder["state"])
        else:
            building_epoch_box["epoch"] += 1
        builder_attempt_box["attempt"] = 0

    # Scouting-phase epoch: the scout-side analogue of planning_epoch. Bumped on
    # every planning -> scouting transition (a user-confirmed planner -> scout
    # hand-back), so the scout reviewer hash-gate baseline from the prior
    # scouting pass is invalidated by a re-entry (D12). The initial scouting
    # pass runs at the persisted epoch (0 for a fresh session).
    scouting_epoch_box = {"epoch": state_store.get_scouting_epoch(
        holder["state"]) if session_enabled else 0}

    def bump_scouting_epoch():
        if session_enabled:
            holder["state"] = state_store.bump_scouting_epoch(
                spath, prior=holder["state"])
            scouting_epoch_box["epoch"] = state_store.get_scouting_epoch(
                holder["state"])
        else:
            scouting_epoch_box["epoch"] += 1
        scout_attempt_box["attempt"] = 0

    # M2 Package E (BL-3-RESIDUAL): now that every phase epoch box holds its
    # persisted value, resolve each role's actual starting attempt from
    # durable PhaseState rather than trusting the box's `0` initializer --
    # see `_resolve_attempt_start`. A fresh session (or an epoch this
    # process is the first to touch) resolves back to 0 on its very first
    # read, so this changes nothing for the overwhelmingly common case; it
    # only matters for a process resuming an epoch a PRIOR process already
    # drove one or more attempts into a terminal/needs_authority state.
    if session_enabled:
        scout_attempt_box["attempt"] = _resolve_attempt_start(
            session_uuid, "scout", scouting_epoch_box["epoch"])
        planner_attempt_box["attempt"] = _resolve_attempt_start(
            session_uuid, "planner", epoch_box["epoch"])
        builder_attempt_box["attempt"] = _resolve_attempt_start(
            session_uuid, "builder", building_epoch_box["epoch"])

    # Reviewer hash-gate (scout + planner only). Each bundle's three callables
    # close over the active session-state holder + the phase epoch box + the
    # paired reviewer role + the current context revision, so a skip reuses the
    # LAST APPROVED artifact set only within the same epoch and acked context.
    # record() updates holder['state'] IN PLACE (mirroring context_acker) so the
    # baseline survives the next lead-ack / phase-save that threads holder.
    # Disabled (None) when persistence is off — a baseline has nowhere to live.
    def make_skip_baseline(reviewer_role, covered_paths, epoch_box_ref):
        if not (session_enabled and reviewer_role in selected):
            return None

        def compute_composite():
            return state_store.composite_artifact_hash(covered_paths)

        def eligible(composite):
            return state_store.review_skip_eligible(
                holder["state"], reviewer_role, epoch_box_ref["epoch"],
                current_rev, composite)

        def record(composite):
            holder["state"] = state_store.record_review_baseline(
                spath, reviewer_role, epoch_box_ref["epoch"], current_rev,
                composite, prior=holder["state"])
            trace.event("review.baseline.recorded", role=reviewer_role,
                        epoch=epoch_box_ref["epoch"], context_revision=current_rev)

        return SkipBaseline(compute_composite, eligible, record)

    scout_skip_baseline = make_skip_baseline(
        SCOUT_REVIEWER, [intel_path, intel_md_path], scouting_epoch_box)
    planner_skip_baseline = make_skip_baseline(
        PLANNING_ADVISOR, [plan_json_path, plan_md_path], epoch_box)

    # Build baseline: the build-reviewer reviews the builder's full working-tree
    # delta, which it captures itself (status --porcelain + git diff HEAD +
    # untracked). Recorded once, the first time building is entered this run, so
    # the reviewer knows which commit the delta is measured from; a dirty start
    # is surfaced to the user (pre-existing changes get conflated otherwise).
    baseline_box = {"computed": False, "note": None, "repos": None}

    def build_baseline():
        # Per-repo baseline over the user-confirmed repo set (plan JSON
        # result.repos, falling back to discovery from run_cwd — never the
        # session-file/intel dir). Each selected root gets its own (HEAD, dirty)
        # snapshot; the explicit root list (with a has_head flag) is threaded to
        # the reviewer so a no-commit/fallback root is still named and captured.
        if not baseline_box["computed"]:
            repo_paths = _plan_repo_set(plan_json_path, run_cwd)
            entries = []
            repos = []
            dirty_repos = []

            def gather():
                # Per-repo git reads (rev-parse + status --porcelain, 10s
                # timeouts each) run synchronously over the repo set — the slow
                # window. trace.event does not touch io_out, so it is safe under
                # the spinner; the dirty warning (which DOES write io_out) is
                # deferred until after the spinner stops.
                for path in repo_paths:
                    head, dirty = _git_build_baseline(path)
                    entries.append({"path": path, "head": head, "dirty": dirty})
                    repos.append({"path": path, "has_head": head is not None})
                    # The per-file manifest alongside the prose baseline. A
                    # dirty start has no commit describing what the build began
                    # from, so build/review metrics are computed against this
                    # rather than against HEAD.
                    manifest_path = write_build_baseline_manifest(
                        session_uuid, cwd=path)
                    trace.event("build.baseline", repo=path, head=head,
                                dirty=bool(dirty),
                                manifest_written=bool(manifest_path))
                    if head and dirty:
                        dirty_repos.append(path)

            _with_status_spinner(io_out, "reading repo state", gather)
            # Spinner is down — now safe to write the dirty-worktree warning to
            # io_out without a CR-frame interleave.
            for path in dirty_repos:
                io_out.write(
                    "cowork: building from a dirty worktree in %s — "
                    "pre-existing changes will be mixed into the build "
                    "review. Commit or stash unrelated work for a clean "
                    "review.\n" % path)
                io_out.flush()
            baseline_box["note"] = build_baselines_note(entries)
            baseline_box["repos"] = repos
            baseline_box["computed"] = True
        return baseline_box

    # Phase loop: scouting -> (on intel approval, planner on team) planning ->
    # (on a user-confirmed hand-back) scouting -> ... Plan approval, EOF, or an
    # interrupt ends the run; the persisted phase makes a rerun resume here.
    rc = 0
    # Discover the candidate git roots from the LAUNCH folder (run_cwd, never the
    # session-file/intel dir) once, and prepend the same note to EVERY scout seed
    # — the initial seed AND the planner hand-back re-run — so the scout's
    # discover-and-confirm responsibility survives every cycle.
    repo_candidates = discover_git_roots(run_cwd)
    repo_discovery_note = assemble_repo_discovery_note(repo_candidates, run_cwd)
    repo_discovery_fragment = _repo_discovery_fragment(
        repo_candidates, run_cwd)

    def with_discovery(seed):
        # Prepend the discovery note to EVERY scout seed — fresh, plain resume,
        # and hand-back re-run alike — so the discover-and-confirm responsibility
        # is present on every cycle. The note is a standing reminder, not a new
        # task, so a plain auto-continue resume still carries no new goal (the
        # note alone, never a re-injected user goal). An empty seed collapses to
        # the note alone — no trailing blank lines.
        if not seed:
            return repo_discovery_fragment
        if isinstance(seed, handoff.HandoffBlock):
            return handoff.compose_handoff_blocks(
                repo_discovery_fragment, handoff.STATIC_SEPARATOR, seed)
        return (str(repo_discovery_note) + "\n\n" + str(seed).strip()).strip()

    # A resumed scout receives any unseen context through context->update and
    # otherwise gets only the standing discovery reminder.  Re-injecting the
    # raw saved goal here would duplicate it beside the path-only update block
    # and destroy the typed cross-role provenance.
    scout_seed = with_discovery(
        "" if role_resume_id("scout") else context)
    planner_seed = None
    builder_seed = None
    if phase == "planning":
        # Resuming into the planning phase. A saved planner session continues
        # with the (possibly new) context; a planning phase persisted WITHOUT a
        # planner session id (killed between save_phase and the id save) must
        # start a fresh planner from the approved intel, not from a bare
        # context.
        if role_resume_id("planner"):
            planner_seed = context
        else:
            planner_seed = assemble_planner_seed(intel_path, shared_context, intel_dir, current_rev)
    elif phase == "building":
        # Resuming into the building phase. A saved builder session continues
        # with the (possibly new) context; a building phase persisted WITHOUT a
        # builder session id (killed between save_phase and the id save) must
        # start a fresh builder from the approved plan, not from a bare context.
        if role_resume_id("builder"):
            builder_seed = context
        else:
            builder_seed = assemble_builder_seed(
                plan_json_path, plan_md_path, shared_context,
                intel_dir, current_rev)

    # M2 Package E: durable external-kill terminal truth. `active_work_box`
    # names the WorkUnit engagement currently live -- gate or mid-turn, it
    # does not matter which, since a signal handler interrupts whatever is
    # blocking at the moment it arrives -- so a real SIGTERM durably records
    # `aborted` for THAT engagement via A's reducer + B's persistence
    # contract, distinguishable at read time from a live `running`/
    # `awaiting_gate` record and never `completed` (only an explicit,
    # candidate-bound gate approval ever reaches that state). The handler
    # never fabricates a `role_work_id`: `_role_work_id` is a pure function
    # of (session_uuid, role, epoch), so it recomputes the SAME identity
    # `run_scout`/`run_planner`/`run_builder` mint internally, rather than
    # threading a second, competing identity back out of them.
    active_work_box = {"session_uuid": session_uuid, "work_id": None}
    _prior_sigterm_handler = None

    def _handle_external_kill(signum, frame):
        _advance_phase(
            active_work_box["session_uuid"], active_work_box["work_id"],
            "aborted", evidence={"reason": "sigterm"}, source="signal",
            unlocked=True)
        if trace:
            trace.event("run.external_kill", role_work_id=active_work_box["work_id"])
        raise SystemExit(128 + signum)

    try:
        _prior_sigterm_handler = signal.signal(signal.SIGTERM,
                                               _handle_external_kill)
    except (ValueError, RuntimeError):
        # Not the main thread (or platform without SIGTERM): the durable
        # external-kill record is unavailable in this runtime context, but
        # every other seam in this file is unaffected -- never blocks a run.
        _prior_sigterm_handler = None

    try:
        while True:
            active_work_box["work_id"] = (
                _role_work_id(session_uuid, "scout",
                             scouting_epoch_box["epoch"],
                             scout_attempt_box["attempt"])
                if phase == "scouting" and session_uuid else
                _role_work_id(session_uuid, "planner", epoch_box["epoch"],
                             planner_attempt_box["attempt"])
                if phase == "planning" and session_uuid else
                _role_work_id(session_uuid, "builder",
                             building_epoch_box["epoch"],
                             builder_attempt_box["attempt"])
                if phase == "building" and session_uuid else None)
            if phase == "scouting":
                if "scout" not in selected:
                    # Only reachable through a hand-back on a team that resumed into
                    # planning without the scout. The fresh-team case was refused
                    # above.
                    io_out.write(
                        "cowork: cannot run the scouting phase — scout is not on "
                        "the team.\n")
                    rc = 2
                    break
                if not ensure_controller_available("scout", reason="lead_launch"):
                    rc = 1
                    break
                outcome_box = {"outcome": None, "payload": None}
                (scout_first_send_cb, scout_first_send_rejected_cb,
                 scout_first_send_box) = _first_send_delivery_tracker(
                    _make_pending_replay_cb(
                        "scout", pending_switch_for("scout"), phase,
                        session_uuid, trace, clear_pending_switch_for)
                    if pending_switch_for("scout") else None)
                rc = run_scout_fn(
                    config,
                    with_headless_lead(seed_with_switch_note(
                        "scout", deliver_context("scout", scout_seed))),
                    selected,
                    io_in=io_in, io_out=io_out,
                    evaluation_policy=evaluation_policy,
                    resume_id=role_resume_id("scout"),
                    on_session=role_saver("scout"),
                    intel_path=intel_path, review_path=review_path,
                    reviewer_resume_id=role_resume_id(SCOUT_REVIEWER),
                    on_reviewer_session=role_saver(SCOUT_REVIEWER),
                    reviewer_context=reviewer_ctx,
                    reviewer_context_update=reviewer_gap(SCOUT_REVIEWER)
                    if SCOUT_REVIEWER in selected else None,
                    on_reviewer_context_ack=context_acker(SCOUT_REVIEWER),
                    trace=trace,
                    eval_scratch_path=eval_scratch["scout"],
                    reviewer_eval_scratch_path=eval_scratch[SCOUT_REVIEWER],
                    scores_path=scores_path, session_uuid=session_uuid,
                    intel_md_path=intel_md_path,
                    skip_baseline=scout_skip_baseline,
                    review_packet_ctx={"epoch": scouting_epoch_box["epoch"],
                                       "attempt": scout_attempt_box["attempt"],
                                       "context_revision": current_rev},
                    switch_controller_fn=switch_controller,
                    reviewer_switch_note_fn=switch_note_for,
                    on_reviewer_switch_consumed=clear_pending_switch_for,
                    on_first_send_accepted=scout_first_send_cb,
                    on_first_send_rejected=scout_first_send_rejected_cb,
                    reviewer_controller_check_fn=reviewer_controller_check,
                    headless=headless,
                    gate_preview=make_gate_preview(
                        "scout", planner_on_team, session_enabled),
                    save_pending_turn_fn=save_pending_turn_for,
                    clear_pending_turn_fn=clear_pending_switch_for,
                    worktree=active_worktree, worktree_base=active_worktree_root,
                    on_outcome=lambda o, p=None: outcome_box.update(
                        outcome=o, payload=p))
                if rc != 0:
                    action = recover_controller_failure("scout", "startup_or_probe")
                    if action == "retry":
                        # BL-3: a fresh attempt, not a fresh epoch -- the
                        # prior attempt's WorkUnit may already be terminal
                        # (rejected_preflight/needs_authority), so the next
                        # attempt must mint its own identity.
                        scout_attempt_box["attempt"] += 1
                        continue
                    if action == "switch":
                        scout_attempt_box["attempt"] += 1
                        scout_seed = prepare_fresh_seed_after_switch("scout")
                        continue
                    break
                if (rc == 0 and outcome_box["outcome"] == "switch_controller"):
                    payload = outcome_box["payload"] or {}
                    if payload.get("pending"):
                        pending_switch_turns["scout"] = payload.get("pending")
                    if switch_controller("scout", reason=payload.get("reason"),
                                         source="gate",
                                         target=payload.get("target")):
                        # BL-3: this engagement was `running` under the OLD
                        # controller; the NEW controller's launch needs a
                        # fresh WorkUnit (no legal `preflight_started` edge
                        # back to `preflighting` from `running`).
                        scout_attempt_box["attempt"] += 1
                        scout_seed = prepare_fresh_seed_after_switch("scout")
                        continue
                    rc = 1
                    break
                if rc == 0 and scout_first_send_box["delivered"]:
                    ack_lead("scout")
                if (rc == 0 and outcome_box["outcome"] == "approved"
                        and planner_on_team):
                    phase = set_phase("planning")
                    bump_planning_epoch()
                    # A planner session that already exists (hand-back round trip,
                    # or a crash after planning started) digests the updated intel;
                    # a fresh one is seeded with the approved intel + context.
                    if role_resume_id("planner"):
                        planner_seed = intel_updated_block(intel_path)
                    else:
                        planner_seed = assemble_planner_seed(
                            intel_path, shared_context, intel_dir, current_rev)
                    continue
                break

            if phase == "planning":
                if not ensure_controller_available("planner", reason="lead_launch"):
                    rc = 1
                    break
                planner_box = {"outcome": None, "payload": None}
                (planner_first_send_cb, planner_first_send_rejected_cb,
                 planner_first_send_box) = _first_send_delivery_tracker(
                    _make_pending_replay_cb(
                        "planner", pending_switch_for("planner"), phase,
                        session_uuid, trace, clear_pending_switch_for)
                    if pending_switch_for("planner") else None)
                rc = run_planner_fn(
                    config,
                    with_headless_lead(seed_with_switch_note(
                        "planner",
                        deliver_context(
                            "planner",
                            planner_seed if planner_seed is not None else ""))),
                    selected, io_in=io_in, io_out=io_out,
                    evaluation_policy=evaluation_policy,
                    resume_id=role_resume_id("planner"),
                    on_session=role_saver("planner"),
                    plan_json_path=plan_json_path, plan_md_path=plan_md_path,
                    review_path=planner_review_path,
                    reviewer_resume_id=role_resume_id(PLANNING_ADVISOR),
                    on_reviewer_session=role_saver(PLANNING_ADVISOR),
                    reviewer_context=reviewer_ctx,
                    reviewer_context_update=reviewer_gap(PLANNING_ADVISOR)
                    if PLANNING_ADVISOR in selected else None,
                    on_reviewer_context_ack=context_acker(PLANNING_ADVISOR),
                    trace=trace,
                    eval_scratch_path=eval_scratch["planner"],
                    reviewer_eval_scratch_path=eval_scratch[PLANNING_ADVISOR],
                    scores_path=scores_path, session_uuid=session_uuid,
                    intel_path=intel_path, planning_epoch=epoch_box["epoch"],
                    intel_md_path=intel_md_path,
                    skip_baseline=planner_skip_baseline,
                    review_packet_ctx={"epoch": epoch_box["epoch"],
                                       "attempt": planner_attempt_box["attempt"],
                                       "context_revision": current_rev},
                    switch_controller_fn=switch_controller,
                    reviewer_switch_note_fn=switch_note_for,
                    on_reviewer_switch_consumed=clear_pending_switch_for,
                    on_first_send_accepted=planner_first_send_cb,
                    on_first_send_rejected=planner_first_send_rejected_cb,
                    reviewer_controller_check_fn=reviewer_controller_check,
                    headless=headless,
                    gate_preview=make_gate_preview(
                        "planner", builder_on_team, session_enabled),
                    save_pending_turn_fn=save_pending_turn_for,
                    clear_pending_turn_fn=clear_pending_switch_for,
                    worktree=active_worktree, worktree_base=active_worktree_root,
                    on_outcome=lambda o, p: planner_box.update(outcome=o, payload=p))
                if rc != 0:
                    action = recover_controller_failure("planner", "startup_or_probe")
                    if action == "retry":
                        # BL-3: see the scout phase's identical comment --
                        # a fresh attempt, not a fresh epoch.
                        planner_attempt_box["attempt"] += 1
                        continue
                    if action == "switch":
                        planner_attempt_box["attempt"] += 1
                        planner_seed = prepare_fresh_seed_after_switch("planner")
                        continue
                    break
                if (rc == 0 and planner_box["outcome"] == "switch_controller"):
                    payload = planner_box["payload"] or {}
                    if payload.get("pending"):
                        pending_switch_turns["planner"] = payload.get("pending")
                    if switch_controller("planner", reason=payload.get("reason"),
                                         source="gate",
                                         target=payload.get("target")):
                        planner_attempt_box["attempt"] += 1
                        planner_seed = prepare_fresh_seed_after_switch("planner")
                        continue
                    rc = 1
                    break
                if rc == 0 and planner_first_send_box["delivered"]:
                    ack_lead("planner")
                if rc == 0 and planner_box["outcome"] == "handoff":
                    # User-confirmed hand-back (planner -> its pre-processor):
                    # resume the scout session with the handoff payload and run the
                    # full scout cycle again.
                    phase = set_phase("scouting")
                    # Each planner -> scout hand-back is a new scouting phase: bump
                    # the scouting epoch so a stale scout hash-gate baseline from the
                    # prior pass cannot authorize a skip on the re-investigated intel.
                    bump_scouting_epoch()
                    trace.event("handoff.execute", from_role="planner",
                                to_role=HANDBACK_PREPROCESSOR["planner"],
                                **trace_store.prompt_meta(
                                    planner_box["payload"] or "", prefix="payload"))
                    scout_seed = with_discovery(
                        handoff_wake_block(planner_box["payload"], intel_dir))
                    planner_seed = None
                    continue
                if (rc == 0 and planner_box["outcome"] == "approved"
                        and builder_on_team):
                    # Plan approved with a builder on the team: chain into the
                    # building phase. Each plan-approved -> building transition is a
                    # new building phase (the epoch bumps so the consumed-plan evals
                    # re-fire after a hand-back round trip even on byte-identical
                    # re-approved plans). A builder session that already exists
                    # (hand-back round trip, or a crash after building started)
                    # digests the updated plan; a fresh one is seeded from scratch.
                    phase = set_phase("building")
                    bump_building_epoch()
                    if role_resume_id("builder"):
                        builder_seed = plan_updated_block(
                            plan_json_path, plan_md_path)
                    else:
                        builder_seed = assemble_builder_seed(
                            plan_json_path, plan_md_path, shared_context,
                            intel_dir, current_rev)
                    continue
                if (rc == 0 and planner_box["outcome"] == "approved"
                        and not builder_on_team):
                    # No builder on the team: the plan is the deliverable. Informa-
                    # tional only; the phase stays `planning` so a rerun resumes the
                    # planner conversation.
                    io_out.write(
                        "cowork: building not selected — run ends with the plan as "
                        "the deliverable.\n")
                # Plan approval (no builder), EOF, or interrupt ends the run the
                # same way the scout loop always has.
                break

            # building phase
            if not ensure_controller_available("builder", reason="lead_launch"):
                rc = 1
                break
            builder_box = {"outcome": None, "payload": None}
            (builder_first_send_cb, builder_first_send_rejected_cb,
             builder_first_send_box) = _first_send_delivery_tracker(
                _make_pending_replay_cb(
                    "builder", pending_switch_for("builder"), phase,
                    session_uuid, trace, clear_pending_switch_for)
                if pending_switch_for("builder") else None)
            rc = run_builder_fn(
                config,
                with_headless_lead(seed_with_switch_note(
                    "builder",
                    deliver_context("builder",
                                    builder_seed if builder_seed is not None else ""))),
                selected, io_in=io_in, io_out=io_out,
                evaluation_policy=evaluation_policy,
                resume_id=role_resume_id("builder"),
                on_session=role_saver("builder"),
                build_status_path=build_status_path,
                build_review_path=build_review_path,
                reviewer_resume_id=role_resume_id(BUILD_REVIEWER),
                on_reviewer_session=role_saver(BUILD_REVIEWER),
                reviewer_context=reviewer_ctx,
                reviewer_context_update=reviewer_gap(BUILD_REVIEWER)
                if BUILD_REVIEWER in selected else None,
                on_reviewer_context_ack=context_acker(BUILD_REVIEWER),
                trace=trace,
                eval_scratch_path=eval_scratch["builder"],
                reviewer_eval_scratch_path=eval_scratch[BUILD_REVIEWER],
                scores_path=scores_path, session_uuid=session_uuid,
                plan_json_path=plan_json_path, plan_md_path=plan_md_path,
                building_epoch=building_epoch_box["epoch"],
                baseline_note=build_baseline()["note"],
                baseline_repos=build_baseline()["repos"],
                build_summary_path=build_summary_path,
                review_packet_ctx={"epoch": building_epoch_box["epoch"],
                                   "attempt": builder_attempt_box["attempt"],
                                   "context_revision": current_rev},
                switch_controller_fn=switch_controller,
                reviewer_switch_note_fn=switch_note_for,
                on_reviewer_switch_consumed=clear_pending_switch_for,
                on_first_send_accepted=builder_first_send_cb,
                on_first_send_rejected=builder_first_send_rejected_cb,
                reviewer_controller_check_fn=reviewer_controller_check,
                headless=headless,
                gate_preview=make_gate_preview(
                    "builder", builder_on_team, session_enabled),
                save_pending_turn_fn=save_pending_turn_for,
                clear_pending_turn_fn=clear_pending_switch_for,
                worktree=active_worktree, worktree_base=active_worktree_root,
                on_outcome=lambda o, p: builder_box.update(outcome=o, payload=p))
            if rc != 0:
                action = recover_controller_failure("builder", "startup_or_probe")
                if action == "retry":
                    # BL-3: see the scout phase's identical comment -- a
                    # fresh attempt, not a fresh epoch.
                    builder_attempt_box["attempt"] += 1
                    continue
                if action == "switch":
                    builder_attempt_box["attempt"] += 1
                    builder_seed = prepare_fresh_seed_after_switch("builder")
                    continue
                break
            if (rc == 0 and builder_box["outcome"] == "switch_controller"):
                payload = builder_box["payload"] or {}
                if payload.get("pending"):
                    pending_switch_turns["builder"] = payload.get("pending")
                if switch_controller("builder", reason=payload.get("reason"),
                                     source="gate",
                                     target=payload.get("target")):
                    builder_attempt_box["attempt"] += 1
                    builder_seed = prepare_fresh_seed_after_switch("builder")
                    continue
                rc = 1
                break
            if rc == 0 and builder_first_send_box["delivered"]:
                ack_lead("builder")
            if rc == 0 and builder_box["outcome"] == "handoff":
                # User-confirmed hand-back (builder -> planner): resume the planner
                # session with the handoff payload, re-plan, and chain forward into
                # the building phase again on the next plan approval.
                phase = set_phase("planning")
                bump_planning_epoch()
                trace.event("handoff.execute", from_role="builder",
                            to_role=HANDBACK_PREPROCESSOR["builder"],
                            **trace_store.prompt_meta(
                                builder_box["payload"] or "", prefix="payload"))
                planner_seed = plan_handback_wake_block(
                    builder_box["payload"], intel_dir)
                builder_seed = None
                continue
            # Build approval is terminal for this run (the phase stays `building`,
            # so a rerun resumes the builder conversation), and EOF/interrupt ends
            # the run the same way.
            break
    finally:
        if _prior_sigterm_handler is not None:
            try:
                signal.signal(signal.SIGTERM, _prior_sigterm_handler)
            except (ValueError, RuntimeError):
                pass

    # SESSION END: the last checkpoint. It also drains anything queued during
    # the final phase, so a normally-ending run leaves nothing pending.
    # Session end closes every phase: nothing more can supersede a candidate.
    measurement_checkpoint("session.end",
                           closed_phases=list(PHASE_PAIRS) + [phase])
    trace.event("run.end", rc=rc)
    return rc


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        # --check / --report are read-only and short-circuit BELOW, before
        # run_flow — which is what keeps them working on a session whose saved
        # policy is unreadable. They stay mutually exclusive with the two
        # session-mutating controller flags.
        for flag, supplied in (("--switch-controller",
                                bool(args.switch_controller)),
                               ("--allow-controllers",
                                args.allow_controllers is not None)):
            if supplied and args.check:
                sys.stderr.write(
                    "cowork: %s cannot be combined with --check.\n" % flag)
                return 2
            if supplied and args.report:
                sys.stderr.write(
                    "cowork: %s cannot be combined with --report.\n" % flag)
                return 2
        if args.check:
            return preflight.main()
        if args.report:
            return run_report(args)
        # Targeted orchestrator-owned evaluation: a read-mostly side channel
        # (it writes only orchestrator-evaluations.json, never session state or
        # a phase gate), dispatched here like --check/--report, before run_flow.
        if getattr(args, "evaluate_role", None):
            return run_orchestrator_eval(args)
        # Runtime controller dispatches are governed from this point onward.
        # Read-only --check/--report paths above never need a broker.
        prior_guard = bridge.set_nested_guard_active(True)
        try:
            return run_flow(args)
        finally:
            bridge.set_nested_guard_active(prior_guard)
    except KeyboardInterrupt:
        # Clean exit on Ctrl-C instead of dumping a traceback. 130 = 128 + SIGINT.
        sys.stderr.write("\ncowork: interrupted.\n")
        return 130
    except EOFError:
        # Ctrl-D at a prompt / closed stdin.
        sys.stderr.write("\ncowork: input closed.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
