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
import glob
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
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
import cowork_diffpacket as diffpacket  # noqa: E402
import cowork_ui as ui  # noqa: E402

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
SCOUT_REVIEWER = "scout-reviewer"
PLANNING_ADVISOR = "planning-advisor"
BUILD_REVIEWER = "build-reviewer"
ROLES = ["scout", SCOUT_REVIEWER, "planner", PLANNING_ADVISOR, "builder",
         BUILD_REVIEWER]

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
    SCOUT_REVIEWER: {"controller": "codex", "model": None, "effort": None,
                     "yolo": True, "mode": "implement"},
    "planner": {"controller": "claude", "model": None, "effort": None,
                "yolo": True, "mode": "implement"},
    PLANNING_ADVISOR: {"controller": "codex", "model": None, "effort": None,
                       "yolo": True, "mode": "implement"},
    "builder": {"controller": "claude", "model": None, "effort": None,
                "yolo": True, "mode": "implement"},
    BUILD_REVIEWER: {"controller": "codex", "model": None, "effort": None,
                     "yolo": True, "mode": "implement"},
}

CONTROLLERS = ("claude", "codex", "opencode")
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


def _q_checkbox(message, options, checked=None):
    """questionary multi-select. Returns the picked list (or None on Ctrl-C)."""
    import questionary
    from questionary import Choice
    checked = set(checked or [])
    return questionary.checkbox(
        message, choices=[Choice(o, checked=(o in checked)) for o in options]
    ).ask()


def _q_select(options, default=None, message=""):
    """questionary single-select. Returns the picked item, falling back to
    `default` on cancel so callers never get None."""
    import questionary
    picked = questionary.select(
        message or "", choices=list(options), default=default).ask()
    return picked if picked is not None else default


def _q_text(message, default=""):
    """questionary free-text input. Returns `default` on cancel."""
    import questionary
    val = questionary.text(message, default=default).ask()
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


def build_parser():
    p = argparse.ArgumentParser(prog="cowork", add_help=True)
    p.add_argument("--check", action="store_true",
                   help="run the preflight dependency check only")
    p.add_argument("--report", nargs="?", const=True, metavar="SESSION_UUID",
                   help="print a plain-text token/byte report for a cowork "
                        "session (defaults to this directory's most recent "
                        "session) and exit")
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
                   metavar="ROLE=CONTROLLER",
                   help="switch one current-phase role in an existing saved "
                        "session to claude or codex, then continue")
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
    return p


def _is_non_interactive(args):
    return bool(args.team or args.config or args.context is not None
                or args.context_file or getattr(args, "headless", False))


def run_report(args, io_out=None):
    """Handle `cowork --report [<session-uuid>]` (#2): read the session trace and
    print a plain-text token/byte summary. With no uuid, default to this
    directory's most recent session. Returns a process exit code."""
    io_out = io_out or sys.stdout
    session_uuid = args.report if isinstance(args.report, str) else None
    if not session_uuid:
        sessions = state_store.list_sessions()
        if not sessions:
            io_out.write("cowork: no sessions found for this directory.\n")
            return 1
        session_uuid = sessions[0]["id"]
    trace_path = trace_store.trace_path_for(session_uuid)
    if not os.path.exists(trace_path):
        io_out.write(
            "cowork: no trace found for session %s (looked at %s).\n"
            % (session_uuid, trace_path))
        return 1
    io_out.write(cowork_report.report_for_trace(trace_path, session_uuid))
    # Evaluation analysis (scores + tool/model identity + per-eval usage)
    # appended when this session recorded peer evaluations.
    scores_path = state_store.scores_path_for(session_uuid)
    if os.path.exists(scores_path):
        io_out.write("\n")
        io_out.write(cowork_report.report_for_scores(scores_path))
    io_out.flush()
    return 0


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
    return "\n\n".join([role_text.strip(), team_note.strip(), context.strip()]).strip()


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
    """Upsert the session role's live identity — tool, model, provider session
    id — into the per-session `identities.json` registry, so eval aggregation
    can stamp the EVALUATEE's tool+model onto score entries.

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
                "session_id": (result.get("session_id")
                               or result.get("thread_id")
                               or getattr(session, "session_id", None)
                               or getattr(session, "thread_id", None)),
            })
    except Exception:  # noqa: BLE001 - identity is observational only
        pass


def _send(session, text, meta=None):
    """Send one turn, passing per-turn accounting `meta` (#1) only when the
    session's send() accepts it. Real bridge sessions do; test-injected fake
    sessions keep their historical `send(text)` signature and receive no meta,
    so the streaming/test contract stays byte-identical.

    Every turn also refreshes the role-identity registry (tool/model/session
    id) from the session + its result — see `_record_role_identity`."""
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
             "path": intel_path, "kind": "json"}]
    if intel_md_path:
        arts.append({"label": "intel markdown (the user's review surface)",
                     "path": intel_md_path, "kind": "markdown"})
    return arts


def _review_packet(packet_ctx, artifacts, force_full_reread=False):
    """Build the path-first embedded-artifact block via the diff-packet helper
    (#4). `packet_ctx` carries {reviewer_role, epoch, context_revision,
    snapshot_dir}; returns None when packet_ctx is absent (legacy full-embed
    callers fall back to embedding bodies). The returned value is a
    diffpacket.Packet (a str subclass) carrying its delivery mode + per-path
    embedded bytes (#3)."""
    if not packet_ctx:
        return None
    return diffpacket.build_review_packet(
        packet_ctx["reviewer_role"], packet_ctx["epoch"],
        packet_ctx["context_revision"], artifacts, packet_ctx["snapshot_dir"],
        force_full_reread=force_full_reread)


def _carry_delivery(text, src):
    """Wrap an assembled reviewer-context string so its caller can read how the
    embedded artifacts were actually delivered (#3) without re-inferring the
    packet form. `src` is the diffpacket.Packet the assembler spliced in (or an
    already-wrapped body being re-prefixed with a context-update block);
    delivery + per-path embedded bytes are copied off it. When `src` carries no
    delivery (a legacy full-embed body — a plain str), the text is returned
    unwrapped, so a caller reading getattr(block, "delivery", "embedded") sees
    "embedded" there. Transparent as a plain str either way."""
    delivery = getattr(src, "delivery", None)
    if delivery is None:
        return text
    return diffpacket.Packet(text, delivery=delivery,
                             embedded=getattr(src, "embedded", None))


def assemble_reviewer_context(context, selected, intel_path, intel_md_path=None,
                              packet_ctx=None):
    """The reviewer's situational context: the SAME initial `context` the scout
    received, the team framing, and the scout's current intel to review.

    When `intel_md_path` is given, BOTH the intel JSON (machine source of truth)
    and the intel markdown (the user's review surface) are embedded, so the
    scout-reviewer reviews both and can check the markdown stays CONSISTENT with
    the JSON (it must not under- or mis-report it).

    With `packet_ctx` (#4) the intel bodies are NOT embedded: a path-first
    full-reread packet (paths + hashes + sizes + read-from-disk instruction) is
    sent and a snapshot is written for the next round's diff. The fresh path
    always uses full-reread (no diff is possible yet, D6).

    Deliberately excludes the scout's write-target `brief` / `first` payload —
    that carries the scout's own guardrail and would mis-instruct the reviewer."""
    team = ", ".join(selected) if selected else "(unspecified)"
    packet = _review_packet(
        packet_ctx, _intel_artifacts(intel_path, intel_md_path),
        force_full_reread=True)
    if packet is not None:
        return _carry_delivery(
            "Shared initial context — this is the SAME context the scout was "
            "given:\n%s\n\n"
            "Team on this session: %s\n\n"
            "Review the scout's current intel critically against the context "
            "above.\n\n%s" % (context.strip(), team, packet), packet)
    intel_text = _read_text(intel_path)
    if intel_md_path:
        return (
            "Shared initial context — this is the SAME context the scout was "
            "given:\n%s\n\n"
            "Team on this session: %s\n\n"
            "The scout's current intel JSON (the machine source of truth — "
            "review it critically against the context above):\n%s\n\n"
            "The scout's current intel markdown (the user's review surface — "
            "check it stays small, scannable, and CONSISTENT with the JSON):\n%s"
            % (context.strip(), team, intel_text.strip(),
               _read_text(intel_md_path).strip())
        )
    return (
        "Shared initial context — this is the SAME context the scout was given:\n"
        "%s\n\n"
        "Team on this session: %s\n\n"
        "The scout's current intel (review it critically against the context "
        "above):\n%s" % (context.strip(), team, intel_text.strip())
    )


def assemble_reviewer_handoff(verdict, review, artifact="intel"):
    """Build the role-facing handoff string from a reviewer verdict dict.

    Pure string templating — NO second model call. This is the reviewed role's
    half of the faithful-relay guardrail: for `needs_user` it carries the
    reviewer's FULL `user_question` plus an instruction to relay it without
    changing its meaning or dropping context. `artifact` names what was
    reviewed ("intel" for the scout, "plan" for the planner). Returns "" for
    `approve` (no handoff; fall through to the user gate)."""
    review = review or {}
    findings = review.get("findings") or []
    if verdict == "needs_user":
        question = (review.get("user_question") or "").strip()
        return (
            "[reviewer handoff] Before this can go to the user for approval, a "
            "blocking product question is unresolved. Put this question to the "
            "user in your own next reply. You MAY rephrase it into your own voice, "
            "but you must NOT change its meaning or omit any part of its context. "
            "Then set status back to needs_input.\n\n"
            "Question: %s" % question
        )
    if verdict == "revise":
        bullet = "\n".join("- " + str(f) for f in findings) if findings else \
            "- (no specific findings provided)"
        return (
            "[reviewer handoff] A reviewer checked your %s and it is not ready "
            "to hand off yet. Address the following, update your %s, and set "
            "status back to ready_for_review when done. Do not mention the "
            "reviewer to the user.\n%s" % (artifact, artifact, bullet)
        )
    return ""


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
_REVIEWER_EVALUATEE = {SCOUT_REVIEWER: "scout", PLANNING_ADVISOR: "planner",
                       BUILD_REVIEWER: "builder"}


def assemble_eval_prompt(evaluator, scratch_path, specs):
    """The private evaluation request sent to `evaluator` on its own session.

    `specs` is a list of {evaluatee, criteria, artifact_block} dicts — the
    artifact_block embeds the evidence (the full verdict JSON for
    role->reviewer evals; the full approved scout intel JSON for ->scout
    evals) so the prompt is self-contained for every verdict kind. The
    aggregate scores path is deliberately never part of this prompt."""
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


def _consumed_upstream_spec(consumed, scores_path, evaluator, round_index):
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
    text = "\n\n".join(_read_text(p).strip() for p in paths)
    label = consumed.get("label") or "upstream artifact"
    arts = [{"label": label, "path": p,
             "kind": "json" if str(p).endswith(".json") else "markdown"}
            for p in paths]
    packet = diffpacket.build_full_reread_packet(
        arts,
        header_label="The %s this phase consumed (current files on disk — the "
                     "authoritative source of truth; read them before scoring):"
                     % label)
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
                    round_index, stamp_by_evaluatee, trace=None):
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
        stamped.append(entry)
    ok = state_store.append_score_entries(scores_path, session_uuid, stamped)
    if trace:
        trace.event("eval.aggregated", evaluator=evaluator, phase=phase,
                    round=round_index, count=len(stamped),
                    result="ok" if ok else "write_failed")
    return ok


def _make_evaluate_fn(role, reviewer_role, phase, scratch_path, scores_path,
                      session_uuid, intel_path=None, planning_epoch=None,
                      consumed_upstream=None, trace=None, intel_md_path=None,
                      context_revision=None):
    """Build the role-side `evaluate_fn(session, verdict, round_index)` for
    `_role_loop`, or None when eval is not wired (missing paths).

    The closure sends the eval prompt on the role's own persistent session
    with its output muted (the eval is private), reads the role's scratch
    back, and aggregates. The verdict JSON is always embedded — on approve the
    findings never reach the role via the reviewer handoff, so embedding keeps
    the prompt self-contained for every verdict kind.

    When a phase consumes an upstream artifact (the planning phase consumes the
    scout intel; the building phase consumes the approved plan), the FIRST eval
    turn additionally bundles a once-per-phase consumed-upstream eval with that
    artifact embedded. `consumed_upstream` names it; for back-compat callers may
    instead pass `intel_path`/`planning_epoch` and the scout descriptor is built
    for them."""
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
        specs = [{
            "evaluatee": reviewer_role,
            "criteria": EVAL_CRITERIA[(role, reviewer_role)],
            "artifact_block":
                "The reviewer's full verdict JSON for this round:\n%s"
                % json.dumps(verdict or {}, indent=2, sort_keys=True),
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
        # Per-turn accounting (#1/D11): the lead's eval is a follow-up turn on
        # its own still-open session (a resume). The reviewer's verdict is
        # embedded inline (not a file); the only embedded ARTIFACT FILES are the
        # consumed-upstream bundle (scout intel for the planner eval; plan
        # JSON+md for the builder eval), and only when it rides this turn
        # (len(specs) > 1). The scratch file is the eval's OUTPUT target (cleared
        # above), so it is never an embedded artifact.
        # The consumed-upstream bundle now rides path-first (#2), so its
        # artifact files are referenced by path, not embedded — tag the
        # descriptors accordingly so the report does not over-count them.
        eval_artifacts = None
        if len(specs) > 1:
            eval_artifacts = _artifact_descriptors(
                consumed_upstream.get("artifact_paths"), delivery="path")
        eval_turn_id = str(uuid.uuid4())
        with _muted_session(session):
            send_result = _send(session, prompt, meta={
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


def context_update_block(text):
    """Wake block for any role resuming a CLI session that has not acknowledged
    the current session context revision. Role-agnostic: scout, scout-reviewer,
    and future roles all receive the same framing."""
    return (
        "New user context was provided for this resumed cowork session.\n\n"
        "Treat this as the current task context. Keep prior session knowledge "
        "only where it remains compatible.\n\n"
        "<context>\n%s\n</context>" % text.strip()
    )


def assemble_reviewer_resume_context(intel_path, intel_md_path=None,
                                     context_update=None, packet_ctx=None,
                                     force_full_reread=False):
    """Lighter context for a RESUMED reviewer session: its thread already holds
    the role + the prior context, so only the updated intel is sent — plus a
    context-update wake block when the session context changed since the
    reviewer last acknowledged it. When `intel_md_path` is given, BOTH the
    updated intel JSON and markdown are sent (the reviewer reviews both).

    With `packet_ctx` (#4) the bodies are replaced by a diff packet when
    eligible (prior snapshot for this epoch+context_revision, canonicalizable,
    within the size cap), else a path-first full-reread packet. `force_full_reread`
    (the malformed/weak-verdict retry, D8) forces the full-reread packet."""
    packet = _review_packet(
        packet_ctx, _intel_artifacts(intel_path, intel_md_path),
        force_full_reread=force_full_reread)
    if packet is not None:
        body = _carry_delivery(
            "The scout has updated its intel since your last review. Re-review "
            "against the current task context and write your verdict to the "
            "review file again.\n\n%s" % packet, packet)
    elif intel_md_path:
        body = (
            "The scout has updated its intel since your last review. Re-review "
            "both current artifacts below against the current task context, and "
            "write your verdict to the review file again.\n\n"
            "Current intel JSON:\n%s\n\n"
            "Current intel markdown (check it stays consistent with the JSON):"
            "\n%s"
            % (_read_text(intel_path).strip(),
               _read_text(intel_md_path).strip())
        )
    else:
        body = (
            "The scout has updated its intel since your last review. Re-review "
            "the current intel below against the current task context, and "
            "write your verdict to the review file again:\n%s"
            % _read_text(intel_path).strip()
        )
    if context_update:
        return _carry_delivery(
            context_update_block(context_update) + "\n\n" + body, body)
    return body


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
               epoch=None, context_revision=None, snapshot_dir=None,
               force_full_reread=False):
        return run_reviewer_once(
            config, context, selected, intel_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            epoch=epoch, context_revision=context_revision,
            snapshot_dir=snapshot_dir, force_full_reread=force_full_reread,
            artifact_paths=[intel_path, intel_md_path], phase="scouting",
            reviewer_role=SCOUT_REVIEWER,
            prompt_path=SCOUT_REVIEWER_PROMPT_PATH,
            protected="the scout intel files (JSON and markdown)",
            context_fn=lambda ctx, sel, p, packet_ctx=None:
                assemble_reviewer_context(
                    ctx, sel, p, intel_md_path, packet_ctx=packet_ctx),
            resume_context_fn=lambda p, context_update=None, packet_ctx=None,
                force_full_reread=False:
                assemble_reviewer_resume_context(
                    p, intel_md_path, context_update=context_update,
                    packet_ctx=packet_ctx, force_full_reread=force_full_reread))
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


def assemble_planner_seed(intel_path, context):
    """The fresh planner's situational context: a path-first FULL-REREAD packet
    for the approved scout intel (#1 — the body is read from disk, not embedded)
    plus the current shared session context. The artifact SET (intel JSON) is
    unchanged; only the delivery is path-first."""
    packet = diffpacket.build_full_reread_packet(
        _intel_artifacts(intel_path),
        header_label="Approved scout intel (current file on disk — the "
                     "authoritative source of truth):")
    return (
        "The scout phase is complete and the user APPROVED the scout intel. "
        "Digest it and drive the planning conversation.\n\n"
        "%s\n\n"
        "Current shared context:\n%s" % (packet, (context or "").strip())
    )


def intel_updated_block(intel_path):
    """Wake block for a resumed planner after a hand-back round trip: the scout
    re-ran its full cycle and the user approved the UPDATED intel. The intel is
    delivered path-first (#1 — read from disk, not embedded)."""
    packet = diffpacket.build_full_reread_packet(
        _intel_artifacts(intel_path),
        header_label="Updated approved intel (current file on disk — the "
                     "authoritative source of truth):")
    return (
        "The scout intel changed since you started planning: your hand-back was "
        "executed, the scout re-investigated, and the user approved the updated "
        "intel. Digest it and continue planning. Keep prior plan content "
        "only where it remains compatible.\n\n"
        "%s" % packet
    )


def handoff_wake_block(payload):
    """Wake block for the scout session resumed by a planner hand-back."""
    return (
        "The planner handed the work back to you mid-planning (the user "
        "confirmed the hand-back). Re-run your full cycle: investigate, clarify "
        "with the user, update your intel file, and set status "
        "ready_for_review when done.\n\n"
        "<handoff>\n%s\n</handoff>" % (payload or "").strip()
    )


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


def assemble_builder_seed(plan_json_path, plan_md_path, context):
    """The fresh builder's situational context: a path-first FULL-REREAD packet
    for the approved plan (#1 — JSON + markdown read from disk, not embedded)
    plus the current shared session context. The artifact SET (plan JSON + MD)
    is unchanged; only the delivery is path-first."""
    packet = diffpacket.build_full_reread_packet(
        _plan_artifacts(plan_json_path, plan_md_path),
        header_label="Approved plan (current files on disk — the authoritative "
                     "source of truth):")
    return (
        "The planning phase is complete and the user APPROVED the plan. "
        "Execute it: make the code changes, verify them, and drive the build "
        "conversation.\n\n"
        "%s\n\n"
        "Current shared context:\n%s" % (packet, (context or "").strip())
    )


def plan_updated_block(plan_json_path, plan_md_path):
    """Wake block for a resumed builder after a hand-back round trip: the
    builder handed back to the planner, the planner re-planned, and the user
    approved the UPDATED plan. The plan is delivered path-first (#1 — read from
    disk, not embedded)."""
    packet = diffpacket.build_full_reread_packet(
        _plan_artifacts(plan_json_path, plan_md_path),
        header_label="Updated approved plan (current files on disk — the "
                     "authoritative source of truth):")
    return (
        "The plan changed since you started building: your hand-back was "
        "executed, the planner re-planned, and the user approved the UPDATED "
        "plan. Digest the changes and continue building. Keep prior work "
        "only where it remains compatible.\n\n"
        "%s" % packet
    )


def plan_handback_wake_block(payload):
    """Wake block for the planner session resumed by a builder hand-back."""
    return (
        "The builder handed the work back to you mid-build (the user confirmed "
        "the hand-back). Re-plan as needed: digest the builder's note, update "
        "your plan files, clarify with the user, and set status "
        "ready_for_review when done.\n\n"
        "<handoff>\n%s\n</handoff>" % (payload or "").strip()
    )


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
         "path": plan_json_path, "kind": "json"},
        {"label": "plan markdown (the user's review surface)",
         "path": plan_md_path, "kind": "markdown"},
    ]


def assemble_advisor_context(context, selected, plan_json_path, plan_md_path,
                             packet_ctx=None):
    """The planning-advisor's situational context: the shared session context,
    the team framing, and BOTH planner artifacts to review.

    With `packet_ctx` (#4) the plan bodies are replaced by a path-first
    full-reread packet (fresh path always full-reread, D6); a snapshot is
    written for the next round's diff."""
    team = ", ".join(selected) if selected else "(unspecified)"
    packet = _review_packet(
        packet_ctx, _plan_artifacts(plan_json_path, plan_md_path),
        force_full_reread=True)
    if packet is not None:
        return _carry_delivery(
            "Shared session context — this is the SAME context the planner was "
            "given:\n%s\n\n"
            "Team on this session: %s\n\n"
            "Review the planner's current plan critically against the context "
            "above.\n\n%s" % (context.strip(), team, packet), packet)
    return (
        "Shared session context — this is the SAME context the planner was "
        "given:\n%s\n\n"
        "Team on this session: %s\n\n"
        "The planner's current plan JSON (the machine source of truth — review "
        "it critically against the context above):\n%s\n\n"
        "The planner's current plan markdown (the user's review surface — check "
        "it stays small, scannable, and consistent with the JSON):\n%s"
        % (context.strip(), team, _read_text(plan_json_path).strip(),
           _read_text(plan_md_path).strip())
    )


def assemble_advisor_resume_context(plan_json_path, plan_md_path,
                                    context_update=None, packet_ctx=None,
                                    force_full_reread=False):
    """Lighter context for a RESUMED planning-advisor session: only the updated
    plan artifacts — plus a context-update wake block when the session context
    changed since the advisor last acknowledged it.

    With `packet_ctx` (#4) the bodies are replaced by a diff packet when
    eligible, else a path-first full-reread packet; `force_full_reread` forces
    the full-reread packet (the malformed/weak-verdict retry, D8)."""
    packet = _review_packet(
        packet_ctx, _plan_artifacts(plan_json_path, plan_md_path),
        force_full_reread=force_full_reread)
    if packet is not None:
        body = _carry_delivery(
            "The planner has updated its plan since your last review. Re-review "
            "against the current task context and write your verdict to the "
            "review file again.\n\n%s" % packet, packet)
    else:
        body = (
            "The planner has updated its plan since your last review. Re-review "
            "both current artifacts below against the current task context, and "
            "write your verdict to the review file again.\n\n"
            "Current plan JSON:\n%s\n\n"
            "Current plan markdown:\n%s"
            % (_read_text(plan_json_path).strip(),
               _read_text(plan_md_path).strip())
        )
    if context_update:
        return _carry_delivery(
            context_update_block(context_update) + "\n\n" + body, body)
    return body


def make_planning_advisor_runner(plan_md_path, trace=None,
                                 extra_writable_dir=None):
    """Build the real (non-test) reviewer runner for the planning phase: a
    `run_reviewer_once` closure carrying the advisor role, prompt, and the
    dual-artifact context assemblers. `extra_writable_dir` is the relocated
    session-assets root, granted to the advisor CLI so its review/eval writes
    (now outside cwd) succeed on the no-yolo path."""
    def runner(config, context, selected, plan_json_path, review_path,
               resume_id=None, on_session=None, context_update=None,
               eval_scratch_path=None, eval_specs=None, surface_io_out=None,
               epoch=None, context_revision=None, snapshot_dir=None,
               force_full_reread=False):
        return run_reviewer_once(
            config, context, selected, plan_json_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            epoch=epoch, context_revision=context_revision,
            snapshot_dir=snapshot_dir, force_full_reread=force_full_reread,
            artifact_paths=[plan_json_path, plan_md_path], phase="planning",
            reviewer_role=PLANNING_ADVISOR,
            prompt_path=PLANNING_ADVISOR_PROMPT_PATH,
            protected="the planner's plan files",
            context_fn=lambda ctx, sel, p, packet_ctx=None:
                assemble_advisor_context(
                    ctx, sel, p, plan_md_path, packet_ctx=packet_ctx),
            resume_context_fn=lambda p, context_update=None, packet_ctx=None,
                force_full_reread=False:
                assemble_advisor_resume_context(
                    p, plan_md_path, context_update=context_update,
                    packet_ctx=packet_ctx, force_full_reread=force_full_reread))
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
    brief = assemble_worktree_brief(status_path, base_toplevel, name, explicit)
    # Clear any stale artifact so a failed/no-write run reads as None, never a
    # leftover 'ready' from an earlier attempt.
    try:
        os.remove(status_path)
    except OSError:
        pass
    if trace:
        trace.event("worktree.run.start", controller=controller,
                    base_toplevel=base_toplevel, name=name, explicit=explicit,
                    status_path=status_path)
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
        if session_factory:
            session = session_factory("opencode")
        else:
            session = bridge.OpencodeSession(
                WORKTREE_PROMPT_PATH, wt_config["mode"], wt_config["yolo"],
                io_out=io_out, speaker=WORKTREE_ROLE, trace=trace,
                extra_writable_dir=extra_writable_dir,
                model=wt_config.get("model"), effort=wt_config.get("effort"))
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
        _send(session, first, meta={"prompt_kind": "worktree_seed",
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
    """The full-delta capture recipe handed to the build-reviewer. Plain
    `git diff` is NOT enough — it misses staged changes and untracked new files
    (and the builder creates files), so the recipe names every channel.

    `repos` is the EXPLICIT selected repo-root list, each entry
    ``{"path", "has_head"}``. When given, the recipe enumerates EVERY root and
    branches the capture per root: a root WITH a baseline HEAD uses
    ``git -C <root> diff HEAD``; a root with NO HEAD (unborn repo / non-git
    fallback) must NOT run ``git diff HEAD`` (it fails 'bad revision HEAD') and
    uses ``status --porcelain`` + ``diff --cached`` + ``diff`` + untracked reads.
    The union of per-root deltas is the review unit. When `repos` is empty the
    recipe falls back to the single process-cwd form (back-compat)."""
    note = ("\n" + baseline_note) if baseline_note else ""
    if not repos:
        return (
            "The unit of review is the builder's FULL working-tree delta against "
            "this plan. The delta is NOT embedded here — capture the COMPLETE "
            "delta yourself. Plain `git diff` is insufficient: it omits STAGED "
            "changes and UNTRACKED new files (and the builder creates files). Run:"
            "\n  - `git status --porcelain` — every staged, unstaged, and "
            "untracked path at a glance;"
            "\n  - `git diff HEAD` (or `git diff --stat HEAD` first, then targeted "
            "`git diff HEAD -- <path>`) — all tracked staged+unstaged changes since "
            "the last commit;"
            "\n  - read each untracked/new file directly — it will NOT appear in "
            "`git diff`."
            "%s"
            "\nReview the full delta critically against the plan and context "
            "above." % note)

    blocks = []
    for r in repos:
        path = r.get("path", ".")
        if r.get("has_head"):
            blocks.append(
                "  Repo %s (has a baseline commit):"
                "\n    - `git -C %s status --porcelain` — staged, unstaged, and "
                "untracked paths;"
                "\n    - `git -C %s diff HEAD` (or `git -C %s diff --stat HEAD` "
                "first, then targeted `git -C %s diff HEAD -- <path>`) — all "
                "tracked staged+unstaged changes since the last commit;"
                "\n    - read each untracked/new file under %s directly — it "
                "will NOT appear in `git diff`."
                % (path, path, path, path, path, path))
        else:
            blocks.append(
                "  Repo %s (NO baseline commit — unborn repo or non-git "
                "fallback; do NOT run `git diff HEAD`, it fails):"
                "\n    - `git -C %s status --porcelain` — every path at a glance;"
                "\n    - `git -C %s diff --cached` and `git -C %s diff` — staged "
                "and unstaged changes;"
                "\n    - read untracked/new files under %s directly."
                % (path, path, path, path, path))
    return (
        "The unit of review is the builder's FULL working-tree delta against "
        "this plan, taken as the UNION of the deltas of EACH of these selected "
        "repo roots. The delta is NOT embedded here — capture the COMPLETE delta "
        "yourself, per root. Plain `git diff` is insufficient: it omits STAGED "
        "changes and UNTRACKED new files (and the builder creates files). "
        "Capture the delta of EACH of these repos:"
        "\n%s"
        "%s"
        "\nReview the union of per-root deltas critically against the plan and "
        "context above. An empty delta in a repo the plan touches is a finding; "
        "ignore repos the plan does not list."
        % ("\n".join(blocks), note))


def _build_summary_block(build_summary_path):
    """The build-reviewer-facing block embedding the builder's markdown summary,
    or "" when no summary path is wired (back-compat). The reviewer must
    consistency-check it against the actual working-tree delta + status JSON
    (D9) — the summary is the user's build-gate surface, so an unreviewed or
    mis-reporting summary must never reach the user."""
    if not build_summary_path:
        return ""
    return (
        "The builder's current markdown summary (the user's review surface — "
        "consistency-check it against the working-tree delta and the status "
        "JSON; it must not under- or mis-report what was actually built):\n%s"
        "\n\n" % _read_text(build_summary_path).strip())


def _build_reviewer_artifacts(plan_json_path, plan_md_path, build_status_path,
                              build_summary_path=None):
    arts = [
        {"label": "approved plan JSON (machine source of truth)",
         "path": plan_json_path, "kind": "json"},
        {"label": "approved plan markdown", "path": plan_md_path,
         "kind": "markdown"},
        {"label": "builder status JSON (status + verification log)",
         "path": build_status_path, "kind": "json"},
    ]
    if build_summary_path:
        arts.append({"label": "builder markdown summary (the user's review "
                     "surface)", "path": build_summary_path, "kind": "markdown"})
    return arts


def assemble_build_reviewer_context(context, selected, plan_json_path,
                                    plan_md_path, build_status_path,
                                    baseline_note="", baseline_repos=None,
                                    build_summary_path=None, packet_ctx=None):
    """The build-reviewer's situational context: the shared session context,
    the team framing, BOTH plan artifacts, the builder's status JSON, the
    builder's markdown summary (when wired), and the full-delta capture recipe
    (the delta is NOT embedded — a stale snapshot would mis-review).
    `baseline_repos` is the explicit selected repo-root list (each
    ``{path, has_head}``) that drives the per-root capture recipe.

    With `packet_ctx` (#4/D9) the embedded artifacts (plan JSON+md, status JSON,
    summary) become a path-first full-reread packet; the live working-tree delta
    recipe is left untouched."""
    team = ", ".join(selected) if selected else "(unspecified)"
    packet = _review_packet(
        packet_ctx, _build_reviewer_artifacts(
            plan_json_path, plan_md_path, build_status_path, build_summary_path),
        force_full_reread=True)
    if packet is not None:
        return _carry_delivery(
            "Shared session context — this is the SAME context the builder was "
            "given:\n%s\n\n"
            "Team on this session: %s\n\n"
            "%s\n\n"
            "%s"
            % (context.strip(), team, packet,
               _build_diff_recipe(baseline_repos, baseline_note)), packet)
    return (
        "Shared session context — this is the SAME context the builder was "
        "given:\n%s\n\n"
        "Team on this session: %s\n\n"
        "The approved plan JSON the builder is executing (the machine source "
        "of truth — review the build against it):\n%s\n\n"
        "The approved plan markdown (the human-readable summary):\n%s\n\n"
        "The builder's current status JSON (its status + verification log):"
        "\n%s\n\n"
        "%s"
        "%s"
        % (context.strip(), team, _read_text(plan_json_path).strip(),
           _read_text(plan_md_path).strip(),
           _read_text(build_status_path).strip(),
           _build_summary_block(build_summary_path),
           _build_diff_recipe(baseline_repos, baseline_note)))


def assemble_build_reviewer_resume_context(plan_json_path, plan_md_path,
                                           build_status_path,
                                           context_update=None,
                                           baseline_note="",
                                           baseline_repos=None,
                                           build_summary_path=None,
                                           packet_ctx=None,
                                           force_full_reread=False):
    """Lighter context for a RESUMED build-reviewer session: its thread already
    holds the role + the prior context, so only the updated artifacts are sent
    — plus a context-update wake block when the session context changed since
    the reviewer last acknowledged it. The full delta is still read live; the
    builder's markdown summary (when wired) is re-sent for the consistency
    check.

    With `packet_ctx` (#4/D9) the embedded artifacts become a diff packet when
    eligible, else a path-first full-reread packet; `force_full_reread` forces
    the full-reread packet. The live delta recipe is untouched."""
    packet = _review_packet(
        packet_ctx, _build_reviewer_artifacts(
            plan_json_path, plan_md_path, build_status_path, build_summary_path),
        force_full_reread=force_full_reread)
    if packet is not None:
        body = _carry_delivery(
            "The builder has updated its work since your last review. Re-review "
            "the current full working-tree delta against the plan and the "
            "builder's current status, and write your verdict to the review "
            "file again.\n\n"
            "%s\n\n"
            "%s"
            % (packet, _build_diff_recipe(baseline_repos, baseline_note)),
            packet)
    else:
        body = (
            "The builder has updated its work since your last review. Re-review "
            "the current full working-tree delta against the plan and the "
            "builder's current status below, and write your verdict to the "
            "review file again.\n\n"
            "Current plan JSON:\n%s\n\n"
            "Current plan markdown:\n%s\n\n"
            "Current builder status JSON:\n%s\n\n"
            "%s"
            "%s"
            % (_read_text(plan_json_path).strip(),
               _read_text(plan_md_path).strip(),
               _read_text(build_status_path).strip(),
               _build_summary_block(build_summary_path),
               _build_diff_recipe(baseline_repos, baseline_note)))
    if context_update:
        return _carry_delivery(
            context_update_block(context_update) + "\n\n" + body, body)
    return body


def make_build_reviewer_runner(plan_json_path, plan_md_path, baseline_note="",
                               baseline_repos=None, trace=None,
                               extra_writable_dir=None, build_summary_path=None):
    """Build the real (non-test) reviewer runner for the building phase: a
    `run_reviewer_once` closure carrying the build-reviewer role, prompt, and
    the full-delta context assemblers. The reviewed artifact passed to the
    runner is the builder's status file path; the delta itself is read live by
    the reviewer (`baseline_note` tells it which commit each repo's delta is
    measured from and whether a worktree started dirty; `baseline_repos` is the
    explicit selected repo-root list, each ``{path, has_head}``, that drives the
    per-root capture recipe)."""
    def runner(config, context, selected, build_status_path, review_path,
               resume_id=None, on_session=None, context_update=None,
               eval_scratch_path=None, eval_specs=None, surface_io_out=None,
               epoch=None, context_revision=None, snapshot_dir=None,
               force_full_reread=False):
        return run_reviewer_once(
            config, context, selected, build_status_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            epoch=epoch, context_revision=context_revision,
            snapshot_dir=snapshot_dir, force_full_reread=force_full_reread,
            artifact_paths=[plan_json_path, plan_md_path, build_status_path,
                            build_summary_path], phase="building",
            reviewer_role=BUILD_REVIEWER,
            prompt_path=BUILD_REVIEWER_PROMPT_PATH,
            protected="the builder's working-tree delta and status file",
            context_fn=lambda ctx, sel, p, packet_ctx=None:
                assemble_build_reviewer_context(
                    ctx, sel, plan_json_path, plan_md_path, p,
                    baseline_note=baseline_note, baseline_repos=baseline_repos,
                    build_summary_path=build_summary_path, packet_ctx=packet_ctx),
            resume_context_fn=lambda p, context_update=None, packet_ctx=None,
                force_full_reread=False:
                assemble_build_reviewer_resume_context(
                    plan_json_path, plan_md_path, p,
                    context_update=context_update, baseline_note=baseline_note,
                    baseline_repos=baseline_repos,
                    build_summary_path=build_summary_path,
                    packet_ctx=packet_ctx, force_full_reread=force_full_reread))
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
        # so it is a resume; it references the reviewed artifact + the review
        # file by PATH (the reviewer-side eval specs name them as paths, and the
        # consumed-upstream evidence rides path-first after #2), so tag the
        # descriptors as path-first — no bodies are embedded here.
        eval_turn_id = str(uuid.uuid4())
        send_result = _send(session, assemble_eval_prompt(
            reviewer_role, eval_scratch_path, eval_specs),
            meta={"prompt_kind": "eval", "fresh": False, "resume": True,
                  "phase": eval_specs[0].get("phase"),
                  "round": eval_specs[0].get("round"),
                  "context_revision": context_revision,
                  "artifacts": _artifact_descriptors(artifact_paths,
                                                     delivery="path"),
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
                      epoch=None, context_revision=None, snapshot_dir=None,
                      force_full_reread=False, artifact_paths=None, phase=None):
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
    # Diff-packet context (#4): only built when a snapshot_dir is wired (the real
    # runners pass it). packet_ctx keys the per-reviewer snapshot by phase epoch
    # + context revision; a test runner / legacy direct call leaves it None and
    # the assemblers fall back to embedding full bodies.
    packet_ctx = None
    if snapshot_dir is not None:
        packet_ctx = {"reviewer_role": reviewer_role, "epoch": epoch,
                      "context_revision": context_revision,
                      "snapshot_dir": snapshot_dir}
    # Build the reviewer context FIRST (before the trace + accounting) so the
    # delivery the packet actually chose — path / diff / embedded — is known and
    # can tag every descriptor truthfully (#3). The form is dynamic: a resumed
    # reviewer with a prior snapshot may get a diff; a fresh/legacy one gets a
    # full-reread or (no packet_ctx) the embedded fallback. ctx_block is a
    # diffpacket.Packet carrying .delivery + per-path .embedded when a packet
    # rode, else a plain str (delivery defaults to "embedded").
    if resume_id:
        ctx_block = (resume_context_fn or assemble_reviewer_resume_context)(
            intel_path, context_update=context_update, packet_ctx=packet_ctx,
            force_full_reread=force_full_reread)
    else:
        ctx_block = (context_fn or assemble_reviewer_context)(
            context, selected, intel_path, packet_ctx=packet_ctx)
    # Per-turn accounting (#1/D11) merged into the bridge's controller.turn.start:
    # what kind of prompt, fresh-vs-resume, the FULL reviewed artifact-set
    # descriptors (every embedded artifact, not just the primary path) tagged by
    # the delivery ctx_block actually used, the phase, and the context revision.
    # role/controller are set by the bridge itself. The single review_artifacts
    # value is reused across the fresh AND resume sends and all run.start/run.end
    # traces — one truthful delivery tag for the whole pass.
    meta_artifact_paths = artifact_paths or [intel_path]
    review_delivery = getattr(ctx_block, "delivery", "embedded")
    review_embedded = getattr(ctx_block, "embedded", None)
    review_artifacts = _artifact_descriptors(
        meta_artifact_paths, delivery=review_delivery, embedded=review_embedded)
    if trace:
        trace.event("review.run.start", role=reviewer_role,
                    controller=cfg["controller"], resume=bool(resume_id),
                    fresh=not bool(resume_id), prompt_kind="reviewer_pass",
                    phase=phase, epoch=epoch, context_revision=context_revision,
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
        "epoch": epoch,
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
                verdict = _controller_failure_verdict(
                    {"ok": False, "result": "probe_failed"}, alert=alert)
                if trace:
                    trace.event("review.run.end", role=reviewer_role,
                                result="probe_failed",
                                verdict=None,
                                controller_failure=True,
                                prompt_kind="reviewer_pass", phase=phase,
                                epoch=epoch, context_revision=context_revision,
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
        if session_factory:
            session = session_factory("opencode", review_io)
        else:
            session = bridge.OpencodeSession(
                prompt_path, cfg["mode"], cfg["yolo"], io_out=review_io,
                speaker=reviewer_role, internal=surface,
                resume_session_id=resume_id, on_session_id=cb, trace=trace,
                extra_writable_dir=extra_writable_dir,
                model=cfg.get("model"), effort=cfg.get("effort"))
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
        send_result = _send(session, prompt, meta=review_meta)
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
                    prompt_kind="reviewer_pass", phase=phase, epoch=epoch,
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
                    prompt_kind="reviewer_pass", phase=phase, epoch=epoch,
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


def builder_review_text(build_status_path, enabled=False):
    return "build ready for review — %s" % ui.render_path(
        build_status_path, enabled)


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


def _read_review(io_in, io_out, allow_ask=True, preview=None):
    """At ready_for_review, decide approve-vs-(ask)-vs-revise.

    With `allow_ask` (the scout intel gate and planner plan gate) a TTY shows a
    questionary select: 'Approve & finish' / 'Ask a question' / 'Request
    changes', plus a non-default 'Stop' when a `preview` is supplied. Without
    `allow_ask` (the builder build gate) a TTY with a `preview` shows a 3-way
    select — Approve & finish / Request changes / Stop — while a preview-less
    call keeps the historical binary confirm 'Approve & finish?' contract. Off a
    TTY both keep the historical blank=finish / text=revise contract (no Stop)
    so the scripted/test path is unchanged.

    When `preview` (a GatePreview) is given, every choice label carries a short,
    phase- and team-aware consequence; when None the plain labels are used.

    Returns _END to approve & finish, _STOP for the non-default Stop choice
    or a dismissed preview-enabled menu such as Ctrl-C (clean exit), the
    ('_ASK', text) marker tuple to ask a question (answered in chat without
    reopening work; only when `allow_ask`), or revision feedback."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        if not allow_ask:
            if preview is None:
                # Builder gate, preview-less: unchanged binary confirm contract.
                if ui.confirm("Approve & finish?"):
                    return _END
                while True:
                    fb = ui.prompt_user(io_in, io_out,
                                        header="Revise — your feedback")
                    if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                        return _END
                    return fb
            # Builder gate with a preview: a 3-way select so every consequence
            # is visible before selection — Approve & finish / Request changes /
            # Stop (non-default).
            while True:
                choice = ui.select(
                    "Ready for review — what now?",
                    [("approve", _preview_approve_label(preview)),
                     ("changes", _preview_changes_label(preview)),
                     ("stop", _preview_stop_label(preview))])
                if choice == "approve":
                    return _END
                if choice == "stop" or choice is None:
                    # Questionary returns None for a dismissed menu, including
                    # a single Ctrl-C.  Cancellation follows the explicit Stop
                    # path; it must never be reinterpreted as a revision.
                    return _STOP
                # 'changes': request changes, but never trap the user — a
                # blank/cancelled feedback finishes.
                fb = ui.prompt_user(io_in, io_out,
                                    header="Request changes — your feedback")
                if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                    return _END
                return fb
        approve_label = (_preview_approve_label(preview) if preview is not None
                         else "Approve & finish")
        ask_label = (_preview_ask_label(preview) if preview is not None
                     else "Ask a question")
        changes_label = (_preview_changes_label(preview) if preview is not None
                         else "Request changes")
        choices = [("approve", approve_label),
                   ("ask", ask_label),
                   ("changes", changes_label)]
        if preview is not None:
            choices.append(("stop", _preview_stop_label(preview)))
        while True:
            choice = ui.select("Ready for review — what now?", choices)
            if choice == "approve":
                return _END
            if choice == "stop" or (choice is None and preview is not None):
                # Real run_flow gates always carry a preview.  There, a
                # dismissed Questionary menu (notably Ctrl-C) is the same clean
                # non-approving outcome as the visible Stop choice.  Preserve
                # the preview-less compatibility path below.
                return _STOP
            if choice == "ask":
                q = ui.prompt_user(io_in, io_out, header="Your question")
                if q is ui.CANCEL or q is ui.EOF or q.strip() == "":
                    # Nothing typed: re-show the gate rather than approve — a
                    # blank question must never be read as a sign-off.
                    continue
                return (_ASK, q)
            # 'changes' (or a dismissed legacy preview-less select): request
            # changes, but never trap the user — blank feedback approves.
            fb = ui.prompt_user(io_in, io_out, header="Request changes — your feedback")
            if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                return _END
            return fb
    line = io_in.readline()
    if line == "" or line.strip() == "":
        return _END
    return line.rstrip("\n")


def _read_review_dissent(io_in, io_out, preview=None):
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

    Returns _END to approve & finish, _STOP for the non-default Stop choice or
    menu cancellation (clean exit), _ITERATE to hand the reviewer's unresolved
    findings back to the role, or the custom feedback text."""
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
            "Reviewer still requests changes — what now?", choices)
        if choice == "approve":
            return _END
        if choice == "stop" or (choice is None and preview is not None):
            # Questionary cancellation/Ctrl-C follows the explicit Stop path
            # on every preview-enabled real-flow gate.
            return _STOP
        if choice == "tell":
            fb = ui.prompt_user(io_in, io_out, header="Your instructions")
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


def _stuck_gate_text(status_path, role, enabled=False):
    """The banner shown at the visible stuck gate."""
    return (
        "the %s appears stuck — it reopened work but its status file did not "
        "change across an automatic repair attempt.\n  status file: %s\n"
        "choose: retry (run it once more), switch-controller (move this role "
        "to the alternate controller), inspect (show the status file), or "
        "end (end this phase cleanly)." % (role, ui.render_path(
            status_path, enabled)))


def _controller_failure_text(role, controller, reason, alert=None):
    text = (
        "the %s controller for %s cannot make progress (%s).\n"
        "choose: retry (try %s again), switch-controller (move this role to "
        "the alternate controller), or end (end this phase cleanly)."
        % (controller, role, reason, controller))
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


def _read_stuck_gate(io_in, io_out):
    """Read the stuck-gate choice. On a TTY a 3-way questionary select; off a
    TTY a readline where `retry`/`inspect` map to those actions and anything
    else (including blank/EOF) ends the phase — the safe terminating default so
    a scripted/test path is never trapped at the gate.

    Returns one of `_STUCK_RETRY`, `_STUCK_INSPECT`, `_STUCK_END`."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        choice = ui.select(
            "The role reopened work but didn't update its status — what now?",
            [("retry", "Run it once more"),
             ("switch-controller", "Move this role to the alternate controller"),
             ("inspect", "Show the status file"),
             ("end", "End this phase")])
        return {"retry": _STUCK_RETRY, "inspect": _STUCK_INSPECT,
                "switch-controller": _STUCK_SWITCH,
                "end": _STUCK_END}.get(choice, _STUCK_END)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _STUCK_RETRY
    if token in ("switch", "switch-controller"):
        return _STUCK_SWITCH
    if token == "inspect":
        return _STUCK_INSPECT
    return _STUCK_END


def _read_controller_failure_gate(io_in, io_out):
    """Read the controller-failure gate choice.

    Off a TTY, only explicit retry/switch tokens continue; blank/EOF keeps the
    historical safe terminating behavior.
    """
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        choice = ui.select(
            "The controller cannot continue — what now?",
            [("retry", "Try the same controller again"),
             ("switch-controller", "Move this role to the alternate controller"),
             ("end", "End this phase")])
        return {"retry": _CTRL_RETRY, "switch-controller": _CTRL_SWITCH,
                "end": _CTRL_END}.get(choice, _CTRL_END)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _CTRL_RETRY
    if token in ("switch", "switch-controller"):
        return _CTRL_SWITCH
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


def _reviewer_fail_gate_text(reviewer_role, role, detail=None):
    """The banner shown at the visible reviewer-failure gate."""
    text = (
        "the %s could not return a usable verdict (account limit, crash, or an "
        "empty/garbled write) across %d tries — it is not reviewing the %s's "
        "work.\nchoose: retry (run the reviewer once more), skip-review (stop "
        "reviewing for the rest of this phase and go straight to the approve/"
        "revise gate), switch-controller (move the reviewer to the alternate "
        "controller), or end (end this phase cleanly)."
        % (reviewer_role, REVIEW_FAIL_CAP, role))
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


def _read_reviewer_fail_gate(io_in, io_out):
    """Read the reviewer-failure-gate choice. On a TTY a 3-way questionary
    select; off a TTY a readline where `retry`/`end` map to those actions and
    anything else (including blank/EOF) skips the review — the safe default so a
    scripted/test path is never trapped AND a broken reviewer never blocks a
    headless run (skip-review then reaches the user gate, which off a TTY reads
    blank=approve, preserving the historical 'scripted runs complete' contract).

    Returns one of `_REVFAIL_RETRY`, `_REVFAIL_SKIP`, `_REVFAIL_END`."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        choice = ui.select(
            "The reviewer isn't returning a usable verdict — what now?",
            [("retry", "Run the reviewer once more"),
             ("skip-review", "Skip review for this phase — go to approve/revise"),
             ("switch-controller", "Move the reviewer to the alternate controller"),
             ("end", "End this phase")])
        return {"retry": _REVFAIL_RETRY, "skip-review": _REVFAIL_SKIP,
                "switch-controller": _REVFAIL_SWITCH,
                "end": _REVFAIL_END}.get(choice, _REVFAIL_SKIP)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _REVFAIL_RETRY
    if token in ("switch", "switch-controller"):
        return _REVFAIL_SWITCH
    if token == "end":
        return _REVFAIL_END
    return _REVFAIL_SKIP


def _read_handoff_confirm(io_in, io_out, prompt="Hand the work back to the scout?"):
    """The hand-back confirmation gate. On a TTY an explicit questionary
    confirm (with the role-appropriate `prompt`); off a TTY a readline where
    blank/y/yes confirms (mirrors the blank=approve contract of `_read_review`
    for the scripted/test path)."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        return ui.confirm(prompt)
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
               on_first_send_accepted=None, headless=False,
               review_allow_ask=True, gate_preview=None,
               require_pending_question=False):
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
    pending = first
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
    outcome_kind = "ended"
    payload = None
    try:
        # Controller-switch packets are controller-only recovery context.  The
        # switch itself already has a compact user-facing status line; echoing
        # this packet here would dump artifacts and orchestration markup into
        # the terminal as though the user had written it.
        internal_switch_context = context.lstrip().startswith(
            "[controller switch handoff]")
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
                if trace:
                    trace.event("status.invalidated", role=role,
                                path=status_path, changed=changed,
                                from_status="ready_for_review",
                                to_status="needs_input",
                                reason="work_reopened",
                                before_status=before_status,
                                after_status=after_status)
                pending_reopens_work = False
            pending_reopen_reason = None
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
            lead_artifacts = list(seed_artifact_paths or []) if first_send else []
            lead_artifacts.append(status_path)
            lead_meta = {
                "prompt_kind": lead_kind,
                "phase": phase,
                "fresh": first_send and not is_resume,
                "resume": is_resume or not first_send,
                "context_revision": context_revision,
                "artifacts": _artifact_descriptors(lead_artifacts,
                                                   delivery="path"),
            }
            if trace:
                trace.event("role.fingerprint.before", role=role,
                            status=fp_before["status"],
                            sha256=fp_before["sha256"],
                            size=fp_before["size"], exists=fp_before["exists"])
                trace.event("role.send.start", role=role,
                            prompt_kind=lead_kind, phase=phase,
                            fresh=lead_meta["fresh"], resume=lead_meta["resume"],
                            context_revision=context_revision,
                            artifacts=lead_meta["artifacts"],
                            **trace_store.prompt_meta(pending))
            send_result = _send(session, pending, meta=lead_meta)
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
                    outcome_kind = "ended"
                    break
                while True:
                    ui.banner(io_out, _controller_failure_text(
                        role, getattr(session, "controller", "configured"),
                        send_result.get("error_type")
                        or send_result.get("subtype")
                        or send_result.get("result") or "send failed"),
                        "dissent")
                    action = _read_controller_failure_gate(io_in, io_out)
                    if action is _CTRL_RETRY:
                        if trace:
                            trace.event("user.action", role=role,
                                        action="controller_failure_retry")
                        break
                    if action is _CTRL_SWITCH:
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
                        }
                        break
                    if trace:
                        trace.event("user.action", role=role,
                                    action="controller_failure_end")
                    outcome_kind = "ended"
                    break
                if outcome_kind in ("switch_controller", "ended"):
                    break
                continue
            if (first_send and send_result.get("ok", True)
                    and on_first_send_accepted):
                on_first_send_accepted()
                on_first_send_accepted = None
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
                    pending = _repair_prompt(artifact_noun)
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
                    outcome_kind = "ended"
                    break
                gate_decision = None
                while gate_decision is None:
                    ui.banner(io_out, _stuck_gate_text(
                        status_path, role, ui.is_tty(io_out)), "dissent")
                    action = _read_stuck_gate(io_in, io_out)
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
                    pending = _repair_prompt(artifact_noun)
                    in_repair = True  # re-checked; re-shows gate if still stuck
                    continue
                if gate_decision is _STUCK_SWITCH:
                    if trace:
                        trace.event("user.action", role=role,
                                    action="stuck_switch")
                    outcome_kind = "switch_controller"
                    payload = {
                        "role": role,
                        "reason": "stuck",
                        "pending": _repair_prompt(artifact_noun),
                        "prompt_kind": "repair",
                    }
                    break
                # _STUCK_END: end this phase cleanly, like EOF.
                if trace:
                    trace.event("user.action", role=role, action="stuck_end")
                outcome_kind = "ended"
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
                        confirmed = _read_handoff_confirm(
                            io_in, io_out, handoff_confirm_prompt)
                    if trace:
                        trace.event("handoff.gate", role=role,
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
                    if trace:
                        trace.event("status.invalidated", role=role,
                                    path=status_path, changed=changed,
                                    from_status="handoff_back",
                                    to_status="needs_input",
                                    reason="handoff_declined",
                                    before_status=decl_before,
                                    after_status=decl_after)
                    pending = handoff_declined_text_fn()
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
                                findings_count=len(verdict.get("findings") or []),
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
                            ui.banner(io_out, _reviewer_fail_gate_text(
                                reviewer_role, role,
                                verdict.get("controller_failure_alert")),
                                "dissent")
                            decision = _read_reviewer_fail_gate(io_in, io_out)
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
                            if decision is _REVFAIL_SWITCH:
                                switcher = getattr(
                                    review_fn, "switch_controller", None)
                                if switcher and switcher(
                                        reason="reviewer_failure"):
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
                            try:
                                with ui.Spinner(io_out,
                                                label="scoring this round"):
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
                        if v == "approve":
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
                                "needs_user", verdict, artifact=artifact_noun)
                            pending_reopens_work = True
                            pending_reopen_reason = "reviewer_needs_user"
                            if trace:
                                trace.event("review.handoff",
                                            from_role=reviewer_role,
                                            to_role=role, kind="needs_user")
                            review_action = "continue"
                        elif review_rounds < REVIEW_ROUND_CAP:
                            # A legitimate revise (reviewer wants changes): hand
                            # back to the role for another pass.
                            pending = assemble_reviewer_handoff(
                                "revise", verdict, artifact=artifact_noun)
                            pending_reopens_work = True
                            pending_reopen_reason = "reviewer_revise"
                            if trace:
                                trace.event("review.handoff",
                                            from_role=reviewer_role,
                                            to_role=role, kind="revise")
                            review_action = "continue"
                        else:
                            # Round cap reached on a legitimate revise: fall
                            # through to the user with the dissent attached (D5).
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
                        outcome_kind = "ended"
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
                    outcome = _read_review_dissent(io_in, io_out,
                                                   preview=gate_preview)
                else:
                    outcome = _read_review(io_in, io_out,
                                           allow_ask=review_allow_ask,
                                           preview=gate_preview)
                if outcome is _STOP:
                    # The explicit non-default Stop choice (TTY only): a clean
                    # exit — no approval, no revision turn, no done banner. This
                    # mirrors the off-TTY 'end' path, so run_flow never advances
                    # the phase and the saved (resumable) session is left intact.
                    if trace:
                        trace.event("user.action", role=role, action="stop",
                                    gate="ready_for_review")
                    outcome_kind = "ended"
                    break
                if outcome is _ITERATE:
                    # Hand the reviewer's unresolved findings straight back to
                    # the role — the user shouldn't have to retype them.
                    pending = assemble_reviewer_handoff(
                        "revise", dissent_verdict, artifact=artifact_noun)
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
                    ui.banner(io_out, done_text(
                        status_path, ui.is_tty(io_out)), "done")
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
                    pending = assemble_user_question(question_text, artifact_noun)
                    pending_user_question = True
                    if trace:
                        trace.event(
                            "user.action", role=role, action="question",
                            gate="ready_for_review",
                            **trace_store.prompt_meta(question_text,
                                                      prefix="input"))
                    continue
                pending = outcome  # revision feedback → another turn
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
                        pending = _missing_question_repair_prompt(artifact_noun)
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
                        outcome_kind = "ended"
                        break
                    if trace:
                        trace.event("headless.auto", role=role,
                                    gate="needs_input", action="nudge",
                                    nudges=headless_nudges)
                    pending = _headless_nudge_text(artifact_noun)
                    pending_reopens_work = True
                    pending_reopen_reason = "user_answer"
                    continue
                outcome = _read_turn(io_in, io_out)
                if outcome is _END:
                    if trace:
                        trace.event("user.action", role=role, action="eof")
                    break
                pending = outcome
                if trace:
                    trace.event("user.action", role=role, action="answer",
                                **trace_store.prompt_meta(outcome, prefix="input"))
                pending_reopens_work = True
                pending_reopen_reason = "user_answer"
    except KeyboardInterrupt:
        if trace:
            trace.event("role.interrupted", role=role)
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
                on_first_send_accepted=None, headless=False,
                gate_preview=None):
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
        gate_preview=gate_preview, require_pending_question=True)
    if intel_md_path:
        loop_kwargs["review_text"] = (
            lambda _p, en=False: scout_review_text(intel_md_path, en))
        loop_kwargs["done_text"] = (
            lambda _p, en=False: scout_done_text(intel_md_path, en))
    rc, outcome, payload = _role_loop(
        session, first, intel_path, context, io_in, io_out,
        on_first_send_accepted=on_first_send_accepted, **loop_kwargs)
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
                   reviewer_controller_check_fn=None):
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
    approved intel JSON embedded, read at eval time) in the planning phase.
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
    # a session_uuid and a review_packet_ctx (epoch + context_revision) are
    # present; the real runners / default run_reviewer_once accept the params,
    # test-injected runners do not (kept byte-identical).
    packet_snapshot_dir = (state_store.session_assets_dir(session_uuid)
                           if (session_uuid and review_packet_ctx) else None)
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
        # Diff-packet params (#4) ride the same surface-capable guard: the
        # default run_reviewer_once and the real runner closures accept them;
        # test-injected runners stay byte-identical (no new kwargs).
        if packet_snapshot_dir is not None and (
                reviewer_runner is None
                or getattr(runner, "_coplan_surface_capable", False)):
            kwargs["epoch"] = review_packet_ctx.get("epoch")
            kwargs["context_revision"] = review_packet_ctx.get(
                "context_revision")
            kwargs["snapshot_dir"] = packet_snapshot_dir
            kwargs["force_full_reread"] = force_full_reread
        specs = None
        if eval_enabled and evaluatee:
            specs = [{
                "evaluatee": evaluatee,
                "criteria": EVAL_CRITERIA[(reviewer_role, evaluatee)],
                "artifact_block":
                    "The verdict you just wrote for this round is your own "
                    "review file:\n  %s\nEvaluate the %s's artifact you just "
                    "reviewed:\n  %s" % (review_path, evaluatee, artifact_path),
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
                    consumed_upstream, scores_path, reviewer_role, round_index)
                if spec == "deduped":
                    holder["consumed_done"] = True
                elif spec:
                    spec = dict(spec, phase=phase, round=round_index)
                    specs.append(spec)
            kwargs["eval_scratch_path"] = eval_scratch_path
            kwargs["eval_specs"] = specs
        verdict = runner(config, runner_context, selected, artifact_path,
                         review_path, **kwargs)
        if specs:
            if len(specs) > 1:
                holder["consumed_done"] = True
            _aggregate_eval(
                eval_scratch_path, scores_path, session_uuid, reviewer_role,
                phase, round_index,
                {s["evaluatee"]: _eval_spec_stamp(s) for s in specs},
                trace=trace)
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

    def switch_review_controller(reason="reviewer_failure"):
        if not switch_controller_fn:
            return False
        ok = switch_controller_fn(reviewer_role, reason=reason, source="gate")
        if not ok:
            return False
        holder["resume_id"] = None
        holder["context_update"] = None
        holder["ack"] = None
        holder["switch_note"] = switch_note_fn(
            reviewer_role) if switch_note_fn else None
        return True

    review_fn.switch_controller = switch_review_controller

    return review_fn


def run_scout(config, context, selected, io_in=None, io_out=None,
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
              on_first_send_accepted=None,
              reviewer_controller_check_fn=None, headless=False,
              gate_preview=None):
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
    brief = assemble_scout_brief(selected, intel_path or "", intel_md_path)
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
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
        evaluate_fn = _make_evaluate_fn(
            "scout", SCOUT_REVIEWER, "scouting", eval_scratch_path,
            scores_path, session_uuid, trace=trace,
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

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting scout",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=SCOUT_PROMPT_PATH, trace=trace, role="scout",
                extra_writable_dir=sessions_dir, cache_enabled=True))
        if not ok:
            if trace:
                trace.event("role.end", role="scout", result="probe_failed")
            io_out.write("cowork: " + alert + "\n")
            io_out.flush()
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
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="scout", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start scout controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            return 1
        first = (brief + "\n\n" + context).strip()
        return _scout_loop(session, first, intel_path, context, io_in, io_out,
                           review_fn=review_fn, trace=trace,
                           on_outcome=on_outcome, evaluate_fn=evaluate_fn,
                           intel_md_path=intel_md_path,
                           skip_baseline=skip_baseline,
                           context_revision=(review_packet_ctx or {}).get(
                               "context_revision"),
                           is_resume=bool(resume_id),
                           on_first_send_accepted=on_first_send_accepted,
                           headless=headless, gate_preview=gate_preview)

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
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="scout", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start scout controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            return 1
        first = (brief + "\n\n" + context).strip()
        return _scout_loop(session, first, intel_path, context, io_in, io_out,
                           review_fn=review_fn, trace=trace,
                           on_outcome=on_outcome, evaluate_fn=evaluate_fn,
                           intel_md_path=intel_md_path,
                           skip_baseline=skip_baseline,
                           context_revision=(review_packet_ctx or {}).get(
                               "context_revision"),
                           is_resume=bool(resume_id),
                           on_first_send_accepted=on_first_send_accepted,
                           headless=headless, gate_preview=gate_preview)

    role_text = read_scout_prompt()
    prompt = assemble_codex_prompt(role_text, brief, context)
    _emit_codex_role_prompt_bytes(trace, "scout", role_text)
    if resume_id:
        io_out.write("cowork: resuming codex session %s\n" % resume_id)
    cb = (lambda i: on_session("codex", i)) if on_session else None
    if session_factory:
        session = session_factory("codex", resume_thread_id=resume_id,
                                  on_thread_id=cb)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=io_out, speaker="scout",
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
            extra_writable_dir=sessions_dir,
            model=cfg.get("model"), effort=cfg.get("effort"))
    return _scout_loop(session, prompt, intel_path, context, io_in, io_out,
                       review_fn=review_fn, trace=trace, on_outcome=on_outcome,
                       evaluate_fn=evaluate_fn, intel_md_path=intel_md_path,
                       skip_baseline=skip_baseline,
                       context_revision=(review_packet_ctx or {}).get(
                           "context_revision"),
                       is_resume=bool(resume_id),
                       on_first_send_accepted=on_first_send_accepted,
                       headless=headless, gate_preview=gate_preview)


def run_planner(config, context, selected, io_in=None, io_out=None,
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
                on_first_send_accepted=None,
                reviewer_controller_check_fn=None, headless=False,
                gate_preview=None):
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
    brief = assemble_planner_brief(plan_json_path or "", plan_md_path or "")
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
    runner = reviewer_runner or make_planning_advisor_runner(
        plan_md_path, trace=trace, extra_writable_dir=sessions_dir)
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, review_path, reviewer_runner=runner,
        reviewer_resume_id=reviewer_resume_id,
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
        evaluate_fn = _make_evaluate_fn(
            "planner", PLANNING_ADVISOR, "planning", eval_scratch_path,
            scores_path, session_uuid, intel_path=intel_path,
            planning_epoch=planning_epoch, intel_md_path=intel_md_path,
            trace=trace,
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

    def report(outcome, payload):
        if on_outcome:
            on_outcome(outcome, payload)

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
        gate_preview=gate_preview, require_pending_question=True)

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting planner",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=PLANNER_PROMPT_PATH, trace=trace,
                role="planner", extra_writable_dir=sessions_dir,
                cache_enabled=True))
        if not ok:
            if trace:
                trace.event("role.end", role="planner", result="probe_failed")
            io_out.write("cowork: " + alert + "\n")
            io_out.flush()
            report("ended", None)
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
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="planner", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start planner controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            report("ended", None)
            return 1
        first = (brief + "\n\n" + context).strip()
        rc, outcome, payload = _role_loop(
            session, first, plan_json_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted, **loop_kwargs)
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
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="planner", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start planner controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            report("ended", None)
            return 1
        first = (brief + "\n\n" + context).strip()
        rc, outcome, payload = _role_loop(
            session, first, plan_json_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted, **loop_kwargs)
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
    if session_factory:
        session = session_factory("codex", resume_thread_id=resume_id,
                                  on_thread_id=cb)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=io_out, speaker="planner",
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
            extra_writable_dir=sessions_dir,
            model=cfg.get("model"), effort=cfg.get("effort"))
    rc, outcome, payload = _role_loop(
        session, prompt, plan_json_path, context, io_in, io_out,
        on_first_send_accepted=on_first_send_accepted, **loop_kwargs)
    report(outcome, payload)
    return rc


def run_builder(config, context, selected, io_in=None, io_out=None,
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
                on_first_send_accepted=None,
                reviewer_controller_check_fn=None, headless=False,
                gate_preview=None):
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
    brief = assemble_builder_brief(build_status_path or "", build_summary_path)
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
    runner = reviewer_runner or make_build_reviewer_runner(
        plan_json_path, plan_md_path, baseline_note=baseline_note,
        baseline_repos=baseline_repos, trace=trace,
        extra_writable_dir=sessions_dir, build_summary_path=build_summary_path)
    consumed = plan_consumed_upstream(plan_json_path, plan_md_path,
                                      building_epoch)
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, build_review_path, reviewer_runner=runner,
        reviewer_resume_id=reviewer_resume_id,
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
        evaluate_fn = _make_evaluate_fn(
            "builder", BUILD_REVIEWER, "building", eval_scratch_path,
            scores_path, session_uuid, consumed_upstream=consumed, trace=trace,
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

    def report(outcome, payload):
        if on_outcome:
            on_outcome(outcome, payload)

    loop_kwargs = dict(
        role="builder", review_fn=review_fn, trace=trace,
        reviewer_role=BUILD_REVIEWER,
        needs_input_text=builder_needs_input_text,
        review_text=lambda _p, en=False: builder_review_text(
            build_surface_path or "", en),
        done_text=lambda _p, en=False: builder_done_text(
            build_surface_path or "", en),
        artifact_noun="build",
        # review_allow_ask=False removes only the "Ask a question" choice from
        # the builder gate (scoped to the scout/planner artifact gates). With a
        # gate_preview the builder gate is still the preview-enabled 3-way CLI
        # select — Approve & finish / Request changes / Stop (see _read_review);
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
        gate_preview=gate_preview, require_pending_question=True)

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting builder",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=BUILDER_PROMPT_PATH, trace=trace,
                role="builder", extra_writable_dir=sessions_dir,
                cache_enabled=True))
        if not ok:
            if trace:
                trace.event("role.end", role="builder", result="probe_failed")
            io_out.write("cowork: " + alert + "\n")
            io_out.flush()
            report("ended", None)
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
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="builder", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start builder controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            report("ended", None)
            return 1
        first = (brief + "\n\n" + context).strip()
        rc, outcome, payload = _role_loop(
            session, first, build_status_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted, **loop_kwargs)
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
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.event("role.end", role="builder", result="start_failed",
                            error_type=type(exc).__name__)
            io_out.write("cowork: failed to start builder controller: %s\n"
                         % type(exc).__name__)
            io_out.flush()
            report("ended", None)
            return 1
        first = (brief + "\n\n" + context).strip()
        rc, outcome, payload = _role_loop(
            session, first, build_status_path, context, io_in, io_out,
            on_first_send_accepted=on_first_send_accepted, **loop_kwargs)
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
    if session_factory:
        session = session_factory("codex", resume_thread_id=resume_id,
                                  on_thread_id=cb)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=io_out, speaker="builder",
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
            extra_writable_dir=sessions_dir,
            model=cfg.get("model"), effort=cfg.get("effort"))
    rc, outcome, payload = _role_loop(
        session, prompt, build_status_path, context, io_in, io_out,
        on_first_send_accepted=on_first_send_accepted, **loop_kwargs)
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
SessionChoice = collections.namedtuple(
    "SessionChoice", ["path", "new_uuid", "cancelled", "error"])
SessionChoice.__new__.__defaults__ = (None, None, False, None)


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

    if args.switch_controller:
        if args.no_session:
            return SessionChoice(
                error="--switch-controller cannot be combined with --no-session "
                      "(it must update an existing saved session).")
        if args.new:
            return SessionChoice(
                error="--switch-controller cannot be combined with --new "
                      "(it must update an existing saved session).")
        if args.team:
            return SessionChoice(
                error="--switch-controller cannot be combined with --team "
                      "(it reuses the saved team).")
        if args.config:
            return SessionChoice(
                error="--switch-controller cannot be combined with --config "
                      "(it reuses the saved role config).")

        cwd = os.getcwd()
        interactive_picker_ok = ui.is_tty(io_in) and ui.is_tty(io_out)
        if args.session_file:
            if not os.path.exists(args.session_file):
                return SessionChoice(
                    error="--switch-controller: session file does not exist: %s"
                          % args.session_file)
            return SessionChoice(path=args.session_file)

        discovered = state_store.list_sessions(cwd)
        if not discovered:
            return SessionChoice(
                error="--switch-controller: no saved sessions found in %s."
                      % state_store.session_dir(cwd))

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
                    error="--resume with --switch-controller needs an "
                          "interactive terminal; use --session-file instead.")
            return run_picker()
        if len(discovered) == 1:
            return SessionChoice(path=discovered[0]["path"])
        if interactive_picker_ok:
            return run_picker()
        return SessionChoice(
            error="--switch-controller found multiple saved sessions; pass "
                  "--session-file to choose one.")

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
    choice = select_fn("Resume an existing session or start a new one?",
                       [("resume", "Resume an existing session"),
                        ("new", "Start a new session")])
    if choice == "resume":
        return run_picker()
    if choice == "new":
        return mint_new()
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
    """Fallback target when a switch is requested without an explicit target.
    claude <-> codex stay a toggle; opencode falls back to claude."""
    return "codex" if controller == "claude" else "claude"


def validate_switch_role(role, target, phase, selected, state):
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
    return None


def switch_handoff_packet(role, phase, pending_switch, artifact_paths=None,
                          shared_context="", pending_turn=None):
    """Fresh-provider handoff text prepended to a switched role/reviewer."""
    if not pending_switch:
        return ""
    from_controller = pending_switch.get("from_controller") or "unknown"
    to_controller = pending_switch.get("to_controller") or "unknown"
    lines = [
        "[controller switch handoff]",
        "You are continuing an existing cowork %s phase as %s." % (phase, role),
        "Controller switched: %s -> %s." % (from_controller, to_controller),
        "This is a fresh %s provider conversation. Hidden chat history from %s "
        "is not available; cowork-visible session state, artifacts, shared "
        "context, and the working tree continue." % (to_controller, from_controller),
    ]
    if pending_switch.get("reason"):
        lines.append("Switch reason: %s." % pending_switch.get("reason"))
    if pending_switch.get("source"):
        lines.append("Switch source: %s." % pending_switch.get("source"))
    if shared_context:
        lines.extend(["", "<shared_context>", shared_context.strip(),
                      "</shared_context>"])
    for path in artifact_paths or []:
        if not path:
            continue
        lines.extend(["", "<artifact path=%r>" % path, _read_text(path),
                      "</artifact>"])
    if pending_turn:
        lines.extend([
            "",
            "<failed_pending_turn>",
            pending_turn.strip(),
            "</failed_pending_turn>",
            "Process the failed pending turn above after orienting yourself.",
        ])
    return "\n".join(lines).strip()


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
    if args.switch_controller and session_enabled:
        reason = None
        message = None
        if saved is None:
            reason = "switch_controller_unloadable_session"
            message = (
                "--switch-controller: session file is not a loadable cowork "
                "session: %s" % spath)
        elif not state_store.has_config(saved):
            reason = "switch_controller_missing_config"
            message = (
                "--switch-controller requires a saved session with saved "
                "team/config.")
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

    def check_controller_tool(controller):
        return preflight.check_tools(
            [controller], which=which if which is not None else shutil.which)

    def reviewer_controller_check(role):
        if role not in config:
            return None
        controller = config[role].get("controller")
        ok, alerts = check_controller_tool(controller)
        if ok:
            return None
        trace.event("review.controller_preflight_failed", role=role,
                    phase=phase, controller=controller,
                    alerts_count=len(alerts))
        return alerts

    def switch_controller(role, reason=None, target=None, source="gate"):
        if role not in config:
            io_out.write("cowork: cannot switch %s — role is not configured.\n"
                         % role)
            return False
        current = config[role].get("controller")
        target = target or alternate_controller(current)
        trace.event("controller.switch.request", role=role, phase=phase,
                    source=source, reason=reason, from_controller=current,
                    to_controller=target)
        if target == current:
            io_out.write("cowork: %s is already using %s.\n" % (role, target))
            trace.event("controller.switch.end", role=role, phase=phase,
                        result="already_current", controller=target)
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
        entry = {
            "from_controller": current,
            "to_controller": target,
            "reason": reason,
            "source": source,
            "created": time.time(),
        }
        if session_enabled:
            holder["state"] = state_store.switch_role_controller(
                spath, role, target, prior=holder["state"], reason=reason,
                source=source, created=entry["created"])
            # Keep the in-memory config in lockstep with the saved config.
            config[role] = dict(holder["state"]["config"][role])
        else:
            config[role] = dict(config[role], controller=target)
            pending_switches[role] = entry
        local_ids.pop(role, None)
        trace.event("controller.switch.commit", role=role, phase=phase,
                    source=source, reason=reason, from_controller=current,
                    to_controller=target)
        io_out.write("cowork: switched %s controller %s -> %s\n"
                     % (role, current, target))
        io_out.flush()
        return True

    def ensure_controller_available(role, reason="launch"):
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
            ui.banner(io_out, _controller_failure_text(
                role, controller, "missing executable", alert), "dissent")
            action = _read_controller_failure_gate(io_in, io_out)
            if action is _CTRL_RETRY:
                trace.event("user.action", role=role,
                            action="controller_failure_retry",
                            reason=reason)
                continue
            if action is _CTRL_SWITCH:
                trace.event("user.action", role=role,
                            action="controller_failure_switch",
                            reason=reason)
                if switch_controller(role, reason="missing_executable",
                                     source="gate"):
                    return True
                continue
            trace.event("user.action", role=role,
                        action="controller_failure_end", reason=reason)
            return False

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
            ui.banner(io_out, _controller_failure_text(
                role, controller, reason, alert), "dissent")
            action = _read_controller_failure_gate(io_in, io_out)
            if action is _CTRL_RETRY:
                trace.event("user.action", role=role,
                            action="controller_failure_retry",
                            reason=reason)
                return "retry"
            if action is _CTRL_SWITCH:
                trace.event("user.action", role=role,
                            action="controller_failure_switch",
                            reason=reason)
                if switch_controller(role, reason=reason, source="gate"):
                    return "switch"
                continue
            trace.event("user.action", role=role,
                        action="controller_failure_end", reason=reason)
            return "end"

    def switch_arg_error():
        if not args.switch_controller:
            return None
        role, target = args.switch_controller
        return validate_switch_role(role, target, phase, selected, holder["state"])

    err = switch_arg_error()
    if err:
        trace.event("run.end", rc=2, reason="switch_controller_validation")
        io_out.write("cowork: " + err + "\n")
        return 2
    if args.switch_controller:
        role, target = args.switch_controller
        if not switch_controller(role, reason="cli", target=target, source="cli"):
            trace.event("run.end", rc=1, reason="switch_controller_failed")
            return 1

    # Resolved BEFORE the context step so we can skip the goal prompt on a
    # resume of the current phase's user-facing role.
    lead_role = PHASE_LEADS[phase]
    lead_resume_id = role_resume_id(lead_role)
    if lead_resume_id:
        trace.event("run.resume", role=lead_role,
                    controller=config[lead_role]["controller"],
                    session_id=lead_resume_id, phase=phase)

    # Step 3: context. On a resume, skip the goal prompt and auto-continue.
    context = resolve_context(
        args, resuming=bool(lead_resume_id) or bool(args.switch_controller))

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
        body = (seed or "").strip()
        return (HEADLESS_LEAD_NOTE + "\n\n" + body) if body \
            else HEADLESS_LEAD_NOTE

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
        block = context_update_block(gap)
        seed = (seed or "").strip()
        if not seed or seed == gap.strip():
            return block
        return block + "\n\n" + seed

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

    def set_phase(new_phase):
        if session_enabled:
            holder["state"] = state_store.save_phase(
                spath, new_phase, prior=holder["state"])
        trace.event("phase.change", context_revision=current_rev,
                    **{"from": phase, "to": new_phase})
        return new_phase

    # All per-session produced artifacts live under the session-assets home
    # (~/.cowork/sessions/<uuid>/, COWORK_SESSIONS_ROOT-overridable), joining
    # the trace and scores already kept there; only .cowork/session.json stays
    # project-local as the per-directory anchor. Create the home up front so the
    # agent CLIs (which write their own artifacts) always have a target dir.
    intel_dir = state_store.session_assets_dir(session_uuid)
    os.makedirs(intel_dir, exist_ok=True)
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
            wt_cfg = {"controller": getattr(args, "wt_controller", "claude"),
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
            trace.event("worktree.created", path=wt_path, branch=wt_branch)
        # Redirect the rest of the session into the worktree: every spawned CLI
        # uses cwd=os.getcwd() (cowork_bridge), and run_cwd drives discovery and
        # the build baseline.
        os.chdir(wt_path)
        run_cwd = wt_path
        trace.event("worktree.redirect", cwd=wt_path)
        io_out.write("cowork: running inside worktree %s (branch %s)\n"
                     % (wt_path, wt_branch))
        io_out.flush()

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
        return switch_handoff_packet(
            role, phase, pending_switch_for(role),
            artifact_paths=switch_artifacts_for(role),
            shared_context=shared_context,
            pending_turn=pending_switch_turns.get(role))

    def seed_with_switch_note(role, seed):
        note = switch_note_for(role)
        if not note:
            return seed
        seed = (seed or "").strip()
        return (note + "\n\n" + seed) if seed else note

    def prepare_fresh_seed_after_switch(role):
        if role == "scout":
            return with_discovery(shared_context)
        if role == "planner":
            return assemble_planner_seed(intel_path, shared_context)
        if role == "builder":
            return assemble_builder_seed(
                plan_json_path, plan_md_path, shared_context)
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

    def bump_planning_epoch():
        if session_enabled:
            holder["state"] = state_store.bump_planning_epoch(
                spath, prior=holder["state"])
            epoch_box["epoch"] = state_store.get_planning_epoch(
                holder["state"])
        else:
            epoch_box["epoch"] += 1

    def bump_building_epoch():
        if session_enabled:
            holder["state"] = state_store.bump_building_epoch(
                spath, prior=holder["state"])
            building_epoch_box["epoch"] = state_store.get_building_epoch(
                holder["state"])
        else:
            building_epoch_box["epoch"] += 1

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
                    trace.event("build.baseline", repo=path, head=head,
                                dirty=bool(dirty))
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

    def with_discovery(seed):
        # Prepend the discovery note to EVERY scout seed — fresh, plain resume,
        # and hand-back re-run alike — so the discover-and-confirm responsibility
        # is present on every cycle. The note is a standing reminder, not a new
        # task, so a plain auto-continue resume still carries no new goal (the
        # note alone, never a re-injected user goal). An empty seed collapses to
        # the note alone — no trailing blank lines.
        seed = (seed or "").strip()
        return (repo_discovery_note + "\n\n" + seed) if seed else repo_discovery_note

    scout_seed = with_discovery(context)
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
            planner_seed = assemble_planner_seed(intel_path, shared_context)
    elif phase == "building":
        # Resuming into the building phase. A saved builder session continues
        # with the (possibly new) context; a building phase persisted WITHOUT a
        # builder session id (killed between save_phase and the id save) must
        # start a fresh builder from the approved plan, not from a bare context.
        if role_resume_id("builder"):
            builder_seed = context
        else:
            builder_seed = assemble_builder_seed(
                plan_json_path, plan_md_path, shared_context)
    while True:
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
            rc = run_scout_fn(
                config,
                with_headless_lead(seed_with_switch_note(
                    "scout", deliver_context("scout", scout_seed))),
                selected,
                io_in=io_in, io_out=io_out,
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
                                   "context_revision": current_rev},
                switch_controller_fn=switch_controller,
                reviewer_switch_note_fn=switch_note_for,
                on_reviewer_switch_consumed=clear_pending_switch_for,
                on_first_send_accepted=(
                    (lambda: clear_pending_switch_for("scout"))
                    if pending_switch_for("scout") else None),
                reviewer_controller_check_fn=reviewer_controller_check,
                headless=headless,
                gate_preview=make_gate_preview(
                    "scout", planner_on_team, session_enabled),
                on_outcome=lambda o, p=None: outcome_box.update(
                    outcome=o, payload=p))
            if rc != 0:
                action = recover_controller_failure("scout", "startup_or_probe")
                if action == "retry":
                    continue
                if action == "switch":
                    scout_seed = prepare_fresh_seed_after_switch("scout")
                    continue
                break
            if (rc == 0 and outcome_box["outcome"] == "switch_controller"):
                payload = outcome_box["payload"] or {}
                if payload.get("pending"):
                    pending_switch_turns["scout"] = payload.get("pending")
                if switch_controller("scout", reason=payload.get("reason"),
                                     source="gate"):
                    scout_seed = prepare_fresh_seed_after_switch("scout")
                    continue
                rc = 1
                break
            if rc == 0:
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
                        intel_path, shared_context)
                continue
            break

        if phase == "planning":
            if not ensure_controller_available("planner", reason="lead_launch"):
                rc = 1
                break
            planner_box = {"outcome": None, "payload": None}
            rc = run_planner_fn(
                config,
                with_headless_lead(seed_with_switch_note(
                    "planner",
                    deliver_context(
                        "planner",
                        planner_seed if planner_seed is not None else ""))),
                selected, io_in=io_in, io_out=io_out,
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
                                   "context_revision": current_rev},
                switch_controller_fn=switch_controller,
                reviewer_switch_note_fn=switch_note_for,
                on_reviewer_switch_consumed=clear_pending_switch_for,
                on_first_send_accepted=(
                    (lambda: clear_pending_switch_for("planner"))
                    if pending_switch_for("planner") else None),
                reviewer_controller_check_fn=reviewer_controller_check,
                headless=headless,
                gate_preview=make_gate_preview(
                    "planner", builder_on_team, session_enabled),
                on_outcome=lambda o, p: planner_box.update(outcome=o, payload=p))
            if rc != 0:
                action = recover_controller_failure("planner", "startup_or_probe")
                if action == "retry":
                    continue
                if action == "switch":
                    planner_seed = prepare_fresh_seed_after_switch("planner")
                    continue
                break
            if (rc == 0 and planner_box["outcome"] == "switch_controller"):
                payload = planner_box["payload"] or {}
                if payload.get("pending"):
                    pending_switch_turns["planner"] = payload.get("pending")
                if switch_controller("planner", reason=payload.get("reason"),
                                     source="gate"):
                    planner_seed = prepare_fresh_seed_after_switch("planner")
                    continue
                rc = 1
                break
            if rc == 0:
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
                    handoff_wake_block(planner_box["payload"]))
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
                        plan_json_path, plan_md_path, shared_context)
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
        rc = run_builder_fn(
            config,
            with_headless_lead(seed_with_switch_note(
                "builder",
                deliver_context("builder",
                                builder_seed if builder_seed is not None else ""))),
            selected, io_in=io_in, io_out=io_out,
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
                               "context_revision": current_rev},
            switch_controller_fn=switch_controller,
            reviewer_switch_note_fn=switch_note_for,
            on_reviewer_switch_consumed=clear_pending_switch_for,
            on_first_send_accepted=(
                (lambda: clear_pending_switch_for("builder"))
                if pending_switch_for("builder") else None),
            reviewer_controller_check_fn=reviewer_controller_check,
            headless=headless,
            gate_preview=make_gate_preview(
                "builder", builder_on_team, session_enabled),
            on_outcome=lambda o, p: builder_box.update(outcome=o, payload=p))
        if rc != 0:
            action = recover_controller_failure("builder", "startup_or_probe")
            if action == "retry":
                continue
            if action == "switch":
                builder_seed = prepare_fresh_seed_after_switch("builder")
                continue
            break
        if (rc == 0 and builder_box["outcome"] == "switch_controller"):
            payload = builder_box["payload"] or {}
            if payload.get("pending"):
                pending_switch_turns["builder"] = payload.get("pending")
            if switch_controller("builder", reason=payload.get("reason"),
                                 source="gate"):
                builder_seed = prepare_fresh_seed_after_switch("builder")
                continue
            rc = 1
            break
        if rc == 0:
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
            planner_seed = plan_handback_wake_block(builder_box["payload"])
            builder_seed = None
            continue
        # Build approval is terminal for this run (the phase stays `building`,
        # so a rerun resumes the builder conversation), and EOF/interrupt ends
        # the run the same way.
        break

    trace.event("run.end", rc=rc)
    return rc


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.switch_controller and args.check:
            sys.stderr.write(
                "cowork: --switch-controller cannot be combined with --check.\n")
            return 2
        if args.switch_controller and args.report:
            sys.stderr.write(
                "cowork: --switch-controller cannot be combined with --report.\n")
            return 2
        if args.check:
            return preflight.main()
        if args.report:
            return run_report(args)
        return run_flow(args)
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
