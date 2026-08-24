#!/usr/bin/env python3
"""M3 Package F -- macOS launchd wake adapter.

This module makes NO recovery decision of its own. Every eligibility and
idempotency question -- is this lease due, is it already claimed, has it
exhausted its retry ceiling -- is answered exclusively by Package D's own
versioned wake-trigger contract (`cowork_capacity_scheduler.run_wake_trigger`
/ `WAKE_TRIGGER_EXIT_CODES`, consulted here by NAME, never a bare literal
integer). This module owns no storage and no lock: it never imports
`cowork_state` (Package B) directly and never re-derives a claim/consumption-
state decision Package D already makes.

Two responsibilities only:

  1. Launchd REGISTRATION: build/write a `launchd` property list whose
     `Label` is set to the caller's `automation_ref` VERBATIM -- the exact
     same string later presented to Package D's `--automation-ref` on fire,
     never a derived or prefixed variant -- and install/uninstall it via an
     injectable `launchctl_runner` (defaults to a real `launchctl`
     subprocess in production; every test injects a fake one, so no test in
     this package's own suite ever performs a real launchd registration).

  2. The ON-FIRE handler (`fire`): what `launchd`'s `ProgramArguments`
     actually invoke when the timer fires. It resolves an explicit `now`
     (via an injectable `now_provider`, defaulting to the real wall clock in
     production; every test injects a fake one), delegates the ENTIRE
     eligibility/idempotency decision to Package D's `run_wake_trigger`
     (called in-process, exactly per `build_wake_trigger_arg_parser`'s
     versioned argument shape), and -- ONLY when Package D reports its
     `"success"` exit code (a fresh claim or an idempotent same-owner
     duplicate) -- invokes Package E's published resume-trigger CLI shape
     strictly as an EXTERNAL SUBPROCESS (an injectable `resume_runner`; this
     module never imports Package E, so it cannot re-implement or bypass
     whatever eligibility/idempotency logic that CLI itself owns). A failed
     or crashing resume-trigger subprocess is reported as
     `"resume_trigger_failed"` -- the durable PauseLease claim Package D
     already committed is NEVER rolled back (Package D/B's durable state is
     the sole source of truth) and this module NEVER reports success for a
     resume-trigger invocation that did not genuinely exit zero.

Adapter absence is safe by construction: neither Package D nor Package B
imports this module (see `test_cowork_wake_macos.py`'s
`AdapterAbsenceIsSafeTest`), so durable recovery (a manual `wake_trigger`
invocation, `reclaim_if_expired`, or Package F's own sibling manual/emergency
adapter) remains fully possible with zero involvement from this module.

Public API:
    WakeAdapterError
    build_launchd_plist(automation_ref, program_arguments,
        start_interval_seconds=None, run_at_load=False)
    plist_path_for(automation_ref, base_dir=None)
    write_plist(path, plist_dict)
    LAUNCHCTL_EXIT_CODES (success, failed)
    install(automation_ref, program_arguments, start_interval_seconds=None,
        base_dir=None, launchctl_runner=None) -> dict incl. "ok"
    uninstall(automation_ref, base_dir=None, launchctl_runner=None)
        -> dict incl. "ok"; never deletes the plist when unload failed
    status(automation_ref, base_dir=None, launchctl_runner=None)
        -> dict incl. "ok"
    build_d_wake_trigger_argv(session_uuid, lease_id, claimant_ref,
        automation_ref, now, reference_now=None,
        max_clock_skew_seconds=None, max_jitter_seconds=None)
    run_d_wake_decision(..., wake_trigger=None) -> (exit_code, payload)
        -- payload parsing NEVER raises, even for malformed/multi-line D
        output (see `_parse_d_output`)
    build_resume_trigger_argv(resume_trigger_cmd, session_uuid, lease_id,
        claimant_ref, automation_ref)
    invoke_resume_trigger(resume_trigger_cmd, session_uuid, lease_id,
        claimant_ref, automation_ref, runner=None, timeout=...)
    FIRE_EXIT_SUCCESS, FIRE_EXIT_RESUME_TRIGGER_FAILED (F-locally disjoint
        from every value in `scheduler.WAKE_TRIGGER_EXIT_CODES` except the
        shared "0 means success" convention -- see StructuralGatesTest's
        `test_f_local_exit_codes_disjoint_from_d_exit_codes`)
    fire(session_uuid, lease_id, automation_ref, resume_trigger_cmd,
        claimant_ref=None, now=None, reference_now=None,
        max_clock_skew_seconds=None, max_jitter_seconds=None,
        wake_trigger=None, resume_runner=None, now_provider=None)
        -- on a non-`"success"` D exit code, the result carries an explicit
        `"d_disposition"` naming Package D's own outcome (F-MJ-01/F-N01:
        never just a bare integer); on a resume-trigger failure, the result
        carries a `"durable_ceiling_note"` documenting the F-MJ-02 seam
        below
    build_arg_parser(), main(argv)

Python 3.9+, stdlib plus `cowork_capacity_scheduler` (Package D) only.
"""

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_capacity_scheduler as scheduler  # Package D

# --------------------------------------------------------------------------- #
# Errors.                                                                     #
# --------------------------------------------------------------------------- #


class WakeAdapterError(Exception):
    """Base class for every error this module itself raises."""


# --------------------------------------------------------------------------- #
# Identity safety: `automation_ref` becomes a launchd Label AND a filename   #
# component, so it is validated here before ever touching the filesystem --  #
# the VALUE passed through to Package D is always the exact, unmodified      #
# caller-supplied string; this check only ever refuses, never rewrites it.   #
# --------------------------------------------------------------------------- #

_SAFE_AUTOMATION_REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\-\.]{0,254}$')


def _assert_safe_automation_ref(automation_ref):
    if not isinstance(automation_ref, str) or not automation_ref:
        raise ValueError("automation_ref must be a nonempty string")
    if not _SAFE_AUTOMATION_REF_RE.match(automation_ref):
        raise ValueError(
            "automation_ref %r is unsafe as a launchd Label/filename "
            "(must match [A-Za-z0-9][A-Za-z0-9_\\-.]{0,254})" % automation_ref)
    if os.sep in automation_ref or (os.altsep and os.altsep in automation_ref):
        raise ValueError(
            "automation_ref %r contains a path separator" % automation_ref)


# --------------------------------------------------------------------------- #
# Launchd registration: plist build/write, install/uninstall/status via an  #
# injectable launchctl runner. NEVER invoked with the real runner in this   #
# package's own test suite.                                                  #
# --------------------------------------------------------------------------- #

DEFAULT_LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")


def build_launchd_plist(automation_ref, program_arguments,
                        start_interval_seconds=None, run_at_load=False):
    """Build the launchd property-list dict for one wake job. `Label` is set
    to `automation_ref` VERBATIM -- the exact identity later checked by
    Package D's `automation_ref` match on fire. `program_arguments` is the
    caller-assembled argv (this module's own `fire` subcommand, plus
    whatever static flags the caller wants baked in); this function performs
    no interpretation of it."""
    _assert_safe_automation_ref(automation_ref)
    if not isinstance(program_arguments, (list, tuple)) or not program_arguments:
        raise ValueError("program_arguments must be a nonempty list of strings")
    if not all(isinstance(a, str) for a in program_arguments):
        raise ValueError("program_arguments must all be strings")
    plist = {
        "Label": automation_ref,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": bool(run_at_load),
    }
    if start_interval_seconds is not None:
        if (not isinstance(start_interval_seconds, (int, float))
                or isinstance(start_interval_seconds, bool)
                or start_interval_seconds <= 0):
            raise ValueError(
                "start_interval_seconds must be a positive number, got %r"
                % (start_interval_seconds,))
        plist["StartInterval"] = int(start_interval_seconds)
    return plist


def plist_path_for(automation_ref, base_dir=None):
    """Path of the launchd plist for one `automation_ref`. Raises ValueError
    for an unsafe `automation_ref` before ever constructing a path."""
    _assert_safe_automation_ref(automation_ref)
    base_dir = base_dir or DEFAULT_LAUNCH_AGENTS_DIR
    return os.path.join(base_dir, "%s.plist" % automation_ref)


def write_plist(path, plist_dict):
    """Durably write `plist_dict` as an XML property list at `path`,
    creating any missing parent directory."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(plist_dict, fh)
    return path


def _real_launchctl_runner(argv):
    """The ONLY place this module ever shells out to a real `launchctl`.
    Never called by this package's own test suite -- every test supplies its
    own fake `launchctl_runner`."""
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return {"returncode": completed.returncode, "stdout": completed.stdout,
           "stderr": completed.stderr}


# Launchctl's own CLI convention: exit 0 means success, anything else is a
# failure this module must never silently treat as success (F-MJ-03).
LAUNCHCTL_EXIT_SUCCESS = 0
LAUNCHCTL_EXIT_FAILED = 7

LAUNCHCTL_EXIT_CODES = {
    "success": LAUNCHCTL_EXIT_SUCCESS,
    "failed": LAUNCHCTL_EXIT_FAILED,
}


def _launchctl_result_ok(result):
    """A launchctl invocation is treated as successful ONLY when its own
    reported `returncode` is exactly `0` -- a missing/malformed key (a fake
    runner's own bug, in a test) is treated as failure, never silently as
    success."""
    return isinstance(result, dict) and result.get("returncode") == 0


def install(automation_ref, program_arguments, start_interval_seconds=None,
           run_at_load=False, base_dir=None, launchctl_runner=None):
    """Write the plist for `automation_ref` and load it via `launchctl load
    -w <path>`. `launchctl_runner` defaults to a REAL subprocess in
    production; ordinary tests always inject a fake one -- no real launchd
    registration ever happens in this package's own test suite.

    Returns `{"plist_path", "launchctl_result", "ok"}` -- `ok` is `True`
    only when `launchctl load` itself genuinely reported exit code `0`
    (F-MJ-03); a failed load still leaves the plist file written (so a
    caller can inspect/retry), but the caller (`main`) reports a nonzero
    CLI status for it, never a silent success."""
    launchctl_runner = launchctl_runner or _real_launchctl_runner
    plist = build_launchd_plist(automation_ref, program_arguments,
                                start_interval_seconds=start_interval_seconds,
                                run_at_load=run_at_load)
    path = plist_path_for(automation_ref, base_dir=base_dir)
    write_plist(path, plist)
    result = launchctl_runner(["launchctl", "load", "-w", path])
    return {"plist_path": path, "launchctl_result": result,
           "ok": _launchctl_result_ok(result)}


def uninstall(automation_ref, base_dir=None, launchctl_runner=None):
    """Unload (`launchctl unload -w <path>`) and remove the plist for
    `automation_ref`, if present. Safe to call when nothing is installed --
    `launchctl unload` of a missing path is left to the (fake, in tests)
    runner to report.

    F-MJ-03: the plist file is removed ONLY when `launchctl unload` itself
    genuinely reported exit code `0` -- a FAILED unload leaves the plist
    file exactly as it was (never deletes registration state the OS may
    still be holding), so a caller can inspect or retry rather than losing
    track of a job launchd itself may still consider loaded. Returns
    `{"plist_path", "launchctl_result", "ok"}`."""
    launchctl_runner = launchctl_runner or _real_launchctl_runner
    path = plist_path_for(automation_ref, base_dir=base_dir)
    result = launchctl_runner(["launchctl", "unload", "-w", path])
    ok = _launchctl_result_ok(result)
    if ok and os.path.exists(path):
        os.remove(path)
    return {"plist_path": path, "launchctl_result": result, "ok": ok}


def status(automation_ref, base_dir=None, launchctl_runner=None):
    """`launchctl list <automation_ref>` plus whether the plist file itself
    is present on disk -- a purely observational query, no mutation.
    Returns `{"plist_path", "plist_present", "launchctl_result", "ok"}`;
    `ok` reflects the `launchctl list` invocation's own exit code (F-MJ-03),
    not whether the job happens to be present -- a caller distinguishes
    "queried successfully, job present/absent" from "the query itself
    failed" via `ok` plus `plist_present`/`launchctl_result`."""
    launchctl_runner = launchctl_runner or _real_launchctl_runner
    path = plist_path_for(automation_ref, base_dir=base_dir)
    result = launchctl_runner(["launchctl", "list", automation_ref])
    return {"plist_path": path, "plist_present": os.path.exists(path),
           "launchctl_result": result, "ok": _launchctl_result_ok(result)}


# --------------------------------------------------------------------------- #
# Package D delegation: exact wake-trigger argument shape, versioned exit    #
# codes consulted by name.                                                   #
# --------------------------------------------------------------------------- #


def build_d_wake_trigger_argv(session_uuid, lease_id, claimant_ref,
                              automation_ref, now, reference_now=None,
                              max_clock_skew_seconds=None,
                              max_jitter_seconds=None):
    """Assemble argv for Package D's `run_wake_trigger`, in the EXACT flag
    names/order `build_wake_trigger_arg_parser` defines. Never invents or
    reorders a flag; every optional flag is omitted (letting Package D apply
    its own documented default) rather than re-deriving that default here."""
    argv = ["--session-uuid", session_uuid, "--lease-id", lease_id,
           "--claimant-ref", claimant_ref, "--automation-ref", automation_ref,
           "--now", now]
    if reference_now is not None:
        argv += ["--reference-now", reference_now]
    if max_clock_skew_seconds is not None:
        argv += ["--max-clock-skew-seconds", str(max_clock_skew_seconds)]
    if max_jitter_seconds is not None:
        argv += ["--max-jitter-seconds", str(max_jitter_seconds)]
    return argv


def _parse_d_output(lines):
    """F-N03: TOTAL handling of Package D's own single-JSON-line-per-
    invocation contract -- NEVER raises, regardless of what `lines` holds.
    Package D's own contract (`run_wake_trigger`'s docstring) is to write
    EXACTLY one JSON object line; this function tolerates every way that
    could be violated (by a genuine Package D bug, or by a test's own faked
    `wake_trigger`) without ever crashing this module or fabricating one of
    Package D's OWN outcome names for data Package D never actually sent.

    Returns a dict. On the well-formed single-line case, that line's parsed
    JSON, unchanged. On zero lines, a malformed line, a line that parses but
    is not a JSON object, or MORE than one line, returns a dict carrying
    `"outcome": "unknown"` plus a `"d_output_anomaly"` naming exactly which
    contract violation was observed -- this is deliberately NEVER one of
    Package D's own versioned outcome names, so a caller can never mistake
    an anomaly report for a genuine Package D decision. Crucially, `fire`
    NEVER uses this payload to decide eligibility -- that decision is
    `run_d_wake_decision`'s OTHER return value, the integer `exit_code`
    Package D's callable itself returned directly, entirely independent of
    whether this parse succeeds."""
    if not lines:
        return {"outcome": "unknown", "d_output_anomaly": "no_output_line"}
    try:
        payload = json.loads(lines[0])
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"outcome": "unknown", "d_output_anomaly": "malformed_json",
               "raw": lines[0]}
    if not isinstance(payload, dict):
        return {"outcome": "unknown", "d_output_anomaly": "not_an_object",
               "raw": lines[0]}
    if len(lines) > 1:
        payload = dict(payload)
        payload["d_output_anomaly"] = "multiple_lines"
    return payload


def run_d_wake_decision(session_uuid, lease_id, claimant_ref, automation_ref,
                        now, reference_now=None, max_clock_skew_seconds=None,
                        max_jitter_seconds=None, wake_trigger=None):
    """Delegate the ENTIRE eligibility/idempotency decision to Package D's
    `run_wake_trigger` (called in-process; `wake_trigger` defaults to
    `scheduler.run_wake_trigger` and is injectable only so tests can observe
    a fault Package D itself already knows how to report, never to
    substitute this module's own decision for Package D's). Returns
    `(exit_code, payload)` where `exit_code` is the exact integer Package
    D's callable returned (one of `WAKE_TRIGGER_EXIT_CODES`'s values,
    trusted verbatim -- THIS is what every eligibility decision downstream
    is keyed on) and `payload` is `_parse_d_output`'s TOTAL, never-raising
    parse of whatever Package D wrote."""
    wake_trigger = wake_trigger or scheduler.run_wake_trigger
    argv = build_d_wake_trigger_argv(
        session_uuid, lease_id, claimant_ref, automation_ref, now,
        reference_now=reference_now,
        max_clock_skew_seconds=max_clock_skew_seconds,
        max_jitter_seconds=max_jitter_seconds)
    lines = []
    exit_code = wake_trigger(argv, output=lines.append)
    payload = _parse_d_output(lines)
    return exit_code, payload


# Reverse lookup so `fire` can attach an explicit, NAMED disposition (F-N01)
# alongside Package D's raw integer exit code -- built once from Package
# D's own exported mapping, never a parallel literal copy of it.
_D_EXIT_CODE_TO_DISPOSITION = {
    value: name for name, value in scheduler.WAKE_TRIGGER_EXIT_CODES.items()}


# --------------------------------------------------------------------------- #
# Package E delegation: external subprocess ONLY -- this module never       #
# imports Package E, so it can never re-implement or bypass whatever        #
# eligibility/idempotency logic that CLI itself owns.                        #
# --------------------------------------------------------------------------- #

RESUME_TRIGGER_TIMEOUT_SECONDS = 60


def build_resume_trigger_argv(resume_trigger_cmd, session_uuid, lease_id,
                              claimant_ref, automation_ref):
    """`resume_trigger_cmd` is the caller-configured base argv (executable
    plus any fixed leading arguments) for Package E's published
    resume-trigger CLI -- this module treats it as opaque external
    configuration and never hardcodes Package E's own shape, since Package E
    is out of this package's writable scope and must never be imported.
    Appends the same identifying flags Package D's own wake-trigger contract
    uses, so a resume-trigger implementation can correlate this invocation
    with the PauseLease Package D just claimed."""
    if not isinstance(resume_trigger_cmd, (list, tuple)) or not resume_trigger_cmd:
        raise ValueError("resume_trigger_cmd must be a nonempty list of strings")
    if not all(isinstance(a, str) for a in resume_trigger_cmd):
        raise ValueError("resume_trigger_cmd must all be strings")
    return list(resume_trigger_cmd) + [
        "--session-uuid", session_uuid, "--lease-id", lease_id,
        "--claimant-ref", claimant_ref, "--automation-ref", automation_ref]


def _real_subprocess_runner(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def invoke_resume_trigger(resume_trigger_cmd, session_uuid, lease_id,
                          claimant_ref, automation_ref, runner=None,
                          timeout=RESUME_TRIGGER_TIMEOUT_SECONDS):
    """Invoke Package E's resume-trigger CLI STRICTLY as an external
    subprocess (`runner` defaults to a real `subprocess.run` in production;
    every test injects a fake one). Never raises for an ordinary subprocess
    failure/crash -- returns a structured `{"outcome": ...}` dict instead, so
    a failed or crashing resume-trigger NEVER masquerades as this module
    raising past a truthful report; the caller (`fire`) is the one place
    that turns this into the adapter's own outcome."""
    runner = runner or _real_subprocess_runner
    argv = build_resume_trigger_argv(
        resume_trigger_cmd, session_uuid, lease_id, claimant_ref, automation_ref)
    try:
        completed = runner(argv, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"outcome": "resume_trigger_failed", "detail": str(exc)}
    returncode = getattr(completed, "returncode", None)
    if returncode != 0:
        return {"outcome": "resume_trigger_failed", "returncode": returncode,
                "stderr": getattr(completed, "stderr", None)}
    return {"outcome": "resume_trigger_invoked", "returncode": 0,
           "stdout": getattr(completed, "stdout", None)}


# --------------------------------------------------------------------------- #
# fire: the on-fire handler. No eligibility decision of its own -- only     #
# dispatches based on Package D's own versioned exit code.                   #
# --------------------------------------------------------------------------- #

FIRE_EXIT_SUCCESS = 0
FIRE_EXIT_RESUME_TRIGGER_FAILED = 6

FIRE_EXIT_CODES = {
    "success": FIRE_EXIT_SUCCESS,
    "resume_trigger_failed": FIRE_EXIT_RESUME_TRIGGER_FAILED,
}


# --------------------------------------------------------------------------- #
# F-MJ-02: the durable ceiling seam this module does NOT own.                #
#                                                                              #
# Package D's own `failed_wake_attempts` ceiling (`cowork_capacity.          #
# FAILED_WAKE_ATTEMPT_CEILING`, enforced via `record_failed_wake_attempt`)   #
# bounds repeated CLAIM attempts -- e.g. repeated lock/I/O failures INSIDE   #
# Package D's own `claim_pause_lease` call, before a claim ever succeeds.    #
# It does NOT bound repeated RESUME-TRIGGER failures AFTER a claim has       #
# already durably succeeded: once a lease is `claimed`, every subsequent     #
# fire of the SAME automation_ref/claimant_ref reaches Package D's own       #
# idempotent `"already_claimed"` outcome (still `WAKE_TRIGGER_EXIT_CODES     #
# ["success"]`) every time, so this module re-attempts the resume-trigger    #
# subprocess on every fire, indefinitely, with NO durable ceiling ever       #
# applying -- see `PostClaimResumeTriggerCeilingSeamTest` in this package's  #
# own test file for a non-vacuous pin of this EXACT current behavior.        #
#                                                                              #
# Closing this gap durably would require a NEW counter Package D itself      #
# owns and persists (mirroring `failed_wake_attempts`, but keyed to          #
# post-claim resume-trigger failures rather than pre-claim ones) -- that is  #
# explicitly Package D's decision-layer responsibility, not this module's:   #
# F-MJ-02 forbids this module from reimplementing any such durable           #
# counter/storage/locking of its own. The one SAFE, BOUNDED improvement      #
# this module makes within its own scope is purely informational: every     #
# `"resume_trigger_failed"` result below carries an explicit `"durable_     #
# ceiling_note"` field naming this exact seam, so a caller (a human          #
# operator, or Package E) observing repeated failures for the same          #
# lease_id has a documented pointer to why nothing durably bounds the        #
# retry count yet, rather than silently assuming Package D's existing        #
# ceiling already covers this case.                                          #
# --------------------------------------------------------------------------- #

_POST_CLAIM_RESUME_CEILING_NOTE = (
    "Package D's failed_wake_attempts ceiling bounds pre-claim attempts "
    "only (repeated lock/I/O failures inside claim_pause_lease); this "
    "module owns no durable counter for post-claim resume-trigger "
    "failures and never reimplements one (F-MJ-02) -- a claimed lease "
    "whose resume-trigger keeps failing will keep being re-attempted on "
    "every subsequent fire with no durable ceiling until Package D adds "
    "one of its own.")


def _real_now_provider():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fire(session_uuid, lease_id, automation_ref, resume_trigger_cmd,
        claimant_ref=None, now=None, reference_now=None,
        max_clock_skew_seconds=None, max_jitter_seconds=None,
        wake_trigger=None, resume_runner=None, now_provider=None):
    """The on-fire handler. Resolves an explicit `now` (real wall clock in
    production via `now_provider`, defaulting to `_real_now_provider`; every
    test injects a fake one), delegates eligibility/idempotency entirely to
    Package D (`run_d_wake_decision`), and -- ONLY on Package D's own
    `"success"` exit code -- invokes Package E's resume-trigger CLI as an
    external subprocess.

    `claimant_ref` defaults to `automation_ref` itself: one launchd job
    (one Label) is one claim identity, so repeated fires of the SAME job
    present the SAME claimant_ref, which is what makes Package D's own
    same-owner idempotency (duplicate fires yield exactly one D claim)
    apply here without this module tracking anything of its own.

    Returns a dict `{"outcome": "resumed" | "wake_not_actionable" |
    "resume_trigger_failed", "exit_code": ..., "d_payload": ...,
    "resume_result": ... (only for the latter two outcomes with a resume-
    trigger attempt)}`. A `"wake_not_actionable"` result additionally
    carries `"d_disposition"` -- Package D's own outcome NAME for
    `exit_code` (F-N01: an explicit disposition, never just a bare
    integer). A `"resume_trigger_failed"` result additionally carries
    `"durable_ceiling_note"` (see the F-MJ-02 section above this function).
    NEVER reports `"resumed"` unless Package D reported its own `"success"`
    exit code AND the resume-trigger subprocess genuinely exited zero."""
    now_provider = now_provider or _real_now_provider
    resolved_now = now if now is not None else now_provider()
    resolved_claimant_ref = claimant_ref if claimant_ref is not None else automation_ref

    exit_code, d_payload = run_d_wake_decision(
        session_uuid, lease_id, resolved_claimant_ref, automation_ref,
        resolved_now, reference_now=reference_now,
        max_clock_skew_seconds=max_clock_skew_seconds,
        max_jitter_seconds=max_jitter_seconds, wake_trigger=wake_trigger)

    if exit_code != scheduler.WAKE_TRIGGER_EXIT_CODES["success"]:
        return {"outcome": "wake_not_actionable", "exit_code": exit_code,
               "d_disposition": _D_EXIT_CODE_TO_DISPOSITION.get(
                   exit_code, "unrecognized_d_exit_code"),
               "d_payload": d_payload}

    resume_result = invoke_resume_trigger(
        resume_trigger_cmd, session_uuid, lease_id, resolved_claimant_ref,
        automation_ref, runner=resume_runner)
    if resume_result["outcome"] != "resume_trigger_invoked":
        return {"outcome": "resume_trigger_failed",
               "exit_code": FIRE_EXIT_RESUME_TRIGGER_FAILED,
               "d_payload": d_payload, "resume_result": resume_result,
               "durable_ceiling_note": _POST_CLAIM_RESUME_CEILING_NOTE}

    return {"outcome": "resumed", "exit_code": FIRE_EXIT_SUCCESS,
           "d_payload": d_payload, "resume_result": resume_result}


# --------------------------------------------------------------------------- #
# CLI.                                                                        #
# --------------------------------------------------------------------------- #


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="cowork_wake_macos",
        description="M3 Package F macOS launchd wake adapter.")
    sub = parser.add_subparsers(dest="command", required=True)

    install_p = sub.add_parser("install", help="register the launchd job")
    install_p.add_argument("--automation-ref", required=True)
    install_p.add_argument("--program-argument", dest="program_arguments",
                           action="append", required=True,
                           help="repeatable; one ProgramArguments entry")
    install_p.add_argument("--start-interval-seconds", type=float, default=None)
    install_p.add_argument("--run-at-load", action="store_true")
    install_p.add_argument("--base-dir", default=None,
                           help="LaunchAgents directory; omit for the real "
                                "~/Library/LaunchAgents")

    uninstall_p = sub.add_parser("uninstall", help="unregister the launchd job")
    uninstall_p.add_argument("--automation-ref", required=True)
    uninstall_p.add_argument("--base-dir", default=None)

    status_p = sub.add_parser("status", help="query the launchd job")
    status_p.add_argument("--automation-ref", required=True)
    status_p.add_argument("--base-dir", default=None)

    fire_p = sub.add_parser("fire", help="the on-fire handler")
    fire_p.add_argument("--session-uuid", required=True)
    fire_p.add_argument("--lease-id", required=True)
    fire_p.add_argument("--automation-ref", required=True)
    fire_p.add_argument("--claimant-ref", default=None)
    fire_p.add_argument("--resume-trigger-cmd", required=True,
                        help="JSON-encoded list of strings: the external "
                             "Package E resume-trigger CLI's base argv")
    fire_p.add_argument("--now", default=None,
                        help="explicit RFC3339 clock reading; omit to use "
                             "the real wall clock")
    fire_p.add_argument("--reference-now", default=None)
    fire_p.add_argument("--max-clock-skew-seconds", type=float, default=None)
    fire_p.add_argument("--max-jitter-seconds", type=float, default=None)

    return parser


def main(argv, output=None):
    write = output if output is not None else sys.stdout.write
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "install":
        result = install(args.automation_ref, args.program_arguments,
                         start_interval_seconds=args.start_interval_seconds,
                         run_at_load=args.run_at_load, base_dir=args.base_dir)
        write(json.dumps(result) + "\n")
        return (LAUNCHCTL_EXIT_CODES["success"] if result["ok"]
               else LAUNCHCTL_EXIT_CODES["failed"])
    if args.command == "uninstall":
        result = uninstall(args.automation_ref, base_dir=args.base_dir)
        write(json.dumps(result) + "\n")
        return (LAUNCHCTL_EXIT_CODES["success"] if result["ok"]
               else LAUNCHCTL_EXIT_CODES["failed"])
    if args.command == "status":
        result = status(args.automation_ref, base_dir=args.base_dir)
        write(json.dumps(result) + "\n")
        return (LAUNCHCTL_EXIT_CODES["success"] if result["ok"]
               else LAUNCHCTL_EXIT_CODES["failed"])

    try:
        resume_trigger_cmd = json.loads(args.resume_trigger_cmd)
    except json.JSONDecodeError as exc:
        write(json.dumps({"outcome": "invalid_arguments", "detail": str(exc)}) + "\n")
        return scheduler.WAKE_TRIGGER_EXIT_CODES["invalid_arguments"]

    result = fire(
        args.session_uuid, args.lease_id, args.automation_ref,
        resume_trigger_cmd, claimant_ref=args.claimant_ref, now=args.now,
        reference_now=args.reference_now,
        max_clock_skew_seconds=args.max_clock_skew_seconds,
        max_jitter_seconds=args.max_jitter_seconds)
    write(json.dumps(result) + "\n")
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
