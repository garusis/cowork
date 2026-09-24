#!/usr/bin/env python3
"""cowork: agent-driven multi-role orchestration of the scouting, planning,
and building phases.

An orchestrating agent invokes `cowork` with arguments (--context/--context-
file, --team, --config, --session-file, ...). A phase loop drives the lead
roles by spawning the selected controller CLI: the `scout` (paired with the
`scout-reviewer`) gathers context; on intel approval the `planner` (paired with
the `planning-advisor`) turns it into a plan; on plan approval the `builder`
(paired with the `build-reviewer`) executes it. Approval comes only from the
paired reviewer's explicit verdict. Anything that needs an answer, an
authorization or an absent reviewer stops the run unapproved with a structured
request (see `build_run_result`), and the orchestrator resumes the session with
more arguments. A hand-back to a pre-processor (planner -> scout,
builder -> planner) executes only with `--authorize-handoff`. Build approval
ends the run with no git side effects.

Nothing here reads human input: no menus, prompts or terminal gates.

Python 3.9+.
"""

import argparse
import collections
import contextlib
import datetime
import errno
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
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_bridge as bridge  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_report  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_transcript as transcript  # noqa: E402
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
import cowork_capacity as capacity_contracts  # noqa: E402
import cowork_capacity_scheduler as capacity_scheduler  # noqa: E402
import cowork_activity as activity_contracts  # noqa: E402
import cowork_watchdog as watchdog  # noqa: E402
import cowork_recovery_breaker as recovery_breaker  # noqa: E402
import cowork_owner  # noqa: E402

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
# reviewer passes without approval, the phase stops unapproved with the
# reviewer's last dissent attached. Shared by all three paired reviewers.
REVIEW_ROUND_CAP = 5

# Max CONSECUTIVE reviewer turns with no usable verdict (account limit, crash,
# empty/garbled write) before the phase stops with a `reviewer_unavailable`
# request: one silent auto-retry of the reviewer, then the stop. Distinct from
# REVIEW_ROUND_CAP, which bounds a reviewer that legitimately keeps requesting
# changes. Shared by all three paired reviewers.
REVIEW_FAIL_CAP = 2

# Runtime notes prepended to a lead's/reviewer's seed on every run, so the role
# KNOWS on its very first turn that no human is attached and how a question that
# genuinely needs outside authority is reported (the runtime layer behind the
# role prompts' "Agent session" sections).
AGENT_LEAD_NOTE = (
    "[agent session] No human is attached to this session; an orchestrating "
    "agent reads your status artifact. Resolve ordinary ambiguity yourself: "
    "choose the most reasonable interpretation, record it in "
    "result.assumptions, and drive to ready_for_review. Only when the work "
    "cannot responsibly proceed without a decision or authority you do not "
    "have, set status needs_input with the exact question in "
    "result.pending_question — the run then stops and the orchestrator answers "
    "when it resumes the session. Never wait for input in chat.")
AGENT_REVIEWER_NOTE = (
    "[agent session] No human is attached to this session. Review with the "
    "context you have and express concerns as revise findings (or approve). "
    "Emit needs_user only for a decision that requires authority beyond the "
    "review itself; it stops the run unapproved until the orchestrator "
    "answers.")

# Role order matches the phase order: context-gather
# (scouting), planning, building. Each lead role is followed by its
# paired critical reviewer. All three phases — `scout`/`scout-reviewer`,
# `planner`/`planning-advisor`, `builder`/`build-reviewer` — are implemented.
#
# `scout-reviewer`, `planning-advisor`, and `build-reviewer` are critical
# reviewers paired with their lead role DURING that role's session
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

# Hand-back contract: a lead role may set `status: "handoff_back"` (plus
# a `handoff` payload) in its status file to hand the work back to its
# pre-processor through an orchestrator-authorized request. The contract is role-generic:
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
# Role config (machine arguments only: --team / --config / saved session).    #
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


# --------------------------------------------------------------------------- #
# Initial context.                                                            #
# --------------------------------------------------------------------------- #


def resolve_context(args, resuming=False):
    """Context from --context or --context-file (a path, or '-' for stdin).

    Absent both, returns "": a resumed session turns that into "Continue the
    session." so the current phase's role picks up where it left off. A fresh
    session without context is refused earlier, in `run_flow`. An explicit
    --context/--context-file on a plain resume redirects the session goal; with
    a decision flag it is that decision's answer and never replaces the goal."""
    if args.context is not None:
        return args.context
    if args.context_file is not None:
        if args.context_file == "-":
            return sys.stdin.read()
        with open(args.context_file, "r") as fh:
            return fh.read()
    return ""


# --------------------------------------------------------------------------- #
# Argument parsing.                                                           #
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
                        "record instead of its rendered text form; with "
                        "--session-owner: print the raw owner status view")
    p.add_argument("--rebuild", action="store_true",
                   help="with --report: rebuild the measurement record from "
                        "the raw sources before printing (by default a report "
                        "loads the existing record and never rebuilds it)")
    # Issue #64 P4 surface 1. NOTE, recorded rather than worked around: adding
    # this flag makes the abbreviation `--session` ambiguous against
    # `--session-file` under argparse's allow_abbrev — the same tradeoff
    # `--eval-session` documents below. The flag name is fixed by the plan, and
    # every full-form flag (`--session-file`, `--no-session`, `--session-owner`)
    # is unaffected.
    p.add_argument("--session-owner", dest="session_owner", nargs="?",
                   const=True, metavar="SESSION_UUID",
                   help="print a read-only view of a cowork session's "
                        "single-writer owner lease: who owns it, from where, "
                        "how fresh its heartbeat is, and the exact recovery "
                        "command (defaults to this directory's most recent "
                        "session). Acquires no lease, constructs no "
                        "controller, and always exits 0 — it reports, it does "
                        "not gate")
    p.add_argument("--evaluation-policy", dest="evaluation_policy",
                   choices=list(state_store.EVALUATION_POLICIES),
                   help="how much of the run gets scored: all_rounds "
                        "(default), final_round, sampled, or off. The overhead "
                        "of the choice is reported separately.")
    p.add_argument("--team",
                   help="comma-separated roles, e.g. "
                        "scout,scout-reviewer (default: every role, or the "
                        "saved team on resume)")
    p.add_argument("--config", action="append", default=[],
                   metavar="ROLE=opt,opt",
                   help="per-role override, e.g. scout=codex,no-yolo,implement "
                        "or builder=opencode,model=anthropic/claude-sonnet-4-5,"
                        "effort=high (options: claude/codex/opencode, "
                        "model=<id>, effort=<level>, yolo/no-yolo, "
                        "plan/implement; repeatable)")
    p.add_argument("--context",
                   help="initial context text (required for a new session); "
                        "with --session-file/--resume it redirects the saved "
                        "session, and with --answer it is the answer")
    p.add_argument("--context-file",
                   help="like --context, read from a file, or '-' for stdin")
    p.add_argument("--session-file",
                   help="the session store to use: resumes that saved session "
                        "when the file exists, or creates a new session there "
                        "(with --context) when it does not")
    p.add_argument("--no-session", action="store_true",
                   help="do not read or write the session store")
    p.add_argument("--new", action="store_true",
                   help="start a new session (the default when no session "
                        "selector is given; requires --context)")
    p.add_argument("--take-over", dest="take_over", action="store_true",
                   help="take over this session's single-writer owner lease "
                        "from a prior process (issue #64). Never implicit: a "
                        "crashed owner is reclaimed only with proof of death, "
                        "and a LIVE same-host owner is terminated first. A "
                        "lease whose owner cannot be PROVED dead -- a foreign "
                        "host, or an unreadable process table -- refuses "
                        "instead, because guessing here is how two writers "
                        "get created")
    p.add_argument("--resume", action="store_true",
                   help="resume this directory's most recent saved session "
                        "(an error when there is none); --session-file names "
                        "one exactly")
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
    p.add_argument("--answer", dest="answer", metavar="REQUEST_ID",
                   help="answer the saved session's open decision request "
                        "(needs_input, reviewer_question, review_round_cap, "
                        "review_not_approved) with --context/--context-file; "
                        "REQUEST_ID is stop.request_id from the run result")
    p.add_argument("--authorize-handoff", dest="authorize_handoff",
                   metavar="REQUEST_ID",
                   help="authorize the saved session's open hand-back "
                        "request; the hand-back executes without another "
                        "lead turn")
    p.add_argument("--decline-handoff", dest="decline_handoff",
                   metavar="REQUEST_ID",
                   help="decline the saved session's open hand-back request; "
                        "the lead continues its own phase (optional "
                        "--context is delivered with the decline)")
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


def run_report(args, io_out=None):
    """Handle `cowork --report [<session-uuid>]` — FOUR ORDERED STEPS with no
    coupling between them (P2, extended by issue #64 P4).

    (a) LOAD `measurement.json`. It is built only when none exists (and the
        report says so) or when `--rebuild` is passed. NEVER implicitly: a
        report that rebuilt every time could not be distinguished from one that
        recomputed its figures, which is the failure D3 exists to prevent.
    (b) WRITE THE OWNER BLOCK, above the provenance banner (P4 surface 2). It
        is LIVE state read at print time, not a measured figure: it is handed
        to `cowork_report.render_owner_status`, never to `render_report`, and
        never enters the record. The view is read ONLY when a lease record
        already exists, so a report run against a git-tracked measurement
        fixture stays a pure read.
    (c) CHECK PROVENANCE and print its banner. It hashes the raw sources to
        decide whether to warn, and produces no measurement figure — which is
        what keeps "the report computes nothing" literally true. Its result is
        never passed into the renderer.
    (d) RENDER the record, with the record as the renderer's only argument.

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
        # a caller with no way to read it would be told it exists and shown only
        # the derivation.
        json.dump(record, io_out, indent=2, sort_keys=True, default=str)
        io_out.write("\n")
        io_out.flush()
        return 0

    # Issue #64 P4 surface 2 — the live owner block, ABOVE the provenance
    # banner. Written unconditionally, because `render_provenance_banner`
    # returns "" for a fresh record: "above the banner" cannot be expressed as
    # an insertion relative to something that is usually absent.
    #
    # The view is read only when a lease record ALREADY EXISTS. That gate is
    # not an optimisation: `read_owner_lease` goes through
    # `_locked_json_transaction`, which creates the session's `owner/`
    # directory and opens `lease.json.lock` BEFORE it reads, and a report run
    # against a git-tracked measurement fixture must not write into the very
    # tree it is reporting on.
    view = None
    try:
        if os.path.exists(state_store.owner_lease_path_for(session_uuid)):
            view = cowork_owner.owner_status_view(session_uuid)
    except (ValueError, OSError):
        # NARROW by design. `owner_status_view` never raises for anything it
        # finds on disk; `owner_lease_path_for` raises ValueError for an unsafe
        # session id, and the stat can raise OSError. Nothing else is swallowed.
        view = None
    io_out.write(cowork_report.render_owner_status(view))

    provenance = measure.check_provenance(session_uuid, record)
    banner = cowork_report.render_provenance_banner(provenance)
    if banner:
        io_out.write(banner)
    # The record is the renderer's ONLY argument. `provenance` deliberately does
    # not travel with it.
    io_out.write(cowork_report.render_report(record))

    io_out.flush()
    return 0


def run_session_owner(args, io_out=None):
    """Handle `cowork --session-owner [<session-uuid>]` — the read-only owner
    status surface (issue #64 P4 surface 1).

    FOUR INERTNESS PROPERTIES, and together they are the whole contract:

      - it ACQUIRES NO LEASE — `cowork_owner.owner_status_view` is a
        projection: it reports, it never gates;
      - it CONSTRUCTS NO CONTROLLER and creates no session of any kind;
      - it MAKES NO PROVIDER CALL and spawns no process of its own;
      - it EXITS 0 for every lease state, including `corrupt`, an unsafe id
        and a directory with no sessions at all.

    The exit code is deliberate. `--session-owner` on a contested session is
    how an operator finds out who holds it and what to do next, so a nonzero
    exit — or worse, a refusal — would make the diagnostic unusable from the
    very scripts that need it most. `run_report`'s `return 1` for an
    unresolvable session is NOT copied here for that reason.

    Unlike `run_report`'s owner block, this path is NOT gated on a lease file
    already existing: `--json` must print the RAW `owner_status_view` for all
    five verdicts, `unowned` included, rather than a locally synthesised
    look-alike. The accepted cost is that a never-leased session may be left
    with an empty `owner/` directory and a zero-byte `lease.json.lock` under
    the sessions root — never inside a repository tree.
    """
    io_out = io_out or sys.stdout
    as_json = bool(getattr(args, "report_json", False))
    session_uuid = (args.session_owner
                    if isinstance(args.session_owner, str) else None)
    if not session_uuid:
        sessions = state_store.list_sessions()
        if not sessions:
            io_out.write("null\n" if as_json
                         else "cowork: no sessions found for this "
                              "directory.\n")
            io_out.flush()
            return 0
        session_uuid = sessions[0]["id"]

    try:
        view = cowork_owner.owner_status_view(session_uuid)
    except ValueError:
        # The ONE documented caller-error path: an unsafe session id typed on
        # the command line. Everything the projection finds ON DISK — missing,
        # damaged, unreadable or foreign — comes back as a `verdict` instead.
        io_out.write("null\n" if as_json
                     else "cowork: %r is not a usable session id.\n"
                          % (session_uuid,))
        io_out.flush()
        return 0

    if as_json:
        # The RAW view, with no wrapper envelope: a caller that has to unwrap
        # a bespoke shape here would be reading a second projection, and two
        # projections of one fact are two things to keep in sync.
        json.dump(view, io_out, indent=2, sort_keys=True, default=str)
        io_out.write("\n")
        io_out.flush()
        return 0

    # The exact recovery command needs the owning session's own anchor path.
    # The lease RECORDS it (`_build_lease` stores the claimant's realpath'd
    # `session_file`), so prefer that: it is the path the owner actually holds,
    # not a reconstruction of it. Fall back to discovering it under the
    # recorded launch directory, and finally to the conventional per-session
    # name — and to None, a bare `cowork --take-over`, rather than a guess.
    session_file = None
    lease = view.get("lease") if isinstance(view.get("lease"), dict) else {}
    recorded = lease.get("session_file")
    try:
        if isinstance(recorded, str) and recorded and os.path.exists(recorded):
            session_file = recorded
    except (OSError, ValueError):
        session_file = None
    launch_dir = lease.get("launch_dir")
    if session_file is None and isinstance(launch_dir, str) and launch_dir:
        try:
            candidates = (state_store.discover_session_files(launch_dir)
                          if os.path.isdir(launch_dir) else [])
        except (OSError, ValueError):
            candidates = []
        wanted = "session.%s.json" % session_uuid
        for candidate in candidates:
            if os.path.basename(candidate) == wanted:
                session_file = candidate
                break
            try:
                state = state_store.load(candidate)
            except (OSError, ValueError):
                continue
            if state is None:
                continue
            if state_store.get_session_uuid(state) == session_uuid:
                session_file = candidate
                break
        if session_file is None:
            try:
                session_file = state_store.new_session_path(launch_dir,
                                                            session_uuid)
            except (OSError, ValueError):
                session_file = None

    io_out.write(cowork_report.render_owner_status(view, session_file))
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


def assemble_scout_brief(selected, intel_path, intel_md_path=None):
    """Dynamic first-message brief for the scout: where to write, the JSON +
    domain guardrail, and the plan-only fallthrough for this team.

    When `intel_md_path` is given, the scout writes TWO files: the JSON (machine
    source of truth + status channel) and a readable markdown rendering (the
    review surface, also reviewed by the scout-reviewer). Both are the
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
            "  Markdown (the readable review surface, small scannable sections): "
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
    return "%s\n%s" % (target, plan_note)


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
# scout's initial context (the run `context`, NOT the scout's write-target      #
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


def _agent_lead_fragment():
    """The one closed runtime note that may prefix a cross-role lead seed."""
    return handoff._static_role_text(AGENT_LEAD_NOTE)


def _agent_reviewer_fragment():
    """The closed runtime reviewer note, typed, for a resumed reviewer's
    composed wake prefix."""
    return handoff._static_role_text(AGENT_REVIEWER_NOTE)


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


def _is_known_static_fragment(s):
    t = str(s or "").strip()
    if not t:
        return True
    if getattr(s, "kind", None) == "static_role" or type(s).__name__ == "_BoundaryText":
        return True
    known_markers = (
        AGENT_LEAD_NOTE, AGENT_REVIEWER_NOTE,
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
                            protected="the scout intel files (JSON and markdown)"):
    """The reviewer's write-target instruction — its analogue of the scout brief.
    It points at the review file only (never the reviewed artifact, named by
    `protected`)."""
    return (
        "Write your verdict as a single JSON object to exactly this file:\n"
        "  %s\n"
        "That review file is your ONLY write target. Do NOT edit %s "
        "or any other file (reading/searching the repo is fine). Use the "
        "verdict schema from your role (verdict: approve|revise|needs_user, "
        "findings, and user_question when needs_user)."
        % (review_path, protected)
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
        arts.append({"label": "intel markdown (the readable review surface)",
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
    findings there, keeping the transport file-only. Only `revise` hands back:
    a reviewer `needs_user` stops the run with a structured request instead.
    `artifact` names what was reviewed ("intel" for the scout, "plan" for the
    planner, "build" for the builder). Returns "" for any other verdict.

    `review_path` is the reviewer's verdict file; when absent (a legacy/test
    call passing only the verdict dict), the verdict is materialized to a
    tempfile so the transport still carries a real path."""
    if verdict != "revise":
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
    return handoff.render_handoff(
        "reviewer->lead:handback_revise", artifacts=[art], facts=facts)


def scout_reviewed_text(verdict=None, round_index=None, round_cap=None):
    """Transcript marker recording that a review happened (D7).

    It exposes only the verdict class, never reviewer findings or questions. The
    substring 'reviewed' is asserted by tests. With `round_index`/`round_cap` it
    appends a round counter so the transcript shows review-budget progress and
    resets (a fresh '(round 1/N)' in a new invocation)."""
    v = verdict.get("verdict") if isinstance(verdict, dict) else verdict
    counter = ""
    if round_index is not None and round_cap:
        counter = " (round %d/%d)" % (round_index, round_cap)
    if v == "approve":
        return "reviewed: approved" + counter
    if v == "revise":
        return "reviewed: changes requested" + counter
    if v == "needs_user":
        return "reviewed: needs an orchestrator decision" + counter
    return "reviewed" + counter


def review_skipped_text():
    """Transcript marker recording that the paired reviewer turn was SKIPPED by the
    hash-gate: the lead's reviewed artifact set is byte-identical to what that
    reviewer last approved this phase, so the prior approval is reused (D6).

    Content-free and single-voice (modeled on scout_reviewed_text); never a
    silent bypass. The substring 'review skipped' is asserted by tests."""
    return "review skipped — unchanged since last approved"


class _QuietSink:
    """A write sink that discards everything — used as a muted session's
    `io_out` so its raw stream is never interleaved into the lead's transcript
    (single-voice invariant, D7)."""

    def write(self, _s):
        return None

    def flush(self):
        return None


# --------------------------------------------------------------------------- #
# Peer evaluations: after every review round both sides of the active pairing  #
# privately score each other (1-5 per criterion + feedback + enhancement       #
# suggestions); planner and planning-advisor additionally evaluate the scout    #
# once per planning phase. Each evaluator writes only its own scratch file;    #
# the orchestrator stamps metadata and aggregates into the per-session         #
# scores.json. Purely observational: failures are traced and skipped, and no   #
# evaluation content ever reaches the transcript or the evaluated role.        #
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
        "authority escalation (escalated what needs authority, resolved what "
        "the context settles)",
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

# Which lead role a paired reviewer evaluates on its eval turn.
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
        "the cowork orchestrator. It is NOT part of the task: it is never "
        "written to the run transcript, and the roles you evaluate never see "
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
        "file or any scores file; never mention this evaluation in your reply. "
        "Keep your reply text minimal — the scratch file is the deliverable."
        % ("\n\n".join(blocks), scratch_path)
    )


@contextlib.contextmanager
def _muted_session(session):
    """Temporarily swap a role session's io_out for a quiet sink.

    The lead session writes the transcript: the bridges stream assistant
    text and denial messages to `session.io_out`, resolved at send
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
        clock = measure.newest_source_mtime(
            os.getcwd(), _source_paths_for_manifest(),
            deleted=verification.git_deleted_paths(None))
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
    # rather than treating as "nothing has changed". An unstaged deletion git
    # reports is timed by its parent directory; any other absence fails closed.
    newest_mtime = measure.newest_source_mtime(
        os.getcwd(), _source_paths_for_manifest(),
        deleted=verification.git_deleted_paths(None))
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
    """The transcript notice when a promotion is handed back unverified."""
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


def checkpoint_disposition_path_for(session_uuid, checkpoint_id):
    """Path of one checkpoint's own review-disposition record — sibling to
    `state_store.checkpoint_receipt_path_for`'s file, inside the SAME
    per-checkpoint directory (`state_store.checkpoint_dir_for`). A
    checkpoint_id is already the sole key for its own directory, so no new
    session-wide sidecar or `cowork_state.py` path helper is needed the way
    the whole-transaction dispositions sidecar needed one (M5 Package D,
    garusis/cowork-internal#24 extension)."""
    return os.path.join(
        state_store.checkpoint_dir_for(session_uuid, checkpoint_id),
        "disposition.json")


def checkpoint_latest_disposition(session_uuid, checkpoint_id):
    """The latest recorded disposition for one checkpoint claim, or None —
    the checkpoint analog of `_latest_verification_disposition` (D-0001),
    reading the per-checkpoint record instead of the session-wide
    transaction sidecar."""
    if not (session_uuid and checkpoint_id):
        return None
    entry = state_store.read_json_tolerant(
        checkpoint_disposition_path_for(session_uuid, checkpoint_id))
    return (entry or {}).get("disposition")


def checkpoint_emit_disposition(session_uuid, trace, checkpoint_id,
                                disposition, review_round=None, work_id=None):
    """Record ONE review disposition for a checkpoint claim — the checkpoint
    analog of `_emit_verification_disposition` (D-0001/D-0002): PRIMARY the
    `verification.disposition` trace event (carrying `checkpoint_id` rather
    than `transaction_id`), PLUS the per-checkpoint disposition record
    beside the checkpoint's own receipt, PLUS an in-place update of the
    named `work_id`'s current-checkpoint binding when that binding still
    names this checkpoint (so a same-process render sees the new value
    without a re-join, mirroring D-0002 for checkpoints)."""
    if not (session_uuid and checkpoint_id
            and disposition in verification.DISPOSITIONS):
        return
    if trace:
        trace.event("verification.disposition", checkpoint_id=checkpoint_id,
                    disposition=disposition, review_round=review_round)
    state_store.write_json_atomic(
        checkpoint_disposition_path_for(session_uuid, checkpoint_id),
        {"checkpoint_id": checkpoint_id, "disposition": disposition,
         "review_round": review_round})
    if not work_id:
        return
    pointer_path = state_store.current_checkpoint_pointer_path_for(
        session_uuid, work_id)
    pointer = state_store.read_json_tolerant(pointer_path)
    if isinstance(pointer, dict) \
            and pointer.get("checkpoint_id") == checkpoint_id \
            and pointer.get("disposition") != disposition:
        state_store.write_json_atomic(
            pointer_path, dict(pointer, disposition=disposition))


def checkpoint_overlay(pointer, disposition=None):
    """THE ONE checkpoint overlay renderer — the checkpoint analog of
    `verification_overlay`: a dict of content-free tokens derived from a
    checkpoint pointer/binding (never a byte of raw stdout/stderr or agent
    prose — a checkpoint's executor is a deterministic, non-model command,
    but its result payload is still delivered by PATH only, never inlined).
    `disposition` is the render-time join onto the latest disposition known
    for the checkpoint claim; absent, the pointer's own field is used.
    Returns None when there is no bound `checkpoint_id`."""
    if not isinstance(pointer, dict) or not pointer.get("checkpoint_id"):
        return None
    return {
        "checkpoint_id": pointer.get("checkpoint_id"),
        "work_id": pointer.get("work_id"),
        "phase": pointer.get("phase"),
        "candidate_digest": pointer.get("candidate_digest"),
        "verdict": pointer.get("verdict"),
        "rejection_reason": pointer.get("rejection_reason"),
        "disposition": (disposition or pointer.get("disposition")
                        or verification.DISPOSITION_PENDING_REVIEW),
    }


def checkpoint_current_overlay(session_uuid, work_id):
    """`(overlay, pointer)` for the CURRENT checkpoint bound to one role
    engagement (`work_id`) — the checkpoint analog of
    `_current_verification_overlay`. The binding at
    `state_store.current_checkpoint_pointer_path_for` names the checkpoint
    id; every other field is read straight from that checkpoint's own
    TERMINAL receipt (never from an in-progress request/result, which are
    not yet reviewer-facing), plus a `receipt_path` for path-only delivery
    so a reviewer surface never has to touch `state_store.checkpoint_*`
    paths itself. Returns `(None, None)` when nothing is bound yet or the
    bound checkpoint has no terminal receipt."""
    if not (session_uuid and work_id):
        return None, None
    binding = state_store.read_json_tolerant(
        state_store.current_checkpoint_pointer_path_for(session_uuid, work_id))
    checkpoint_id = (binding or {}).get("checkpoint_id")
    if not checkpoint_id:
        return None, None
    receipt = state_store.read_json_tolerant(
        state_store.checkpoint_receipt_path_for(session_uuid, checkpoint_id))
    if not isinstance(receipt, dict):
        return None, None
    pointer = {
        "checkpoint_id": checkpoint_id,
        "work_id": receipt.get("work_id"),
        "phase": receipt.get("phase"),
        "candidate_digest": receipt.get("candidate_digest"),
        "verdict": receipt.get("verdict"),
        "rejection_reason": receipt.get("rejection_reason"),
        "receipt_path": state_store.checkpoint_receipt_path_for(
            session_uuid, checkpoint_id),
        "disposition": isinstance(binding, dict) and binding.get(
            "disposition") or None,
    }
    disposition = checkpoint_latest_disposition(session_uuid, checkpoint_id)
    return checkpoint_overlay(pointer, disposition=disposition), pointer


def checkpoint_superseded_ids(session_uuid, work_id, current_checkpoint_id):
    """Every OTHER checkpoint id ever requested for this `work_id` besides
    `current_checkpoint_id` — the stale/superseded claims a reviewer handoff
    must mechanically suppress once a later checkpoint becomes the CURRENT
    bound claim for the same role engagement. Read-only and count/id-only
    (never a byte of the superseded claim's own content): derived from
    `state_store.list_checkpoint_ids` plus each id's own `request.json`
    `work_id` field, never from a separate index that could itself drift out
    of sync."""
    if not (session_uuid and work_id):
        return []
    superseded = []
    for checkpoint_id in state_store.list_checkpoint_ids(session_uuid):
        if checkpoint_id == current_checkpoint_id:
            continue
        request = state_store.read_json_tolerant(
            state_store.checkpoint_request_path_for(session_uuid,
                                                     checkpoint_id))
        if isinstance(request, dict) and request.get("work_id") == work_id:
            superseded.append(checkpoint_id)
    return sorted(superseded)


def checkpoint_handoff_facts(session_uuid, work_id):
    """The builder->build-reviewer edge's five `checkpoint_*` facts (M5
    Package E, garusis/cowork-internal#60) — the producer D's own
    `checkpoint_current_overlay`/`checkpoint_superseded_ids` primitives never
    got wired to. Maps `checkpoint_current_overlay`'s unprefixed overlay
    fields onto the edge's declared `checkpoint_id`/`checkpoint_phase`/
    `checkpoint_verdict`/`checkpoint_disposition` fact keys (see
    `cowork_handoff._FACT_SCHEMAS`, which now validates all five closed-shape
    — M5D-R-m1) and reports `checkpoint_superseded_count`.

    M5D-R-m2 disposition: D's own frozen rendering block (`cowork_handoff.
    _render_owned_verification_block`, never edited here) describes suppressed
    claims as "earlier ... superseded" — this producer makes that wording
    TRUTHFUL rather than merely softening it: `checkpoint_superseded_count`
    counts ONLY superseded checkpoints whose own request `created_at`
    genuinely precedes the current checkpoint's — never a same-or-later one
    that happens to share the count bucket. Returns `{}` when nothing is
    bound yet."""
    overlay, _pointer = checkpoint_current_overlay(session_uuid, work_id)
    if not overlay:
        return {}
    checkpoint_id = overlay["checkpoint_id"]
    facts = {
        "checkpoint_id": checkpoint_id,
        "checkpoint_phase": overlay.get("phase"),
        "checkpoint_verdict": overlay.get("verdict"),
        "checkpoint_disposition": overlay.get("disposition"),
    }
    current_request = state_store.read_json_tolerant(
        state_store.checkpoint_request_path_for(session_uuid, checkpoint_id))
    current_created_at = (current_request or {}).get("created_at") or ""
    earlier_count = 0
    for other_id in checkpoint_superseded_ids(session_uuid, work_id,
                                              checkpoint_id):
        other_request = state_store.read_json_tolerant(
            state_store.checkpoint_request_path_for(session_uuid, other_id))
        other_created_at = (other_request or {}).get("created_at") or ""
        if other_created_at and current_created_at and (
                other_created_at < current_created_at):
            earlier_count += 1
    if earlier_count:
        facts["checkpoint_superseded_count"] = earlier_count
    return facts


def dispatch_role_checkpoint(session_uuid, work_id, phase, candidate_digest,
                             argv, cwd, mutation_class,
                             status=verification.CHECKPOINT_STATUS_REQUIRED,
                             declared_output_paths=None,
                             expected_evidence=None, timeout_s=None,
                             env=None, run=True):
    """THE central checkpoint gateway (M5 Package E, garusis/cowork-internal
    #60): the ONE place a role-loop/dispatch point mints a typed
    CheckpointRequest, binds it as the CURRENT checkpoint for `work_id`
    (`state_store.current_checkpoint_pointer_path_for`, the same pointer
    `checkpoint_current_overlay`/`checkpoint_handoff_facts` already read),
    and — unless `run=False` (a caller that hands execution to a separate
    process) — runs it to a terminal receipt via
    `cowork_verification.run_checkpoint`.

    A FRESH checkpoint_id is minted per call (`verification.
    mint_checkpoint_id`): dispatching again for the SAME `work_id` naturally
    supersedes the prior checkpoint the instant this call rebinds the
    current-checkpoint pointer — `checkpoint_superseded_ids` picks up every
    OTHER checkpoint ever requested for this `work_id`, and
    `checkpoint_gate_evidence` below only ever reads the pointer's CURRENT
    id, so a stale/superseded checkpoint's own (possibly still-accepted)
    receipt can never be consulted as gate evidence for this `work_id` again.

    Returns `(checkpoint_id, receipt_or_None)` — `receipt_or_None` is the
    terminal CheckpointReceipt when `run=True`, else `None` (the caller is
    responsible for eventually calling `verification.run_checkpoint` or
    `verification.submit_checkpoint_result` itself)."""
    checkpoint_id = verification.mint_checkpoint_id(work_id)
    verification.build_and_persist_checkpoint_request(
        session_uuid, checkpoint_id, work_id, phase, candidate_digest, argv,
        cwd, mutation_class, status=status, env=env,
        expected_evidence=expected_evidence, timeout_s=timeout_s,
        declared_output_paths=declared_output_paths)
    state_store.write_json_atomic_durable(
        state_store.current_checkpoint_pointer_path_for(session_uuid,
                                                         work_id),
        {"checkpoint_id": checkpoint_id, "work_id": work_id, "phase": phase})
    receipt = None
    if run:
        receipt = verification.run_checkpoint(session_uuid, checkpoint_id)
    return checkpoint_id, receipt


def checkpoint_gate_evidence(session_uuid, work_id, expected_candidate_digest,
                             candidate_index=None):
    """The fail-closed pre-check a checkpoint-gated dispatch point consults
    BEFORE ever attempting `cowork_control_plane.advance(..., "gate_
    validated", ...)`: `(evidence, reason_code)`, where `evidence` is
    `advance()`-ready gate evidence (`cowork_control_plane.
    checkpoint_receipt_to_gate_evidence`) only when the work's CURRENT bound
    checkpoint (never a stale/superseded one — the pointer is the sole
    source of "current") has a terminal, `verdict="accepted"` receipt bound
    to EXACTLY `expected_candidate_digest`. Otherwise returns `(None,
    reason_code)` and the caller must never call `advance()` with fabricated
    evidence — `reason_code` is one of `"checkpoint_missing"`,
    `"checkpoint_not_terminal"`, `"checkpoint_rejected"`, or
    `"checkpoint_cross_candidate"`.

    This function is a caller-side convenience; the REAL enforcement that a
    stale/cross-candidate checkpoint can never advance the live control
    plane is `advance()`'s own, unmodified `_gate_evidence_matches_candidate`
    check — see `cowork_control_plane.checkpoint_receipt_to_gate_evidence`'s
    own docstring and `scripts/test_m5_package_e_integration.py`'s direct
    proof against the real reducer."""
    _overlay, pointer = checkpoint_current_overlay(session_uuid, work_id)
    if not isinstance(pointer, dict) or not pointer.get("checkpoint_id"):
        return None, "checkpoint_missing"
    checkpoint_id = pointer["checkpoint_id"]
    receipt = state_store.read_json_tolerant(
        state_store.checkpoint_receipt_path_for(session_uuid, checkpoint_id))
    if not isinstance(receipt, dict) or receipt.get("terminal") is not True:
        return None, "checkpoint_not_terminal"
    if receipt.get("verdict") != verification.CHECKPOINT_ACCEPTED:
        return None, "checkpoint_rejected"
    if receipt.get("candidate_digest") != expected_candidate_digest:
        return None, "checkpoint_cross_candidate"
    evidence = control_plane.checkpoint_receipt_to_gate_evidence(
        receipt, candidate_index=candidate_index)
    if evidence is None:
        return None, "checkpoint_not_accepted"
    return evidence, None


def checkpoint_wake_block(session_uuid, work_id, role):
    """Render route 14 (`cowork_handoff`'s `cowork->role:checkpoint_wake`,
    M5 Package E) for the WAITING role's own currently-bound checkpoint —
    distinct from D's reviewer-facing `checkpoint_receipt` import
    (`checkpoint_handoff_facts`/`assemble_build_reviewer_context` above):
    this imports a checkpoint's status into the role that DISPATCHED it,
    whatever its current lifecycle state (pending/claimed/terminal), by
    pointing straight at that state's own already-durable artifact (no new
    file is ever written for this — the request/claim/receipt already on
    disk from `verification.build_and_persist_checkpoint_request`/
    `claim_checkpoint`/`publish_checkpoint_receipt` is the wake payload).

    Returns `None` — no wake needed — when nothing is bound for `work_id`.

    LIVENESS (M5 criterion 5, A-C5-CRASH-STRAND). Reconstruction goes
    through `verification.reconstruct_checkpoint_state_with_liveness`, not
    the bare `reconstruct_checkpoint_state`, so this production wake path is
    a real call site for `classify_checkpoint_claim_liveness`. Without it a
    checkpoint whose claimant crashed past its own persisted lease wakes the
    role as plain `claimed` forever — the durable claim really does still
    say `claimed`, and nothing else on this path could tell the role that
    nobody is behind it any more.

    The verdict is exposed ADDITIVELY, as the returned block's own
    `checkpoint_claim_liveness` attribute, and deliberately NOT as a handoff
    fact: `cowork_handoff`'s `cowork->role:checkpoint_wake` edge declares a
    CLOSED fact vocabulary (`role`, `checkpoint_id`, `checkpoint_phase`,
    `checkpoint_verdict`, `checkpoint_state`) and rejects any undeclared
    fact, and widening that edge is not this seam's to do. The `checkpoint_
    state` fact therefore keeps EXACTLY its existing four-value vocabulary
    and is never overloaded with a liveness value — the two vocabularies are
    disjoint and travel in separate, explicitly named channels, so no
    consumer can read a crash-stranded claim as an ordinary live one."""
    binding = state_store.read_json_tolerant(
        state_store.current_checkpoint_pointer_path_for(session_uuid,
                                                         work_id))
    checkpoint_id = (binding or {}).get("checkpoint_id")
    if not checkpoint_id:
        return None
    reconstructed = verification.reconstruct_checkpoint_state_with_liveness(
        session_uuid, checkpoint_id)
    state = reconstructed["state"]
    if state == "unknown":
        return None
    status_path_fn = {
        "pending": state_store.checkpoint_request_path_for,
        "claimed": state_store.checkpoint_claim_path_for,
        "terminal": state_store.checkpoint_receipt_path_for,
    }[state]
    receipt = reconstructed.get("receipt") or {}
    request = reconstructed.get("request") or {}
    # The label below is cosmetic only: render_handoff's own SLOT_LABELS
    # registry is what actually decides the descriptor line's label (never
    # a caller-supplied one) -- this module reaches into handoff internals
    # only through render_handoff's own artifact/facts contract, never by
    # reading handoff.SLOT_LABELS directly.
    artifacts = [{"label": "checkpoint status",
                 "path": status_path_fn(session_uuid, checkpoint_id),
                 "kind": "json", "source": "checkpoint_status"}]
    facts = {"role": role, "checkpoint_id": checkpoint_id,
            "checkpoint_state": state}
    if request.get("phase"):
        facts["checkpoint_phase"] = request["phase"]
    if receipt.get("verdict"):
        # Only a TERMINAL checkpoint has a verdict at all — a pending/
        # claimed checkpoint's fact stays omitted rather than a fabricated
        # None (cowork_handoff._FACT_SCHEMAS["checkpoint_verdict"] is a
        # closed enum with no null member).
        facts["checkpoint_verdict"] = receipt["verdict"]
    block = handoff.render_handoff(
        "cowork->role:checkpoint_wake", artifacts=artifacts, facts=facts,
        ctx={})
    # Additive channel (see LIVENESS above): always present, always one of
    # the classifier's own literals, never merged into `checkpoint_state`.
    # The rendered prose is byte-identical to what this route rendered
    # before — this attaches a fact the caller may read, and changes nothing
    # the role is shown.
    block.checkpoint_claim_liveness = reconstructed["claim_liveness"]
    return block


def _latest_verification_disposition(session_uuid, transaction_id,
                                     checkpoint_id=None):
    """The latest sidecar disposition value for one transaction id, or None.
    The sidecar is the reconciled read-through cache of the
    `verification.disposition` trace events (D-0001); render surfaces read it
    rather than replaying the trace.

    Checkpoint scope (additive, M5 Package D): when `checkpoint_id` is given,
    the SAME lookup is performed for a per-checkpoint claim instead — via
    `checkpoint_latest_disposition` — and `transaction_id` is ignored;
    every existing whole-transaction caller is unaffected since
    `checkpoint_id` defaults to `None`."""
    if checkpoint_id is not None:
        return checkpoint_latest_disposition(session_uuid, checkpoint_id)
    if not (session_uuid and transaction_id):
        return None
    entry = state_store.read_verification_dispositions(
        session_uuid).get(transaction_id)
    return (entry or {}).get("disposition")


def _emit_verification_disposition(session_uuid, trace, transaction_id,
                                   disposition, review_round=None,
                                   reviewed_manifest_digest=None,
                                   checkpoint_id=None, work_id=None):
    """Record ONE review disposition for an owned transaction (D-0001):
    PRIMARY the `verification.disposition` trace event, PLUS the reconciled
    sidecar entry written at the same moment, PLUS an in-place update of the
    current-receipt pointer's own `disposition` field when the pointer names
    this transaction (so a same-process render sees the new value without a
    re-join, D-0002).

    Checkpoint scope (additive, M5 Package D): when `checkpoint_id` is given,
    the SAME disposition vocabulary is recorded for a per-checkpoint claim
    instead — via `checkpoint_emit_disposition` — and `transaction_id`/
    `reviewed_manifest_digest` are ignored; every existing whole-transaction
    caller is unaffected since `checkpoint_id` defaults to `None`."""
    if checkpoint_id is not None:
        checkpoint_emit_disposition(
            session_uuid, trace, checkpoint_id, disposition,
            review_round=review_round, work_id=work_id)
        return
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


# Prefix of the substituted final-suite label code. Whitespace-free and short
# (19 chars + 16 hex = 35, far under `handoff._MAX_TOKEN`), so the substituted
# value is always a single content-free token.
_FINAL_SUITE_LABEL_CODE_PREFIX = "final_suite_sha256_"


def _content_free_final_suite_label(label):
    """The overlay's content-free rendering of a final-suite label
    (garusis/cowork-internal#45).

    `final_suite_label` is the ONE whole-transaction overlay field whose value
    is AUTHORED — a planner writes it in the verification inventory — while
    every other overlay key is a hex id, a closed enum, an int or a bool. An
    ordinary human phrase like `focused regression suite` is not a single
    token, so copying it verbatim onto the builder->build-reviewer edges made
    `handoff._assert_content_free` raise and aborted context assembly before
    the reviewer ever started. Sanitizing here — inside the ONE overlay
    renderer (D-0003), which already declares itself content-free — makes that
    declaration true for every consumer at once, without loosening the gate.

    - A label that ALREADY satisfies `handoff.is_content_free_token` (which
      covers `None`, and today's `legacy_unknown`) is returned unchanged, so
      every existing overlay renders byte-identically to before.
    - Anything else is replaced by a deterministic sha256 digest code of the
      label's own UTF-8 bytes. sha256 — never the builtin `hash()`, which is
      per-process salted — so the same label yields the same code in every
      process and across a resume, and so variants a naive slug would merge
      (`focused regression suite` vs `focused_regression_suite`) stay distinct.

    The AUTHORED label is never altered where it is reached by PATH: the
    transaction receipt, the current-receipt pointer, and the run report all
    keep the planner's own wording. Only the inline overlay fact carries the
    code. A non-str, non-token value is digested via `repr` rather than raised
    on — the overlay must never itself crash context assembly, which is the
    exact failure this function exists to remove."""
    if handoff.is_content_free_token(label):
        return label
    material = label if isinstance(label, str) else repr(label)
    return _FINAL_SUITE_LABEL_CODE_PREFIX + hashlib.sha256(
        material.encode("utf-8")).hexdigest()[:16]


def verification_overlay(pointer, disposition=None):
    """THE ONE overlay renderer (D-0003): a dict of content-free tokens derived
    from the current-receipt pointer (which itself carries only owned state —
    never a byte of agent prose). `disposition` is the render-time join onto
    the latest disposition known for the transaction (D-0002); absent, the
    pointer's own field is used. Returns None when there is no bound receipt.

    `final_suite_label` is the one authored value the pointer carries, so it
    goes through `_content_free_final_suite_label` on its way out: already-safe
    tokens pass through unchanged, and an authored phrase becomes a
    deterministic code. The authored wording itself stays verbatim in the
    receipt and the pointer, which reach the reviewer by absolute path.

    Checkpoint scope (additive, M5 Package D): a checkpoint pointer/binding
    carries `checkpoint_id`, never `transaction_id` — the two key spaces are
    disjoint by construction (`state_store.checkpoint_dir_for` vs.
    `state_store.verification_transaction_dir`), so a pointer naming a
    `checkpoint_id` is unambiguously routed to `checkpoint_overlay` instead,
    with no new parameter needed and no change to any whole-transaction
    caller's existing pointer shape or return value."""
    if isinstance(pointer, dict) and pointer.get("checkpoint_id"):
        return checkpoint_overlay(pointer, disposition=disposition)
    if not isinstance(pointer, dict) or not pointer.get("transaction_id"):
        return None
    return {
        "txn_id": pointer.get("transaction_id"),
        "manifest_digest": pointer.get("manifest_digest"),
        "index_digest": pointer.get("index_digest"),
        "verdict": pointer.get("verdict"),
        "final_suite_label": _content_free_final_suite_label(
            pointer.get("final_suite_label")),
        "final_suite_binding": pointer.get("final_suite_binding"),
        "command_count": pointer.get("command_count"),
        "disposition": (disposition or pointer.get("disposition")
                        or verification.DISPOSITION_PENDING_REVIEW),
        "contradiction": bool(pointer.get("contradiction")),
    }


def _current_verification_overlay(session_uuid, work_id=None):
    """`(overlay, pointer)` for the CURRENT bound receipt, or `(None, None)`.
    The disposition is joined at render time from the sidecar so a resumed
    reviewer edge mid-loop shows the CURRENT value rather than hardcoding
    `pending_review` (D-0002).

    Checkpoint scope (additive, M5 Package D): when `work_id` is given, the
    CURRENT checkpoint bound to that role engagement is resolved instead —
    via `checkpoint_current_overlay` — and `session_uuid` alone is used to
    key the whole-transaction pointer exactly as before whenever `work_id`
    is absent, so every existing caller (which never passes `work_id`) sees
    no change at all."""
    if work_id is not None:
        return checkpoint_current_overlay(session_uuid, work_id)
    pointer = state_store.read_current_receipt_pointer(session_uuid)
    if not isinstance(pointer, dict) or not pointer.get("transaction_id"):
        return None, None
    disposition = _latest_verification_disposition(
        session_uuid, pointer["transaction_id"])
    return verification_overlay(pointer, disposition=disposition), pointer


def render_verification_overlay_block(overlay, receipt_path=None,
                                      agent_status_path=None):
    """The review-surface banner block (UX-021): the owned facts first, the
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
    """The D-0004/D-0005 gate-outcome grant at the building-phase gate: the
    paired reviewer's explicit approve accepts the bound
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
                              trace=None, io_out=None, at=None,
                              closed_phases=None, session_factory=None):
    """The evaluation drain at one boundary, and its visible state.

    DELIBERATELY NOT INSIDE the best-effort measurement block that surrounds its
    caller: its exceptions must not be silently eaten by a swallow-all meant for
    measurement.

    Nothing here waits for a decision: the drain always proceeds under the
    effective policy. With nothing scoreable this is pure reconciliation rather
    than scoring: it is what records the policy-off holds, and what RETIRES
    superseded candidates. Returning early would mean a boundary whose only
    work is a retire set never retires anything — and session end is exactly
    such a boundary, since it closes every phase.
    """
    result = drain_evaluations(
        session_uuid, config=config, trace=trace, at=at,
        closed_phases=closed_phases, effective_policy=effective_policy,
        session_factory=session_factory)
    if any(result.get(key) for key in
           ("pending_running", "drained_total", "held", "terminal_total")):
        transcript.render_drain_state(io_out, effective_policy, result)
    return result


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


def _context_update_prefix(context_update, assets_dir=None, revision=None):
    """The resumed reviewer's wake prefix: un-acked context TEXT is rendered
    as a context-update block; an already-typed block (`run_flow` composed the
    wake block with a decision record for this reviewer) is used as is, so
    the answer never lands inside the context file."""
    if not context_update:
        return None
    if isinstance(context_update, handoff.HandoffBlock):
        return context_update
    return context_update_block(context_update, assets_dir, revision)


def assemble_reviewer_resume_context(intel_path, intel_md_path=None,
                                     context_update=None, assets_dir=None,
                                     context_revision=None):
    """Lighter context for a RESUMED reviewer session, delivered FILE-ONLY via
    the shared transport: its thread already holds the role + the prior context,
    so only the updated intel is sent (by path — JSON, and markdown when given).
    When the session context changed since the reviewer last acknowledged it
    (`context_update` is the un-acked context text), a context-update wake block
    referencing the persisted context FILE is prepended. No body is inlined."""
    prefix = _context_update_prefix(context_update, assets_dir,
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
# planner: the single lead of the planning phase, paired with the              #
# planning-advisor exactly as the scout pairs with the scout-reviewer. The      #
# planner writes TWO artifacts: a plan JSON (machine deliverable and status     #
# channel) and a readable plan MD (the review surface).                        #
# --------------------------------------------------------------------------- #


def assemble_planner_brief(plan_json_path, plan_md_path):
    """The planner's write-target instruction — its analogue of the scout brief.
    It names BOTH plan artifacts and nothing else."""
    return (
        "Write your plan as TWO files, to exactly these paths:\n"
        "  JSON (machine deliverable + your status channel): %s\n"
        "  Markdown (the readable review surface, small scannable sections): %s\n"
        "Those two plan files are your ONLY write targets. Do not create, edit, "
        "or delete any other file (reading/searching the repo is fine)."
        % (plan_json_path, plan_md_path)
    )


def assemble_planner_seed(intel_path, context, assets_dir=None,
                          context_revision=None):
    """The fresh planner's situational context (route 3), FILE-ONLY: the approved
    scout intel AND the shared session context, both carried by PATH via the
    shared transport. scout->planner is a cross-role handoff, so the context is
    persisted and referenced by path — never inlined (only the original
    orchestrator->scout prompt inlines context text)."""
    artifacts = [_shared_context_artifact(context, assets_dir, context_revision)]
    artifacts.extend(_intel_artifacts(intel_path))
    return handoff.render_handoff("scout->planner:seed", artifacts=artifacts)


def intel_updated_block(intel_path):
    """Wake block (route 3, resume) for a resumed planner after a hand-back round
    trip: the scout re-ran its full cycle and the scout-reviewer approved the UPDATED
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


# --------------------------------------------------------------------------- #
# builder: the single lead of the building phase, paired with the              #
# build-reviewer exactly as the scout pairs with the scout-reviewer and the     #
# planner with the planning-advisor. The builder edits the repository to        #
# execute the approved plan; its status JSON is a status channel + verification  #
# log, NOT a deliverable in itself.                                             #
# --------------------------------------------------------------------------- #


def assemble_builder_brief(build_status_path, build_summary_path=None):
    """The builder's status-file instruction. Unlike the scout/planner, the
    builder's write target is the WHOLE REPO (it edits source to execute the
    plan); the status file named here is only its status/verification channel,
    not a write restriction.

    When `build_summary_path` is given, the builder ALSO emits a readable
    markdown summary at its self-audit (when it marks ready_for_review): the
    review surface for the build, consistency-checked by the
    build-reviewer against the working-tree delta. It is a deliverable, not a
    write restriction (the builder still edits the whole repo)."""
    summary_note = ""
    if build_summary_path:
        summary_note = (
            "\nAt your self-audit, when you mark the build ready_for_review, also "
            "write a readable markdown summary of the build to exactly this "
            "file:\n  %s\n"
            "Cover, in small scannable sections: a TL;DR; the changes by file; "
            "the verification results; any issues & deviations from the plan; "
            "and anything left open. Keep it CONSISTENT with the actual "
            "working-tree changes and your status JSON." % build_summary_path
        )
    # No trailing newline: the brief is delivered as an exact static fragment
    # (`_role_seed_delivery`), which matches it against its own stripped form.
    return (
        "Write and keep current your status as a single JSON object to exactly "
        "this file:\n  %s\n"
        "That status file is your status + verification channel (status, "
        "handoff, and the result.verification log) — NOT a restriction on what "
        "you may edit. You execute the approved plan by editing the repository "
        "itself. Do NOT run any git commit or PR/branch tooling: approval ends "
        "the run and leaves the changes in the working tree for the "
        "orchestrator.%s"
        % (build_status_path, summary_note)
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
    planning-advisor approved the UPDATED plan. The plan is carried path-first via the shared
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


def _plan_artifacts(plan_json_path, plan_md_path):
    return [
        {"label": "plan JSON (machine source of truth)",
         "path": plan_json_path, "kind": "json", "source": "plan_json"},
        {"label": "plan markdown (the readable review surface)",
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
    prefix = _context_update_prefix(context_update, assets_dir,
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
        "convention, WITHOUT asking for input.\n\n"
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
                 io_out=None, session_factory=None,
                 claude_spawn=None, session_uuid=None, trace=None,
                 extra_writable_dir=None):
    """Spawn ONE agent (controller from --wt-controller) to create the worktree,
    then read back its status artifact. No reviewer, no gate (D4). Returns the
    parsed artifact dict (or None when the agent wrote nothing); the CALLER
    validates it deterministically via validate_worktree (D13).

    `wt_config` is the single-role config dict {controller, yolo, mode}. The
    role runs with execution enabled (yolo) so it can run `git worktree add`
    (D5). `session_factory` is injectable for tests."""
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
    transcript.notice(io_out, "worktree — creating a git worktree for this session\n"
              "name → %s\nbase → %s" % (name, base_toplevel))
    io_out.flush()

    if controller == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        if session_factory:
            session = session_factory("claude")
        else:
            ok, alert = bridge.probe_claude_stream_json(
                    spawn, mode=wt_config["mode"], yolo=wt_config["yolo"],
                    role_prompt_file=WORKTREE_PROMPT_PATH, trace=trace,
                    role=WORKTREE_ROLE, extra_writable_dir=extra_writable_dir,
                    cache_enabled=True)
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
        "Your discovery responsibility: determine WHICH of these git roots the "
        "ticket actually touches, and record the chosen subset in your intel "
        "(result.repos, with a `selected` flag per root, plus "
        "result.repo_discovery). When exactly ONE root was discovered (including "
        "an ancestor or fallback single-root outcome), take it as the set. With "
        "2+ candidates, decide from the context and record the choice in "
        "result.assumptions; use needs_input only when the context cannot "
        "decide it."
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


def write_build_baseline_manifest(session_uuid, cwd=None, checkpoint_id=None,
                                  artifact_kind=None):
    """Persist the PER-FILE build baseline (`build_baseline.json`).

    The prose baseline records a HEAD sha and a dirty flag, which is enough for a
    human and not enough for a measurement: a session that starts from a dirty
    tree — as this project's own runs do — has no commit describing what the
    build actually started from, so "what did this build change" measured against
    HEAD attributes someone else's uncommitted work to the builder.

    The manifest hashes every tracked file instead, so build and review metrics
    are computed against the tree as it actually was. Tolerant: any failure
    returns None and the run continues.

    `checkpoint_id`/`artifact_kind` (M5 Package E, additive, both default
    `None`): when a caller captures this baseline as part of a checkpoint's
    own before/after scope, stamping them onto the manifest correlates the
    two records — never changes this function's path/return contract."""
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
    if checkpoint_id:
        manifest["checkpoint_id"] = checkpoint_id
        manifest["artifact_kind"] = artifact_kind
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
                              verification_receipt_path=None,
                              checkpoint_receipt_path=None):
    arts = [
        {"label": "approved plan JSON (machine source of truth)",
         "path": plan_json_path, "kind": "json", "source": "plan_json"},
        {"label": "approved plan markdown", "path": plan_md_path,
         "kind": "markdown", "source": "plan_md"},
        {"label": "builder status JSON (status + verification log)",
         "path": build_status_path, "kind": "json", "source": "build_status"},
    ]
    if build_summary_path:
        arts.append({"label": "builder markdown summary (the readable review "
                     "surface)", "path": build_summary_path, "kind": "markdown",
                     "source": "build_summary"})
    if verification_receipt_path:
        # ORCH-050: the owned transaction's terminal result.json reaches the
        # reviewer by ABSOLUTE PATH as its own declared slot (optional, like
        # build_summary — a legacy session has none).
        arts.append({"label": "owned verification receipt (orchestrator-run "
                     "transaction result)", "path": verification_receipt_path,
                     "kind": "json", "source": "verification_receipt"})
    if checkpoint_receipt_path:
        # M5 Package E: populates D's already-declared, previously-unwired
        # `checkpoint_receipt` artifact slot (see `checkpoint_handoff_facts`
        # for the matching facts producer) — additive, D's own slot label in
        # cowork_handoff.SLOT_LABELS is untouched.
        arts.append({"label": "owned checkpoint receipt (orchestrator-run "
                     "checkpoint result)", "path": checkpoint_receipt_path,
                     "kind": "json", "source": "checkpoint_receipt"})
    return arts


def assemble_build_reviewer_context(context, selected, plan_json_path,
                                    plan_md_path, build_status_path,
                                    baseline_note="", baseline_repos=None,
                                    build_summary_path=None, assets_dir=None,
                                    context_revision=None,
                                    verification_receipt_path=None,
                                    verification_overlay=None,
                                    checkpoint_receipt_path=None,
                                    checkpoint_facts=None):
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
    derived owned-facts overlay as closed-schema edge facts. `checkpoint_
    receipt_path` + `checkpoint_facts` (M5 Package E — see
    `checkpoint_handoff_facts`) carry the bound checkpoint's terminal receipt
    the same way; both default to nothing bound, so a caller that never
    supplies them sees no change at all."""
    artifacts = [_shared_context_artifact(context, assets_dir, context_revision)]
    artifacts.extend(_build_reviewer_artifacts(
        plan_json_path, plan_md_path, build_status_path, build_summary_path,
        verification_receipt_path=verification_receipt_path,
        checkpoint_receipt_path=checkpoint_receipt_path))
    artifacts.append(_build_baseline_artifact(baseline_note, assets_dir))
    facts = {"team": list(selected or [])}
    if verification_overlay:
        facts.update(verification_overlay)
    if checkpoint_facts:
        facts.update(checkpoint_facts)
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
                                           verification_overlay=None,
                                           checkpoint_receipt_path=None,
                                           checkpoint_facts=None):
    """Lighter context for a RESUMED build-reviewer session, delivered FILE-ONLY
    via the shared transport: only the updated artifacts are sent by PATH (plan,
    status, summary, build-baseline) — plus a context-update wake block
    referencing the persisted context FILE when the session context changed. The
    full delta is still read live via the diff recipe; no body is inlined. The
    owned verification receipt + overlay ride exactly as on the fresh edge
    (ORCH-050), so a resumed reviewer never loses the receipt mid-loop. The
    bound checkpoint's receipt + facts (M5 Package E) ride the same way via
    `checkpoint_receipt_path`/`checkpoint_facts`."""
    artifacts = list(_build_reviewer_artifacts(
        plan_json_path, plan_md_path, build_status_path, build_summary_path,
        verification_receipt_path=verification_receipt_path,
        checkpoint_receipt_path=checkpoint_receipt_path))
    artifacts.append(_build_baseline_artifact(baseline_note, assets_dir))
    ctx = {"repos": list(baseline_repos or [])}
    if context_update:
        ctx["context_update_prefix"] = _context_update_prefix(
            context_update, assets_dir, context_revision)
    facts = {"team": []}
    if verification_overlay:
        facts.update(verification_overlay)
    if checkpoint_facts:
        facts.update(checkpoint_facts)
    return handoff.render_handoff(
        "builder->build-reviewer:review_resume",
        artifacts=artifacts, facts=facts, ctx=ctx)


def make_build_reviewer_runner(plan_json_path, plan_md_path, baseline_note="",
                               baseline_repos=None, trace=None,
                               extra_writable_dir=None, build_summary_path=None,
                               session_uuid=None, role_work_id=None):
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
    disposition current as of that render (D-0002). `role_work_id` (M5
    Package E) additionally wires the CURRENT bound checkpoint, if any, the
    same way via `checkpoint_handoff_facts`."""
    def receipt_kwargs():
        overlay, pointer = _current_verification_overlay(session_uuid)
        kwargs = {
            "verification_overlay": overlay,
            "verification_receipt_path": (
                pointer.get("receipt_path")
                if isinstance(pointer, dict) else None),
        }
        if session_uuid and role_work_id:
            checkpoint_facts = checkpoint_handoff_facts(
                session_uuid, role_work_id)
            if checkpoint_facts:
                kwargs["checkpoint_facts"] = checkpoint_facts
                kwargs["checkpoint_receipt_path"] = (
                    state_store.checkpoint_receipt_path_for(
                        session_uuid, checkpoint_facts["checkpoint_id"]))
        return kwargs

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
    a quiet sink so nothing reaches the transcript. On any failure or missing/malformed
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
    # When `surface_io_out` is set the REVIEW turn streams to the transcript
    # under the reviewer's own label; otherwise it
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


# Transcript notice producers (written through `transcript.notice`). Keyword
# substrings ("needs input", "ready for review", "scout finished") are part of
# the transcript tests assert on.


def scout_start_text(intel_path, resuming=False):
    if resuming:
        head = (
            "scout — resuming the saved session\n"
            "Continuing from the saved context. Pass --new for a fresh session "
            "or --context to redirect this one."
        )
    else:
        head = (
            "scout — gathering context\n"
            "Investigating and writing the intel; the scout-reviewer decides "
            "approval. An open question stops the run for the orchestrator."
        )
    return head + "\nintel → %s" % transcript.render_path(intel_path)


def scout_needs_input_text():
    return "scout needs input — stopping for the orchestrator"


def scout_review_text(intel_path):
    return "scout intel ready for review — %s" % transcript.render_path(intel_path)


def scout_done_text(intel_path):
    return "scout finished — intel → %s" % transcript.render_path(intel_path)


def planner_start_text(plan_md_path, resuming=False):
    if resuming:
        head = (
            "planner — resuming the saved planning session\n"
            "Continuing from the saved context."
        )
    else:
        head = (
            "planner — planning from the approved intel\n"
            "Drafting the plan; the planning-advisor decides approval. An open "
            "question stops the run for the orchestrator."
        )
    return head + "\nplan → %s" % transcript.render_path(plan_md_path)


def planner_needs_input_text():
    return "planner needs input — stopping for the orchestrator"


def planner_review_text(plan_md_path):
    return "plan ready for review — %s" % transcript.render_path(plan_md_path)


def planner_done_text(plan_md_path):
    return "planner finished — plan approved → %s" % transcript.render_path(plan_md_path)


def handoff_gate_text(payload):
    return ("planner wants to hand the work back to the scout\n"
            "handoff note:\n%s" % (payload or "").strip())


def builder_start_text(build_surface_path, resuming=False):
    if resuming:
        head = (
            "builder — resuming the saved build session\n"
            "Continuing from the saved context."
        )
    else:
        head = (
            "builder — building from the approved plan\n"
            "Making and verifying the changes; the build-reviewer decides "
            "approval. An open question stops the run for the orchestrator."
        )
    # The review surface is the build summary when one is wired (mirrors the
    # scout's intel.md / planner's plan.md start banner); falls back to the
    # status file when no summary path is given.
    return head + "\nsummary → %s" % transcript.render_path(build_surface_path)


def builder_needs_input_text():
    return "builder needs input — stopping for the orchestrator"


def builder_review_text(build_status_path, overlay=None,
                        receipt_path=None, agent_status_path=None):
    """The building-phase review-surface banner line, plus — when an owned
    verification receipt binds the current candidate (ORCH-050/UX-021) — the
    derived overlay block: owned facts, the separately-labeled agent prose
    pointer, and the contradiction warning. With no overlay the historical
    one-line banner is returned byte-identically."""
    text = "build ready for review — %s" % transcript.render_path(build_status_path)
    block = render_verification_overlay_block(
        overlay, receipt_path=receipt_path,
        agent_status_path=agent_status_path)
    return text + block if block else text


def builder_done_text(build_status_path):
    return ("builder finished — review your working tree → %s"
            % transcript.render_path(build_status_path))


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
# turn loop ended on a failure" — a turn that wrote no status, a controller
# failure, an unresolved no-op, an unusable reviewer, or a pre-loop refusal
# (manifest/policy/probe/start) all collapse to this ONE outward
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

# M4 Package D: the shared shutdown event `_handle_external_kill` (run_flow's
# real SIGTERM handler) sets FIRST, before its own durable `aborted` write --
# checked by every in-turn activity tick BEFORE it fires or appends, so a
# tick can never race a genuine external-kill's terminal durable write with
# a stale "still productive" append landing after it.
_ACTIVITY_SHUTDOWN_EVENT = threading.Event()

# M4 Package D: provider refusal/no-first-token, no-fallback termination
# (see `_role_loop`'s send-failure seam). One literal nonzero code, outside
# {0,1,2,130} (this file's own pre-existing rc/KeyboardInterrupt/EOFError
# literals) and outside 128..255 (every signal-exit range this file's own
# SIGTERM handler -- `128 + signum` -- and every real POSIX signal number
# already occupy).
PROVIDER_REFUSAL_EXIT_CODE = 17

# `_role_loop` outcome naming a run that terminated the WHOLE
# process (never merely ended one phase) because a send failure was a
# typed provider refusal or a first-token-deadline expiry, with no fallback
# available -- distinct from the ordinary `_OUTCOME_ENDED` (which leaves the
# process exit code at 0).
_OUTCOME_PROCESS_TERMINATED = "process_terminated"

# `_role_loop` outcome naming a phase that STOPPED without approval because
# it needs something this process cannot supply: an answer, an explicit
# authorization, or a usable reviewer. Never an approval; the phase is not
# advanced and the session stays resumable. `payload` is the structured
# request built by `_agent_stop_payload`.
_OUTCOME_STOPPED = "stopped"

# What a stop needs from the orchestrator (the closed `requires` vocabulary).
AGENT_STOP_REQUIREMENTS = ("answer", "authorization", "reviewer", "operator")


def _agent_stop_payload(kind, role, requires, **facts):
    """The structured, non-approving request a stopped phase reports.

    `kind` names the gate that stopped; `requires` is one of
    AGENT_STOP_REQUIREMENTS. `None`-valued facts are dropped so the record
    carries only what was actually observed."""
    if requires not in AGENT_STOP_REQUIREMENTS:
        raise ValueError("unknown stop requirement %r" % (requires,))
    out = {"kind": kind, "role": role, "requires": requires,
           "approved": False}
    for key, value in facts.items():
        if value is not None:
            out[key] = value
    return out


def _handoff_request_digest(note):
    """Content digest of one hand-back payload (traced beside the gate
    decision; the orchestrator-facing id is the decision request_id)."""
    return hashlib.sha256(
        str(note).encode("utf-8", "replace")).hexdigest()


_DECISION_VIEW_KEYS = ("request_id", "kind", "role", "phase", "requires",
                       "question", "handoff", "findings", "state")


def _decision_request_view(request):
    """The run-result `stop` view of a decision request record (or None)."""
    if not isinstance(request, dict):
        return None
    view = {key: request.get(key) for key in _DECISION_VIEW_KEYS
            if request.get(key) is not None}
    view["approved"] = False
    return view


def _decision_candidate_changed(request):
    """True when the artifact a stopped phase left behind no longer has the
    bytes it had when the request was opened: a response written for that
    state would be applied to different work."""
    path = (request or {}).get("status_path")
    expected = (request or {}).get("status_sha256")
    if not path:
        return False
    return state_store.fingerprint_status(path)["sha256"] != expected


def _decision_response_hint(request):
    kinds = state_store.DECISION_RESPONSES.get((request or {}).get("kind"), ())
    rid = (request or {}).get("request_id")
    if "answer" in kinds:
        return "--answer %s --context-file <answer>" % rid
    return " or ".join("--%s %s" % (k.replace("_", "-"), rid) for k in kinds)


HANDOFF_DECLINED_NOTE = (
    "[orchestrator decision] Your hand-back request to the %s was DECLINED. "
    "Do not hand back again for the same reason: continue your own phase "
    "with the approved inputs you already have. Your status file was moved "
    "from handoff_back to needs_input; rewrite it with the correct status "
    "when your turn ends.")


def _handoff_declined_fragment(to_role):
    """The one closed runtime note delivered to a lead whose hand-back the
    orchestrator declined."""
    return handoff._static_role_text(
        HANDOFF_DECLINED_NOTE % (to_role or "pre-processor"))


def decision_answer_block(answer_path):
    """The typed block (route 14) that delivers one orchestrator answer to the
    lead whose phase stopped on the request -- by PATH, never inlined."""
    return handoff.render_handoff(
        "orchestrator->lead:decision_answer",
        artifacts=[{"label": "answer", "path": os.path.abspath(answer_path),
                    "kind": "markdown", "source": "answer"}])


def decision_record_block(answers):
    """The typed block that makes every earlier orchestrator answer available
    beside the original brief, or None when the session has none."""
    paths = [a.get("answer_path") for a in answers or ()
             if a.get("answer_path")]
    if not paths:
        return None
    return handoff.render_handoff(
        "orchestrator->role:decision_record",
        artifacts=[{"label": "answer", "path": os.path.abspath(path),
                    "kind": "markdown", "source": "answer"}
                   for path in paths])


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
        "`result.pending_question`. Repair the status artifact now: if a "
        "decision that needs authority is truly required, record the exact, "
        "self-contained question in `result.pending_question` and keep "
        "`status: needs_input` (the run then stops for the orchestrator). If "
        "no such decision is required, finish the work and set "
        "`status: ready_for_review`." % artifact)


def _repair_prompt(artifact_noun):
    """The firm, role-parameterized instruction sent on the single automatic
    repair turn. It tells the role that
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


def _controller_failure_verdict(send_result=None, alert=None):
    out = {"malformed": True, "controller_failure": True}
    if send_result:
        out["controller_failure_result"] = dict(send_result)
    if alert:
        out["controller_failure_alert"] = alert
    return out


# =========================================================================== #
# M4 Package D: the activity-emission seam.                                   #
#                                                                              #
# This block is the ONLY production owner of WHEN a `cowork_activity.         #
# ActivityRecord` is appended: at a role's real turn boundary (see            #
# `_role_loop`'s retained `_send` call), and at bounded, scheduled in-turn    #
# ticks while that same send is still blocking. It invokes Package C's        #
# `classify_<controller>_activity` (never reclassifies independently),        #
# Package C's pinned `live_child_handle` (via `cowork_watchdog.process_       #
# probe`, never re-derived here), and Package B's `append_activity_record`.   #
#                                                                              #
# A tick durably records visibility ONLY -- it performs zero `io_out`         #
# writes, launches no controller turn/subprocess, and is not M5.5 fan-out:    #
# `_run_activity_tick_loop` calls nothing but the caller-supplied `fire()`,   #
# which itself only classifies already-observed process-liveness evidence    #
# and appends. Tick lifecycle is `try/finally`: `_role_loop` creates the      #
# daemon thread immediately before its retained `_send` call and closes it    #
# (`tick_stop_event.set()` + a BOUNDED `join`) in the `finally` clause         #
# wrapping that same call, regardless of how the call returns. The shared     #
# `_ACTIVITY_SHUTDOWN_EVENT` (set FIRST by `_handle_external_kill` on a real  #
# SIGTERM) is checked immediately before every tick fire AND immediately      #
# before the durable append itself, closing the post-SIGTERM append race.    #
# =========================================================================== #

_ACTIVITY_TICK_INTERVAL_SECONDS = 5.0

_ACTIVITY_CLASSIFIERS = {
    "claude": bridge.classify_claude_activity,
    "codex": bridge.classify_codex_activity,
    "opencode": bridge.classify_opencode_activity,
}

# The best HONEST evidence-kind label a genuinely successful turn's
# flattened `send_result` supports from OUTSIDE the streaming loop (no
# per-event/tool-call granularity is visible at this layer -- see
# `_turn_boundary_activity_evidence`'s own docstring): each controller's
# OWN real text-carrying kind, so `classify_<controller>_activity` maps it
# to `productive_model_work` through its real, unmodified kind table
# rather than degrading to `provider_wait` for want of a recognized kind.
_ACTIVITY_SUCCESS_KIND_BY_CONTROLLER = {
    "claude": "assistant", "codex": "message", "opencode": "message",
}


def _run_activity_tick_loop(stop_event, fire, interval_seconds=None):
    """The bounded in-turn activity-visibility daemon tick loop.

    Fires `fire()` every `interval_seconds` until `stop_event` is set (the
    per-turn signal `_role_loop`'s own `finally` clause sets once the send
    it is ticking for returns) OR the shared `_ACTIVITY_SHUTDOWN_EVENT` is
    set (a real external kill) -- whichever comes first. `stop_event.wait`
    IS the sleep: a set event returns immediately (`True`) rather than
    blocking the full interval, so teardown is prompt, not merely bounded
    by the next tick's timeout. Performs no I/O itself beyond calling
    `fire()` -- see its own docstring for why `fire()`, too, never touches
    `io_out` or launches anything."""
    interval = (_ACTIVITY_TICK_INTERVAL_SECONDS if interval_seconds is None
               else interval_seconds)
    while True:
        if stop_event.wait(timeout=interval):
            return
        if _ACTIVITY_SHUTDOWN_EVENT.is_set():
            return
        fire()


def _turn_boundary_activity_evidence(controller, send_result):
    """Reconstruct the smallest real evidence dict `cowork_bridge.classify_
    <controller>_activity` recognizes from `send_result`'s own flattened
    fields -- the identical honest-reconstruction discipline `_synthesize_
    raw_failure_evidence` already uses for M3's own failure classification
    (see its docstring): this never fabricates a discriminant `send_result`
    does not itself carry. A genuinely successful turn's evidence kind is
    the best HONEST label available from outside the streaming loop (see
    `_ACTIVITY_SUCCESS_KIND_BY_CONTROLLER`'s own note) -- never a claim of
    tool-call-level granularity this layer does not have."""
    if not isinstance(send_result, dict):
        return {"kind": None}
    if send_result.get("result") == "no_first_token":
        return {"kind": "no_first_token"}
    if send_result.get("denied") is True:
        return {"kind": "denied"}
    if not send_result.get("ok", True):
        return {"kind": "error", "is_error": True}
    kind = _ACTIVITY_SUCCESS_KIND_BY_CONTROLLER.get(controller, "assistant")
    return {"kind": kind, "text": str(send_result.get("result") or "ok")}


def _emit_activity_record(session_uuid, work_id, session, evidence,
                          turn_started_monotonic, trace=None, role=None):
    """The activity-emission seam's sole append point: classify `evidence`
    via Package C's `classify_<controller>_activity`, durably append the
    resulting ActivityRecord via Package B's `append_activity_record`.
    Returns the durably stored, normalized record, or `None` when nothing
    was appended (no session/work identity, an unrecognized controller, the
    shared shutdown event is set, or any append failure).

    Best-effort: any `ValueError`/`OSError` is swallowed, exactly like
    `_record_provider_health`'s own best-effort discipline -- observability
    must never block or break the turn it is observing. Checks the shared
    `_ACTIVITY_SHUTDOWN_EVENT` immediately before appending, so a
    post-SIGTERM append can never race the external-kill handler's own
    durable `aborted` write."""
    if not session_uuid or not work_id:
        return None
    controller = getattr(session, "controller", None)
    classify = _ACTIVITY_CLASSIFIERS.get(controller)
    if classify is None:
        return None
    try:
        activity_class = classify(evidence)
        age_seconds = max(0.0, time.monotonic() - turn_started_monotonic)
        record = {
            "schema_version": activity_contracts.SCHEMA_VERSION,
            "record": "ActivityRecord", "work_id": work_id,
            "time": _capacity_now(),
            "activity_class": activity_class, "source": controller,
            "artifact_fingerprint": None, "artifact_delta": [],
            "provider_health": None, "age_seconds": age_seconds,
        }
        if _ACTIVITY_SHUTDOWN_EVENT.is_set():
            return None
        stored = state_store.append_activity_record(session_uuid, record)
    except (ValueError, OSError):
        return None
    if trace:
        trace.event("activity.recorded", role=role, work_id=work_id,
                    activity_class=activity_class, source=controller)
    return stored


_ACTIVITY_REVIEW_INTERVAL_SECONDS = 300


def _ensure_scheduled_review(session_uuid, work_id, now_iso,
                             activity_class=None):
    """Durably (re)establish issue #58's authoritative `next_inspection_at`
    for `work_id` -- Package B owns the STORE (`write_scheduled_review`);
    this activity-emission seam owns the cadence POLICY. Best-effort: any
    failure returns `None` rather than raising.

    M4D-MAJ-02: the PRIOR schedule is read and evaluated BEFORE any
    refresh decision -- unconditionally pushing `next_inspection_at`
    further into the future on every single turn (including a terminal
    one) would make an overdue review structurally unreachable, silently
    defeating issue #58's hard-stall detection. The policy:
      - `activity_class` is a NON-terminal, real/positive class (
        `productive_model_work`/`local_tool_work`/`owned_verification`/
        `provider_wait`/`policy_denial`) -> genuine progress legitimately
        defers the next check; refresh/extend to `now + interval`, exactly
        as before.
      - `activity_class` is TERMINAL (`process_crash`/`hung_descendant`/
        `no_evidence_silence`) and a prior schedule already exists -> NEVER
        extend it; the existing (possibly already-overdue) schedule stands
        unchanged, so `review_due` can go True on its own timing.
      - `activity_class` is TERMINAL and NO prior schedule exists yet (the
        very first turn recorded for this work_id was itself terminal) ->
        due for review IMMEDIATELY (`next_inspection_at = now`), never
        deferred a full interval -- the first sign of trouble is due for a
        watchdog look at once, not after an arbitrary wait.
    """
    if not session_uuid or not work_id:
        return None
    try:
        prior = state_store.read_next_inspection(session_uuid, work_id)
        terminal = activity_class in watchdog.TERMINAL_ACTIVITY_CLASSES
        if terminal and prior is not None:
            return prior
        if terminal:
            next_at = now_iso
        else:
            now_dt = datetime.datetime.fromisoformat(
                now_iso.replace("Z", "+00:00"))
            next_at = (now_dt + datetime.timedelta(
                seconds=_ACTIVITY_REVIEW_INTERVAL_SECONDS)).isoformat(
                ).replace("+00:00", "Z")
        record = {
            "schema_version": activity_contracts.SCHEMA_VERSION,
            "record": "ScheduledReviewRecord", "work_id": work_id,
            "next_inspection_at": next_at,
            "interval_seconds": _ACTIVITY_REVIEW_INTERVAL_SECONDS,
            "last_inspection_result_ref": None,
        }
        return state_store.write_scheduled_review(session_uuid, record)
    except (ValueError, OSError):
        return None


def _reconcile_before_presentation(session_uuid, work_id,
                                   reconciled_classification, trace=None):
    """Call Package B's `reread_before_gate` immediately before an actual
    stall/retry/invalidation presentation, retaining ORIGINAL plus
    RECONCILED classification durably whenever the two genuinely disagree
    (a no-op, by `reread_before_gate`'s own law, when they do not). Best-
    effort/non-blocking: any failure never blocks the presentation itself,
    and a missing prior ActivityRecord (`ValueError`) is exactly as
    harmless a no-op as an identical re-read."""
    if not session_uuid or not work_id:
        return None
    now = _capacity_now()
    digest = hashlib.sha256(
        ("%s:%s:%s" % (work_id, reconciled_classification, now))
        .encode("utf-8")).hexdigest()
    try:
        reconciliation = state_store.reread_before_gate(
            session_uuid, work_id, now, reconciled_classification,
            revision_digest=digest, quiescence_marker="gate_presentation")
    except (ValueError, OSError):
        return None
    if reconciliation is not None and trace:
        trace.event("activity.reconciled", work_id=work_id,
                    original=reconciliation["original_classification"],
                    reconciled=reconciliation["reconciled_classification"])
    return reconciliation


def _hung_descendant_ps_evidence(session, effective_classification):
    """M4D-MAJ-02: wire REAL independent `ps`/orphan evidence -- never
    fabricated -- when the CURRENT effective classification is
    `hung_descendant`. Uses Package C's own pinned `live_child_handle`
    (never re-derived): a pid genuinely still referenced by the session
    object is checked via `cowork_watchdog.independent_hung_descendant_
    evidence`'s real, bounded `ps` invocation; the common same-turn
    `no_first_token` case (where Package C's own bounded first-token
    deadline mechanism already terminated AND REAPED the child before this
    seam ever runs) correctly yields no pid and therefore no evidence --
    honest either way, never a guess. Returns `None` for every other
    classification (no ps check is even attempted)."""
    if effective_classification != "hung_descendant":
        return None
    proc = bridge.live_child_handle(session)
    pid = getattr(proc, "pid", None) if proc is not None else None
    if pid is None:
        return None
    return watchdog.independent_hung_descendant_evidence(pid)


def _watchdog_decision_for_presentation(session_uuid, work_id, session,
                                        trace=None, role=None):
    """M4 Package D's sole `cowork_watchdog.decide` call site consumed by
    `_role_loop`'s gate presentations: reads the CURRENT durable
    (activity_record, reconciliation_record) pair and the CURRENT
    `ScheduledReviewRecord` (both Package B reads), then combines them with
    a genuine live process probe (`session`) via `cowork_watchdog.decide` --
    dual evidence, never elapsed time or the durable record alone. Also
    attempts REAL independent hung-descendant `ps` evidence (see
    `_hung_descendant_ps_evidence`) whenever the effective classification
    is `hung_descendant`, so `hard_stall_eligible` is genuinely reachable
    through that path when the evidence actually supports it. Returns
    `None` when there is no durable activity to decide from yet (nothing to
    warn about) or on any read/validation failure -- never raises, and
    never blocks the presentation it precedes."""
    if not session_uuid or not work_id:
        return None
    try:
        current = state_store.latest_activity(session_uuid, work_id)
        if current is None:
            return None
        schedule_record = state_store.read_next_inspection(
            session_uuid, work_id)
        effective_class = (
            current["reconciliation_record"]["reconciled_classification"]
            if current["reconciliation_record"] is not None
            else current["activity_record"]["activity_class"])
        hung_ps_evidence = _hung_descendant_ps_evidence(
            session, effective_class)
        decision = watchdog.decide(
            work_id, _capacity_now(), current["activity_record"],
            current["reconciliation_record"], schedule_record,
            session=session, hung_ps_evidence=hung_ps_evidence)
    except (ValueError, OSError):
        return None
    if trace:
        trace.event("watchdog.decision", role=role, work_id=work_id,
                    verdict=decision["verdict"],
                    durable_evidence_ref=decision["durable_evidence_ref"],
                    process_probe_ref=decision["process_probe_ref"])
    return decision


def _render_activity_snapshot(io_out, activity_record, decision,
                              schedule_record):
    """D's sole output-arbitration call site: `transcript.render_activity`,
    called ONLY here -- at the retained turn boundary, strictly after
    `_send()` has returned. Never called from an in-turn tick. Best-
    effort: a malformed/incomplete input never raises past this point --
    a broken activity snapshot must never break the turn it decorates."""
    if activity_record is None or decision is None or schedule_record is None:
        return
    try:
        compact_state = activity_contracts.project_compact_state(
            activity_record, decision, schedule_record)
        transcript.render_activity(io_out, compact_state)
    except (ValueError, TypeError):
        pass


def _role_loop(session, first, status_path, context, io_out,
               role="scout", review_fn=None, trace=None,
               reviewer_role=SCOUT_REVIEWER,
               needs_input_text=scout_needs_input_text,
               review_text=scout_review_text,
               done_text=scout_done_text,
               artifact_noun="intel",
               handoff_enabled=False,
               handoff_gate_text_fn=handoff_gate_text,
               evaluate_fn=None, skip_baseline=None, context_revision=None,
               phase=None, is_resume=False, seed_artifact_paths=None,
               on_first_send_accepted=None, on_first_send_rejected=None,
                  require_pending_question=False, review_path=None,
                  save_pending_turn_fn=None, clear_pending_turn_fn=None,
                  spath=None, session_uuid=None, build_summary_path=None,
                  role_work_id=None, checkpoint_id=None, artifact_kind=None):
    """Drive a lead role's per-turn loop: send → read status → review, stop,
    or finish. Role-generic: the scout, planner and builder all run on this
    loop, differing only in banners, status file, paired reviewer, and whether
    the hand-back contract is enabled. Nothing here reads human input.

    Returns `(rc, outcome, payload)` where outcome is one of:
      - "approved": the paired reviewer explicitly approved at the
        `ready_for_review` gate (or its hash-gate approval was carried).
      - "stopped": the phase needs an answer, an authorization, or a usable
        reviewer; `payload` is the structured `_agent_stop_payload` request.
        Never an approval.
      - "ended": the phase ended on a failure (controller failure, stale
        no-op); `payload` names the failure when one was observed.
      - "interrupted": a signal interrupted the run.
      - "awaiting_capacity" (M3 Package E): a genuine quota/overload send
        failure durably entered `awaiting_capacity` (CapacityPacket +
        PauseLease persisted, pending turn persisted-then-acknowledged);
        `payload` carries the role/provider/candidate/controller-policy/
        model/effort binding. The caller (`run_flow`) treats this like any
        other unrecognized outcome -- it ends this run cleanly; resumption
        happens out-of-process via the resume-trigger CLI once the
        lease is genuinely eligible.

    When `review_fn` is provided (the paired reviewer is on the team), each
    `ready_for_review` runs the reviewer (topology D):
    `review_fn(status_path, round_index)` returns a verdict dict
    {verdict, findings, user_question}. Only `approve` approves. `revise` hands
    back to the role, bounded by REVIEW_ROUND_CAP rounds, after which the
    phase stops with the dissent attached. A reviewer question, a reviewer
    that fails REVIEW_FAIL_CAP times, and a missing reviewer all stop.

    When `evaluate_fn(session, verdict, round_index)` is provided, it runs
    right after each verdict readback and BEFORE branching on the verdict kind
    — one seam that covers approve, revise, needs_user, and round-cap rounds
    identically. It is purely observational: failures are traced and skipped.

    When `handoff_enabled`, a `handoff_back` status with a payload is an
    authority request: the phase stops with a `handoff_requested` request and
    the status is left untouched (`run_flow` executes or declines it only on
    an explicit orchestrator decision). A `handoff_back` without a payload
    degrades to the needs-input stop (never an implicit hand-back).

    `checkpoint_id`/`artifact_kind` (M5 Package E, additive, both default
    `None`): when the caller has already dispatched a deterministic
    checkpoint for this role engagement (`dispatch_role_checkpoint`), this
    traces the binding so a resume/audit trail can correlate this loop
    invocation with the checkpoint that gates it -- never changes this
    function's own control flow or return value; a caller that never
    supplies them sees no change at all."""
    if checkpoint_id and trace:
        trace.event("checkpoint.role_loop_bound", role=role,
                    checkpoint_id=checkpoint_id, artifact_kind=artifact_kind)
    # `_role_loop` is the actual initial lead boundary. Production callers
    # already pass a typed seed; direct/test callers enter through this one
    # closed initial-context constructor rather than a generic lead fallback.
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
    # Stale-no-op repair state: True between the firing of the single automatic
    # repair turn and its result (or an explicit machine re-invocation). When True,
    # the next send is re-checked for a no-op even without a fresh reopen.
    in_repair = False
    repair_reason = None  # reopen reason carried into the repair/escalation
    repair_ordinal = 0  # increments each time _repair_delivery is actually dispatched
    send_start_event_id = None  # trace event ID from the most recent role.send.start
    last_send_source_ref = None  # source_ref built for the most recent send
    review_rounds = 0
    # Consecutive reviewer turns with no usable verdict (reset by a usable
    # verdict).
    review_failures = 0
    # A real wrapper may require needs_input artifacts to record the question.
    # One automatic repair is enough; a second malformed turn becomes an
    # explicit diagnostic at the input gate instead of an invisible loop.
    missing_question_repairs = 0
    outcome_kind = _OUTCOME_ENDED
    payload = None

    def _breaker_cause():
        """The durable recovery breaker's cause key for this engagement --
        (ledger, role, config_digest, controller, candidate) -- or None when
        there is no session/proven manifest to key a genuine fingerprint on
        (the breaker is silent rather than fabricating one). Keyed on the
        SAME manifest digest `_complete_phase` binds as the candidate."""
        if not session_uuid:
            return None
        manifest = dispatch_manifest.load_manifest(
            state_store.manifest_path_for(session_uuid, role))
        config_digest = ((manifest or {}).get("binding") or {}).get(
            "config_digest")
        if not config_digest:
            return None
        return (state_store.ledger_path_for(session_uuid), role,
                config_digest, getattr(session, "controller", None) or
                "unknown", (manifest or {}).get("digest"))

    def _breaker_record(reason):
        """Durably count one failed attempt at this exact cause. A later
        machine re-invocation of the same cause is refused before dispatch
        once the budget is spent (see the pre-send check below)."""
        cause = _breaker_cause()
        if cause is None:
            return None
        decision = recovery_breaker.attempt(*(cause + (reason,)))
        if trace:
            trace.event("recovery.breaker.decision", role=role, reason=reason,
                        fingerprint=decision["fingerprint"],
                        attempt_count=decision["attempt_count"],
                        threshold=decision["threshold"],
                        tripped=decision["tripped"])
        return decision

    def _breaker_exhausted(reason):
        cause = _breaker_cause()
        if cause is None:
            return None
        history = recovery_breaker.history(*(cause + (reason,)))
        if len(history) >= recovery_breaker.TRIP_THRESHOLD:
            return {"attempts": len(history),
                    "threshold": recovery_breaker.TRIP_THRESHOLD}
        return None

    breaker_checked = False

    def _end_unapproved(stop):
        """Record a non-approving end. A request for an answer or an
        authorization is a genuine authority wait (`needs_authority`, outcome
        "stopped"); an unusable/absent reviewer or a broken turn is a failure
        (`failed`, outcome "ended") -- the two are never conflated."""
        authority = stop["requires"] in ("answer", "authorization")
        _advance_phase(
            session_uuid, role_work_id,
            "capability_missing" if authority else "execution_failed",
            evidence={"reason": stop["kind"]}, source="gate.runtime")
        return (_OUTCOME_STOPPED if authority else _OUTCOME_ENDED), stop

    try:
        # Controller-switch packets are controller-only recovery context.  The
        # switch itself already has a compact transcript status line; echoing
        # this packet here would dump artifacts and orchestration markup into
        # the transcript as though it were the run context.
        internal_switch_context = context.lstrip().startswith(
            handoff.SWITCH_HANDOFF_MARKER)
        if context.strip() and not internal_switch_context:
            io_out.write(transcript.CONTEXT_LABEL + context.strip() + "\n")
            io_out.flush()
        while True:
            if not breaker_checked:
                # A machine re-invocation of an engagement whose identical
                # cause (same role, config, controller and candidate) has
                # already failed TRIP_THRESHOLD times is refused BEFORE any
                # paid dispatch; a different controller or config is a
                # different cause with its own budget.
                breaker_checked = True
                exhausted = _breaker_exhausted("controller_failure")
                if exhausted:
                    if trace:
                        trace.event("gate.decision", decider="runtime",
                                    role=role, gate="recovery_budget",
                                    action="refuse", **exhausted)
                    if on_first_send_rejected:
                        on_first_send_rejected()
                    outcome_kind, payload = _end_unapproved(
                        _agent_stop_payload(
                            "recovery_budget_exhausted", role,
                            requires="operator",
                            controller=getattr(session, "controller", None),
                            **exhausted))
                    break
            # Capture the reopen signal BEFORE the invalidate/reset block runs.
            reopened_this_turn = pending_reopen_reason is not None
            reopen_reason_this_turn = pending_reopen_reason
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
            # M4 Package D: the activity-emission seam's bounded in-turn
            # daemon tick, ticking ONLY while this real turn-boundary send
            # is in flight -- created/closed in try/finally, bounded join.
            turn_started_monotonic = time.monotonic()
            tick_stop_event = threading.Event()
            tick_thread = None
            if session_uuid and role_work_id and getattr(
                    session, "controller", None) in _ACTIVITY_CLASSIFIERS:
                def _fire_tick(_session=session, _work_id=role_work_id,
                              _started=turn_started_monotonic):
                    if (_ACTIVITY_SHUTDOWN_EVENT.is_set()
                            or tick_stop_event.is_set()):
                        return
                    _emit_activity_record(
                        session_uuid, _work_id, _session, {"kind": "tick"},
                        _started, trace=trace, role=role)
                tick_thread = threading.Thread(
                    target=_run_activity_tick_loop,
                    args=(tick_stop_event, _fire_tick), daemon=True)
                tick_thread.start()
            try:
                send_result = _send(
                    session, delivery, meta=lead_meta)
            finally:
                tick_stop_event.set()
                if tick_thread is not None:
                    tick_thread.join(timeout=2.0)
            if trace:
                trace.event("role.send.end", role=role,
                            ok=bool(send_result.get("ok", True)),
                            result=send_result.get("result"),
                            error_type=send_result.get("error_type"),
                            subtype=send_result.get("subtype"))
            # M4 Package D: the activity-emission seam's real turn-boundary
            # append (Package C's classify_<controller>_activity on this
            # turn's real, flattened evidence, durably appended via Package
            # B) and D's sole output-arbitration call site: the activity
            # snapshot is written ONLY here, after the send has returned.
            turn_activity_record = _emit_activity_record(
                session_uuid, role_work_id, session,
                _turn_boundary_activity_evidence(
                    getattr(session, "controller", None), send_result),
                turn_started_monotonic, trace=trace, role=role)
            if turn_activity_record is not None:
                turn_schedule_record = _ensure_scheduled_review(
                    session_uuid, role_work_id, turn_activity_record["time"],
                    activity_class=turn_activity_record["activity_class"])
                turn_watchdog_decision = _watchdog_decision_for_presentation(
                    session_uuid, role_work_id, session, trace=trace,
                    role=role)
                _render_activity_snapshot(
                    io_out, turn_activity_record, turn_watchdog_decision,
                    turn_schedule_record)
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
                # M3 Package E: classify this send failure via C's pure
                # taxonomy and durably record ProviderHealth for EVERY
                # classification, including the explicit
                # `unknown_provider_failure` member -- never skipped just
                # because it is unclassifiable.
                controller_name = getattr(session, "controller", None)
                raw_evidence = _synthesize_raw_failure_evidence(
                    controller_name, send_result)
                controller_outcome = _classify_raw_failure(
                    controller_name, raw_evidence)
                _record_provider_health(
                    session_uuid, role, controller_name, controller_outcome,
                    _capacity_now())
                # M4 Package D: reconcile + decide immediately before the
                # actual stall/retry/invalidation presentation below (the
                # capacity-entry write or the structured end/termination)
                # -- retains
                # original plus reconciled classification durably, and
                # combines durable evidence with a live process probe
                # before any terminal-leaning watchdog verdict.
                _classify_for_reconcile = _ACTIVITY_CLASSIFIERS.get(
                    controller_name)
                if _classify_for_reconcile is not None:
                    _reconcile_before_presentation(
                        session_uuid, role_work_id,
                        _classify_for_reconcile(
                            _turn_boundary_activity_evidence(
                                controller_name, send_result)),
                        trace=trace)
                _watchdog_decision_for_presentation(
                    session_uuid, role_work_id, session, trace=trace,
                    role=role)
                if controller_outcome in capacity_contracts.CAPACITY_ELIGIBLE_OUTCOMES:
                    # quota_limited/overloaded: a genuine provider-capacity
                    # signal must never auto-retry the SAME provider (the
                    # frozen brief's invariant) -- attempt a durable
                    # awaiting-capacity entry BEFORE falling through to the
                    # ordinary controller-failure end below.
                    # `provider_session_id`
                    # is sourced from THIS session's own durable resume
                    # state (`_durable_provider_session_id`), never an
                    # in-process session object attribute -- works
                    # identically for a fresh dispatch and a resumed one.
                    capacity_payload = _enter_awaiting_capacity(
                        session_uuid, role_work_id, role, controller_name,
                        (_durable_provider_session_id(
                            session_uuid, role, controller_name)
                         or getattr(session, "session_id", None)
                         or getattr(session, "thread_id", None)),
                        controller_outcome, str(pending),
                        getattr(session, "model", None),
                        getattr(session, "effort", None),
                        raw_evidence=raw_evidence,
                        # Only the FIRST send carries the launch's decision
                        # blocks (a later send follows an accepted, already
                        # acknowledged one).
                        decision_bindings=(
                            _decision_launch_bindings(session_uuid, role)
                            if first_send else None))
                    if capacity_payload is not None:
                        if trace:
                            trace.event(
                                "capacity.awaiting", role=role, phase=phase,
                                controller_outcome=controller_outcome,
                                package_id=capacity_payload.get("package_id"),
                                lease_id=capacity_payload.get("lease_id"))
                        io_out.write(
                            "cowork: %s is awaiting provider capacity (%s) "
                            "-- durably paused; resume via the capacity "
                            "resume-trigger once eligible.\n"
                            % (role, controller_outcome))
                        io_out.flush()
                        if first_send and on_first_send_rejected:
                            on_first_send_rejected()
                        outcome_kind = "awaiting_capacity"
                        payload = capacity_payload
                        break
                # Agent-only: there is no in-process recovery choice. A controller
                # failure ends the phase with a structured, non-approving
                # outcome; recovery is a machine re-invocation (resume, or
                # --switch-controller) by the orchestrator.
                #
                # M4 Package D: a run has no fallback at all -- so a TYPED
                # provider refusal or a
                # first-token-deadline expiry (never mere elapsed time or
                # an event tail: `no_first_token` is real, positive
                # evidence the deadline mechanism itself observed and
                # reaped) terminates the WHOLE PROCESS nonzero, naming
                # the provider reason, rather than quietly ending only
                # this phase. Every other send failure ends the phase with
                # the structured controller-failure outcome below.
                _turn_outcome = send_result.get("controller_turn_outcome")
                _no_fallback_terminal = (
                    send_result.get("result") == "no_first_token"
                    or (isinstance(_turn_outcome, dict)
                        and _turn_outcome.get("outcome") == "refused"))
                if _no_fallback_terminal:
                    _termination_reason = (
                        (_turn_outcome or {}).get("failure_class")
                        or send_result.get("result") or "no_first_token")
                    io_out.write(
                        "cowork: run terminating -- %s (%s), "
                        "no fallback available.\n"
                        % (controller_name or "controller",
                           _termination_reason))
                    io_out.flush()
                    if trace:
                        trace.event(
                            "gate.decision", decider="runtime", role=role,
                            gate="controller_failure",
                            action="terminate_process",
                            reason=_termination_reason)
                    _advance_phase(
                        session_uuid, role_work_id, "execution_failed",
                        evidence={
                            "reason": "send_failed",
                            "terminates_process": True,
                            "termination_reason": _termination_reason},
                        source="gate.runtime")
                    if first_send and on_first_send_rejected:
                        on_first_send_rejected()
                    outcome_kind = _OUTCOME_PROCESS_TERMINATED
                    payload = {
                        "exit_code": PROVIDER_REFUSAL_EXIT_CODE,
                        "role": role, "controller": controller_name,
                        "reason": _termination_reason}
                    break
                if trace:
                    trace.event("gate.decision", decider="runtime", role=role,
                                gate="controller_failure", action="end")
                _breaker_record("controller_failure")
                _advance_phase(
                    session_uuid, role_work_id, "execution_failed",
                    evidence={"reason": "send_failed"},
                    source="gate.runtime")
                if first_send and on_first_send_rejected:
                    on_first_send_rejected()
                outcome_kind = _OUTCOME_ENDED
                payload = _agent_stop_payload(
                    "controller_failure", role, requires="operator",
                    controller=controller_name,
                    controller_outcome=controller_outcome,
                    status_path=status_path)
                break
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
                # Second consecutive no-op: the automatic repair failed. End
                # with a structured failure instead of looping forever.
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
                # Agent-only: the bounded automatic repair already failed, so
                # the phase ends with a structured, non-approving outcome
                # rather than waiting on an inspect/retry choice.
                if trace:
                    trace.event("gate.decision", decider="runtime", role=role, gate="stuck",
                                action="end")
                _advance_phase(
                    session_uuid, role_work_id, "execution_failed",
                    evidence={"reason": "stale_noop"},
                    source="gate.runtime")
                outcome_kind = _OUTCOME_ENDED
                payload = _agent_stop_payload(
                    "stale_noop", role, requires="operator",
                    status_path=status_path, reopen_reason=repair_reason)
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
                    transcript.notice(io_out, handoff_gate_text_fn(note))
                    # A hand-back is a cross-phase authority request. The
                    # phase stops with a structured request and the status is
                    # left untouched; only an explicit orchestrator decision
                    # on a later invocation (--authorize-handoff or
                    # --decline-handoff naming the recorded request) acts on
                    # it.
                    handoff_digest = _handoff_request_digest(note)
                    if trace:
                        trace.event("gate.decision", decider="runtime",
                                    role=role, gate="handoff_back",
                                    action="stop",
                                    payload_digest=handoff_digest)
                    outcome_kind, payload = _end_unapproved(_agent_stop_payload(
                        "handoff_requested", role, requires="authorization",
                        status_path=status_path, handoff=note,
                        payload_digest=handoff_digest,
                        to_role=HANDBACK_PREPROCESSOR.get(role)))
                    break
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
                        transcript.notice(io_out, unverified_readiness_text(
                            readiness.get("reason") or ""))
                        continue
                reviewer_approved = False
                # Hash-gate (scout + planner): when the lead's reviewed artifact
                # set is byte-identical to what the paired reviewer LAST APPROVED
                # in this phase epoch + acked context revision, skip the reviewer
                # turn entirely — reuse that approval, with a visible marker
                # (never a silent bypass, D6).
                # Only on the FIRST round of a fresh ready_for_review
                # (review_rounds == 0); a revise loop already in progress always
                # re-reviews. The builder passes no bundle, so it never skips.
                review_skipped = False
                if (skip_baseline is not None and review_fn is not None
                        and review_rounds == 0):
                    composite = skip_baseline.compute_composite()
                    if skip_baseline.eligible(composite):
                        review_skipped = True
                        if trace:
                            trace.event("review.skipped", role=reviewer_role,
                                        reason="unchanged_since_approved",
                                        composite=composite)
                        transcript.notice(io_out, review_skipped_text())
                        # The reviewer's own prior approval of these exact
                        # bytes (same epoch + acked context) is carried.
                        reviewer_approved = True
                # Reviewer gate (topology D): the only source of approval.
                if review_fn is not None and not review_skipped and \
                        review_rounds < REVIEW_ROUND_CAP:
                    review_rounds += 1
                    if trace:
                        trace.event("review.round.start", role=reviewer_role,
                                    round=review_rounds,
                                    round_cap=REVIEW_ROUND_CAP)
                    # None: fall through to the approval check this round.
                    # "continue"/"stop": act on the OUTER loop after the inner one.
                    review_action = None
                    stop_payload = None
                    # A reviewer-failure RETRY (D8) re-runs the reviewer with the
                    # path-first full-reread packet instead of a diff: a
                    # malformed/weak verdict means the diff was insufficient to
                    # judge, so the retry forces a full reread.
                    force_full_reread = False
                    # Inner loop so a reviewer-failure RETRY (and the one silent
                    # auto-retry) re-runs the reviewer in place — same round, no
                    # bounce through the role.
                    while True:
                        # The bytes the reviewer is judging: an approval binds
                        # to exactly this artifact state.
                        reviewed_sha256 = state_store.fingerprint_status(
                            status_path)["sha256"]
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
                                # stale no-op's one automatic repair attempt).
                                force_full_reread = True  # D8: retry full-reread
                                continue
                            # Agent-only: a reviewer that cannot return a
                            # usable verdict is an ABSENT reviewer. Review is
                            # never skipped and the work is never approved
                            # without it: the phase stops with a structured,
                            # non-approving request.
                            if trace:
                                trace.event(
                                    "gate.decision", decider="runtime", role=role,
                                    reviewer_role=reviewer_role,
                                    gate="reviewer_failure", action="stop")
                            review_action = "stop"
                            stop_payload = _agent_stop_payload(
                                "reviewer_unavailable", role,
                                requires="reviewer",
                                reviewer_role=reviewer_role,
                                status_path=status_path,
                                review_path=review_path,
                                failures=review_failures,
                                detail=verdict.get(
                                    "controller_failure_alert"))
                            break
                        # Usable verdict: clear the failure counter and branch.
                        review_failures = 0
                        transcript.notice(io_out, scout_reviewed_text(
                            verdict, review_rounds, REVIEW_ROUND_CAP))
                        if evaluate_fn is not None:
                            # SEAL AND ENQUEUE ONLY — no send, no
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
                        if v == "needs_user" and has_question:
                            # A reviewer question needs an answer this process
                            # cannot supply. It is never guessed at and never
                            # downgraded: the phase stops with the question as
                            # a structured request (answer by resuming with
                            # --context).
                            if trace:
                                trace.event(
                                    "gate.decision", decider="runtime", role=role,
                                    reviewer_role=reviewer_role,
                                    gate="reviewer_needs_user", action="stop")
                            review_action = "stop"
                            stop_payload = _agent_stop_payload(
                                "reviewer_question", role, requires="answer",
                                reviewer_role=reviewer_role,
                                question=str(
                                    verdict.get("user_question") or "").strip(),
                                findings=list(verdict.get("findings") or []),
                                status_path=status_path,
                                review_path=review_path)
                            break
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
                        if v == "approve" and state_store.fingerprint_status(
                                status_path)["sha256"] != reviewed_sha256:
                            # The artifact changed while it was under review:
                            # the approval names bytes that no longer exist.
                            review_action = "stop"
                            stop_payload = _agent_stop_payload(
                                "review_candidate_changed", role,
                                requires="reviewer",
                                reviewer_role=reviewer_role,
                                status_path=status_path,
                                review_path=review_path)
                            break
                        if v == "approve":
                            reviewer_approved = True
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
                            # Only an explicit approve approves the phase.
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
                        elif suppress_reopen_for_challenges:
                            # D-0004 mechanical supersession: EVERY blocking
                            # finding is an uncited-or-contradicted verification
                            # challenge against a candidate the green owned
                            # receipt certifies. The FINDINGS are superseded
                            # (recorded, never erased); the transaction
                            # SURVIVES as `pending_review` — NO builder reopen,
                            # NO review.handoff — and the fall-through gate
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
                            # The reviewer still did not approve: supersession
                            # removes the reopen, never the approval
                            # requirement. The transaction stays
                            # `pending_review` and the phase stops for an
                            # explicit decision.
                            review_action = "stop"
                            stop_payload = _agent_stop_payload(
                                "review_not_approved", role,
                                requires="answer",
                                reviewer_role=reviewer_role,
                                verdict=v,
                                findings=list(verdict.get("findings") or []),
                                superseded_challenges=len(
                                    defeated_challenges),
                                status_path=status_path,
                                review_path=review_path)
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
                            # Round cap reached on a legitimate revise: stop
                            # unapproved with the dissent attached (D5).
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
                            review_rounds = 0
                            if trace:
                                trace.event("review.round_cap",
                                            role=reviewer_role,
                                            round_cap=REVIEW_ROUND_CAP)
                            # Unresolved dissent is never accepted: the phase
                            # stops with the reviewer's findings attached.
                            review_action = "stop"
                            stop_payload = _agent_stop_payload(
                                "review_round_cap", role,
                                requires="answer",
                                reviewer_role=reviewer_role,
                                round_cap=REVIEW_ROUND_CAP,
                                findings=list(verdict.get("findings") or []),
                                status_path=status_path,
                                review_path=review_path)
                        break
                    if review_action == "continue":
                        continue
                    if review_action == "stop":
                        outcome_kind, payload = _end_unapproved(stop_payload)
                        break
                if not reviewer_approved:
                    # Approval is an explicit reviewer approve (or its carried
                    # hash-gate approval) and nothing else. A missing reviewer
                    # never approves by omission.
                    stop_kind = ("reviewer_absent" if review_fn is None
                                 else "review_not_approved")
                    if trace:
                        trace.event("gate.decision", decider="runtime", role=role,
                                    gate="ready_for_review", action="stop",
                                    reason=stop_kind)
                    outcome_kind, payload = _end_unapproved(_agent_stop_payload(
                        stop_kind, role, requires="reviewer",
                        reviewer_role=reviewer_role, status_path=status_path,
                        review_path=review_path))
                    break
                bound_receipt = (
                    state_store.read_current_receipt_pointer(session_uuid)
                    if role == "builder" and session_uuid else None)
                if (isinstance(bound_receipt, dict)
                        and bound_receipt.get("transaction_id")
                        and not _accepted_manifest_matches(bound_receipt)):
                    # Verification no longer holds for the candidate the
                    # reviewer approved: never an approval. The receipt's
                    # candidate was abandoned, which the grant records.
                    _grant_gate_acceptance(session_uuid, trace)
                    outcome_kind, payload = _end_unapproved(
                        _agent_stop_payload(
                            "verification_not_current", role,
                            requires="operator",
                            transaction_id=bound_receipt.get("transaction_id"),
                            status_path=status_path))
                    break
                transcript.notice(io_out, review_text(status_path))
                if trace:
                    trace.event("gate.show", role=role,
                                gate="ready_for_review", path=status_path,
                                has_dissent=False)
                    trace.event("gate.decision", decider="reviewer",
                                role=role, reviewer_role=reviewer_role,
                                gate="ready_for_review", action="approve")
                    trace.event("gate.show", role=role, gate="done",
                                path=status_path)
                # D-0004/D-0005 gate-outcome grant: the reviewer's explicit
                # approve accepts the still-pending bound transaction while
                # the candidate manifest equals the receipt's.
                if role == "builder":
                    _grant_gate_acceptance(session_uuid, trace)
                transcript.notice(io_out, done_text(
                    status_path))
                if session_uuid and role_work_id:
                    _approved_manifest = dispatch_manifest.load_manifest(
                        state_store.manifest_path_for(session_uuid, role))
                    _approved_digest = (_approved_manifest or {}).get("digest")
                    if _approved_digest:
                        _complete_phase(session_uuid, role_work_id,
                                        _approved_digest,
                                        source="gate.reviewer")
                outcome_kind = "approved"
                break
            else:
                if status == "needs_input":
                    review_rounds = 0  # role re-opened work: fresh review budget
                    pending_question = _pending_question(status_path)
                    if pending_question:
                        missing_question_repairs = 0
                    elif (require_pending_question
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
                        transcript.notice(
                            io_out,
                            "%s\nNo question was recorded; asking %s to repair "
                            "its status." % (needs_input_text(), role))
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
                            "repair.")
                    transcript.notice(io_out, gate_text)
                # An open question needs an answer this process cannot supply:
                # stop with the question as a structured request. The status
                # is left as-is; the orchestrator answers by resuming the
                # session with --context.
                # A turn that ended in neither state (no status written, or
                # an in-progress one) is stopped the same way, named apart.
                stop_kind = ("needs_input" if status == "needs_input"
                             else "role_turn_incomplete")
                if trace:
                    trace.event("gate.decision", decider="runtime", role=role,
                                gate="needs_input", action="stop",
                                reason=stop_kind)
                outcome_kind, payload = _end_unapproved(_agent_stop_payload(
                    stop_kind, role,
                    requires=("answer" if stop_kind == "needs_input"
                              else "operator"),
                    question=_pending_question(status_path) or None,
                    status=status, status_path=status_path))
                break
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
            # Cleanup is secondary evidence beside the role's own result,
            # which stays "closed" whatever the cleanup outcome was.
            cleanup = getattr(session, "last_cleanup", None)
            fields = {}
            if isinstance(cleanup, dict):
                fields = {"cleanup_outcome": cleanup.get("outcome"),
                          "cleanup_confirmed": cleanup.get("confirmed")}
            trace.event("role.end", role=role, result="closed", **fields)
    return 0, outcome_kind, payload


def _scout_loop(session, first, intel_path, context, io_out,
                review_fn=None, trace=None, on_outcome=None,
                evaluate_fn=None, intel_md_path=None, skip_baseline=None,
                context_revision=None, is_resume=False,
                on_first_send_accepted=None, on_first_send_rejected=None,
                review_path=None,
                save_pending_turn_fn=None, clear_pending_turn_fn=None,
                session_uuid=None, role_work_id=None):
    """The scout instantiation of `_role_loop` (kept as the historical entry
    point). Returns 0; the loop outcome is reported via `on_outcome` so
    `run_flow` can chain into the planning phase on approval.

    `intel_md_path`, when given, repoints the review/done gate surfaces at the
    readable intel markdown (mirroring the planner gate pointing at plan.md);
    the status file driving the loop stays the intel JSON. `skip_baseline` wires
    the reviewer hash-gate (see `_role_loop`)."""
    loop_kwargs = dict(
        role="scout", review_fn=review_fn, trace=trace,
        reviewer_role=SCOUT_REVIEWER, evaluate_fn=evaluate_fn,
        skip_baseline=skip_baseline, context_revision=context_revision,
        phase="scouting", is_resume=is_resume,
        require_pending_question=True,
        review_path=review_path, save_pending_turn_fn=save_pending_turn_fn,
        clear_pending_turn_fn=clear_pending_turn_fn)
    if intel_md_path:
        loop_kwargs["review_text"] = (
            lambda _p: scout_review_text(intel_md_path))
        loop_kwargs["done_text"] = (
            lambda _p: scout_done_text(intel_md_path))
    rc, outcome, payload = _role_loop(
        session, first, intel_path, context, io_out,
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
                   review_packet_ctx=None,
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
        # Surface the review turn to the run transcript. The default
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
        if verdict is not None and not (isinstance(verdict, dict)
                                        and verdict.get("controller_failure")):
            # The reviewer ran against the current context: acknowledge the
            # revision once and stop repeating the wake block. A controller
            # failure never reached the reviewer, so the wake block (and any
            # decision record riding it) and the ack are kept for the next
            # pass of this run.
            holder["context_update"] = None
            if holder["ack"]:
                # An ack that also settles a decision delivery needs to know
                # whether this pass actually reached the reviewer.
                if getattr(holder["ack"], "accepts_verdict", False):
                    holder["ack"](verdict)
                else:
                    holder["ack"]()
                holder["ack"] = None
            if holder["switch_note"] and not _is_review_failure(verdict):
                if on_switch_consumed:
                    on_switch_consumed(reviewer_role)
                holder["switch_note"] = None
        return verdict


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


class GraphDeclarationRejected(Exception):
    """Raised by `_compile_role_manifest` (F-LIVE-GRAPH-WIRING-1) when the
    current, attempt-scoped WorkUnit's OWN persisted `graph_revision`
    declaration fails Package C's dispatch-time check
    (`cowork_preflight.check_dependency_graph_declaration`) -- a self-edge,
    dangling predecessor, cycle, or cross-candidate/cross-policy fan-in
    against the durably stored graph revision. `run_manifest_preflight` is
    NEVER reached for the call that raises this: `.reason` is the exact
    check reason, for the caller to bind onto the WorkUnit's
    `preflight_rejected` -> `rejected_preflight` transition (the existing E
    preflight-rejection seam) verbatim -- never a re-derived or summarized
    message."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


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
        force_recompile=False,
        role_work_id=None):
    """Compile, preflight, and persist the dispatch manifest for one role.

    `role_work_id` (M2 Package E live graph wiring, F-LIVE-GRAPH-WIRING-1):
    the current, attempt-scoped WorkUnit identity (`_role_work_id`) whose OWN
    persisted `graph_revision` declaration is checked -- via
    `cowork_preflight.check_dependency_graph_declaration`, Package C's
    dispatch-time graph primitive -- unconditionally, before this call ever
    reaches `run_manifest_preflight` below: the live manifest-preflight seam
    cannot call it without first applying the WorkUnit graph decision. A
    caller with no WorkUnit-tracked identity for this dispatch (evaluator/
    worktree/reviewer/switch/pre-launch) passes `role_work_id=None`, and a
    WorkUnit that was minted but declares no graph participation
    (`graph_revision` is `None`) is `check_dependency_graph_declaration`'s
    own trivial-pass case -- both resolve identically to "not applicable",
    never a fabricated or parallel identity, and preserve the pre-existing
    dispatch path exactly. A malformed, stale, or mismatched declaration
    raises `GraphDeclarationRejected` instead of returning -- this manifest
    is never compiled, preflighted, or persisted for that call.

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
    Raises GraphDeclarationRejected -- see that class's own docstring --
    before any of the above.
    """
    import cowork_action_policy as _ap

    _work_unit = None
    if role_work_id and session_uuid:
        _work_unit = state_store.work_unit_from_history_record(
            state_store.current_work_unit_state(session_uuid, role_work_id))
    # M2 Package E live-graph-wiring correction (M-2): a malformed PERSISTED
    # graph shape (a hand-tampered/corrupted revisions.jsonl or WorkUnit
    # record -- never producible through this codebase's own validated
    # write paths) can make `check_dependency_graph_declaration` itself
    # raise instead of returning an ok=False result (e.g. `graph_node_from_
    # work_unit`/`validate_revision` schema-validating a malformed stored
    # node). Every such exception is fail-closed here into the SAME
    # GraphDeclarationRejected outcome as an ordinary ok=False result --
    # never left to fall through to the generic `except Exception:` a call
    # site's own manifest-compile guard already has, which would silently
    # degrade a graph-identity failure into an unrelated capability_missing/
    # needs_authority outcome instead of the truthful graph rejection.
    try:
        _graph_check = preflight.check_dependency_graph_declaration(
            _work_unit, session_uuid, state_module=state_store)
    except Exception as _graph_exc:
        raise GraphDeclarationRejected(
            "malformed persisted graph declaration (%s): %s"
            % (type(_graph_exc).__name__, _graph_exc))
    if not _graph_check["ok"]:
        raise GraphDeclarationRejected(_graph_check["reason"])

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
# `cowork_state.append_phase_state_entry`; no exit code, EOF, stop            #
# outcome, or status-file read ever sets `lifecycle_state` directly. A    #
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


# --------------------------------------------------------------------------- #
# Issue #64 P2: the single-writer owner context and the fencing gate.         #
#                                                                              #
# The fencing token has to reach 24 dispatch sites and 50 `_advance_phase`    #
# sites spread across nine functions. Threading it through every signature    #
# would widen this package's reach to almost all of this module, so it uses   #
# the mechanism this file already uses for exactly this shape of problem: a   #
# module-level box with EXPLICIT save/restore, the same pattern               #
# `bridge.set_nested_guard_active` and `active_work_box` establish.           #
#                                                                              #
# `_OWNER_CONTEXT` is a plain dict, deliberately NOT a `threading.local`:     #
# `run_flow` is main-thread-only and the SIGTERM handler that reads it runs   #
# on the main thread. The owner-heartbeat thread never reads it -- it         #
# captures `(session_uuid, owner_id, epoch)` BY VALUE at start, so a nested   #
# `run_flow` can never make it renew the wrong lease.                         #
#                                                                              #
# `enforced` is the whole `--no-session` answer. It is True only between a    #
# successful acquire and its matching restore. While it is False              #
# `_owner_gate_fact` returns None (so `dispatch.decide` sees no fact at all   #
# and behaves exactly as it did before #64) and `_require_owner` is a no-op.  #
# --------------------------------------------------------------------------- #

_OWNER_CONTEXT = {
    "session_uuid": None,
    "owner_id": None,
    "epoch": None,
    "enforced": False,
    "matched": True,
    "provider_conflict": None,
    "pending_dispatch_conflict": None,
}


def _set_owner_context(session_uuid, owner_id, epoch):
    """Publish a freshly acquired lease's fencing token and RETURN THE PRIOR
    CONTEXT for its caller to restore.

    Saving and restoring the prior value -- rather than resetting to a
    constant -- is what makes a nested or reentrant `run_flow` leave the outer
    context exactly as it found it, including any provider conflict the outer
    run has recorded but not yet drained."""
    prior = dict(_OWNER_CONTEXT)
    _OWNER_CONTEXT.update({
        "session_uuid": session_uuid,
        "owner_id": owner_id,
        "epoch": epoch,
        "enforced": True,
        "matched": True,
        "provider_conflict": None,
        "pending_dispatch_conflict": None,
    })
    return prior


def _restore_owner_context(prior):
    """Restore a context previously returned by `_set_owner_context`, verbatim.

    Called from the same `finally` that releases the lease, so every exit path
    -- return, refusal, KeyboardInterrupt, EOFError, crash -- restores exactly
    once. A `None` prior (nothing was ever published) restores nothing."""
    if prior is None:
        return None
    _OWNER_CONTEXT.clear()
    _OWNER_CONTEXT.update(prior)
    return None


def _current_owner_context():
    """A COPY of the live owner context -- never the live dict itself, so a
    reader (notably the SIGTERM handler) cannot mutate it by accident."""
    return dict(_OWNER_CONTEXT)


def _owner_gate_fact(controller=None, resume_session_id=None, purpose=None,
                     role=None):
    """The `owner_result` reducer fact for `dispatch.decide()`, or None.

    None means "no fact": `decide()` then behaves exactly as it did at the
    accredited base. It is returned in two structurally different cases, and
    the ordering between them is deliberate:

      1. `purpose == "evaluator"` -- STRUCTURALLY EXEMPT (plan rule E2). The
         evaluator dispatch site sits under two swallow-all handlers that no
         package here is authorized to touch, so a refusal raised there would
         be converted into an anonymous scoring failure. The compensating
         fence sits one frame ABOVE both swallows instead, at
         `evaluation_transition` (rule E3), which fences the whole evaluation
         region rather than one site. The exemption costs nothing on the
         provider limb: the evaluator session is fresh by design and reports
         no session id back.

      2. No lease is held (`--no-session`, or any entry point that acquires
         none).

    Otherwise it is the durable fencing check: `assert_owner` re-reads
    `owner/lease.json` under the lock and the refusal it produces is mapped to
    the `session_not_owned` / `owner_lease` refusal pair.

    ENFORCEMENT POINT 1 (issue #64 P3), the provider-exclusivity limb, runs
    LAST and only for a dispatch that names a `resume_session_id` -- there is
    nothing to be exclusive about when no provider conversation is being
    resumed. Two orderings are deliberate:

      a. It runs AFTER `assert_owner` succeeds and AFTER `matched` is set
         True. This session's own lease validity is the prior question, and a
         FOREIGN provider binding is not a loss of THIS session's ownership --
         so `matched` must stay True on this path, or the terminal sidecar
         would misreport why the run ended.

      b. It runs after the evaluator exemption, which returns above: the
         evaluator session is fresh by design and resumes nothing, so the
         exemption costs nothing on this limb.

    Exclusivity follows the owning session's LIVENESS, not the record: a
    binding held by this same session, or by one whose lease no longer
    classifies `live_owner`, allows. Only a different, currently live owner
    refuses, with `provider_session_bound` / `owner_lease` -- both already in
    the frozen dispatch vocabulary.

    `ProviderBindingUnavailable` is deliberately NOT caught here. It is not a
    collision and proves nothing, and there is no refusal code for it in the
    frozen dispatch vocabulary, so it cannot be expressed as a refusing fact.
    Propagating it is fail-closed AND typed: `run_flow`'s catch point 1 maps
    it to rc 3 with reason `provider_binding_unavailable`. The declared cost
    is that `dispatch.contract` / `dispatch.decision` are not emitted on that
    one path, because the raise precedes `decide()`.

    A proven binding refusal also STASHES the typed `ProviderSessionConflict`
    it just built on `_OWNER_CONTEXT["pending_dispatch_conflict"]`, so
    `_decide_and_trace` can raise that exact object instead of a generic lease
    error -- which is the only thing that decides whether the operator's
    recovery advice names the OTHER session or their own. `role` is threaded
    through as a TRAILING keyword purely so the stashed conflict carries the
    role the refusal message prints; no existing call site changes. The stash
    is OVERWRITE-on-each-evaluation and is cleared on every non-refusing exit
    of this limb, so a stale conflict can never be reported for a later
    refusal -- deliberately the opposite of the `provider_conflict` slot's
    first-refusal-wins deferral, which holds proof of a refusal that has
    already happened."""
    if purpose == "evaluator":
        return None
    if not _OWNER_CONTEXT["enforced"]:
        return None
    try:
        cowork_owner.assert_owner(_OWNER_CONTEXT["session_uuid"],
                                  _OWNER_CONTEXT["owner_id"],
                                  _OWNER_CONTEXT["epoch"])
    except cowork_owner.OwnerLeaseError as exc:
        _OWNER_CONTEXT["matched"] = False
        _OWNER_CONTEXT["pending_dispatch_conflict"] = None
        return {"allowed": False, "refusal_code": "session_not_owned",
                "refusal_message": str(exc), "source": "owner_lease"}
    _OWNER_CONTEXT["matched"] = True
    # Covers EVERY non-refusing exit below in one place: the allow return, the
    # `not (controller and resume_session_id)` fall-through, and the inner
    # non-matching branch. A clear that dominates them all cannot be forgotten
    # when this branch structure is read again later.
    _OWNER_CONTEXT["pending_dispatch_conflict"] = None
    if controller and resume_session_id:
        record = cowork_owner.read_provider_binding(
            controller, resume_session_id)
        bound_to = (record or {}).get("owner_session_uuid")
        if (bound_to and bound_to != _OWNER_CONTEXT["session_uuid"]
                and cowork_owner.classify_owner_lease(bound_to)
                == "live_owner"):
            # `cowork_owner.refusal_message` is the single renderer for this
            # refusal, and the conflict stashed here is the SAME object
            # `_decide_and_trace` raises -- so the trace field and the
            # operator's screen are two renderings of one fact, not two
            # independent phrasings that can drift apart.
            conflict = cowork_owner.ProviderSessionConflict(
                controller, resume_session_id, bound_to, role)
            _OWNER_CONTEXT["pending_dispatch_conflict"] = conflict
            return {
                "allowed": False,
                "refusal_code": "provider_session_bound",
                "refusal_message": cowork_owner.refusal_message(conflict),
                "source": "owner_lease"}
    return dict(_ALLOW_FACT)


def _require_owner(session_uuid=None, advisory=False):
    """The fencing check every governed durable write in this file passes
    through. Returns None, or raises an `OwnerLeaseError` subclass.

    A no-op when no lease is held, and a no-op when `session_uuid` names a
    DIFFERENT session than the one this context owns -- so a governed write
    for an unrelated session is never fenced by someone else's lease.

    A pending provider-binding refusal is drained FIRST and re-raised here,
    inside `run_flow`'s own frame: `bind_provider_session` is called from a
    callback that fires during a live send, and letting the exception cross
    that boundary would route it into the send gateway's own
    `except Exception`, converting a typed refusal into an anonymous turn
    failure. It is a deferral, not a swallow -- the exception object is
    preserved and re-raised at the next governed seam, and it is drained
    EXACTLY ONCE.

    `advisory=True` (the `unlocked=True` signal-handler path) computes the
    same verdict, records it in `matched` for the terminal sidecar to report,
    and NEVER raises and NEVER drains -- a #64 check must not be able to cost
    the external-kill handler its durable `aborted` record, its trace event or
    its `SystemExit`."""
    ctx = _OWNER_CONTEXT
    if not ctx["enforced"]:
        return None
    if session_uuid is not None and session_uuid != ctx["session_uuid"]:
        return None
    pending = ctx["provider_conflict"]
    if pending is not None:
        ctx["matched"] = False
        if advisory:
            return None
        ctx["provider_conflict"] = None
        raise pending
    try:
        cowork_owner.assert_owner(ctx["session_uuid"], ctx["owner_id"],
                                  ctx["epoch"])
        ctx["matched"] = True
    except cowork_owner.OwnerLeaseError:
        ctx["matched"] = False
        if advisory:
            return None
        raise
    return None


def _record_provider_conflict(exc):
    """Hold one provider-binding refusal for `_require_owner` to drain and
    re-raise at the next governed seam. Returns None and NEVER raises.

    DEFERRAL RATHER THAN AN IMMEDIATE RAISE, for the reason `_require_owner`'s
    own docstring already records: `bind_provider_session` is called from the
    session-id callback, which fires during a live send, and letting the
    exception cross that boundary would route it into the send gateway's own
    `except Exception` -- converting a typed refusal into an anonymous turn
    failure. Recording it here keeps the exception OBJECT intact (so its type,
    its message and its `__cause__` all survive) and re-raises it inside
    `run_flow`'s own frame, where catch point 1 maps it to a typed `run.end`
    reason.

    FIRST REFUSAL WINS: an already-pending refusal is never overwritten by a
    later one. That matters most in the case it was written for -- a later
    `ProviderBindingUnavailable` must never be able to displace an earlier
    `ProviderSessionConflict`, because a proven collision outranks an
    unverifiable index and is the one an operator has to act on. Nothing is
    lost either way: the run ends at the next governed seam regardless, and
    the later condition is separately traced at its own seam.

    A no-op when no lease is held, so `--no-session` behaviour is unchanged."""
    if not _OWNER_CONTEXT["enforced"]:
        return None
    if _OWNER_CONTEXT["provider_conflict"] is None:
        _OWNER_CONTEXT["provider_conflict"] = exc
    return None


def _run_owner_heartbeat_loop(stop_event, fire, interval_seconds):
    """The dedicated owner-lease heartbeat daemon tick loop.

    `stop_event.wait` IS the sleep, so teardown is prompt rather than bounded
    by the next tick -- the same shape `_run_activity_tick_loop` uses.

    Deliberately NOT gated on `_ACTIVITY_SHUTDOWN_EVENT`, and deliberately not
    a reuse of `_run_activity_tick_loop`: that event is a per-turn/kill signal
    `run_flow` CLEARS mid-run, long after the lease is acquired, so a
    heartbeat gated on it would be terminable by an unrelated per-send
    lifecycle. The two loops have genuinely different termination conditions.

    `fire()` is a compare-and-swap renew that never raises on a lost lease;
    the handler here covers a store-level failure, because a heartbeat must
    never be able to fail a turn."""
    while True:
        if stop_event.wait(timeout=interval_seconds):
            return
        try:
            fire()
        except Exception:  # noqa: BLE001 - a heartbeat never fails a turn
            pass


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
    _require_owner(session_uuid)
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
    _require_owner(session_uuid)
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
                   unlocked=False, expected_candidate=None):
    """The one seam every production phase advance in this file passes
    through: A's closed reducer decides the next PhaseState from the durable
    current one, and B's public persistence contract makes it durable.

    `expected_candidate` (M3 Package E) is forwarded to `control_plane.
    advance` unchanged: every pre-existing caller omits it (None, the
    historical default -- byte-identical behavior for every M2 event and
    for `gate_validated`, which itself treats an omitted value as a
    deliberate opt-out). The three M3 capacity events
    (`capacity_reserved`/`capacity_wake_claimed`/
    `capacity_wake_preflight_failed`) instead REQUIRE a genuine one
    (M3A-REV-001-RESIDUAL) -- callers advancing one of those pass the
    WorkUnit's own `{"candidate_manifest_digest", "candidate_index"}` here
    so the reducer can enforce that the evidence names the SAME candidate,
    never a caller-asserted one.

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
    # Issue #64: the fencing check. ADVISORY on the `unlocked=True` path,
    # whose one production caller is `_handle_external_kill`: a lost lease is
    # recorded in the owner context (and reported in the terminal sidecar the
    # handler writes one statement later) but never raised, so a #64 check can
    # never cost that handler its durable `aborted` record.
    _require_owner(session_uuid, advisory=unlocked)
    if not session_uuid or not work_id:
        return None
    current = state_store.current_phase_state(session_uuid, work_id)
    state = (current or {}).get("state", "pending")
    new_state, reason_code = control_plane.advance(
        state, event, evidence=evidence, expected_candidate=expected_candidate)
    if new_state == state and reason_code in (
            "illegal_transition", "gate_evidence_missing",
            "gate_evidence_candidate_mismatch",
            # M3 Package E: the same "refusal writes nothing" contract,
            # extended to the three M3A-REV-001-RESIDUAL capacity events'
            # own refusal reason codes.
            "capacity_evidence_missing",
            "capacity_evidence_expected_candidate_required",
            "capacity_evidence_candidate_mismatch",
            "capacity_wake_evidence_missing",
            "capacity_wake_evidence_expected_candidate_required",
            "capacity_wake_evidence_candidate_mismatch",
            "capacity_wake_preflight_evidence_missing",
            "capacity_wake_preflight_evidence_expected_candidate_required",
            "capacity_wake_preflight_evidence_candidate_mismatch"):
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


# --------------------------------------------------------------------------- #
# M3 Package E: orchestration-resume wiring.                                  #
#                                                                              #
# Wires C's pure outcome classification (`cowork_bridge.classify_<ctrl>_      #
# failure`) and D's lease decisions (`cowork_capacity_scheduler`) into the    #
# live retain-on-send-failure seam (`_role_loop`'s send-failure block) and    #
# its structured controller-failure end. Quota/overload/authentication       #
# outcomes never retry the SAME provider (quota/overload durably enter        #
# `awaiting_capacity`; no in-process retry exists for any other outcome).     #
# Every classification                                                        #
# -- including the explicit `unknown_provider_failure` member -- is recorded #
# as a durable ProviderHealth fact (`_record_provider_health`).              #
#                                                                              #
# `cowork_bridge.py`'s own `send()` methods return an already-flattened      #
# `{error_type, subtype, result, retry_evidence}` summary, not the raw       #
# provider event C's classifiers expect -- `_synthesize_raw_failure_         #
# evidence` reconstructs the smallest raw shape that summary still honestly  #
# supports, never inventing a discriminant that was not actually present     #
# (an unrecoverable case, e.g. a claude `system`/`api_error` shape whose     #
# `error` dict names neither a `type` token nor -- for `retry_evidence` --   #
# a `retry_after` value, degrades to `unknown_provider_failure`/"unverified" #
# respectively -- fail-closed, never a guess). REV-BLK-01: when `send()`     #
# DID genuinely observe a provider-attested `retry_after` (currently only    #
# `bridge.ClaudeSession`'s `system`/`api_error` path, see                    #
# `cowork_bridge._claude_provider_retry_evidence`), that `retry_evidence`    #
# sub-object rides through this same reconstruction UNCHANGED, letting a     #
# real send failure carrying genuine evidence reach `_capacity_evidence_     #
# fields`/`bridge.capacity_packet_candidate` as trustworthy -- never         #
# fabricated for evidence `send()` never actually observed.                  #
# --------------------------------------------------------------------------- #

_CONTROLLER_FAILURE_CLASSIFIERS = {
    "claude": bridge.classify_claude_failure,
    "codex": bridge.classify_codex_failure,
    "opencode": bridge.classify_opencode_failure,
}

# This worker's own design choice (like ProviderHealth's schema itself,
# `cowork_state.py`'s M3B banner) for mapping a ControllerOutcome onto
# ProviderHealth's closed `status` enum -- not transcribed from the frozen
# plan text, which defines no such mapping. `authentication_failed`/
# `policy_blocked`/`local_guard_exhausted` are "unavailable" (never
# auto-retried / requires local operator action); every other member,
# INCLUDING the explicit `unknown_provider_failure` (never silently
# dropped), is "degraded" (a transient or unclassified signal, not yet
# proven permanently broken).
_PROVIDER_HEALTH_STATUS_FOR_OUTCOME = {
    "quota_limited": "degraded",
    "overloaded": "degraded",
    "authentication_failed": "unavailable",
    "policy_blocked": "unavailable",
    "guard_unavailable": "degraded",
    "transport_failed": "degraded",
    "malformed_output": "degraded",
    "local_guard_exhausted": "unavailable",
    "unknown_provider_failure": "degraded",
}


def _capacity_now():
    """RFC3339 wall-clock reading, minted at the ONE call site each capacity
    seam below reads `now` from -- never read a second time mid-seam, so a
    single capacity-entry/wake attempt is internally consistent."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _synthesize_raw_failure_evidence(controller, send_result):
    """Reconstruct the smallest raw-evidence dict `cowork_bridge.classify_
    <controller>_failure` recognizes from `send_result`'s own flattened
    `result`/`error_type` fields -- see the module-banner note above for why
    this reconstruction (rather than a raw provider event) is all E can
    honestly base a classification on. Never fabricates a discriminant
    `send_result` does not itself carry.

    When `send_result` itself carries a `retry_evidence` sub-object (a
    controller's `send()` -- currently only `bridge.ClaudeSession`, see
    `_claude_provider_retry_evidence` -- attaches this ONLY when the raw
    provider event genuinely carried one), it is copied onto the
    reconstructed raw dict UNCHANGED as `raw["retry_evidence"]`, the exact
    key `bridge.extract_retry_evidence` reads. `send_result` never
    carrying the key (every shape this repository has attested, and
    every non-provider local_guard/transport failure) leaves it absent,
    which `extract_retry_evidence` itself correctly degrades to the
    "unverified" sentinel -- this function neither invents evidence nor
    strips genuine evidence it was actually handed."""
    retry_evidence = send_result.get("retry_evidence")

    def _with_evidence(raw):
        if retry_evidence is not None:
            raw["retry_evidence"] = retry_evidence
        return raw

    if send_result.get("result") == "denied":
        status = ("unreachable" if send_result.get("error_type")
                  == "guard_unavailable" else "denied")
        return {"type": "local_guard", "status": status}
    error_type = send_result.get("error_type")
    if error_type == "guard_unavailable":
        return {"type": "local_guard", "status": "unreachable"}
    if error_type == "eof":
        return {"type": "transport_error", "exception_type": "eof"}
    if not isinstance(error_type, str) or not error_type:
        return {"type": "__unrecognized__"}
    if controller == "codex":
        return _with_evidence({"type": "error", "code": error_type})
    if controller == "opencode":
        return _with_evidence({"type": "error", "error": {"name": error_type}})
    return _with_evidence({"type": "assistant", "error": error_type})


def _classify_raw_failure(controller, raw_evidence):
    """Package C classification of one failed send's already-synthesized
    raw evidence (see `_synthesize_raw_failure_evidence`), or
    `unknown_provider_failure` for an unrecognized controller / raw shape --
    this seam is total: it never raises and never leaves a failed send
    unclassified. Callers needing both the classification AND the raw
    evidence itself (M3 Package E's real capacity_packet_candidate wiring,
    `_capacity_evidence_fields`) compute `raw_evidence` once via
    `_synthesize_raw_failure_evidence` and pass it to both."""
    classify = _CONTROLLER_FAILURE_CLASSIFIERS.get(controller)
    if classify is None:
        return "unknown_provider_failure"
    try:
        return classify(raw_evidence)
    except ValueError:
        return "unknown_provider_failure"


def _record_provider_health(session_uuid, role, provider, controller_outcome,
                            now):
    """Durably record ProviderHealth for every C classification reached at
    the send-failure seam -- including the explicit `unknown_provider_
    failure` member (the frozen brief's named case): a classification this
    worker cannot act on is still truthfully recorded, never silently
    dropped. Best-effort: a missing session_uuid/provider, or any storage
    failure, never blocks the caller's own recovery-gate flow -- ProviderHealth
    is an observability fact, not a gate itself."""
    if not session_uuid or not provider:
        return
    try:
        prior = state_store.read_provider_health(session_uuid, role, provider)
        consecutive_failures = min(
            (prior or {}).get("consecutive_failures", 0) + 1, 1000000)
        state_store.write_provider_health(session_uuid, {
            "role": role, "provider": provider,
            "status": _PROVIDER_HEALTH_STATUS_FOR_OUTCOME.get(
                controller_outcome, "degraded"),
            "consecutive_failures": consecutive_failures,
            "last_outcome": controller_outcome,
            "last_updated_at": now,
        })
    except (ValueError, OSError):
        pass


def _durable_provider_session_id(session_uuid, role, controller):
    """The durable, resume-surviving `provider_session_id` for (role,
    controller) -- read from THIS session's own persisted state via the
    IDENTICAL lookup (`_find_session_state` + `state_store.
    get_role_session`) the resume-trigger CLI already uses to
    reconstruct a resume session. Never an in-process session object's own
    attribute: e.g. `bridge.ClaudeSession` never updates its own
    `self.session_id` once the provider's real id is observed mid-turn --
    only the `on_session_id` callback that persists it durably (via
    `role_saver` -> `state_store.save_role_session`) does, and that
    callback fires (updating durable state) BEFORE `send()` can return a
    failure for THIS SAME turn. Sourcing from durable state therefore works
    identically for a fresh dispatch (the id observed and persisted this
    run, before the failing send) and a resumed one (already persisted
    from a prior run) -- the exact fresh/resumed-role parity the frozen
    brief requires. Returns None when unresolvable (session persistence
    disabled, or no provider session observed yet)."""
    if not session_uuid or not role:
        return None
    state = _find_session_state(os.getcwd(), session_uuid)
    if state is None:
        return None
    return state_store.get_role_session(state, role, controller)


def _capacity_candidate_binding(session_uuid, work_id, role):
    """The genuine, PROVEN candidate identity this WorkUnit's own dispatch
    manifest already bound -- `(candidate_manifest_digest, candidate_index,
    controller_policy_digest)` -- or None when the WorkUnit is not yet
    candidate-bound (M3A-REV-001-RESIDUAL: a capacity transition can never
    be produced without one, so this is the fail-closed precondition every
    capacity-entry/wake-preflight caller below checks first).

    `controller_policy_digest` reuses the dispatch manifest's own
    `binding.config_digest` -- the same reuse `_breaker_decision` already
    relies on for its causal fingerprint -- since that digest already names
    the exact controller/model/effort/mode/instruction-set policy governing
    this dispatch; E defines no separate, competing policy-digest concept."""
    if not session_uuid or not work_id:
        return None
    current = state_store.current_work_unit_state(session_uuid, work_id)
    projected = state_store.work_unit_from_history_record(current)
    if projected is None:
        return None
    digest = projected.get("candidate_manifest_digest")
    if not digest:
        return None
    try:
        manifest = dispatch_manifest.load_manifest(
            state_store.manifest_path_for(session_uuid, role))
    except Exception:  # noqa: BLE001 - a corrupt manifest fails closed below
        manifest = None
    controller_policy_digest = ((manifest or {}).get("binding") or {}).get(
        "config_digest")
    if not controller_policy_digest:
        return None
    return {
        "candidate_manifest_digest": digest,
        "candidate_index": projected.get("candidate_index"),
        "controller_policy_digest": controller_policy_digest,
    }


def _capacity_evidence_fields(controller, controller_outcome, raw_evidence, now):
    """Real `(resume_mode, retry_after, capacity_source)`, derived from C's
    own production evidence-extraction seam -- NEVER synthesized from the
    `controller_outcome` label alone (the exact E v1 review gap this
    replaces `_capacity_source_for`'s old fabricated-hash approach).

    `quota_limited` uses `cowork_bridge.capacity_packet_candidate` EXACTLY
    -- its own re-classification cross-check (requiring `raw_evidence` to
    independently classify `quota_limited` again) and all. `overloaded`
    sits outside that seam's own accepted classification (its docstring:
    quota_limited only) -- `extract_retry_evidence`/`classify_trust_source`/
    `parse_retry_after_text` are the SAME public, lower-level primitives
    `capacity_packet_candidate` itself is built from; reused directly here
    for `overloaded`'s otherwise-identical shape, never a separately
    synthesized value.

    Total: `raw_evidence` that is absent, malformed, or fails
    `capacity_packet_candidate`'s own re-classification cross-check
    degrades to the generic extraction path (which itself degrades to C's
    documented "unverified"/untrustworthy sentinel for anything not
    honestly extractable) rather than raising -- a caller reaching this
    helper has already committed to a capacity-eligible outcome and must
    always get a shape-valid, honestly-sourced result back."""
    if controller_outcome == "quota_limited":
        try:
            candidate = bridge.capacity_packet_candidate(
                controller, controller, raw_evidence, now)
        except ValueError:
            candidate = None
        if candidate is not None:
            return (candidate["resume_mode"], candidate["retry_after"],
                    candidate["capacity_source"])
    retry_evidence = bridge.extract_retry_evidence(raw_evidence)
    evidence_source = retry_evidence["source"]
    retry_after = retry_evidence["value"]
    parsed_retry = capacity_contracts.parse_retry_after_text(retry_after)
    resume_mode = "scheduled" if parsed_retry is not None else "manual_signal"
    try:
        evidence_bytes = json.dumps(raw_evidence, sort_keys=True).encode("utf-8")
    except TypeError:
        evidence_bytes = json.dumps(
            {"controller_outcome": controller_outcome, "issued_at": now},
            sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(evidence_bytes).hexdigest()
    return (resume_mode, retry_after if resume_mode == "scheduled" else None,
           {"kind": evidence_source, "sha256": digest})


# Orchestrator decision deliveries composed into a lead's CURRENT launch, per
# (session_uuid, role): `run_flow` binds them when it composes the seed and
# unbinds them when the launch returns or is acknowledged. `_role_loop` reads
# them only for a capacity pause of that launch's FIRST send, so the durable
# pending turn names exactly the deliveries its bytes carry.
_DECISION_LAUNCH_BINDINGS = {}


def _bind_decision_launch(session_uuid, role, bindings):
    key = (session_uuid, role)
    if bindings:
        _DECISION_LAUNCH_BINDINGS[key] = tuple(dict(b) for b in bindings)
    else:
        _DECISION_LAUNCH_BINDINGS.pop(key, None)


def _decision_launch_bindings(session_uuid, role):
    return [dict(b) for b in _DECISION_LAUNCH_BINDINGS.get(
        (session_uuid, role), ())]


def _pause_lease_horizon_epoch(lease):
    """Epoch seconds after which a PauseLease is past the retry horizon: its
    latest event (issue, `not_before`, claim) plus
    `cowork_capacity.MAX_RETRY_HORIZON_SECONDS`. None when it has no event
    timestamp or any of them is unreadable."""
    anchors = []
    for key in ("issued_at", "not_before", "claimed_at"):
        value = lease.get(key)
        if not value:
            continue
        epoch = capacity_contracts.rfc3339_to_epoch_seconds(value)
        if epoch is None:
            return None
        anchors.append(epoch)
    if not anchors:
        return None
    return max(anchors) + capacity_contracts.MAX_RETRY_HORIZON_SECONDS


def _pause_lease_horizon_release_at(lease):
    """RFC3339 (UTC, whole seconds, rounded up) form of
    `_pause_lease_horizon_epoch`, or None."""
    epoch = _pause_lease_horizon_epoch(lease or {})
    if epoch is None:
        return None
    return datetime.datetime.fromtimestamp(
        int(-(-epoch // 1)), datetime.timezone.utc).isoformat().replace(
            "+00:00", "Z")


def _pause_lease_past_horizon(lease, now=None):
    """True when a still-`unclaimed`/`claimed` PauseLease has had no event
    (issue, `not_before`, claim) for longer than the capacity retry horizon
    (`cowork_capacity.MAX_RETRY_HORIZON_SECONDS`). No PauseLease field names
    an expiry, and no wake is ever scheduled past that horizon, so such a
    lease -- e.g. one whose claimant crashed after an accepted send -- is
    treated as expired. An unreadable timestamp never counts as expired."""
    horizon = _pause_lease_horizon_epoch(lease)
    now_epoch = capacity_contracts.rfc3339_to_epoch_seconds(
        now or _capacity_now())
    if horizon is None or now_epoch is None:
        return False
    return now_epoch > horizon


def _acknowledged_turn_lease_hold(session_uuid, record, expire_stale=False,
                                  expiry_failures=None):
    """The shared lease-liveness core of both capacity holds: the PauseLease
    id still owning an ACKNOWLEDGED pending-turn `record`, or None.

    The lease is live while it is `unclaimed` or `claimed` and not past the
    retry horizon (`_pause_lease_past_horizon`). A terminal lease (consumed,
    cancelled, replaced, expired), a missing lease, or a lease past the
    horizon releases the hold. With `expire_stale`, a lease past the horizon
    is also durably marked expired before the hold is released, so no later
    trigger can resend its stale turn. When that expiry cannot be persisted
    the hold is KEPT (its lease id is returned) and the error type name is
    recorded in `expiry_failures` (a dict keyed by lease id, when given) so
    the caller refuses with it. A `PauseLeaseConflict` means the lease is
    already terminal or gone, which releases the hold."""
    lease_id = record.get("lease_id")
    lease = (state_store.read_pause_lease(session_uuid, lease_id)
             if lease_id else None)
    if not lease or lease.get("consumption_state") not in (
            "unclaimed", "claimed"):
        return None
    if _pause_lease_past_horizon(lease):
        if expire_stale:
            try:
                state_store.mark_pause_lease_expired(session_uuid, lease_id)
            except state_store.PauseLeaseConflict:
                pass
            except (OSError, TimeoutError, ValueError) as exc:
                if expiry_failures is not None:
                    expiry_failures[lease_id] = type(exc).__name__
                return lease_id
        return None
    return lease_id


def _capacity_turn_decision_hold(session_uuid, role, request_id,
                                 expire_stale=False, expiry_failures=None):
    """The PauseLease id holding decision `request_id` for `role`, or None.

    A decision is held when `role`'s acknowledged capacity pending turn
    carries it and that turn's PauseLease is live: `unclaimed` or `claimed`
    and not past the retry horizon (`_pause_lease_past_horizon`). Only the
    resume-trigger that sends those turn bytes may deliver (and acknowledge)
    it. A terminal lease (consumed, cancelled, replaced, expired), a missing
    lease, or a lease past the horizon releases the hold. With
    `expire_stale`, a lease past the horizon is also durably marked expired
    before the hold is released, so no later trigger can resend its stale
    turn. When that expiry cannot be persisted the hold is KEPT (its lease id
    is returned) and the error type name is recorded in `expiry_failures`
    (a dict keyed by lease id, when given) so the caller refuses with it. A
    `PauseLeaseConflict` means the lease is already terminal or gone, which
    releases the hold."""
    record = state_store.read_pending_turn_before_pause(session_uuid, role)
    if not (isinstance(record, dict) and record.get("acknowledged")):
        return None
    if not any(b.get("request_id") == request_id
               for b in record.get("decision_bindings") or ()):
        return None
    return _acknowledged_turn_lease_hold(
        session_uuid, record, expire_stale, expiry_failures)


def _capacity_turn_holds_decision(session_uuid, role, request_id):
    """True when a live capacity pause holds decision `request_id` for
    `role` (see `_capacity_turn_decision_hold`)."""
    return _capacity_turn_decision_hold(
        session_uuid, role, request_id) is not None


def _capacity_held_decisions(session_uuid, trusted_state):
    """Every trusted pending decision delivery a live capacity pause holds:
    `{"request_id", "role", "lease_id"}` dicts, oldest first. Leases past
    the retry horizon are durably expired on the way (they hold nothing);
    one whose expiry could not be persisted still holds, and its dict also
    carries `"expiry_failed": <error type name>`."""
    holds = []
    expiry_failures = {}
    for entry in state_store.read_pending_decision_deliveries(
            session_uuid, trusted_state=trusted_state):
        rid = entry.get("request_id")
        trusted = state_store.trusted_decision_response(
            trusted_state, rid,
            (entry.get("delivery") or {}).get("response_kind")) or {}
        for target in state_store.pending_delivery_targets(entry):
            if target not in (trusted.get("targets") or ()):
                continue
            lease_id = _capacity_turn_decision_hold(
                session_uuid, target, rid, expire_stale=True,
                expiry_failures=expiry_failures)
            if lease_id:
                hold = {"request_id": rid, "role": target,
                        "lease_id": lease_id}
                if lease_id in expiry_failures:
                    hold["expiry_failed"] = expiry_failures[lease_id]
                holds.append(hold)
    return holds


def _capacity_turn_role_hold(session_uuid, role, expire_stale=False,
                             expiry_failures=None):
    """The PauseLease id holding `role`'s acknowledged capacity pending turn,
    or None. This is `_capacity_turn_decision_hold` without the
    `decision_bindings` filter: an ORDINARY capacity pause -- one no
    orchestrator decision rides -- is owned by its lease just the same, and
    only the resume-trigger that sends those turn bytes may replay them. An
    UNACKNOWLEDGED record is a pause still mid-flight and rollback-eligible,
    so it never holds. Lease liveness, durable expiry and the fail-closed
    unpersistable-expiry path are the shared
    `_acknowledged_turn_lease_hold`."""
    record = state_store.read_pending_turn_before_pause(session_uuid, role)
    if not (isinstance(record, dict) and record.get("acknowledged")):
        return None
    return _acknowledged_turn_lease_hold(
        session_uuid, record, expire_stale, expiry_failures)


def _capacity_held_roles(session_uuid):
    """Every role whose acknowledged capacity pending turn a live lease still
    owns: `{"role", "lease_id"}` dicts in `ROLES` order. Leases past the retry
    horizon are durably expired on the way (they hold nothing); one whose
    expiry could not be persisted still holds, and its dict also carries
    `"expiry_failed": <error type name>`."""
    holds = []
    expiry_failures = {}
    for role in ROLES:
        lease_id = _capacity_turn_role_hold(
            session_uuid, role, expire_stale=True,
            expiry_failures=expiry_failures)
        if lease_id:
            hold = {"role": role, "lease_id": lease_id}
            if lease_id in expiry_failures:
                hold["expiry_failed"] = expiry_failures[lease_id]
            holds.append(hold)
    return holds


def _capacity_hold_refusal(session_uuid, hold):
    """`(stop_details, message, rc)` for a run refused because `hold` (one
    `_capacity_held_decisions` entry) keeps its decision on a capacity
    pause. The details name the lease's state, claimant/automation refs and
    the time its retry horizon releases it, so a crashed claimant can be
    recovered; no reset or cancel command is invented."""
    rid, role, lease_id = hold["request_id"], hold["role"], hold["lease_id"]
    lease = state_store.read_pause_lease(session_uuid, lease_id) or {}
    lease_state = lease.get("consumption_state")
    claimant_ref = lease.get("claimant_ref")
    automation_ref = lease.get("automation_ref")
    release_at = _pause_lease_horizon_release_at(lease)
    details = {
        "kind": "decision_held_by_capacity_pause",
        "request_id": rid, "role": role, "lease_id": lease_id,
        "lease_state": lease_state, "claimant_ref": claimant_ref,
        "automation_ref": automation_ref,
        "horizon_release_at": release_at}
    if hold.get("expiry_failed"):
        details["expiry_failed"] = hold["expiry_failed"]
        details["lease_path"] = state_store.pause_lease_path_for(
            session_uuid, lease_id)
        return details, (
            "decision %s for %s rides the capacity-paused turn of lease %s, "
            "which is past its retry horizon (%s) but could not be durably "
            "marked expired (%s); the hold is kept so no wake resends that "
            "stale turn. Make the lease record at %s writable and rerun"
            % (rid, role, lease_id, release_at or "unknown",
               hold["expiry_failed"], details["lease_path"])), 1
    if lease_state == "claimed":
        return details, (
            "decision %s for %s rides the capacity-paused turn of lease %s, "
            "claimed by %s but never consumed (its claimant stopped after "
            "claiming). Retrigger it with the SAME identities: "
            "`resume-trigger --session-uuid %s --lease-id %s --claimant-ref "
            "%s --automation-ref %s` (verbatim, no --redirected-context); "
            "after the lease is released (consumed, or past its retry "
            "horizon at %s) a plain run delivers it"
            % (rid, role, lease_id, claimant_ref, session_uuid, lease_id,
               claimant_ref, automation_ref, release_at or "unknown")
        ), CAPACITY_WAIT_EXIT_CODE
    return details, (
        "decision %s for %s rides the capacity-paused turn of lease %s; it "
        "is delivered by `resume-trigger --lease-id %s` (verbatim, no "
        "--redirected-context), or released when that lease is cancelled or "
        "expires" % (rid, role, lease_id, lease_id)), CAPACITY_WAIT_EXIT_CODE


def _capacity_role_hold_refusal(session_uuid, hold, launch_dir=None):
    """`(stop_details, message, rc)` for a run refused because `hold` (one
    `_capacity_held_roles` entry) keeps its role's paused turn on a live
    lease. Keyed on the role rather than a decision: an ordinary pause carries
    none, so the details name no `request_id`. They do name the lease's state,
    claimant/automation refs and the time its retry horizon releases it, so a
    crashed claimant can be recovered; no reset or cancel command is
    invented."""
    role, lease_id = hold["role"], hold["lease_id"]
    lease = state_store.read_pause_lease(session_uuid, lease_id) or {}
    lease_state = lease.get("consumption_state")
    claimant_ref = lease.get("claimant_ref")
    automation_ref = lease.get("automation_ref")
    release_at = _pause_lease_horizon_release_at(lease)
    details = {
        "kind": "role_held_by_capacity_pause",
        "role": role, "lease_id": lease_id,
        "lease_state": lease_state, "claimant_ref": claimant_ref,
        "automation_ref": automation_ref,
        "horizon_release_at": release_at}
    if hold.get("expiry_failed"):
        details["expiry_failed"] = hold["expiry_failed"]
        details["lease_path"] = state_store.pause_lease_path_for(
            session_uuid, lease_id)
        return details, (
            "role %s has a capacity-paused turn held by lease %s, which is "
            "past its retry horizon (%s) but could not be durably marked "
            "expired (%s); the hold is kept so no wake resends that stale "
            "turn. Make the lease record at %s writable and rerun"
            % (role, lease_id, release_at or "unknown", hold["expiry_failed"],
               details["lease_path"])), 1
    if lease_state == "claimed":
        # The COMPLETE recovery invocation: an agent copying it from another
        # directory would otherwise trip the trigger's own cwd preflight.
        cwd_arg = (" --cwd %s" % launch_dir) if launch_dir else ""
        return details, (
            "role %s has a capacity-paused turn held by lease %s, claimed by "
            "%s but never consumed (its claimant stopped after claiming). "
            "Retrigger it with the SAME identities: `resume-trigger "
            "--session-uuid %s --lease-id %s --claimant-ref %s "
            "--automation-ref %s%s` (verbatim, no --redirected-context); "
            "after the lease is released (consumed, or past its retry "
            "horizon at %s) a plain run continues the session"
            % (role, lease_id, claimant_ref, session_uuid, lease_id,
               claimant_ref, automation_ref, cwd_arg,
               release_at or "unknown")), CAPACITY_WAIT_EXIT_CODE
    # An unclaimed lease is still owned by its scheduled wake adapter, so this
    # names the mechanism that will replay the turn -- deliberately NOT a full
    # four-identity command a reader might run to race that adapter.
    return details, (
        "role %s has a capacity-paused turn held by lease %s; it is replayed "
        "by `resume-trigger --lease-id %s` (verbatim, no "
        "--redirected-context), or released when that lease is cancelled or "
        "expires" % (role, lease_id, lease_id)), CAPACITY_WAIT_EXIT_CODE


def _acknowledge_capacity_turn_decisions(session_uuid, pending_record,
                                         failed=None):
    """The capacity pending turn was accepted by the provider: acknowledge
    exactly the decision deliveries bound to those turn bytes, per target.
    Returns the acknowledged bindings; a binding whose acknowledgment failed
    is appended to `failed` (when given) so the caller reports it.

    Delivery here is AT LEAST ONCE, not exactly once. A store failure leaves
    that delivery pending and never blocks the lease consumption that must
    follow an accepted send, so a later plain run re-sends the block. A crash
    between the accepted send and the acknowledgment leaves the lease
    `claimed`: the decision stays held until that lease is cancelled or
    passes the retry horizon, and the next delivery re-sends the block.
    Either way only the block (by path) repeats: no phase or epoch
    transition is applied twice."""
    acked = []
    for binding in (pending_record or {}).get("decision_bindings") or ():
        try:
            state_store.mark_decision_delivered(
                session_uuid, binding["request_id"],
                target=binding.get("target") or pending_record.get("role"))
        except (OSError, TimeoutError, ValueError, KeyError):
            if failed is not None:
                failed.append(dict(binding))
            continue
        acked.append(dict(binding))
    return acked


def _redirect_refused_decisions(args, pending_record):
    """The request ids a `--redirected-context` resume-trigger would drop
    from the paused turn's bytes (empty for a verbatim wake, or a turn that
    carries no decision)."""
    if getattr(args, "redirected_context", None) is None:
        return []
    return [str(b.get("request_id"))
            for b in (pending_record or {}).get("decision_bindings") or ()]


def _capacity_turn_tampered_answer(session_uuid, state, pending_record):
    """The first decision the capacity pending turn names that the session
    store did not record, or whose answer no longer has the recorded bytes;
    None when every one is intact."""
    for binding in (pending_record or {}).get("decision_bindings") or ():
        rid = binding.get("request_id")
        entry = state_store.trusted_decision_response(state, rid)
        if entry is None:
            return rid
        if entry.get("answer_sha256"):
            try:
                state_store.verified_decision_answer_path(
                    session_uuid, state, rid)
            except state_store.DecisionAnswerTampered:
                return rid
    return None


def _enter_awaiting_capacity(session_uuid, work_id, role, provider,
                             provider_session_id, controller_outcome,
                             pending_text, model, effort, raw_evidence=None,
                             replace_lease_id=None,
                             replace_automation_ref=None,
                             decision_bindings=None):
    """Durable capacity-entry seam: on a genuine `quota_limited`/`overloaded`
    classification, persist the in-flight pending turn BEFORE
    acknowledgment (`cowork_state.write_pending_turn_before_pause`), mint
    and persist a CapacityPacket + PauseLease bound EXACTLY to this
    engagement's candidate/role/provider-session/controller-policy/model/
    effort identity, advance the control-plane reducer's `capacity_reserved`
    event with matching candidate-bound evidence, and only THEN acknowledge
    the pending turn (`cowork_state.acknowledge_pending_turn_before_pause`)
    -- the persist-before-ack, exactly-once-consumption contract the frozen
    brief names.

    `raw_evidence` (the same raw shape `_classify_raw_failure` classified)
    feeds `_capacity_evidence_fields` so `capacity_source`/`resume_mode`/
    `retry_after` derive from C's real evidence-extraction seam, never a
    synthesized value.

    `decision_bindings` (`{"request_id", "target"}` dicts) names the
    orchestrator decision deliveries `pending_text` carries; they are written
    into the same pending-turn record, so the resume-trigger that later sends
    these bytes acknowledges exactly them.

    `replace_lease_id`, when given, is a PRIOR lease for this EXACT SAME
    binding that a genuine post-claim resume-trigger failure just left
    claimed/terminal -- the fresh lease is minted via D's same-binding
    `capacity_scheduler.replace` (carrying `failed_wake_attempts` forward
    monotonically) instead of `start_new_episode` (which would silently
    reset the per-binding automatic-recovery chain back to 0). Omitted
    (None, the default) for every FRESH live-seam entry, which always
    starts a brand new episode.

    ATOMICITY (rolls back on any failure after the pending-turn write, so a
    failed attempt never durably blocks a later, correctly-evidenced one):
    an unacknowledged pending-turn record THIS call itself minted is
    cleared via `_rollback_pending` on every failure path below; a
    just-(re)created live PauseLease is additionally cancelled if the
    control-plane transition itself is refused, so a failed entry never
    leaves a live, unclaimed lease that nothing durably shows as paused.

    Returns a payload dict (role, provider, provider_session_id,
    pending_turn_digest, controller_outcome, candidate/controller-policy
    binding, model, effort, package_id, lease_id, automation_ref) on
    success, or None when any precondition (missing candidate binding,
    missing provider_session_id/pending text, an outcome outside
    `cowork_capacity.CAPACITY_ELIGIBLE_OUTCOMES`, or a storage/validation
    failure) means this failure cannot be honestly entered as capacity --
    the caller falls back to its ordinary controller-failure/resume-
    trigger failure handling instead of fabricating a transition."""
    if not (session_uuid and work_id and provider_session_id and pending_text
           and controller_outcome in capacity_contracts.CAPACITY_ELIGIBLE_OUTCOMES):
        return None
    binding = _capacity_candidate_binding(session_uuid, work_id, role)
    if binding is None:
        return None
    now = _capacity_now()
    # Minted BEFORE the pending-turn write so the durable record can name
    # the exact lease it will be consumed under (the resume-trigger's own
    # exactly-once check binds a replay to THIS lease_id, never merely to
    # the role).
    lease_id = str(uuid.uuid4())
    try:
        pending_record = state_store.write_pending_turn_before_pause(
            session_uuid, role, pending_text, lease_id=lease_id,
            decision_bindings=decision_bindings)
    except ValueError:
        return None

    def _rollback_pending():
        # Best-effort: an unacknowledged pending-turn record THIS attempt
        # itself minted must never survive a failed capacity entry, else it
        # durably blocks every later genuine attempt for this role
        # (`write_pending_turn_before_pause` refuses a DIFFERENT
        # unacknowledged record for the same role). Only ever clears a
        # record still unacknowledged AND naming THIS attempt's own
        # lease_id -- never a different, unrelated in-flight attempt's.
        current = state_store.read_pending_turn_before_pause(session_uuid, role)
        if (current and current.get("acknowledged") is False
                and current.get("lease_id") == lease_id):
            state_store.clear_pending_turn_before_pause(session_uuid, role)

    artifact_hashes = {"manifest": binding["candidate_manifest_digest"]}
    package_id = str(uuid.uuid4())
    automation_ref = "cowork.orchestration_resume/v%d" % (
        capacity_scheduler.SCHEDULER_DECISION_LAYER_VERSION)

    try:
        resume_mode, retry_after, capacity_source = _capacity_evidence_fields(
            provider, controller_outcome, raw_evidence, now)
    except ValueError:
        _rollback_pending()
        return None

    not_before = None
    if resume_mode == "scheduled":
        # Mirrors `validate_capacity_packet`'s own `retry_after` epoch
        # resolution exactly (a raw RFC3339 timestamp value used verbatim,
        # or a bare/`s`-suffixed duration resolved relative to `now`) so
        # `wakeup.not_before`/`PauseLease.not_before` always name the SAME
        # target wake time `retry_after` itself encodes -- never a value
        # this seam re-derives differently.
        parsed_retry = capacity_contracts.parse_retry_after_text(retry_after)
        if parsed_retry["kind"] == "timestamp":
            not_before = parsed_retry["value"]
        else:
            issued_epoch = capacity_contracts.rfc3339_to_epoch_seconds(now)
            not_before = datetime.datetime.fromtimestamp(
                issued_epoch + parsed_retry["value"], tz=datetime.timezone.utc
            ).isoformat().replace("+00:00", "Z")

    capacity_packet = {
        "schema_version": capacity_contracts.SCHEMA_VERSION,
        "package_id": package_id,
        "provider_capacity_class": "subscription_quota_exhausted",
        "provider": provider,
        "resume_mode": resume_mode,
        "retry_after": retry_after,
        "capacity_source": capacity_source,
        "binding": {
            "role": role,
            "provider_session_id": provider_session_id,
            "controller_policy_digest": binding["controller_policy_digest"],
            "candidate_digest": binding["candidate_manifest_digest"],
            "artifact_hashes": artifact_hashes,
        },
        "wakeup": {
            "lease_id": lease_id if resume_mode == "scheduled" else None,
            "automation_ref": automation_ref,
            "not_before": not_before,
        },
        "manual_resume": {
            "condition": ("verified manual-capacity-signal required -- E "
                         "holds no private signing key"),
            # Package A requires a non-null `accepted_source` for every
            # resume_mode='manual_signal' packet (a POLICY declaration of
            # WHO may legitimately authorize the eventual resume, not a
            # historical fact about one that already happened -- no resume
            # has happened yet at entry time). E always declares the
            # strictest of the two: only a genuinely verified signal routed
            # through the top-level authority adapter may ever accept this
            # pause's resume -- never a bare external application, and never
            # a self-claimed signal E itself could fabricate (E holds no
            # private signing key). Populated unconditionally (Package A
            # permits this for `resume_mode='scheduled'` too -- only
            # `manual_signal` REQUIRES it).
            "accepted_source": "top_level_authority_adapter",
            "signal_journal_ref": None,
        },
        "issued_at": now,
    }
    try:
        capacity_packet = capacity_contracts.validate_capacity_packet(
            capacity_packet)
    except ValueError:
        _rollback_pending()
        return None

    pause_lease = {
        "schema_version": capacity_contracts.SCHEMA_VERSION,
        "package_id": package_id,
        "lease_id": lease_id,
        "role": role,
        "provider_session_id": provider_session_id,
        "controller_policy_digest": binding["controller_policy_digest"],
        "candidate_digest": binding["candidate_manifest_digest"],
        "resume_mode": resume_mode,
        "not_before": not_before,
        "automation_ref": automation_ref,
        "artifact_hashes": artifact_hashes,
        "consumption_state": "unclaimed",
        "failed_wake_attempts": 0,
        "issued_at": now,
    }
    try:
        pause_lease = capacity_contracts.validate_pause_lease(pause_lease)
    except ValueError:
        _rollback_pending()
        return None

    try:
        state_store.write_capacity_packet(session_uuid, capacity_packet)
        if replace_lease_id is not None:
            capacity_scheduler.replace(
                session_uuid, replace_lease_id, pause_lease,
                replace_automation_ref or automation_ref)
        else:
            capacity_scheduler.start_new_episode(session_uuid, pause_lease)
    except (ValueError, OSError, capacity_scheduler.SchedulerError):
        _rollback_pending()
        return None

    if replace_lease_id is not None:
        # A repeat quota/overload signal on resume is ITSELF one more
        # genuine failed wake attempt -- account it on the freshly
        # replaced (still-unclaimed) lease, same-binding, never resetting
        # the per-binding chain. Best-effort and best-attempted AFTER the
        # durable replace above already landed: a failure here only means
        # this one repeat signal is not separately counted toward the
        # ceiling, never a reason to roll back the capacity entry itself
        # (the lease/packet are already genuinely, durably paused).
        try:
            capacity_scheduler.record_failed_wake_attempt(
                session_uuid, lease_id, replace_automation_ref or automation_ref)
        except Exception:  # noqa: BLE001 - best-effort, never masks capacity entry
            pass

    expected_candidate = {
        "candidate_manifest_digest": binding["candidate_manifest_digest"],
        "candidate_index": binding["candidate_index"],
    }
    evidence = {"capacity_evidence": {
        "controller_outcome": controller_outcome,
        "role": role, "provider_session_id": provider_session_id,
        "controller_policy_digest": binding["controller_policy_digest"],
        "candidate_manifest_digest": binding["candidate_manifest_digest"],
        "candidate_index": binding["candidate_index"],
        "resume_mode": resume_mode,
        "model": model, "effort": effort,
        "artifact_hashes": artifact_hashes,
        "automation_ref": automation_ref,
    }}
    record = _advance_phase(
        session_uuid, work_id, "capacity_reserved", evidence=evidence,
        source="send_failure_capacity", expected_candidate=expected_candidate)
    if record is None or record.get("state") != "awaiting_capacity":
        # The control-plane refused this transition (e.g. a race against
        # this exact candidate binding): never leave a live, unclaimed
        # lease/packet that nothing durably shows as paused -- cancel the
        # just-(re)created lease and roll back the pending-turn write so a
        # later, correctly-evidenced attempt is never blocked by either.
        try:
            capacity_scheduler.cancel(session_uuid, lease_id, automation_ref)
        except capacity_scheduler.SchedulerLeaseConflict:
            pass
        _rollback_pending()
        return None

    try:
        state_store.acknowledge_pending_turn_before_pause(
            session_uuid, role, pending_record["sha256"])
    except ValueError:
        pass

    return {
        "role": role, "provider": provider,
        "provider_session_id": provider_session_id,
        "pending_turn_digest": pending_record["sha256"],
        "controller_outcome": controller_outcome,
        "candidate_manifest_digest": binding["candidate_manifest_digest"],
        "candidate_index": binding["candidate_index"],
        "controller_policy_digest": binding["controller_policy_digest"],
        "model": model, "effort": effort,
        "package_id": package_id, "lease_id": lease_id,
        "automation_ref": automation_ref,
    }


def _reject_graph_declaration(session_uuid, role_work_id, role, controller,
                              reason, model=None, effort=None, source=None):
    """Persist a truthful `rejected_preflight` outcome for `role_work_id`
    after `_compile_role_manifest` raises `GraphDeclarationRejected` at a
    seam (pre-launch, controller-switch) that runs BEFORE that role's own
    `_ensure_work_unit`/`preflight_started` sequence -- unlike `run_scout`/
    `run_planner`/`run_builder`, which can assume `preflight_started` has
    already fired by the time their own rejection paths run (see their BL-2
    comment), these seams cannot.

    Mints the WorkUnit (idempotent -- a no-op if already minted, exactly
    like `_ensure_work_unit`'s own contract) and advances it through
    `preflight_started` first, so the terminal `preflight_rejected`
    transition this then applies is legal from a fresh/`pending` WorkUnit
    -- never a silent `illegal_transition` no-op that would leave a
    truthful rejection unrecorded. A WorkUnit ALREADY at `preflighting`
    (e.g. this same role's own runner already started it) hits the
    identical `preflight_started` no-op `_advance_phase` itself already
    documents as safe, then proceeds to `preflight_rejected` normally. A
    WorkUnit already PAST `preflighting` (e.g. `running`, mid-turn) has no
    legal edge to `rejected_preflight` in Package A's closed reducer --
    `_advance_phase` no-ops there too, exactly like every other rejection
    seam in this file when the reducer refuses a transition; this function
    never redesigns that reducer to force one."""
    if not session_uuid or not role_work_id:
        return
    _ensure_work_unit(session_uuid, role_work_id, role, controller,
                      model=model, effort=effort)
    _advance_phase(session_uuid, role_work_id, "preflight_started",
                   source=source)
    _advance_phase(session_uuid, role_work_id, "preflight_rejected",
                   evidence={"dependency_graph_declaration": reason},
                   source="dependency_graph_declaration")


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
    -> completed). Exit code 0, EOF, a stop outcome, and status-file
    presence never appear here — only the paired reviewer's explicit APPROVAL at
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
                      probe_result=None, resume_session_id=None, phase=None,
                      owner_result=None):
    """Build a fresh DispatchContract, call `dispatch.decide()` bound to the
    exact manifest identifier governing this dispatch (`manifest['digest']`,
    when a manifest was compiled/revalidated for this attempt), and emit the
    paired `dispatch.contract` / `dispatch.decision` trace events every
    production call site shares. Returns the DispatchDecision dict.

    Issue #64: `owner_result` is the single-writer ownership fact, defaulted
    from this call's OWN existing parameters plus the module owner context, so
    not one of the 24 production call sites changes. It is the FIRST fact
    `dispatch.decide()` evaluates.

    An `owner_lease`-sourced refusal RAISES rather than returning -- after
    both trace events are emitted, so the refusal is fully evidenced. Raising
    is the correct semantics, not a convenience: a process that has lost the
    lease must not go on to write `_advance_phase("preflight_rejected", ...)`,
    which is exactly what the local refusal branch at each call site would do.
    It is also the only shape that survives the deliberately
    non-short-circuiting `refuse and not resume_id` conditions three lead-role
    sites use."""
    contract = _make_dispatch_contract(role, controller, purpose, site,
                                       resume_session_id=resume_session_id,
                                       phase=phase)
    if owner_result is None:
        owner_result = _owner_gate_fact(controller, resume_session_id, purpose,
                                        role)
    decision = dispatch.decide(
        contract, owner_result=owner_result, policy_result=policy_result,
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
    if (decision["outcome"] == "refuse"
            and decision["source"] == "owner_lease"):
        # A PROVEN provider-binding refusal raises the conflict the gate
        # already built, so catch point 1 renders the block naming the OTHER
        # session and reports the same typed reason the decision above just
        # recorded. The drain is unconditional (read-then-None, never `pop`:
        # the box's declared key set must not change, because
        # `_current_owner_context` and `_restore_owner_context` copy the whole
        # dict), and the raise is additionally guarded on the refusal code --
        # both halves are needed, or a stale conflict could be reported for a
        # `session_not_owned` refusal or for a later evaluation.
        pending = _OWNER_CONTEXT["pending_dispatch_conflict"]
        _OWNER_CONTEXT["pending_dispatch_conflict"] = None
        if (pending is not None
                and decision["refusal_code"] == "provider_session_bound"):
            raise pending
        raise cowork_owner.OwnerLeaseLost(
            _OWNER_CONTEXT["session_uuid"], _OWNER_CONTEXT["owner_id"],
            _OWNER_CONTEXT["epoch"], decision["refusal_code"])
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


def run_scout(config, context, selected, io_out=None,
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
              reviewer_switch_note_fn=None,
              on_reviewer_switch_consumed=None,
              on_first_send_accepted=None, on_first_send_rejected=None,
              reviewer_controller_check_fn=None,
              save_pending_turn_fn=None,
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
                force_recompile=bool(resume_id),
                role_work_id=role_work_id)
        except GraphDeclarationRejected as _grej:
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"dependency_graph_declaration": _grej.reason},
                source="dependency_graph_declaration")
            return 1
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
    transcript.notice(io_out, scout_start_text(
        intel_md_path or intel_path or "", resuming=bool(resume_id)))
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
        ok, alert = bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=SCOUT_PROMPT_PATH, trace=trace, role="scout",
                extra_writable_dir=sessions_dir, cache_enabled=True)
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
        return _scout_loop(session, first, intel_path, context, io_out,
                           review_fn=review_fn, trace=trace,
                           on_outcome=on_outcome, evaluate_fn=evaluate_fn,
                           intel_md_path=intel_md_path,
                           skip_baseline=skip_baseline,
                           context_revision=(review_packet_ctx or {}).get(
                               "context_revision"),
                           is_resume=bool(resume_id),
                           on_first_send_accepted=on_first_send_accepted,
                           on_first_send_rejected=on_first_send_rejected,

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
        return _scout_loop(session, first, intel_path, context, io_out,
                           review_fn=review_fn, trace=trace,
                           on_outcome=on_outcome, evaluate_fn=evaluate_fn,
                           intel_md_path=intel_md_path,
                           skip_baseline=skip_baseline,
                           context_revision=(review_packet_ctx or {}).get(
                               "context_revision"),
                           is_resume=bool(resume_id),
                           on_first_send_accepted=on_first_send_accepted,
                           on_first_send_rejected=on_first_send_rejected,

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
    return _scout_loop(session, prompt, intel_path, context, io_out,
                       review_fn=review_fn, trace=trace, on_outcome=on_outcome,
                       evaluate_fn=evaluate_fn, intel_md_path=intel_md_path,
                       skip_baseline=skip_baseline,
                       context_revision=(review_packet_ctx or {}).get(
                           "context_revision"),
                       is_resume=bool(resume_id),
                       on_first_send_accepted=on_first_send_accepted,
                       on_first_send_rejected=on_first_send_rejected,

                        review_path=review_path,
                        save_pending_turn_fn=save_pending_turn_fn,
                        clear_pending_turn_fn=clear_pending_turn_fn,
                            session_uuid=session_uuid,
                            role_work_id=role_work_id)


def run_planner(config, context, selected, io_out=None,
                evaluation_policy=None,
                claude_spawn=None, resume_id=None, on_session=None,
                plan_json_path=None, plan_md_path=None,
                session_factory=None, review_path=None,
                reviewer_runner=None, reviewer_resume_id=None,
                on_reviewer_session=None, reviewer_context=None,
                reviewer_context_update=None, on_reviewer_context_ack=None,
                trace=None, on_outcome=None,
                eval_scratch_path=None, reviewer_eval_scratch_path=None,
                scores_path=None, session_uuid=None, intel_path=None,
                planning_epoch=None, skip_baseline=None, intel_md_path=None,
                review_packet_ctx=None,
                reviewer_switch_note_fn=None,
                on_reviewer_switch_consumed=None,
                on_first_send_accepted=None, on_first_send_rejected=None,
                reviewer_controller_check_fn=None,
                save_pending_turn_fn=None,
                clear_pending_turn_fn=None, worktree=None, worktree_base=None):
    """Spin up the planner's CLI and drive the planning loop (the planner
    instantiation of `_role_loop`).

    `context` is the seed message for this cycle: the approved-intel seed on a
    fresh chain, a digest wake block after a hand-back round trip, or "" on a
    plain resume (auto-continue). The plan JSON (`plan_json_path`) doubles as
    the planner's status channel; `plan_md_path` is the readable review surface.
    `review_path` + the planning-advisor being on the team enable the advisor
    gate; `reviewer_runner` overrides the advisor pass (for tests).
    `eval_scratch_path`/`reviewer_eval_scratch_path` + `scores_path` +
    `session_uuid` wire the per-round peer evaluations (planner <->
    planning-advisor, each bundling a one-time ->scout eval of the approved
    intel at `intel_path`); absent, no evaluations happen.
    `on_outcome(outcome, payload)` reports how the loop ended so `run_flow` can
    chain, stop or finish the session."""
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
                force_recompile=bool(resume_id),
                role_work_id=role_work_id)
        except GraphDeclarationRejected as _grej:
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"dependency_graph_declaration": _grej.reason},
                source="dependency_graph_declaration")
            if on_outcome:
                on_outcome(_OUTCOME_ENDED, None)
            return 1
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
    transcript.notice(io_out, planner_start_text(plan_md_path or "",
                                         resuming=bool(resume_id)))
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
        review_text=lambda _p: planner_review_text(
            plan_md_path or ""),
        done_text=lambda _p: planner_done_text(plan_md_path or ""),
        artifact_noun="plan",
        handoff_enabled=True,
        evaluate_fn=evaluate_fn, skip_baseline=skip_baseline,
        context_revision=(review_packet_ctx or {}).get("context_revision"),
        phase="planning", is_resume=bool(resume_id),
        seed_artifact_paths=[intel_path],
        require_pending_question=True,
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
        ok, alert = bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=PLANNER_PROMPT_PATH, trace=trace,
                role="planner", extra_writable_dir=sessions_dir,
                cache_enabled=True)
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
            session, first, plan_json_path, context, io_out,
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
            session, first, plan_json_path, context, io_out,
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
        session, prompt, plan_json_path, context, io_out,
        on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
            session_uuid=session_uuid, role_work_id=role_work_id)
    report(outcome, payload)
    return rc


def run_builder(config, context, selected, io_out=None,
                evaluation_policy=None,
                claude_spawn=None, resume_id=None, on_session=None,
                build_status_path=None, build_review_path=None,
                session_factory=None,
                reviewer_runner=None, reviewer_resume_id=None,
                on_reviewer_session=None, reviewer_context=None,
                reviewer_context_update=None, on_reviewer_context_ack=None,
                trace=None, on_outcome=None,
                eval_scratch_path=None, reviewer_eval_scratch_path=None,
                scores_path=None, session_uuid=None, plan_json_path=None,
                plan_md_path=None, building_epoch=None, baseline_note="",
                baseline_repos=None, build_summary_path=None,
                review_packet_ctx=None,
                reviewer_switch_note_fn=None,
                on_reviewer_switch_consumed=None,
                on_first_send_accepted=None, on_first_send_rejected=None,
                reviewer_controller_check_fn=None,
                save_pending_turn_fn=None,
                clear_pending_turn_fn=None, worktree=None, worktree_base=None,
                checkpoint_id=None, artifact_kind=None):
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
    stop or finish the session."""
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
                force_recompile=bool(resume_id),
                role_work_id=role_work_id)
        except GraphDeclarationRejected as _grej:
            _advance_phase(
                session_uuid, role_work_id, "preflight_rejected",
                evidence={"dependency_graph_declaration": _grej.reason},
                source="dependency_graph_declaration")
            if on_outcome:
                on_outcome(_OUTCOME_ENDED, None)
            return 1
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
        session_uuid=session_uuid, role_work_id=role_work_id)
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
    # The lead's gate surfaces (start / review / done) point at the build
    # summary markdown when one is wired — the readable review surface — mirroring
    # the scout's intel.md and the planner's plan.md; the status file driving the
    # loop stays build_status_path. Falls back to the status file otherwise.
    build_surface_path = build_summary_path or build_status_path
    transcript.notice(io_out, builder_start_text(build_surface_path or "",
                                         resuming=bool(resume_id)))
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

    def _gate_review_text(_p):
        # UX-021: the review-surface gate renders the SAME derived overlay the reviewer
        # surface carries — read fresh from the current-receipt pointer at
        # banner time, with the agent-authored prose labeled separately and a
        # visible warning when the contradiction flag is set.
        overlay, pointer = _current_verification_overlay(session_uuid)
        return builder_review_text(
            build_surface_path or "", overlay=overlay,
            receipt_path=(pointer.get("receipt_path")
                          if isinstance(pointer, dict) else None),
            agent_status_path=build_status_path)

    loop_kwargs = dict(
        role="builder", review_fn=review_fn, trace=trace,
        reviewer_role=BUILD_REVIEWER,
        needs_input_text=builder_needs_input_text,
        review_text=_gate_review_text,
        done_text=lambda _p: builder_done_text(
            build_surface_path or ""),
        artifact_noun="build",
        handoff_enabled=True,
        handoff_gate_text_fn=builder_handoff_gate_text,
        evaluate_fn=evaluate_fn,
        context_revision=(review_packet_ctx or {}).get("context_revision"),
        phase="building", is_resume=bool(resume_id),
        seed_artifact_paths=[plan_json_path, plan_md_path],
        require_pending_question=True,
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
        ok, alert = bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=BUILDER_PROMPT_PATH, trace=trace,
                role="builder", extra_writable_dir=sessions_dir,
                cache_enabled=True)
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
            session, first, build_status_path, context, io_out,
            on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
                session_uuid=session_uuid, role_work_id=role_work_id,
                checkpoint_id=checkpoint_id, artifact_kind=artifact_kind)
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
            session, first, build_status_path, context, io_out,
            on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
                session_uuid=session_uuid, role_work_id=role_work_id,
                checkpoint_id=checkpoint_id, artifact_kind=artifact_kind)
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
        session, prompt, build_status_path, context, io_out,
        on_first_send_accepted=on_first_send_accepted,
            on_first_send_rejected=on_first_send_rejected, **loop_kwargs,
            session_uuid=session_uuid, role_work_id=role_work_id,
            checkpoint_id=checkpoint_id, artifact_kind=artifact_kind)
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
# team/config/phase logic, from explicit flags only. It never prompts and     #
# never resumes saved work without --session-file or --resume.                #
# --------------------------------------------------------------------------- #

# path: chosen session-file path (None only on error). new_uuid: the minted
# uuid on a New path (so run_flow names the file and the internal session_uuid
# identically), else None. resume: True when an explicit selector names saved
# work. error/reason: a refused invocation (rc 2) and its closed reason code.
SessionChoice = collections.namedtuple(
    "SessionChoice", ["path", "new_uuid", "error", "reason", "resume"])
SessionChoice.__new__.__defaults__ = (None, None, None, None, False)


def _decision_flags(args):
    """The orchestrator decision responses this invocation carries, as
    (response_kind, request_id) pairs."""
    out = []
    for kind, attr in (("answer", "answer"),
                       ("authorize_handoff", "authorize_handoff"),
                       ("decline_handoff", "decline_handoff")):
        value = getattr(args, attr, None)
        if value:
            out.append((kind, value))
    return out


def select_session(args):
    """Decide which session this run uses, from arguments alone. Returns a
    SessionChoice; never prompts and never picks saved work implicitly.

      * --session-file PATH  -> that session (saved work when the file exists)
      * --resume             -> the directory's most recent saved session
      * --no-session         -> an ephemeral session, never read or written
      * --new, or nothing    -> a new session (it needs --context)

    At most one selector may be given. Saved-session operations (a
    controller update, an orchestrator decision, --take-over) require an
    explicit --session-file or --resume."""
    selectors = [flag for flag, on in (
        ("--session-file", bool(args.session_file)),
        ("--resume", bool(args.resume)),
        ("--new", bool(args.new)),
        ("--no-session", bool(args.no_session))) if on]
    if len(selectors) > 1:
        return SessionChoice(
            error="conflicting session selectors: %s (pass exactly one)"
                  % " ".join(selectors),
            reason="conflicting_session_selectors")
    explicit_saved = bool(args.session_file or args.resume)
    saved_ops = []
    if args.switch_controller:
        saved_ops.append("--switch-controller")
    if getattr(args, "allow_controllers", None) is not None:
        saved_ops.append("--allow-controllers")
    saved_ops += ["--" + kind.replace("_", "-")
                  for kind, _id in _decision_flags(args)]
    if getattr(args, "take_over", False):
        saved_ops.append("--take-over")
    if saved_ops and not explicit_saved:
        return SessionChoice(
            error="%s applies to a saved session; name it with "
                  "--session-file PATH or --resume" % " ".join(saved_ops),
            reason="saved_session_selector_required")
    controller_flag = ("--switch-controller" if args.switch_controller
                       else "--allow-controllers"
                       if getattr(args, "allow_controllers", None) is not None
                       else None)
    if controller_flag and args.team:
        return SessionChoice(
            error="%s cannot be combined with --team "
                  "(it reuses the saved team)." % controller_flag,
            reason="conflicting_arguments")
    if controller_flag and args.config:
        return SessionChoice(
            error="%s cannot be combined with --config "
                  "(it reuses the saved role config)." % controller_flag,
            reason="conflicting_arguments")

    cwd = os.getcwd()
    if args.no_session:
        return SessionChoice(path=state_store.session_path())
    if args.session_file:
        if saved_ops and not os.path.exists(args.session_file):
            return SessionChoice(
                error="%s: session file does not exist: %s"
                      % (saved_ops[0], args.session_file),
                reason="session_not_found")
        return SessionChoice(path=args.session_file,
                             resume=os.path.exists(args.session_file))
    if args.resume:
        discovered = state_store.list_sessions(cwd)
        if not discovered:
            return SessionChoice(
                error="--resume: no sessions to resume in %s."
                      % state_store.session_dir(cwd),
                reason="session_not_found")
        return SessionChoice(path=discovered[0]["path"], resume=True)
    u = str(uuid.uuid4())
    return SessionChoice(path=state_store.new_session_path(cwd, u),
                         new_uuid=u)


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


def controller_policy_invalid_text(session_file):
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
        % transcript.render_path(session_file))


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


# --------------------------------------------------------------------------- #
# Run result: the machine contract of one `cowork` run.                        #
#                                                                              #
# Every `cowork` run invocation writes EXACTLY ONE JSON object as the final    #
# line of stdout (`main` -> `emit_run_result`). Exit codes:                    #
#   0   approved: the last phase that ran was approved by its paired reviewer  #
#   1   failed: controller/preflight/reviewer failure, a failed phase, or an   #
#       internal error                                                         #
#   2   invalid invocation (refused before anything was dispatched)            #
#   3   owner conflict                                                         #
#   4   stopped: an open decision request needs an orchestrator answer or      #
#       authorization (stop.request_id; decision_argv)                         #
#   5   paused awaiting provider capacity (resume-trigger)                     #
#   17  provider refusal / no first token                                      #
#   130 interrupted                                                            #
#   143 terminated by SIGTERM                                                  #
#                                                                              #
# The provider transcript is written to stderr; stdout carries only the       #
# result line. A consumer binds the record to the process exit status (its    #
# `rc` must equal it) and treats a missing line (SIGKILL) as a failure.       #
# --------------------------------------------------------------------------- #

RUN_RESULT_VERSION = 1
AGENT_STOP_EXIT_CODE = 4
CAPACITY_WAIT_EXIT_CODE = 5

_RC_OUTCOME = {1: "failed", 2: "invalid_invocation", 3: "owner_conflict",
               AGENT_STOP_EXIT_CODE: "stopped",
               CAPACITY_WAIT_EXIT_CODE: "awaiting_capacity",
               PROVIDER_REFUSAL_EXIT_CODE: "terminated", 130: "interrupted",
               128 + 15: "terminated"}


def _final_rc(rc, last_outcome):
    """Map a normally-ending run's last phase outcome onto the exit code.

    Only an explicit approval keeps 0: a stop, a pause for capacity, an
    interrupt, a failed phase, or no recorded outcome at all is never success."""
    if rc != 0:
        return rc
    if last_outcome == "approved":
        return 0
    if last_outcome == _OUTCOME_STOPPED:
        return AGENT_STOP_EXIT_CODE
    if last_outcome == "awaiting_capacity":
        return CAPACITY_WAIT_EXIT_CODE
    if last_outcome == "interrupted":
        return 130
    return 1


def build_run_result(rc, result_box):
    """The structured record `main` emits for one run."""
    last = result_box.get("last") or {}
    outcome = last.get("outcome")
    payload = last.get("payload")
    masked = rc != 0 and outcome == "approved"
    if masked:
        # A later failure (a lead that could not launch, a crash at session
        # end) is never masked by an earlier phase's approval, and that
        # earlier phase is not reported as the current one.
        outcome = None
        payload = None
    if outcome == _OUTCOME_PROCESS_TERMINATED:
        outcome = "terminated"
    elif outcome in (_OUTCOME_ENDED, None):
        outcome = _RC_OUTCOME.get(rc, "failed") if rc != 0 else "failed"
    session_file = result_box.get("session_file")
    stop = payload if isinstance(payload, dict) and outcome in (
        _OUTCOME_STOPPED, "failed", "awaiting_capacity",
        "terminated") else None
    if stop is None:
        stop = result_box.get("stop")
    result = {
        "cowork_result": RUN_RESULT_VERSION,
        "rc": rc,
        "outcome": outcome,
        "approved": rc == 0 and outcome == "approved",
        "session_uuid": result_box.get("session_uuid"),
        "session_file": session_file,
        "persisted": bool(session_file),
        "phase": result_box.get("phase") or (None if masked
                                              else last.get("phase")),
        "role": None if masked else last.get("role"),
        "phase_outcome": None if masked else last.get("outcome"),
        "stop": stop,
        "reason": result_box.get("reason"),
    }
    if result_box.get("decision_ack_failed"):
        result["decision_ack_failed"] = list(result_box["decision_ack_failed"])
    if session_file:
        resume = ["--session-file", session_file]
        result["resume_argv"] = resume
        request_id = (stop or {}).get("request_id")
        kinds = state_store.DECISION_RESPONSES.get((stop or {}).get("kind"), ())
        if request_id and kinds:
            result["decision_argv"] = [
                resume + ["--answer", request_id, "--context-file", "<answer>"]
                if kind == "answer" else
                resume + ["--" + kind.replace("_", "-"), request_id]
                for kind in kinds]
    return result


def emit_run_result(io_out, rc, result_box):
    """Write the one JSON run-result line. Best-effort: a closed stdout never
    masks the run's own exit code."""
    try:
        io_out.write(json.dumps(build_run_result(rc, result_box),
                                sort_keys=True) + "\n")
        io_out.flush()
    except (OSError, ValueError):
        pass


def run_flow(args, io_out=None, which=None, run_scout_fn=None,
             run_planner_fn=None, run_builder_fn=None, run_worktree_fn=None,
             result_box=None):
    """Run one agent-driven cowork invocation. Returns the exit code (see the
    run-result block above); `result_box`, when given, is filled with what
    `build_run_result` needs, and `main` emits that record."""
    io_out = io_out or sys.stdout
    result_box = result_box if result_box is not None else {}
    run_scout_fn = run_scout_fn or run_scout
    run_planner_fn = run_planner_fn or run_planner
    run_builder_fn = run_builder_fn or run_builder
    run_worktree_fn = run_worktree_fn or run_worktree
    worktree_requested = bool(getattr(args, "worktree", None))
    # The builder and reviewer CLI sessions spawn in the process cwd, so their
    # `git diff` is relative to cwd — NOT to the session-file parent (which may
    # live outside the repo when --session-file points elsewhere). The build
    # baseline must be read from the same cwd to match what they see.
    run_cwd = os.getcwd()

    context_supplied = (args.context is not None
                        or args.context_file is not None)
    decisions = _decision_flags(args)

    def refuse(reason, message, rc=2, trace_obj=None):
        """A refused invocation: nothing is dispatched. The closed `reason`
        code rides the run result."""
        result_box["reason"] = reason
        if trace_obj is not None:
            trace_obj.event("run.end", rc=rc, reason=reason)
        io_out.write("cowork: " + message + "\n")
        io_out.flush()
        return rc

    if len(decisions) > 1:
        return refuse("conflicting_decisions",
                      "one decision per invocation: %s"
                      % " ".join("--" + k.replace("_", "-")
                                 for k, _i in decisions))
    decision = decisions[0] if decisions else None
    if decision and decision[0] == "answer" and not context_supplied:
        return refuse("answer_requires_context",
                      "--answer needs the answer in --context or "
                      "--context-file")
    if decision and decision[0] == "authorize_handoff" and context_supplied:
        return refuse("conflicting_arguments",
                      "--authorize-handoff executes the recorded request as "
                      "is; it takes no --context")
    # The supplied text is read exactly once, here, before anything is
    # created: an unreadable --context-file is an invalid invocation.
    try:
        supplied_text = resolve_context(args) if context_supplied else ""
    except OSError as exc:
        return refuse("context_file_unreadable",
                      "--context-file %s cannot be read (%s)"
                      % (args.context_file, type(exc).__name__))

    # Deterministic git work tree gate (D1): runs early, before session
    # selection, so a non-git launch fails fast with rc 2 and no half-init.
    #
    # (a) A git work tree is a PREREQUISITE, not a runtime condition: every
    #     controller's write boundary is derived from the launch directory's
    #     git toplevel — cowork_bridge._guard_runtime builds
    #     action_policy.OwnedScope.repo_roots from _git_worktree_scope(
    #     os.getcwd()) for opencode and for claude/codex alike, and raises
    #     RuntimeError('git_toplevel_unavailable') without one. There is no
    #     boundary to confine a role to outside a work tree, so such a run is
    #     an unsupported environment and is refused rather than attempted.
    # (b) It is gated HERE because this is the last point before session
    #     selection, the owner lease, the trace, any session asset and any
    #     dispatch — so the refusal is provably pre-dispatch and leaves
    #     nothing behind, while the four argument validations above keep
    #     their more specific diagnoses.
    # (c) Precedence consequence: from a cwd that is not a work tree this
    #     pre-empts every refusal code below it (session selection, decision,
    #     preflight, worktree). rc stays 2 and the outcome stays
    #     invalid_invocation in every moved case; README records the set.
    #
    # One toplevel lookup serves both gates. The --worktree base is that single
    # launch toplevel — NOT discover_git_roots (single repo only) — carried to
    # the worktree creation block below, and None when none was requested.
    launch_toplevel = git_worktree_toplevel(run_cwd)
    worktree_base = launch_toplevel if worktree_requested else None
    if launch_toplevel is None:
        if worktree_requested:
            return refuse("worktree_requires_git",
                          "--worktree requires launching inside a git work "
                          "tree; %s is not one." % run_cwd)
        return refuse("requires_git_work_tree",
                      "cowork must be launched inside a git work tree — "
                      "every role runs confined to it; %s is not one. Launch "
                      "cowork from inside a git repository, or run `git init` "
                      "there first." % run_cwd)
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

    # Session store: select which session this run uses from explicit flags
    # only, BEFORE any team/config/phase logic. A refused selection is rc 2
    # with nothing read, written or dispatched.
    session_enabled = not args.no_session
    choice = select_session(args)
    if choice.error:
        return refuse(choice.reason or "session_selection_refused",
                      choice.error)
    spath = choice.path
    saved = state_store.load(spath) if session_enabled else None
    controller_update_requested = (
        bool(args.switch_controller) or args.allow_controllers is not None)
    resuming_saved = bool(session_enabled and choice.resume
                          and state_store.has_config(saved))
    # Nothing here can ask for the goal: work that is not an explicitly
    # selected saved session (a new session, or --no-session) needs its
    # context in the arguments.
    if not context_supplied and not resuming_saved:
        if session_enabled and choice.resume and not controller_update_requested:
            return refuse("session_not_resumable",
                          "%s is not a resumable cowork session (no saved "
                          "team/config); pass --context to start a new "
                          "session there" % spath)
        if not controller_update_requested:
            return refuse("context_required",
                          "a new session requires initial context; pass "
                          "--context or --context-file.")
    if decision and not resuming_saved:
        return refuse("session_not_resumable",
                      "--%s applies to a saved session with an open decision "
                      "request; %s is not one" % (
                          decision[0].replace("_", "-"), spath))
    # Team, config and reviewer pairing are validated BEFORE any session is
    # created or lease acquired: an invalid invocation leaves nothing behind
    # for a later --resume to pick up.
    if args.team:
        preview_selected, team_err = parse_team(args.team)
        if team_err:
            return refuse("parse_team_error", team_err)
    elif resuming_saved:
        preview_selected = [r for r in ROLES if r in saved["team"]]
    else:
        preview_selected = list(ROLES)
    if not preview_selected:
        return refuse("no_roles_selected", "no roles selected; nothing to do.")
    if args.config:
        config_ok, config_err = apply_config_args(
            default_config(preview_selected), args.config)
        if not config_ok:
            return refuse("config_error", config_err)
    # The same phase resolution the run itself applies after the lease.
    preview_phase = (effective_phase_for(saved, preview_selected)
                     if session_enabled else "scouting")
    if preview_phase == "scouting" and "scout" not in preview_selected:
        return refuse(
            "scout_not_selected",
            "scout not selected: every cowork run begins with the scouting "
            "phase; add scout and scout-reviewer to --team (a saved session "
            "already past scouting resumes into its saved phase).")
    for lead in PHASE_LEADS.values():
        reviewer = handoff.ROLE_REGISTRY[lead]["reviewer"]
        if lead in preview_selected and reviewer not in preview_selected:
            return refuse(
                "reviewer_not_selected",
                "%s needs its paired reviewer %s on the team (approval comes "
                "only from the reviewer); add it to --team" % (lead, reviewer))
    # Both session-mutating controller flags need a loadable saved session with
    # saved team/config; each error names the flag that was actually supplied.
    if controller_update_requested and session_enabled:
        controller_flag = ("--switch-controller" if args.switch_controller
                           else "--allow-controllers")
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
            return refuse(reason, message)
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
    # No decision delivery is bound to a launch until this run composes one.
    for _launch_key in [key for key in _DECISION_LAUNCH_BINDINGS
                        if key[0] == session_uuid]:
        _DECISION_LAUNCH_BINDINGS.pop(_launch_key, None)
    trace = trace_store.Trace(
        trace_store.trace_path_for(session_uuid) if session_enabled else None,
        session_uuid=session_uuid,
        enabled=session_enabled,
    )
    trace.event("run.start", cwd=os.getcwd(), session_file=spath,
                session_enabled=session_enabled)
    result_box.update(session_uuid=session_uuid,
                      session_file=spath if session_enabled else None)
    # Issue #64: SINGLE-WRITER OWNERSHIP. Acquired HERE -- after `run.start`
    # is traced and BEFORE the global preflight, before the `session.start`
    # measurement checkpoint, before any phase entry, before any controller or
    # provider session is constructed, before any subprocess is spawned, and
    # before any governed durable write. A second live process is therefore
    # refused with ZERO paid dispatch and ZERO shared mutation, which is the
    # ordering invariant this whole package exists to establish.
    #
    # `--no-session` mints a real ephemeral `session_uuid` above and passes it
    # onward exactly like a persisted one, but acquires NOTHING here:
    # `enforced` stays False, every gate no-ops, and the run behaves exactly
    # as it did at the accredited base.
    owner_lease = None
    owner_id = None
    owner_epoch = None
    takeover_mode = None
    prior_owner_context = None
    heartbeat_stop_event = None
    heartbeat_thread = None
    release_reason = "normal_exit"
    if session_enabled:
        try:
            owner_claimant = cowork_owner.owner_identity(
                session_uuid, "run_flow", os.getcwd(), spath)
            # `--take-over` never guesses and never falls back from one mode
            # to the other: the mode comes from the same in-lock verdict the
            # acquire gate uses, and `None` means "no takeover is safe here"
            # -- either there is nothing to take over (ordinary acquire
            # below), or death is unprovable and the refusal stands.
            takeover_mode = (
                cowork_owner.select_takeover_mode(session_uuid)
                if getattr(args, "take_over", False) else None)
            if takeover_mode is not None:
                owner_lease = cowork_owner.take_over(
                    session_uuid, owner_claimant, takeover_mode)
            else:
                owner_lease = cowork_owner.acquire_owner_lease(
                    session_uuid, owner_claimant)
        except cowork_owner.OwnerLeaseError as exc:
            trace.event("run.end", rc=3,
                        reason=cowork_owner.owner_refusal_reason(exc))
            io_out.write(cowork_owner.refusal_message(
                exc, session_uuid=session_uuid, session_file=spath))
            io_out.flush()
            return 3
        owner_id = owner_lease["owner_id"]
        owner_epoch = owner_lease["epoch"]
        prior_owner_context = _set_owner_context(
            session_uuid, owner_id, owner_epoch)
        trace.event("owner.acquired", owner_id=owner_id, epoch=owner_epoch,
                    pid=owner_lease.get("pid"),
                    host_id=owner_lease.get("host_id"),
                    takeover_mode=takeover_mode)
        # The heartbeat captures `(session_uuid, owner_id, epoch)` BY VALUE:
        # it never reads the module owner context, so a nested `run_flow`
        # cannot make it renew the wrong lease. It is deliberately not gated
        # on `_ACTIVITY_SHUTDOWN_EVENT` -- see `_run_owner_heartbeat_loop`.
        heartbeat_stop_event = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_run_owner_heartbeat_loop,
            args=(heartbeat_stop_event,
                  (lambda _s=session_uuid, _o=owner_id, _e=owner_epoch:
                   cowork_owner.renew_owner_lease(_s, _o, _e)),
                  owner_lease.get("heartbeat_interval_s")
                  or cowork_owner.DEFAULT_HEARTBEAT_INTERVAL_S),
            daemon=True)
        heartbeat_thread.start()
    try:
        # Orchestrator decisions bind to the ONE open decision request this
        # session recorded when a phase stopped, and are checked here, before
        # anything is written or dispatched. An open answer/authorization
        # request without a response is refused, never silently redirected.
        open_request = (state_store.read_decision_request(session_uuid)
                        if session_enabled else None)
        if not (isinstance(open_request, dict)
                and open_request.get("state") == "open"):
            open_request = None
        if decision is not None:
            try:
                bound_request = state_store.check_decision_response(
                    session_uuid, decision[1], decision[0])
            except state_store.DecisionConflict as exc:
                result_box["stop"] = _decision_request_view(exc.request)
                return refuse("decision_" + exc.reason, str(exc),
                              trace_obj=trace)
            if _decision_candidate_changed(bound_request):
                # The request describes bytes that no longer exist: it is
                # retired (never applied to different work) and the next plain
                # run continues from the artifact as it now is.
                state_store.retire_decision_request(
                    session_uuid, "candidate_changed")
                trace.event("decision.request.retired",
                            request_id=bound_request.get("request_id"),
                            reason="candidate_changed")
                result_box["stop"] = _decision_request_view(bound_request)
                return refuse(
                    "decision_stale",
                    "request %s no longer matches its artifact (%s changed "
                    "since the phase stopped)" % (
                        decision[1], bound_request.get("status_path")),
                    trace_obj=trace)
        elif (open_request is not None
                and _decision_candidate_changed(open_request)):
            state_store.retire_decision_request(
                session_uuid, "candidate_changed")
            trace.event("decision.request.retired",
                        request_id=open_request.get("request_id"),
                        reason="candidate_changed")
        elif open_request is not None:
            result_box["stop"] = _decision_request_view(open_request)
            result_box["phase"] = open_request.get("phase")
            return refuse(
                "decision_required",
                "session has an open %s request %s; respond with %s"
                % (open_request.get("kind"), open_request.get("request_id"),
                   _decision_response_hint(open_request)),
                rc=AGENT_STOP_EXIT_CODE, trace_obj=trace)
        if session_enabled and session_uuid:
            # A consumed decision still owed to a role, but with no matching
            # response record in the session store, is neither delivered
            # (it is not orchestrator authority) nor silently dropped: the
            # run stops before anything is written or dispatched.
            unverified = state_store.read_unverified_decision_deliveries(
                session_uuid, saved)
            if unverified:
                rids = [entry.get("request_id") for entry in unverified]
                result_box["stop"] = {
                    "kind": "decision_delivery_unverified",
                    "request_id": rids[0], "request_ids": rids,
                    "decision_record_path":
                        state_store.decision_request_path_for(session_uuid)}
                return refuse(
                    "decision_delivery_unverified",
                    "decision request(s) %s are consumed with a pending "
                    "delivery, but the session store %s has no matching "
                    "orchestrator response record; nothing was run. Restore "
                    "the session store's decision_responses from a copy, or "
                    "have the supervisor retire the delivery record at %s"
                    % (", ".join(str(r) for r in rids), spath,
                       state_store.decision_request_path_for(session_uuid)),
                    rc=1, trace_obj=trace)
            # A decision whose block rides a live capacity pause is delivered
            # only by the resume-trigger that sends those turn bytes. Running
            # the target now would launch it without its decision, so the run
            # waits for capacity instead -- before any write, drain or
            # dispatch. An expired, cancelled or consumed lease holds nothing.
            holds = _capacity_held_decisions(session_uuid, saved)
            if holds:
                first = holds[0]
                trace.event("decision.delivery.held",
                            request_id=first["request_id"],
                            role=first["role"], lease_id=first["lease_id"],
                            reason="capacity_pending_turn")
                stop, message, hold_rc = _capacity_hold_refusal(
                    session_uuid, first)
                stop["holds"] = holds
                result_box["stop"] = stop
                return refuse("decision_held_by_capacity_pause", message,
                              rc=hold_rc, trace_obj=trace)
            # An ORDINARY capacity pause -- one no orchestrator decision rides
            # -- is owned by its PauseLease exactly as a decision-bound one is:
            # only the resume-trigger that sends those turn bytes may replay
            # them, so relaunching the role here would spend quota on a turn
            # the lease still owns. This runs AFTER the decision gate so a
            # decision-bound pause keeps its more specific refusal, and it is
            # run-level by construction: the team is not parsed yet, which is
            # exactly what makes "nothing written, nothing dispatched"
            # provable.
            role_holds = _capacity_held_roles(session_uuid)
            if role_holds:
                first = role_holds[0]
                trace.event("capacity.role.held", role=first["role"],
                            lease_id=first["lease_id"],
                            reason="capacity_pending_turn")
                stop, message, hold_rc = _capacity_role_hold_refusal(
                    session_uuid, first, launch_dir=run_cwd)
                stop["holds"] = role_holds
                result_box["stop"] = stop
                return refuse("role_held_by_capacity_pause", message,
                              rc=hold_rc, trace_obj=trace)
        # The caller-visible lever on measurement overhead. A CLI value is persisted
        # so the choice survives a resume; otherwise the saved value stands, and the
        # default is `all_rounds`. Whatever it is, the overhead of scoring is
        # reported as its own cost class, so the choice can be made from data.
        # The CLI value is written only after every precheck below passed (a
        # refused invocation changes no saved configuration).
        evaluation_policy = getattr(args, "evaluation_policy", None)
        persist_evaluation_policy = bool(evaluation_policy and session_enabled)
        if not evaluation_policy:
            evaluation_policy = state_store.get_evaluation_policy(saved)
        trace.event("evaluation.policy", policy=evaluation_policy)
        reuse_config = (session_enabled and state_store.has_config(saved)
                        and not args.team and not args.config)

        # Step 1: team (--team, else the saved team, else every role).
        # Validated before the session was created (see above).
        if args.team:
            selected, _err = parse_team(args.team)
        elif reuse_config:
            selected = [r for r in ROLES if r in saved["team"]]
        else:
            selected = list(ROLES)

        # Step 2: config.
        config = default_config(selected)
        if args.config:
            apply_config_args(config, args.config)
        elif reuse_config:
            # normalize: older saved sessions predate the model/effort keys.
            config = {r: normalize_role_config(saved["config"][r])
                      for r in selected if r in saved["config"]}
            io_out.write("cowork: using saved session config (%s)\n" % spath)
        trace.event("run.config", selected=selected, reuse_config=reuse_config,
                    config={r: dict(config[r]) for r in selected if r in config})

        # Team + config are persisted the first time (or whenever freshly
        # chosen) only after the decision/worktree/controller prechecks below.

        # Global preflight (Python only). Controller executables are checked
        # on-demand when each role is about to launch.
        kwargs = {}
        if which is not None:
            kwargs["which"] = which
        ok, alerts = preflight.preflight({}, **kwargs)
        trace.event("preflight.result", ok=ok, alerts_count=len(alerts))
        if not ok:
            return refuse("preflight_failed",
                          "preflight failed:\n" + "".join(
                              "  - " + alert + "\n" for alert in alerts),
                          rc=1, trace_obj=trace)

        # Phase: resume into the persisted phase (default scouting). The cascade
        # falls back when the resumed phase's lead role is not on the team: a
        # `building` phase without a builder falls back to planning; a `planning`
        # phase without a planner falls back to scouting.
        phase = effective_phase_for(saved, selected) if session_enabled else "scouting"
        planner_on_team = "planner" in selected
        builder_on_team = "builder" in selected
        # Team shape (scout on a scouting team, every lead paired with its
        # reviewer) was refused before the session existed, above.

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
                """Persist the provider session id a controller just reported.

                ENFORCEMENT POINT 3 (issue #64 P3), the durable backstop:
                BIND BEFORE PERSIST. This is the moment a provider
                conversation first becomes THIS session's, and it is reached
                by all three call chains (the bridge's `on_session_id`, the
                reviewer's and the lead role's fresh mint), so it is the seam
                that catches a collision no pre-dispatch check could have seen
                -- an id only observed mid-turn.

                EXACTLY TWO TYPED HANDLERS, in this order, and no broad
                handler anywhere in this closure. Their obligations are
                deliberately OPPOSITE, because the facts they carry are:

                  `ProviderSessionConflict` is PROOF that another live session
                  owns this conversation. It records the refusal for the next
                  governed seam, traces it, and RETURNS -- persisting nothing,
                  because writing an id we have just been proven not to own is
                  the corruption #64 exists to prevent.

                  `ProviderBindingUnavailable` proves NOTHING -- the index
                  could not be consulted. It records and traces the failure
                  and then FALLS THROUGH to the persist below on purpose. A
                  paid provider session id observed alongside an unreadable
                  index must still be written, or a live conversation is
                  stranded outside the durable record that names it (and
                  `_durable_provider_session_id` could no longer resolve it) --
                  which is the very defect this issue exists to remove.

                Recording rather than raising is what keeps a typed refusal
                from crossing the callback boundary into the send gateway's
                own `except Exception`; see `_record_provider_conflict`."""
                if not sid:
                    return
                if _OWNER_CONTEXT["enforced"]:
                    try:
                        cowork_owner.bind_provider_session(
                            controller, sid, _current_owner_context(), role)
                    except cowork_owner.ProviderSessionConflict as exc:
                        _record_provider_conflict(exc)
                        trace.event("owner.provider_session_conflict",
                                    role=role, controller=controller,
                                    session_id=sid,
                                    owner_session_uuid=exc.owner_session_uuid)
                        return
                    except cowork_owner.ProviderBindingUnavailable as exc:
                        _record_provider_conflict(exc)
                        trace.event(
                            "owner.provider_binding_unavailable", role=role,
                            controller=controller, session_id=sid,
                            error_type=type(exc).__name__,
                            cause_type=(type(exc.__cause__).__name__
                                        if exc.__cause__ is not None
                                        else None),
                            detail=str(exc))
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

        def ensure_controller_dispatchable(role, reason="launch"):
            """The pre-launch gate for one role: is its configured controller both
        ALLOWED by this session's policy and actually installed?

        The policy check runs first and is NOT retryable — a policy block is not
        an environment problem, so retrying or prompting would be theatre. It
        prints the block, traces it, and returns False. A missing executable
        is likewise reported and returns False (no retry/switch prompt)."""
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
                result_box["reason"] = "lead_policy_blocked"
                return False
            if session_uuid:
                _disp_cfg = config.get(role) or {}
                _disp_sdir = state_store.session_assets_dir(session_uuid)
                _disp_role_work_id = _current_role_work_id(role)
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
                        force_recompile=False,
                        role_work_id=_disp_role_work_id)
                except GraphDeclarationRejected as _grej:
                    _reject_graph_declaration(
                        session_uuid, _disp_role_work_id, role, controller,
                        _grej.reason, model=_disp_cfg.get("model"),
                        effort=_disp_cfg.get("effort"),
                        source="run_flow_pre_launch")
                    trace.event("controller.failure", role=role, phase=phase,
                                controller=controller,
                                reason="graph_declaration_rejected",
                                artifact_progress=False)
                    io_out.write(
                        "cowork: manifest not proven for %s — dispatch blocked.\n"
                        % role)
                    io_out.flush()
                    result_box["reason"] = "graph_declaration_rejected"
                    return False
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
                    result_box["reason"] = "manifest_not_proven"
                    return False
            controller = config[role].get("controller")
            ok, alerts = check_controller_tool(controller)
            if ok:
                return True
            io_out.write("cowork: %s controller %s is not dispatchable:\n"
                         % (role, controller))
            for alert in alerts:
                io_out.write("  - " + alert + "\n")
            io_out.flush()
            trace.event("controller.failure", role=role, phase=phase,
                        controller=controller, reason="missing_executable",
                        artifact_progress=False)
            # No in-process recovery choice: a missing controller is an
            # environment problem cowork cannot fix. Fail with a structured
            # stop; the orchestrator installs it or re-invokes with
            # --switch-controller.
            trace.event("gate.decision", decider="runtime", role=role,
                        gate="controller_failure", action="end",
                        reason=reason)
            result_box["reason"] = "lead_controller_missing"
            return False

        # Kept as the historical name for in-tree callers; the policy check is now
        # part of the same pre-launch decision.
        ensure_controller_available = ensure_controller_dispatchable

        def recover_controller_failure(role, reason, alert=None):
            """A lead failed to start. There is no in-process retry/switch
            choice: the failure is traced and the run ends; recovery is a
            machine re-invocation (resume, or --switch-controller)."""
            controller = config[role].get("controller")
            trace.event("controller.failure", role=role, phase=phase,
                        controller=controller, reason=reason,
                        artifact_progress=False)
            trace.event("gate.decision", decider="runtime", role=role,
                        gate="controller_failure", action="end",
                        reason=reason)
            result_box.setdefault("reason", "controller_startup_failed")
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
                ok, alert = (lambda c=cfg, r=role: bridge.probe_claude_stream_json(
                        bridge._real_claude_spawn,
                        mode=c.get("mode", "implement"),
                        yolo=c.get("yolo", True),
                        role_prompt_file=ROLE_PROMPT_PATHS.get(r),
                        trace=trace, role=r,
                        extra_writable_dir=state_store.session_assets_dir(
                            session_uuid),
                        cache_enabled=True))()
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
            io_out.write(controller_policy_invalid_text(spath))
            io_out.flush()
            result_box["reason"] = "controller_policy_invalid"
            trace.event("run.end", rc=2, reason="controller_policy_invalid")
            return 2

        def decision_precheck(lead, reason_prefix, allowed, cfg):
            """No-spend, no-write readiness of one lead and its paired
            reviewer against the policy and controllers this run WILL use
            (`allowed`/`cfg`, a validated controller update included): both
            on the team, and each controller allowed and installed. Refuses
            (and returns False) before any write, evaluation drain, worktree
            agent or lead call; the real pre-launch gates still run when the
            lead launches."""
            if lead not in selected:
                refuse("%s_not_selected" % reason_prefix,
                       "%s is not on the team; nothing can take this work"
                       % lead, trace_obj=trace)
                result_box["refusal_rc"] = 2
                return False
            reviewer = handoff.ROLE_REGISTRY[lead]["reviewer"]
            if reviewer not in selected:
                refuse("reviewer_not_selected",
                       "%s needs its paired reviewer %s on the team (approval "
                       "comes only from the reviewer); add it to --team"
                       % (lead, reviewer), trace_obj=trace)
                result_box["refusal_rc"] = 2
                return False
            for role, reason in ((reviewer, "reviewer_not_dispatchable"),
                                 (lead, "lead_not_dispatchable")):
                controller = (cfg.get(role) or {}).get("controller")
                if not policy.is_allowed(allowed, controller):
                    result_box["refusal_rc"] = 1
                    refuse(reason,
                           "%s is configured for %s, which this session does "
                           "not allow (allowed: %s)" % (
                               role, controller,
                               policy.format_allowed(allowed)),
                           rc=1, trace_obj=trace)
                    return False
                tool_ok, tool_alerts = check_controller_tool(controller)
                if not tool_ok:
                    result_box["refusal_rc"] = 1
                    refuse(reason,
                           "%s controller %s is not dispatchable:\n  - %s"
                           % (role, controller, "\n  - ".join(tool_alerts)),
                           rc=1, trace_obj=trace)
                    return False
            return True

        # Everything a decision, a paid worktree agent or a controller update
        # depends on is checked HERE: before the controller update or any
        # other configuration is written, before the session-start evaluation
        # drain, and before any worktree or lead call. A refusal leaves the
        # saved team, config, policy and open request exactly as they were.
        precheck_allowed = (saved_policy_raw if saved_policy_kind == "allowed"
                            else None)
        precheck_config = config
        proposal_err = None
        if proposal is not None:
            proposal_effective, proposal_err, _warnings = \
                validate_controller_proposal(
                    proposal, precheck_allowed, phase, selected,
                    holder["state"])
            if not proposal_err:
                precheck_allowed = proposal_effective
                precheck_config = dict(config)
                for _role, _target in proposal.mappings or ():
                    precheck_config[_role] = dict(config.get(_role) or {},
                                                  controller=_target)
        decision_target = decision_target_phase = None
        decision_targets = []
        # An invalid proposal is refused by `apply_controller_update` below,
        # before it writes anything.
        if proposal_err is None:
            if decision is not None:
                bound = state_store.read_decision_request(session_uuid) or {}
                if decision[0] == "authorize_handoff":
                    decision_target = HANDBACK_PREPROCESSOR.get(
                        bound.get("role"))
                    decision_target_phase = {
                        v: k for k, v in PHASE_LEADS.items()}.get(
                            decision_target)
                    reason_prefix = "handoff_target"
                else:
                    decision_target = bound.get("role")
                    decision_target_phase = bound.get("phase")
                    reason_prefix = "decision_target"
                if not decision_precheck(decision_target, reason_prefix,
                                         precheck_allowed, precheck_config):
                    return result_box.get("refusal_rc", 2)
                decision_targets = [decision_target]
                if bound.get("kind") == "reviewer_question":
                    # The paired reviewer asked: it receives the answer too.
                    decision_targets.append(
                        handoff.ROLE_REGISTRY[decision_target]["reviewer"])
            elif worktree_requested and not decision_precheck(
                    PHASE_LEADS[phase], "lead", precheck_allowed,
                    precheck_config):
                return result_box.get("refusal_rc", 2)
            recorded_worktree = (state_store.get_worktree(holder["state"])
                                 if session_enabled else None)
            if worktree_requested and not recorded_worktree:
                wt_controller = getattr(args, "wt_controller", "claude")
                if not policy.is_allowed(precheck_allowed, wt_controller):
                    return refuse(
                        "worktree_policy_blocked",
                        "the worktree controller %s is not allowed in this "
                        "session (allowed: %s)" % (
                            wt_controller,
                            policy.format_allowed(precheck_allowed)),
                        trace_obj=trace)

        if proposal is not None:
            ok, message, rc_reject = apply_controller_update(proposal)
            if not ok:
                io_out.write("cowork: " + message + "\n")
                io_out.flush()
                reject_reason = ("switch_controller_failed" if rc_reject == 1
                                 else "controller_policy_rejected")
                result_box["reason"] = reject_reason
                trace.event("run.end", rc=rc_reject, reason=reject_reason)
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

        # Only now, with every refusal behind us, is freshly chosen
        # configuration persisted.
        if persist_evaluation_policy:
            try:
                holder["state"] = saved = state_store.save_evaluation_policy(
                    spath, evaluation_policy, prior=holder["state"])
            except ValueError:
                pass
        if session_enabled and not reuse_config:
            holder["state"] = saved = state_store.save_config(
                spath, selected, config, prior=holder["state"] or {})

        lead_role = PHASE_LEADS[phase]
        lead_resume_id = role_resume_id(lead_role)
        if lead_resume_id:
            trace.event("run.resume", role=lead_role,
                        controller=config[lead_role]["controller"],
                        session_id=lead_resume_id, phase=phase)

        # Step 3: context, read once before the session existed.
        context = supplied_text
        # With a decision flag the supplied text is that decision's answer
        # (or decline note): it is persisted as its own request-bound artifact
        # below and NEVER becomes the session goal. Only a plain explicit
        # --context redirect changes the goal.
        decision_text = context if decision is not None else None
        if decision is not None:
            context = ""

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

        # What consumed orchestrator decisions still owe each target role,
        # filled from the durable pending deliveries below:
        #   decision_pending_for: role -> [request_id, ...] not yet acknowledged
        #   decision_answer_for:  role -> [request_id, ...] carrying an answer
        #   decline_note_for:     role -> True when a decline note is owed
        #   decision_included:    role -> [request_id, ...] composed into that
        #                         role's current launch (acknowledged only by
        #                         that launch's accepted first send / pass)
        decision_pending_for = {}
        decision_answer_for = {}
        decline_note_for = {}
        decision_included = {}

        def verified_answer_path(rid):
            """The answer artifact of `rid`, checked against the digest the
            session store recorded when the orchestrator answered -- before
            EVERY use. A mismatch raises DecisionAnswerTampered (the run
            ends; edited content is never delivered as an orchestrator
            decision)."""
            return state_store.verified_decision_answer_path(
                session_uuid, holder["state"], rid)

        def answers_record_block():
            """Every recorded orchestrator answer of this session, verified
            now, as one typed block (or None)."""
            if not session_enabled:
                return None
            return decision_record_block(state_store.decision_answers(
                session_uuid, trusted_state=holder["state"]))

        def _compose_front(fragment, seed):
            if not seed:
                return fragment
            if isinstance(seed, handoff.HandoffBlock) or getattr(
                    seed, "kind", None) == "static_role":
                return handoff.compose_handoff_blocks(
                    fragment, handoff.STATIC_SEPARATOR, seed)
            return (str(fragment) + "\n\n" + str(seed).strip()).strip()

        def with_decision_note(role, seed):
            """Compose what orchestrator decisions owe `role` onto its next
            seed: each answer block (by path, verified) and/or the hand-back
            decline note. Once per launch; acknowledged by that launch's
            accepted first send. The request ids are also bound to the launch
            (`_bind_decision_launch`) so a capacity pause of this first send
            records exactly which deliveries its pending turn carries."""
            for rid in reversed(decision_answer_for.get(role) or ()):
                seed = _compose_front(
                    decision_answer_block(verified_answer_path(rid)), seed)
            if decline_note_for.get(role):
                seed = _compose_front(_handoff_declined_fragment(
                    HANDBACK_PREPROCESSOR.get(role)), seed)
            rids = list(decision_pending_for.get(role) or ())
            if rids:
                decision_included[role] = rids
            _bind_decision_launch(session_uuid, role, [
                {"request_id": rid, "target": role} for rid in rids])
            return seed

        def with_agent_lead_note(seed):
            """Prepend the runtime agent-session note to a LEAD seed, so the lead
        knows on its first turn that no human is attached and how an
        unanswerable question is reported (runtime layer of the role prompt)."""
            note = AGENT_LEAD_NOTE
            if not seed:
                return note
            if isinstance(seed, handoff.HandoffBlock):
                return handoff.compose_handoff_blocks(
                    _agent_lead_fragment(), handoff.STATIC_SEPARATOR, seed)
            return (str(note) + "\n\n" + str(seed).strip()).strip()

        def reviewer_context_now():
            """The reviewer context passed to a paired reviewer launch: the
            runtime agent-session reviewer note (prompt layer), the shared
            context, and every recorded orchestrator answer beside it (fresh
            reviewer sessions read it from here; the goal is never replaced).
            Rebuilt per launch so the answers are verified at each use."""
            text = (AGENT_REVIEWER_NOTE + "\n\n"
                    + (shared_context or "")).strip()
            record = answers_record_block()
            if record is not None:
                text = (text + "\n\n" + str(record)).strip()
            return text

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

        The runtime agent-session reviewer note is prepended (or sent alone when
        there is no other gap) so a RESUMED reviewer — whose first pass uses
        context_update, not reviewer_context — still gets the note on its first
        turn of this invocation. A FRESH reviewer
        ignores context_update and gets the note via reviewer_context instead, so
        the note is never doubled.

        An orchestrator answer owed to this reviewer (it asked the question)
        rides the RESUMED reviewer's context_update as its own typed decision
        record block beside the wake block -- never folded into the context
        text, so the goal and the answer keep their own provenance. A FRESH
        reviewer reads it from reviewer_context. Either way the request ids
        are recorded as included, and `reviewer_acker` acknowledges them on
        the reviewer's first pass that actually reached it."""
            gap = None
            resumed = bool(role_resume_id(reviewer_role))
            if session_enabled and resumed:
                gap = state_store.role_context_gap(holder["state"], reviewer_role)
                trace.event("context.gap", role=reviewer_role, revision=current_rev,
                            context_revision=current_rev,
                            delivered=bool(gap), reason="reviewer_resume")
            rids = list(decision_pending_for.get(reviewer_role) or ())
            if rids:
                decision_included[reviewer_role] = rids
            if not resumed:
                return gap
            text = (AGENT_REVIEWER_NOTE + "\n\n" + gap).strip() if gap \
                else AGENT_REVIEWER_NOTE
            answer_rids = [rid for rid in decision_answer_for.get(
                reviewer_role) or () if rid in rids]
            if not answer_rids:
                return text
            record = decision_record_block(
                [{"answer_path": verified_answer_path(rid)}
                 for rid in answer_rids])
            trace.event("decision.delivery.compose", role=reviewer_role,
                        request_ids=answer_rids, resumed=True)
            # Typed parts only: the closed reviewer note, the context-update
            # block for a real gap (the context file holds the context text
            # alone), and the answer record by path.
            parts = [_agent_reviewer_fragment(), handoff.STATIC_SEPARATOR]
            if gap:
                parts += [context_update_block(gap, intel_dir, current_rev),
                          handoff.STATIC_SEPARATOR]
            return handoff.compose_handoff_blocks(*(parts + [record]))

        def reviewer_acker(role):
            """The paired reviewer's first-pass acknowledgment: the context
            revision and the decision deliveries composed into this reviewer
            launch -- neither when the pass never reached the reviewer (a
            controller failure)."""
            ack_context = context_acker(role)

            def ack(verdict=None):
                if isinstance(verdict, dict) and verdict.get(
                        "controller_failure"):
                    return
                if ack_context:
                    ack_context()
                decision_delivered(role)
            ack.accepts_verdict = True
            return ack

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
            # Issue #64 (plan rule E3): the compensating fence for the
            # structurally exempt evaluator dispatch site. It sits HERE, one
            # frame above BOTH swallow-all handlers that strand that site
            # (`_score_queued_entry`'s and `cowork_eval.drain`'s), so an owner
            # refusal unwinds cleanly into `run_flow`'s own handler instead of
            # being converted into an anonymous scoring failure. It fences the
            # whole evaluation region -- every evaluator paid dispatch, every
            # queue mutation the drain performs -- rather than one site.
            #
            # It evaluates ownership ONCE per measurement boundary, so a lease
            # lost mid-drain is first observed at the next governed seam
            # rather than per queue entry. That bound is deliberate.
            _require_owner(session_uuid)
            return run_evaluation_transition(
                session_uuid, evaluation_policy, config=config, trace=trace,
                io_out=io_out, at=at, closed_phases=closed_phases)

        def measurement_checkpoint(at, closed_phases=None):
            """The three measurement steps that run together at every boundary.

        Ordered on purpose: ingest and reconcile FIRST (so the ledger holds the
        minted verification attempts), then drain the evaluation queue, then
        rebuild the record from all of it. Doing it in this order is what makes
        the record current by construction during a live run, so an ordinary
        `--report` loads rather than rebuilds.

        The ingest and rebuild steps stay entirely best-effort: they degrade the
        measurement and never the run. The evaluation transition in the middle
        has its OWN error policy (see `evaluation_transition`): its exceptions
        must not be eaten by a swallow-all meant for measurement.
        """
            results = _measurement_ingest(at)
            evaluation_transition(at, closed_phases=closed_phases)
            _measurement_rebuild(at, results)

        def reviewer_ready_for(lead):
            """Refuse a lead launch BEFORE any paid dispatch when its paired
            reviewer -- the only source of approval -- is not on the team or
            cannot be dispatched (policy or missing executable)."""
            reviewer = handoff.ROLE_REGISTRY[lead]["reviewer"]
            if reviewer not in selected:
                # rc 2 only while nothing has run; after an earlier phase
                # spent, this is a failure of the run, not a bad invocation.
                result_box["refusal_rc"] = 1 if result_box.get("last") else 2
                refuse("reviewer_not_selected",
                       "%s needs its paired reviewer %s on the team (approval "
                       "comes only from the reviewer); add it to --team"
                       % (lead, reviewer))
                return False
            alerts = reviewer_controller_check(reviewer)
            if alerts:
                result_box["refusal_rc"] = 1
                refuse("reviewer_not_dispatchable",
                       "%s's reviewer %s cannot be dispatched:\n  - %s"
                       % (lead, reviewer, "\n  - ".join(alerts)))
                return False
            return True

        def record_outcome(role, box):
            """Remember the latest phase outcome for the run result, and
            durably open a decision request for a stop that needs an answer
            or an authorization (a saved session only)."""
            payload = box["payload"]
            if (box["outcome"] == _OUTCOME_STOPPED and session_enabled
                    and isinstance(payload, dict)
                    and payload.get("kind") in state_store.DECISION_RESPONSES):
                status_path = payload.get("status_path")
                record = state_store.open_decision_request(session_uuid, {
                    "kind": payload["kind"], "role": role, "phase": phase,
                    "requires": payload.get("requires"),
                    "work_id": active_work_box.get("work_id"),
                    "status_path": status_path,
                    "status_sha256": (state_store.fingerprint_status(
                        status_path)["sha256"] if status_path else None),
                    "question": payload.get("question"),
                    "handoff": payload.get("handoff"),
                    "findings": payload.get("findings"),
                })
                payload = dict(payload, request_id=record["request_id"])
                box["payload"] = payload
                trace.event("decision.request.open", role=role, phase=phase,
                            kind=payload["kind"],
                            request_id=record["request_id"])
            result_box["last"] = {"phase": phase, "role": role,
                                  "outcome": box["outcome"],
                                  "payload": payload}

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
        # scored here rather than lost. Every refusal precheck ran above, so a
        # refused invocation never pays for this drain.
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
        # Everything a decision or a paid worktree agent depends on was checked
        # above (`decision_precheck`), before any write or spend.
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
                    # Normally refused by the precheck above; reached only when
                    # a recorded worktree no longer validates. The drain may
                    # already have spent, so this is a failed run.
                    return refuse("worktree_policy_blocked", str(exc), rc=1,
                                  trace_obj=trace)
                wt_cfg = {"controller": wt_controller,
                          "model": None, "effort": None,
                          "yolo": True, "mode": "implement"}
                artifact = run_worktree_fn(
                    wt_cfg, worktree_status_path, worktree_base, wt_name,
                    bool(explicit_name), io_out=io_out,
                    session_uuid=session_uuid, trace=trace,
                    extra_writable_dir=intel_dir)
                ok, wt_path, wt_branch, err = validate_worktree(
                    worktree_base, artifact)
                if not ok:
                    # Fail-fast (D13): no chdir, no scouting — the session never
                    # half-redirects into a bad/nonexistent tree. The worktree
                    # agent already ran: a failed run, not an invalid
                    # invocation.
                    trace.event("worktree.failed", detail=err)
                    return refuse("worktree_failed",
                                  "worktree creation failed: %s" % err, rc=1,
                                  trace_obj=trace)
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
                # An untyped seed here is the raw shared-context text of a
                # fresh launch (e.g. a resume that both switches controller
                # and redirects with --context). The switch packet already
                # carries the CURRENT shared context by path, so the raw text
                # is not re-sent; the scout keeps its standing discovery
                # note, which is typed.
                if role == "scout":
                    return handoff.compose_handoff_blocks(
                        note, handoff.STATIC_SEPARATOR,
                        repo_discovery_fragment)
                return note
            raise TypeError("controller-switch note lacks handoff provenance")

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
        # every planning -> scouting transition (an orchestrator-authorized planner -> scout
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

        def _current_role_work_id(role):
            """The current, attempt-scoped WorkUnit identity for one of the
        three primary WorkUnit-tracked roles -- the SAME identity `_role_
        work_id` derives inside that role's own runner (`run_scout`/`run_
        planner`/`run_builder`), computed from the SAME epoch/attempt boxes
        this loop already threads into `review_packet_ctx` for it (M2
        Package E live-graph-wiring correction, M-1): `switch_controller`/
        `ensure_controller_dispatchable` bind their OWN manifest compile to
        this exact identity instead of trivially skipping the graph check
        (`role_work_id=None`). A reviewer role (scout-reviewer/planning-
        advisor/build-reviewer) or `worktree` has no WorkUnit tracked in
        the existing E v3 design -- `None`, never a fabricated identity."""
            if not session_uuid:
                return None
            if role == "scout":
                return _role_work_id(session_uuid, "scout",
                                     scouting_epoch_box["epoch"],
                                     scout_attempt_box["attempt"])
            if role == "planner":
                return _role_work_id(session_uuid, "planner", epoch_box["epoch"],
                                     planner_attempt_box["attempt"])
            if role == "builder":
                return _role_work_id(session_uuid, "builder",
                                     building_epoch_box["epoch"],
                                     builder_attempt_box["attempt"])
            return None

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
        # is written to the transcript (pre-existing changes get conflated otherwise).
        baseline_box = {"computed": False, "note": None, "repos": None}

        def build_baseline():
            # Per-repo baseline over the approved repo set (plan JSON
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
                    # timeouts each) run synchronously over the repo set; the
                    # dirty warning is written once the snapshot is complete.
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

                gather()
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
        # (on an authorized hand-back) scouting -> ... Plan approval, EOF, or an
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

        # The orchestrator's decision, validated and prechecked before anything
        # ran, is consumed HERE exactly once, under the store lock (a
        # concurrent or replayed response is refused). What the decision still
        # owes a role -- its phase transition and the block the role must
        # receive -- is written in the SAME transaction, so no crash, signal,
        # evaluation drain or failed launch after this point can lose it: a
        # plain resume reads the pending deliveries back and delivers each
        # target once.
        if decision is not None:
            delivery = {"response_kind": decision[0],
                        "role": decision_target,
                        "targets": list(decision_targets),
                        "target_phase": decision_target_phase}
            if decision[0] == "authorize_handoff":
                delivery["epoch_before"] = (
                    scouting_epoch_box["epoch"] if decision_target == "scout"
                    else epoch_box["epoch"])
                delivery["transition_applied"] = False
            answer_sha = None
            if decision_text is not None and decision_text.strip():
                answer_path, answer_sha = state_store.write_decision_answer(
                    session_uuid, decision[1], decision_text)
                delivery.update(answer_path=answer_path,
                                answer_sha256=answer_sha)
            # The orchestrator's response (kind, targets, exact answer
            # digest) is recorded in the session store BEFORE consumption:
            # every later use is checked against it, never against the
            # role-writable assets copy alone.
            holder["state"] = state_store.record_trusted_decision_response(
                spath, decision[1], decision[0], answer_sha256=answer_sha,
                targets=decision_targets, prior=holder["state"])
            try:
                consumed = state_store.consume_decision_request(
                    session_uuid, decision[1], decision[0],
                    response_digest=answer_sha, delivery=delivery)
            except state_store.DecisionConflict as exc:
                result_box["stop"] = _decision_request_view(exc.request)
                return refuse("decision_" + exc.reason, str(exc),
                              trace_obj=trace)
            trace.event("gate.decision", decider="orchestrator",
                        gate=consumed.get("kind"), action=decision[0],
                        role=consumed.get("role"),
                        request_id=consumed.get("request_id"),
                        targets=list(decision_targets))
        # Every consumed decision some target has not acknowledged yet --
        # including older ones (a changed team, a second stop) -- is processed
        # per target, oldest first; only decisions the session store recorded
        # count as orchestrator authority.
        pending_entries = (state_store.read_pending_decision_deliveries(
            session_uuid, trusted_state=holder["state"])
            if session_enabled else [])
        for entry in pending_entries:
            delivery = entry.get("delivery") or {}
            rid = entry.get("request_id")
            response_kind = delivery.get("response_kind")
            trusted = state_store.trusted_decision_response(
                holder["state"], rid, response_kind) or {}
            targets = []
            for target in state_store.pending_delivery_targets(entry):
                if target not in (trusted.get("targets") or ()):
                    continue
                if _capacity_turn_holds_decision(session_uuid, target, rid):
                    # A paused turn already carries this block; whoever sends
                    # it acknowledges it (the capacity resume-trigger).
                    trace.event("decision.delivery.held", request_id=rid,
                                role=target, reason="capacity_pending_turn")
                    continue
                targets.append(target)
            if not targets:
                continue
            if decision is None:
                trace.event("decision.delivery.resume", request_id=rid,
                            targets=targets)
            lead_target = delivery.get("role")
            if (response_kind == "authorize_handoff"
                    and lead_target in ("scout", "planner")
                    and lead_target in targets):
                note = entry.get("handoff") or ""
                if not delivery.get("transition_applied"):
                    target_phase = delivery.get("target_phase")
                    if phase != target_phase:
                        phase = set_phase(target_phase)
                    epoch_now = (scouting_epoch_box if lead_target == "scout"
                                 else epoch_box)["epoch"]
                    if epoch_now == delivery.get("epoch_before"):
                        if lead_target == "scout":
                            bump_scouting_epoch()
                        else:
                            bump_planning_epoch()
                    state_store.update_decision_delivery(
                        session_uuid, rid, transition_applied=True)
                    trace.event("handoff.execute",
                                from_role=entry.get("role"),
                                to_role=lead_target, request_id=rid,
                                **trace_store.prompt_meta(note,
                                                          prefix="payload"))
                if lead_target == "scout":
                    scout_seed = with_discovery(
                        handoff_wake_block(note, intel_dir))
                    planner_seed = None
                else:
                    planner_seed = plan_handback_wake_block(note, intel_dir)
                    builder_seed = None
            elif (response_kind == "decline_handoff"
                    and lead_target in targets):
                status_path = entry.get("status_path")
                if status_path:
                    state_store.invalidate_ready_status(
                        status_path, from_status="handoff_back")
                decline_note_for[lead_target] = True
            for target in targets:
                decision_pending_for.setdefault(target, []).append(rid)
                if trusted.get("answer_sha256"):
                    verified_answer_path(rid)      # refuse tampering up front
                    decision_answer_for.setdefault(target, []).append(rid)

        def with_decision_record(seed):
            """Compose the session's orchestrator answers (verified now) onto
            a FRESH lead seed (built from the original brief + approved
            artifacts)."""
            record = answers_record_block()
            if record is None or not isinstance(seed, handoff.HandoffBlock):
                return seed
            return handoff.compose_handoff_blocks(
                seed, handoff.STATIC_SEPARATOR, record)

        if planner_seed is not None and not role_resume_id("planner"):
            planner_seed = with_decision_record(planner_seed)
        if builder_seed is not None and not role_resume_id("builder"):
            builder_seed = with_decision_record(builder_seed)

        def decision_first_send(role, accepted_cb):
            """Wrap a launch's first-send-accepted callback so the decision
            block that rode this launch is acknowledged exactly when the
            provider accepted it (never earlier)."""
            def accepted():
                if accepted_cb:
                    accepted_cb()
                decision_delivered(role)
            return accepted

        def decision_delivered(role):
            """Acknowledge, for `role` only, every decision delivery composed
            into its current launch. Other targets of the same decision stay
            pending until their own launch acknowledges them."""
            _bind_decision_launch(session_uuid, role, None)
            rids = decision_included.pop(role, None)
            if not rids:
                return
            for rid in rids:
                try:
                    state_store.mark_decision_delivered(session_uuid, rid,
                                                        target=role)
                except (OSError, TimeoutError, ValueError, KeyError) as exc:
                    # The same store failures
                    # `_acknowledge_capacity_turn_decisions` reports.
                    # The block reached the role but the acknowledgment was
                    # not recorded: it stays pending and a later run re-sends
                    # it (at least once). Reported, never claimed delivered.
                    result_box.setdefault("decision_ack_failed", []).append(
                        {"request_id": rid, "target": role})
                    trace.event("decision.delivery.ack_failed", role=role,
                                request_id=rid, error=type(exc).__name__)
                    continue
                trace.event("decision.delivery.ack", role=role,
                            request_id=rid)
            decision_pending_for[role] = [
                rid for rid in decision_pending_for.get(role) or ()
                if rid not in rids]
            decision_answer_for[role] = [
                rid for rid in decision_answer_for.get(role) or ()
                if rid not in rids]
            decline_note_for.pop(role, None)

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
        # M4D-MAJ-03: reset the shared, process-global shutdown event at THIS
        # run's own boundary -- before the real SIGTERM handler below is
        # installed, and before any activity tick this run creates can ever
        # observe it. Without this, a SIGTERM (real, or -- as `test_cowork.
        # py`'s own frozen `os.kill(os.getpid(), signal.SIGTERM)` regressions
        # exercise -- simulated within an earlier `run_flow` call in the SAME
        # process) leaves `_ACTIVITY_SHUTDOWN_EVENT` set, silently suppressing
        # every tick/turn-boundary append for every LATER, otherwise-healthy
        # run_flow invocation in that process: a genuinely per-run signal
        # leaking process-wide scope. Cleared, never set, here -- only a real
        # SIGTERM inside THIS run's own `_handle_external_kill` may set it.
        _ACTIVITY_SHUTDOWN_EVENT.clear()

        active_work_box = {"session_uuid": session_uuid, "work_id": None}
        _prior_sigterm_handler = None

        def _handle_external_kill(signum, frame):
            # M4 Package D: set FIRST, before the durable `aborted` write below
            # -- see `_ACTIVITY_SHUTDOWN_EVENT`'s own module-level docstring for
            # why this ordering is what closes the post-SIGTERM append race.
            _ACTIVITY_SHUTDOWN_EVENT.set()
            _advance_phase(
                active_work_box["session_uuid"], active_work_box["work_id"],
                "aborted", evidence={"reason": "sigterm"}, source="signal",
                unlocked=True)
            if trace:
                trace.event("run.external_kill", role_work_id=active_work_box["work_id"])
            # Issue #64: mark this owner terminal on the SIGNAL path -- and
            # only ever through the per-owner sidecar, which takes no lock,
            # reads nothing, appends to no JSONL and cannot name any other
            # owner's file, so a successor's `owner/lease.json` is left
            # byte-identical. APPENDED after the three pre-existing effects
            # are already committed, and wrapped, so a #64 failure can never
            # cost this handler its durable `aborted` record, its trace event
            # or its `SystemExit`.
            _octx = _current_owner_context()
            if _octx["enforced"] and _octx["owner_id"]:
                try:
                    cowork_owner.mark_owner_terminal_unlocked(
                        _octx["session_uuid"], _octx["owner_id"],
                        _octx["epoch"], "sigterm",
                        owner_matched=_octx["matched"])
                except Exception:  # noqa: BLE001 - never suppresses SystemExit
                    pass
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
                        # rc 2 only while nothing has run this invocation.
                        rc = refuse("handoff_target_not_selected",
                                    "cannot run the scouting phase — scout is "
                                    "not on the team.",
                                    rc=1 if result_box.get("last") else 2)
                        break
                    if not reviewer_ready_for("scout"):
                        rc = result_box.get("refusal_rc", 2)
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
                    scout_first_send_cb = decision_first_send("scout", scout_first_send_cb)
                    rc = run_scout_fn(
                        config,
                        with_agent_lead_note(with_decision_note("scout", seed_with_switch_note(
                            "scout", deliver_context("scout", scout_seed)))),
                        selected,
                        io_out=io_out,
                        evaluation_policy=evaluation_policy,
                        resume_id=role_resume_id("scout"),
                        on_session=role_saver("scout"),
                        intel_path=intel_path, review_path=review_path,
                        reviewer_resume_id=role_resume_id(SCOUT_REVIEWER),
                        on_reviewer_session=role_saver(SCOUT_REVIEWER),
                        reviewer_context=reviewer_context_now(),
                        reviewer_context_update=reviewer_gap(SCOUT_REVIEWER)
                        if SCOUT_REVIEWER in selected else None,
                        on_reviewer_context_ack=reviewer_acker(SCOUT_REVIEWER),
                        trace=trace,
                        eval_scratch_path=eval_scratch["scout"],
                        reviewer_eval_scratch_path=eval_scratch[SCOUT_REVIEWER],
                        scores_path=scores_path, session_uuid=session_uuid,
                        intel_md_path=intel_md_path,
                        skip_baseline=scout_skip_baseline,
                        review_packet_ctx={"epoch": scouting_epoch_box["epoch"],
                                           "attempt": scout_attempt_box["attempt"],
                                           "context_revision": current_rev},
                        reviewer_switch_note_fn=switch_note_for,
                        on_reviewer_switch_consumed=clear_pending_switch_for,
                        on_first_send_accepted=scout_first_send_cb,
                        on_first_send_rejected=scout_first_send_rejected_cb,
                        reviewer_controller_check_fn=reviewer_controller_check,
                        save_pending_turn_fn=save_pending_turn_for,
                        clear_pending_turn_fn=clear_pending_switch_for,
                        worktree=active_worktree, worktree_base=active_worktree_root,
                        on_outcome=lambda o, p=None: outcome_box.update(
                            outcome=o, payload=p))
                    _bind_decision_launch(session_uuid, "scout", None)
                    record_outcome("scout", outcome_box)
                    if rc != 0:
                        recover_controller_failure("scout", "startup_or_probe")
                        break
                    if rc == 0 and outcome_box["outcome"] == _OUTCOME_PROCESS_TERMINATED:
                        # M4 Package D: provider refusal/no-first-token, no
                        # fallback -- terminate the WHOLE process nonzero rather
                        # than falling through to the ordinary rc==0 end.
                        rc = (outcome_box["payload"] or {}).get(
                            "exit_code", PROVIDER_REFUSAL_EXIT_CODE)
                        break
                    if rc == 0 and scout_first_send_box["delivered"]:
                        ack_lead("scout")
                        decision_delivered("scout")
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
                            planner_seed = with_decision_record(
                                assemble_planner_seed(
                                    intel_path, shared_context, intel_dir,
                                    current_rev))
                        continue
                    break

                if phase == "planning":
                    if not reviewer_ready_for("planner"):
                        rc = result_box.get("refusal_rc", 2)
                        break
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
                    planner_first_send_cb = decision_first_send("planner", planner_first_send_cb)
                    rc = run_planner_fn(
                        config,
                        with_agent_lead_note(with_decision_note("planner", seed_with_switch_note(
                            "planner",
                            deliver_context(
                                "planner",
                                planner_seed if planner_seed is not None else "")))),
                        selected, io_out=io_out,
                        evaluation_policy=evaluation_policy,
                        resume_id=role_resume_id("planner"),
                        on_session=role_saver("planner"),
                        plan_json_path=plan_json_path, plan_md_path=plan_md_path,
                        review_path=planner_review_path,
                        reviewer_resume_id=role_resume_id(PLANNING_ADVISOR),
                        on_reviewer_session=role_saver(PLANNING_ADVISOR),
                        reviewer_context=reviewer_context_now(),
                        reviewer_context_update=reviewer_gap(PLANNING_ADVISOR)
                        if PLANNING_ADVISOR in selected else None,
                        on_reviewer_context_ack=reviewer_acker(PLANNING_ADVISOR),
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
                        reviewer_switch_note_fn=switch_note_for,
                        on_reviewer_switch_consumed=clear_pending_switch_for,
                        on_first_send_accepted=planner_first_send_cb,
                        on_first_send_rejected=planner_first_send_rejected_cb,
                        reviewer_controller_check_fn=reviewer_controller_check,
                        save_pending_turn_fn=save_pending_turn_for,
                        clear_pending_turn_fn=clear_pending_switch_for,
                        worktree=active_worktree, worktree_base=active_worktree_root,
                        on_outcome=lambda o, p: planner_box.update(outcome=o, payload=p))
                    _bind_decision_launch(session_uuid, "planner", None)
                    record_outcome("planner", planner_box)
                    if rc != 0:
                        recover_controller_failure("planner", "startup_or_probe")
                        break
                    if rc == 0 and planner_box["outcome"] == _OUTCOME_PROCESS_TERMINATED:
                        rc = (planner_box["payload"] or {}).get(
                            "exit_code", PROVIDER_REFUSAL_EXIT_CODE)
                        break
                    if rc == 0 and planner_first_send_box["delivered"]:
                        ack_lead("planner")
                        decision_delivered("planner")
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
                            builder_seed = with_decision_record(
                                assemble_builder_seed(
                                    plan_json_path, plan_md_path,
                                    shared_context, intel_dir, current_rev))
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
                if not reviewer_ready_for("builder"):
                    rc = result_box.get("refusal_rc", 2)
                    break
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
                builder_first_send_cb = decision_first_send("builder", builder_first_send_cb)
                rc = run_builder_fn(
                    config,
                    with_agent_lead_note(with_decision_note("builder", seed_with_switch_note(
                        "builder",
                        deliver_context("builder",
                                        builder_seed if builder_seed is not None else "")))),
                    selected, io_out=io_out,
                    evaluation_policy=evaluation_policy,
                    resume_id=role_resume_id("builder"),
                    on_session=role_saver("builder"),
                    build_status_path=build_status_path,
                    build_review_path=build_review_path,
                    reviewer_resume_id=role_resume_id(BUILD_REVIEWER),
                    on_reviewer_session=role_saver(BUILD_REVIEWER),
                    reviewer_context=reviewer_context_now(),
                    reviewer_context_update=reviewer_gap(BUILD_REVIEWER)
                    if BUILD_REVIEWER in selected else None,
                    on_reviewer_context_ack=reviewer_acker(BUILD_REVIEWER),
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
                    reviewer_switch_note_fn=switch_note_for,
                    on_reviewer_switch_consumed=clear_pending_switch_for,
                    on_first_send_accepted=builder_first_send_cb,
                    on_first_send_rejected=builder_first_send_rejected_cb,
                    reviewer_controller_check_fn=reviewer_controller_check,
                    save_pending_turn_fn=save_pending_turn_for,
                    clear_pending_turn_fn=clear_pending_switch_for,
                    worktree=active_worktree, worktree_base=active_worktree_root,
                    on_outcome=lambda o, p: builder_box.update(outcome=o, payload=p))
                _bind_decision_launch(session_uuid, "builder", None)
                record_outcome("builder", builder_box)
                if rc != 0:
                    recover_controller_failure("builder", "startup_or_probe")
                    break
                if rc == 0 and builder_box["outcome"] == _OUTCOME_PROCESS_TERMINATED:
                    rc = (builder_box["payload"] or {}).get(
                        "exit_code", PROVIDER_REFUSAL_EXIT_CODE)
                    break
                if rc == 0 and builder_first_send_box["delivered"]:
                    ack_lead("builder")
                    decision_delivered("builder")
                # Build approval is terminal for this run (the phase stays `building`,
                # so a rerun resumes the builder conversation), and EOF/interrupt ends
                # the run the same way.
                break
        finally:
            # No launch outlives the loop: a lead that raised never leaves its
            # decision bindings behind for a later launch in this process.
            for _bound_role in ("scout", "planner", "builder"):
                _bind_decision_launch(session_uuid, _bound_role, None)
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
        result_box["phase"] = phase
        rc = _final_rc(rc, (result_box.get("last") or {}).get("outcome"))
        trace.event("run.end", rc=rc,
                    outcome=(result_box.get("last") or {}).get("outcome"))
        return rc
    except cowork_owner.OwnerLeaseError as exc:
        # Catch point 1 (plan §3.7): the DECLARED BASE, never a tuple of
        # subclass names -- one name that covers every subclass and cannot
        # fall out of date when one is added. It sits inside the `try` whose
        # `finally` releases, so the release still runs, and the release is a
        # compare-and-swap, so it writes nothing when the lease is already
        # foreign.
        release_reason = "owner_refusal"
        trace.event("run.end", rc=3,
                    reason=cowork_owner.owner_refusal_reason(exc))
        io_out.write(cowork_owner.refusal_message(
            exc, session_uuid=session_uuid, session_file=spath))
        io_out.flush()
        return 3
    except state_store.DecisionAnswerTampered as exc:
        # A recorded orchestrator answer no longer has the bytes the
        # orchestrator gave: nothing more is delivered under its authority,
        # and any delivery still owed stays pending. The request is already
        # consumed, so it cannot be answered again: the only recovery is
        # restoring the exact bytes (every later run re-verifies every
        # recorded answer and stops the same way until then).
        result_box["reason"] = "decision_answer_tampered"
        result_box["stop"] = {
            "kind": "decision_answer_tampered",
            "request_id": exc.request_id,
            "answer_path": exc.answer_path,
            "expected_sha256": exc.expected_sha256}
        trace.event("decision.answer.tampered", request_id=exc.request_id,
                    answer_path=exc.answer_path,
                    expected_sha256=exc.expected_sha256)
        trace.event("run.end", rc=1, reason="decision_answer_tampered")
        io_out.write("cowork: %s\n" % exc)
        if exc.expected_sha256:
            io_out.write(
                "cowork: recovery: restore the exact bytes of the answer to "
                "request %s at %s (sha256 %s), then rerun; the request is "
                "consumed and cannot be answered again\n" % (
                    exc.request_id, exc.answer_path, exc.expected_sha256))
        io_out.flush()
        return 1
    except KeyboardInterrupt:
        release_reason = "interrupted"
        raise
    except EOFError:
        release_reason = "input_closed"
        raise
    except BaseException:
        release_reason = "crash"
        raise
    finally:
        # Every exit path -- all ~15 returns, the refusal above, Ctrl-C, a
        # closed stdin, an external kill's SystemExit and any crash -- lands
        # here exactly once. `KeyboardInterrupt`/`EOFError` unwind through
        # THIS `finally` before `main()`'s own handlers ever see them, which
        # is why `main` needs no lease logic of its own.
        if heartbeat_stop_event is not None:
            heartbeat_stop_event.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2.0)
        if owner_id is not None:
            cowork_owner.release_owner_lease(
                session_uuid, owner_id, owner_epoch, release_reason)
            try:
                cowork_owner.release_provider_bindings(session_uuid, owner_id)
            except cowork_owner.OwnerLeaseError as _binding_exc:
                # Best-effort by contract: a binding-index failure during
                # teardown is traced, never allowed to displace this run's
                # real exit code.
                trace.event("owner.binding_release_failed",
                            error_type=type(_binding_exc).__name__,
                            detail=str(_binding_exc))
        _restore_owner_context(prior_owner_context)


# --------------------------------------------------------------------------- #
# M3 Package E: resume-trigger CLI, consumed externally by                   #
# Package F's wake adapters -- a versioned contract deliberately narrow, in  #
# the same spirit as D's own `cowork_capacity_scheduler.run_wake_trigger`:   #
# claim, binding-preflight, InvalidationRecord no-replay, and the ONE        #
# exactly-once replay of the persisted pending turn. It never drives the     #
# rest of the role's lead loop -- once the accepted send lands, an explicit #
# `cowork.py --session-file` (or `--resume`) run continues normally.        #
# --------------------------------------------------------------------------- #

RESUME_TRIGGER_CONTRACT_VERSION = 1

RESUME_TRIGGER_EXIT_SUCCESS = capacity_scheduler.WAKE_TRIGGER_EXIT_SUCCESS
RESUME_TRIGGER_EXIT_INTERNAL_ERROR = capacity_scheduler.WAKE_TRIGGER_EXIT_INTERNAL_ERROR
RESUME_TRIGGER_EXIT_INVALID_ARGUMENTS = (
    capacity_scheduler.WAKE_TRIGGER_EXIT_INVALID_ARGUMENTS)
RESUME_TRIGGER_EXIT_NOT_DUE = capacity_scheduler.WAKE_TRIGGER_EXIT_NOT_DUE
RESUME_TRIGGER_EXIT_CONFLICT = capacity_scheduler.WAKE_TRIGGER_EXIT_CONFLICT
RESUME_TRIGGER_EXIT_ATTEMPTS_EXHAUSTED = (
    capacity_scheduler.WAKE_TRIGGER_EXIT_ATTEMPTS_EXHAUSTED)
# E-specific outcomes additive beyond D's own WAKE_TRIGGER_EXIT_CODES.
RESUME_TRIGGER_EXIT_BINDING_MISMATCH = 6
RESUME_TRIGGER_EXIT_INVALIDATED = 7
RESUME_TRIGGER_EXIT_NO_PENDING_TURN = 8
RESUME_TRIGGER_EXIT_SEND_FAILED = 9
# Issue #64: the single-writer refusal. 10 is the FIRST FREE INTEGER -- 0-9
# above are fully allocated -- so this is additive, and no pre-existing
# outcome-name -> integer mapping moves.
RESUME_TRIGGER_EXIT_OWNER_CONFLICT = 10
# Issue #64 P3: the provider/controller exclusivity refusals. 11 and 12 are the
# NEXT FREE INTEGERS -- 0-10 above are fully allocated -- so these are additive
# too, and no pre-existing outcome-name -> integer mapping moves. The two are
# deliberately DISTINCT codes because they are distinct facts: 11 is a PROVEN
# collision (a different live session owns that provider conversation), 12 is
# an index that could not be consulted at all and proves nothing.
RESUME_TRIGGER_EXIT_PROVIDER_SESSION_BOUND = 11
RESUME_TRIGGER_EXIT_PROVIDER_BINDING_UNAVAILABLE = 12

# The concrete, versioned outcome-name -> exit-code contract Package F must
# consult by name (never a bare literal integer) -- matching D's own
# `WAKE_TRIGGER_EXIT_CODES` naming/versioning discipline exactly, extended
# additively rather than renumbered.
RESUME_TRIGGER_EXIT_CODES = dict(capacity_scheduler.WAKE_TRIGGER_EXIT_CODES)
RESUME_TRIGGER_EXIT_CODES.update({
    "binding_mismatch": RESUME_TRIGGER_EXIT_BINDING_MISMATCH,
    "invalidated": RESUME_TRIGGER_EXIT_INVALIDATED,
    "no_pending_turn": RESUME_TRIGGER_EXIT_NO_PENDING_TURN,
    "send_failed": RESUME_TRIGGER_EXIT_SEND_FAILED,
    "owner_conflict": RESUME_TRIGGER_EXIT_OWNER_CONFLICT,
    "provider_session_bound": RESUME_TRIGGER_EXIT_PROVIDER_SESSION_BOUND,
    "provider_binding_unavailable": (
        RESUME_TRIGGER_EXIT_PROVIDER_BINDING_UNAVAILABLE),
})

_EPOCH_GETTER_FOR_ROLE = {
    "scout": state_store.get_scouting_epoch,
    "planner": state_store.get_planning_epoch,
    "builder": state_store.get_building_epoch,
}


def build_resume_trigger_arg_parser():
    """Build the versioned resume-trigger argument parser (see
    RESUME_TRIGGER_CONTRACT_VERSION).

    `--role` and `--now` are BOTH optional (never `required=True`), unlike
    D's own pure decision-layer wake-trigger contract: Package F's own
    fixed, unchangeable `build_resume_trigger_argv` argv shape supplies
    ONLY `--session-uuid --lease-id --claimant-ref --automation-ref` --
    E's CLI must stay compatible with that EXACT argv, never require a flag
    F itself can never supply.

    `--role`, when omitted, is derived from the CLAIMED PauseLease's own
    bound `role` field (the only genuinely available source at THIS
    external entrypoint when the caller supplies none) -- when explicitly
    given (every non-F/manual/test invocation), the wrong-first-role
    cross-check still applies exactly as before.

    `--now`, when omitted, is read from the real wall clock (this is a
    genuine external CLI process boundary, unlike D's in-process pure
    decision layer, which never touches the wall clock for fake-clock
    testability) -- when explicitly given (every test invocation), that
    exact value is used, preserving full fake-clock determinism for tests."""
    parser = argparse.ArgumentParser(
        prog="cowork.py resume-trigger", add_help=True,
        description=(
            "M3 Package E versioned resume-trigger contract (v%d), "
            "consumed externally by Package F's wake adapters: claim a due "
            "PauseLease (D's scheduler contract), prove the claimed binding "
            "is still exactly the one this engagement paused on, and replay "
            "the exactly-once persisted pending turn." %
            RESUME_TRIGGER_CONTRACT_VERSION))
    parser.add_argument("--session-uuid", required=True)
    parser.add_argument(
        "--role", default=None,
        help="expected bound role; when omitted (Package F's own fixed "
             "argv never supplies it) the role is derived from the "
             "claimed lease's own bound role instead")
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--claimant-ref", required=True)
    parser.add_argument("--automation-ref", required=True)
    parser.add_argument(
        "--now", default=None,
        help="explicit RFC3339 clock reading; when omitted (Package F's "
             "own fixed argv never supplies it) the real wall clock is "
             "read instead")
    parser.add_argument(
        "--cwd", default=None,
        help="working directory the target session lives under; defaults "
             "to the current directory")
    parser.add_argument("--reference-now", default=None)
    parser.add_argument(
        "--max-clock-skew-seconds", type=float,
        default=capacity_scheduler.DEFAULT_MAX_CLOCK_SKEW_SECONDS)
    parser.add_argument(
        "--manual-signal-record", default=None,
        help="path to a JSON manual-capacity-signal record -- the ONLY way "
             "to authorize an early/manual_signal-mode claim (E holds no "
             "private signing key and mints no signature itself); omit for "
             "the ordinary trustworthy-scheduled form")
    parser.add_argument(
        "--pinned-public-keys", default=None,
        help="path to a JSON {signer_public_key_id: hex_public_key} "
             "registry, required together with --manual-signal-record")
    parser.add_argument(
        "--redirected-context", default=None,
        help="optional replacement context text for the resumed turn "
             "(redirected-context resume); omitted means a no-new-context "
             "resume that replays the persisted pending turn verbatim")
    return parser


def _load_json_file(path):
    with open(path, "r") as fh:
        return json.load(fh)


def _find_session_state(cwd, session_uuid):
    """Locate and load the durable session state for `session_uuid` under
    `cwd` -- tries the modern per-uuid filename first (the fast, common
    path) and falls back to scanning every discovered session file
    (`state_store.discover_session_files`, which also covers a legacy
    single-session `.cowork/session.json`) for the one whose OWN persisted
    `session_uuid` field matches -- never guesses a different session's
    state merely because a file happened to be present. None when nothing
    matches."""
    state = state_store.load(state_store.new_session_path(cwd, session_uuid))
    if state is not None:
        return state
    for path in state_store.discover_session_files(cwd):
        candidate = state_store.load(path)
        if candidate is not None and state_store.get_session_uuid(
                candidate) == session_uuid:
            return candidate
    return None


def _current_role_work_id_for_session(cwd, session_uuid, role):
    """Deterministically re-derive the LIVE WorkUnit identity for (session,
    role) from durable state alone -- the same identity scheme
    `_role_work_id`/`_resolve_attempt_start` already use for an in-process
    resume, re-derived here for a standalone resume-trigger CLI invocation
    that starts with no in-memory epoch/attempt counters of its own. None
    when `role` has no known epoch family or the session state is
    unreadable."""
    getter = _EPOCH_GETTER_FOR_ROLE.get(role)
    if getter is None:
        return None
    state = _find_session_state(cwd, session_uuid)
    if state is None:
        return None
    epoch = getter(state)
    attempt = _resolve_attempt_start(session_uuid, role, epoch)
    return _role_work_id(session_uuid, role, epoch, attempt)


def _resume_seed_delivery(pending_text, redirected_context):
    """Typed seed for a resume-trigger turn -- issue #57: `pending_text`
    and any `redirected_context` override MUST pass through the closed
    boundary constructors before reaching any transport call, never a bare/
    untyped string handed straight to a session's send(). Covers both
    resume forms the frozen brief names: a no-new-context resume
    (`redirected_context` is None -- the exact persisted turn replays
    verbatim) and a redirected-context resume (a fresh context string
    substitutes for it). Raises TypeError -- never lets an untyped value
    reach `_send` -- for anything that is not genuinely a string."""
    text = pending_text if redirected_context is None else redirected_context
    if not isinstance(text, str):
        raise TypeError(
            "resume seed text must be a string, got %r" % (type(text),))
    return _initial_user_delivery(text)


def _resume_wake_failure_kind(lease, current_binding, provider_session_id):
    """The genuine `cowork_control_plane._BINDING_WAKE_FAILURE_KINDS` member
    naming why a claimed lease's binding no longer matches this
    engagement's CURRENT durable identity, checked in a fixed,
    most-specific-first order -- or None when the binding still matches."""
    if current_binding is None:
        return "candidate_mismatch"
    if lease["provider_session_id"] != provider_session_id:
        return "session_mismatch"
    if (lease["controller_policy_digest"]
            != current_binding["controller_policy_digest"]):
        return "controller_policy_mismatch"
    if lease["candidate_digest"] != current_binding["candidate_manifest_digest"]:
        return "candidate_mismatch"
    return None


def _construct_resume_session(role, controller, cfg, resume_provider_session_id,
                              sessions_dir, trace):
    """Reconstruct a live bridge session for the exactly-once resumed send,
    from the SAME durable (controller, model, effort, mode, yolo) config the
    original dispatch used (`state["config"][role]`) -- never guessed or
    defaulted differently from the engagement's own genuine identity.
    Returns None for an unrecognized controller."""
    prompt_path = ROLE_PROMPT_PATHS.get(role) or SCOUT_PROMPT_PATH
    mode = cfg.get("mode", "implement")
    yolo = cfg.get("yolo", True)
    model = cfg.get("model")
    effort = cfg.get("effort")
    devnull = open(os.devnull, "w")
    if controller == "claude":
        return bridge.ClaudeSession(
            prompt_path, mode, yolo, io_out=devnull, speaker=role,
            resume_id=resume_provider_session_id, trace=trace,
            extra_writable_dir=sessions_dir, model=model, effort=effort)
    if controller == "codex":
        return bridge.CodexSession(
            mode, yolo, io_out=devnull, speaker=role,
            resume_thread_id=resume_provider_session_id, trace=trace,
            extra_writable_dir=sessions_dir, model=model, effort=effort)
    if controller == "opencode":
        return bridge.OpencodeSession(
            prompt_path, mode, yolo, io_out=devnull, speaker=role,
            resume_session_id=resume_provider_session_id, trace=trace,
            extra_writable_dir=sessions_dir, model=model, effort=effort)
    return None


def _account_failed_wake_attempt(session_uuid, lease, automation_ref):
    """Durably account ONE genuine failed wake/resume attempt against
    `lease`'s own per-binding `failed_wake_attempts` counter -- TRUTHFULLY,
    regardless of whether `lease` is still `unclaimed` (a standalone/manual
    resume-trigger invocation that never itself claimed) or already
    `claimed` (Package F's OWN real claim-then-invoke ordering: F's
    `fire()` claims the lease via D's `run_wake_trigger` BEFORE ever
    invoking this CLI as a subprocess, so by the time E's own preflight
    runs, the lease is typically already `claimed` under the SAME
    claimant_ref/automation_ref this function receives).

    `capacity_scheduler.record_failed_wake_attempt` (D, wrapping B) can
    only ever increment an `unclaimed` lease. For an already-`claimed`
    lease this SAME-BINDING REPLACES it first
    (`capacity_scheduler.replace`, which durably carries
    `failed_wake_attempts` FORWARD, never resets it -- M3R-N06/D-MJ-01) to
    obtain a fresh `unclaimed` record for the SAME binding, and only THEN
    records the genuine failed attempt on THAT record -- net effect: the
    counter genuinely increments by exactly 1, the binding is left with a
    fresh `unclaimed` lease available for a future wake attempt (up to the
    ceiling), and the per-binding chain is never reset. Composes ONLY D's/
    B's own existing atomic accessors -- no new storage, no new lock,
    exactly the frozen brief's own constraint.

    The replacement's `resume_mode`/`not_before`/`issued_at` are carried
    forward VERBATIM from `lease` itself (never re-derived, never set to
    "now") -- this is a bookkeeping continuation of the SAME pause
    episode, not a new capacity signal: a `scheduled`-mode binding stays
    automatically re-claimable by Package F's own ordinary wake trigger
    across every accounted failure (an already-validated `not_before`
    paired with its OWN original `issued_at` is trivially still within
    Package A's retry horizon), while a `manual_signal`-mode binding
    correctly keeps requiring a genuinely authorized signal every time.

    Best-effort throughout (never raises): a lease already moved on (a
    genuine race, or an already-ceiling-exhausted binding refusing the
    increment) is truthfully left exactly as it durably is -- this
    helper's own failure is never mistaken for, and never masks, the
    caller's own refusal reason."""
    try:
        current = state_store.read_pause_lease(session_uuid, lease["lease_id"])
        if current is None:
            return
        state = current.get("consumption_state")
        target_lease_id = lease["lease_id"]
        if state == "claimed":
            new_lease_id = str(uuid.uuid4())
            new_lease = {
                "schema_version": lease["schema_version"],
                "package_id": lease["package_id"],
                "lease_id": new_lease_id,
                "role": lease["role"],
                "provider_session_id": lease["provider_session_id"],
                "controller_policy_digest": lease["controller_policy_digest"],
                "candidate_digest": lease["candidate_digest"],
                "resume_mode": lease["resume_mode"],
                "not_before": lease["not_before"],
                "automation_ref": automation_ref,
                "artifact_hashes": dict(lease["artifact_hashes"]),
                "consumption_state": "unclaimed",
                "failed_wake_attempts": 0,
                "issued_at": lease["issued_at"],
            }
            capacity_scheduler.replace(
                session_uuid, target_lease_id, new_lease, automation_ref)
            target_lease_id = new_lease_id
        elif state != "unclaimed":
            return
        capacity_scheduler.record_failed_wake_attempt(
            session_uuid, target_lease_id, automation_ref)
    except Exception:  # noqa: BLE001 - best-effort accounting, never masks the caller's refusal
        pass


def run_resume_trigger(argv, output=None, session_factory=None):
    """Versioned resume-trigger entrypoint (see
    RESUME_TRIGGER_CONTRACT_VERSION), consumed externally by Package F's
    wake adapters -- writes exactly one JSON result line to `output`
    (defaults to `sys.stdout.write`) and returns one of
    RESUME_TRIGGER_EXIT_CODES; NEVER calls `sys.exit` itself, so it is safe
    to call repeatedly, including from tests.

    `--role`/`--now` argv compatibility: Package F's own fixed
    `build_resume_trigger_argv` NEVER supplies `--role` or `--now` (only
    `--session-uuid --lease-id --claimant-ref --automation-ref`) -- both
    default to None and are resolved here: `effective_role` from the
    lease's own bound role when `--role` is omitted (the wrong-first-role
    cross-check still applies whenever `--role` IS explicitly given), and
    `effective_now` from the real wall clock when `--now` is omitted (a
    genuine external CLI process boundary, unlike D's pure decision layer).

    Order of operations, each fail-closed:
      0. Consult D's own `wake_decision` BEFORE ever attempting the
         state-mutating claim (matching D's own `run_wake_trigger`
         ordering) -- an already-ceiling-exhausted binding refuses outright
         (`attempts_exhausted`), never attempting another claim.
      1. Read-only PRE-CLAIM snapshot of the stored lease, then
         wrong-first-role and ALL binding/evidence preflight checks
         (candidate binding, provider-session/policy binding, the
         persisted-pending-turn precondition) against THAT snapshot --
         entirely BEFORE this CLI's OWN state-mutating claim attempt. A
         refusal here never itself claims the lease (so, for the
         manual-signal form, never consumes the single-use signal either)
         and durably accounts ONE genuine failed wake attempt
         (`_account_failed_wake_attempt` -- truthful whether the lease is
         still unclaimed, as in a standalone invocation, or ALREADY
         claimed by Package F/D before this CLI even started, per F's own
         claim-then-invoke ordering) -- the mechanism that makes
         `FAILED_WAKE_ATTEMPT_CEILING` genuinely reachable across repeated
         resume-trigger failures under BOTH invocation shapes (closes
         F-MJ-02).
      1b. Issue #64 P3, provider/controller EXCLUSIVITY, still read-only and
         still pre-claim: refuse (`provider_session_bound`, exit 11) when the
         provider conversation this trigger would resume is bound to a
         DIFFERENT session whose owner lease is still live, and refuse
         (`provider_binding_unavailable`, exit 12) when the index cannot be
         consulted at all. Placed AFTER every check above -- so their
         precedence is unchanged -- and BEFORE the ownership acquire, so a
         refusal here has claimed nothing, advanced no phase and constructed
         no controller session. Unlike the refusals above it accounts NO
         failed wake attempt: nothing was attempted.
      2. Claim the named PauseLease -- via D's ordinary `claim` (the
         trustworthy-scheduled form) or, when `--manual-signal-record` is
         given, D's `claim_with_authorized_early_override` (the ONLY path
         that may claim a manual_signal-mode/early lease, and only after
         genuine Ed25519 verification against the caller-pinned public-key
         registry -- E holds no private key and mints no signature itself,
         so D/B refuse an unsigned or self-claimed signal before this
         function ever observes a "claimed" outcome). Under Package F's own
         invocation this is IDEMPOTENT (D's own same-owner "already_claimed"
         outcome, since F already claimed this lease under the SAME
         claimant_ref before invoking this CLI) -- never a second, distinct
         claim. A genuine operational claim failure (OSError) ALSO durably
         accounts one failed wake attempt, exactly like D's own
         `run_wake_trigger`.
      3. A race-safety re-check against the just-claimed lease (the
         pre-claim snapshot could in principle be stale by the time the
         claim itself lands) -- refused truthfully via `_release_and_
         conflict` (same-binding replace-and-increment; never stranded).
      4. Advance `capacity_wake_claimed` (awaiting_capacity -> preflighting)
         with `expected_candidate` drawn from THIS engagement's CURRENT
         durable WorkUnit+manifest binding and evidence citing the LEASE's
         own candidate digest: the reducer's own M3A-REV-001-RESIDUAL
         candidate check refuses (leaving the state at `awaiting_capacity`,
         unchanged) when they disagree -- the candidate half of
         binding-preservation is enforced by Package A itself, never
         re-derived here. A refusal here releases the just-claimed lease
         (`_release_and_conflict`).
      5. Binding-preserving wake preflight, re-checked post-claim as
         defense-in-depth against the same TOCTOU race -- refused
         (`capacity_wake_preflight_failed`, back to `awaiting_capacity`)
         with the lease released, never stranded.
      6. InvalidationRecord no-replay: refuses outright (advancing
         `preflight_rejected`, a terminal state, lease CANCELLED -- this
         exact candidate can never legally resume again, so no further
         wake-ceiling accounting is meaningful) when this exact candidate
         has an InvalidationRecord on file -- never replays completed
         paired work without one.
      7. Only once 0-6 all hold: advance `preflight_passed` (preflighting
         -> running) and attempt the accepted send using a typed seed
         (`_resume_seed_delivery` -- issue #57: never an untyped seed). The
         lease is NOT yet marked consumed -- consumption must reflect the
         TRUE outcome (see step 8).
      8. On a successful send: mark the lease consumed FIRST, then consume
         the pending turn exactly once (`clear_pending_turn_before_pause`)
         -- an accepted send consumes exactly once. On a failed send: the
         pending turn is RETAINED (never cleared) and the lease is NEVER
         marked consumed for a failed send -- classify + record
         ProviderHealth exactly like the live seam, and either re-enter
         `awaiting_capacity` via a SAME-BINDING REPLACEMENT of this exact
         claimed lease (`_enter_awaiting_capacity(..., replace_lease_id=
         ...)` -- preserves `failed_wake_attempts` monotonically AND
         accounts this repeat signal as one more genuine failed attempt,
         never resets the per-binding automatic-recovery chain via a fresh
         episode) for a genuine repeat quota/overload, or CANCELS the
         lease and advances `execution_failed` (a terminal WorkUnit outcome
         -- no further wake-ceiling accounting is meaningful; no
         decision gate exists in a resume trigger)."""
    write = output if output is not None else sys.stdout.write
    parser = build_resume_trigger_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return RESUME_TRIGGER_EXIT_INVALID_ARGUMENTS

    if args.manual_signal_record and not args.pinned_public_keys:
        write(json.dumps({
            "outcome": "invalid_arguments",
            "detail": "--pinned-public-keys is required with "
                      "--manual-signal-record"}) + "\n")
        return RESUME_TRIGGER_EXIT_INVALID_ARGUMENTS

    session_uuid = args.session_uuid
    cwd = args.cwd or os.getcwd()
    effective_now = args.now if args.now is not None else _capacity_now()

    # Step 0 (D-MJ-02 parity, F-MJ-02 seam): consult wake eligibility BEFORE
    # any state-mutating claim.
    try:
        decision = capacity_scheduler.wake_decision(session_uuid, args.lease_id)
    except ValueError:
        write(json.dumps({"outcome": "conflict", "lease_id": args.lease_id,
                          "reason": "not_found"}) + "\n")
        return RESUME_TRIGGER_EXIT_CONFLICT
    if decision == "wake_attempts_exhausted":
        write(json.dumps({"outcome": "attempts_exhausted",
                          "lease_id": args.lease_id}) + "\n")
        return RESUME_TRIGGER_EXIT_ATTEMPTS_EXHAUSTED

    # Step 1: read-only pre-claim snapshot. Every wrong-first-role/binding/
    # evidence preflight check below runs against THIS snapshot.
    stored_lease = state_store.read_pause_lease(session_uuid, args.lease_id)
    if stored_lease is None:
        write(json.dumps({"outcome": "conflict", "lease_id": args.lease_id,
                          "reason": "not_found"}) + "\n")
        return RESUME_TRIGGER_EXIT_CONFLICT
    try:
        precheck_lease = capacity_contracts.validate_pause_lease(
            state_store.pause_lease_from_stored_record(stored_lease))
    except ValueError as exc:
        write(json.dumps({"outcome": "internal_error",
                          "detail": str(exc)}) + "\n")
        return RESUME_TRIGGER_EXIT_INTERNAL_ERROR

    effective_role = args.role if args.role is not None else precheck_lease["role"]

    def _preflight_conflict(reason, code, **details):
        _account_failed_wake_attempt(
            session_uuid, precheck_lease, args.automation_ref)
        line = {"outcome": "conflict",
                "lease_id": precheck_lease["lease_id"],
                "reason": reason}
        line.update(details)
        write(json.dumps(line) + "\n")
        return code

    if args.role is not None and args.role != precheck_lease["role"]:
        # Wrong first role after resume: stop durably for supervision --
        # refused before any claim/phase-advance is even attempted under
        # the wrong identity. (Never checked when `--role` is omitted --
        # Package F's own fixed argv never supplies one to cross-check.)
        return _preflight_conflict("role_mismatch", RESUME_TRIGGER_EXIT_BINDING_MISMATCH)

    work_id = _current_role_work_id_for_session(cwd, session_uuid, effective_role)
    current_binding = (
        _capacity_candidate_binding(session_uuid, work_id, effective_role)
        if work_id else None)
    state = _find_session_state(cwd, session_uuid)
    cfg = ((state or {}).get("config") or {}).get(effective_role) or {}
    controller = cfg.get("controller")
    provider_session_id = (
        state_store.get_role_session(state, effective_role, controller)
        if state else None)

    if not work_id or current_binding is None:
        return _preflight_conflict(
            "candidate_mismatch", RESUME_TRIGGER_EXIT_BINDING_MISMATCH)

    mismatch = _resume_wake_failure_kind(
        precheck_lease, current_binding, provider_session_id)
    if mismatch:
        return _preflight_conflict(mismatch, RESUME_TRIGGER_EXIT_BINDING_MISMATCH)

    pending_record = state_store.read_pending_turn_before_pause(
        session_uuid, effective_role)
    if (not pending_record or not pending_record.get("acknowledged")
           or pending_record.get("lease_id") != precheck_lease["lease_id"]):
        return _preflight_conflict(
            "no_pending_turn", RESUME_TRIGGER_EXIT_NO_PENDING_TURN)
    # An orchestrator answer this turn carries by path is re-sent only while
    # it still has the bytes the session store recorded (never edited content
    # under orchestrator authority).
    tampered_rid = _capacity_turn_tampered_answer(
        session_uuid, state, pending_record)
    if tampered_rid:
        expected = (state_store.trusted_decision_response(
            state, tampered_rid) or {}).get("answer_sha256")
        details = {"request_id": tampered_rid, "expected_sha256": expected}
        if expected:
            details["answer_path"] = state_store.decision_answer_path_for(
                session_uuid, tampered_rid)
            details["recovery"] = ("restore the exact bytes (sha256 %s) at "
                                   "answer_path, then trigger again"
                                   % expected)
        else:
            details["recovery"] = ("the session store has no response record "
                                   "for this request; the turn cannot be "
                                   "resent under orchestrator authority")
        return _preflight_conflict(
            "decision_answer_tampered", RESUME_TRIGGER_EXIT_BINDING_MISMATCH,
            **details)
    # A turn that carries orchestrator decision blocks is only ever resent
    # verbatim: a redirected context would replace those bytes, and the
    # decisions would be acknowledged without ever being sent. Refused before
    # any claim, phase advance, session construction or send, and (like the
    # exclusivity refusal below) without charging a failed wake attempt --
    # nothing was attempted. The lease stays live and still holds the
    # decisions; a verbatim trigger delivers them.
    bound_rids = _redirect_refused_decisions(args, pending_record)
    if bound_rids:
        write(json.dumps({
            "outcome": "invalid_arguments",
            "reason": "decision_bound_turn_not_redirectable",
            "lease_id": precheck_lease["lease_id"],
            "request_ids": bound_rids,
            "detail": "the paused turn carries orchestrator decision(s) %s; "
                      "resume it without --redirected-context"
                      % ", ".join(bound_rids)}) + "\n")
        return RESUME_TRIGGER_EXIT_INVALID_ARGUMENTS

    # Issue #64 P3, ENFORCEMENT POINT 2: provider/controller exclusivity, one
    # step BEFORE ownership is even acquired. Placed here, a refusal has
    # claimed nothing, advanced no phase, constructed no controller session
    # and sent nothing -- everything below this point either mutates state or
    # costs money.
    #
    # Deliberately NOT routed through `_preflight_conflict`: that helper
    # accounts a FAILED WAKE ATTEMPT, and an exclusivity refusal must not
    # consume one. Nothing has been attempted -- the wake was refused, not
    # tried -- and charging it would silently widen behaviour and erode the
    # per-binding automatic-recovery chain. It also sits AFTER every
    # pre-existing preflight refusal, so their precedence is unchanged.
    #
    # A proven collision and an unconsultable index are separate outcomes with
    # separate exit codes, because they oblige an operator to do different
    # things: 11 means another live session holds this conversation, 12 means
    # we could not find out.
    if controller and provider_session_id:
        try:
            rt_binding = cowork_owner.read_provider_binding(
                controller, provider_session_id)
        except cowork_owner.ProviderBindingUnavailable as exc:
            write(json.dumps({
                "outcome": "provider_binding_unavailable",
                "session_uuid": session_uuid,
                "lease_id": precheck_lease["lease_id"],
                "controller": controller,
                "provider_session_id": provider_session_id,
                "detail": str(exc)}) + "\n")
            return RESUME_TRIGGER_EXIT_PROVIDER_BINDING_UNAVAILABLE
        rt_bound_to = (rt_binding or {}).get("owner_session_uuid")
        if (rt_bound_to and rt_bound_to != session_uuid
                and cowork_owner.classify_owner_lease(rt_bound_to)
                == "live_owner"):
            write(json.dumps({
                "outcome": "provider_session_bound",
                "session_uuid": session_uuid,
                "lease_id": precheck_lease["lease_id"],
                "controller": controller,
                "provider_session_id": provider_session_id,
                "owner_session_uuid": rt_bound_to}) + "\n")
            return RESUME_TRIGGER_EXIT_PROVIDER_SESSION_BOUND

    # Issue #64: SINGLE-WRITER OWNERSHIP for the resume-trigger entry point too.
    # Acquired BEFORE step 2's state-mutating claim -- and therefore before
    # any phase advance, any provider-session construction and any send -- so
    # a duplicate resume-trigger against a session another process already
    # owns is refused having claimed nothing and sent nothing.
    rt_release_reason = "normal_exit"
    try:
        rt_lease = cowork_owner.acquire_owner_lease(
            session_uuid,
            cowork_owner.owner_identity(session_uuid, "resume_trigger", cwd,
                                        None))
    except cowork_owner.OwnerLeaseError as exc:
        write(json.dumps({
            "outcome": "owner_conflict",
            "session_uuid": session_uuid,
            "reason": cowork_owner.owner_refusal_reason(exc),
            "detail": str(exc)}) + "\n")
        return RESUME_TRIGGER_EXIT_OWNER_CONFLICT
    rt_owner_id = rt_lease["owner_id"]
    rt_owner_epoch = rt_lease["epoch"]
    rt_prior_owner_context = _set_owner_context(
        session_uuid, rt_owner_id, rt_owner_epoch)
    try:
        # Step 2: every read-only preflight check above passed -- only NOW
        # attempt this CLI's OWN state-mutating claim (idempotent under
        # Package F's own claim-then-invoke ordering -- see the docstring).
        manual_signal_record = None
        try:
            if args.manual_signal_record:
                manual_signal_record = _load_json_file(args.manual_signal_record)
                pinned_public_keys = _load_json_file(args.pinned_public_keys)
                result = capacity_scheduler.claim_with_authorized_early_override(
                    session_uuid, args.lease_id, args.claimant_ref, effective_now,
                    manual_signal_record, pinned_public_keys, args.automation_ref)
            else:
                result = capacity_scheduler.claim(
                    session_uuid, args.lease_id, args.claimant_ref, effective_now,
                    args.automation_ref, reference_now=args.reference_now,
                    max_clock_skew_seconds=args.max_clock_skew_seconds)
        except capacity_scheduler.SchedulerLeaseConflict as exc:
            write(json.dumps({"outcome": "conflict", "lease_id": exc.lease_id,
                              "reason": exc.reason}) + "\n")
            return (RESUME_TRIGGER_EXIT_NOT_DUE if exc.reason == "early_refusal"
                   else RESUME_TRIGGER_EXIT_CONFLICT)
        except capacity_scheduler.SchedulerOverrideRecordingFailed as exc:
            write(json.dumps({"outcome": "internal_error",
                              "detail": str(exc)}) + "\n")
            return RESUME_TRIGGER_EXIT_INTERNAL_ERROR
        except ValueError as exc:
            write(json.dumps({"outcome": "invalid_arguments",
                              "detail": str(exc)}) + "\n")
            return RESUME_TRIGGER_EXIT_INVALID_ARGUMENTS
        except OSError as exc:
            # D-MJ-02 parity: a genuine operational claim failure durably
            # accounts one failed wake attempt too.
            _account_failed_wake_attempt(
                session_uuid, precheck_lease, args.automation_ref)
            write(json.dumps({"outcome": "internal_error",
                              "detail": str(exc)}) + "\n")
            return RESUME_TRIGGER_EXIT_INTERNAL_ERROR

        try:
            canonical_lease = capacity_contracts.validate_pause_lease(
                state_store.pause_lease_from_stored_record(result["lease"]))
        except ValueError as exc:
            write(json.dumps({"outcome": "internal_error",
                              "detail": str(exc)}) + "\n")
            return RESUME_TRIGGER_EXIT_INTERNAL_ERROR

        def _release_and_conflict(reason, code):
            # Every RETRIABLE post-claim refusal below releases the
            # just-claimed lease truthfully via a same-binding
            # replace-and-increment (never stranded, never silently reset) --
            # this is what makes the ceiling genuinely reachable under
            # Package F's real claim-then-invoke ordering too.
            _account_failed_wake_attempt(
                session_uuid, canonical_lease, args.automation_ref)
            write(json.dumps({"outcome": "conflict",
                              "lease_id": canonical_lease["lease_id"],
                              "reason": reason}) + "\n")
            return code

        def _release_and_conflict_terminal(reason, code):
            # A genuinely TERMINAL post-claim refusal (the candidate itself is
            # permanently done, e.g. invalidated): cancels rather than
            # replaces -- retrying is never meaningful again for this exact
            # candidate, so no ceiling accounting applies.
            try:
                capacity_scheduler.cancel(
                    session_uuid, canonical_lease["lease_id"], args.automation_ref)
            except capacity_scheduler.SchedulerLeaseConflict:
                pass
            write(json.dumps({"outcome": "conflict",
                              "lease_id": canonical_lease["lease_id"],
                              "reason": reason}) + "\n")
            return code

        # Step 3: race-safety re-check -- the pre-claim snapshot could in
        # principle be stale by the time the claim itself landed.
        if (effective_role != canonical_lease["role"]
                or _resume_wake_failure_kind(
                    canonical_lease, current_binding, provider_session_id)):
            return _release_and_conflict(
                "candidate_mismatch", RESUME_TRIGGER_EXIT_BINDING_MISMATCH)

        expected_candidate = {
            "candidate_manifest_digest": current_binding["candidate_manifest_digest"],
            "candidate_index": current_binding["candidate_index"],
        }
        if manual_signal_record is not None:
            wake_kind_evidence = {
                "kind": "manual_signal",
                "signal_journal_ref": manual_signal_record["signal_journal_ref"],
                "signer_public_key_id": manual_signal_record["signer_public_key_id"],
                "detached_signature": manual_signal_record["detached_signature"],
            }
        else:
            wake_kind_evidence = {
                "kind": "trustworthy_reset",
                "consumption_state": "consumed",
                "not_before": canonical_lease["not_before"],
                "current_clock": effective_now,
            }
        capacity_wake_evidence = {
            "lease_id": canonical_lease["lease_id"],
            "role": canonical_lease["role"],
            "provider_session_id": canonical_lease["provider_session_id"],
            "controller_policy_digest": canonical_lease["controller_policy_digest"],
            "candidate_manifest_digest": canonical_lease["candidate_digest"],
            "candidate_index": current_binding["candidate_index"],
        }
        capacity_wake_evidence.update(wake_kind_evidence)
        claimed_record = _advance_phase(
            session_uuid, work_id, "capacity_wake_claimed",
            evidence={"capacity_wake_evidence": capacity_wake_evidence},
            source="resume_trigger", expected_candidate=expected_candidate)
        if (claimed_record or {}).get("state") != "preflighting":
            # Package A's own candidate-identity gate refused (the lease names
            # a different candidate than this engagement's current one) --
            # never silently resumed a stale binding.
            return _release_and_conflict(
                "candidate_mismatch", RESUME_TRIGGER_EXIT_BINDING_MISMATCH)

        def _refuse_wake_preflight(failure_kind):
            evidence = {"capacity_wake_preflight_failure": {
                "lease_id": canonical_lease["lease_id"],
                "role": canonical_lease["role"],
                "provider_session_id": canonical_lease["provider_session_id"],
                "controller_policy_digest": canonical_lease["controller_policy_digest"],
                "candidate_manifest_digest": canonical_lease["candidate_digest"],
                "candidate_index": current_binding["candidate_index"],
                "failure_kind": failure_kind}}
            _advance_phase(
                session_uuid, work_id, "capacity_wake_preflight_failed",
                evidence=evidence, source="resume_trigger",
                expected_candidate=expected_candidate)
            return _release_and_conflict(
                failure_kind, RESUME_TRIGGER_EXIT_BINDING_MISMATCH)

        mismatch = _resume_wake_failure_kind(
            canonical_lease, current_binding, provider_session_id)
        if mismatch:
            return _refuse_wake_preflight(mismatch)

        for invalidation in state_store.read_invalidation_history(session_uuid):
            if (invalidation.get("invalidated_candidate_digest")
                    == canonical_lease["candidate_digest"]
                    and invalidation.get("invalidated_session_id") == session_uuid):
                _advance_phase(
                    session_uuid, work_id, "preflight_rejected",
                    evidence={"reason": "invalidation_record_on_file",
                             "candidate_manifest_digest":
                                 canonical_lease["candidate_digest"]},
                    source="resume_trigger")
                return _release_and_conflict_terminal(
                    "invalidated", RESUME_TRIGGER_EXIT_INVALIDATED)

        # The pending-turn precondition was already proven true on the
        # pre-claim snapshot (step 1); re-read fresh (never trust a value this
        # stale) since it is what actually seeds the send below.
        pending_record = state_store.read_pending_turn_before_pause(
            session_uuid, effective_role)
        if (not pending_record or not pending_record.get("acknowledged")
               or pending_record.get("lease_id") != canonical_lease["lease_id"]):
            return _release_and_conflict(
                "no_pending_turn", RESUME_TRIGGER_EXIT_NO_PENDING_TURN)
        if _redirect_refused_decisions(args, pending_record):
            # The fresh re-read gained decision bindings after the pre-claim
            # check: refused the same way, the claim released.
            return _release_and_conflict(
                "decision_bound_turn_not_redirectable",
                RESUME_TRIGGER_EXIT_INVALID_ARGUMENTS)

        _advance_phase(session_uuid, work_id, "preflight_passed",
                       source="resume_trigger")

        seed_text = pending_record.get("turn_text")
        try:
            delivery = _resume_seed_delivery(seed_text, args.redirected_context)
        except TypeError as exc:
            write(json.dumps({"outcome": "internal_error",
                              "detail": str(exc)}) + "\n")
            return RESUME_TRIGGER_EXIT_INTERNAL_ERROR

        sessions_dir = state_store.session_assets_dir(session_uuid)
        if session_factory is not None:
            session = session_factory(controller, effective_role, provider_session_id,
                                      cfg.get("model"), cfg.get("effort"))
        else:
            session = _construct_resume_session(
                effective_role, controller, cfg, provider_session_id, sessions_dir,
                trace=None)
        if session is None:
            write(json.dumps({"outcome": "internal_error",
                              "detail": "unrecognized controller %r" % controller}
                            ) + "\n")
            return RESUME_TRIGGER_EXIT_INTERNAL_ERROR

        # The controller this trigger constructed is closed on every path
        # below -- each return and any exception -- while the owner lease
        # (released in the outer finally) is still held.
        try:
            send_result = _send(session, delivery,
                                meta={"prompt_kind": "resume_wake"})
            if send_result.get("ok", True):
                # The provider accepted the exact turn bytes (never a
                # redirected substitute: refused above): the orchestrator
                # decision deliveries bound to them reached their target, so
                # they are acknowledged now -- before anything else can fail
                # -- and a later plain run never rebuilds them. A failed
                # acknowledgment is reported in the result line and re-sent
                # later (at least once).
                ack_failed = []
                _acknowledge_capacity_turn_decisions(
                    session_uuid, pending_record, failed=ack_failed)
                # Step 8, accepted send: mark the durable lease consumed
                # FIRST -- only THEN is `consumption_state=consumed` asserted
                # anywhere (the pending turn is cleared after) -- an accepted
                # send consumes exactly once.
                try:
                    capacity_scheduler.mark_consumed(
                        session_uuid, canonical_lease["lease_id"],
                        args.automation_ref)
                except capacity_scheduler.SchedulerLeaseConflict as exc:
                    write(json.dumps({"outcome": "internal_error",
                                      "detail": "post-send consume conflict: %s"
                                      % exc.reason}) + "\n")
                    return RESUME_TRIGGER_EXIT_INTERNAL_ERROR
                state_store.clear_pending_turn_before_pause(
                    session_uuid, effective_role)
                success = {"outcome": "success",
                           "lease_id": canonical_lease["lease_id"]}
                if ack_failed:
                    success["decision_ack_failed"] = [
                        b.get("request_id") for b in ack_failed]
                write(json.dumps(success) + "\n")
                return RESUME_TRIGGER_EXIT_SUCCESS

            # Post-wake send failure: the lease is NEVER marked consumed for a
            # failed send (it is still `claimed`), and the pending turn is
            # RETAINED (never cleared).
            raw_evidence = _synthesize_raw_failure_evidence(
                controller, send_result)
            controller_outcome = _classify_raw_failure(controller, raw_evidence)
            _record_provider_health(
                session_uuid, effective_role, controller, controller_outcome,
                _capacity_now())
            if controller_outcome in capacity_contracts.CAPACITY_ELIGIBLE_OUTCOMES:
                # Re-entry via SAME-BINDING REPLACEMENT of this exact claimed
                # lease -- never a fresh `start_new_episode`, which would
                # silently reset the per-binding automatic-recovery chain
                # back to 0.
                capacity_payload = _enter_awaiting_capacity(
                    session_uuid, work_id, effective_role, controller,
                    provider_session_id, controller_outcome, seed_text,
                    cfg.get("model"), cfg.get("effort"),
                    raw_evidence=raw_evidence,
                    replace_lease_id=canonical_lease["lease_id"],
                    replace_automation_ref=args.automation_ref,
                    decision_bindings=pending_record.get("decision_bindings"))
                if capacity_payload is not None:
                    write(json.dumps({
                        "outcome": "send_failed",
                        "lease_id": canonical_lease["lease_id"],
                        "controller_outcome": controller_outcome,
                        "re_entered_capacity": True}) + "\n")
                    return RESUME_TRIGGER_EXIT_SEND_FAILED
            # Not capacity-eligible, or the replacement attempt itself failed:
            # the claimed lease is released (never stranded) and this
            # candidate's execution is terminally marked failed -- no further
            # wake-ceiling accounting is meaningful for it, so a plain cancel
            # (not a replace).
            try:
                capacity_scheduler.cancel(
                    session_uuid, canonical_lease["lease_id"],
                    args.automation_ref)
            except capacity_scheduler.SchedulerLeaseConflict:
                pass
            _advance_phase(
                session_uuid, work_id, "execution_failed",
                evidence={"reason": "send_failed",
                         "controller_outcome": controller_outcome},
                source="resume_trigger")
            write(json.dumps({
                "outcome": "send_failed", "lease_id": canonical_lease["lease_id"],
                "controller_outcome": controller_outcome,
                "re_entered_capacity": False}) + "\n")
            return RESUME_TRIGGER_EXIT_SEND_FAILED
        finally:
            _close_resume_session(session)
    except cowork_owner.OwnerLeaseError as exc:
        # The same typed refusal, observed later: a lease lost mid-run (a
        # takeover while this trigger was working) reaches a governed seam
        # and raises here. Caught for the same reason `run_flow` catches it
        # -- this entry point's published contract is "exactly one JSON
        # result line, and one of RESUME_TRIGGER_EXIT_CODES", and a traceback
        # would break both. The DECLARED BASE, so a later subclass is covered
        # by construction.
        rt_release_reason = "owner_refusal"
        write(json.dumps({
            "outcome": "owner_conflict",
            "session_uuid": session_uuid,
            "reason": cowork_owner.owner_refusal_reason(exc),
            "detail": str(exc)}) + "\n")
        return RESUME_TRIGGER_EXIT_OWNER_CONFLICT
    finally:
        # Exactly once, on every exit path this function has -- each of its
        # returns, and any exception unwinding out of it.
        cowork_owner.release_owner_lease(
            session_uuid, rt_owner_id, rt_owner_epoch, rt_release_reason)
        _restore_owner_context(rt_prior_owner_context)


def _close_resume_session(session):
    """Close the controller session a resume trigger constructed. A close
    error never changes the trigger's one result line or its exit code."""
    try:
        session.close()
    except Exception:  # noqa: BLE001 - cleanup is secondary evidence
        pass


def _terminate_run(signum, frame):
    """Whole-run SIGTERM handler: unwind (releasing the lease in run_flow's
    finally) and exit 128+SIGTERM with a structured result."""
    raise SystemExit(128 + int(signal.SIGTERM))


class _ResumeTriggerTerminated(SystemExit):
    """SIGTERM delivered to a `resume-trigger` process."""


def _terminate_resume_trigger(signum, frame):
    """Resume-trigger SIGTERM handler: unwind once through the session close
    and the owner-lease release. A repeated SIGTERM is ignored for the rest
    of the unwind so it cannot cut the cleanup short."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise _ResumeTriggerTerminated(128 + int(signal.SIGTERM))


def _run_resume_trigger_governed(argv, output=None, session_factory=None):
    """The `resume-trigger` CLI entry: `run_resume_trigger` with SIGTERM
    governed, so a terminated trigger still closes its controller, releases
    its owner lease, and keeps the contract of exactly one JSON result line
    and one of RESUME_TRIGGER_EXIT_CODES. The line is `internal_error` unless
    one was already written. Off the main thread, where no handler can be
    installed, it runs unguarded."""
    write = output if output is not None else sys.stdout.write
    written = [0]

    def counted(text):
        written[0] += 1
        return write(text)

    try:
        prior = signal.signal(signal.SIGTERM, _terminate_resume_trigger)
    except (ValueError, RuntimeError):
        return run_resume_trigger(argv, output=output,
                                  session_factory=session_factory)
    try:
        return run_resume_trigger(argv, output=counted,
                                  session_factory=session_factory)
    except _ResumeTriggerTerminated:
        if not written[0]:
            write(json.dumps({"outcome": "internal_error",
                              "detail": "terminated by SIGTERM"}) + "\n")
        return RESUME_TRIGGER_EXIT_INTERNAL_ERROR
    finally:
        try:
            signal.signal(signal.SIGTERM,
                          prior if prior is not None else signal.SIG_DFL)
        except (ValueError, RuntimeError, TypeError):
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["resume-trigger"]:
        # M3 Package E: the resume-trigger CLI is a wholly separate,
        # independently-versioned contract (RESUME_TRIGGER_CONTRACT_VERSION)
        # -- dispatched here, before the main flat argparse, exactly like D's
        # own standalone `cowork_capacity_scheduler.run_wake_trigger` never
        # shares a parser with anything else.
        return _run_resume_trigger_governed(argv[1:])
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exc:
            # argparse already wrote its usage error to stderr. A refused
            # invocation still ends with its one structured result (--help,
            # exit 0, is not a run and emits none).
            if exc.code not in (0, None):
                emit_run_result(sys.stdout, 2, {"reason": "argument_error"})
            raise
        # --check / --report / --session-owner are read-only and short-circuit
        # BELOW, before run_flow — which is what keeps them working on a
        # session whose saved policy is unreadable. They stay mutually
        # exclusive with every session-mutating flag (controller updates,
        # orchestrator decisions, --take-over); the refusal ends with its one
        # structured result like any other invalid invocation.
        mutating = [("--switch-controller", bool(args.switch_controller)),
                    ("--allow-controllers",
                     args.allow_controllers is not None),
                    ("--take-over", bool(getattr(args, "take_over", False)))]
        mutating += [("--" + kind.replace("_", "-"), True)
                     for kind, _rid in _decision_flags(args)]
        for flag, supplied in mutating:
            for read_only, active in (("--check", args.check),
                                      ("--report", args.report),
                                      ("--session-owner",
                                       args.session_owner)):
                if supplied and active:
                    sys.stderr.write("cowork: %s cannot be combined with "
                                     "%s.\n" % (flag, read_only))
                    emit_run_result(sys.stdout, 2,
                                    {"reason": "conflicting_arguments"})
                    return 2
        if args.check:
            return preflight.main()
        if args.report:
            return run_report(args)
        # Issue #64 P4 surface 1: read-only owner status, dispatched here
        # beside --check/--report and deliberately ABOVE the nested guard
        # below — a diagnostic query never governs a broker.
        if args.session_owner:
            return run_session_owner(args)
        # Targeted orchestrator-owned evaluation: a read-mostly side channel
        # (it writes only orchestrator-evaluations.json, never session state or
        # a phase gate), dispatched here like --check/--report, before run_flow.
        if getattr(args, "evaluate_role", None):
            return run_orchestrator_eval(args)
        # Runtime controller dispatches are governed from this point onward.
        # Read-only --check/--report paths above never need a broker.
        prior_guard = bridge.set_nested_guard_active(True)
        result_box = {}
        rc = None
        # Whole-run SIGTERM: outside run_flow's own phase handler (lease,
        # preflight, worktree setup, the final measurement checkpoint) a
        # termination still unwinds through the finally below and ends with
        # its structured result (rc 143). SIGKILL cannot be observed; a caller
        # must treat a missing result line as a failure. Known narrow window:
        # the handler is restored in the `finally` below before the result is
        # emitted, so a SECOND SIGTERM arriving during that unwind can end the
        # process without the line -- the same missing-line rule applies.
        try:
            prior_sigterm = signal.signal(signal.SIGTERM, _terminate_run)
        except (ValueError, RuntimeError):
            prior_sigterm = None
        try:
            # The provider transcript goes to stderr; stdout carries only the
            # one trusted run-result line, so no model output can stand in
            # for it.
            rc = run_flow(args, io_out=sys.stderr, result_box=result_box)
            return rc
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
            raise
        except Exception as exc:  # noqa: BLE001 - one truthful terminal result
            if isinstance(exc, cowork_owner.OwnerLeaseError):
                raise
            import traceback
            traceback.print_exc(file=sys.stderr)
            rc = 1
            result_box["reason"] = "internal_error"
            result_box["error_type"] = type(exc).__name__
            return rc
        finally:
            if prior_sigterm is not None:
                try:
                    signal.signal(signal.SIGTERM, prior_sigterm)
                except (ValueError, RuntimeError):
                    pass
            bridge.set_nested_guard_active(prior_guard)
            # The result line must agree with the exit code the backstops
            # below produce for an exception escaping run_flow.
            escaping = sys.exc_info()[1]
            if isinstance(escaping, cowork_owner.OwnerLeaseError):
                rc = 3
            elif isinstance(escaping, KeyboardInterrupt):
                rc = 130
            elif rc is None:
                rc = 1
            emit_run_result(sys.stdout, rc, result_box)
    except cowork_owner.OwnerLeaseError:
        # Issue #64 catch point 2: a STRUCTURAL backstop, not a second
        # reporting surface. `run_flow`'s own handler already traced the
        # refusal, wrote the operator message and returned 3; this exists so
        # that "an owner refusal can never surface as a traceback" is a
        # property of the process, not an argument about reachability. It
        # names the DECLARED BASE, so a subclass added later cannot fall out
        # of it.
        return 3
    except KeyboardInterrupt:
        # SIGINT: exit without a traceback. 130 = 128 + SIGINT.
        sys.stderr.write("\ncowork: interrupted.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
