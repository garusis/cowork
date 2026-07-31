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
                  evidence_poll_delay_s=None, output_cap_bytes=None):
    """Build the versioned JSON request document persisted before the worker
    is spawned (see `cowork_state.verification_request_path_for`).

    `configuration` is a caller-supplied, already-normalized dict (e.g. team/
    role config relevant to verification); it is included verbatim in the
    single-flight request key so a configuration change always mints a new
    key. Nothing here performs I/O; the caller writes the returned dict with
    `cowork_state.write_json_atomic`.
    """
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
        if not _wait_for_permit(session_uuid, transaction_id, index,
                                attempt_id, should_stop):
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


def _read_worker_identity(session_uuid, transaction_id, timeout_s=10,
                          poll_delay_s=0.2, sleep=time.sleep, now=time.time,
                          proc=None):
    """Poll for the worker's self-reported identity, up to `timeout_s`.

    ORCH-030 fix: without `proc`, a worker that crashes immediately (e.g. a
    missing snapshotted import) was indistinguishable from a healthy worker
    that simply hadn't reported yet — the parent waited out the ENTIRE
    startup allowance either way. When `proc` is given, this checks
    `proc.poll()` on every iteration and returns as soon as the process has
    exited with no identity on file, instead of continuing to poll a dead
    process for the full timeout. Returns the identity dict, or `None` on
    timeout/no-report (the caller distinguishes "still running, gave up"
    from "already exited" via `proc.poll()` itself after this returns).
    """
    identity_path = state_store.verification_worker_identity_path_for(
        session_uuid, transaction_id)
    deadline = now() + timeout_s
    while now() < deadline:
        identity = state_store.read_json_tolerant(identity_path)
        if identity:
            return identity
        if proc is not None and proc.poll() is not None:
            # The process is already gone and still never reported identity
            # — no amount of further polling will change that. Give the
            # filesystem one last, very short grace window in case identity
            # and process-exit raced (identity write completing just as the
            # process was reaped), then stop.
            sleep(min(poll_delay_s, 0.05))
            return state_store.read_json_tolerant(identity_path)
        sleep(poll_delay_s)
    return None


def verify_worker_identity(identity, snapshot_manifest, worker_file_rel):
    """True only when the worker's self-reported source hash matches the
    snapshot manifest's entry for its own file AND its protocol version
    matches ours. Any mismatch (including a missing report) means the
    transaction is UNVERIFIED — never accepted on faith."""
    if not isinstance(identity, dict):
        return False
    if identity.get("protocol_version") != PROTOCOL_VERSION:
        return False
    entry = (snapshot_manifest or {}).get(worker_file_rel)
    if not entry or entry.get("type") != "file":
        return False
    return identity.get("source_hash") == entry.get("sha256")


def _poll_attempt_events(events_path, seen_count):
    """Read new lines from the attempt-events stream past `seen_count`.
    Returns `(events, new_seen_count)`."""
    events = state_store.read_jsonl_tolerant(events_path)
    return events[seen_count:], len(events)


# Hard cap on the worker's captured startup stdout+stderr — BOTH what gets
# written to disk (the WRITE side, `_capture_startup_log`) and what a
# reader will ever pull into memory afterward (the READ side,
# `_read_worker_startup_log`). A worker's normal contract is to write
# nothing here at all; this exists solely to catch a crash-before-identity
# traceback, never to be a general-purpose log sink, so a generous but
# finite cap (well beyond any real traceback) is correct, not a
# functional loss.
MAX_STARTUP_LOG_BYTES = 64 * 1024  # 64 KiB


def _capture_startup_log(pipe_fh, log_path, max_bytes):
    """Read `pipe_fh` (the worker's merged stdout+stderr) to completion,
    retaining a bounded TAIL/RING of AT MOST `max_bytes` — the LAST bytes
    the child wrote, not the first. A worker's real diagnostic value (a
    Python traceback) is almost always at the very END of its output; a
    "keep the first N bytes, drop the rest" policy would keep whatever
    boilerplate or noise came first and discard the traceback entirely for
    any output longer than the cap. This bounds MEMORY too — the rolling
    buffer never holds more than `max_bytes` (+ up to one 4 KiB chunk
    mid-trim) at any point, regardless of how much the child ultimately
    writes — and is flushed to disk exactly ONCE, after the pipe reaches
    EOF (the child has exited), so a reader that waits for this thread to
    finish (see `spawn_worker`'s returned thread, joined before
    `_read_worker_startup_log`) always sees the complete final tail, never
    a partial write.

    Runs in a daemon thread; the pipe is closed when the child exits and
    this function returns."""
    buf = bytearray()
    try:
        while True:
            chunk = pipe_fh.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                del buf[:len(buf) - max_bytes]
        with open(log_path, "wb") as out:
            out.write(bytes(buf))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe_fh.close()
        except OSError:
            pass


def spawn_worker(python_executable, checkout_root, request_path,
                 session_uuid=None, transaction_id=None):
    """Spawn `python3 <checkout_root>/scripts/cowork_verification.py --worker
    <request_path>` with DEVNULL stdin, in a new process group/session
    (`start_new_session=True`), and a liveness pipe whose write end the
    parent holds and the worker's read end it inherits. Returns `(proc,
    liveness_write_fd, capture_thread)`; the caller closes
    `liveness_write_fd` to signal shutdown/cancel and the worker's
    watchdog thread observes EOF. `capture_thread` is `None` when no
    `session_uuid`/`transaction_id` was given (nothing to capture into);
    otherwise the caller MUST join it (bounded) before reading the
    startup log — see `_read_worker_startup_log` — since the log is only
    flushed to disk once, when this thread finishes.

    `PYTHONDONTWRITEBYTECODE=1` is set so the bootstrap checkout never gets
    `__pycache__` written into it (which would itself be a mutation of an
    "immutable" snapshot directory) — the checkout is writable at the
    OS-permission level (cleanup must still be able to remove it), so this
    env var is the actual guard, not filesystem read-only enforcement.

    stdout/stderr are captured via a PIPE and a background thread
    (`_capture_startup_log`), bounded to `MAX_STARTUP_LOG_BYTES` on disk —
    NOT connected directly to an unbounded on-disk file (`Popen(stdout=
    open(path))` writes however much the child produces with no cap at
    all) — so a worker that crashes before ever reporting identity (an
    ImportError from a missing snapshotted module, say) leaves the parent
    a genuinely bounded, structured diagnostic instead of an unbounded
    disk write.
    """
    worker_script = os.path.join(checkout_root, "scripts",
                                 "cowork_verification.py")
    read_fd, write_fd = os.pipe()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    capture_log_path = None
    if session_uuid and transaction_id:
        capture_log_path = state_store.verification_worker_startup_log_path_for(
            session_uuid, transaction_id)
        os.makedirs(os.path.dirname(capture_log_path), exist_ok=True)
    stdout_target = subprocess.PIPE if capture_log_path else subprocess.DEVNULL
    stderr_target = (subprocess.STDOUT if capture_log_path
                     else subprocess.DEVNULL)
    try:
        proc = subprocess.Popen(
            [python_executable, worker_script, "--worker", request_path,
             "--liveness-fd", str(read_fd)],
            cwd=checkout_root, stdin=subprocess.DEVNULL,
            stdout=stdout_target, stderr=stderr_target,
            start_new_session=True, env=env,
            pass_fds=(read_fd,), close_fds=True)
    except BaseException:
        # `Popen` raising means no worker process exists to ever hold or
        # observe the write end either — the caller never gets `write_fd`
        # back to close it themselves, so leaving it open here leaks a
        # file descriptor for the lifetime of the parent process. Close
        # BOTH ends on this path before propagating.
        os.close(read_fd)
        os.close(write_fd)
        raise
    else:
        os.close(read_fd)
    capture_thread = None
    if capture_log_path and proc.stdout is not None:
        capture_thread = threading.Thread(
            target=_capture_startup_log,
            args=(proc.stdout, capture_log_path, MAX_STARTUP_LOG_BYTES),
            daemon=True)
        capture_thread.start()
    return proc, write_fd, capture_thread


def _read_worker_startup_log(session_uuid, transaction_id, max_bytes=4096):
    """Bounded tail of the worker's captured startup stdout/stderr, for a
    structured `startup_failure` reason. Bounded on BOTH ends: the file
    itself is already capped at `MAX_STARTUP_LOG_BYTES` by the writer
    (`_capture_startup_log`), and this reads at most `max_bytes` off disk
    via `seek` — never the whole file into memory first — before
    truncating. Never raises — a log that cannot be read yields an
    explicit note rather than blocking failure reporting."""
    log_path = state_store.verification_worker_startup_log_path_for(
        session_uuid, transaction_id)
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                data = b"...(truncated)...\n" + fh.read(max_bytes)
            else:
                data = fh.read(max_bytes)
    except OSError:
        return "(startup log unavailable)"
    return data.decode("utf-8", "replace")


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


def bounded_evidence_wait(session_uuid, transaction_id, expected_labels,
                          poll_attempts=DEFAULT_EVIDENCE_POLL_ATTEMPTS,
                          poll_delay_s=DEFAULT_EVIDENCE_POLL_DELAY_S,
                          sleep=time.sleep):
    """Poll the worker's attempt-events stream for terminal events for every
    label in `expected_labels`, for a BOUNDED number of attempts. On the
    original attempt id, revises to terminal state as evidence arrives; past
    the bound, writes an explicit `unresolved`/`absent` terminal state for
    whatever is still missing and STOPS POLLING — it never re-launches a
    command.

    Returns `{label: terminal_event_or_synthetic_unresolved}`.
    """
    events_path = state_store.verification_attempt_events_path_for(
        session_uuid, transaction_id)
    terminal_by_label = {}
    attempt = 0
    while attempt < poll_attempts and len(terminal_by_label) < len(
            expected_labels):
        events = state_store.read_jsonl_tolerant(events_path)
        for ev in events:
            if ev.get("event") == "terminal" and ev.get("label") in (
                    expected_labels or ()):
                terminal_by_label[ev["label"]] = ev
        if len(terminal_by_label) >= len(expected_labels):
            break
        attempt += 1
        if attempt < poll_attempts:
            sleep(poll_delay_s)
    for label in expected_labels or ():
        if label not in terminal_by_label:
            terminal_by_label[label] = {
                "event": "terminal", "label": label,
                "evidence_state": EVIDENCE_UNRESOLVED,
                "exit_code": None, "note": "evidence not observed within "
                "the bounded poll; the underlying command was never "
                "re-launched",
            }
        else:
            terminal_by_label[label].setdefault(
                "evidence_state", EVIDENCE_PRESENT)
    return terminal_by_label


# =========================================================================== #
# Section 10: top-level orchestrator entry point.                             #
# =========================================================================== #


def run_transaction(repo, session_uuid, raw_verification, configuration=None,
                    python_executable=None, command_timeout_s=None,
                    term_grace_s=None, waiter_deadline_s=None,
                    evidence_poll_attempts=None, evidence_poll_delay_s=None,
                    cancel_event=None):
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
        evidence_poll_delay_s=evidence_poll_delay_s)
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


def _revise_attempt_ledger(ledger_path, transaction_id, label, fields,
                           attempt_state):
    """The one call site every ledger revision in `_run_owned_transaction`
    goes through: appends under the pre-minted id, and returns whether that
    succeeded AND landed under the expected id (never a mismatched or
    freshly-minted one — `revise_owned_attempt` now fails closed with
    `None` rather than minting a replacement, see `cowork_ledger.py`).
    Returns `(record_or_None, ok)`."""
    record = ledger.revise_owned_attempt(
        ledger_path, transaction_id, label, fields,
        attempt_state=attempt_state)
    return record, record is not None


def _revise_attempt_ledger_with_retry(ledger_path, transaction_id, label,
                                      fields, attempt_state, attempts=2,
                                      delay_s=0.05, sleep=time.sleep):
    """`_revise_attempt_ledger` with a small BOUNDED retry — used by the
    lifecycle backstop specifically, where a genuinely TRANSIENT failure
    (a momentary write glitch that clears on the very next attempt) must
    not be reported as the same kind of persistent `ledger_failure` as a
    real, lasting one. `revise_owned_attempt` is naturally idempotent
    (finds the canonical id by key and appends a fresh revision under it
    each call), so retrying here never risks a duplicate id or a lost
    revision — worst case it appends more than one `attempt_state`-
    identical revision, which downstream readers already treat as "the
    latest one wins". Still fails closed: if EVERY attempt fails, this
    reports failure exactly like the non-retrying version, honestly."""
    record = None
    ok = False
    for i in range(max(1, attempts)):
        record, ok = _revise_attempt_ledger(
            ledger_path, transaction_id, label, fields, attempt_state)
        if ok:
            return record, ok
        if i < attempts - 1:
            sleep(delay_s)
    return record, ok


def _wait_for_attempt_and_revise_ledger(
        session_uuid, transaction_id, entry, request, ledger_path,
        overall_deadline, timeout_policy, snapshot_manifest_digest,
        bounded_evidence_wait_fn=None):
    """Wait for one entry's terminal evidence (primary execution-bound wait,
    then the short evidence_retry_policy only if still absent — see
    `_execution_wait_budget_s`), then revise the SAME pre-minted ledger id
    with whatever was observed — covering evidence that arrived on time,
    evidence that was DELAYED (resolved only by the secondary wait), and
    evidence that expired UNRESOLVED/ABSENT, all through this one call site.

    `bounded_evidence_wait_fn` is injectable (defaults to the real
    `bounded_evidence_wait`) so tests can control exactly what each wait
    phase observes without racing a real subprocess's timing.

    Returns `(attempt_dict, ledger_ok)`. `ledger_ok=False` means the
    revision failed or landed under an unexpected id — FAIL CLOSED: the
    caller must not treat this attempt as trustworthy evidence.
    """
    wait_fn = bounded_evidence_wait_fn or bounded_evidence_wait
    label = entry["label"]
    execution_budget_s = _execution_wait_budget_s(timeout_policy)
    remaining_overall = max(0.0, overall_deadline - time.time())
    primary_wait_s = min(execution_budget_s, remaining_overall)
    primary_poll_delay_s = 1.0
    primary_poll_attempts = (
        int(primary_wait_s // primary_poll_delay_s) + 1
        if primary_wait_s > 0 else 0)
    terminal = wait_fn(
        session_uuid, transaction_id, [label],
        poll_attempts=primary_poll_attempts,
        poll_delay_s=primary_poll_delay_s)
    attempt = terminal.get(label, {})
    if attempt.get("evidence_state") != EVIDENCE_PRESENT:
        # Execution should have ended by now — evidence is still missing.
        # THIS is where the plan's short evidence_retry_policy applies: one
        # more bounded poll for evidence that is merely slow to land, before
        # concluding it is genuinely absent.
        retry_policy = request.get("evidence_retry_policy") or {}
        terminal = wait_fn(
            session_uuid, transaction_id, [label],
            poll_attempts=retry_policy.get(
                "poll_attempts", DEFAULT_EVIDENCE_POLL_ATTEMPTS),
            poll_delay_s=retry_policy.get(
                "poll_delay_s", DEFAULT_EVIDENCE_POLL_DELAY_S))
        attempt = terminal.get(label, {})
    attempt["kind"] = entry.get("kind")
    attempt["ledger_attempt_id"] = entry.get("ledger_attempt_id")
    for meta_key in ("invalidation_reason", "reuse_decision",
                    "triggering_finding", "marginal_cost"):
        if meta_key in entry:
            attempt[meta_key] = entry[meta_key]

    evidence_state = attempt.get("evidence_state")
    exit_code = attempt.get("exit_code")
    timed_out = bool(attempt.get("timed_out"))
    if evidence_state == EVIDENCE_PRESENT:
        if timed_out:
            exit_status, adjudication = "timeout", "fail"
        elif exit_code == 0:
            exit_status, adjudication = "pass", "pass"
        else:
            exit_status, adjudication = "fail", "fail"
        revise_state = "terminal"
    else:
        exit_status, adjudication = "unknown", (
            "unresolved" if evidence_state == EVIDENCE_UNRESOLVED
            else "unknown")
        revise_state = "unresolved"
    record, ledger_ok = _revise_attempt_ledger(
        ledger_path, transaction_id, label,
        fields={
            # Owned-transaction-native fields.
            "exit_code": exit_code,
            "evidence_state": evidence_state,
            "timed_out": timed_out,
            "wall_time_s": attempt.get("wall_time_s"),
            "verification_kind": entry.get("kind"),
            # Compatible with the legacy attempt shape
            # (`cowork_ledger._ATTEMPT_FIELDS`) so the SAME record can be
            # read by code written against that vocabulary: `exit_status`/
            # `adjudication` (not raw `exit_code`), `command_fingerprint`,
            # `started_at`/`ended_at`, and `observed_source_digest` (the
            # exact tree this attempt ran against).
            "exit_status": exit_status,
            "adjudication": adjudication,
            "command_fingerprint": " ".join(entry.get("command") or []),
            "started_at": attempt.get("started_at"),
            "ended_at": attempt.get("ended_at"),
            "observed_source_digest": snapshot_manifest_digest,
        },
        attempt_state=revise_state)
    if not ledger_ok:
        attempt["evidence_state"] = EVIDENCE_UNRESOLVED
        attempt["ledger_revision_failed"] = True
    elif record.get("id") != entry.get("ledger_attempt_id"):
        # Defensive: a revision that landed under a DIFFERENT id than the
        # one minted for this entry is exactly the "mismatched revision"
        # this contract must fail closed on.
        ledger_ok = False
        attempt["evidence_state"] = EVIDENCE_UNRESOLVED
        attempt["ledger_revision_mismatch"] = True
    return attempt, ledger_ok


def _run_owned_transaction(repo, session_uuid, transaction_id, request,
                           entries, final_suite_label, snapshot,
                           checkout_root, python_executable,
                           cancel_event=None):
    manifest_doc = state_store.read_json_tolerant(snapshot["manifest_path"])
    manifest_files = (manifest_doc or {}).get("files", {})
    worker_rel_path = os.path.join("scripts", "cowork_verification.py")
    ledger_path = state_store.ledger_path_for(session_uuid)

    request_path = state_store.verification_request_path_for(
        session_uuid, transaction_id)
    proc, liveness_write_fd, startup_capture_thread = spawn_worker(
        python_executable, checkout_root, request_path,
        session_uuid=session_uuid, transaction_id=transaction_id)

    timeout_policy = request.get("timeout_policy") or {}
    overall_deadline = time.time() + _overall_deadline_s(
        entries, timeout_policy)

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

    identity = _read_worker_identity(session_uuid, transaction_id,
                                     timeout_s=timeout_policy.get(
                                         "startup_allowance_s")
                                     or DEFAULT_STARTUP_ALLOWANCE_S,
                                     proc=proc)
    worker_verified = verify_worker_identity(
        identity, manifest_files, worker_rel_path)
    startup_failure = None
    if not worker_verified and identity is None:
        # ORCH-030: distinguish "the worker process already exited without
        # ever reporting identity" (a real startup failure, with a captured
        # reason) from "still running, gave up waiting" — `_read_worker_
        # identity` already returns promptly for the former instead of
        # burning the whole startup allowance.
        exit_code = proc.poll()
        if exit_code is not None:
            # The process has exited, so its stdout pipe has already
            # delivered EOF to `_capture_startup_log` — but that thread
            # still needs to finish draining/writing before its file is
            # complete. Join it (bounded — this must never itself hang the
            # transaction) BEFORE reading, so `log_tail` is never read
            # from a partially-written file.
            if startup_capture_thread is not None:
                startup_capture_thread.join(timeout=5)
            startup_failure = {
                "reason": ("request_rejected"
                          if exit_code == WORKER_EXIT_REQUEST_REJECTED
                          else "worker_exited_before_identity_report"),
                "exit_code": exit_code,
                "log_tail": _read_worker_startup_log(
                    session_uuid, transaction_id),
            }

    attempts = []
    mutation = None
    verdict = VERDICT_UNVERIFIED
    final_suite_binding = "not_reached"
    ledger_failure = None
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
                if cancel_event is not None and cancel_event.is_set():
                    deadline_hit = True
                    break
                if time.time() > overall_deadline:
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
                attempt, ledger_ok = _wait_for_attempt_and_revise_ledger(
                    session_uuid, transaction_id, entry, request,
                    ledger_path, overall_deadline, timeout_policy,
                    snapshot["manifest_digest"])
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
                    break
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
        cleanup_active_command_group(session_uuid, transaction_id,
                                     term_grace_s=timeout_policy.get(
                                         "term_grace_s")
                                     or DEFAULT_TERM_GRACE_S)
        terminate_worker(proc, liveness_write_fd,
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


if __name__ == "__main__":
    sys.exit(main())
