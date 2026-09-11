#!/usr/bin/env python3
"""Owned verification transaction: one orchestrator-owned, hermetic,
manifest-bound execution of the planner-approved command inventory.

This module is BOTH:

  (a) a library the long-lived orchestrator (`cowork.py`) imports and calls
      `run_transaction(...)` on, and
  (b) a standalone worker entry point a subprocess execs directly from an
      immutable snapshot checkout: `python3 cowork_verification.py --worker
      <request-file>`.

WHY ONE FILE DOES BOTH: the worker must run the CURRENT candidate source even
when the long-lived parent process started from an older version — there is no
way to "import" code that did not exist when the parent's Python process
started. So the parent snapshots this file (along with every other tracked and
untracked-non-ignored source file) into an immutable, content-addressed
checkout and spawns `python3 <snapshot>/scripts/cowork_verification.py
--worker <request>` as a brand new process. The worker then self-hashes its
own `__file__` and reports `{source_hash, protocol_version}` before running
any command; the parent requires that hash to equal the snapshot manifest's
entry for this file, or the whole transaction is UNVERIFIED — never trusted on
faith.

OWNERSHIP MODEL. The parent (not the plan, not the worker, not any command) is
the sole author of:

  - the immutable snapshot the worker and every command run against;
  - the subprocess `cwd` for the worker and for every command, always a path
    inside the materialized snapshot checkout, never a plan- or
    command-supplied value;
  - the single-flight lock and its key;
  - the worker's overall deadline and the liveness pipe that lets the worker
    detect parent death/cancellation and self-terminate its active command.

FAIL CLOSED. Any of: an unreadable/unsupported filesystem entry during
snapshot capture, a pre/copy/post hash mismatch, a worker source-hash or
protocol mismatch, a live-candidate source or git-index change detected before
or after any command, a malformed/expired evidence poll — aborts the
transaction before further commands run and is reported precisely. Nothing is
ever rolled back; a fail-closed transaction leaves the live tree exactly as it
found it (mutated or not) because automatic repair could overwrite exactly the
diagnostic state a user needs to see.

POSIX ONLY (macOS/Linux). Process-group semantics (`os.setsid`,
`os.killpg`, `start_new_session=True`) have no portable Windows equivalent;
this module is not expected to run there.

Python 3.9+, stdlib only.
"""

import argparse
import datetime
import errno
import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_state as state_store  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import socket  # noqa: E402

# --------------------------------------------------------------------------- #
# Protocol version and schema constants.                                      #
# --------------------------------------------------------------------------- #

# Bumped whenever the request/result JSON shape changes in a way a differently
# -versioned worker/parent could not safely interpret. A parent and worker that
# disagree on this treat the transaction as UNVERIFIED rather than guessing.
# 2: every inventory entry's `ledger_attempt_id` (the pre-minted ledger V-id)
# is now a REQUIRED field, not an optional one a worker may fall back from —
# a worker built against protocol 1 would silently invent an identity for a
# field it does not know is mandatory, so the version is bumped rather than
# leaving that shape change unversioned.
# 3: the worker must now WAIT for a parent-issued per-entry PERMIT (see
# `verification_permit_path_for`) before starting each entry, and the
# parent must not issue entry N+1's permit until entry N's terminal/
# unresolved ledger revision has durably succeeded. A protocol-2 worker
# runs entries as fast as it can with no such gate — a materially
# different behavioral contract, not just a new optional field, so this is
# a real version bump: a protocol-2 worker paired with a protocol-3
# parent (or vice versa) must be treated as UNVERIFIED, never silently
# run under the wrong lifecycle guarantee.
PROTOCOL_VERSION = 3

# Inventory schema versions (see `normalize_inventory`).
SCHEMA_2 = 2      # label, command, execution_mode, kind (+ optional metadata)
SCHEMA_1 = 1      # normalized legacy label/command-only plans

EXECUTION_MODES = ("isolated_snapshot", "candidate_read_only")

# Closed four-value `kind` enum (see the planner decision this encodes).
KIND_BASELINE = "baseline"
KIND_FOCUSED = "focused"
KIND_PREFLIGHT = "preflight"
KIND_FINAL_SUITE = "final_suite"
KINDS = (KIND_BASELINE, KIND_FOCUSED, KIND_PREFLIGHT, KIND_FINAL_SUITE)

# Legacy two-field (label/command only) plans normalize to this kind, and
# their final-suite binding is reported as "legacy_unknown" rather than
# invented (a legacy plan never expressed which entry, if any, was the final
# suite).
KIND_LEGACY_REQUIRED = "legacy_required"
FINAL_SUITE_LEGACY_UNKNOWN = "legacy_unknown"

# Terminal transaction verdicts.
VERDICT_GREEN = "green"
VERDICT_RED = "red"
VERDICT_UNVERIFIED = "unverified"

# Review dispositions for an owned transaction (ORCH-050 / CV-050). Bound to
# (transaction_id, candidate manifest) by the orchestrator at the review
# verdict / gate; carried by `verification.disposition` trace events and the
# reconciled per-session sidecar. `pending_review` until a review round judges
# the bound candidate; `accepted` only on an approving verdict (or an explicit
# gate approval) with the accepted candidate manifest still equal to the
# transaction's captured manifest; `superseded_by_finding` when a valid later
# blocking finding invalidates the green transaction; `rejected` when the
# transaction itself was red/unverified or its candidate was abandoned.
DISPOSITION_PENDING_REVIEW = "pending_review"
DISPOSITION_ACCEPTED = "accepted"
DISPOSITION_SUPERSEDED_BY_FINDING = "superseded_by_finding"
DISPOSITION_REJECTED = "rejected"
DISPOSITIONS = (DISPOSITION_PENDING_REVIEW, DISPOSITION_ACCEPTED,
                DISPOSITION_SUPERSEDED_BY_FINDING, DISPOSITION_REJECTED)

# Worker exit code for a request the worker validated and REJECTED before
# ever publishing identity (e.g. a protocol-2 request missing a required
# `ledger_attempt_id`) — distinct from 2 (unreadable request) and a normal
# 0, so the parent can report a specific "request_rejected" startup_failure
# reason instead of the generic "worker crashed" one.
WORKER_EXIT_REQUEST_REJECTED = 3

# Terminal per-attempt evidence states.
EVIDENCE_PRESENT = "present"
EVIDENCE_UNRESOLVED = "unresolved"
EVIDENCE_ABSENT = "absent"

# --------------------------------------------------------------------------- #
# Timeout / retry policy defaults. All overridable per-request; these are the
# fallbacks a caller that does not specify a policy gets.                     #
# --------------------------------------------------------------------------- #

DEFAULT_COMMAND_TIMEOUT_S = 300
DEFAULT_TERM_GRACE_S = 10
DEFAULT_STARTUP_ALLOWANCE_S = 30
DEFAULT_CLEANUP_ALLOWANCE_S = 30
DEFAULT_EVIDENCE_ALLOWANCE_S = 30
DEFAULT_EVIDENCE_POLL_ATTEMPTS = 10
DEFAULT_EVIDENCE_POLL_DELAY_S = 1.0
DEFAULT_LOCK_WAITER_DEADLINE_S = 600
DEFAULT_OUTPUT_CAP_BYTES = 1 * 1024 * 1024  # 1 MiB per stream, per command.

# Shell metacharacter tokens statically rejected in isolated_snapshot argv
# (see `validate_argv_safety`). Matched as whole-or-substring tokens, not
# regex, so no argument can smuggle a shell operator past validation.
_SHELL_METACHARS = (";", "&&", "||", "|", "`", "$(")
_CD_TOKENS = ("cd", "pushd", "popd", "source", ".")


# --------------------------------------------------------------------------- #
# Extraction seam re-exports (M5 Package A; garusis/cowork-internal#24/#44/    #
# #51). Each name below is imported by its EXACT pre-existing bare name from   #
# the seam module Package A relocated its implementation into, so every spine  #
# call site below -- and `scripts/test_cowork.py`'s one                        #
# `mock.patch.object(verification, "spawn_worker", ...)` call site plus its    #
# direct `verification.<name>(...)` attribute call sites -- keep resolving     #
# with zero edits to that file. Python resolves an unqualified name at CALL    #
# time against this module's own `__dict__`, so `mock.patch.object` on this    #
# module's attribute intercepts every bare-name call site exactly as it did    #
# when the implementation lived here directly. `self_source_hash` is           #
# deliberately EXCLUDED from this list: it stays defined below, in Section 7,  #
# never imported from either seam module -- see that function's own            #
# docstring for why.                                                           #
#                                                                              #
# WHY THIS IS WRAPPED IN try/except ModuleNotFoundError (narrowly, not a     #
# blanket ImportError -- see below). A `--worker` subprocess execs THIS      #
# file alone, from an immutable snapshot checkout of the TARGET repo         #
# `run_transaction`'s caller pointed at (see the module docstring's WHY ONE  #
# FILE DOES BOTH). A target repo that does not itself track                  #
# `cowork_verification_worker.py`/`cowork_verification_evidence.py` --        #
# this module's own two extraction-seam siblings -- alongside its own         #
# `cowork_verification.py` will not have them materialized in that checkout   #
# either (this is the base commit's own established contract: the checkout   #
# is a straight snapshot of the target repo's tracked + untracked-non-        #
# ignored files, nothing more; `scripts/test_cowork.py`'s own                 #
# `_seed_worker_into_repo` fixture is exactly such a target repo, seeding      #
# only `cowork_verification.py`/`cowork_state.py`/`cowork_policy.py`/          #
# `cowork_ledger.py`). Every name imported below is PARENT-SIDE ONLY --        #
# `worker_main` (the sole entry point a `--worker` subprocess actually runs)   #
# never calls any of them -- so a worker process spawned into such a          #
# checkout must still be able to load this module and run its approved        #
# inventory; only an actual CALL to one of these names, outside a checkout    #
# that has both sibling files present, is an error, and it is raised loudly   #
# at that call (never silently), not swallowed at import time.                #
#                                                                              #
# NARROWED TO THE TWO SEAM MODULES BY NAME. `except ImportError` alone would  #
# also swallow a genuine bug INSIDE `cowork_verification_worker.py`/           #
# `cowork_verification_evidence.py` -- e.g. one of THEM failing to import      #
# `cowork_state` for an unrelated reason -- silently misreporting a real       #
# defect as "the sibling files are merely absent". `ModuleNotFoundError.name`  #
# names the SPECIFIC module Python could not find; only when it is exactly    #
# one of these two known, expected-to-sometimes-be-missing siblings does the  #
# fallback apply. Any other `ImportError`/`ModuleNotFoundError` -- including   #
# one raised from further down an import chain a present seam module itself   #
# starts -- propagates and fails loudly, exactly as it would have before this  #
# fallback existed.                                                          #
# --------------------------------------------------------------------------- #

_SEAM_MODULE_NAMES = ("cowork_verification_worker",
                     "cowork_verification_evidence")

try:
    from cowork_verification_worker import (  # noqa: E402
        spawn_worker, verify_worker_identity, _read_worker_startup_log,
        _capture_startup_log, MAX_STARTUP_LOG_BYTES, WorkerStartupResult,
        reclaim_tool_snapshot_checkout,
    )
    from cowork_verification_evidence import (  # noqa: E402
        bounded_evidence_wait, _poll_attempt_events, _revise_attempt_ledger,
        _revise_attempt_ledger_with_retry, _wait_for_attempt_and_revise_ledger,
        should_defer_teardown,
    )
except ModuleNotFoundError as _seam_import_error:
    if _seam_import_error.name not in _SEAM_MODULE_NAMES:
        raise
    # Captured as a plain string BEFORE the closure below is ever defined:
    # Python implicitly `del`s an `except ... as name:` target the moment
    # this block exits, so a closure that instead captured
    # `_seam_import_error` itself (by reference, as closures do) would
    # raise a NameError the first time it was actually CALLED, well after
    # this except block has already exited -- silently defeating the whole
    # point of a loud, clear fallback error.
    _seam_import_error_message = str(_seam_import_error)

    def _seam_unavailable(*_args, **_kwargs):
        raise ImportError(
            "cowork_verification_worker.py/cowork_verification_evidence.py "
            "are not present alongside this checkout of "
            "cowork_verification.py (%s); this name is parent-side only "
            "and unavailable to a --worker subprocess spawned into a "
            "target repo that does not track its own copy of the Cowork "
            "tool source." % _seam_import_error_message)

    spawn_worker = _seam_unavailable
    verify_worker_identity = _seam_unavailable
    _read_worker_startup_log = _seam_unavailable
    _capture_startup_log = _seam_unavailable
    reclaim_tool_snapshot_checkout = _seam_unavailable
    bounded_evidence_wait = _seam_unavailable
    _poll_attempt_events = _seam_unavailable
    _revise_attempt_ledger = _seam_unavailable
    _revise_attempt_ledger_with_retry = _seam_unavailable
    _wait_for_attempt_and_revise_ledger = _seam_unavailable
    should_defer_teardown = _seam_unavailable
    # Drift-protected fallback (M5A minor): this literal must stay equal to
    # cowork_verification_worker.MAX_STARTUP_LOG_BYTES's own definition --
    # scripts/test_m5_package_a_contracts.py's
    # DriftProtectedFallbackConstantTests mechanically compares this exact
    # source line's value against that module's real constant on every run,
    # so a future edit to one without the other fails a gate immediately
    # instead of silently diverging.
    MAX_STARTUP_LOG_BYTES = 64 * 1024
    WorkerStartupResult = None


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def new_transaction_id():
    """Mint a fresh transaction id. Caller-owned identity (see
    `cowork_state.verification_transaction_dir`) — not derived from content,
    so two transactions with identical content still get distinct homes."""
    return uuid.uuid4().hex


# =========================================================================== #
# Section 1: request/result protocol + schema-2 inventory validation.         #
# =========================================================================== #


class InventoryError(ValueError):
    """Raised by `normalize_inventory`/`validate_argv_safety` for a
    structurally invalid or unsafe inventory. Carries a stable `code` so a
    caller can render or test against the specific rejection reason without
    parsing prose."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def normalize_inventory(raw_verification, declared_schema=None):
    """Validate and normalize an approved command inventory.

    Accepts either:

      - a schema-2 list of dicts, each with `label`, `command` (an argv
        list), `execution_mode`, `kind`, and optional measurement/attribution
        metadata (`invalidation_reason`, `reuse_decision`,
        `triggering_finding`, `marginal_cost`, `measures`); or
      - a legacy list of dicts with only `label`/`command` (no
        `execution_mode`/`kind`/`verification_schema` anywhere), which is
        normalized to schema-1 records: `execution_mode="isolated_snapshot"`,
        `kind="legacy_required"`.

    Returns `(schema, entries, final_suite_label)` where `schema` is
    `SCHEMA_2` or `SCHEMA_1`, `entries` is the normalized list (each a dict
    with at least label/command/execution_mode/kind), and `final_suite_label`
    is the label of the single `kind=final_suite` entry for schema 2, or
    `FINAL_SUITE_LEGACY_UNKNOWN` for schema 1.

    Raises `InventoryError` for anything a schema-2 plan gets wrong: unknown
    or missing `execution_mode`/`kind`, a duplicate label, or a missing,
    multiple, or non-last `final_suite` entry. Validation happens BEFORE any
    worker starts — this function does no I/O and spawns nothing.

    `declared_schema`, when given, is the plan's OWN `verification_schema`
    field (an int, or `None` if the plan never set one) as read by the
    caller from the plan document itself — a value the caller controls, not
    something derived from the entries it is about to validate. When given,
    it is authoritative: a plan that declared no `verification_schema` (or
    `1`) may never be smuggled through as schema 2 by an entry that merely
    carries `execution_mode`/`kind` fields (e.g. a builder- or reviewer-
    constructed inventory that copied those keys onto an otherwise-legacy
    plan), and a plan that declared `verification_schema: 2` may never
    silently fall back to legacy normalization because an entry happens to
    omit those fields. Either mismatch is a hard `InventoryError` rather
    than a silent reinterpretation — this is what keeps a legacy plan's
    weaker (whole-inventory, `legacy_unknown`-final-suite) readiness
    contract from being upgraded, or a schema-2 plan's stricter contract
    from being downgraded, by anything other than the plan itself.
    """
    if not isinstance(raw_verification, list) or not raw_verification:
        raise InventoryError("empty_inventory", "verification inventory is "
                             "missing or empty")
    is_schema2 = any(
        isinstance(e, dict) and ("execution_mode" in e or "kind" in e)
        for e in raw_verification)
    if declared_schema is not None:
        if declared_schema == SCHEMA_2 and not is_schema2:
            raise InventoryError(
                "declared_schema_mismatch",
                "plan declares verification_schema=2 but its inventory "
                "entries carry no execution_mode/kind fields")
        if declared_schema != SCHEMA_2 and is_schema2:
            raise InventoryError(
                "declared_schema_mismatch",
                "plan declares verification_schema=%r (legacy) but its "
                "inventory entries carry execution_mode/kind fields; a "
                "legacy plan's contract cannot be upgraded by entry-level "
                "fields" % (declared_schema,))
    if not is_schema2:
        return _normalize_legacy_inventory(raw_verification)
    return _normalize_schema2_inventory(raw_verification)


def _normalize_legacy_inventory(raw_verification):
    entries = []
    seen_labels = set()
    for i, item in enumerate(raw_verification):
        if not isinstance(item, dict):
            raise InventoryError("bad_entry", "entry %d is not an object" % i)
        label = str(item.get("label") or "").strip()
        command = item.get("command")
        if not label:
            raise InventoryError("missing_label", "entry %d has no label" % i)
        if label in seen_labels:
            raise InventoryError("duplicate_label",
                                 "duplicate label %r" % label)
        seen_labels.add(label)
        if not _is_argv_list(command):
            raise InventoryError(
                "bad_command", "entry %r command must be a non-empty argv "
                "list of strings" % label)
        entries.append({
            "label": label,
            "command": list(command),
            "execution_mode": "isolated_snapshot",
            "kind": KIND_LEGACY_REQUIRED,
        })
    return SCHEMA_1, entries, FINAL_SUITE_LEGACY_UNKNOWN


def _normalize_schema2_inventory(raw_verification):
    entries = []
    seen_labels = set()
    final_suite_label = None
    final_suite_index = None
    for i, item in enumerate(raw_verification):
        if not isinstance(item, dict):
            raise InventoryError("bad_entry", "entry %d is not an object" % i)
        label = str(item.get("label") or "").strip()
        command = item.get("command")
        mode = item.get("execution_mode")
        kind = item.get("kind")
        if not label:
            raise InventoryError("missing_label", "entry %d has no label" % i)
        if label in seen_labels:
            raise InventoryError("duplicate_label",
                                 "duplicate label %r" % label)
        seen_labels.add(label)
        if mode not in EXECUTION_MODES:
            raise InventoryError(
                "bad_execution_mode",
                "entry %r has unknown/missing execution_mode %r "
                "(expected one of %s)" % (label, mode, EXECUTION_MODES))
        if kind not in KINDS:
            raise InventoryError(
                "bad_kind",
                "entry %r has unknown/missing kind %r (expected one of %s)"
                % (label, kind, KINDS))
        if kind == KIND_PREFLIGHT and mode != "candidate_read_only":
            raise InventoryError(
                "preflight_wrong_mode",
                "entry %r is kind=preflight but execution_mode is %r "
                "(preflight must be candidate_read_only)" % (label, mode))
        if kind != KIND_PREFLIGHT and mode != "isolated_snapshot":
            raise InventoryError(
                "downgraded_mode",
                "entry %r (kind=%s) must use execution_mode=isolated_snapshot"
                % (label, kind))
        if not _is_argv_list(command):
            raise InventoryError(
                "bad_command", "entry %r command must be a non-empty argv "
                "list of strings" % label)
        if kind == KIND_FINAL_SUITE:
            if final_suite_label is not None:
                raise InventoryError(
                    "multiple_final_suite",
                    "more than one kind=final_suite entry (%r and %r)"
                    % (final_suite_label, label))
            final_suite_label = label
            final_suite_index = i
        entry = {
            "label": label,
            "command": list(command),
            "execution_mode": mode,
            "kind": kind,
        }
        for key in ("invalidation_reason", "reuse_decision",
                    "triggering_finding", "marginal_cost", "measures"):
            if key in item:
                entry[key] = item[key]
        entries.append(entry)
    if final_suite_label is None:
        raise InventoryError("missing_final_suite",
                             "no kind=final_suite entry present")
    if final_suite_index != len(raw_verification) - 1:
        raise InventoryError("final_suite_not_last",
                             "kind=final_suite entry %r is not last"
                             % final_suite_label)
    return SCHEMA_2, entries, final_suite_label


def _is_argv_list(command):
    return (isinstance(command, list) and len(command) > 0
            and all(isinstance(tok, str) for tok in command))


def normalized_inventory_key(schema, entries):
    """A stable string used as part of the single-flight request key.

    Execution is SERIAL and FAIL-FAST: a mutation or failure on entry N
    means entries N+1.. are never reached at all. Two inventories with the
    exact same entries but a DIFFERENT ORDER among the non-final-suite
    entries are therefore NOT equivalent — reordering two baseline checks
    changes which one runs first and which later entries get skipped after
    a failure. This must preserve entry ORDER in the key (previously
    `sorted(...)` erased it, so two behaviorally distinct orderings could
    collide onto the same `request_key` and share a single-flight result
    for the wrong execution sequence). `execution_mode` and `kind` are
    still included per entry so a mode/kind change on an otherwise-
    identical, identically-ordered inventory always produces a different
    key (the planner policy can never be silently downgraded by an
    equivalent-looking waiter).
    """
    normalized = [
        (e["label"], tuple(e["command"]), e["execution_mode"], e["kind"])
        for e in entries]
    blob = json.dumps([list(t) for t in normalized], sort_keys=True)
    return hashlib.sha256(("%d:" % schema).encode("utf-8")
                          + blob.encode("utf-8")).hexdigest()


def deduplicate_inventory(entries):
    """Collapse entries whose (command, execution_mode) are identical to a
    single execution, preserving the first occurrence's label/kind/metadata
    and recording which later labels reused it. Returns `(deduped_entries,
    reused)` where `reused` maps a kept label to the list of labels that were
    deduplicated onto it. This is what turns 19 planner-listed commands into
    fewer actual executions when several labels request the same command.

    The `kind=final_suite` entry is NEVER a dedup target or a dedup source:
    it always runs as its own dedicated execution, even if its command
    happens to be textually identical to an earlier entry's. Collapsing it
    into an earlier entry would silently drop the `final_suite` kind (the
    kept entry keeps the FIRST occurrence's kind) and make the one-accepted-
    final-suite-execution guarantee unreachable — "final" is a role, not
    just a command string, so it is exempt from the command-identity
    dedup that plain focused/baseline checks are subject to.
    """
    kept = []
    index_by_identity = {}
    reused = {}
    for entry in entries:
        if entry.get("kind") == KIND_FINAL_SUITE:
            kept.append(entry)
            continue
        identity = (tuple(entry["command"]), entry["execution_mode"])
        if identity in index_by_identity:
            keeper_label = kept[index_by_identity[identity]]["label"]
            reused.setdefault(keeper_label, []).append(entry["label"])
            continue
        index_by_identity[identity] = len(kept)
        kept.append(entry)
    return kept, reused


def build_request(session_uuid, transaction_id, repo, snapshot_manifest_digest,
                  index_digest, configuration, schema, entries,
                  final_suite_label, worker_source_hash=None,
                  command_timeout_s=None, term_grace_s=None,
                  overall_deadline_s=None, evidence_poll_attempts=None,
                  evidence_poll_delay_s=None, output_cap_bytes=None,
                  work_id=None):
    """Build the versioned JSON request document persisted before the worker
    is spawned (see `cowork_state.verification_request_path_for`).

    `configuration` is a caller-supplied, already-normalized dict (e.g. team/
    role config relevant to verification); it is included verbatim in the
    single-flight request key so a configuration change always mints a new
    key. Nothing here performs I/O; the caller writes the returned dict with
    `cowork_state.write_json_atomic`.

    `work_id` (M2 Package E, additive): the WorkUnit identity of the role
    engagement this verification transaction is bound to, when the caller
    has one (see `cowork.py`'s `_role_work_id`). Purely a join-key
    correlation field on the persisted request document — never consulted
    by `request_key`, dedup, or any decision this module makes — so an
    absent `work_id` (every pre-M2 caller) changes nothing about existing
    behavior."""
    entries, reused = deduplicate_inventory(entries)
    inventory_key = normalized_inventory_key(schema, entries)
    config_blob = json.dumps(configuration or {}, sort_keys=True)
    request_key = hashlib.sha256(
        ("%s|%s|%s|%s" % (snapshot_manifest_digest, index_digest,
                          config_blob, inventory_key)).encode("utf-8")
    ).hexdigest()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "transaction_id": transaction_id,
        "session_uuid": session_uuid,
        "work_id": work_id,
        "request_key": request_key,
        "repo": repo,
        "snapshot": {
            "manifest_digest": snapshot_manifest_digest,
            "index_digest": index_digest,
        },
        "configuration": configuration or {},
        "inventory_schema": schema,
        "inventory": entries,
        "final_suite_label": final_suite_label,
        "reused_labels": reused,
        "worker_source_hash": worker_source_hash,
        "timeout_policy": {
            "command_timeout_s": command_timeout_s or DEFAULT_COMMAND_TIMEOUT_S,
            "term_grace_s": term_grace_s or DEFAULT_TERM_GRACE_S,
            "startup_allowance_s": DEFAULT_STARTUP_ALLOWANCE_S,
            "cleanup_allowance_s": DEFAULT_CLEANUP_ALLOWANCE_S,
            "evidence_allowance_s": DEFAULT_EVIDENCE_ALLOWANCE_S,
            "overall_deadline_s": overall_deadline_s,
        },
        "evidence_retry_policy": {
            "poll_attempts": evidence_poll_attempts
                             or DEFAULT_EVIDENCE_POLL_ATTEMPTS,
            "poll_delay_s": evidence_poll_delay_s
                           or DEFAULT_EVIDENCE_POLL_DELAY_S,
        },
        "output_cap_bytes": output_cap_bytes or DEFAULT_OUTPUT_CAP_BYTES,
        "created_at": _utc_now(),
    }


# =========================================================================== #
# Section 2: argv safety validation for isolated_snapshot commands.           #
# =========================================================================== #


def validate_argv_safety(entries, snapshot_checkout_root):
    """Statically reject unsafe isolated_snapshot argv BEFORE any worker
    launches. Raises `InventoryError` on the first unsafe entry found.

    Rejects, for every `execution_mode="isolated_snapshot"` entry:
      - any `cd`/`pushd`/`popd`/`source`/`.` token anywhere in argv;
      - any absolute path argument that resolves (via `os.path.realpath`,
        without requiring the path to exist) outside
        `snapshot_checkout_root`;
      - any argument containing a `..` path-traversal segment;
      - any argument that is, or contains, a shell metacharacter token (`;`,
        `&&`, `||`, `|`, backtick, `$(`).

    `candidate_read_only` entries (the CLI preflight) are NOT checked here:
    they intentionally run against the live candidate and are the only mode
    permitted to do so; the orchestrator still sets their cwd itself (never a
    plan-supplied value) at launch time, just to the live repo root instead of
    the snapshot.
    """
    root = os.path.realpath(snapshot_checkout_root)
    for entry in entries:
        if entry.get("execution_mode") != "isolated_snapshot":
            continue
        label = entry.get("label")
        for token in entry.get("command") or []:
            _check_argv_token(label, token, root)


def _check_argv_token(label, token, root):
    if token in _CD_TOKENS:
        raise InventoryError(
            "unsafe_argv_cd",
            "entry %r argv contains a cd/pushd/popd/source token (%r); the "
            "orchestrator alone sets cwd, plan commands may never change it"
            % (label, token))
    for meta in _SHELL_METACHARS:
        if meta in token:
            raise InventoryError(
                "unsafe_argv_shell_metachar",
                "entry %r argv token %r contains shell metacharacter %r"
                % (label, token, meta))
    # `..` traversal: reject any path-shaped argument with a literal `..`
    # segment, using the same splitting shlex/os.sep would use — checked
    # before existence/realpath so a nonexistent traversal target is still
    # caught (realpath alone would silently resolve it).
    parts = token.replace("\\", "/").split("/")
    if ".." in parts:
        raise InventoryError(
            "unsafe_argv_traversal",
            "entry %r argv token %r contains a '..' traversal segment"
            % (label, token))
    if os.path.isabs(token):
        resolved = os.path.realpath(token)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise InventoryError(
                "unsafe_argv_absolute_escape",
                "entry %r argv token %r is an absolute path outside the "
                "snapshot checkout root %r" % (label, token, root))
    elif "/" in token or os.sep in token:
        # A relative, path-shaped argument. Even with no `..` segment, a
        # symlink component already materialized inside the snapshot
        # checkout could resolve outside `root` (e.g. a tracked symlink
        # whose target is an absolute live-worktree path). Resolve it
        # against the checkout root and reject if the resolved path
        # escapes — this is the "symlink component known to resolve
        # outside the snapshot" rejection the escape-rejection contract
        # requires, distinct from the `..`-literal and absolute-path
        # checks above.
        candidate = os.path.join(root, token)
        resolved = os.path.realpath(candidate)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise InventoryError(
                "unsafe_argv_symlink_escape",
                "entry %r argv token %r resolves (via a symlink component) "
                "outside the snapshot checkout root %r" % (label, token, root))


# =========================================================================== #
# Section 3: immutable content-addressed snapshot builder.                    #
# =========================================================================== #


class SnapshotRaceError(RuntimeError):
    """The candidate source or git index changed during snapshot capture (or
    the copied snapshot disagrees with either pre- or post-copy enumeration).
    Carries `report` — the precise before/after diff — so the caller can
    surface it and abort before any worker/command launches."""

    def __init__(self, report):
        self.report = report
        super().__init__("snapshot race detected: %s"
                         % json.dumps(report, sort_keys=True)[:500])


def _git(args, cwd, timeout=30):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          timeout=timeout)


def git_repo_paths(repo):
    """Tracked + untracked-non-ignored relative paths, mirroring
    `cowork.py::_source_paths_for_manifest` exactly (same git invocation) so
    the transaction snapshot and the existing build-baseline manifest agree
    on what "source" means. Returns None on any git failure (fail closed —
    the caller must treat None as "cannot snapshot")."""
    try:
        listed = _git(["ls-files", "--cached", "--others",
                       "--exclude-standard"], repo)
        if listed.returncode != 0:
            return None
        return sorted({p for p in listed.stdout.decode(
            "utf-8", "replace").splitlines() if p.strip()})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def git_index_path(repo):
    """The exact `.git/index` (or worktree-specific gitdir index) path for
    `repo`, via `git rev-parse --git-dir` — never a hardcoded `.git/index`,
    which is wrong for a linked worktree."""
    try:
        res = _git(["rev-parse", "--git-dir"], repo)
        if res.returncode != 0:
            return None
        git_dir = res.stdout.decode("utf-8", "replace").strip()
        if not git_dir:
            return None
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(repo, git_dir)
        return os.path.join(git_dir, "index")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def read_index_bytes(repo):
    """Raw bytes of the git index, or None if absent/unreadable (a repo with
    no commits yet has no index file — treated as empty-but-present, `b""`,
    so its digest is still well-defined rather than None)."""
    path = git_index_path(repo)
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        if path and not os.path.exists(path):
            return b""
        return None


def index_digest(index_bytes):
    if index_bytes is None:
        return None
    return hashlib.sha256(index_bytes).hexdigest()


def _lstat_entry(full_path):
    """Classify one filesystem entry for snapshot capture. Returns a dict
    `{type, sha256, size, mode, symlink_target}` for a supported entry
    (`file` or `symlink`), or raises SnapshotRaceError-independent
    `_UnsupportedEntry` for devices/sockets/FIFOs/unreadable/missing paths —
    the snapshot builder fails closed on any of those rather than silently
    omitting them."""
    st = os.lstat(full_path)
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        target = os.readlink(full_path)
        return {"type": "symlink", "sha256": None, "size": None,
               "mode": None, "symlink_target": target}
    if stat.S_ISREG(mode):
        with open(full_path, "rb") as fh:
            raw = fh.read()
        executable = bool(mode & stat.S_IXUSR)
        return {"type": "file", "sha256": hashlib.sha256(raw).hexdigest(),
               "size": len(raw), "mode": "755" if executable else "644",
               "symlink_target": None, "_bytes": raw}
    raise _UnsupportedEntry(full_path, mode)


class _UnsupportedEntry(RuntimeError):
    def __init__(self, path, mode):
        self.path = path
        self.mode = mode
        super().__init__("unsupported filesystem entry type at %r (mode %o)"
                         % (path, mode))


def _enumerate_and_hash(repo, paths):
    """Build `{rel_path: entry_dict}` (without `_bytes`, stripped for the
    manifest) for every path, fail-closed on anything unsupported, escaping
    the repo, or unreadable. Returns `(manifest_entries, raw_bytes_by_path)`.
    """
    manifest = {}
    raw_by_path = {}
    real_repo = os.path.realpath(repo)
    for rel in paths:
        full = os.path.join(repo, rel)
        real_full = os.path.realpath(os.path.dirname(full))
        if real_full != real_repo and not real_full.startswith(
                real_repo + os.sep):
            raise SnapshotRaceError({
                "reason": "path_escapes_repo", "path": rel})
        try:
            entry = _lstat_entry(full)
        except (_UnsupportedEntry, OSError) as exc:
            raise SnapshotRaceError({
                "reason": "unsupported_or_unreadable_entry", "path": rel,
                "detail": str(exc)})
        raw = entry.pop("_bytes", None)
        manifest[rel] = entry
        if raw is not None:
            raw_by_path[rel] = raw
    return manifest, raw_by_path


def _manifest_fingerprint(manifest):
    """Order-independent digest over a snapshot manifest (path, type, sha256,
    mode, symlink_target), analogous to `cowork_state.manifest_digest` but
    covering type/mode/symlink identity too, since a snapshot must catch a
    file that turned into a symlink (or vice versa) between passes."""
    pairs = sorted(
        "%s:%s:%s:%s:%s" % (path, e.get("type"), e.get("sha256"),
                            e.get("mode"), e.get("symlink_target"))
        for path, e in manifest.items())
    return hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()


def build_snapshot(repo, session_uuid, transaction_id):
    """Capture an immutable, content-addressed snapshot of `repo`'s tracked +
    untracked-non-ignored source and its raw git index.

    Enumerates and hashes source+index BEFORE copying, copies regular-file
    bytes into the content-addressed object store while recording executable
    mode and symlink targets without following them, then RE-enumerates and
    re-hashes source+index AFTER the copy. Requires the pre-copy manifest
    fingerprint, the copied manifest fingerprint, and the post-copy manifest
    fingerprint to all be equal (and likewise for the index digest); any
    mismatch raises `SnapshotRaceError`, and the partial snapshot directory is
    deleted before the error propagates — nothing is left half-written for a
    later reader to trip over.

    Returns `{"manifest_digest", "index_digest", "manifest_path",
    "index_path"}` on success; also persists the manifest/index files via
    `cowork_state` at their deterministic per-transaction paths.
    """
    pre_paths = git_repo_paths(repo)
    if pre_paths is None:
        raise SnapshotRaceError({"reason": "git_ls_files_failed"})
    pre_index = read_index_bytes(repo)
    if pre_index is None:
        raise SnapshotRaceError({"reason": "git_index_unreadable"})
    pre_manifest, raw_by_path = _enumerate_and_hash(repo, pre_paths)
    pre_fingerprint = _manifest_fingerprint(pre_manifest)
    pre_index_digest = index_digest(pre_index)

    objects_dir = state_store.verification_snapshot_objects_dir(session_uuid)
    copied_manifest = {}
    try:
        for rel, entry in pre_manifest.items():
            copied_manifest[rel] = dict(entry)
            if entry["type"] == "file":
                obj_path = state_store.verification_snapshot_object_path(
                    session_uuid, entry["sha256"])
                if not os.path.exists(obj_path):
                    _write_object_atomic(obj_path, raw_by_path[rel])
                copied_sha = hashlib.sha256(raw_by_path[rel]).hexdigest()
                if copied_sha != entry["sha256"]:
                    raise SnapshotRaceError({
                        "reason": "copy_hash_mismatch", "path": rel,
                        "expected": entry["sha256"], "copied": copied_sha})

        copied_fingerprint = _manifest_fingerprint(copied_manifest)

        post_paths = git_repo_paths(repo)
        post_index = read_index_bytes(repo)
        if post_paths is None or post_index is None:
            raise SnapshotRaceError({"reason": "git_unreadable_post_copy"})
        post_manifest, _ = _enumerate_and_hash(repo, post_paths)
        post_fingerprint = _manifest_fingerprint(post_manifest)
        post_index_digest = index_digest(post_index)

        if not (pre_fingerprint == copied_fingerprint == post_fingerprint):
            raise SnapshotRaceError({
                "reason": "manifest_race",
                "pre": pre_fingerprint, "copied": copied_fingerprint,
                "post": post_fingerprint,
                "pre_paths": sorted(pre_paths),
                "post_paths": sorted(post_paths)})
        if not (pre_index_digest == post_index_digest):
            raise SnapshotRaceError({
                "reason": "index_race",
                "pre_index_digest": pre_index_digest,
                "post_index_digest": post_index_digest})
    except SnapshotRaceError:
        _delete_partial_snapshot(session_uuid, transaction_id)
        raise

    manifest_doc = {
        "generated_at": _utc_now(), "repo": repo,
        "manifest_digest": pre_fingerprint, "files": copied_manifest,
    }
    manifest_path = state_store.verification_snapshot_manifest_path_for(
        session_uuid, transaction_id)
    state_store.write_json_atomic(manifest_path, manifest_doc)
    index_path = state_store.verification_snapshot_index_path_for(
        session_uuid, transaction_id)
    _write_raw_atomic(index_path, pre_index)

    return {
        "manifest_digest": pre_fingerprint,
        "index_digest": pre_index_digest,
        "manifest_path": manifest_path,
        "index_path": index_path,
    }


def _write_object_atomic(path, raw_bytes):
    dirname = os.path.dirname(path)
    os.makedirs(dirname, exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(raw_bytes)
    os.replace(tmp, path)


def _write_raw_atomic(path, raw_bytes):
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(raw_bytes)
    os.replace(tmp, path)


def _delete_partial_snapshot(session_uuid, transaction_id):
    import shutil
    for path in (
        state_store.verification_snapshot_manifest_path_for(
            session_uuid, transaction_id),
        state_store.verification_snapshot_index_path_for(
            session_uuid, transaction_id),
        state_store.verification_snapshot_checkout_dir(
            session_uuid, transaction_id),
    ):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _materialize_files_from_manifest(session_uuid, manifest_files, dest_root):
    """Copy every manifest entry's bytes (regular files) or recreate its
    symlink into `dest_root`, restoring executable mode — a FRESH, real
    byte-for-byte copy out of the content-addressed object store every time
    this is called, never a hard link, so two callers (or two calls for two
    different commands) never share inode state that one could mutate out
    from under the other."""
    os.makedirs(dest_root, exist_ok=True)
    for rel, entry in manifest_files.items():
        dest = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if entry["type"] == "symlink":
            if os.path.islink(dest) or os.path.exists(dest):
                os.remove(dest)
            os.symlink(entry["symlink_target"], dest)
            continue
        obj_path = state_store.verification_snapshot_object_path(
            session_uuid, entry["sha256"])
        with open(obj_path, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
        mode = 0o755 if entry.get("mode") == "755" else 0o644
        os.chmod(dest, mode)


def materialize_checkout(session_uuid, transaction_id):
    """Build the BOOTSTRAP checkout from a captured snapshot manifest +
    content-addressed objects: this is ONLY where the worker process itself
    is spawned from (`spawn_worker`) — never where an isolated_snapshot
    COMMAND's cwd resolves (see `materialize_command_checkout` for that;
    each command gets its own fresh, disposable checkout so one command's
    output/mutations can never leak into another's, or into the worker code
    driving later commands).

    This is argv/cwd isolation, NOT an access-control boundary: the
    orchestrator never selects this path as a command's cwd, and static
    argv validation rejects any LITERAL argument that names or escapes into
    it before launch — but that check inspects argv tokens, not what a
    command's own inline logic does at runtime. A command whose source
    itself computes or discovers this path (the same class of gap as a
    command that mutates the live candidate via inline code rather than a
    literal path argument) is not stopped by argv validation, only by
    fail-closed mutation detection catching the RESULT. So: excluded from
    the normal/expected command-input path, not "inaccessible," and
    certainly not filesystem-enforced read-only — its files are NOT
    chmod'd read-only at the OS level, since a later cleanup pass must
    still be able to remove the directory. Symlinks are recreated as
    symlinks (target recorded, never followed at capture time); executable
    mode is restored. Returns the checkout root path.
    """
    manifest_doc = state_store.read_json_tolerant(
        state_store.verification_snapshot_manifest_path_for(
            session_uuid, transaction_id))
    if not manifest_doc:
        raise SnapshotRaceError({"reason": "manifest_missing_at_materialize"})
    checkout_root = state_store.verification_snapshot_checkout_dir(
        session_uuid, transaction_id)
    _materialize_files_from_manifest(
        session_uuid, manifest_doc.get("files", {}), checkout_root)
    return checkout_root


def materialize_command_checkout(session_uuid, transaction_id, index):
    """Materialize a FRESH, writable, per-command checkout for exactly one
    `isolated_snapshot` command: a real directory tree freshly copied (never
    hard-linked) from the frozen snapshot manifest/objects, at
    `verification_command_checkout_dir(session_uuid, transaction_id, index)`
    — a location distinct from every other command's checkout AND from the
    bootstrap checkout (excluded from the normal command-input path, but
    not an access-control boundary — see `materialize_checkout`) — then
    given FUNCTIONAL LOCAL GIT/INDEX
    SEMANTICS of its own: `git init` plus the transaction's own captured raw
    index bytes written directly to `.git/index`, so `git rev-parse
    --show-toplevel`, `git ls-files`, and any tracked-vs-untracked detection
    a command runs work correctly, entirely self-contained — never by
    reading, cloning from, or otherwise consulting the live candidate
    repository. Returns the checkout root path.

    The caller is responsible for removing this checkout after the command's
    terminal event is recorded — this function only builds it.
    """
    manifest_doc = state_store.read_json_tolerant(
        state_store.verification_snapshot_manifest_path_for(
            session_uuid, transaction_id))
    if not manifest_doc:
        raise SnapshotRaceError({"reason": "manifest_missing_at_materialize"})
    checkout_root = state_store.verification_command_checkout_dir(
        session_uuid, transaction_id, index)
    if os.path.exists(checkout_root):
        # Defensive: an index is never reused within one transaction, but a
        # crashed prior attempt at the same index must not silently merge
        # its leftovers into this fresh materialization.
        shutil.rmtree(checkout_root)
    _materialize_files_from_manifest(
        session_uuid, manifest_doc.get("files", {}), checkout_root)

    subprocess.run(["git", "init", "-q"], cwd=checkout_root, check=True)
    subprocess.run(["git", "config", "user.email", "verification@cowork.local"],
                   cwd=checkout_root, check=True)
    subprocess.run(["git", "config", "user.name", "cowork-verification"],
                   cwd=checkout_root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"],
                   cwd=checkout_root, check=True)
    index_src_path = state_store.verification_snapshot_index_path_for(
        session_uuid, transaction_id)
    if os.path.exists(index_src_path):
        with open(index_src_path, "rb") as src:
            index_bytes = src.read()
        with open(os.path.join(checkout_root, ".git", "index"), "wb") as dst:
            dst.write(index_bytes)
    return checkout_root


def remove_command_checkout(session_uuid, transaction_id, index):
    """Remove exactly one command's per-command checkout after its terminal
    event is recorded. Best-effort: a removal failure is not itself a
    transaction-invalidating condition (the checkout is disposable scratch
    space, not evidence), but it is never silently retried into a later
    command's materialization — `materialize_command_checkout` always starts
    from a clean directory regardless."""
    checkout_root = state_store.verification_command_checkout_dir(
        session_uuid, transaction_id, index)
    shutil.rmtree(checkout_root, ignore_errors=True)


# =========================================================================== #
# Section 4: single-flight lock (fcntl.flock, POSIX).                        #
# =========================================================================== #


class LockTimeoutError(RuntimeError):
    """A waiter exceeded its bounded deadline without acquiring the lock or
    finding a terminal matching result."""


def _try_flock_exclusive_nonblocking(path):
    """Try a non-blocking exclusive `fcntl.flock` on `path`. Returns the open
    file descriptor, STILL LOCKED, on success — the caller owns it and MUST
    eventually pass it to `_release_flock` (there is no automatic release
    here, unlike a context manager, because the whole point of this helper
    is to let the caller hold the OS-level lock across a long-running
    operation, not just around the instant of acquisition). Returns `None`
    on `EWOULDBLOCK`/`EAGAIN` (someone else holds it), in which case the fd
    opened for the attempt is already closed before returning.
    """
    import fcntl
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None
        raise
    return fd


def _release_flock(fd):
    """Unlock and close a fd returned by `_try_flock_exclusive_nonblocking`.
    Safe to call with `None` (no-op) so callers can release unconditionally
    in a `finally` regardless of which path acquired (or didn't acquire) the
    lock."""
    if fd is None:
        return
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def acquire_single_flight(session_uuid, request_key, transaction_id,
                          waiter_deadline_s=None, poll_delay_s=0.5,
                          sleep=time.sleep, now=time.time):
    """Acquire the kernel-level single-flight lock for `request_key`, or wait
    for an equivalent in-flight transaction to reach a terminal state.

    Returns one of:
      - `("acquired", None, lock_fd)` — caller owns the OS-level flock AND
        must run a fresh transaction. The fd is returned STILL LOCKED —
        the caller MUST hold it open for the transaction's ENTIRE
        execution (minting, spawning, running every command, persisting
        the terminal result) and only release it via `release_single_
        flight` once the terminal result has actually been published.
        Releasing any earlier — as a prior version of this function did,
        by using a context manager that unlocked the instant this
        function returned — left NO real OS-level exclusion during the
        transaction itself: a second, genuinely overlapping caller for the
        same `request_key` could acquire the free flock and start a
        DUPLICATE transaction while the first was still running, with only
        an unenforced `state: running` marker in the `.meta` file standing
        between them (and the abandonment branch below would even
        overwrite that marker without checking whether the recorded owner
        was actually alive, since the real lock was never actually held
        by it). Owner metadata `{pid, start_time, transaction_id}` is
        already published before this returns.
      - `("reuse", result_dict, None)` — an equivalent transaction already
        reached a TERMINAL result for this exact key; the caller must not
        run anything and there is no fd to release.
      - raises `LockTimeoutError` if the bounded waiter deadline elapses
        without either of the above.

    Owner-death recovery: if flock cannot be acquired (someone holds it) AND
    the metadata file's recorded owner pid is dead, this reclaims the lock —
    marking any non-terminal prior transaction record `abandoned` — rather
    than trusting stale metadata forever. A live owner is waited on normally.
    Because the flock is now genuinely held for the owner's whole run, this
    recovery path is only reachable when the owner's PROCESS actually died
    (the kernel then frees the flock automatically) — never merely because
    the owner happened to return from this function.
    """
    lock_path = state_store.verification_lock_path_for(
        session_uuid, request_key)
    deadline = now() + (waiter_deadline_s or DEFAULT_LOCK_WAITER_DEADLINE_S)
    while True:
        fd = _try_flock_exclusive_nonblocking(lock_path)
        if fd is not None:
            owner = state_store.read_json_tolerant(lock_path + ".meta")
            if (isinstance(owner, dict)
                    and owner.get("request_key") == request_key
                    and owner.get("state") == "terminal"
                    and owner.get("result")):
                _release_flock(fd)
                return "reuse", owner["result"], None
            if (isinstance(owner, dict)
                    and owner.get("state") not in (None, "terminal")):
                owner = dict(owner)
                owner["state"] = "abandoned"
                state_store.write_json_atomic(lock_path + ".meta", owner)
            state_store.write_json_atomic(lock_path + ".meta", {
                "pid": os.getpid(), "start_time": now(),
                "transaction_id": transaction_id,
                "request_key": request_key, "state": "running",
            })
            return "acquired", None, fd
        owner = state_store.read_json_tolerant(lock_path + ".meta")
        if isinstance(owner, dict) and owner.get("request_key") == request_key:
            if owner.get("state") == "terminal" and owner.get("result"):
                return "reuse", owner["result"], None
            if owner.get("state") == "running" and not _pid_alive(
                    owner.get("pid")):
                # Dead owner: loop again immediately to attempt reclaim via a
                # fresh flock (the kernel already freed it on process exit).
                continue
        if now() >= deadline:
            raise LockTimeoutError(
                "single-flight waiter deadline exceeded for request_key=%s"
                % request_key)
        sleep(poll_delay_s)


def release_single_flight(lock_fd):
    """Release the OS-level flock returned by `acquire_single_flight`'s
    `("acquired", None, lock_fd)` result. Callers must invoke this exactly
    once, after the transaction has reached a terminal state and that
    result has been persisted/published — never earlier. Safe to call with
    `None` (the `("reuse", ...)` case has nothing to release)."""
    _release_flock(lock_fd)


def _persist_terminal_result(session_uuid, transaction_id, request_key,
                             result):
    """Durably write `result` to its own advertised, session-relative
    `result.json` path, then publish it to the single-flight lock for
    waiter reuse — but ONLY the result actually confirmed durable. A caller
    or a lock waiter reusing a `verdict: green` result is trusting that the
    file at `verification_result_path_for(...)` truly exists with that
    content; if the write itself reports failure (`write_json_atomic`
    returns False rather than raising), publishing the original result
    anyway — green or not — would hand out a claim the disk cannot back up.
    Fails closed: on a failed write, the IN-MEMORY result is downgraded to
    `unverified` (never reused as green) before it is published to the
    lock or returned to the caller, and `result_persistence_failed: True`
    is attached so the honest reason is visible on the object itself, not
    just inferred from a missing file."""
    result_path = state_store.verification_result_path_for(
        session_uuid, transaction_id)
    persisted = state_store.write_json_atomic(result_path, result)
    if not persisted:
        result = TransactionResult(dict(
            result, verdict=VERDICT_UNVERIFIED,
            result_persistence_failed=True))
    publish_terminal_lock_result(session_uuid, request_key, result)
    return result


def publish_terminal_lock_result(session_uuid, request_key, result):
    """Mark the owned lock's metadata terminal with `result`, so a waiter that
    polls next reuses it instead of racing a new transaction. Best-effort:
    failure here does not affect the transaction's own result, only whether a
    concurrent waiter can reuse it."""
    lock_path = state_store.verification_lock_path_for(
        session_uuid, request_key)
    meta = state_store.read_json_tolerant(lock_path + ".meta") or {}
    meta["state"] = "terminal"
    meta["result"] = result
    state_store.write_json_atomic(lock_path + ".meta", meta)


# =========================================================================== #
# Section 5: process-group execution primitives (serial, owned, POSIX).       #
# =========================================================================== #


class ProcessGroupTimeout(RuntimeError):
    """A command's process group did not terminate within TERM grace and had
    to be escalated to KILL."""


def _pgid_alive(pgid):
    """Whether any process remains in process group `pgid`. Uses
    `os.killpg(pgid, 0)` — no third-party/psutil dependency — which raises
    `ProcessLookupError` once the group is empty on POSIX."""
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def run_command_in_group(argv, cwd, timeout_s, term_grace_s,
                         output_cap_bytes, env=None,
                         on_start=None, liveness_should_stop=None):
    """Run one command in its own process group, DEVNULL stdin, bounded
    output capture, TERM-then-KILL timeout escalation, and post-terminate
    descendant verification.

    `on_start(pgid)` is called the instant the child's pgid is known (before
    waiting on output), so the caller can atomically publish it for
    liveness-driven external cleanup. `liveness_should_stop()` is polled
    periodically; when it returns True the command is terminated exactly as
    on timeout (used by the worker's liveness watchdog to kill the active
    command on parent-pipe EOF).

    Returns a dict: `{exit_code, timed_out, term_sent, kill_sent,
    descendants_confirmed_gone, stdout, stderr, stdout_truncated,
    stderr_truncated, started_at, ended_at, wall_time_s}`.
    """
    started = time.time()
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True, env=env)
    for stream in (proc.stdout, proc.stderr):
        os.set_blocking(stream.fileno(), False)
    pgid = os.getpgid(proc.pid)
    if on_start:
        on_start(pgid)

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    timed_out = False
    term_sent = False
    kill_sent = False
    liveness_stopped = False

    deadline = started + timeout_s
    open_streams = 2
    while open_streams > 0:
        remaining = deadline - time.time()
        if remaining <= 0:
            timed_out = True
            break
        if liveness_should_stop and liveness_should_stop():
            liveness_stopped = True
            break
        for key, _ in sel.select(timeout=min(0.25, max(0.0, remaining))):
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            name = key.data
            if not chunk:
                sel.unregister(key.fileobj)
                open_streams -= 1
                continue
            if len(buffers[name]) < output_cap_bytes:
                room = output_cap_bytes - len(buffers[name])
                buffers[name].extend(chunk[:room])
                if len(chunk) > room:
                    truncated[name] = True
            else:
                truncated[name] = True
        if proc.poll() is not None and open_streams == 0:
            break
    sel.close()

    if not timed_out and not liveness_stopped:
        try:
            proc.wait(timeout=max(0.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            timed_out = True

    if timed_out or liveness_stopped:
        term_sent = True
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        grace_deadline = time.time() + term_grace_s
        while time.time() < grace_deadline and _pgid_alive(pgid):
            time.sleep(0.1)
        if _pgid_alive(pgid):
            kill_sent = True
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            kill_deadline = time.time() + term_grace_s
            while time.time() < kill_deadline and _pgid_alive(pgid):
                time.sleep(0.1)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    exit_code = proc.returncode

    # Drain anything still buffered in the OS pipes without waiting for a
    # detached descendant that inherited one of the write ends.
    for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        try:
            if stream and not stream.closed:
                while True:
                    try:
                        rest = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not rest:
                        break
                    if len(buffers[name]) < output_cap_bytes:
                        room = output_cap_bytes - len(buffers[name])
                        buffers[name].extend(rest[:room])
                        if len(rest) > room:
                            truncated[name] = True
                    else:
                        truncated[name] = True
        except (OSError, ValueError):
            pass
        finally:
            if stream and not stream.closed:
                stream.close()

    descendants_confirmed_gone = not _pgid_alive(pgid)
    ended = time.time()
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "liveness_stopped": liveness_stopped,
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "descendants_confirmed_gone": descendants_confirmed_gone,
        "pgid": pgid,
        "stdout": bytes(buffers["stdout"]).decode("utf-8", "replace"),
        "stderr": bytes(buffers["stderr"]).decode("utf-8", "replace"),
        "stdout_truncated": truncated["stdout"],
        "stderr_truncated": truncated["stderr"],
        "started_at": datetime.datetime.fromtimestamp(
            started, datetime.timezone.utc).isoformat(),
        "ended_at": datetime.datetime.fromtimestamp(
            ended, datetime.timezone.utc).isoformat(),
        "wall_time_s": ended - started,
    }


# =========================================================================== #
# Section 6: mutation detection (fail-closed).                                #
# =========================================================================== #


def detect_mutation(repo, expected_manifest_digest, expected_index_digest,
                    expected_manifest=None):
    """Compare the LIVE candidate's current source manifest + git-index
    digest against the values captured at snapshot time. Returns None when
    nothing changed, or a precise mutation report dict `{changed_paths,
    manifest_digest_before, manifest_digest_after, index_digest_before,
    index_digest_after}` otherwise. Fail-closed: any git failure while
    checking is itself treated as a mutation (`reason: "git_unreadable"`)
    rather than silently passing.

    `expected_manifest`, when given (the snapshot's `{path: entry}` dict),
    enables precise per-path `changed_paths` reporting (added/removed/
    content-changed); without it, only the digests and an empty
    `changed_paths` list are reported.
    """
    paths = git_repo_paths(repo)
    if paths is None:
        return {"reason": "git_unreadable",
               "manifest_digest_before": expected_manifest_digest,
               "manifest_digest_after": None, "changed_paths": []}
    try:
        manifest, _ = _enumerate_and_hash(repo, paths)
    except SnapshotRaceError as exc:
        return {"reason": "enumeration_failed", "detail": exc.report,
               "manifest_digest_before": expected_manifest_digest,
               "manifest_digest_after": None, "changed_paths": []}
    current_digest = _manifest_fingerprint(manifest)
    current_index = index_digest(read_index_bytes(repo))
    if (current_digest == expected_manifest_digest
            and current_index == expected_index_digest):
        return None
    changed_paths = []
    if isinstance(expected_manifest, dict):
        before_keys = set(expected_manifest.keys())
        after_keys = set(manifest.keys())
        changed_paths.extend(sorted(after_keys - before_keys))
        changed_paths.extend(sorted(before_keys - after_keys))
        for rel in sorted(before_keys & after_keys):
            if expected_manifest[rel] != manifest[rel]:
                changed_paths.append(rel)
        changed_paths = sorted(set(changed_paths))
    return {
        "reason": "source_or_index_mutated",
        "manifest_digest_before": expected_manifest_digest,
        "manifest_digest_after": current_digest,
        "index_digest_before": expected_index_digest,
        "index_digest_after": current_index,
        "changed_paths": changed_paths,
    }


def current_candidate_identity(repo):
    """The LIVE candidate's manifest/index digest pair, computed with the
    EXACT SAME canonical algorithm `build_snapshot`/`detect_mutation` use
    (`git_repo_paths` + `_enumerate_and_hash` + `_manifest_fingerprint` for
    the manifest; `read_index_bytes` + `index_digest` for the index).

    This is the ONE canonical identity a caller outside this module (e.g.
    `cowork.py`'s readiness gate) must use to compare "the candidate as it
    is right now" against a `TransactionResult`'s own `snapshot.
    manifest_digest`/`snapshot.index_digest` — comparing either of those
    against a digest computed by a DIFFERENT algorithm (such as `cowork_
    state.manifest_digest`) is comparing two different identity schemes
    and will disagree even when nothing moved, which is exactly what let a
    green transaction bind readiness to a candidate that had not actually
    been re-checked against it. Returns `(None, None)` when the repo's git
    state cannot be read (fail-closed: a caller must treat `None` as "not
    equal to anything", never skip the comparison).
    """
    paths = git_repo_paths(repo)
    if paths is None:
        return None, None
    try:
        manifest, _ = _enumerate_and_hash(repo, paths)
    except SnapshotRaceError:
        return None, None
    index_bytes = read_index_bytes(repo)
    if index_bytes is None:
        return None, None
    return _manifest_fingerprint(manifest), index_digest(index_bytes)


# =========================================================================== #
# Section 7: worker self-identity.                                            #
# =========================================================================== #


def self_source_hash():
    """SHA-256 of this module's OWN file bytes, computed from `__file__`. The
    worker reports this (plus `PROTOCOL_VERSION`) before running any command;
    the parent requires equality with the snapshot manifest's entry for this
    file's path, or the transaction is UNVERIFIED. This is what lets a
    version-A parent trust that a version-B worker really is the exact code
    the snapshot captured, not a substituted or subsequently-edited file."""
    with open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# =========================================================================== #
# Section 8: worker main loop (runs INSIDE the spawned snapshot subprocess).  #
# =========================================================================== #


def _worker_liveness_watchdog(pipe_read_fd, stop_flag, should_stop_flag):
    """Background thread: block on reading `pipe_read_fd`. The parent closes
    its write end on shutdown/cancel, which delivers EOF (a zero-length read)
    here; the parent process dying outright has the same effect (the OS
    closes its fds). Either way this thread sets `should_stop_flag` so the
    active command's process group is torn down and the worker exits instead
    of running unattended forever."""
    try:
        while not stop_flag["stop"]:
            chunk = os.read(pipe_read_fd, 1)
            if chunk == b"":
                should_stop_flag["stop"] = True
                return
    except OSError:
        should_stop_flag["stop"] = True


DEFAULT_PERMIT_WAIT_S = 600  # backstop only — `should_stop` (liveness) is
                             # the real bound; this exists purely so a
                             # worker can never wait literally forever if
                             # the liveness pipe itself somehow never
                             # delivers EOF.


def _wait_for_permit(session_uuid, transaction_id, index, ledger_attempt_id,
                     should_stop, timeout_s=DEFAULT_PERMIT_WAIT_S,
                     poll_delay_s=0.05, sleep=time.sleep, now=time.time):
    """Block until the PARENT issues a permit naming EXACTLY this
    `(transaction_id, index, ledger_attempt_id)` — the worker must never
    start an entry the parent has not explicitly authorized. This is what
    closes the run-ahead gap: without it, the worker (running its own copy
    of the inventory as fast as it can) could start — or finish — entry
    N+1 before the parent had even discovered a ledger failure revising
    entry N, so a later backstop pass would wrongly mark N+1 `not_reached`
    when it had actually run.

    Checked on every poll iteration: `should_stop` (the liveness-watchdog
    flag — a parent that closes the liveness pipe, whether for ordinary
    shutdown/cancel or because it chose not to issue the next permit after
    a ledger failure, unblocks this wait) AND a bounded fallback timeout
    as an independent backstop. Returns True once the matching permit is
    observed, False on should_stop or timeout — the caller must NOT run
    the command in the False case.
    """
    permit_path = state_store.verification_permit_path_for(
        session_uuid, transaction_id)
    deadline = now() + timeout_s
    while now() < deadline:
        if should_stop["stop"]:
            return False
        permit = state_store.read_json_tolerant(permit_path)
        if (isinstance(permit, dict)
                and permit.get("transaction_id") == transaction_id
                and permit.get("index") == index
                and permit.get("ledger_attempt_id") == ledger_attempt_id):
            return True
        sleep(poll_delay_s)
    return False


def worker_main(request_path, liveness_fd=None):
    """The worker process's entire program: read the request, report self-
    identity, run the approved inventory serially inside the materialized
    snapshot checkout, and write terminal attempt events + nothing else. Never
    imports or touches the live candidate path — only the snapshot checkout
    it was spawned from.

    Exit codes: 0 on a completed run (individual command failures are still
    reported as `exit_code != 0` attempt events, not a nonzero worker exit);
    2 on a malformed/unreadable request (nothing could be attempted).
    """
    request = state_store.read_json_tolerant(request_path)
    if not isinstance(request, dict):
        sys.stderr.write("cowork_verification worker: unreadable request "
                         "at %r\n" % request_path)
        return 2

    session_uuid = request.get("session_uuid")
    transaction_id = request.get("transaction_id")
    events_path = state_store.verification_attempt_events_path_for(
        session_uuid, transaction_id)
    inventory = request.get("inventory") or []

    # REQUEST VALIDATION IS BOUND TO WORKER ACCEPTANCE: this runs BEFORE
    # identity is ever published, not after. Publishing a valid identity
    # first (the earlier bug) told the parent "trust what follows" before
    # the request had actually been accepted — a malformed/protocol-1
    # request then still passed `verify_worker_identity`, so the parent
    # proceeded into the per-entry loop and waited a FULL command-execution
    # budget (potentially minutes) for evidence a rejected request could
    # never produce. Now: no identity file is written at all on rejection,
    # so `verify_worker_identity` sees no report (fails the SAME way a
    # crashed worker does) and — combined with the ORCH-030 `proc.poll()`
    # fast-exit detection in `_read_worker_identity` — the parent recognizes
    # this promptly instead of waiting out the startup allowance, let alone
    # a per-command evidence budget.
    # Explicit PROTOCOL VERSION check — presence of `ledger_attempt_id` is
    # NOT sufficient on its own: a request could carry a `ledger_attempt_id`
    # on every entry (protocol 2's field) while still declaring a stale
    # `protocol_version` if `build_request` itself came from a mismatched
    # caller, or a future protocol bump changes shape in some OTHER way
    # this worker does not know about. Reject on shape, not just on one
    # named field's presence, before identity is ever published.
    if request.get("protocol_version") != PROTOCOL_VERSION:
        state_store.append_jsonl_atomic(events_path, {
            "event": "terminal", "attempt_id": None, "label": None,
            "evidence_state": EVIDENCE_ABSENT,
            "note": "request_rejected_protocol_version_mismatch",
            "requested_protocol_version": request.get("protocol_version"),
            "worker_protocol_version": PROTOCOL_VERSION, "at": _utc_now()})
        return WORKER_EXIT_REQUEST_REJECTED

    missing_id_labels = [e.get("label") for e in inventory
                         if not e.get("ledger_attempt_id")]
    if missing_id_labels:
        state_store.append_jsonl_atomic(events_path, {
            "event": "terminal", "attempt_id": None, "label": None,
            "evidence_state": EVIDENCE_ABSENT,
            "note": "request_rejected_missing_ledger_attempt_id",
            "labels": missing_id_labels, "at": _utc_now()})
        return WORKER_EXIT_REQUEST_REJECTED

    identity_path = state_store.verification_worker_identity_path_for(
        session_uuid, transaction_id)
    state_store.write_json_atomic(identity_path, {
        "source_hash": self_source_hash(),
        "protocol_version": PROTOCOL_VERSION,
        "pid": os.getpid(),
        "reported_at": _utc_now(),
    })

    active_pgid_path = state_store.verification_active_pgid_path_for(
        session_uuid, transaction_id)

    stop_flag = {"stop": False}
    should_stop = {"stop": False}
    watchdog = None
    if liveness_fd is not None:
        watchdog = threading.Thread(
            target=_worker_liveness_watchdog,
            args=(liveness_fd, stop_flag, should_stop), daemon=True)
        watchdog.start()

    timeout_policy = request.get("timeout_policy") or {}
    command_timeout_s = (timeout_policy.get("command_timeout_s")
                         or DEFAULT_COMMAND_TIMEOUT_S)
    term_grace_s = timeout_policy.get("term_grace_s") or DEFAULT_TERM_GRACE_S
    output_cap_bytes = request.get("output_cap_bytes") or DEFAULT_OUTPUT_CAP_BYTES

    repo = request.get("repo")
    snapshot_meta = request.get("snapshot") or {}
    expected_manifest_digest = snapshot_meta.get("manifest_digest")
    expected_index_digest = snapshot_meta.get("index_digest")
    mutated = False
    # STICKY PERMIT LOSS (blocker79). This is a FAIL-CLOSED POLICY, not a
    # proof: once one entry's permit wait has timed out, the worker refuses
    # every remaining entry rather than re-waiting for each. A later permit
    # is NOT provably impossible -- the parent normally issues entry N+1's
    # permit only after entry N terminalized on its side, but a permit that
    # was merely late, or a parent on an unexpected path, could still write
    # one. Refusing it is the safe direction: the cost of re-waiting is a
    # full `DEFAULT_PERMIT_WAIT_S` backstop per remaining entry for
    # authorization that has already been observed not to arrive in time,
    # and a permit that arrives after its own wait already expired is by
    # definition no longer timely. Same `mutated` idiom directly above: the
    # durable `skipped_no_permit` record for every remaining entry is still
    # written, one per entry, exactly as before -- only the re-waiting is
    # dropped. This never runs a command without a timely matching permit;
    # it can only ever skip more promptly.
    permit_lost = False

    for index, entry in enumerate(inventory):
        if should_stop["stop"]:
            state_store.append_jsonl_atomic(events_path, {
                "event": "skipped_liveness_lost", "label": entry.get("label"),
                "at": _utc_now()})
            continue
        if mutated:
            # A prior command in THIS worker mutated the live candidate's
            # source or index; the worker races ahead of the parent's own
            # (necessarily asynchronous) mutation polling, so it must stop
            # itself here rather than rely solely on the parent to catch it
            # — otherwise "fail closed on mutation, run nothing further"
            # only holds when the parent happens to be faster than the
            # worker, which it usually is not.
            state_store.append_jsonl_atomic(events_path, {
                "event": "skipped_mutation_detected",
                "label": entry.get("label"), "at": _utc_now()})
            continue
        if repo and expected_manifest_digest and expected_index_digest:
            pre_mutation = detect_mutation(
                repo, expected_manifest_digest, expected_index_digest)
            if pre_mutation is not None:
                mutated = True
                state_store.append_jsonl_atomic(events_path, {
                    "event": "skipped_mutation_detected",
                    "label": entry.get("label"), "at": _utc_now()})
                continue
        # The pre-minted ledger `V-xxxx` id (see `run_transaction`, which
        # minted one per entry before this worker was ever spawned and
        # embedded it in the immutable request) IS this attempt's identity.
        # The worker never invents its own — the upfront check above
        # already refused to run anything if any entry lacked one, so this
        # is always present here.
        attempt_id = entry.get("ledger_attempt_id")
        # WAIT FOR THE PARENT'S PERMIT before starting this entry. The
        # parent only issues it after this entry's OWN "running" ledger
        # revision has already durably succeeded, and only issues the
        # NEXT one after this entry's terminal/unresolved revision has
        # also durably succeeded — so a failure the parent discovers
        # while revising entry N's ledger state genuinely stops entry
        # N+1 from ever launching, closing the run-ahead gap.
        if permit_lost or not _wait_for_permit(
                session_uuid, transaction_id, index, attempt_id,
                should_stop):
            permit_lost = True
            state_store.append_jsonl_atomic(events_path, {
                "event": "skipped_no_permit", "label": entry.get("label"),
                "attempt_id": attempt_id, "at": _utc_now()})
            continue
        is_isolated = entry.get("execution_mode") == "isolated_snapshot"
        command_checkout = None
        if is_isolated:
            # A FRESH, disposable, per-command checkout — never the shared
            # bootstrap checkout, never reused from an earlier command — with
            # its own functional local Git/index, materialized fresh from the
            # frozen snapshot for exactly this one command.
            command_checkout = materialize_command_checkout(
                session_uuid, transaction_id, index)
            cwd = command_checkout
        else:
            cwd = request.get("repo")
        state_store.append_jsonl_atomic(events_path, {
            "event": "start", "attempt_id": attempt_id,
            "label": entry.get("label"), "command": entry.get("command"),
            "execution_mode": entry.get("execution_mode"),
            "kind": entry.get("kind"), "cwd": cwd, "at": _utc_now()})

        def _publish_pgid(pgid, _label=entry.get("label")):
            state_store.write_json_atomic(active_pgid_path, {
                "pgid": pgid, "label": _label, "started_at": _utc_now()})

        try:
            result = run_command_in_group(
                entry["command"], cwd=cwd, timeout_s=command_timeout_s,
                term_grace_s=term_grace_s, output_cap_bytes=output_cap_bytes,
                on_start=_publish_pgid,
                liveness_should_stop=lambda: should_stop["stop"])
        finally:
            if is_isolated:
                # Removed the INSTANT this command's run is over — before
                # the next command's materialization, before its own
                # terminal event is even written — so nothing it left
                # behind (a generated file, an ignored artifact, a git
                # object) can ever be observed by a later command, and a
                # crash mid-command still leaves no residue for the next
                # index to trip over.
                remove_command_checkout(session_uuid, transaction_id, index)

        try:
            os.remove(active_pgid_path)
        except OSError:
            pass

        event = {"event": "terminal", "attempt_id": attempt_id,
                 "label": entry.get("label"), "at": _utc_now()}
        event.update(result)
        state_store.append_jsonl_atomic(events_path, event)

        if should_stop["stop"]:
            break
        # A command that ran the SNAPSHOT copy could still have mutated the
        # LIVE candidate (a test with an absolute live-path bug, or a
        # deliberately hostile command the argv validator's syntactic checks
        # cannot see through) — check again immediately after every
        # command, not just before the next one, so the worker itself never
        # begins another command once the candidate has moved.
        if repo and expected_manifest_digest and expected_index_digest:
            post_mutation = detect_mutation(
                repo, expected_manifest_digest, expected_index_digest)
            if post_mutation is not None:
                mutated = True
                continue
        if result.get("exit_code") not in (0, None) or result.get("timed_out"):
            # A failing/timed-out command stops the rest of THIS worker's
            # inventory too — the parent's own stop-on-failure loop only
            # gates evidence it has not yet polled; without this the worker
            # would already have executed every later command (including a
            # kind=final_suite entry) before the parent ever notices the
            # earlier failure.
            break

    stop_flag["stop"] = True
    return 0


# =========================================================================== #
# Section 9: parent-side worker lifecycle, evidence polling, orchestration.   #
# =========================================================================== #


class TransactionResult(dict):
    """A terminal, immutable verification transaction result. Plain dict
    subclass (so it stays trivially JSON-serializable) with documented shape:

        {
          "transaction_id", "request_key", "verdict": green|red|unverified,
          "final_suite_label", "final_suite_binding": ran_once|legacy_unknown|
              not_reached,
          "attempts": [{"label", "attempt_id", "exit_code", "timed_out",
                        "evidence_state", ...per-command fields}],
          "mutation": None | {mutation report},
          "worker_identity_verified": bool,
          "reused_lock_result": bool,
          "created_at", "finished_at",
        }
    """


# `_read_worker_identity`, `verify_worker_identity`, `_poll_attempt_events`,
# `MAX_STARTUP_LOG_BYTES`, `_capture_startup_log`, `spawn_worker`, and
# `_read_worker_startup_log` were relocated to `cowork_verification_worker.py`
# (M5 Package A worker_capture_seam) and `cowork_verification_evidence.py`
# and are re-exported above by their exact bare names -- see the
# "Extraction seam re-exports" block near the top of this file.

def terminate_worker(proc, liveness_write_fd, term_grace_s=DEFAULT_TERM_GRACE_S):
    """Tear down a spawned worker: close the liveness pipe write end (EOF ->
    the worker's watchdog kills its active command and the worker exits on
    its own), then TERM/KILL the worker's own process group if it has not
    exited within the grace period, and reap it. Idempotent-safe to call more
    than once."""
    try:
        os.close(liveness_write_fd)
    except OSError:
        pass
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    deadline = time.time() + term_grace_s
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        term_deadline = time.time() + term_grace_s
        while time.time() < term_deadline and proc.poll() is None:
            time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def cleanup_active_command_group(session_uuid, transaction_id,
                                 term_grace_s=DEFAULT_TERM_GRACE_S):
    """Read the worker's atomically-published active-command pgid (if any)
    and TERM-then-KILL it directly. Used by the parent when the worker itself
    is unresponsive and cannot be trusted to clean up its own child (the
    liveness pipe already asks it to; this is the parent's independent
    backstop so an orphaned command group is never left behind)."""
    path = state_store.verification_active_pgid_path_for(
        session_uuid, transaction_id)
    active = state_store.read_json_tolerant(path)
    if not active or not active.get("pgid"):
        return
    pgid = active["pgid"]
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + term_grace_s
    while time.time() < deadline and _pgid_alive(pgid):
        time.sleep(0.1)
    if _pgid_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# `bounded_evidence_wait` was relocated to `cowork_verification_evidence.py`
# (M5 Package A evidence_reconciliation_seam) and is re-exported above by its
# exact bare name -- see the "Extraction seam re-exports" block near the top
# of this file.


# =========================================================================== #
# Section 10: top-level orchestrator entry point.                             #
# =========================================================================== #


def run_transaction(repo, session_uuid, raw_verification, configuration=None,
                    python_executable=None, command_timeout_s=None,
                    term_grace_s=None, waiter_deadline_s=None,
                    evidence_poll_attempts=None, evidence_poll_delay_s=None,
                    cancel_event=None, work_id=None):
    """Build the snapshot, acquire single-flight, spawn+verify the worker,
    drive it through the approved inventory, tear everything down on
    completion/cancel/timeout, and return a `TransactionResult`.

    This is the ONE function `cowork.py` (a later pass) calls at builder
    promotion. It does not itself raise for ordinary verification failure —
    a red or unverified result is a normal, structured return value; it
    raises only `InventoryError` for a structurally invalid inventory (a
    caller bug, checked before anything is spawned) and lets unexpected
    OSErrors propagate (there is no safe way to fabricate a result when the
    filesystem itself is failing).

    `cancel_event` is an optional `threading.Event`-like object (any object
    with `.is_set()`); when set during the run, the transaction tears down
    the worker/command exactly as on a deadline and returns `unverified`.

    `work_id` (M2 Package E, additive): threaded straight through to
    `build_request`'s own `work_id` — see that function's docstring.
    """
    python_executable = python_executable or sys.executable or "python3"
    transaction_id = new_transaction_id()
    schema, entries, final_suite_label = normalize_inventory(raw_verification)

    snapshot = build_snapshot(repo, session_uuid, transaction_id)
    checkout_root = materialize_checkout(session_uuid, transaction_id)
    # Validate against the CHECKS ROOT (the parent of every per-command
    # checkout: `<txn>/checks/0000`, `<txn>/checks/0001`, ...), not the
    # bootstrap checkout — that's the boundary every isolated_snapshot
    # command's cwd will actually resolve inside, even though the specific
    # per-command subdirectory doesn't exist yet at validation time
    # (`os.path.realpath` normalizes a not-yet-existing path just fine).
    checks_root = state_store.verification_command_checks_root_for(
        session_uuid, transaction_id)
    validate_argv_safety(entries, checks_root)

    request = build_request(
        session_uuid, transaction_id, repo, snapshot["manifest_digest"],
        snapshot["index_digest"], configuration, schema, entries,
        final_suite_label, command_timeout_s=command_timeout_s,
        term_grace_s=term_grace_s,
        evidence_poll_attempts=evidence_poll_attempts,
        evidence_poll_delay_s=evidence_poll_delay_s, work_id=work_id)
    request_key = request["request_key"]

    lock_state, reused_result, lock_fd = acquire_single_flight(
        session_uuid, request_key, transaction_id,
        waiter_deadline_s=waiter_deadline_s)
    if lock_state == "reuse":
        _delete_partial_snapshot(session_uuid, transaction_id)
        result = TransactionResult(reused_result)
        result["reused_lock_result"] = True
        return result

    # The OS-level flock (`lock_fd`) is held from here through the
    # transaction's ENTIRE execution — minting, spawning, running every
    # command, persisting the terminal result — and released only in this
    # `finally`, after that persistence has actually happened. This is what
    # makes single-flight real exclusion rather than an unenforced `.meta`
    # marker: a second, genuinely overlapping caller for the SAME
    # `request_key` cannot acquire the flock (and so cannot start a
    # duplicate transaction) until this one's terminal result is durably
    # published and this fd is released — at which point it finds a
    # `state: terminal` record and reuses it instead of running anything.
    try:
        return _run_transaction_body(
            repo, session_uuid, transaction_id, request, request_key,
            final_suite_label, snapshot, checkout_root, python_executable,
            cancel_event)
    finally:
        release_single_flight(lock_fd)


def _run_transaction_body(repo, session_uuid, transaction_id, request,
                          request_key, final_suite_label, snapshot,
                          checkout_root, python_executable, cancel_event):
    """The mint-through-persist body of `run_transaction`, factored out so
    the whole thing can sit inside `run_transaction`'s single `try/finally`
    that releases the single-flight flock — see the comment at that call
    site."""
    # Mint one stable ledger `V-xxxx` attempt id per (deduplicated) inventory
    # entry BEFORE any command launches, and embed it into the entry itself
    # — the request document written just below is IMMUTABLE once persisted,
    # so this is the one point where these ids can still be attached to it.
    #
    # ONE ALL-OR-NOTHING BATCH CALL under `mint_owned_attempts_batch`'s
    # session-ledger allocation lock — not a per-entry loop. A per-entry
    # loop (read/next_id/append per entry, no lock spanning the whole
    # sequence) let two DIFFERENT transactions in the same session race and
    # allocate the identical V id, and let a failure on entry N leave
    # entries 1..N-1 durably minted while N..last were not — a partial
    # owned inventory. The request-key single-flight lock does not cover
    # this: it only prevents two callers with the EXACT SAME normalized
    # request from double-running, not two DIFFERENT transactions (say, a
    # baseline run and a focused-repair run) from minting concurrently in
    # the same session's ledger.
    #
    # REQUIRED, not best-effort: every entry must have a durable id before
    # ANYTHING is spawned. A partial mint (some entries id'd, one not) is
    # exactly the state that let a transaction go green without a pre-launch
    # id for every command — so any batch failure aborts the WHOLE
    # transaction here, before the worker is ever spawned, before the
    # request is even persisted.
    ledger_path = state_store.ledger_path_for(session_uuid)
    mint_failed_label = None
    mint_entries = [
        (entry["label"],
         {"command": entry.get("command"),
          "execution_mode": entry.get("execution_mode"),
          # NOT "kind": mint_owned_attempts_batch always overwrites "kind"
          # with the ledger's own record-type tag ("attempt") — colliding
          # with this field silently discarded the verification inventory's
          # baseline/focused/preflight/final_suite classification on every
          # owned ledger record. "verification_kind" is the explicit,
          # collision-free, measurement-compatible name.
          "verification_kind": entry.get("kind")})
        for entry in request["inventory"]]
    minted_by_label = ledger.mint_owned_attempts_batch(
        ledger_path, transaction_id, mint_entries)
    if minted_by_label is None:
        mint_failed_label = (request["inventory"][0]["label"]
                             if request["inventory"] else None)
    else:
        for entry in request["inventory"]:
            minted = minted_by_label.get(entry["label"])
            if not minted or not minted.get("id"):
                mint_failed_label = entry["label"]
                break
            entry["ledger_attempt_id"] = minted.get("id")

    if mint_failed_label is not None:
        _delete_partial_snapshot(session_uuid, transaction_id)
        result = TransactionResult({
            "transaction_id": transaction_id,
            "request_key": request_key,
            "verdict": VERDICT_UNVERIFIED,
            "final_suite_label": final_suite_label,
            "final_suite_binding": "not_reached",
            "attempts": [],
            "mutation": None,
            "worker_identity_verified": False,
            "worker_identity": None,
            "reused_lock_result": False,
            "startup_failure": None,
            "ledger_failure": {
                "reason": "mint_failed",
                "label": mint_failed_label,
            },
            "snapshot": {"manifest_digest": snapshot["manifest_digest"],
                        "index_digest": snapshot["index_digest"]},
            "created_at": request.get("created_at"),
            "finished_at": _utc_now(),
        })
        return _persist_terminal_result(
            session_uuid, transaction_id, request_key, result)

    request_path = state_store.verification_request_path_for(
        session_uuid, transaction_id)
    state_store.write_json_atomic(request_path, request)

    # `request["inventory"]` is the DEDUPLICATED list `build_request` already
    # computed (identical (command, execution_mode) entries collapsed to one
    # execution, see `deduplicate_inventory`) — walking the original
    # `entries` here instead would silently re-run every duplicate, which is
    # exactly what single-flight/dedup exists to prevent.
    deduped_entries = request["inventory"]
    try:
        result = _run_owned_transaction(
            repo, session_uuid, transaction_id, request, deduped_entries,
            final_suite_label, snapshot, checkout_root, python_executable,
            cancel_event=cancel_event)
    except OSError as exc:
        # A worker that cannot even be spawned (bad interpreter path,
        # permissions, resource exhaustion) is an UNVERIFIED transaction,
        # not an uncaught crash — the same fail-closed posture as every
        # other worker-boundary failure below. Every entry was already
        # minted a ledger id above; none of this code path's own logic ever
        # reaches `_run_owned_transaction`'s backstop (the exception fires
        # at `spawn_worker` itself, before that function's `try/finally`
        # even starts), so the backstop runs here too — otherwise every one
        # of these ids would be left "pending" forever, exactly the
        # "worker-start failure leaves attempts pending" gap.
        #
        # M5B-R-M2: `spawn_worker` raising OSError here means the exception
        # fired at (or after) `resolve_worker_source`'s own `Popen` call,
        # AFTER a tool-snapshot checkout may already have been materialized
        # on disk but BEFORE `_run_owned_transaction`'s own try/finally
        # (the checkout's only other reclaim site, below) was ever entered
        # -- so this startup-failure path is the one parent-side terminal
        # path that finally block can never cover, and must reclaim the
        # checkout itself. Idempotent no-op when nothing was ever
        # materialized (`resolve_worker_source` returned `None`, or failed
        # before reaching `Popen` at all).
        reclaim_tool_snapshot_checkout(session_uuid, transaction_id)
        backstop_failed_labels = []
        for entry in deduped_entries:
            revised = ledger.revise_owned_attempt(
                ledger_path, transaction_id, entry["label"],
                fields={"reason": "worker_spawn_failed", "detail": str(exc)},
                attempt_state="not_reached")
            if revised is None:
                backstop_failed_labels.append(entry["label"])
        ledger_failure = ({"reason": "not_reached_revision_failed",
                          "labels": backstop_failed_labels}
                         if backstop_failed_labels else None)
        result = TransactionResult({
            "transaction_id": transaction_id,
            "request_key": request_key,
            "verdict": VERDICT_UNVERIFIED,
            "final_suite_label": final_suite_label,
            "final_suite_binding": "not_reached",
            "attempts": [],
            "mutation": None,
            "worker_identity_verified": False,
            "worker_identity": None,
            "startup_failure": {"reason": "worker_spawn_failed",
                                "detail": str(exc)},
            "ledger_failure": ledger_failure,
            "reused_lock_result": False,
            "snapshot": {"manifest_digest": snapshot["manifest_digest"],
                        "index_digest": snapshot["index_digest"]},
            "created_at": request.get("created_at"),
            "finished_at": _utc_now(),
        })
    # Persist the terminal result to its OWN advertised, session-relative
    # path — `verification/transactions/<transaction_id>/result.json` —
    # BEFORE publishing the single-flight lock's terminal metadata or
    # returning to the caller (who may immediately fold the transaction id
    # and this exact path into a hand-back). Without this, the path named
    # in every red/unverified hand-back pointed at a file that never
    # existed: the only place a `TransactionResult` was ever written to
    # disk was inside the LOCK's `.meta` (keyed by request_key, for waiter
    # reuse), never at the per-transaction path a caller or a human is
    # told to go read.
    return _persist_terminal_result(
        session_uuid, transaction_id, request_key, result)


def _issue_permit(session_uuid, transaction_id, index, ledger_attempt_id):
    """Authorize the worker to start EXACTLY entry `index` (bound to this
    transaction and its pre-minted ledger id) — the one write
    `_wait_for_permit` on the worker side is polling for. Called ONLY
    after that entry's own "running" ledger revision has already durably
    succeeded (see the caller in `_run_owned_transaction`). Returns True
    on success; a failure here is treated exactly like any other ledger
    lifecycle failure — it stops the transaction, and the entry is
    correctly backstopped as `not_reached` (the worker can never have
    started it without this write landing)."""
    permit_path = state_store.verification_permit_path_for(
        session_uuid, transaction_id)
    return state_store.write_json_atomic(permit_path, {
        "transaction_id": transaction_id, "index": index,
        "ledger_attempt_id": ledger_attempt_id, "issued_at": _utc_now()})


# `_revise_attempt_ledger`, `_revise_attempt_ledger_with_retry`, and
# `_wait_for_attempt_and_revise_ledger` were relocated to
# `cowork_verification_evidence.py` (M5 Package A
# evidence_reconciliation_seam) and are re-exported above by their exact
# bare names -- see the "Extraction seam re-exports" block near the top
# of this file.

def _run_owned_transaction(repo, session_uuid, transaction_id, request,
                           entries, final_suite_label, snapshot,
                           checkout_root, python_executable,
                           cancel_event=None):
    # M5E-GATE-CUMULATIVE-01 (v2 repair): NOT a request-overridable timeout
    # -- an internal heuristic (below) for telling an ordinary, healthy
    # `spawn_worker` call (dominated by the identity read's own single
    # minimum poll cycle plus real subprocess-launch overhead -- well under
    # a second in practice) apart from one where identity verification
    # genuinely BLOCKED (e.g. a stalled or slow-to-report worker). See the
    # comment at its use site, below, for why this -- not a fresh,
    # unconditional re-read of `cancel_event` -- is what closes the
    # identity-wait cancellation gap without racing Package C's own
    # mid-flight disposition.
    identity_wait_blocked_threshold_s = 0.4

    # `manifest_files` is still needed here for `detect_mutation` below (an
    # unrelated purpose: has the target repo's tracked+untracked-non-ignored
    # tree changed during the transaction) — it just no longer feeds a
    # worker-identity check in THIS function, since that check (and its own,
    # independently re-derived copy of this same manifest) now lives inside
    # `spawn_worker` (worker_capture_seam). `worker_rel_path` is gone
    # entirely from this function for the same reason.
    manifest_doc = state_store.read_json_tolerant(snapshot["manifest_path"])
    manifest_files = (manifest_doc or {}).get("files", {})
    ledger_path = state_store.ledger_path_for(session_uuid)

    # `timeout_policy`/`overall_deadline` are computed BEFORE `spawn_worker`
    # is called (M5A-R-M1) -- restoring the base commit's own ordering, not
    # merely mirroring it. This matters now that `spawn_worker` itself
    # blocks, internally, for up to `timeout_policy.startup_allowance_s`
    # while it reads the worker's identity report (worker_capture_seam):
    # reading `time.time()` for `overall_deadline` AFTER that internal wait
    # instead of before it would silently shift the deadline later by
    # however long the wait took. `_overall_deadline_s` already budgets
    # `startup_allowance_s` as part of the window it returns, so computing
    # the deadline HERE -- immediately before the clock-consuming spawn,
    # exactly where the base commit read it -- is what spends that budget
    # exactly once, instead of implicitly re-adding it on top of itself.
    timeout_policy = request.get("timeout_policy") or {}
    overall_deadline = time.time() + _overall_deadline_s(
        entries, timeout_policy)

    # M5E-GATE-CUMULATIVE-01 (v2 repair): snapshot cancel_event's state HERE,
    # immediately before the clock-consuming `spawn_worker` call below --
    # this is the ONLY thing a cancellation genuinely requested before this
    # transaction ever began dispatching can rely on (entry 0's own
    # `not (worker) verified yet` gate, unchanged from the base's own
    # pre-Package-C shape). It is deliberately NOT, by itself, the whole
    # story for entry 0 any more -- see `_spawn_worker_duration_s` below.
    cancel_requested_before_spawn = bool(
        cancel_event is not None and cancel_event.is_set())

    request_path = state_store.verification_request_path_for(
        session_uuid, transaction_id)
    _spawn_worker_started_at = time.time()
    worker_result = spawn_worker(
        python_executable, checkout_root, request_path,
        session_uuid=session_uuid, transaction_id=transaction_id)
    _spawn_worker_duration_s = time.time() - _spawn_worker_started_at
    # `worker_result` unpacks as the base commit's original three-item
    # handle bundle (`WorkerStartupResult.__iter__` yields exactly those
    # three — see that class's own docstring for why). `classification` —
    # identity/worker_verified/startup_failure, computed entirely inside
    # `spawn_worker` before it returned (worker_capture_seam) — is the
    # M5R2-B2 widened fourth field, read via attribute access; the
    # `proc`-stashed fallback covers a caller that forwards a bare 3-tuple
    # onward instead of this object (see `spawn_worker`'s own docstring).
    # Neither source is trusted merely because it is non-None: both are
    # validated with `isinstance(..., dict)` before any `.get(...)` call,
    # so a malformed or mocked return can never crash this function with an
    # AttributeError -- it degrades to an empty (unverified) classification
    # instead, exactly as a genuinely absent identity report already does.
    # This is what lets `_run_owned_transaction` populate `TransactionResult`
    # directly from `classification` instead of separately calling
    # `verify_worker_identity`/`_read_worker_identity` itself.
    proc, liveness_write_fd, startup_capture_thread = worker_result
    classification = getattr(worker_result, "classification", None)
    if not isinstance(classification, dict):
        classification = getattr(
            proc, "_cowork_startup_classification", None)
    if not isinstance(classification, dict):
        classification = {}
    identity = classification.get("identity")
    worker_verified = classification.get("worker_verified", False)
    startup_failure = classification.get("startup_failure")

    # M5E-GATE-CUMULATIVE-01 (v2 repair): entry 0's own "was this cancelled
    # before it ever got a chance to start" gate is now the OR of two
    # independent, non-competing facts, neither of which is a stale
    # single-point-in-time read:
    #
    #   1. `cancel_requested_before_spawn` -- cancellation already requested
    #      before `spawn_worker` was even called. Exactly the v1 candidate's
    #      own snapshot, unchanged: this alone is what
    #      `test_cancel_set_before_launch_still_completes_bounded_and_
    #      unverified` (Package A) and `test_cancel_set_before_the_worker_
    #      even_starts_stays_unverified` (Package C) rely on, and it costs
    #      nothing to preserve verbatim.
    #
    #   2. `_spawn_worker_duration_s >= identity_wait_blocked_threshold_s`
    #      -- `spawn_worker` itself blocks, internally, for up to
    #      `timeout_policy.startup_allowance_s` while it reads the worker's
    #      identity report (worker_capture_seam). The v1 candidate's own
    #      snapshot (fact 1 alone, sampled once before that blocking call)
    #      is invisible to a cancellation that lands DURING that wait: entry
    #      0 still dispatches into a worker the `_cancel_watcher` thread
    #      below is concurrently tearing down, and the transaction is left
    #      waiting on evidence that can never arrive until the entry's own
    #      (unrelated, often much longer) command timeout expires --
    #      unbounded, not the "bounded and torn down" the identity read's
    #      own timeout_s promises.
    #
    #      Checking `cancel_event.is_set()` freshly the instant `spawn_
    #      worker` returns would close that gap, but at the cost of racing
    #      Package C's OWN mid-flight disposition: an UNCANCELLED,
    #      ORDINARILY-paced `spawn_worker` call (no artificial delay) can
    #      itself take a comparable fraction of a second (the identity
    #      read's own minimum poll cycle plus real subprocess-launch
    #      overhead), so a cancel_event that a caller sets shortly after
    #      launching a transaction (Package C's own test fires one from a
    #      background thread, timed to land once the FIRST command is
    #      already running) can coincidentally already be visible by the
    #      time `spawn_worker` returns even though it was never meant to
    #      preempt entry 0 at all -- that must still resolve to Package C's
    #      RED, not this gate's UNVERIFIED. Gating on `spawn_worker`'s own
    #      observed DURATION instead of on cancel_event's coincidental
    #      timing sidesteps that race entirely: an ordinary, healthy
    #      identity read is never mistaken for one that genuinely blocked,
    #      regardless of exactly when an unrelated cancel_event happened to
    #      fire, because the duration check does not depend on cancel_
    #      event's timing at all -- only on how long `spawn_worker` itself
    #      actually ran.
    cancel_requested_before_dispatch = bool(
        cancel_requested_before_spawn
        or (cancel_event is not None and cancel_event.is_set()
            and _spawn_worker_duration_s
            >= identity_wait_blocked_threshold_s))

    # A `cancel_event` set WHILE a command is mid-flight must not wait for
    # the between-commands check below (which could be minutes away on a
    # long-running command). A tiny watcher thread polls the event and, the
    # instant it fires, tears the worker/active-command group down through
    # the same idempotent path the `finally` block uses at normal
    # completion — this reuses the liveness-pipe-EOF mechanism (closing
    # `liveness_write_fd` wakes the worker's watchdog, which kills its own
    # active command group) instead of inventing a second teardown path.
    cancel_watcher_stop = threading.Event()

    def _cancel_watcher():
        while not cancel_watcher_stop.is_set():
            if cancel_event is not None and cancel_event.is_set():
                cleanup_active_command_group(
                    session_uuid, transaction_id,
                    term_grace_s=timeout_policy.get("term_grace_s")
                    or DEFAULT_TERM_GRACE_S)
                terminate_worker(
                    proc, liveness_write_fd,
                    term_grace_s=timeout_policy.get("cleanup_allowance_s")
                    or DEFAULT_CLEANUP_ALLOWANCE_S)
                return
            cancel_watcher_stop.wait(0.1)

    cancel_watcher = None
    if cancel_event is not None:
        cancel_watcher = threading.Thread(target=_cancel_watcher, daemon=True)
        cancel_watcher.start()

    # `identity`/`worker_verified`/`startup_failure` are already fully
    # computed above, from `classification` — the base's own
    # request_rejected/worker_exited_before_identity_report distinction (and
    # the `startup_capture_thread.join` that precedes reading the log tail
    # for it) now happens inside `spawn_worker`, bound-for-bound identical
    # to the base, just relocated (see cowork_verification_worker.py's
    # `_classify_worker_startup`).

    def _worker_alive():
        """Whether the spawned worker process is still running, as the
        parent can OBSERVE it (blocker79). `proc.poll()` is what the
        evidence waits below consult before spending a further polling
        cycle on a label: a worker that has exited can never emit another
        attempt event, so continuing to poll for one is pure stall -- and
        the same call REAPS the exited child, which is what left a defunct
        worker behind for the whole of the parent's remaining wait. A
        handle that cannot be polled at all is reported ALIVE: not being
        able to observe an exit is never proof of one, and it leaves the
        pre-existing bounded wait exactly as it was. This never decides
        anything about the command's OWN process group -- a dead worker
        does not prove that group is gone, and `bounded_evidence_wait`
        still resolves that separately from its own durable evidence."""
        poll = getattr(proc, "poll", None)
        if not callable(poll):
            return True
        return poll() is None

    attempts = []
    mutation = None
    verdict = VERDICT_UNVERIFIED
    final_suite_binding = "not_reached"
    ledger_failure = None
    active_label = None
    # STARTED vs TERMINALIZED are tracked SEPARATELY, deliberately: a label
    # can be `started` (the parent has committed to this entry's turn — the
    # worker, running independently and serially through the SAME
    # inventory, may already be executing it or about to, REGARDLESS of
    # whether the parent's own "running" ledger write below succeeds) while
    # never being `terminalized` (its terminal/unresolved revision itself
    # failed, or was never reached). The backstop below uses this
    # distinction to choose `unresolved` (honest: "may have run, evidence
    # incomplete") for anything started-but-not-terminalized, and reserves
    # `not_reached` for what the parent is actually sure never started.
    started_labels = set()
    terminalized_labels = set()
    deadline_hit = False

    try:
        if worker_verified:
            for index, entry in enumerate(entries):
                label = entry["label"]
                # M5E-GATE-CUMULATIVE-01: entry 0 alone consults the
                # PRE-DISPATCH snapshot (see its own comment above,
                # `cancel_requested_before_dispatch`); every later entry
                # still consults cancel_event live, exactly as before --
                # unchanged for index >= 1.
                cancelled_now = (
                    cancel_requested_before_dispatch if index == 0
                    else cancel_event is not None and cancel_event.is_set())
                if cancelled_now:
                    deadline_hit = True
                    break
                if time.time() > overall_deadline:
                    # should_defer_teardown consult (evidence_reconciliation_
                    # seam): Package A's own stub always returns False, so
                    # `not should_defer_teardown(...)` is always True here —
                    # this branch is bound-for-bound identical to the base
                    # commit's unconditional `deadline_hit = True; break`.
                    if not should_defer_teardown(
                            session_uuid, transaction_id, active_label):
                        deadline_hit = True
                        break
                mutation = detect_mutation(
                    repo, snapshot["manifest_digest"],
                    snapshot["index_digest"], expected_manifest=manifest_files)
                if mutation is not None:
                    break
                # Mark the attempt "running" as the parent commits to
                # waiting for this entry — completes the lifecycle
                # (`pending -> running -> terminal/unresolved`) instead of
                # jumping straight from "minted" to "terminal" with no
                # observed-start fact recorded at all. This must succeed
                # BEFORE the worker is authorized to start (below) — the
                # worker literally cannot run ahead of a "running" state
                # the parent never durably recorded.
                _running_record, running_ok = _revise_attempt_ledger(
                    ledger_path, transaction_id, label, fields={},
                    attempt_state="running")
                if not running_ok:
                    ledger_failure = {"reason": "running_revision_failed",
                                      "label": label}
                    break
                # ISSUE THE PERMIT: the worker (which blocks on `_wait_for_
                # permit` before starting ANY entry) is now, and only now,
                # authorized to run this specific entry. Closes the
                # run-ahead gap: a protocol-2 worker started entries as
                # fast as it could, independent of the parent's own
                # bookkeeping pace, so a fast inventory could run entry
                # N+1 (or the whole rest of the inventory) before the
                # parent had even discovered a ledger failure revising
                # entry N — after which a later backstop pass would
                # WRONGLY report entry N+1 as `not_reached` when it had
                # actually run. Only once this succeeds is the entry
                # actually `started` (see `started_labels` below) — before
                # this point, the worker cannot have run it, so a break
                # here still correctly backstops to `not_reached`.
                permit_ok = _issue_permit(session_uuid, transaction_id,
                                          index, entry.get("ledger_attempt_id"))
                if not permit_ok:
                    ledger_failure = {"reason": "permit_issue_failed",
                                      "label": label}
                    break
                started_labels.add(label)
                # From here until this entry's evidence is confirmed PRESENT
                # (below), the worker may still be actively running it —
                # `active_label` is what the should_defer_teardown consults
                # (here and in the `finally` block) name as "possibly still
                # in flight" for this transaction.
                active_label = label
                attempt, ledger_ok = _wait_for_attempt_and_revise_ledger(
                    session_uuid, transaction_id, entry, request,
                    ledger_path, overall_deadline, timeout_policy,
                    snapshot["manifest_digest"],
                    is_worker_alive=_worker_alive)
                attempts.append(attempt)
                if ledger_ok:
                    terminalized_labels.add(label)
                if not ledger_ok:
                    ledger_failure = {
                        "reason": ("ledger_revision_mismatch"
                                  if attempt.get("ledger_revision_mismatch")
                                  else "terminal_revision_failed"),
                        "label": label}
                    break
                if entry["label"] == final_suite_label:
                    final_suite_binding = (
                        "ran_once"
                        if attempt.get("evidence_state") == EVIDENCE_PRESENT
                        else "not_reached")
                if attempt.get("evidence_state") != EVIDENCE_PRESENT:
                    # should_defer_teardown consult (evidence_reconciliation_
                    # seam): Package A's own stub always returns False, so
                    # `not should_defer_teardown(...)` is always True here —
                    # bound-for-bound identical to the base commit's
                    # unconditional `break`.
                    if not should_defer_teardown(
                            session_uuid, transaction_id, active_label):
                        break
                else:
                    # This entry is genuinely, fully resolved — nothing is
                    # in flight for it anymore.
                    active_label = None
                if attempt.get("exit_code") not in (0, None) or attempt.get(
                        "timed_out"):
                    break

        # BACKSTOP: complete the lifecycle for every entry not already
        # TERMINALIZED — deadline hit before it started, mutation detected,
        # an earlier entry's ledger failure stopped the loop, or the worker
        # was never verified in the first place (in which case NO entry was
        # ever started). Runs UNCONDITIONALLY, not just on the worker-
        # verified path: none of these may be left "pending"/"running"
        # forever. An entry the parent is SURE never started is
        # `not_reached`; an entry that MAY have run (started, but its own
        # terminal/unresolved revision never landed) is honestly
        # `unresolved`, under its own pre-minted id, exactly once — never
        # `not_reached`, which would claim certainty the parent does not
        # have.
        #
        # FAIL CLOSED on the backstop itself, with a BOUNDED RETRY: a
        # revision here can fail exactly like any other (a transient
        # write glitch, a broken ledger, a mismatched id) — silently
        # discarding that failure is precisely what let "zero pending
        # owned attempts" be an unverified claim rather than a proven one.
        # `_revise_attempt_ledger_with_retry` gives a genuinely transient
        # failure (one that clears on the very next attempt) a chance to
        # settle before this is reported as a real ledger_failure; a
        # PERSISTENT failure is still honestly reported, never masked.
        not_reached_reason = (
            "worker_not_verified" if not worker_verified
            else "deadline_exceeded" if deadline_hit
            else "source_or_index_mutated" if mutation is not None
            else ledger_failure["reason"] if ledger_failure
            else "prior_attempt_stopped_the_inventory")
        unresolved_reason = (ledger_failure["reason"] if ledger_failure
                             else "prior_attempt_stopped_the_inventory")
        backstop_failed_labels = []
        for entry in entries:
            label = entry["label"]
            if label in terminalized_labels:
                continue
            if label in started_labels:
                _record, _ok = _revise_attempt_ledger_with_retry(
                    ledger_path, transaction_id, label,
                    fields={"reason": unresolved_reason,
                           "may_have_run": True},
                    attempt_state="unresolved")
            else:
                _record, _ok = _revise_attempt_ledger_with_retry(
                    ledger_path, transaction_id, label,
                    fields={"reason": not_reached_reason},
                    attempt_state="not_reached")
            if not _ok:
                backstop_failed_labels.append(label)
        if backstop_failed_labels:
            # A backstop failure is reported even when an EARLIER
            # `ledger_failure` already existed — the earlier reason
            # explains the RED/UNVERIFIED cause; this one explains why the
            # ledger itself may still be carrying dangling pending/running
            # ids, which is its own distinct incompleteness a caller must
            # not silently lose.
            ledger_failure = {
                "reason": "not_reached_revision_failed",
                "labels": backstop_failed_labels,
                "prior_reason": ledger_failure.get("reason")
                               if ledger_failure else None,
            }

        if not worker_verified:
            verdict = VERDICT_UNVERIFIED
        elif ledger_failure is not None:
            verdict = VERDICT_UNVERIFIED
        elif deadline_hit:
            verdict = VERDICT_UNVERIFIED
        elif mutation is not None:
            verdict = VERDICT_RED
        else:
            final_mutation = detect_mutation(
                repo, snapshot["manifest_digest"],
                snapshot["index_digest"], expected_manifest=manifest_files)
            if final_mutation is not None:
                mutation = final_mutation
                verdict = VERDICT_RED
            elif len(attempts) == len(entries) and all(
                    a.get("evidence_state") == EVIDENCE_PRESENT
                    and a.get("exit_code") == 0 and not a.get("timed_out")
                    for a in attempts):
                verdict = VERDICT_GREEN
                if final_suite_label != FINAL_SUITE_LEGACY_UNKNOWN:
                    final_suite_binding = "ran_once"
            else:
                verdict = VERDICT_RED
    finally:
        cancel_watcher_stop.set()
        if cancel_watcher is not None:
            cancel_watcher.join(timeout=2)
        # should_defer_teardown consult (evidence_reconciliation_seam):
        # Package A's own stub always returns False, so this branch is
        # bound-for-bound identical to the base commit's unconditional
        # cleanup_active_command_group/terminate_worker calls (M5R-C2).
        # Only Package C's later implementation may return True — leaving
        # eventual teardown to its own resume-time reconciliation entry
        # point instead of tearing the worker down here. Computed ONCE and
        # reused below (M5B-V4-RV-m6): `should_defer_teardown` is safe to
        # call twice (a second call just re-reads the now-durable deferred
        # marker this same call may have just written), but the checkout
        # reclaim decision below must agree with THIS decision, not a
        # separately-recomputed one that could theoretically observe a
        # different in-flight liveness snapshot.
        _defer_teardown = should_defer_teardown(
            session_uuid, transaction_id, active_label)
        if not _defer_teardown:
            cleanup_active_command_group(session_uuid, transaction_id,
                                         term_grace_s=timeout_policy.get(
                                             "term_grace_s")
                                         or DEFAULT_TERM_GRACE_S)
            terminate_worker(
                proc, liveness_write_fd,
                term_grace_s=timeout_policy.get("cleanup_allowance_s")
                or DEFAULT_CLEANUP_ALLOWANCE_S)
        # The early-crash branch above already joins `startup_capture_
        # thread` on ITS OWN path (before reading the log back). Every
        # OTHER exit from this function — normal completion, an exception
        # propagating past the entry loop, cancellation — reaches this
        # `finally` too, and none of those previously joined the thread at
        # all: the worker is now terminated (its stdout pipe has EOF'd),
        # so the capture thread is finishing or already done, but nothing
        # forced the caller to wait for it before returning. A caller that
        # inspects the log file immediately after `_run_owned_transaction`
        # returns could race a still-writing thread. Bounded (never hangs
        # the transaction on a stuck capture); idempotent to join twice.
        if startup_capture_thread is not None:
            startup_capture_thread.join(timeout=5)
        # M5B-R-M2: reclaim THIS transaction's captured tool-snapshot
        # checkout now that the worker has terminated (`terminate_worker`,
        # above) and every piece of evidence the parent still needs from it
        # -- `identity`/`startup_failure` (read into `classification`
        # before `spawn_worker` even returned) and the startup log tail
        # (already folded into `startup_failure["log_tail"]`, and the
        # capture thread that wrote it is now joined, immediately above)
        # -- has already been read into this function's own local
        # variables. Reaches every parent-side terminal path this
        # `finally` already covers: normal completion, cancellation, a
        # deadline hit, and any exception raised inside the `try` block
        # above. `reclaim_tool_snapshot_checkout` is scoped to exactly this
        # `transaction_id`'s own checkout, is idempotent, and never raises,
        # so calling it here can never mask an earlier exception this
        # `finally` is propagating.
        reclaim_tool_snapshot_checkout(session_uuid, transaction_id)

    return TransactionResult({
        "transaction_id": transaction_id,
        "request_key": request.get("request_key"),
        "verdict": verdict,
        "final_suite_label": final_suite_label,
        "final_suite_binding": final_suite_binding,
        "attempts": attempts,
        "mutation": mutation,
        "worker_identity_verified": worker_verified,
        "worker_identity": identity,
        "startup_failure": startup_failure,
        "ledger_failure": ledger_failure,
        "reused_lock_result": False,
        "snapshot": {"manifest_digest": snapshot["manifest_digest"],
                     "index_digest": snapshot["index_digest"]},
        "created_at": request.get("created_at"),
        "finished_at": _utc_now(),
    })


def _execution_wait_budget_s(timeout_policy):
    """How long the PARENT must keep polling for one command's terminal
    evidence before it is entitled to conclude anything is wrong with it.

    Mirrors the worst case the WORKER's own `run_command_in_group` bounds
    itself to for that command: the command's own timeout, then a TERM
    grace period, then (if still alive) a KILL grace period, plus a small
    fixed buffer for process reap and event-file I/O. Polling for any
    LESS than this risks the parent giving up on — and then killing, via
    the `finally` block's `cleanup_active_command_group` — a command that
    is still actively, healthily running well within its own approved
    timeout. The short `evidence_retry_policy` is a SEPARATE, later step
    for evidence that is merely slow to become visible after execution has
    already ended; it is never a substitute for this budget.
    """
    command_timeout_s = (timeout_policy.get("command_timeout_s")
                         or DEFAULT_COMMAND_TIMEOUT_S)
    term_grace_s = timeout_policy.get("term_grace_s") or DEFAULT_TERM_GRACE_S
    return command_timeout_s + 2 * term_grace_s + 5


def _overall_deadline_s(entries, timeout_policy):
    if timeout_policy.get("overall_deadline_s"):
        return timeout_policy["overall_deadline_s"]
    per_command = (timeout_policy.get("command_timeout_s")
                  or DEFAULT_COMMAND_TIMEOUT_S)
    fixed = (timeout_policy.get("startup_allowance_s")
            or DEFAULT_STARTUP_ALLOWANCE_S) + (
        timeout_policy.get("cleanup_allowance_s")
        or DEFAULT_CLEANUP_ALLOWANCE_S) + (
        timeout_policy.get("evidence_allowance_s")
        or DEFAULT_EVIDENCE_ALLOWANCE_S)
    return fixed + per_command * max(1, len(entries))


# =========================================================================== #
# Section 11: CLI entry point (`--worker` mode is what a subprocess execs).   #
# =========================================================================== #


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cowork_verification.py",
        description="Owned verification transaction worker/library.")
    parser.add_argument("--worker", metavar="REQUEST_FILE",
                        help="Run as the worker: execute the approved "
                        "inventory described by REQUEST_FILE and exit.")
    parser.add_argument("--liveness-fd", type=int, default=None,
                        help="Read end of the parent-liveness pipe, "
                        "inherited via pass_fds.")
    args = parser.parse_args(argv)
    if args.worker:
        return worker_main(args.worker, liveness_fd=args.liveness_fd)
    parser.print_help()
    return 2


# =========================================================================== #
# Section 12: checkpoint request/result/receipt contracts + claim/lease       #
# (M5 Package A; garusis/cowork-internal#60).                                 #
# =========================================================================== #
#
# A CHECKPOINT is a single orchestrator-owned command handed to a
# deterministic, non-model executor (the shell-blocked builder's baseline/
# RED/generator-check/focused/final-suite steps) -- distinct from an owned
# VERIFICATION TRANSACTION above (a whole approved inventory the parent
# spawns a worker subprocess to run serially). A checkpoint's `argv`/`cwd`/
# `env` are still entirely orchestrator-authored (never plan- or
# agent-supplied) and its result is still verified against exactly what was
# requested, mirroring this module's existing verification-transaction
# discipline -- `InventoryError`'s stable-`.code` pattern, `PROTOCOL_VERSION`'s
# version-bump discipline, and `acquire_single_flight`'s owner-metadata
# claim/lease shape -- but the executor is a single command, not a whole
# inventory, and the caller polls/publishes a receipt rather than spawning a
# worker subprocess. Persistence paths live in `cowork_state.py`'s
# "checkpoint request/result/receipt + claim/lease persistence" section.
#
# PROTOCOL/SCHEMA VERSIONING. `CHECKPOINT_SCHEMA_VERSION` is bumped whenever
# the request/result/receipt JSON shape changes in a way a differently
# -versioned reader could not safely interpret -- mirroring
# `PROTOCOL_VERSION`'s own discipline above. This is a SEPARATE version
# counter from `PROTOCOL_VERSION` and from the inventory `SCHEMA_1`/
# `SCHEMA_2` pair: a checkpoint request/result/receipt is never read by the
# `--worker` CLI entry point or by `normalize_inventory`, so bumping one
# counter never forces a bump of the others, and legacy schema-1/schema-2
# INVENTORIES are entirely unaffected by (and never validated against) this
# section -- `normalize_inventory` above still accepts exactly what it did
# before this section was added.


class CheckpointError(ValueError):
    """Raised by `normalize_checkpoint_request`/`normalize_checkpoint_result`/
    `normalize_checkpoint_receipt`/`validate_checkpoint_result_against_request`
    for a structurally invalid, unversioned, or unsafe checkpoint document.
    Carries a stable `code` so a caller can render or test against the
    specific rejection reason without parsing prose -- mirrors
    `InventoryError`'s own discipline exactly."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


# Bumped whenever the CheckpointRequest/CheckpointResult/CheckpointReceipt
# JSON shape changes in a way a differently-versioned reader could not
# safely interpret. See this section's own docstring above for why this is
# a separate counter from `PROTOCOL_VERSION`/`SCHEMA_1`/`SCHEMA_2`.
CHECKPOINT_SCHEMA_VERSION = 1

# Closed three-value `mutation_class` enum a CheckpointRequest declares.
MUTATION_CLASS_READ_ONLY = "read_only"
MUTATION_CLASS_ISOLATED = "isolated"
MUTATION_CLASS_LIVE_CANDIDATE = "live_candidate"
MUTATION_CLASSES = (MUTATION_CLASS_READ_ONLY, MUTATION_CLASS_ISOLATED,
                    MUTATION_CLASS_LIVE_CANDIDATE)

# Closed two-value `status` enum: whether a missing/unresolved terminal
# receipt blocks phase advancement (required) or not (optional).
CHECKPOINT_STATUS_REQUIRED = "required"
CHECKPOINT_STATUS_OPTIONAL = "optional"
CHECKPOINT_STATUSES = (CHECKPOINT_STATUS_REQUIRED, CHECKPOINT_STATUS_OPTIONAL)

# Terminal CheckpointReceipt verdicts.
CHECKPOINT_ACCEPTED = "accepted"
CHECKPOINT_REJECTED = "rejected"
CHECKPOINT_VERDICTS = (CHECKPOINT_ACCEPTED, CHECKPOINT_REJECTED)

# Claim/lease record states, mirroring `acquire_single_flight`'s owner
# `.meta` shape (`state`: "running"/"terminal"/"abandoned" there).
CHECKPOINT_CLAIM_CLAIMED = "claimed"
CHECKPOINT_CLAIM_TERMINAL = "terminal"
CHECKPOINT_CLAIM_ABANDONED = "abandoned"

# The bounded startup/publish grace, in seconds, a claim's durable lease
# deadline adds ON TOP OF the checkpoint's own persisted
# `CheckpointRequest.timeout_s`. A claimant's total wall-clock obligation is
# never just its command's timeout: it must also start the command, escalate
# TERM then KILL if that command overruns, re-snapshot the mutation watch,
# and durably persist its result and receipt. Charging a claimant only
# `timeout_s` would classify a HEALTHY claimant -- one that used its full,
# approved command timeout and is now publishing -- as a crash.
#
# Derived from this module's OWN existing worker-deadline policy constants
# (`_execution_wait_budget_s`/`_overall_deadline_s` above compose the very
# same allowances for a whole verification transaction), never a fresh magic
# number: claim-to-spawn startup, the command's own TERM+KILL escalation,
# post-run cleanup, and terminal-evidence persistence. BOUNDED and
# module-owned: never caller-supplied, never raised at runtime, and a
# classification basis only -- nothing reclaims, cancels, or takes over a
# claim when it elapses.
#
# A request that declares NO `timeout_s` is deliberately UNBOUNDED: its
# command may legitimately run forever, so its claim records no finite
# expiry at all (`lease_deadline_at: None`) and a healthy live claimant can
# never be falsely classified as crashed by the passage of time. This grace
# applies only where the request itself supplied a finite bound.
CHECKPOINT_CLAIM_LEASE_GRACE_S = float(
    DEFAULT_STARTUP_ALLOWANCE_S + 2 * DEFAULT_TERM_GRACE_S
    + DEFAULT_CLEANUP_ALLOWANCE_S + DEFAULT_EVIDENCE_ALLOWANCE_S)

# Required CheckpointRequest keys and the exact, closed key set a raw
# request may carry -- anything outside this set is rejected
# (`unknown_key`), the same strict discipline `_normalize_schema2_inventory`
# applies per-entry, generalized to the whole document.
_CHECKPOINT_REQUEST_REQUIRED_KEYS = (
    "checkpoint_schema_version", "checkpoint_id", "session_uuid", "phase",
    "candidate_digest", "argv", "cwd", "mutation_class", "status",
)
_CHECKPOINT_REQUEST_OPTIONAL_KEYS = (
    "work_id", "env", "expected_evidence", "timeout_s",
    "declared_output_paths", "created_at",
)
_CHECKPOINT_REQUEST_ALLOWED_KEYS = frozenset(
    _CHECKPOINT_REQUEST_REQUIRED_KEYS + _CHECKPOINT_REQUEST_OPTIONAL_KEYS)

_CHECKPOINT_RESULT_REQUIRED_KEYS = (
    "checkpoint_schema_version", "checkpoint_id", "executor_identity",
    "argv", "cwd", "exit_code", "evidence_state",
)
_CHECKPOINT_RESULT_OPTIONAL_KEYS = (
    "candidate_digest_after", "stdout_digest", "stderr_digest",
    "started_at", "finished_at", "generated_paths", "output_paths",
    "mutation_detected", "timed_out",
)
_CHECKPOINT_RESULT_ALLOWED_KEYS = frozenset(
    _CHECKPOINT_RESULT_REQUIRED_KEYS + _CHECKPOINT_RESULT_OPTIONAL_KEYS)

_CHECKPOINT_RECEIPT_REQUIRED_KEYS = (
    "checkpoint_schema_version", "checkpoint_id", "session_uuid", "phase",
    "candidate_digest", "verdict", "terminal",
)
_CHECKPOINT_RECEIPT_OPTIONAL_KEYS = (
    "work_id", "rejection_reason", "result", "claim", "published_at",
)
_CHECKPOINT_RECEIPT_ALLOWED_KEYS = frozenset(
    _CHECKPOINT_RECEIPT_REQUIRED_KEYS + _CHECKPOINT_RECEIPT_OPTIONAL_KEYS)


def _checkpoint_require_keys(raw, required_keys, allowed_keys, code_prefix):
    if not isinstance(raw, dict):
        raise CheckpointError(
            "%s_not_object" % code_prefix,
            "%s document is not a JSON object" % code_prefix)
    unknown = set(raw.keys()) - allowed_keys
    if unknown:
        raise CheckpointError(
            "%s_unknown_key" % code_prefix,
            "%s document has unknown key(s) %s (allowed: %s)"
            % (code_prefix, sorted(unknown), sorted(allowed_keys)))
    missing = [k for k in required_keys if k not in raw]
    if missing:
        raise CheckpointError(
            "%s_missing_key" % code_prefix,
            "%s document is missing required key(s) %s"
            % (code_prefix, missing))


def _checkpoint_check_schema_version(raw, code_prefix):
    version = raw.get("checkpoint_schema_version")
    if version != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError(
            "%s_schema_version_mismatch" % code_prefix,
            "%s document declares checkpoint_schema_version=%r, expected %r"
            % (code_prefix, version, CHECKPOINT_SCHEMA_VERSION))


def _checkpoint_check_no_traversal(label, value, code_prefix):
    """Reject a path-shaped string containing a literal `..` traversal
    segment or that is not a plain relative/absolute path string at all --
    mirrors `_check_argv_token`'s own `..`-segment rejection, generalized to
    checkpoint declared/observed paths (which are not argv tokens, so the
    shell-metacharacter/`cd`-token checks that function also applies do not
    apply here)."""
    if not isinstance(value, str) or not value:
        raise CheckpointError(
            "%s_bad_path" % code_prefix,
            "%s %r is not a non-empty string" % (code_prefix, label))
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        raise CheckpointError(
            "%s_path_traversal" % code_prefix,
            "%s %r contains a '..' traversal segment" % (code_prefix, value))


def normalize_checkpoint_request(raw):
    """Validate and normalize a CheckpointRequest: phase, work id, candidate
    digest, exact argv, orchestrator-owned cwd, bounded environment
    exceptions, expected evidence, timeout, `mutation_class`, and
    required/optional `status`. Pure -- no I/O, no clock reads besides what
    the caller already put in `raw`. Raises `CheckpointError` (stable
    `.code`) on any schema/version/unknown-key/type/path-traversal
    violation; returns a shallow-copied, normalized dict on success.

    `cwd` and every entry of `declared_output_paths` (required, non-empty,
    when `mutation_class == "live_candidate"`) are checked for a literal
    `..` traversal segment -- an orchestrator-authored value should never
    need one, and a request that has one is rejected before it can ever be
    compared against a result's own paths."""
    _checkpoint_require_keys(
        raw, _CHECKPOINT_REQUEST_REQUIRED_KEYS,
        _CHECKPOINT_REQUEST_ALLOWED_KEYS, "checkpoint_request")
    _checkpoint_check_schema_version(raw, "checkpoint_request")
    checkpoint_id = raw.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise CheckpointError("checkpoint_request_bad_checkpoint_id",
                              "checkpoint_id must be a non-empty string")
    if not isinstance(raw.get("session_uuid"), str) or not raw["session_uuid"]:
        raise CheckpointError("checkpoint_request_bad_session_uuid",
                              "session_uuid must be a non-empty string")
    if not isinstance(raw.get("phase"), str) or not raw["phase"]:
        raise CheckpointError("checkpoint_request_bad_phase",
                              "phase must be a non-empty string")
    if not isinstance(raw.get("candidate_digest"), str) or not raw[
            "candidate_digest"]:
        raise CheckpointError("checkpoint_request_bad_candidate_digest",
                              "candidate_digest must be a non-empty string")
    if not _is_argv_list(raw.get("argv")):
        raise CheckpointError(
            "checkpoint_request_bad_argv",
            "argv must be a non-empty list of strings")
    _checkpoint_check_no_traversal("cwd", raw.get("cwd"),
                                   "checkpoint_request")
    if not os.path.isabs(raw["cwd"]):
        raise CheckpointError("checkpoint_request_relative_cwd",
                              "cwd must be an absolute, orchestrator-owned "
                              "path: %r" % (raw["cwd"],))
    mutation_class = raw.get("mutation_class")
    if mutation_class not in MUTATION_CLASSES:
        raise CheckpointError(
            "checkpoint_request_bad_mutation_class",
            "mutation_class %r not in %s" % (mutation_class, MUTATION_CLASSES))
    status = raw.get("status")
    if status not in CHECKPOINT_STATUSES:
        raise CheckpointError(
            "checkpoint_request_bad_status",
            "status %r not in %s" % (status, CHECKPOINT_STATUSES))
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise CheckpointError(
            "checkpoint_request_bad_env",
            "env must be a flat string-to-string mapping of bounded "
            "environment exceptions")
    timeout_s = raw.get("timeout_s")
    if timeout_s is not None and (
            isinstance(timeout_s, bool) or not isinstance(
                timeout_s, (int, float)) or timeout_s <= 0):
        raise CheckpointError("checkpoint_request_bad_timeout_s",
                              "timeout_s must be a positive number or "
                              "absent: %r" % (timeout_s,))
    declared_output_paths = raw.get("declared_output_paths") or []
    if not isinstance(declared_output_paths, list):
        raise CheckpointError(
            "checkpoint_request_bad_declared_output_paths",
            "declared_output_paths must be a list of strings")
    for p in declared_output_paths:
        _checkpoint_check_no_traversal(
            "declared_output_paths entry", p, "checkpoint_request")
    if mutation_class == MUTATION_CLASS_LIVE_CANDIDATE and not (
            declared_output_paths):
        raise CheckpointError(
            "checkpoint_request_live_candidate_needs_output_paths",
            "mutation_class=live_candidate requires a non-empty "
            "declared_output_paths")
    if mutation_class != MUTATION_CLASS_LIVE_CANDIDATE and (
            declared_output_paths):
        raise CheckpointError(
            "checkpoint_request_unauthorized_output_paths",
            "declared_output_paths is only meaningful for "
            "mutation_class=live_candidate, got mutation_class=%r"
            % (mutation_class,))
    expected_evidence = raw.get("expected_evidence") or []
    if not isinstance(expected_evidence, list) or not all(
            isinstance(e, str) for e in expected_evidence):
        raise CheckpointError(
            "checkpoint_request_bad_expected_evidence",
            "expected_evidence must be a list of strings")
    normalized = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "session_uuid": raw["session_uuid"],
        "work_id": raw.get("work_id"),
        "phase": raw["phase"],
        "candidate_digest": raw["candidate_digest"],
        "argv": list(raw["argv"]),
        "cwd": raw["cwd"],
        "env": dict(env),
        "expected_evidence": list(expected_evidence),
        "timeout_s": timeout_s,
        "mutation_class": mutation_class,
        "declared_output_paths": list(declared_output_paths),
        "status": status,
        "created_at": raw.get("created_at") or _utc_now(),
    }
    return normalized


def normalize_checkpoint_result(raw):
    """Validate and normalize a CheckpointResult: bounded stdout/stderr
    digests, exit facts, timestamps, generated paths, candidate-after
    identity, mutation detection, and evidence state. Pure -- no I/O.
    Raises `CheckpointError` on any schema/version/unknown-key/type/path-
    traversal violation; returns a shallow-copied, normalized dict on
    success. `evidence_state` reuses this module's own
    `EVIDENCE_PRESENT`/`EVIDENCE_UNRESOLVED`/`EVIDENCE_ABSENT` constants --
    the same three-value vocabulary an owned verification attempt already
    uses -- rather than inventing a parallel one."""
    _checkpoint_require_keys(
        raw, _CHECKPOINT_RESULT_REQUIRED_KEYS,
        _CHECKPOINT_RESULT_ALLOWED_KEYS, "checkpoint_result")
    _checkpoint_check_schema_version(raw, "checkpoint_result")
    checkpoint_id = raw.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise CheckpointError("checkpoint_result_bad_checkpoint_id",
                              "checkpoint_id must be a non-empty string")
    if not isinstance(raw.get("executor_identity"), str) or not raw[
            "executor_identity"]:
        raise CheckpointError("checkpoint_result_bad_executor_identity",
                              "executor_identity must be a non-empty string")
    if not _is_argv_list(raw.get("argv")):
        raise CheckpointError(
            "checkpoint_result_bad_argv",
            "argv must be a non-empty list of strings")
    _checkpoint_check_no_traversal("cwd", raw.get("cwd"), "checkpoint_result")
    exit_code = raw.get("exit_code")
    if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise CheckpointError("checkpoint_result_bad_exit_code",
                              "exit_code must be an int or None: %r"
                              % (exit_code,))
    evidence_state = raw.get("evidence_state")
    if evidence_state not in (EVIDENCE_PRESENT, EVIDENCE_UNRESOLVED,
                              EVIDENCE_ABSENT):
        raise CheckpointError(
            "checkpoint_result_bad_evidence_state",
            "evidence_state %r not in %s"
            % (evidence_state,
               (EVIDENCE_PRESENT, EVIDENCE_UNRESOLVED, EVIDENCE_ABSENT)))
    generated_paths = raw.get("generated_paths") or []
    output_paths = raw.get("output_paths") or []
    for label, paths in (("generated_paths", generated_paths),
                         ("output_paths", output_paths)):
        if not isinstance(paths, list):
            raise CheckpointError(
                "checkpoint_result_bad_%s" % label,
                "%s must be a list of strings" % label)
        for p in paths:
            _checkpoint_check_no_traversal(label, p, "checkpoint_result")
    normalized = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "executor_identity": raw["executor_identity"],
        "argv": list(raw["argv"]),
        "cwd": raw["cwd"],
        "exit_code": exit_code,
        "evidence_state": evidence_state,
        "candidate_digest_after": raw.get("candidate_digest_after"),
        "stdout_digest": raw.get("stdout_digest"),
        "stderr_digest": raw.get("stderr_digest"),
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "generated_paths": list(generated_paths),
        "output_paths": list(output_paths),
        "mutation_detected": bool(raw.get("mutation_detected")),
        "timed_out": bool(raw.get("timed_out")),
    }
    return normalized


def normalize_checkpoint_receipt(raw):
    """Validate and normalize a CheckpointReceipt: the terminal, immutable
    accepted/rejected verdict binding a CheckpointRequest to (at most) one
    CheckpointResult, published exactly once. Pure -- no I/O. Raises
    `CheckpointError` on any schema/version/unknown-key/type violation;
    returns a shallow-copied, normalized dict on success."""
    _checkpoint_require_keys(
        raw, _CHECKPOINT_RECEIPT_REQUIRED_KEYS,
        _CHECKPOINT_RECEIPT_ALLOWED_KEYS, "checkpoint_receipt")
    _checkpoint_check_schema_version(raw, "checkpoint_receipt")
    checkpoint_id = raw.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise CheckpointError("checkpoint_receipt_bad_checkpoint_id",
                              "checkpoint_id must be a non-empty string")
    if not isinstance(raw.get("session_uuid"), str) or not raw["session_uuid"]:
        raise CheckpointError("checkpoint_receipt_bad_session_uuid",
                              "session_uuid must be a non-empty string")
    if not isinstance(raw.get("phase"), str) or not raw["phase"]:
        raise CheckpointError("checkpoint_receipt_bad_phase",
                              "phase must be a non-empty string")
    if not isinstance(raw.get("candidate_digest"), str) or not raw[
            "candidate_digest"]:
        raise CheckpointError("checkpoint_receipt_bad_candidate_digest",
                              "candidate_digest must be a non-empty string")
    verdict = raw.get("verdict")
    if verdict not in CHECKPOINT_VERDICTS:
        raise CheckpointError(
            "checkpoint_receipt_bad_verdict",
            "verdict %r not in %s" % (verdict, CHECKPOINT_VERDICTS))
    if raw.get("terminal") is not True:
        raise CheckpointError(
            "checkpoint_receipt_not_terminal",
            "a CheckpointReceipt must always be published with "
            "terminal=True -- it is the once-only terminal marker itself")
    if verdict == CHECKPOINT_REJECTED and not raw.get("rejection_reason"):
        raise CheckpointError(
            "checkpoint_receipt_missing_rejection_reason",
            "verdict=rejected requires a non-empty rejection_reason")
    normalized = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "session_uuid": raw["session_uuid"],
        "work_id": raw.get("work_id"),
        "phase": raw["phase"],
        "candidate_digest": raw["candidate_digest"],
        "verdict": verdict,
        "rejection_reason": raw.get("rejection_reason"),
        "result": raw.get("result"),
        "claim": raw.get("claim"),
        "terminal": True,
        "published_at": raw.get("published_at") or _utc_now(),
    }
    return normalized


def validate_checkpoint_result_against_request(result, request):
    """Cross-check a normalized CheckpointResult against the
    CheckpointRequest it claims to answer -- fail closed on any mismatch,
    rather than trusting the result's own say-so. Raises `CheckpointError`
    (stable `.code`); returns `None` on success.

    Checks, in order: same `checkpoint_id` (never a mismatched one);
    identical `argv` (wrong-argv); identical `cwd` (wrong-cwd); every
    `output_paths`/`generated_paths` entry the result reports is within the
    request's own `declared_output_paths` (over-broad rejection); and any
    reported mutation is authorized only for `mutation_class=
    live_candidate` (unauthorized-mutating rejection for `read_only`/
    `isolated`, which must run genuinely unmutating)."""
    if result.get("checkpoint_id") != request.get("checkpoint_id"):
        raise CheckpointError(
            "checkpoint_result_wrong_checkpoint_id",
            "result checkpoint_id %r does not match request checkpoint_id "
            "%r" % (result.get("checkpoint_id"), request.get("checkpoint_id")))
    if result.get("argv") != request.get("argv"):
        raise CheckpointError(
            "checkpoint_result_wrong_argv",
            "result argv %r does not match the orchestrator-owned request "
            "argv %r" % (result.get("argv"), request.get("argv")))
    if result.get("cwd") != request.get("cwd"):
        raise CheckpointError(
            "checkpoint_result_wrong_cwd",
            "result cwd %r does not match the orchestrator-owned request "
            "cwd %r" % (result.get("cwd"), request.get("cwd")))
    declared = set(request.get("declared_output_paths") or [])
    reported = set(result.get("output_paths") or []) | set(
        result.get("generated_paths") or [])
    over_broad = reported - declared
    if over_broad:
        raise CheckpointError(
            "checkpoint_result_over_broad_output",
            "result reports path(s) %s outside the request's declared "
            "declared_output_paths %s" % (sorted(over_broad), sorted(declared)))
    mutation_class = request.get("mutation_class")
    if result.get("mutation_detected") and mutation_class in (
            MUTATION_CLASS_READ_ONLY, MUTATION_CLASS_ISOLATED):
        raise CheckpointError(
            "checkpoint_result_unauthorized_mutation",
            "result reports a mutation for mutation_class=%r, which must "
            "run genuinely unmutating" % (mutation_class,))
    return None


def _create_checkpoint_claim_exclusive(path, record):
    """Kernel-exclusive claim creation (M5A-R-M3): `os.open` with
    `O_CREAT | O_EXCL` atomically fails (`OSError`, typically
    `FileExistsError`) if `path` already exists, so two genuinely
    concurrent claimants racing the SAME checkpoint can never both "win" --
    unlike a tolerant-read-then-atomic-write check-and-set, which only ever
    proves exclusion against a single caller's own sequential retries,
    never against a second caller landing between that read and that
    write. The kernel alone decides the one winner; every loser's `os.open`
    fails before a single byte is written. Fsyncs the winner's own bytes
    before returning True -- durable the instant this returns, not merely
    page-cached."""
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        return False
    return True


def claim_checkpoint(session_uuid, checkpoint_id, executor_identity):
    """Acquire the once-only claim/lease for one checkpoint: `{checkpoint_id,
    executor_identity, claimed_at, state}`. KERNEL-EXCLUSIVE creation
    (M5A-R-M3, `_create_checkpoint_claim_exclusive` above): `os.O_CREAT |
    os.O_EXCL`, never a read-then-write check-and-set, so two genuinely
    concurrent claimants can never both observe "not yet claimed" and both
    proceed to write -- the kernel serializes the race at `os.open` itself
    and only the winner's call ever succeeds.

    Returns `(True, claim_record)` on a fresh claim -- the caller that
    receives this IS the sole winner, proven by the kernel, not merely by
    this process's own prior read. Returns `(False, existing_claim_record)`
    WITHOUT writing anything when the checkpoint is already claimed,
    including by the same `executor_identity` -- a duplicate claim attempt
    is always rejected, never silently re-granted or double-executed. A
    loser re-reads the record actually on disk with a brief bounded retry
    (covering the narrow window where the winner's own write is still in
    flight) rather than trusting a locally-built guess, so it never reports
    a claim that is not really durable yet.

    DURABLE DEADLINE BASIS (M5F-CLAIM-CRASH-1): the record additionally
    carries `lease_timeout_s`, `lease_grace_s`, and `lease_deadline_at`,
    derived from the checkpoint's ALREADY PERSISTED
    `CheckpointRequest.timeout_s` (read tolerantly here) plus the bounded,
    module-owned `CHECKPOINT_CLAIM_LEASE_GRACE_S` above -- the claimant's
    startup, TERM/KILL escalation, cleanup, and result/receipt publication
    are part of its obligation, so a healthy claimant that used its FULL
    approved command timeout and is still publishing is never past its
    lease. Without these fields a claim stranded by a crashed claimant
    looks byte-for-byte like a claim whose executor is still running, and
    `classify_checkpoint_claim_liveness` below could not tell the two apart
    from artifacts alone.

    An UNBOUNDED request -- one that declares no usable `timeout_s`, whose
    command `run_checkpoint` therefore runs with no timeout at all -- gets
    `lease_timeout_s: None` and `lease_deadline_at: None`: NO finite expiry
    is invented for it, so the mere passage of time can never classify a
    healthy, legitimately long-running claimant as crashed. So does a
    checkpoint with NO request artifact at all: nothing durable bounds it.

    UNREADABLE IS NOT UNBOUNDED (M5C5-LEASE-FIDELITY-1). `read_json_
    tolerant` swallows every `OSError`/`ValueError`, so it answers `None`
    both for a request that was never persisted AND for one that IS
    persisted but could not be read right now (fd exhaustion, a transient
    EIO, a network filesystem blip, torn bytes). Those two are NOT
    interchangeable here. `run_checkpoint` already read this same request
    successfully a moment earlier, and the claim this function writes
    records its own bound VERBATIM -- `classify_checkpoint_claim_liveness`
    below never second-guesses a present `lease_timeout_s` against the
    request -- so treating a failed reread as "unbounded" would burn a
    permanently unbounded lease into durable state for a request that
    durably declares `timeout_s`, moving it forever into the one class no
    elapsed time can ever indict. Instead, ONLY a definitive `ENOENT`
    licenses the unbounded fallback: every other outcome, including a
    failure to answer the existence question at all and a request whose
    persisted `timeout_s` is not a positive number, raises
    `CheckpointError` BEFORE anything is persisted. Failing closed here
    costs one refused claim that a retry can still win; failing open costs
    an irrecoverable durable lie.

    These fields are additive and advisory: they change nothing about
    acquisition, nothing about duplicate refusal, and nothing reclaims,
    rewrites, or takes over a claim whose deadline has passed."""
    claim_path = state_store.checkpoint_claim_path_for(
        session_uuid, checkpoint_id)
    request_path = state_store.checkpoint_request_path_for(
        session_uuid, checkpoint_id)
    request = state_store.read_json_tolerant(request_path)
    if request is None:
        # Disambiguate the tolerant reader's overloaded `None` -- see
        # UNREADABLE IS NOT UNBOUNDED above. Deterministic and total: the
        # `ENOENT` arm is the only one that continues.
        try:
            os.stat(request_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CheckpointError(
                "checkpoint_request_unreadable",
                "the CheckpointRequest for checkpoint_id=%r could not be "
                "read or even proven absent, so no lease can be derived "
                "from it: %s" % (checkpoint_id, exc))
        else:
            raise CheckpointError(
                "checkpoint_request_unreadable",
                "the CheckpointRequest artifact for checkpoint_id=%r "
                "exists but did not read back as an object; refusing to "
                "persist a claim whose lease would be unbounded only "
                "because that read failed" % (checkpoint_id,))
    timeout_s = request.get("timeout_s") if isinstance(request, dict) else None
    if timeout_s is not None and (
            isinstance(timeout_s, bool) or not isinstance(
                timeout_s, (int, float)) or timeout_s <= 0):
        # A persisted request whose declared bound is unusable is malformed,
        # not unbounded: `normalize_checkpoint_request` rejects exactly this
        # shape with the same code, so no legitimately authored request can
        # reach here. Silently coercing it to `None` would be the same
        # durable downgrade by another route.
        raise CheckpointError(
            "checkpoint_request_bad_timeout_s",
            "timeout_s must be a positive number or absent: %r"
            % (timeout_s,))
    claimed_at = datetime.datetime.now(datetime.timezone.utc)
    deadline_at = None
    if timeout_s is not None:
        try:
            deadline_at = (claimed_at + datetime.timedelta(
                seconds=timeout_s + CHECKPOINT_CLAIM_LEASE_GRACE_S)
                ).isoformat().replace("+00:00", "Z")
        except (OverflowError, ValueError):
            # A nominally-valid but absurd `timeout_s` whose deadline is not
            # representable: record no finite expiry rather than a wrong one.
            # Never-expiring is the only safe direction -- a false crash
            # verdict against a healthy claimant is the failure this whole
            # deadline basis exists to avoid.
            deadline_at = None
    record = {
        "checkpoint_id": checkpoint_id,
        "executor_identity": executor_identity,
        "claimed_at": claimed_at.isoformat().replace("+00:00", "Z"),
        "state": CHECKPOINT_CLAIM_CLAIMED,
        "lease_timeout_s": timeout_s,
        "lease_grace_s": CHECKPOINT_CLAIM_LEASE_GRACE_S,
        "lease_deadline_at": deadline_at,
    }
    if _create_checkpoint_claim_exclusive(claim_path, record):
        return True, record
    existing = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        existing = state_store.read_json_tolerant(claim_path)
        if isinstance(existing, dict) and existing.get("state"):
            break
        time.sleep(0.01)
    return False, existing


def classify_checkpoint_claim_liveness(session_uuid, checkpoint_id, now=None):
    """Classify ONE checkpoint claim's liveness from DURABLE ARTIFACTS ALONE
    -- request, claim, result, receipt -- and return a single literal
    criterion-5 ActivityClass string (M5F-CLAIM-CRASH-1 /
    A-C5-CRASH-STRAND). A claim stranded by a claimant that crashed (or
    whose descendant hung) past its own persisted deadline must be
    DISTINGUISHABLE from a genuinely live claim without any terminal
    output, any live process handle, and any in-memory state -- a stranded
    claim must never read as productive work, as a provider wait, or as
    no-evidence silence.

    Reads nothing but the four artifact paths (`state_store.checkpoint_*_
    path_for`) plus the mere EXISTENCE of the claim path. No terminal, no
    stdout/stderr, no tty, no process table, no subprocess: the answer is
    identical in a fresh process on a machine where the claimant never ran.
    `cowork_activity.py` is deliberately NOT imported (this module has no
    dependency on it, in either direction); the returned values are literal
    strings that MATCH that module's closed `ACTIVITY_CLASSES` vocabulary
    without coupling to it.

    `now` may be `None` (current UTC), an aware/naive `datetime`, or an
    RFC3339 string -- callers and tests inject a clock rather than sleeping
    through a real lease.

    Returned classes, in decision order:

      - `"no_evidence_silence"` -- no claim artifact exists at all. Nothing
        ever claimed this checkpoint, so there is genuinely no evidence of
        any executor to classify. This is the ONLY branch that may return
        silence, and a stranded claim never reaches it.
      - `"process_crash"` -- a claim FILE exists but does not read back as a
        claim record (truncated/corrupt bytes: a claimant that died mid
        -write), or a claim record carries no usable temporal basis at all.
        Fail-closed: durable evidence of a claimant exists, so this is never
        collapsed into silence or into productive work.
      - `"owned_verification"` -- the claim is terminal, OR a receipt is
        durably present (a crash between the receipt write and the terminal
        claim marker still means the verification work itself completed --
        see `publish_checkpoint_receipt`'s receipt-first ordering), OR the
        lease is UNBOUNDED (the request declared no `timeout_s`, so its
        command may legitimately run forever and no elapsed time can ever
        indict it), OR the claim is still WITHIN its persisted deadline. A
        live claim is orchestrator-owned verification in progress -- never
        a crash, and deliberately never `productive_model_work`/
        `provider_wait`, neither of which a deterministic non-model
        executor ever performs.
      - `"hung_descendant"` -- past deadline, no receipt, and a durable
        result records `timed_out` -- the executed descendant overran its
        own timeout and the claimant never published a receipt for it.
      - `"process_crash"` -- past deadline, no receipt, and no such
        timed-out result: the claimant vanished, leaving its claim stranded
        with nothing behind it.

    A LEASE IS NOT A COMMAND TIMEOUT. The deadline a claim persists is its
    command's approved `timeout_s` PLUS `CHECKPOINT_CLAIM_LEASE_GRACE_S`
    (startup, TERM/KILL escalation, cleanup, and result/receipt
    publication). A healthy claimant at -- or past -- its raw command
    timeout but still inside that total lease is `owned_verification`, not
    a crash: expiry only becomes actionable once the claimant has had every
    second its own policy allows it.

    CLASSIFICATION ONLY. This function never writes, never deletes, never
    rewrites a claim, never marks anything `abandoned` (that dead
    vocabulary is never written into a claim, and
    `reconstruct_checkpoint_state` has no branch for it), and never
    reclaims, cancels, or hands over a lease. Availability and recovery are
    somebody else's problem; distinguishability is this function's whole
    job. Never raises: every read is tolerant."""

    def _parse_instant(value):
        """An aware UTC datetime from a datetime/RFC3339 string, else None.
        `datetime.fromisoformat` does not accept a trailing `Z` on every
        supported interpreter, so normalize it first."""
        if isinstance(value, datetime.datetime):
            parsed = value
        elif isinstance(value, str) and value:
            try:
                parsed = datetime.datetime.fromisoformat(
                    value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)

    claim_path = state_store.checkpoint_claim_path_for(
        session_uuid, checkpoint_id)
    claim = state_store.read_json_tolerant(claim_path)
    if not isinstance(claim, dict) or not claim.get("state"):
        return "process_crash" if os.path.exists(
            claim_path) else "no_evidence_silence"
    receipt = state_store.read_json_tolerant(
        state_store.checkpoint_receipt_path_for(session_uuid, checkpoint_id))
    if claim.get("state") == CHECKPOINT_CLAIM_TERMINAL or isinstance(
            receipt, dict):
        return "owned_verification"

    deadline_at = _parse_instant(claim.get("lease_deadline_at"))
    if deadline_at is None:
        # No usable persisted deadline. Either the lease is genuinely
        # UNBOUNDED (a request that declared no `timeout_s`), or this is a
        # legacy claim written before the deadline basis existed, whose
        # basis is rebuilt from `claimed_at` plus the best durable timeout
        # available. A claim that carries the `lease_timeout_s` key states
        # its own bound verbatim (`None` MEANS unbounded, and is never
        # second-guessed against the request); only a legacy claim without
        # that key falls back to the request's own `timeout_s`.
        if "lease_timeout_s" in claim:
            timeout_s = claim.get("lease_timeout_s")
        else:
            request = state_store.read_json_tolerant(
                state_store.checkpoint_request_path_for(session_uuid,
                                                        checkpoint_id))
            timeout_s = request.get("timeout_s") if isinstance(
                request, dict) else None
        if isinstance(timeout_s, bool) or not isinstance(
                timeout_s, (int, float)) or timeout_s <= 0:
            # UNBOUNDED: nothing durable bounds this command's runtime, so
            # no amount of elapsed time is evidence of anything. Never
            # invent a finite expiry here -- that would classify a healthy,
            # legitimately long-running claimant as crashed, which is worse
            # than leaving a genuinely stranded unbounded claim unflagged.
            return "owned_verification"
        claimed_at = _parse_instant(claim.get("claimed_at"))
        if claimed_at is None:
            # A finite bound is declared but nothing durable says when the
            # lease began: fail closed toward the strand (unreachable for
            # any claim `claim_checkpoint` itself writes).
            return "process_crash"
        grace_s = claim.get("lease_grace_s")
        if isinstance(grace_s, bool) or not isinstance(
                grace_s, (int, float)) or grace_s < 0:
            grace_s = CHECKPOINT_CLAIM_LEASE_GRACE_S
        try:
            deadline_at = claimed_at + datetime.timedelta(
                seconds=timeout_s + grace_s)
        except (OverflowError, ValueError):
            return "owned_verification"

    now_at = _parse_instant(now) or datetime.datetime.now(
        datetime.timezone.utc)
    if now_at <= deadline_at:
        return "owned_verification"
    result = state_store.read_json_tolerant(
        state_store.checkpoint_result_path_for(session_uuid, checkpoint_id))
    if isinstance(result, dict) and result.get("timed_out"):
        return "hung_descendant"
    return "process_crash"


def publish_checkpoint_receipt(session_uuid, checkpoint_id, receipt):
    """Publish a CheckpointReceipt exactly once. DURABLE, RECEIPT-FIRST
    ordering (M5A-R-M2): the immutable receipt is written to its own
    advertised path via `write_json_atomic_durable` FIRST; the claim/lease
    is marked `state=terminal` (the once-only terminal publication marker)
    -- also via `write_json_atomic_durable` -- only AFTER that receipt
    write has durably succeeded. A crash, or any failure, between these two
    writes therefore leaves the claim still `state=claimed`, NEVER falsely
    `terminal` with no receipt behind it: a retried
    `publish_checkpoint_receipt` call for the SAME (immutable) receipt
    content simply re-writes the identical receipt bytes (a harmless,
    idempotent overwrite) and then completes the terminal marker -- a
    failed publish is always retryable, never a permanently stuck once-only
    guard with nothing behind it. This mirrors `_persist_terminal_result`'s
    own persist-before-publish principle for a whole transaction's result,
    adapted to a single checkpoint, and upgrades both writes to the
    fsync'd-durable variant: a checkpoint receipt is exactly as load-bearing
    to a resumed session as an owned transaction's `result.json`.

    `receipt` must already be a normalized dict (see
    `normalize_checkpoint_receipt`) with `terminal=True`. Returns `(True,
    receipt)` only once BOTH durable writes have succeeded. Returns
    `(False, existing_receipt_or_None)` WITHOUT overwriting anything when
    the claim is ALREADY `state=terminal` -- a second publish attempt for
    an already-terminal checkpoint is always rejected, never silently
    overwritten. Returns `(False, None)` if the receipt write itself fails
    -- nothing durable has changed, so the caller may simply retry."""
    if receipt.get("terminal") is not True:
        raise CheckpointError(
            "checkpoint_receipt_not_terminal",
            "publish_checkpoint_receipt requires a normalized receipt with "
            "terminal=True")
    claim_path = state_store.checkpoint_claim_path_for(
        session_uuid, checkpoint_id)
    receipt_path = state_store.checkpoint_receipt_path_for(
        session_uuid, checkpoint_id)
    existing_claim = state_store.read_json_tolerant(claim_path)
    if isinstance(existing_claim, dict) and existing_claim.get(
            "state") == CHECKPOINT_CLAIM_TERMINAL:
        return False, state_store.read_json_tolerant(receipt_path)
    if not state_store.write_json_atomic_durable(receipt_path, receipt):
        return False, None
    terminal_claim = dict(existing_claim or {
        "checkpoint_id": checkpoint_id, "executor_identity": None,
        "claimed_at": None})
    terminal_claim["state"] = CHECKPOINT_CLAIM_TERMINAL
    terminal_claim["terminal_at"] = _utc_now()
    if not state_store.write_json_atomic_durable(claim_path, terminal_claim):
        return False, None
    return True, receipt


def reconstruct_checkpoint_state(session_uuid, checkpoint_id):
    """Reconstruct one checkpoint's state from artifacts alone -- request,
    claim, result, and receipt, whichever of these are actually on disk --
    for crash/resume: no in-memory state is ever trusted, matching this
    module's existing fail-closed, artifacts-are-truth discipline (see
    `run_transaction`'s single-flight lock reuse path above). Returns a
    dict `{"checkpoint_id", "request", "claim", "result", "receipt",
    "state"}`, where `state` is one of `"pending"` (a request exists, no
    claim yet), `"claimed"` (a claim exists, not yet terminal), `"terminal"`
    (a receipt was published), or `"unknown"` (no request found at all --
    the caller asked about a checkpoint_id nothing was ever persisted for).
    Never raises: every read goes through `state_store.read_json_tolerant`,
    tolerant of a missing or concurrently-written file."""
    request = state_store.read_json_tolerant(
        state_store.checkpoint_request_path_for(session_uuid, checkpoint_id))
    claim = state_store.read_json_tolerant(
        state_store.checkpoint_claim_path_for(session_uuid, checkpoint_id))
    result = state_store.read_json_tolerant(
        state_store.checkpoint_result_path_for(session_uuid, checkpoint_id))
    receipt = state_store.read_json_tolerant(
        state_store.checkpoint_receipt_path_for(session_uuid, checkpoint_id))
    if request is None:
        state = "unknown"
    elif isinstance(claim, dict) and claim.get(
            "state") == CHECKPOINT_CLAIM_TERMINAL:
        state = "terminal"
    elif isinstance(claim, dict) and claim.get(
            "state") == CHECKPOINT_CLAIM_CLAIMED:
        state = "claimed"
    else:
        state = "pending"
    return {"checkpoint_id": checkpoint_id, "request": request,
           "claim": claim, "result": result, "receipt": receipt,
           "state": state}


# --------------------------------------------------------------------------- #
# M5 Package E: central checkpoint gateway -- dispatch, run, submit, publish  #
# (garusis/cowork-internal#60). Everything above this section (CheckpointError#
# through reconstruct_checkpoint_state) is Package A's frozen contract layer  #
# and is never edited here. This section is the "final seam wiring" this     #
# module's writable authority names: it is the ONE place a CheckpointRequest #
# actually gets executed by a deterministic, non-model executor and the ONE  #
# place a CheckpointResult is cross-checked and turned into a published,     #
# once-only CheckpointReceipt.                                               #
# --------------------------------------------------------------------------- #

# Hard cap on the bytes of one stream (stdout or stderr) hashed into a
# CheckpointResult's `stdout_digest`/`stderr_digest` -- mirrors
# `cowork_verification_worker.MAX_STARTUP_LOG_BYTES`'s bounded-capture
# discipline (that module is not imported here: it owns a different,
# unrelated worker-identity capture; this is an independent, purpose-built
# bound for an arbitrary checkpoint command's own output).
CHECKPOINT_MAX_CAPTURE_BYTES = 256 * 1024


def _checkpoint_executor_identity():
    """This process's own persisted executor identity: hostname + pid. Never
    random -- the SAME process claiming and running a checkpoint reports the
    SAME identity a submitted result is cross-checked against
    (`_checkpoint_result_matches_claim_executor`), so a result genuinely
    submitted by a different process/host can never be silently accepted as
    this claim's own."""
    return "%s:%d" % (socket.gethostname(), os.getpid())


def _checkpoint_digest(data):
    """Sha256 hex digest of AT MOST `CHECKPOINT_MAX_CAPTURE_BYTES` of `data`
    (bytes) -- the bounded stdout/stderr digest a CheckpointResult carries.
    `None` for empty/absent output, never an empty-string digest standing in
    for "no output"."""
    if not data:
        return None
    return hashlib.sha256(data[:CHECKPOINT_MAX_CAPTURE_BYTES]).hexdigest()


def mint_checkpoint_id(work_id=None):
    """A fresh, caller-opaque checkpoint id: `ckpt-<uuid4 hex>`, optionally
    prefixed by a sanitized `work_id` fragment purely for human log
    readability -- never parsed back out of the id by any reader (the id's
    only load-bearing property is uniqueness, exactly like a transaction
    id)."""
    token = uuid.uuid4().hex
    if work_id:
        safe = "".join(c if c.isalnum() else "-" for c in str(work_id))[:24]
        return "ckpt-%s-%s" % (safe, token)
    return "ckpt-%s" % token


def build_and_persist_checkpoint_request(
        session_uuid, checkpoint_id, work_id, phase, candidate_digest, argv,
        cwd, mutation_class, status=CHECKPOINT_STATUS_REQUIRED, env=None,
        expected_evidence=None, timeout_s=None, declared_output_paths=None):
    """Author, normalize, and durably persist ONE CheckpointRequest -- the
    orchestrator-owned, typed replacement for a prose "please run the tests"
    turn. Pure orchestration data in, a normalized dict out (see
    `normalize_checkpoint_request`); raises `CheckpointError` on a
    caller-contract violation (a central-dispatch call site building a
    malformed request is a bug to fail closed on, not to silently coerce).

    Writing is durable (`state_store.write_json_atomic_durable`) and
    idempotent by content: called twice for the SAME `checkpoint_id` with the
    SAME fields, this simply re-writes the identical bytes -- the request is
    authored exactly once per checkpoint_id in practice (a fresh id is
    minted per dispatch via `mint_checkpoint_id`), but resume/retry paths
    that recompute the same request from the same durable inputs never
    corrupt an already-persisted one."""
    raw = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "session_uuid": session_uuid,
        "work_id": work_id,
        "phase": phase,
        "candidate_digest": candidate_digest,
        "argv": list(argv or []),
        "cwd": cwd,
        "env": dict(env or {}),
        "expected_evidence": list(expected_evidence or []),
        "timeout_s": timeout_s,
        "mutation_class": mutation_class,
        "declared_output_paths": list(declared_output_paths or []),
        "status": status,
    }
    normalized = normalize_checkpoint_request(raw)
    path = state_store.checkpoint_request_path_for(session_uuid, checkpoint_id)
    if not state_store.write_json_atomic_durable(path, normalized):
        raise CheckpointError(
            "checkpoint_request_persist_failed",
            "could not durably persist checkpoint request %r" % checkpoint_id)
    return normalized


def _git_status_entries(cwd):
    """`{relative_path: two_char_status_code}` from `git status --porcelain`
    at `cwd`, or `None` when `cwd` is not inside a git worktree / git is
    unavailable -- the caller falls back to watching only the request's own
    `declared_output_paths` in that case. A rename's entry keys on its
    DESTINATION path only (`"R  old -> new"` -> `new`), matching how a
    checkpoint's own declared/reported output paths are named."""
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
            text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    entries = {}
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries[path.strip('"')] = code
    return entries


def _snapshot_mutation_watch(cwd, declared_output_paths):
    """The BEFORE/AFTER snapshot `run_checkpoint` diffs to derive
    `mutation_detected`/`output_paths`/`generated_paths` without trusting the
    executed command's own say-so.

    Prefers a WHOLE-TREE `git status --porcelain` snapshot (`_git_status_
    entries`) when `cwd` is a git worktree -- this catches an UNDECLARED
    mutated/new path too (never silently missed just because a live_candidate
    request never named it -- that path still fails `_authorize_live_
    candidate_mutations`'s declared-output check below, and still trips
    `validate_checkpoint_result_against_request`'s `checkpoint_result_
    unauthorized_mutation` for `read_only`/`isolated`). Falls back to a
    `state_store.build_manifest` over `declared_output_paths` ALONE only when
    `cwd` is not a git worktree / git is unavailable -- narrower, but the
    only signal available without git."""
    entries = _git_status_entries(cwd)
    if entries is not None:
        return {"kind": "git", "entries": entries}
    return {"kind": "manifest",
           "manifest": state_store.build_manifest(
               cwd, declared_output_paths or [])}


def _diff_mutation_watch(before, after):
    """`(mutated, generated)` sorted-list pair from a `_snapshot_mutation_
    watch` BEFORE/AFTER pair -- `generated` (a subset of `mutated`) is every
    path that is BRAND NEW (did not exist before). GIT snapshots: `mutated`
    is every path whose status entry changed (including appearing fresh);
    `generated` is every freshly-appeared path git reports untracked
    (`"??"`). MANIFEST-fallback snapshots: a hash-changed-or-newly-readable
    diff, matching the pre-git-fallback behavior exactly."""
    if before.get("kind") == "git":
        before_entries = before.get("entries") or {}
        after_entries = after.get("entries") or {}
        mutated = sorted(p for p, code in after_entries.items()
                         if before_entries.get(p) != code)
        generated = sorted(p for p in mutated
                           if p not in before_entries
                           and after_entries.get(p) == "??")
        return mutated, generated
    before_by_path = {e["path"]: e
                      for e in (before.get("manifest") or {}).get("files", [])}
    mutated, generated = [], []
    for entry in (after.get("manifest") or {}).get("files", []):
        path = entry["path"]
        prior = before_by_path.get(path)
        prior_ok = bool(prior) and prior.get("state") == "ok"
        if entry.get("state") == "ok" and (
                not prior_ok or prior.get("sha256") != entry.get("sha256")):
            mutated.append(path)
            if not prior_ok:
                generated.append(path)
    return sorted(mutated), sorted(generated)


def _authorize_live_candidate_mutations(cwd, declared_output_paths,
                                         mutated_paths):
    """For `mutation_class=live_candidate` ONLY: every mutated path must be
    BOTH a declared output (`OwnedScope.is_declared_output`, unmodified) AND
    an owned, recoverable write per `cowork_action_policy.decide(...)`,
    unmodified -- the exact reuse the frozen brief requires, never a
    re-derived ownership rule of this module's own invention. Returns the
    sorted list of mutated paths that FAILED either check (empty when every
    mutation is authorized).

    `cowork_action_policy` is imported LAZILY, here, rather than at module
    level: Package A's own isolated-import-boundary test fixtures copy only
    THIS module (plus its known `cowork_state`/`cowork_ledger` siblings) into
    a standalone directory to prove this module's own seam-unavailable/
    import-error propagation semantics in isolation (see
    `scripts/test_m5_package_a_contracts.py`'s `WorkerSubprocessMissingSeam
    SiblingsTests`/`NarrowedImportErrorHandlingTests`) -- a module-level
    import of a dependency those fixtures never copy would break that
    isolation for every caller, not just the one live_candidate mutation
    path that actually needs it."""
    import cowork_action_policy as action_policy
    # `repo_roots` is deliberately LEFT EMPTY: `OwnedScope.owns()` treats
    # every path under any `repo_roots` entry as owned regardless of
    # `declared_outputs` (the same broad "the whole repo is recoverable via
    # git" rule an agent's own writes are checked against) -- passing
    # `cwd` as a repo root here would make EVERY path inside the checkout
    # pass `decide()`, silently defeating the declared-output restriction
    # this function exists to enforce. `declared_outputs` alone is the
    # checkpoint's entire authorized-write surface.
    scope = action_policy.OwnedScope(
        declared_outputs=tuple(
            os.path.join(cwd, p) for p in (declared_output_paths or [])))
    unauthorized = []
    for rel in mutated_paths:
        abs_path = os.path.join(cwd, rel)
        if not scope.is_declared_output(abs_path):
            unauthorized.append(rel)
            continue
        action = {"class": "write", "targets": [abs_path],
                  "resolution_complete": True}
        decision = action_policy.decide(action, scope)
        if not decision.get("allow"):
            unauthorized.append(rel)
    return sorted(unauthorized)


def _checkpoint_evidence_state(expected_evidence, cwd, exit_code, timed_out):
    """Reuse this module's own `EVIDENCE_PRESENT`/`EVIDENCE_UNRESOLVED`/
    `EVIDENCE_ABSENT` vocabulary for a checkpoint's result, exactly as an
    owned verification attempt already does -- never a parallel one. A timed
    out run is always unresolved; named `expected_evidence` paths (relative
    to `cwd`) must ALL exist on disk for evidence to be present; with no
    `expected_evidence` declared, a zero exit code alone counts as
    present."""
    if timed_out:
        return EVIDENCE_UNRESOLVED
    if expected_evidence:
        if all(os.path.exists(os.path.join(cwd, p)) for p in
              expected_evidence):
            return EVIDENCE_PRESENT
        return EVIDENCE_ABSENT
    return EVIDENCE_PRESENT if exit_code == 0 else EVIDENCE_ABSENT


def submit_checkpoint_result(session_uuid, checkpoint_id, raw_result):
    """THE seam: cross-check one submitted CheckpointResult against its own
    CheckpointRequest and claim, then publish the terminal CheckpointReceipt
    exactly once. This is the SOLE path a CheckpointResult -- however
    produced, by `run_checkpoint` below or by a genuinely separate executor
    process submitting one out of band -- can ever become a receipt.

    Fail-closed rejection categories, each caught and turned into a
    `verdict=rejected` receipt (never a raised exception the caller has to
    separately handle -- a bad result is not a caller bug, it is exactly the
    thing this seam exists to catch and record):

      - missing request (`checkpoint_request_missing`);
      - malformed result shape (`normalize_checkpoint_result`'s own codes);
      - wrong `executor_identity` -- does not match the checkpoint's own
        claim (`checkpoint_result_wrong_executor`);
      - wrong `argv`/`cwd`, over-broad output, or unauthorized mutation for
        `read_only`/`isolated` (`validate_checkpoint_result_against_request`'s
        own codes);
      - for `mutation_class=live_candidate`, a mutated path that is not both
        a declared output AND action-policy-authorized
        (`checkpoint_result_unauthorized_mutation`).

    Once-only, claim-gated: a checkpoint with no claim at all refuses
    (`checkpoint_not_claimed` -- a result may never be accepted for a
    checkpoint nothing ever claimed to execute); a checkpoint whose claim is
    ALREADY terminal returns the existing receipt UNCHANGED (a stale/
    duplicate resubmission is a no-op, never a second publish or a
    re-execution) -- delegated entirely to `publish_checkpoint_receipt`'s own
    durable, receipt-first once-only guard.

    Returns the published (or already-terminal, unchanged) CheckpointReceipt
    dict."""
    request = state_store.read_json_tolerant(
        state_store.checkpoint_request_path_for(session_uuid, checkpoint_id))
    if request is None:
        raise CheckpointError(
            "checkpoint_request_missing",
            "no CheckpointRequest persisted for checkpoint_id=%r"
            % (checkpoint_id,))
    claim = state_store.read_json_tolerant(
        state_store.checkpoint_claim_path_for(session_uuid, checkpoint_id))
    if not isinstance(claim, dict) or not claim.get("state"):
        raise CheckpointError(
            "checkpoint_not_claimed",
            "checkpoint_id=%r has no claim -- a result may only be "
            "submitted for a claimed checkpoint" % (checkpoint_id,))
    if claim.get("state") == CHECKPOINT_CLAIM_TERMINAL:
        return state_store.read_json_tolerant(
            state_store.checkpoint_receipt_path_for(session_uuid,
                                                     checkpoint_id))

    verdict = CHECKPOINT_ACCEPTED
    rejection_reason = None
    normalized_result = None
    try:
        normalized_result = normalize_checkpoint_result(raw_result)
        if normalized_result.get("executor_identity") != claim.get(
                "executor_identity"):
            raise CheckpointError(
                "checkpoint_result_wrong_executor",
                "result executor_identity %r does not match the claim's "
                "own executor_identity %r"
                % (normalized_result.get("executor_identity"),
                   claim.get("executor_identity")))
        validate_checkpoint_result_against_request(normalized_result, request)
        if request.get("mutation_class") == MUTATION_CLASS_LIVE_CANDIDATE:
            reported = sorted(
                set(normalized_result.get("output_paths") or [])
                | set(normalized_result.get("generated_paths") or []))
            unauthorized = _authorize_live_candidate_mutations(
                request["cwd"], request.get("declared_output_paths"),
                reported)
            if unauthorized:
                raise CheckpointError(
                    "checkpoint_result_unauthorized_mutation",
                    "mutated path(s) %s are not both a declared output and "
                    "action-policy-authorized" % (unauthorized,))
        if normalized_result.get("timed_out"):
            raise CheckpointError(
                "checkpoint_result_timed_out",
                "checkpoint_id=%r timed out" % (checkpoint_id,))
        if normalized_result.get("exit_code") != 0:
            raise CheckpointError(
                "checkpoint_result_nonzero_exit",
                "checkpoint_id=%r exited %r"
                % (checkpoint_id, normalized_result.get("exit_code")))
        if normalized_result.get("evidence_state") != EVIDENCE_PRESENT:
            raise CheckpointError(
                "checkpoint_result_evidence_not_present",
                "checkpoint_id=%r evidence_state=%r"
                % (checkpoint_id, normalized_result.get("evidence_state")))
    except CheckpointError as exc:
        verdict = CHECKPOINT_REJECTED
        rejection_reason = exc.code
        if normalized_result is None:
            # Malformed beyond normalization -- persist nothing not already
            # durable; the receipt below still binds by checkpoint_id/phase/
            # candidate straight from the (trustworthy) request.
            normalized_result = None

    if normalized_result is not None:
        state_store.write_json_atomic_durable(
            state_store.checkpoint_result_path_for(session_uuid,
                                                    checkpoint_id),
            normalized_result)

    receipt = normalize_checkpoint_receipt({
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "session_uuid": session_uuid,
        "work_id": request.get("work_id"),
        "phase": request["phase"],
        "candidate_digest": request["candidate_digest"],
        "verdict": verdict,
        "rejection_reason": rejection_reason,
        "result": normalized_result,
        "claim": claim,
        "terminal": True,
    })
    published, stored = publish_checkpoint_receipt(
        session_uuid, checkpoint_id, receipt)
    return stored if stored is not None else receipt


def run_checkpoint(session_uuid, checkpoint_id, executor_identity=None):
    """THE deterministic, non-model runner (frozen brief: "a deterministic
    non-model runner with exclusive once-only claim/lease, persisted
    executor identity, bounded stdout/stderr digests, terminal publication,
    and exact candidate binding"): claim the checkpoint exclusively, execute
    its own orchestrator-authored `argv`/`cwd` -- never a plan- or
    agent-supplied value -- as a real subprocess, and hand the observed
    result to `submit_checkpoint_result` for cross-checking and once-only
    publication.

    `executor_identity` defaults to this process's own persisted identity
    (`_checkpoint_executor_identity`) -- ONLY a caller simulating a distinct
    executor (tests) overrides it.

    ONCE-ONLY: if the exclusive claim is already held (by this or any other
    executor), NOTHING is executed -- the checkpoint may not be run twice.
    An already-terminal checkpoint returns its existing receipt unchanged; a
    checkpoint claimed but not yet terminal raises `CheckpointError`
    (`checkpoint_already_claimed`) rather than silently re-running someone
    else's in-flight command.

    Returns the published CheckpointReceipt dict."""
    request = state_store.read_json_tolerant(
        state_store.checkpoint_request_path_for(session_uuid, checkpoint_id))
    if request is None:
        raise CheckpointError(
            "checkpoint_request_missing",
            "no CheckpointRequest persisted for checkpoint_id=%r"
            % (checkpoint_id,))
    executor_identity = executor_identity or _checkpoint_executor_identity()
    claimed, claim_record = claim_checkpoint(
        session_uuid, checkpoint_id, executor_identity)
    if not claimed:
        if isinstance(claim_record, dict) and claim_record.get(
                "state") == CHECKPOINT_CLAIM_TERMINAL:
            return state_store.read_json_tolerant(
                state_store.checkpoint_receipt_path_for(session_uuid,
                                                         checkpoint_id))
        raise CheckpointError(
            "checkpoint_already_claimed",
            "checkpoint_id=%r is already claimed by executor_identity=%r"
            % (checkpoint_id, (claim_record or {}).get("executor_identity")))

    before = _snapshot_mutation_watch(
        request["cwd"], request.get("declared_output_paths"))
    started_at = _utc_now()
    timed_out = False
    try:
        completed = subprocess.run(
            request["argv"], cwd=request["cwd"],
            env=dict(os.environ, **(request.get("env") or {})),
            capture_output=True, timeout=request.get("timeout_s"),
            check=False)
        exit_code = completed.returncode
        stdout_bytes, stderr_bytes = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout_bytes, stderr_bytes = exc.stdout or b"", exc.stderr or b""
    except OSError as exc:
        exit_code = None
        stdout_bytes, stderr_bytes = str(exc).encode("utf-8", "replace"), b""
    finished_at = _utc_now()
    after = _snapshot_mutation_watch(
        request["cwd"], request.get("declared_output_paths"))
    mutated_paths, generated_paths = _diff_mutation_watch(before, after)

    candidate_digest_after = request["candidate_digest"]
    if mutated_paths:
        candidate_digest_after = state_store.manifest_digest(
            state_store.build_manifest(request["cwd"], mutated_paths))

    raw_result = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "executor_identity": executor_identity,
        "argv": list(request["argv"]),
        "cwd": request["cwd"],
        "exit_code": exit_code,
        "evidence_state": _checkpoint_evidence_state(
            request.get("expected_evidence"), request["cwd"], exit_code,
            timed_out),
        "candidate_digest_after": candidate_digest_after,
        "stdout_digest": _checkpoint_digest(stdout_bytes),
        "stderr_digest": _checkpoint_digest(stderr_bytes),
        "started_at": started_at,
        "finished_at": finished_at,
        "generated_paths": generated_paths,
        "output_paths": mutated_paths,
        "mutation_detected": bool(mutated_paths),
        "timed_out": timed_out,
    }
    return submit_checkpoint_result(session_uuid, checkpoint_id, raw_result)


def reconstruct_checkpoint_state_with_liveness(session_uuid, checkpoint_id,
                                               now=None):
    """`reconstruct_checkpoint_state` PLUS the durable liveness verdict for
    the same checkpoint, joined as ONE ADDITIVE field: `claim_liveness`.
    This is the reconstruction surface production wake/status paths use
    (`cowork.checkpoint_wake_block`), and it is the reason
    `classify_checkpoint_claim_liveness` has a real production call site
    rather than test-only reachability.

    WHY A SEPARATE FIELD, NOT A FIFTH `state`. `state` keeps EXACTLY its
    four documented values (`"pending"`, `"claimed"`, `"terminal"`,
    `"unknown"`) and `reconstruct_checkpoint_state` is re-used verbatim --
    byte-for-byte unmodified -- so every existing consumer of `state` reads
    precisely what it read before. The liveness verdict rides alongside
    under its own key, drawn from `classify_checkpoint_claim_liveness`'s own
    disjoint vocabulary (`"owned_verification"`, `"process_crash"`,
    `"hung_descendant"`, `"no_evidence_silence"`). The two vocabularies
    share no member, so no downstream reader can confuse a crash-stranded
    claim with an ordinary live `claimed` one, and none can silently mistake
    a liveness value for a lifecycle state.

    THE DEFECT THIS CLOSES. A claimant that dies past its own persisted
    lease leaves `state="claimed"` on disk forever -- correct as lifecycle
    (nothing terminal was ever published) but indistinguishable, to every
    reader, from a healthy claimant still doing the work. Pairing the two
    fields makes the strand visible without rewriting, reclaiming, or
    otherwise touching the claim: `{"state": "claimed", "claim_liveness":
    "process_crash"}` says both true things at once.

    `now` is threaded straight through to the classifier (`None` = current
    UTC, else a datetime or RFC3339 string), so a caller or test injects a
    clock instead of sleeping through a real lease. Classification stays
    derived from DURABLE ARTIFACTS ALONE -- never terminal text, never a
    live process handle, never in-memory state. Never raises: both halves
    are individually total and read tolerantly."""
    reconstructed = reconstruct_checkpoint_state(session_uuid, checkpoint_id)
    reconstructed["claim_liveness"] = classify_checkpoint_claim_liveness(
        session_uuid, checkpoint_id, now=now)
    return reconstructed


def reconstruct_all_checkpoints(session_uuid, now=None):
    """Every checkpoint's full reconstructed state for one session, from
    artifacts alone -- `{checkpoint_id:
    reconstruct_checkpoint_state_with_liveness(...)}` -- built by
    enumerating `state_store.list_checkpoint_ids`. This is the crash/resume
    entry point: pending, claimed, and terminal checkpoints are all
    reconstructed the same way, with no separate index file to drift out of
    sync. Never raises; a session with no checkpoints yields `{}`.

    Each entry carries Package A's own unmodified `state` PLUS the additive
    `claim_liveness` field (see
    `reconstruct_checkpoint_state_with_liveness`), so a session resumed
    after a claimant crash can tell a stranded `claimed` checkpoint from a
    live one instead of reporting both identically forever. `now` is
    threaded through to the classifier for deterministic time input."""
    return {
        checkpoint_id: reconstruct_checkpoint_state_with_liveness(
            session_uuid, checkpoint_id, now=now)
        for checkpoint_id in state_store.list_checkpoint_ids(session_uuid)
    }


if __name__ == "__main__":
    sys.exit(main())
