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

Additive to the co-plan skill: new file, stdlib only, Python 3.9+, does not
import or modify co_plan_file.py.
"""

import argparse
import collections
import contextlib
import datetime
import glob
import hashlib
import json
import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_bridge as bridge  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
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

# Per-role defaults (controller, yolo, mode), all roles checked by default.
# Roles default to implement mode (write-enabled) and are kept in their lane by
# role-spec guardrails, not by plan mode.
DEFAULTS = {
    "scout": {"controller": "claude", "yolo": True, "mode": "implement"},
    SCOUT_REVIEWER: {"controller": "codex", "yolo": True, "mode": "implement"},
    "planner": {"controller": "claude", "yolo": True, "mode": "implement"},
    PLANNING_ADVISOR: {"controller": "codex", "yolo": True, "mode": "implement"},
    "builder": {"controller": "claude", "yolo": True, "mode": "implement"},
    BUILD_REVIEWER: {"controller": "codex", "yolo": True, "mode": "implement"},
}


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


def apply_config_override(config, role, tokens):
    """Apply tokens (controller/yolo/no-yolo/plan/implement) to one role.
    Returns (ok, error_or_None). Mutates config."""
    if role not in config:
        return False, "unknown or unselected role: %r" % role
    cfg = config[role]
    for token in tokens:
        if token in ("claude", "codex"):
            cfg["controller"] = token
        elif token == "yolo":
            cfg["yolo"] = True
        elif token == "no-yolo":
            cfg["yolo"] = False
        elif token in ("plan", "implement"):
            cfg["mode"] = token
        else:
            return False, "unknown option: %r" % token
    return True, None


def format_config_summary(config, header="Tool config:"):
    """Aligned per-role summary with a column header row."""
    labels = ("role", "controller", "permissions", "mode")
    rows = [
        (role, config[role]["controller"],
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


def configure_roles_interactive(selected, select_fn=None, checkbox_fn=None):
    """Step 2 via questionary. A fast path accepts the defaults (shown as the
    summary); otherwise pick roles to customize and choose controller/yolo/mode."""
    select_fn = select_fn or _q_select
    checkbox_fn = checkbox_fn or _q_checkbox
    config = default_config(selected)
    summary = format_config_summary(config, header="Default tool config:")
    choice = select_fn(["use these defaults", "customize"],
                       default="use these defaults", message=summary)
    if choice != "customize":
        return config
    to_customize = checkbox_fn("Customize which roles?", selected) or []
    for role in selected:
        if role not in to_customize:
            continue
        cfg = config[role]
        cfg["controller"] = select_fn(["claude", "codex"],
                                      default=cfg["controller"],
                                      message=role + " controller")
        yolo = select_fn(["yolo", "no-yolo"],
                         default="yolo" if cfg["yolo"] else "no-yolo",
                         message=role + " permissions")
        cfg["yolo"] = (yolo == "yolo")
        cfg["mode"] = select_fn(["plan", "implement"], default=cfg["mode"],
                                message=role + " mode")
    return config


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


def build_parser():
    p = argparse.ArgumentParser(prog="cowork", add_help=True)
    p.add_argument("--check", action="store_true",
                   help="run the preflight dependency check only")
    p.add_argument("--team",
                   help="comma-separated roles, e.g. scout,planner "
                        "(non-interactive)")
    p.add_argument("--config", action="append", default=[],
                   metavar="ROLE=opt,opt",
                   help="per-role override, e.g. scout=codex,no-yolo,implement "
                        "(repeatable)")
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
    return p


def _is_non_interactive(args):
    return bool(args.team or args.config or args.context is not None
                or args.context_file)


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

    Mirrors `co_plan_file.dependency_status()['caveman']['available']` WITHOUT
    importing co_plan_file: cowork must stay additive (the boundary is enforced
    by `test_cowork_does_not_import_co_plan_file` and the module docstring), so
    the small detector is duplicated here rather than imported. Cheap — a few
    `shutil.which` lookups plus path-existence checks — and run at brief
    assembly, i.e. effectively at session start."""
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


def assemble_scout_brief(selected, intel_path, caveman_available=None):
    """Dynamic first-message brief for the scout: where to write, the JSON +
    domain guardrail, and the plan-only fallthrough for this team."""
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
    return (
        "Write your findings as a single JSON object to exactly this file:\n"
        "  %s\n"
        "That intel file is your ONLY write target. Do not create, edit, or "
        "delete any other file (reading/searching the repo is fine).\n"
        "%s\n\n%s" % (intel_path, plan_note, caveman_directive(caveman_available))
    )


def read_scout_prompt(path=SCOUT_PROMPT_PATH):
    with open(path, "r") as fh:
        return fh.read()


def assemble_codex_prompt(role_text, team_note, context):
    return "\n\n".join([role_text.strip(), team_note.strip(), context.strip()]).strip()


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


def read_scout_reviewer_prompt(path=SCOUT_REVIEWER_PROMPT_PATH):
    with open(path, "r") as fh:
        return fh.read()


def assemble_reviewer_brief(review_path, protected="the scout intel file",
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


def assemble_reviewer_context(context, selected, intel_path):
    """The reviewer's situational context: the SAME initial `context` the scout
    received, the team framing, and the scout's current intel JSON to review.

    Deliberately excludes the scout's write-target `brief` / `first` payload —
    that carries the scout's own guardrail and would mis-instruct the reviewer."""
    intel_text = _read_text(intel_path)
    team = ", ".join(selected) if selected else "(unspecified)"
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
    artifact file is missing. The embedded evidence is the concatenation of
    the consumed artifact files, read at eval time, so the prompt is
    self-contained."""
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
    spec = {
        "evaluatee": evaluatee,
        "criteria": EVAL_CRITERIA[(evaluator, evaluatee)],
        "artifact_block": consumed["embed"] % text,
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


def _scout_consumed_upstream(intel_path, planning_epoch):
    """The consumed-upstream descriptor for the planning phase: the planner
    and planning-advisor scoring the approved scout intel once per phase. A
    literal re-statement of the behavior that used to be hard-coded, so the
    planning-phase eval flow is unchanged."""
    if intel_path is None:
        return None
    return {
        "role": "scout",
        "label": "scout intel",
        "artifact_paths": [intel_path],
        "epoch_field": "planning_epoch",
        "epoch_value": planning_epoch,
        "context": "consumed-intel",
        "embed": "The approved scout intel JSON this phase consumed:\n%s",
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
    stamped = []
    for entry in entries:
        entry = dict(entry)
        entry["evaluator"] = evaluator
        entry["phase"] = phase
        entry["round"] = round_index
        entry["context"] = "review-round"
        entry.update(stamp_by_evaluatee.get(entry.get("evaluatee")) or {})
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
                      consumed_upstream=None, trace=None):
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
        consumed_upstream = _scout_consumed_upstream(intel_path, planning_epoch)
    consumed_done = {"done": consumed_upstream is None}

    def evaluate_fn(session, verdict, round_index):
        # The scratch is per-turn output, not durable state: clear any prior
        # round's file BEFORE the send so a turn that writes nothing yields
        # 'no entry', never a re-read of the previous round's scores.
        try:
            os.remove(scratch_path)
            if trace:
                trace.event("eval.scratch.cleared", role=role,
                            path=scratch_path)
        except OSError:
            pass
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
        with _muted_session(session):
            session.send(prompt)
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


def assemble_reviewer_resume_context(intel_path, context_update=None):
    """Lighter context for a RESUMED reviewer session: its thread already holds
    the role + the prior context, so only the updated intel is sent — plus a
    context-update wake block when the session context changed since the
    reviewer last acknowledged it."""
    body = (
        "The scout has updated its intel since your last review. Re-review the "
        "current intel below against the current task context, and write your "
        "verdict to the review file again:\n%s" % _read_text(intel_path).strip()
    )
    if context_update:
        return context_update_block(context_update) + "\n\n" + body
    return body


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
    """The fresh planner's situational context: the approved scout intel
    (verbatim JSON) plus the current shared session context."""
    return (
        "The scout phase is complete and the user APPROVED the scout intel "
        "below. Digest it and drive the planning conversation.\n\n"
        "Approved scout intel:\n%s\n\n"
        "Current shared context:\n%s" % (_read_text(intel_path).strip(),
                                         (context or "").strip())
    )


def intel_updated_block(intel_path):
    """Wake block for a resumed planner after a hand-back round trip: the scout
    re-ran its full cycle and the user approved the UPDATED intel."""
    return (
        "The scout intel changed since you started planning: your hand-back was "
        "executed, the scout re-investigated, and the user approved the updated "
        "intel below. Digest it and continue planning. Keep prior plan content "
        "only where it remains compatible.\n\n"
        "Updated approved intel:\n%s" % _read_text(intel_path).strip()
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


def assemble_builder_brief(build_status_path, caveman_available=None):
    """The builder's status-file instruction. Unlike the scout/planner, the
    builder's write target is the WHOLE REPO (it edits source to execute the
    plan); the status file named here is only its status/verification channel,
    not a write restriction."""
    return (
        "Write and keep current your status as a single JSON object to exactly "
        "this file:\n  %s\n"
        "That status file is your status + verification channel (status, "
        "handoff, and the result.verification log) — NOT a restriction on what "
        "you may edit. You execute the approved plan by editing the repository "
        "itself. Do NOT run any git commit or PR/branch tooling: approval ends "
        "the run and leaves the changes in the working tree for the user.\n\n%s"
        % (build_status_path, caveman_directive(caveman_available))
    )


def assemble_builder_seed(plan_json_path, plan_md_path, context):
    """The fresh builder's situational context: the approved plan (verbatim
    JSON + markdown) plus the current shared session context."""
    return (
        "The planning phase is complete and the user APPROVED the plan below. "
        "Execute it: make the code changes, verify them, and drive the build "
        "conversation.\n\n"
        "Approved plan JSON (the machine source of truth):\n%s\n\n"
        "Approved plan markdown (the human-readable summary):\n%s\n\n"
        "Current shared context:\n%s"
        % (_read_text(plan_json_path).strip(), _read_text(plan_md_path).strip(),
           (context or "").strip())
    )


def plan_updated_block(plan_json_path, plan_md_path):
    """Wake block for a resumed builder after a hand-back round trip: the
    builder handed back to the planner, the planner re-planned, and the user
    approved the UPDATED plan."""
    return (
        "The plan changed since you started building: your hand-back was "
        "executed, the planner re-planned, and the user approved the UPDATED "
        "plan below. Digest the changes and continue building. Keep prior work "
        "only where it remains compatible.\n\n"
        "Updated approved plan JSON:\n%s\n\n"
        "Updated approved plan markdown:\n%s"
        % (_read_text(plan_json_path).strip(), _read_text(plan_md_path).strip())
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


def assemble_advisor_context(context, selected, plan_json_path, plan_md_path):
    """The planning-advisor's situational context: the shared session context,
    the team framing, and BOTH planner artifacts to review."""
    team = ", ".join(selected) if selected else "(unspecified)"
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
                                    context_update=None):
    """Lighter context for a RESUMED planning-advisor session: only the updated
    plan artifacts — plus a context-update wake block when the session context
    changed since the advisor last acknowledged it."""
    body = (
        "The planner has updated its plan since your last review. Re-review "
        "both current artifacts below against the current task context, and "
        "write your verdict to the review file again.\n\n"
        "Current plan JSON:\n%s\n\n"
        "Current plan markdown:\n%s"
        % (_read_text(plan_json_path).strip(), _read_text(plan_md_path).strip())
    )
    if context_update:
        return context_update_block(context_update) + "\n\n" + body
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
               eval_scratch_path=None, eval_specs=None, surface_io_out=None):
        return run_reviewer_once(
            config, context, selected, plan_json_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            reviewer_role=PLANNING_ADVISOR,
            prompt_path=PLANNING_ADVISOR_PROMPT_PATH,
            protected="the planner's plan files",
            context_fn=lambda ctx, sel, p: assemble_advisor_context(
                ctx, sel, p, plan_md_path),
            resume_context_fn=lambda p, context_update=None:
                assemble_advisor_resume_context(
                    p, plan_md_path, context_update=context_update))
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


def assemble_build_reviewer_context(context, selected, plan_json_path,
                                    plan_md_path, build_status_path,
                                    baseline_note="", baseline_repos=None):
    """The build-reviewer's situational context: the shared session context,
    the team framing, BOTH plan artifacts, the builder's status JSON, and the
    full-delta capture recipe (the delta is NOT embedded — a stale snapshot
    would mis-review). `baseline_repos` is the explicit selected repo-root list
    (each ``{path, has_head}``) that drives the per-root capture recipe."""
    team = ", ".join(selected) if selected else "(unspecified)"
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
        % (context.strip(), team, _read_text(plan_json_path).strip(),
           _read_text(plan_md_path).strip(),
           _read_text(build_status_path).strip(),
           _build_diff_recipe(baseline_repos, baseline_note)))


def assemble_build_reviewer_resume_context(plan_json_path, plan_md_path,
                                           build_status_path,
                                           context_update=None,
                                           baseline_note="",
                                           baseline_repos=None):
    """Lighter context for a RESUMED build-reviewer session: its thread already
    holds the role + the prior context, so only the updated artifacts are sent
    — plus a context-update wake block when the session context changed since
    the reviewer last acknowledged it. The full delta is still read live."""
    body = (
        "The builder has updated its work since your last review. Re-review "
        "the current full working-tree delta against the plan and the "
        "builder's current status below, and write your verdict to the review "
        "file again.\n\n"
        "Current plan JSON:\n%s\n\n"
        "Current plan markdown:\n%s\n\n"
        "Current builder status JSON:\n%s\n\n"
        "%s"
        % (_read_text(plan_json_path).strip(),
           _read_text(plan_md_path).strip(),
           _read_text(build_status_path).strip(),
           _build_diff_recipe(baseline_repos, baseline_note)))
    if context_update:
        return context_update_block(context_update) + "\n\n" + body
    return body


def make_build_reviewer_runner(plan_json_path, plan_md_path, baseline_note="",
                               baseline_repos=None, trace=None,
                               extra_writable_dir=None):
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
               eval_scratch_path=None, eval_specs=None, surface_io_out=None):
        return run_reviewer_once(
            config, context, selected, build_status_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            extra_writable_dir=extra_writable_dir, surface_io_out=surface_io_out,
            reviewer_role=BUILD_REVIEWER,
            prompt_path=BUILD_REVIEWER_PROMPT_PATH,
            protected="the builder's working-tree delta and status file",
            context_fn=lambda ctx, sel, p: assemble_build_reviewer_context(
                ctx, sel, plan_json_path, plan_md_path, p,
                baseline_note=baseline_note, baseline_repos=baseline_repos),
            resume_context_fn=lambda p, context_update=None:
                assemble_build_reviewer_resume_context(
                    plan_json_path, plan_md_path, p,
                    context_update=context_update, baseline_note=baseline_note,
                    baseline_repos=baseline_repos))
    # See make_planning_advisor_runner: marks a real surface-capable closure.
    runner._coplan_surface_capable = True
    return runner


def _run_reviewer_eval(session, reviewer_role, eval_scratch_path, eval_specs,
                       trace=None):
    """Send the reviewer its private evaluation turn on the still-open session
    (after its verdict was read back, before close — no resume round-trip).

    The reviewer already streams to a quiet sink, so no muting wrapper is
    needed. Failures are traced and swallowed: the eval is observational and
    must never affect the verdict."""
    if not (eval_specs and eval_scratch_path):
        return
    # Per-turn output, not durable state: clear any prior round's scratch
    # BEFORE the send (mirrors the review-file clearing above).
    try:
        os.remove(eval_scratch_path)
        if trace:
            trace.event("eval.scratch.cleared", role=reviewer_role,
                        path=eval_scratch_path)
    except OSError:
        pass
    if trace:
        trace.event("eval.request", evaluator=reviewer_role,
                    evaluatees=[s.get("evaluatee") for s in eval_specs],
                    phase=eval_specs[0].get("phase"),
                    round=eval_specs[0].get("round"))
    try:
        session.send(assemble_eval_prompt(
            reviewer_role, eval_scratch_path, eval_specs))
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
                      extra_writable_dir=None, surface_io_out=None):
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
    if trace:
        trace.event("review.run.start", role=reviewer_role,
                    controller=cfg["controller"], resume=bool(resume_id),
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
    if resume_id:
        ctx_block = (resume_context_fn or assemble_reviewer_resume_context)(
            intel_path, context_update=context_update)
    else:
        ctx_block = (context_fn or assemble_reviewer_context)(
            context, selected, intel_path)

    if cfg["controller"] == "claude":
        cb = (lambda i: on_session("claude", i)) if on_session else None
        if session_factory:
            session = session_factory("claude", review_io)
        elif resume_id:
            session = bridge.ClaudeSession(
                prompt_path, cfg["mode"], cfg["yolo"],
                io_out=review_io, speaker=reviewer_role, internal=surface,
                resume_id=resume_id, on_session_id=cb, trace=trace,
                extra_writable_dir=extra_writable_dir)
        else:
            spawn = claude_spawn or bridge._real_claude_spawn
            ok, _alert = bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=prompt_path, trace=trace,
                role=reviewer_role, extra_writable_dir=extra_writable_dir)
            if not ok:
                verdict = state_store.read_review(review_path)
                if trace:
                    trace.event("review.run.end", role=reviewer_role,
                                result="probe_failed",
                                verdict=(verdict or {}).get("verdict"))
                return verdict
            # Pin a known id up front so it is resumable even if killed early.
            sid = str(uuid.uuid4())
            if on_session:
                on_session("claude", sid)
            session = bridge.ClaudeSession(
                prompt_path, cfg["mode"], cfg["yolo"],
                io_out=review_io, speaker=reviewer_role, internal=surface,
                session_id=sid, on_session_id=cb, trace=trace,
                extra_writable_dir=extra_writable_dir)
        first = (brief + "\n\n" + ctx_block).strip()
        try:
            session.send(first)
            verdict = state_store.read_review(review_path)
            # The eval send must never reach the user even when the review turn
            # is surfaced: mute the session around it (D-eval-stays-muted).
            with _muted_session(session) if surface else contextlib.nullcontext():
                _run_reviewer_eval(session, reviewer_role, eval_scratch_path,
                                   eval_specs, trace=trace)
        finally:
            session.close()
        if trace:
            trace.event("review.run.end", role=reviewer_role,
                        result="ok", verdict=(verdict or {}).get("verdict"),
                        malformed=bool((verdict or {}).get("malformed")))
        return verdict

    # codex (default)
    cb = (lambda i: on_session("codex", i)) if on_session else None
    if resume_id:
        prompt = (brief + "\n\n" + ctx_block).strip()  # thread already has role
    else:
        prompt = assemble_codex_prompt(_read_text(prompt_path), brief, ctx_block)
    if session_factory:
        session = session_factory("codex", review_io)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=review_io, speaker=reviewer_role,
            internal=surface, resume_thread_id=resume_id, on_thread_id=cb,
            trace=trace, extra_writable_dir=extra_writable_dir)
    try:
        session.send(prompt)
        verdict = state_store.read_review(review_path)
        # Keep the eval send muted even when the review turn is surfaced.
        with _muted_session(session) if surface else contextlib.nullcontext():
            _run_reviewer_eval(session, reviewer_role, eval_scratch_path,
                               eval_specs, trace=trace)
    finally:
        session.close()
    if trace:
        trace.event("review.run.end", role=reviewer_role, result="ok",
                    verdict=(verdict or {}).get("verdict"),
                    malformed=bool((verdict or {}).get("malformed")))
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


def builder_start_text(build_status_path, resuming=False, enabled=False):
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
    return head + "\nstatus → %s" % ui.render_path(build_status_path, enabled)


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


# Returned by the turn readers to mean "end the conversation" (EOF / Ctrl-D /
# explicit /quit), distinct from a blank line (which re-prompts).
_END = object()

# Returned by the dissent review gate to mean "hand the reviewer's unresolved
# findings back to the role for another pass" — the user opted to keep
# iterating without writing their own feedback.
_ITERATE = object()


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


def _read_review(io_in, io_out):
    """At ready_for_review, decide approve-vs-revise. On a TTY this is an explicit
    questionary confirm (#8); off a TTY it keeps the historical blank=finish /
    text=revise contract so the scripted/test path is unchanged. Returns _END to
    approve & finish, or the revision feedback text."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        if ui.confirm("Approve & finish?"):
            return _END
        while True:
            fb = ui.prompt_user(io_in, io_out, header="Revise — your feedback")
            if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                # Nothing to revise with: treat as approve so the user is never
                # trapped at the gate.
                return _END
            return fb
    line = io_in.readline()
    if line == "" or line.strip() == "":
        return _END
    return line.rstrip("\n")


def _read_review_dissent(io_in, io_out):
    """The `ready_for_review` gate when the reviewer's round cap was exhausted
    without approval. On a TTY a 3-way questionary select — the safe default
    (Enter) keeps iterating on the reviewer's feedback, so unresolved dissent is
    never approved by accident. 'Tell it what to do' prompts for custom
    instructions; blank/dismissed input also falls back to iterating, never
    approval. Off a TTY it keeps the historical blank=finish / text=revise
    contract so the scripted/test path is unchanged.

    Returns _END to approve & finish, _ITERATE to hand the reviewer's unresolved
    findings back to the role, or the custom feedback text."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        choice = ui.select(
            "Reviewer still requests changes — what now?",
            [("iterate", "Keep iterating on the reviewer's feedback"),
             ("tell", "Tell it what to do"),
             ("approve", "Approve & finish anyway")])
        if choice == "approve":
            return _END
        if choice == "tell":
            fb = ui.prompt_user(io_in, io_out, header="Your instructions")
            if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                return _ITERATE
            return fb
        # 'iterate' or a dismissed select: the safe non-approving default.
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


def _stuck_gate_text(status_path, role, enabled=False):
    """The banner shown at the visible stuck gate."""
    return (
        "the %s appears stuck — it reopened work but its status file did not "
        "change across an automatic repair attempt.\n  status file: %s\n"
        "choose: retry (run it once more), inspect (show the status file), or "
        "end (end this phase cleanly)." % (role, ui.render_path(
            status_path, enabled)))


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
             ("inspect", "Show the status file"),
             ("end", "End this phase")])
        return {"retry": _STUCK_RETRY, "inspect": _STUCK_INSPECT,
                "end": _STUCK_END}.get(choice, _STUCK_END)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _STUCK_RETRY
    if token == "inspect":
        return _STUCK_INSPECT
    return _STUCK_END


# Returned by the reviewer-failure gate reader (the visible escalation shown when
# the paired reviewer/advisor fails to return a usable verdict REVIEW_FAIL_CAP
# times running — an account limit, a crash, or an empty/garbled write — distinct
# from a reviewer that legitimately keeps asking for changes, which the
# REVIEW_ROUND_CAP dissent path already handles).
_REVFAIL_RETRY = object()
_REVFAIL_SKIP = object()
_REVFAIL_END = object()


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


def _reviewer_fail_gate_text(reviewer_role, role):
    """The banner shown at the visible reviewer-failure gate."""
    return (
        "the %s could not return a usable verdict (account limit, crash, or an "
        "empty/garbled write) across %d tries — it is not reviewing the %s's "
        "work.\nchoose: retry (run the reviewer once more), skip-review (stop "
        "reviewing for the rest of this phase and go straight to the approve/"
        "revise gate), or end (end this phase cleanly)."
        % (reviewer_role, REVIEW_FAIL_CAP, role))


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
             ("end", "End this phase")])
        return {"retry": _REVFAIL_RETRY, "skip-review": _REVFAIL_SKIP,
                "end": _REVFAIL_END}.get(choice, _REVFAIL_SKIP)
    line = io_in.readline()
    token = line.strip().lower()
    if token == "retry":
        return _REVFAIL_RETRY
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
               evaluate_fn=None):
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
    outcome_kind = "ended"
    payload = None
    try:
        if context.strip():
            io_out.write(ui.label("you", ui.is_tty(io_out)) + context.strip() + "\n")
            io_out.flush()
        while True:
            # Capture the reopen signal BEFORE the invalidate/reset block runs.
            reopened_this_turn = pending_reopen_reason is not None
            reopen_reason_this_turn = pending_reopen_reason
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
            if trace:
                trace.event("role.fingerprint.before", role=role,
                            status=fp_before["status"],
                            sha256=fp_before["sha256"],
                            size=fp_before["size"], exists=fp_before["exists"])
                trace.event("role.send.start", role=role,
                            **trace_store.prompt_meta(pending))
            session.send(pending)
            if trace:
                trace.event("role.send.end", role=role)
            fp_after = state_store.fingerprint_status(status_path)
            if trace:
                trace.event("role.fingerprint.after", role=role,
                            status=fp_after["status"], sha256=fp_after["sha256"],
                            size=fp_after["size"], exists=fp_after["exists"])
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
            if handoff_enabled and status == "handoff_back":
                note = state_store.read_handoff(status_path)
                if trace:
                    trace.event("handoff.signal", role=role, path=status_path,
                                has_payload=bool(note))
                if note:
                    ui.banner(io_out, handoff_gate_text_fn(note), "review")
                    if handoff_confirm:
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
                # Reviewer gate (topology D): runs transparently before the user.
                # `skip_review` (latched at the reviewer-failure gate) bypasses it
                # for the rest of the phase, straight to the user gate.
                if review_fn is not None and not skip_review and \
                        review_rounds < REVIEW_ROUND_CAP:
                    review_rounds += 1
                    if trace:
                        trace.event("review.round.start", role=reviewer_role,
                                    round=review_rounds,
                                    round_cap=REVIEW_ROUND_CAP)
                    # None: fall through to the user gate this round.
                    # "continue"/"end": act on the OUTER loop after the inner one.
                    review_action = None
                    # Inner loop so a reviewer-failure RETRY (and the one silent
                    # auto-retry) re-runs the reviewer in place — same round, no
                    # bounce through the role.
                    while True:
                        # The review turn streams on the internal channel (the
                        # bridge raises its own pre-first-token spinner on io_out);
                        # no outer \r-frame spinner here — it would collide with the
                        # Live region the bridge opens on the same io_out. The muted
                        # probe/eval inside the pass need no visible spinner.
                        verdict = review_fn(status_path, review_rounds) or {}
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
                                continue
                            ui.banner(io_out, _reviewer_fail_gate_text(
                                reviewer_role, role), "dissent")
                            decision = _read_reviewer_fail_gate(io_in, io_out)
                            if decision is _REVFAIL_RETRY:
                                # Re-run the reviewer, SAME round, counter kept —
                                # re-shows the gate if it fails again.
                                if trace:
                                    trace.event("user.action", role=role,
                                                action="review_fail_retry")
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
                        if v == "approve":
                            # Only an explicit approve reaches the user gate.
                            review_rounds = 0
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
                if dissent:
                    outcome = _read_review_dissent(io_in, io_out)
                else:
                    outcome = _read_review(io_in, io_out)
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
                    if trace:
                        trace.event("gate.show", role=role,
                                    gate="needs_input", path=status_path)
                    ui.banner(io_out, needs_input_text(), "needs_input")
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
                evaluate_fn=None):
    """The scout instantiation of `_role_loop` (kept as the historical entry
    point). Returns 0; the loop outcome is reported via `on_outcome` so
    `run_flow` can chain into the planning phase on approval."""
    rc, outcome, _payload = _role_loop(
        session, first, intel_path, context, io_in, io_out,
        role="scout", review_fn=review_fn, trace=trace,
        reviewer_role=SCOUT_REVIEWER, evaluate_fn=evaluate_fn)
    if on_outcome:
        on_outcome(outcome)
    return rc


def make_review_fn(config, context, selected, review_path, reviewer_runner=None,
                   reviewer_resume_id=None, on_reviewer_session=None,
                   context_update=None, on_context_ack=None, trace=None,
                   reviewer_role=SCOUT_REVIEWER, phase=None,
                   eval_scratch_path=None, scores_path=None,
                   session_uuid=None, intel_path=None, planning_epoch=None,
                   consumed_upstream=None, extra_writable_dir=None,
                   surface_io_out=None):
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
    if consumed_upstream is None:
        consumed_upstream = _scout_consumed_upstream(intel_path, planning_epoch)
    holder = {"resume_id": reviewer_resume_id,
              "context_update": context_update,
              "ack": on_context_ack,
              "consumed_done": consumed_upstream is None}

    def review_fn(artifact_path, round_index):
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
        verdict = runner(config, context, selected, artifact_path, review_path,
                         **kwargs)
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
        return verdict

    return review_fn


def run_scout(config, context, selected, io_in=None, io_out=None,
              claude_spawn=None, resume_id=None, on_session=None,
              intel_path=None, session_factory=None, review_path=None,
              reviewer_runner=None, reviewer_resume_id=None,
              on_reviewer_session=None, reviewer_context=None,
              reviewer_context_update=None, on_reviewer_context_ack=None,
              trace=None, on_outcome=None,
              eval_scratch_path=None, reviewer_eval_scratch_path=None,
              scores_path=None, session_uuid=None):
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
    brief = assemble_scout_brief(selected, intel_path or "")
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, review_path, reviewer_runner=reviewer_runner,
        reviewer_resume_id=reviewer_resume_id,
        on_reviewer_session=on_reviewer_session,
        context_update=reviewer_context_update,
        trace=trace, phase="scouting",
        on_context_ack=on_reviewer_context_ack,
        eval_scratch_path=reviewer_eval_scratch_path,
        scores_path=scores_path, session_uuid=session_uuid,
        extra_writable_dir=sessions_dir, surface_io_out=io_out)
    evaluate_fn = None
    if review_fn is not None:
        evaluate_fn = _make_evaluate_fn(
            "scout", SCOUT_REVIEWER, "scouting", eval_scratch_path,
            scores_path, session_uuid, trace=trace)
    if resume_id and not context.strip():
        context = "Continue the session."
    if trace:
        trace.event("role.start", role="scout", controller=cfg["controller"],
                    resume=bool(resume_id), intel_path=intel_path,
                    review_path=review_path)
    ui.banner(io_out, scout_start_text(intel_path or "", resuming=bool(resume_id),
                                       enabled=ui.is_tty(io_out)), "start")
    io_out.flush()

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting scout",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=SCOUT_PROMPT_PATH, trace=trace, role="scout",
                extra_writable_dir=sessions_dir))
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
        if session_factory:
            session = session_factory("claude", session_id=session_id,
                                      resume_id=rid, on_session_id=cb)
        else:
            session = bridge.ClaudeSession(
                SCOUT_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                speaker="scout", session_id=session_id, resume_id=rid,
                on_session_id=cb, trace=trace, extra_writable_dir=sessions_dir)
        first = (brief + "\n\n" + context).strip()
        return _scout_loop(session, first, intel_path, context, io_in, io_out,
                           review_fn=review_fn, trace=trace,
                           on_outcome=on_outcome, evaluate_fn=evaluate_fn)

    role_text = read_scout_prompt()
    prompt = assemble_codex_prompt(role_text, brief, context)
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
            extra_writable_dir=sessions_dir)
    return _scout_loop(session, prompt, intel_path, context, io_in, io_out,
                       review_fn=review_fn, trace=trace, on_outcome=on_outcome,
                       evaluate_fn=evaluate_fn)


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
                planning_epoch=None):
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
        extra_writable_dir=sessions_dir, surface_io_out=io_out)
    evaluate_fn = None
    if review_fn is not None:
        evaluate_fn = _make_evaluate_fn(
            "planner", PLANNING_ADVISOR, "planning", eval_scratch_path,
            scores_path, session_uuid, intel_path=intel_path,
            planning_epoch=planning_epoch, trace=trace)
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
        evaluate_fn=evaluate_fn)

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting planner",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=PLANNER_PROMPT_PATH, trace=trace,
                role="planner", extra_writable_dir=sessions_dir))
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
        if session_factory:
            session = session_factory("claude", session_id=session_id,
                                      resume_id=rid, on_session_id=cb)
        else:
            session = bridge.ClaudeSession(
                PLANNER_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                speaker="planner", session_id=session_id, resume_id=rid,
                on_session_id=cb, trace=trace, extra_writable_dir=sessions_dir)
        first = (brief + "\n\n" + context).strip()
        rc, outcome, payload = _role_loop(
            session, first, plan_json_path, context, io_in, io_out,
            **loop_kwargs)
        report(outcome, payload)
        return rc

    role_text = _read_text(PLANNER_PROMPT_PATH)
    prompt = assemble_codex_prompt(role_text, brief, context)
    if resume_id:
        io_out.write("cowork: resuming codex session %s\n" % resume_id)
        prompt = (brief + "\n\n" + context).strip()  # thread already has role
    cb = (lambda i: on_session("codex", i)) if on_session else None
    if session_factory:
        session = session_factory("codex", resume_thread_id=resume_id,
                                  on_thread_id=cb)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=io_out, speaker="planner",
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
            extra_writable_dir=sessions_dir)
    rc, outcome, payload = _role_loop(
        session, prompt, plan_json_path, context, io_in, io_out, **loop_kwargs)
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
                baseline_repos=None):
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
    brief = assemble_builder_brief(build_status_path or "")
    # Writable root granted to the agent CLIs so a no-yolo role can write its
    # relocated session artifacts (which live outside cwd).
    sessions_dir = (state_store.session_assets_dir(session_uuid)
                    if session_uuid else None)
    runner = reviewer_runner or make_build_reviewer_runner(
        plan_json_path, plan_md_path, baseline_note=baseline_note,
        baseline_repos=baseline_repos, trace=trace,
        extra_writable_dir=sessions_dir)
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
        surface_io_out=io_out)
    evaluate_fn = None
    if review_fn is not None:
        evaluate_fn = _make_evaluate_fn(
            "builder", BUILD_REVIEWER, "building", eval_scratch_path,
            scores_path, session_uuid, consumed_upstream=consumed, trace=trace)
    if resume_id and not context.strip():
        context = "Continue the session."
    if trace:
        trace.event("role.start", role="builder", controller=cfg["controller"],
                    resume=bool(resume_id), build_status_path=build_status_path,
                    review_path=build_review_path)
    ui.banner(io_out, builder_start_text(build_status_path or "",
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
            build_status_path or "", en),
        done_text=lambda _p, en=False: builder_done_text(
            build_status_path or "", en),
        artifact_noun="build",
        handoff_enabled=True, handoff_confirm=handoff_confirm,
        handoff_gate_text_fn=builder_handoff_gate_text,
        handoff_confirm_prompt="Hand the work back to the planner?",
        handoff_declined_text_fn=handoff_declined_to_planner_text,
        evaluate_fn=evaluate_fn)

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = _with_status_spinner(
            io_out, "starting builder",
            lambda: bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=BUILDER_PROMPT_PATH, trace=trace,
                role="builder", extra_writable_dir=sessions_dir))
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
        if session_factory:
            session = session_factory("claude", session_id=session_id,
                                      resume_id=rid, on_session_id=cb)
        else:
            session = bridge.ClaudeSession(
                BUILDER_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                speaker="builder", session_id=session_id, resume_id=rid,
                on_session_id=cb, trace=trace, extra_writable_dir=sessions_dir)
        first = (brief + "\n\n" + context).strip()
        rc, outcome, payload = _role_loop(
            session, first, build_status_path, context, io_in, io_out,
            **loop_kwargs)
        report(outcome, payload)
        return rc

    role_text = _read_text(BUILDER_PROMPT_PATH)
    prompt = assemble_codex_prompt(role_text, brief, context)
    if resume_id:
        io_out.write("cowork: resuming codex session %s\n" % resume_id)
        prompt = (brief + "\n\n" + context).strip()  # thread already has role
    cb = (lambda i: on_session("codex", i)) if on_session else None
    if session_factory:
        session = session_factory("codex", resume_thread_id=resume_id,
                                  on_thread_id=cb)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=io_out, speaker="builder",
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace,
            extra_writable_dir=sessions_dir)
    rc, outcome, payload = _role_loop(
        session, prompt, build_status_path, context, io_in, io_out,
        **loop_kwargs)
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


def run_flow(args, io_in=None, io_out=None, which=None, run_scout_fn=None,
             run_planner_fn=None, run_builder_fn=None):
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    run_scout_fn = run_scout_fn or run_scout
    run_planner_fn = run_planner_fn or run_planner
    run_builder_fn = run_builder_fn or run_builder
    interactive = not _is_non_interactive(args)
    # The builder and reviewer CLI sessions spawn in the process cwd, so their
    # `git diff` is relative to cwd — NOT to the session-file parent (which may
    # live outside the repo when --session-file points elsewhere). The build
    # baseline must be read from the same cwd to match what they see.
    run_cwd = os.getcwd()

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

    # Step 1: team.
    if args.team:
        selected, err = parse_team(args.team)
        if err:
            trace.event("run.end", rc=2, reason="parse_team_error")
            io_out.write("cowork: " + err + "\n")
            return 2
    elif reuse_config:
        selected = [r for r in ROLES if r in saved["team"]]
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
        config = {r: dict(saved["config"][r]) for r in selected
                  if r in saved["config"]}
        io_out.write("cowork: using saved session config (%s)\n" % spath)
    elif interactive:
        config = configure_roles_interactive(selected)
    trace.event("run.config", selected=selected, reuse_config=reuse_config,
                config={r: dict(config[r]) for r in selected if r in config})

    # Persist team + config the first time (or whenever freshly chosen).
    if session_enabled and not reuse_config:
        saved = state_store.save_config(spath, selected, config, prior=saved or {})

    # Preflight (rich/prompt_toolkit/questionary required only for interactive use).
    kwargs = {"interactive": interactive}
    if which is not None:
        kwargs["which"] = which
    ok, alerts = preflight.preflight(config, **kwargs)
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
    phase = state_store.get_phase(saved) if session_enabled else "scouting"
    planner_on_team = "planner" in selected
    builder_on_team = "builder" in selected
    if phase == "building" and not builder_on_team:
        phase = "planning"
    if phase == "planning" and not planner_on_team:
        phase = "scouting"
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

    # Resolved BEFORE the context step so we can skip the goal prompt on a
    # resume of the current phase's user-facing role.
    lead_role = {"scouting": "scout", "planning": "planner",
                 "building": "builder"}[phase]
    lead_resume_id = role_resume_id(lead_role)
    if lead_resume_id:
        trace.event("run.resume", role=lead_role,
                    controller=config[lead_role]["controller"],
                    session_id=lead_resume_id, phase=phase)

    # Step 3: context. On a resume, skip the goal prompt and auto-continue.
    context = resolve_context(args, resuming=bool(lead_resume_id))

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
            trace.event("context.saved", source="input")
        state = holder["state"]
        current_text = state_store.get_context(state) or ""
        current_rev = state_store.get_context_revision(state)
        trace.event("context.current", revision=current_rev,
                    has_context=bool(current_text),
                    context_sha256=(state.get("context") or {}).get("hash")
                    if isinstance(state.get("context"), dict) else None)

    shared_context = (current_text or context) if session_enabled else context

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
                    delivered=True, reason="phase_invocation")
        block = context_update_block(gap)
        seed = (seed or "").strip()
        if not seed or seed == gap.strip():
            return block
        return block + "\n\n" + seed

    def reviewer_gap(reviewer_role):
        """The context-update wake block for a RESUMED paired reviewer that has
        not acknowledged the current revision, else None."""
        if not session_enabled or not role_resume_id(reviewer_role):
            return None
        gap = state_store.role_context_gap(holder["state"], reviewer_role)
        trace.event("context.gap", role=reviewer_role, revision=current_rev,
                    delivered=bool(gap), reason="reviewer_resume")
        return gap

    def context_acker(role):
        if not session_enabled:
            return None

        def ack():
            holder["state"] = state_store.mark_context_seen(
                spath, role, current_rev, prior=holder["state"])
            trace.event("context.ack", role=role, revision=current_rev)
        return ack

    def ack_lead(role):
        # The lead role received the current context in its prompt this run;
        # record the acknowledgment after a successful run (a crash leaves it
        # unacknowledged, so the next resume re-delivers the wake block — the
        # safe direction).
        if session_enabled and current_rev:
            holder["state"] = state_store.mark_context_seen(
                spath, role, current_rev, prior=holder["state"])
            trace.event("context.ack", role=role, revision=current_rev)

    def set_phase(new_phase):
        if session_enabled:
            holder["state"] = state_store.save_phase(
                spath, new_phase, prior=holder["state"])
        trace.event("phase.change", **{"from": phase, "to": new_phase})
        return new_phase

    # All per-session produced artifacts live under the session-assets home
    # (~/.cowork/sessions/<uuid>/, COWORK_SESSIONS_ROOT-overridable), joining
    # the trace and scores already kept there; only .cowork/session.json stays
    # project-local as the per-directory anchor. Create the home up front so the
    # agent CLIs (which write their own artifacts) always have a target dir.
    intel_dir = state_store.session_assets_dir(session_uuid)
    os.makedirs(intel_dir, exist_ok=True)
    intel_path = scout_intel_path(intel_dir, session_uuid)
    review_path = state_store.review_path_for(intel_dir, session_uuid)
    plan_json_path = state_store.planner_plan_json_path_for(intel_dir, session_uuid)
    plan_md_path = state_store.planner_plan_md_path_for(intel_dir, session_uuid)
    planner_review_path = state_store.planner_review_path_for(
        intel_dir, session_uuid)
    build_status_path = state_store.build_status_path_for(
        intel_dir, session_uuid)
    build_review_path = state_store.build_review_path_for(
        intel_dir, session_uuid)
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
            outcome_box = {"outcome": None}
            rc = run_scout_fn(
                config, deliver_context("scout", scout_seed), selected,
                io_in=io_in, io_out=io_out,
                resume_id=role_resume_id("scout"),
                on_session=role_saver("scout"),
                intel_path=intel_path, review_path=review_path,
                reviewer_resume_id=role_resume_id(SCOUT_REVIEWER),
                on_reviewer_session=role_saver(SCOUT_REVIEWER),
                reviewer_context=shared_context,
                reviewer_context_update=reviewer_gap(SCOUT_REVIEWER)
                if SCOUT_REVIEWER in selected else None,
                on_reviewer_context_ack=context_acker(SCOUT_REVIEWER),
                trace=trace,
                eval_scratch_path=eval_scratch["scout"],
                reviewer_eval_scratch_path=eval_scratch[SCOUT_REVIEWER],
                scores_path=scores_path, session_uuid=session_uuid,
                on_outcome=lambda o: outcome_box.update(outcome=o))
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
            planner_box = {"outcome": None, "payload": None}
            rc = run_planner_fn(
                config,
                deliver_context(
                    "planner",
                    planner_seed if planner_seed is not None else ""),
                selected, io_in=io_in, io_out=io_out,
                resume_id=role_resume_id("planner"),
                on_session=role_saver("planner"),
                plan_json_path=plan_json_path, plan_md_path=plan_md_path,
                review_path=planner_review_path,
                reviewer_resume_id=role_resume_id(PLANNING_ADVISOR),
                on_reviewer_session=role_saver(PLANNING_ADVISOR),
                reviewer_context=shared_context,
                reviewer_context_update=reviewer_gap(PLANNING_ADVISOR)
                if PLANNING_ADVISOR in selected else None,
                on_reviewer_context_ack=context_acker(PLANNING_ADVISOR),
                trace=trace,
                eval_scratch_path=eval_scratch["planner"],
                reviewer_eval_scratch_path=eval_scratch[PLANNING_ADVISOR],
                scores_path=scores_path, session_uuid=session_uuid,
                intel_path=intel_path, planning_epoch=epoch_box["epoch"],
                on_outcome=lambda o, p: planner_box.update(outcome=o, payload=p))
            if rc == 0:
                ack_lead("planner")
            if rc == 0 and planner_box["outcome"] == "handoff":
                # User-confirmed hand-back (planner -> its pre-processor):
                # resume the scout session with the handoff payload and run the
                # full scout cycle again.
                phase = set_phase("scouting")
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
        builder_box = {"outcome": None, "payload": None}
        rc = run_builder_fn(
            config,
            deliver_context("builder",
                            builder_seed if builder_seed is not None else ""),
            selected, io_in=io_in, io_out=io_out,
            resume_id=role_resume_id("builder"),
            on_session=role_saver("builder"),
            build_status_path=build_status_path,
            build_review_path=build_review_path,
            reviewer_resume_id=role_resume_id(BUILD_REVIEWER),
            on_reviewer_session=role_saver(BUILD_REVIEWER),
            reviewer_context=shared_context,
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
            on_outcome=lambda o, p: builder_box.update(outcome=o, payload=p))
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
        if args.check:
            return preflight.main()
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
