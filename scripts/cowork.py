#!/usr/bin/env python3
"""cowork: multi-role CLI orchestration entry flow + the scouting and planning
phases.

The 3-step entry flow (team checklist, per-role tool config, initial context),
the preflight dependency check, and a phase loop that drives the user-facing
roles by spawning the selected CLI and bridging it to the user: the `scout`
(paired with the `scout-reviewer`) gathers context; on intel approval the
`planner` (paired with the `planning-advisor`) turns it into a plan, with a
user-confirmed hand-back from planner to scout. Remaining roles
(revisor/builder) are out of scope here.

Selection uses questionary for real interactive checkbox/choice menus. A
non-interactive args path (--team/--config/--context) skips the menus entirely
so the flow is testable and scriptable.

Additive to the co-plan skill: new file, stdlib only, Python 3.9+, does not
import or modify co_plan_file.py.
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import sys
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

# Max reviewer<->role review rounds per `ready_for_review` (D5). After this many
# reviewer passes without approval, cowork falls through to the user review gate
# with the reviewer's last dissent attached. Never hard-blocks. Shared by the
# scout-reviewer and the planning-advisor.
REVIEW_ROUND_CAP = 5

# Role order matches the user's vision: context-gather, scout-reviewer,
# plan-revisor, planner, planning-advisor, implementer. `scout`/`scout-reviewer`
# (the scouting phase) and `planner`/`planning-advisor` (the planning phase) are
# implemented now.
#
# `scout-reviewer` and `planning-advisor` are critical reviewers paired with
# their user-facing role DURING that role's session (deterministically invoked
# when the role sets `ready_for_review`); they are distinct from `revisor`, the
# planned SEQUENTIAL plan-revisor that would run after the scout.
SCOUT_REVIEWER = "scout-reviewer"
PLANNING_ADVISOR = "planning-advisor"
ROLES = ["scout", SCOUT_REVIEWER, "revisor", "planner", PLANNING_ADVISOR,
         "builder"]

# Hand-back contract: a user-facing role may set `status: "handoff_back"` (plus
# a `handoff` payload) in its status file to hand the work back to its
# pre-processor through a user-confirmed gate. The contract is role-generic;
# only planner -> scout is wired this iteration.
HANDBACK_PREPROCESSOR = {"planner": "scout"}

# Per-role defaults (controller, yolo, mode), all roles checked by default.
# Roles default to implement mode (write-enabled) and are kept in their lane by
# role-spec guardrails, not by plan mode.
DEFAULTS = {
    "scout": {"controller": "claude", "yolo": True, "mode": "implement"},
    SCOUT_REVIEWER: {"controller": "codex", "yolo": True, "mode": "implement"},
    "revisor": {"controller": "codex", "yolo": True, "mode": "implement"},
    "planner": {"controller": "claude", "yolo": True, "mode": "implement"},
    PLANNING_ADVISOR: {"controller": "codex", "yolo": True, "mode": "implement"},
    "builder": {"controller": "claude", "yolo": True, "mode": "implement"},
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
    return os.path.join(intel_dir, "scout.intel.%s.json" % session_uuid)


def assemble_scout_brief(selected, intel_path):
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
        "%s" % (intel_path, plan_note)
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


def assemble_reviewer_brief(review_path, protected="the scout intel file"):
    """The reviewer's write-target instruction — its analogue of the scout brief.
    It points at the review file only (never the reviewed artifact, named by
    `protected`)."""
    return (
        "Write your verdict as a single JSON object to exactly this file:\n"
        "  %s\n"
        "That review file is your ONLY write target. Do NOT edit %s "
        "or any other file (reading/searching the repo is fine). Use the "
        "verdict schema from your role (verdict: approve|revise|needs_user, "
        "findings, and user_question when needs_user)." % (review_path, protected)
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
}

# Which user-facing role a paired reviewer evaluates on its eval turn.
_REVIEWER_EVALUATEE = {SCOUT_REVIEWER: "scout", PLANNING_ADVISOR: "planner"}


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


def _eval_spec_stamp(spec):
    """The orchestrator-stamped fields one eval spec contributes to its
    aggregate entry: the context, plus — on consumed-intel specs — the
    planning epoch (it scopes the once-per-phase dedupe: a hand-back round
    trip bumps it even when the re-approved intel is byte-identical) and the
    approved-intel hash (provenance: which intel revision was scored)."""
    stamp = {"context": spec.get("context") or "review-round"}
    if spec.get("planning_epoch") is not None:
        stamp["planning_epoch"] = spec["planning_epoch"]
    if spec.get("intel_sha256"):
        stamp["intel_sha256"] = spec["intel_sha256"]
    return stamp


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
                      trace=None):
    """Build the role-side `evaluate_fn(session, verdict, round_index)` for
    `_role_loop`, or None when eval is not wired (missing paths).

    The closure sends the eval prompt on the role's own persistent session
    with its output muted (the eval is private), reads the role's scratch
    back, and aggregates. The verdict JSON is always embedded — on approve the
    findings never reach the role via the reviewer handoff, so embedding keeps
    the prompt self-contained for every verdict kind. In the planning phase
    the FIRST eval turn additionally bundles the ->scout eval with the
    approved intel JSON embedded (the once-per-phase consumed-intel eval)."""
    if not (scratch_path and scores_path and session_uuid):
        return None
    scout_evaled = {"done": phase != "planning"}

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
        # The ->scout bundle rides only the FIRST eval turn of the phase
        # (round_index == 1, per the plan): intel that appears mid-cycle
        # waits for the next round-1 turn.
        if (not scout_evaled["done"] and round_index == 1 and intel_path
                and os.path.exists(intel_path)
                and (role, "scout") in EVAL_CRITERIA):
            # Once per phase survives a resume/restart: the in-memory flag
            # only covers this closure, so the aggregate itself is the
            # durable record — scoped by the planning epoch, which bumps on
            # every scouting -> planning transition, so a hand-back round
            # trip (a new planning phase) is evaluated again even when the
            # re-approved intel is byte-identical.
            intel_text = _read_text(intel_path)
            if state_store.has_eval_entry(scores_path, role, "scout",
                                          "consumed-intel",
                                          planning_epoch=planning_epoch):
                scout_evaled["done"] = True
            else:
                specs.append({
                    "evaluatee": "scout",
                    "criteria": EVAL_CRITERIA[(role, "scout")],
                    "artifact_block":
                        "The approved scout intel JSON this phase consumed:"
                        "\n%s" % intel_text.strip(),
                    "context": "consumed-intel",
                    "planning_epoch": planning_epoch,
                    "intel_sha256": _intel_sha256(intel_text),
                })
        if trace:
            trace.event("eval.request", evaluator=role,
                        evaluatees=[s["evaluatee"] for s in specs],
                        phase=phase, round=round_index)
        prompt = assemble_eval_prompt(role, scratch_path, specs)
        with _muted_session(session):
            session.send(prompt)
        if len(specs) > 1:
            scout_evaled["done"] = True
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


def assemble_planner_brief(plan_json_path, plan_md_path):
    """The planner's write-target instruction — its analogue of the scout brief.
    It names BOTH plan artifacts and nothing else."""
    return (
        "Write your plan as TWO files, to exactly these paths:\n"
        "  JSON (machine deliverable + your status channel): %s\n"
        "  Markdown (the user's review surface, small scannable sections): %s\n"
        "Those two plan files are your ONLY write targets. Do not create, edit, "
        "or delete any other file (reading/searching the repo is fine)."
        % (plan_json_path, plan_md_path)
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


def make_planning_advisor_runner(plan_md_path, trace=None):
    """Build the real (non-test) reviewer runner for the planning phase: a
    `run_reviewer_once` closure carrying the advisor role, prompt, and the
    dual-artifact context assemblers."""
    def runner(config, context, selected, plan_json_path, review_path,
               resume_id=None, on_session=None, context_update=None,
               eval_scratch_path=None, eval_specs=None):
        return run_reviewer_once(
            config, context, selected, plan_json_path, review_path,
            resume_id=resume_id, on_session=on_session,
            context_update=context_update, trace=trace,
            eval_scratch_path=eval_scratch_path, eval_specs=eval_specs,
            reviewer_role=PLANNING_ADVISOR,
            prompt_path=PLANNING_ADVISOR_PROMPT_PATH,
            protected="the planner's plan files",
            context_fn=lambda ctx, sel, p: assemble_advisor_context(
                ctx, sel, p, plan_md_path),
            resume_context_fn=lambda p, context_update=None:
                assemble_advisor_resume_context(
                    p, plan_md_path, context_update=context_update))
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
                      eval_scratch_path=None, eval_specs=None):
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
            session = session_factory("claude", quiet)
        elif resume_id:
            session = bridge.ClaudeSession(
                prompt_path, cfg["mode"], cfg["yolo"],
                io_out=quiet, speaker=reviewer_role,
                resume_id=resume_id, on_session_id=cb, trace=trace)
        else:
            spawn = claude_spawn or bridge._real_claude_spawn
            ok, _alert = bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=prompt_path, trace=trace,
                role=reviewer_role)
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
                io_out=quiet, speaker=reviewer_role,
                session_id=sid, on_session_id=cb, trace=trace)
        first = (brief + "\n\n" + ctx_block).strip()
        try:
            session.send(first)
            verdict = state_store.read_review(review_path)
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
        session = session_factory("codex", quiet)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=quiet, speaker=reviewer_role,
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace)
    try:
        session.send(prompt)
        verdict = state_store.read_review(review_path)
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


def scout_start_text(intel_path, resuming=False):
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
    return head + "\nintel → %s" % intel_path


def scout_needs_input_text():
    return "scout needs your input"


def scout_review_text(intel_path):
    return "scout intel ready for review — %s" % ui.shorten_path(intel_path)


def scout_done_text(intel_path):
    return "scout finished — intel → %s" % ui.shorten_path(intel_path)


def planner_start_text(plan_md_path, resuming=False):
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
    return head + "\nplan → %s" % plan_md_path


def planner_needs_input_text():
    return "planner needs your input"


def planner_review_text(plan_md_path):
    return "plan ready for review — %s" % ui.shorten_path(plan_md_path)


def planner_done_text(plan_md_path):
    return "planner finished — plan approved → %s" % ui.shorten_path(plan_md_path)


def handoff_gate_text(payload):
    return ("planner wants to hand the work back to the scout\n"
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


def _read_handoff_confirm(io_in, io_out):
    """The hand-back confirmation gate. On a TTY an explicit questionary
    confirm; off a TTY a readline where blank/y/yes confirms (mirrors the
    blank=approve contract of `_read_review` for the scripted/test path)."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        return ui.confirm("Hand the work back to the scout?")
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
    review_rounds = 0
    outcome_kind = "ended"
    payload = None
    try:
        if context.strip():
            io_out.write(ui.label("you", ui.is_tty(io_out)) + context.strip() + "\n")
            io_out.flush()
        while True:
            if pending_reopens_work:
                changed = state_store.invalidate_ready_status(status_path)
                if trace:
                    trace.event("status.invalidated", role=role,
                                path=status_path, changed=changed,
                                from_status="ready_for_review",
                                to_status="needs_input",
                                reason="work_reopened")
                pending_reopens_work = False
            if trace:
                trace.event("role.send.start", role=role,
                            **trace_store.prompt_meta(pending))
            session.send(pending)
            if trace:
                trace.event("role.send.end", role=role)
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
                    ui.banner(io_out, handoff_gate_text(note), "review")
                    confirmed = (handoff_confirm or _read_handoff_confirm)(
                        io_in, io_out)
                    if trace:
                        trace.event("handoff.gate", role=role,
                                    confirmed=bool(confirmed))
                    if confirmed:
                        outcome_kind, payload = "handoff", note
                        break
                    # Declined: downgrade the stale handoff_back so the status
                    # file cannot re-trigger the gate, then let the role
                    # continue planning.
                    changed = state_store.invalidate_ready_status(
                        status_path, from_status="handoff_back")
                    if trace:
                        trace.event("status.invalidated", role=role,
                                    path=status_path, changed=changed,
                                    from_status="handoff_back",
                                    to_status="needs_input",
                                    reason="handoff_declined")
                    pending = handoff_declined_text()
                    continue
                # Payload-less handoff_back: degrade to the needs-input gate
                # (D10) — never an implicit hand-back.
                status = "needs_input"
            if status == "ready_for_review":
                dissent = ""
                dissent_verdict = None
                # Reviewer gate (topology D): runs transparently before the user.
                if review_fn is not None and review_rounds < REVIEW_ROUND_CAP:
                    review_rounds += 1
                    if trace:
                        trace.event("review.round.start", role=reviewer_role,
                                    round=review_rounds,
                                    round_cap=REVIEW_ROUND_CAP)
                    verdict = review_fn(status_path, review_rounds) or {}
                    if trace:
                        trace.event(
                            "review.verdict", role=reviewer_role,
                            round=review_rounds, verdict=verdict.get("verdict"),
                            has_question=bool(str(
                                verdict.get("user_question") or "").strip()),
                            findings_count=len(verdict.get("findings") or []),
                            malformed=bool(verdict.get("malformed")))
                    ui.banner(io_out, scout_reviewed_text(
                        verdict, review_rounds, REVIEW_ROUND_CAP), "info")
                    if evaluate_fn is not None:
                        try:
                            with ui.Spinner(io_out,
                                            label="Handoff in progress"):
                                evaluate_fn(session, verdict, review_rounds)
                        except Exception:  # noqa: BLE001 - observational only
                            if trace:
                                trace.event("eval.error", evaluator=role,
                                            round=review_rounds)
                    v = verdict.get("verdict")
                    has_question = bool(str(verdict.get("user_question") or "").strip())
                    if v == "approve":
                        # Only an explicit approve reaches the user gate.
                        review_rounds = 0
                    elif v == "needs_user" and has_question:
                        review_rounds = 0
                        pending = assemble_reviewer_handoff(
                            "needs_user", verdict, artifact=artifact_noun)
                        pending_reopens_work = True
                        if trace:
                            trace.event("review.handoff", from_role=reviewer_role,
                                        to_role=role, kind="needs_user")
                        continue
                    else:
                        # revise, an unknown/empty verdict (missing or unreadable
                        # review file), or needs_user without a question: the safe
                        # non-approving default — never silently approve.
                        if review_rounds < REVIEW_ROUND_CAP:
                            pending = assemble_reviewer_handoff(
                                "revise", verdict, artifact=artifact_noun)
                            pending_reopens_work = True
                            if trace:
                                trace.event("review.handoff",
                                            from_role=reviewer_role,
                                            to_role=role, kind="revise")
                            continue
                        # Cap reached without approval: fall through to the user
                        # with the reviewer's unresolved dissent attached (D5).
                        dissent = _dissent_suffix(verdict)
                        dissent_verdict = verdict
                        review_rounds = 0
                        if trace:
                            trace.event("review.round_cap", role=reviewer_role,
                                        round_cap=REVIEW_ROUND_CAP)
                if trace:
                    trace.event("gate.show", role=role,
                                gate="ready_for_review", path=status_path,
                                has_dissent=bool(dissent))
                ui.banner(io_out, review_text(status_path) + dissent,
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
                    review_rounds = 0  # user re-engaged: fresh review budget
                    continue
                if outcome is _END:
                    if trace:
                        trace.event("user.action", role=role,
                                    action="approve", gate="ready_for_review")
                        trace.event("gate.show", role=role, gate="done",
                                    path=status_path)
                    ui.banner(io_out, done_text(status_path), "done")
                    outcome_kind = "approved"
                    break
                pending = outcome  # revision feedback → another turn
                if trace:
                    trace.event("user.action", role=role, action="revise",
                                gate="ready_for_review",
                                **trace_store.prompt_meta(outcome, prefix="input"))
                pending_reopens_work = True
                review_rounds = 0  # user re-engaged: fresh review budget
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
                   session_uuid=None, intel_path=None, planning_epoch=None):
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
    aggregate path (the scratch itself stays in .cowork, overwritten per
    round; it is cleared before each eval send, not after)."""
    if reviewer_role not in selected or not review_path:
        return None
    runner = reviewer_runner or run_reviewer_once
    eval_enabled = bool(eval_scratch_path and scores_path and session_uuid)
    evaluatee = _REVIEWER_EVALUATEE.get(reviewer_role)
    holder = {"resume_id": reviewer_resume_id,
              "context_update": context_update,
              "ack": on_context_ack,
              "scout_evaled": phase != "planning"}

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
            # The ->scout bundle rides only the FIRST eval turn of the phase
            # (round_index == 1, per the plan).
            if (not holder["scout_evaled"] and round_index == 1 and intel_path
                    and os.path.exists(intel_path)
                    and (reviewer_role, "scout") in EVAL_CRITERIA):
                # Once per phase survives a resume/restart: the aggregate
                # itself is the durable record (the holder flag only covers
                # this closure) — scoped by the planning epoch, which bumps
                # on every scouting -> planning transition, so a hand-back
                # round trip (a new planning phase) is evaluated again even
                # when the re-approved intel is byte-identical.
                intel_text = _read_text(intel_path)
                if state_store.has_eval_entry(scores_path, reviewer_role,
                                              "scout", "consumed-intel",
                                              planning_epoch=planning_epoch):
                    holder["scout_evaled"] = True
                else:
                    # The advisor never consumed the intel through its review
                    # context, so the orchestrator reads the intel file at
                    # eval time and embeds it — self-contained evidence.
                    specs.append({
                        "evaluatee": "scout",
                        "criteria": EVAL_CRITERIA[(reviewer_role, "scout")],
                        "artifact_block":
                            "The approved scout intel JSON this phase "
                            "consumed:\n%s" % intel_text.strip(),
                        "context": "consumed-intel",
                        "planning_epoch": planning_epoch,
                        "intel_sha256": _intel_sha256(intel_text),
                        "phase": phase, "round": round_index,
                    })
            kwargs["eval_scratch_path"] = eval_scratch_path
            kwargs["eval_specs"] = specs
        verdict = runner(config, context, selected, artifact_path, review_path,
                         **kwargs)
        if specs:
            if len(specs) > 1:
                holder["scout_evaled"] = True
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
    `intel_path` is the scout's only write target (`.cowork/scout.intel.*.json`).
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
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, review_path, reviewer_runner=reviewer_runner,
        reviewer_resume_id=reviewer_resume_id,
        on_reviewer_session=on_reviewer_session,
        context_update=reviewer_context_update,
        on_context_ack=on_reviewer_context_ack,
        trace=trace, phase="scouting",
        eval_scratch_path=reviewer_eval_scratch_path,
        scores_path=scores_path, session_uuid=session_uuid)
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
    ui.banner(io_out, scout_start_text(intel_path or "", resuming=bool(resume_id)),
              "start")
    io_out.flush()

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = bridge.probe_claude_stream_json(
            spawn, mode=cfg["mode"], yolo=cfg["yolo"],
            role_prompt_file=SCOUT_PROMPT_PATH, trace=trace, role="scout",
        )
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
                on_session_id=cb, trace=trace)
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
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace)
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
    runner = reviewer_runner or make_planning_advisor_runner(
        plan_md_path, trace=trace)
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
        intel_path=intel_path, planning_epoch=planning_epoch)
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
                                         resuming=bool(resume_id)), "start")
    io_out.flush()

    def report(outcome, payload):
        if on_outcome:
            on_outcome(outcome, payload)

    loop_kwargs = dict(
        role="planner", review_fn=review_fn, trace=trace,
        reviewer_role=PLANNING_ADVISOR,
        needs_input_text=planner_needs_input_text,
        review_text=lambda _p: planner_review_text(plan_md_path or ""),
        done_text=lambda _p: planner_done_text(plan_md_path or ""),
        artifact_noun="plan",
        handoff_enabled=True, handoff_confirm=handoff_confirm,
        evaluate_fn=evaluate_fn)

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = bridge.probe_claude_stream_json(
            spawn, mode=cfg["mode"], yolo=cfg["yolo"],
            role_prompt_file=PLANNER_PROMPT_PATH, trace=trace, role="planner",
        )
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
                on_session_id=cb, trace=trace)
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
            resume_thread_id=resume_id, on_thread_id=cb, trace=trace)
    rc, outcome, payload = _role_loop(
        session, prompt, plan_json_path, context, io_in, io_out, **loop_kwargs)
    report(outcome, payload)
    return rc


# --------------------------------------------------------------------------- #
# Entry point.                                                                #
# --------------------------------------------------------------------------- #


def run_flow(args, io_in=None, io_out=None, which=None, run_scout_fn=None,
             run_planner_fn=None):
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    run_scout_fn = run_scout_fn or run_scout
    run_planner_fn = run_planner_fn or run_planner
    interactive = not _is_non_interactive(args)

    # Session store: project-local .cowork/session.json unless disabled.
    session_enabled = not args.no_session
    spath = args.session_file or state_store.session_path()
    saved = state_store.load(spath) if session_enabled else None
    # cowork session UUID (distinct from any claude/codex session id): names this
    # session's assets, e.g. the scout intel file.
    if session_enabled:
        saved = state_store.ensure_session(spath, saved, str(uuid.uuid4()))
        session_uuid = state_store.get_session_uuid(saved)
    else:
        session_uuid = str(uuid.uuid4())
    trace = trace_store.Trace(
        trace_store.trace_path_for(os.path.dirname(spath), session_uuid)
        if session_enabled else None,
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

    # Phase: resume into the persisted phase (default scouting). A persisted
    # `planning` phase without a planner on the team falls back to scouting.
    phase = state_store.get_phase(saved) if session_enabled else "scouting"
    planner_on_team = "planner" in selected
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
                "cowork: scout not selected. Only the scouting and planning "
                "phases are implemented in this version; later roles are not "
                "yet available.\n")
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
    lead_role = "planner" if phase == "planning" else "scout"
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

    intel_dir = os.path.dirname(spath) if session_enabled else state_store.session_dir()
    intel_path = scout_intel_path(intel_dir, session_uuid)
    review_path = state_store.review_path_for(intel_dir, session_uuid)
    plan_json_path = state_store.planner_plan_json_path_for(intel_dir, session_uuid)
    plan_md_path = state_store.planner_plan_md_path_for(intel_dir, session_uuid)
    planner_review_path = state_store.planner_review_path_for(
        intel_dir, session_uuid)
    # Peer-evaluation assets: a per-role scratch file (each evaluator's only
    # eval write target) and the orchestrator-only aggregate scores file.
    eval_scratch = {
        role: state_store.eval_scratch_path_for(intel_dir, role, session_uuid)
        for role in ("scout", SCOUT_REVIEWER, "planner", PLANNING_ADVISOR)
    }
    scores_path = state_store.scores_path_for(session_uuid)
    # Planning-phase epoch: bumped on every scouting -> planning transition so
    # the once-per-phase ->scout evals re-run after a hand-back round trip,
    # even when the re-approved intel is byte-identical. Resuming into the
    # planning phase keeps the persisted epoch.
    epoch_box = {"epoch": state_store.get_planning_epoch(holder["state"])
                 if session_enabled else 0}

    def bump_planning_epoch():
        if session_enabled:
            holder["state"] = state_store.bump_planning_epoch(
                spath, prior=holder["state"])
            epoch_box["epoch"] = state_store.get_planning_epoch(
                holder["state"])
        else:
            epoch_box["epoch"] += 1

    # Phase loop: scouting -> (on intel approval, planner on team) planning ->
    # (on a user-confirmed hand-back) scouting -> ... Plan approval, EOF, or an
    # interrupt ends the run; the persisted phase makes a rerun resume here.
    rc = 0
    scout_seed = context
    planner_seed = None
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

        # planning phase
        planner_box = {"outcome": None, "payload": None}
        rc = run_planner_fn(
            config,
            deliver_context("planner",
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
            # User-confirmed hand-back (planner -> its pre-processor): resume
            # the scout session with the handoff payload and run the full scout
            # cycle again.
            phase = set_phase("scouting")
            trace.event("handoff.execute", from_role="planner",
                        to_role=HANDBACK_PREPROCESSOR["planner"],
                        **trace_store.prompt_meta(
                            planner_box["payload"] or "", prefix="payload"))
            scout_seed = handoff_wake_block(planner_box["payload"])
            planner_seed = None
            continue
        # Plan approval is terminal for this run (the phase stays `planning`,
        # so a rerun resumes the planner conversation), and EOF/interrupt ends
        # the run the same way the scout loop always has.
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
