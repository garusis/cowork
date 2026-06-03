#!/usr/bin/env python3
"""cowork: multi-role CLI orchestration entry flow + the scout (context
gatherer) role.

This is the foundation only: the 3-step entry flow (team checklist, per-role
tool config, initial context), the preflight dependency check, and running the
first role (`scout`) by spawning the selected CLI and bridging it to the user.
Later roles (revisor/planner/advisor/builder) are out of scope here.

Selection uses `gum` for real interactive checkbox/choice menus. A
non-interactive args path (--team/--config/--context) skips gum entirely so the
flow is testable and scriptable.

Additive to the co-plan skill: new file, stdlib only, Python 3.9+, does not
import or modify co_plan_file.py.
"""

import argparse
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_bridge as bridge  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOUT_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "scout.md")

# Role order matches the user's vision: context-gather, plan-revisor, planner,
# advisor, implementer -> final names below. Only `scout` is implemented now.
ROLES = ["scout", "revisor", "planner", "advisor", "builder"]

# Per-role defaults (controller, yolo, mode), all roles checked by default.
# Roles default to implement mode (write-enabled) and are kept in their lane by
# role-spec guardrails, not by plan mode.
DEFAULTS = {
    "scout": {"controller": "claude", "yolo": True, "mode": "implement"},
    "revisor": {"controller": "codex", "yolo": True, "mode": "implement"},
    "planner": {"controller": "claude", "yolo": True, "mode": "implement"},
    "advisor": {"controller": "codex", "yolo": True, "mode": "implement"},
    "builder": {"controller": "claude", "yolo": True, "mode": "implement"},
}


# --------------------------------------------------------------------------- #
# gum seam: a single injectable runner makes the menus unit-testable without a #
# TTY or a real gum install.                                                   #
# --------------------------------------------------------------------------- #


def _run_gum(argv, input_text=None):
    """Run a gum command. gum draws its UI on the controlling TTY and prints the
    result to stdout. Returns (returncode, stdout)."""
    proc = subprocess.run(
        argv, input=input_text, stdout=subprocess.PIPE, stderr=None, text=True
    )
    return proc.returncode, proc.stdout


def gum_choose(options, selected=None, header=None, multi=False, run=_run_gum):
    """gum choose. Returns (returncode, [picked items])."""
    argv = ["gum", "choose"]
    if multi:
        argv.append("--no-limit")
    if selected:
        argv.append("--selected=" + ",".join(selected))
    if header:
        argv.append("--header=" + header)
    argv += list(options)
    rc, out = run(argv)
    picks = [line for line in out.splitlines() if line.strip()]
    return rc, picks


def gum_choose_one(options, default=None, header=None, run=_run_gum):
    """Single-choice gum choose. Returns the picked item (or default on cancel)."""
    rc, picks = gum_choose(
        options, selected=[default] if default else None, header=header,
        multi=False, run=run,
    )
    if rc != 0 or not picks:
        return default
    return picks[0]


def gum_confirm(prompt, run=_run_gum):
    """gum confirm. Returns True for the affirmative choice (exit code 0)."""
    rc, _ = run(["gum", "confirm", prompt])
    return rc == 0


def gum_write(header=None, run=_run_gum):
    """gum write (multiline input). Returns the entered text."""
    argv = ["gum", "write"]
    if header:
        argv.append("--header=" + header)
    _, out = run(argv)
    return out.rstrip("\n")


# --------------------------------------------------------------------------- #
# Step 1: team checklist (interactive).                                       #
# --------------------------------------------------------------------------- #


def select_team_interactive(run=_run_gum):
    """Real checkbox menu, all roles preselected. Returns ordered roles."""
    rc, picks = gum_choose(
        ROLES, selected=ROLES, header="Choose your team (space toggles)",
        multi=True, run=run,
    )
    if rc != 0:  # cancelled
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


def configure_roles_interactive(selected, run=_run_gum):
    """Step 2 via gum. The defaults are shown as the menu header (so gum clears
    them when you choose) with a fast path to accept them. Otherwise pick roles
    to customize and choose controller/yolo/mode for each."""
    config = default_config(selected)
    summary = format_config_summary(config, header="Default tool config:")
    choice = gum_choose_one(
        ["use these defaults", "customize"], default="use these defaults",
        header=summary, run=run,
    )
    if choice != "customize":
        return config
    _, to_customize = gum_choose(
        selected, header="Customize which roles?", multi=True, run=run
    )
    for role in selected:
        if role not in to_customize:
            continue
        cfg = config[role]
        cfg["controller"] = gum_choose_one(
            ["claude", "codex"], default=cfg["controller"],
            header=role + " controller", run=run,
        )
        yolo = gum_choose_one(
            ["yolo", "no-yolo"], default="yolo" if cfg["yolo"] else "no-yolo",
            header=role + " permissions", run=run,
        )
        cfg["yolo"] = (yolo == "yolo")
        cfg["mode"] = gum_choose_one(
            ["plan", "implement"], default=cfg["mode"],
            header=role + " mode", run=run,
        )
    return config


# --------------------------------------------------------------------------- #
# Step 3: initial context.                                                    #
# --------------------------------------------------------------------------- #


def gather_context_interactive(run=_run_gum):
    return gum_write(header="Give me the context (files/code needed)", run=run)


def resolve_context(args, run=_run_gum):
    """Context from --context, --context-file (or '-' for stdin), or gum."""
    if args.context is not None:
        return args.context
    if args.context_file is not None:
        if args.context_file == "-":
            return sys.stdin.read()
        with open(args.context_file, "r") as fh:
            return fh.read()
    if _is_non_interactive(args):
        return ""
    return gather_context_interactive(run=run)


# --------------------------------------------------------------------------- #
# Argument parsing / non-interactive path.                                    #
# --------------------------------------------------------------------------- #


def build_parser():
    p = argparse.ArgumentParser(prog="cowork", add_help=True)
    p.add_argument("--check", action="store_true",
                   help="run the preflight dependency check only")
    p.add_argument("--team",
                   help="comma-separated roles, e.g. scout,advisor "
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


def scout_start_banner(intel_path):
    return (
        "\n┌ scout ─ gathering context\n"
        "│ I'll investigate, ask what I need, and propose options. I finish\n"
        "│ on my own once we agree. You drive — answer my questions. Ctrl-C aborts.\n"
        "└ intel → %s\n" % intel_path
    )


def scout_needs_input_cue():
    return "\n── scout needs your input ──────────────\n"


def scout_review_banner(intel_path):
    return (
        "\n✓ scout intel ready for review — %s\n"
        "Enter to approve & finish, or type feedback to revise" % intel_path
    )


def scout_done_banner(intel_path):
    return "\n✓ scout finished — intel → %s\n" % intel_path


def _scout_loop(session, first, intel_path, context, io_in, io_out):
    """Drive the per-turn loop: send → reply → read intel status → prompt/finish.

    Ends when the user approves at `ready_for_review` (blank line) or aborts.
    """
    pending = first
    try:
        if context.strip():
            io_out.write(bridge.USER_LABEL + context.strip() + "\n")
            io_out.flush()
        while True:
            session.send(pending)
            status = state_store.read_status(intel_path)
            if status == "ready_for_review":
                io_out.write(scout_review_banner(intel_path) + "\n")
                io_out.write(bridge.USER_LABEL)
                io_out.flush()
                line = io_in.readline()
                if line == "" or line.strip() == "":
                    io_out.write(scout_done_banner(intel_path))
                    io_out.flush()
                    break
            else:
                if status == "needs_input":
                    io_out.write(scout_needs_input_cue())
                io_out.write("\n" + bridge.USER_LABEL)
                io_out.flush()
                line = io_in.readline()
                if line == "" or line.strip() == "":
                    break  # EOF / blank = abort while still working
            pending = line.rstrip("\n")
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    return 0


def run_scout(config, context, selected, io_in=None, io_out=None,
              claude_spawn=None, resume_id=None, on_session=None,
              intel_path=None, session_factory=None):
    """Spin up the scout's CLI and drive the review loop.

    `resume_id` continues a saved CLI session; `on_session(controller, id)` is
    called so the session id can be persisted for a future resume.
    `intel_path` is the scout's only write target (`.cowork/scout.intel.*.json`).
    `session_factory(controller, **kw)` overrides session creation (for tests).
    """
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    cfg = config["scout"]
    brief = assemble_scout_brief(selected, intel_path or "")
    if resume_id and not context.strip():
        context = "Continue the session."
    io_out.write(scout_start_banner(intel_path or ""))
    io_out.flush()

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = bridge.probe_claude_stream_json(
            spawn, mode=cfg["mode"], yolo=cfg["yolo"],
            role_prompt_file=SCOUT_PROMPT_PATH,
        )
        if not ok:
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
                on_session_id=cb)
        first = (brief + "\n\n" + context).strip()
        return _scout_loop(session, first, intel_path, context, io_in, io_out)

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
            resume_thread_id=resume_id, on_thread_id=cb)
    return _scout_loop(session, prompt, intel_path, context, io_in, io_out)


# --------------------------------------------------------------------------- #
# Entry point.                                                                #
# --------------------------------------------------------------------------- #


def run_flow(args, io_in=None, io_out=None, gum_run=_run_gum, which=None,
             run_scout_fn=None):
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    run_scout_fn = run_scout_fn or run_scout
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
    reuse_config = (session_enabled and state_store.has_config(saved)
                    and not args.team and not args.config)

    # Step 1: team.
    if args.team:
        selected, err = parse_team(args.team)
        if err:
            io_out.write("cowork: " + err + "\n")
            return 2
    elif reuse_config:
        selected = [r for r in ROLES if r in saved["team"]]
    elif interactive:
        selected = select_team_interactive(run=gum_run)
    else:
        selected = list(ROLES)
    if not selected:
        io_out.write("cowork: no roles selected; nothing to do.\n")
        return 0

    # Step 2: config.
    config = default_config(selected)
    if args.config:
        ok, err = apply_config_args(config, args.config)
        if not ok:
            io_out.write("cowork: " + err + "\n")
            return 2
    elif reuse_config:
        config = {r: dict(saved["config"][r]) for r in selected
                  if r in saved["config"]}
        io_out.write("cowork: using saved session config (%s)\n" % spath)
    elif interactive:
        config = configure_roles_interactive(selected, run=gum_run)

    # Persist team + config the first time (or whenever freshly chosen).
    if session_enabled and not reuse_config:
        saved = state_store.save_config(spath, selected, config, prior=saved or {})

    # Preflight (gum required only for the interactive menus).
    kwargs = {"interactive": interactive}
    if which is not None:
        kwargs["which"] = which
    ok, alerts = preflight.preflight(config, **kwargs)
    if not ok:
        io_out.write("cowork preflight failed:\n")
        for alert in alerts:
            io_out.write("  - " + alert + "\n")
        io_out.flush()
        return 1

    # Step 3: context.
    context = resolve_context(args, run=gum_run)

    if "scout" not in selected:
        io_out.write(
            "cowork: scout not selected. Only the scout role is implemented in "
            "this version; later roles are not yet available.\n"
        )
        return 0

    # Resume the scout's saved CLI session if one matches the current controller.
    resume_id = None
    on_session = None
    if session_enabled:
        resume_id = state_store.get_role_session(
            saved, "scout", config["scout"]["controller"])
        holder = {"state": saved}

        def on_session(controller, sid):
            holder["state"] = state_store.save_role_session(
                spath, "scout", controller, sid, prior=holder["state"])

    intel_dir = os.path.dirname(spath) if session_enabled else state_store.session_dir()
    intel_path = scout_intel_path(intel_dir, session_uuid)
    return run_scout_fn(config, context, selected, io_in=io_in, io_out=io_out,
                        resume_id=resume_id, on_session=on_session,
                        intel_path=intel_path)


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
